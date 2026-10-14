#!/usr/bin/env python3
"""Validation-only work owned by the existing native sync supervisor.

Only governed inventory/reference maintenance; no provider calls or business extraction. Work is
bound to committed data and code, with durable timeout-only retry accounting.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import validation_evidence as evidence
import validation_progress as progress
import validation_process

ROOT = Path(__file__).resolve().parents[1]
MAX_ATTEMPTS = 3
RETRY_SECONDS = 60
STABLE_SECONDS = 60
DEFER_DELAYS = (60, 120, 300, 600)
TERMINAL = {"failed", "interrupted", "blocked", "superseded"}


class ValidationFault(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def valid_accounting(job: dict[str, Any]) -> bool:
    if not job:
        return True
    count = job.get("attempt_count", 0)
    if job.get("schema_version") not in {1, 2} or type(count) is not int or count < 0 or (job.get("schema_version") == 1 and count > MAX_ATTEMPTS):
        return False
    if job.get("schema_version") == 2:
        for field in ("timeout_count", "defer_count"):
            value = job.get(field, 0)
            if type(value) is not int or value < 0 or (field == "timeout_count" and value > MAX_ATTEMPTS):
                return False
        if job.get("timeout_count", 0) > count or job.get("defer_count", 0) > count:
            return False
        if job.get("status") == "retry_wait" and (parsed(job.get("next_retry_at")) is None or not 0 < job.get("timeout_count", 0) < MAX_ATTEMPTS or job.get("error_code") != "automatic_validation_timeout"):
            return False
        if job.get("status") == "waiting_stable" and (job.get("timeout_count", 0) >= MAX_ATTEMPTS or any(parsed(job.get(key)) is None for key in ("pending_since", "next_retry_at"))):
            return False
    if not job.get("input_key"):
        return count == 0 and not job.get("status")
    key = str(job["input_key"])
    if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
        return False
    if str(job.get("status")) not in TERMINAL | {"running", "retry_wait", "verified", "waiting_stable"}:
        return False
    if not isinstance(job.get("validation_context"), dict):
        return False
    if count:
        try:
            uuid.UUID(str(job["run_id"]))
            uuid.UUID(str(job["job_id"]))
        except (KeyError, ValueError, TypeError):
            return False
    elif job.get("status") not in {"verified", "waiting_stable"} and not (job.get("schema_version") == 2 and job.get("status") in {"failed", "blocked", "interrupted"}):
        return False
    return True


def read_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValidationFault("automatic_validation_state_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError) as exc:
        raise ValidationFault("automatic_validation_state_invalid") from exc
    if not isinstance(value, dict):
        raise ValidationFault("automatic_validation_state_invalid")
    return value


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def try_lock(path: Path) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        acquired = False
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield handle if acquired else None
        finally:
            if acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)


def stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def parsed(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def index_state(root: Path) -> dict[str, Any]:
    path = root / "state/bridge.sqlite"
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
            connection.execute("PRAGMA query_only=ON")
            values = dict(connection.execute("SELECT key,value FROM maintenance_state WHERE key IN "
                                             "('index_dirty_generation','index_applied_generation','index_last_error')"))
        dirty, applied = int(values["index_dirty_generation"]), int(values["index_applied_generation"])
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        raise ValidationFault("memory_index_state_unavailable") from exc
    if applied < 0 or dirty < applied:
        raise ValidationFault("memory_index_state_invalid")
    if values.get("index_last_error"):
        raise ValidationFault("memory_index_update_failed")
    return {"dirty": dirty, "applied": applied, "ready": dirty == applied}


def boot_identity() -> str:
    try:
        linux = Path("/proc/sys/kernel/random/boot_id")
        if linux.is_file():
            return linux.read_text().strip()
        return subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class AutomaticValidation:
    def __init__(self, root: Path = ROOT, *, clock: Callable[[], datetime] | None = None,
                 runner: Callable[..., Any] | None = None):
        self.root = root.resolve()
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.runner = runner or self.run_validator
        self.boot_id = boot_identity()
        self.state_path = self.root / "state/automatic-memory-validation.json"
        self.history_path = self.root / "state/automatic-memory-validation-history.jsonl"
        self.success_path = self.root / "state/last-memory-validation-success.json"
        self.failure_path = self.root / "state/last-memory-validation-failure.json"

    def maintenance_active(self) -> bool:
        return read_object(self.root / "state/maintenance-mode.json").get("status") != "inactive"

    def save(self, job: dict[str, Any], **updates: Any) -> dict[str, Any]:
        defaults = {"timeout_count": int(job.get("attempt_count") or 0) if job.get("schema_version") == 1 and job.get("error_code") == "automatic_validation_timeout" else 0, "defer_count": 0}
        updated = {**defaults, **job, **updates, "schema_version": 2}
        if updated == job:
            return job
        updated["updated_at"] = stamp(self.clock())
        atomic_json(self.state_path, updated)
        # Append only aggregate evidence.  No source text, IDs, credentials or
        # current-state bookkeeping are added to the business snapshot.
        event = {key: updated.get(key) for key in (
            "updated_at", "status", "input_key", "index_generation", "job_id", "run_id",
            "attempt_count", "started_at", "completed_at", "error_code", "next_retry_at",
            "validation_receipt_at", "preflight_error", "execution_pid",
            "pending_since", "stable_since", "observed_at", "defer_count", "timeout_count",
        )}
        descriptor = os.open(self.history_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return updated

    def publish_failure(self, job: dict[str, Any], code: str, errors: Any = None) -> None:
        count = job.get("attempt_count", 0)
        import validation_failure_receipts
        validation_failure_receipts.publish(self.failure_path, {
            "recorded_at": stamp(self.clock()), "verdict": "fail", "error_code": code,
            "origin": "automatic_full_validation", "phase": job.get("phase") or "validation",
            "validation_context": job.get("validation_context"), "run_id": job.get("run_id"),
            "input_key": job.get("input_key"), "index_generation": job.get("index_generation"),
            "attempt_count": count if type(count) is int and count >= 0 else None,
            "errors": errors[:50] if isinstance(errors, list) else [code],
        })

    def preflight_failed(self, job: dict[str, Any], code: str) -> dict[str, Any]:
        # Repeated denied/invalid probes do not reset a prior job or its budget.
        try:
            current = read_object(self.state_path)
            if current:
                job = current
        except (ValidationFault, OSError):
            code = "automatic_validation_state_invalid"
        if code == "automatic_validation_state_invalid":
            # Keep damaged accounting intact for diagnosis.  Replacing it with
            # an empty job would turn corruption into a fresh retry budget.
            try:
                previous_failure = read_object(self.failure_path)
            except (ValidationFault, OSError):
                previous_failure = {}
            if previous_failure.get("error_code") != code:
                self.publish_failure(job, code)
            return {"status": "blocked", "error_code": code}
        if job.get("preflight_error") != code:
            job = self.save(job, preflight_error=code)
            self.publish_failure(job, code)
        return {"status": "blocked", "error_code": code, "attempt_count": job.get("attempt_count", 0)}

    def run_validator(self, config: dict[str, Any], run_id: str, descriptors: tuple[int, ...]) -> Any:
        timeout, slow = validation_process.settings(config)
        environment = {**os.environ, "BUSINESS_MEMORY_INDEX_LOCK_HELD": "1", "BUSINESS_VALIDATION_RUN_ID": run_id,
                       "BUSINESS_AUTOMATIC_FULL_VALIDATION": "1"}
        environment.pop("BUSINESS_SEMANTIC_WRITE_IN_PROGRESS", None)
        def mark_slow():
            try:
                job = read_object(self.state_path)
                if job.get("run_id") == run_id and job.get("status") == "running":
                    self.save(job, validation_slow=True, slow_since=stamp(self.clock()))
            except (OSError, ValidationFault):
                if progress.current(): progress.current().emit("slow_status_unavailable")
        with progress.stage("full_validator"):
            return validation_process.run(
                ["/usr/bin/ruby", str(self.root / "bin/validate_memory.rb"),
                 str(config["memory_root"]), str(config["external_root"])],
                timeout=timeout, slow_after=slow, env=environment, pass_fds=descriptors, on_slow=mark_slow,
            )

    def tick(self) -> dict[str, Any]:
        with progress.Progress(self.root, "automatic"):
            return self._tick()

    def _tick(self) -> dict[str, Any]:
        with try_lock(self.root / "state/automatic-memory-validation.lock") as owner_lock:
            if owner_lock is None:
                return {"status": "deferred", "error_code": "validation_already_running"}
            job: dict[str, Any] = {}
            try:
                if self.maintenance_active():
                    return {"status": "deferred", "error_code": "maintenance_active"}
                import win11_precommit_closure
                closure = win11_precommit_closure.drain(self.root)
                if closure["status"] in {"deferred", "blocked"}:
                    return closure
                job = read_object(self.state_path)
                if (self.state_path.exists() and not job) or not valid_accounting(job):
                    raise ValidationFault("automatic_validation_state_invalid")
                index = index_state(self.root)
                if not index["ready"]:
                    return {"status": "pending", "error_code": "memory_index_update_pending"}
                with try_lock(self.root / "state/memory-index.lock") as memory_lock:
                    if memory_lock is None:
                        return {"status": "deferred", "error_code": "memory_core_busy"}
                    if self.maintenance_active():
                        return {"status": "deferred", "error_code": "maintenance_active"}
                    index = index_state(self.root)
                    if not index["ready"]:
                        return {"status": "pending", "error_code": "memory_index_update_pending"}
                    with progress.stage("both_locks_held"):
                        return self.validate_current(job, index, (owner_lock.fileno(), memory_lock.fileno()))
            except evidence.InputsChanged:
                current = read_object(self.state_path) or job
                if (not current.get("input_key") or current.get("status") in TERMINAL | {"running"}
                        or int(current.get("timeout_count") or 0) >= MAX_ATTEMPTS):
                    return {"status": "deferred", "error_code": "validation_inputs_changed"}
                return self.save(current, status="waiting_stable", stable_since=None, observed_at=None,
                                 pending_since=current.get("pending_since") or stamp(self.clock()),
                                 error_code="validation_inputs_changed",
                                 next_retry_at=stamp(self.clock()+timedelta(seconds=STABLE_SECONDS)))
            except PermissionError:
                return self.preflight_failed(job, "validation_permission_denied")
            except subprocess.TimeoutExpired:
                return self.preflight_failed(job, "source_inventory_refresh_timeout")
            except ValidationFault as exc:
                return self.preflight_failed(job, exc.code)
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                return self.preflight_failed(job, "validation_context_unavailable")

    def validate_current(self, job: dict[str, Any], index: dict[str, Any], descriptors: tuple[int, ...]) -> dict[str, Any]:
        config = read_object(self.root / "config.json")
        try:
            validation_process.settings(config)
        except ValueError as exc:
            raise ValidationFault("automatic_validation_config_invalid") from exc
        with progress.stage("outer_snapshot"):
            context = evidence.context(self.root)
        if progress.current(): progress.current().code_sha256 = context["code_sha256"]
        input_key = hashlib.sha256(json.dumps([index, context], sort_keys=True).encode()).hexdigest()
        same_input = job.get("input_key") == input_key
        success = read_object(self.success_path)
        failure = read_object(self.failure_path)
        success_at, failure_at = parsed(success.get("recorded_at")), parsed(failure.get("recorded_at"))
        request = read_object(self.root / "state/validation-request.json")
        request_at = parsed(request.get("requested_at"))
        if request and (request.get("schema_version") != 1 or request_at is None):
            raise ValidationFault("automatic_validation_state_invalid")
        current_receipt = evidence.receipt_matches_context(success, index["dirty"], index["applied"], context)["current"]
        if current_receipt and success_at and (not request_at or success_at > request_at) and (not failure or (failure_at and success_at > failure_at)):
            # A genuine manual validation may also complete pending work.  A
            # prior exit code or a stale receipt can never do so.
            return self.save(job if same_input else {}, status="verified", input_key=input_key,
                             index_generation=index["dirty"], validation_context=context,
                             attempt_count=int(job.get("attempt_count") or 0) if same_input else 0,
                             error_code=None, preflight_error=None, next_retry_at=None,
                             validation_receipt_at=success["recorded_at"])
        if job.get("status") == "running":
            # Both locks are now free; the old owner did not record completion.
            # The child inherits the locks, so an orphan still running cannot
            # reach this branch.  Interruption never resets retry accounting.
            job = self.save(job, status="interrupted", error_code="automatic_validation_interrupted",
                            completed_at=stamp(self.clock()), next_retry_at=None)
            self.publish_failure(job, "automatic_validation_interrupted")
            return job
        if same_input and job.get("status") in TERMINAL and job.get("error_code") != "validation_inputs_changed":
            return job
        # Keep an entire waiting episode across input churn. A real terminal
        # failure still needs genuinely changed input before a new job is allowed.
        continuing = job.get("status") in {"waiting_stable", "retry_wait", "superseded"}
        if not continuing:
            job = {}
        now = self.clock()
        timeout_count = int(job.get("timeout_count", job.get("attempt_count", 0) if job.get("error_code") == "automatic_validation_timeout" else 0))
        retry_at = parsed(job.get("next_retry_at"))
        stable_at = parsed(job.get("stable_since")) if same_input and job.get("boot_id") == self.boot_id and self.boot_id != "unknown" else None
        observed_at = parsed(job.get("observed_at"))
        # A sleep/reboot or missed probes does not prove a quiet interval.
        if observed_at and (now < observed_at or now - observed_at > timedelta(seconds=max(180, DEFER_DELAYS[-1] + 120))):
            stable_at = None
        if job.get("status") == "superseded":
            stable_at = None
        stable_at = stable_at or now
        eligible_at = max(stable_at + timedelta(seconds=STABLE_SECONDS), retry_at or now)
        if now < eligible_at:
            return self.save(job, status="waiting_stable", input_key=input_key,
                             validation_context=context, index_generation=index["dirty"],
                             attempt_count=int(job.get("attempt_count") or 0), timeout_count=timeout_count,
                             pending_since=job.get("pending_since") or stamp(now),
                             stable_since=stamp(stable_at), observed_at=stamp(now), boot_id=self.boot_id,
                             error_code="validation_inputs_changed", next_retry_at=stamp(eligible_at))
        if timeout_count >= MAX_ATTEMPTS:
            return self.save(job, status="failed", error_code="automatic_validation_timeout", next_retry_at=None)
        if config.get("source_governance", {}).get("inventory_auto_refresh") is True and job.get("inventory_input_key") != input_key:
            job = self.save(job, status="waiting_stable", phase="inventory",
                            input_key=input_key, validation_context=context, index_generation=index["dirty"],
                            attempt_count=int(job.get("attempt_count") or 0), timeout_count=timeout_count,
                            pending_since=job.get("pending_since") or stamp(now), stable_since=stamp(stable_at),
                            observed_at=stamp(now), boot_id=self.boot_id, next_retry_at=stamp(now))
            with progress.stage("source_inventory_maintenance"):
                try:
                    environment = {**os.environ, "BUSINESS_MEMORY_INDEX_LOCK_HELD": "1", "LC_ALL": "en_US.UTF-8"}
                    result = subprocess.run(
                        ["/usr/bin/ruby", str(self.root / "bin/refresh_internal_memory_ledger.rb"),
                         "--inventory-only", str(Path(config["memory_root"]).parent), str(config["external_root"])],
                        capture_output=True, text=True, timeout=120, env=environment, pass_fds=descriptors,
                    )
                    if result.returncode != 0:
                        raise ValidationFault("source_inventory_refresh_failed")
                    registration = json.loads(result.stdout)
                    if registration.get("references_updated"):
                        refresh = subprocess.run(
                            ["/usr/bin/ruby", str(self.root / "bin/refresh_memory_indexes.rb")],
                            capture_output=True, text=True, timeout=120, env=environment, pass_fds=descriptors,
                        )
                        if refresh.returncode != 0:
                            raise ValidationFault("memory_index_update_failed")
                    index = index_state(self.root)
                except (subprocess.TimeoutExpired, ValidationFault, OSError, ValueError) as exc:
                    code = "source_inventory_refresh_timeout" if isinstance(exc, subprocess.TimeoutExpired) else getattr(exc, "code", "source_inventory_refresh_failed")
                    job = self.save(job, status="failed", phase="inventory", error_code=code, completed_at=stamp(self.clock()), next_retry_at=None)
                    self.publish_failure(job, code)
                    return job
                after_inventory = evidence.context(self.root)
            inventory_key = hashlib.sha256(json.dumps([index, after_inventory], sort_keys=True).encode()).hexdigest()
            job = self.save(job, inventory_input_key=inventory_key, phase=None)
            if inventory_key != input_key:
                return self.save(job, status="waiting_stable", input_key=inventory_key,
                                 validation_context=after_inventory, index_generation=index["dirty"],
                                 stable_since=stamp(self.clock()), observed_at=stamp(self.clock()),
                                 error_code="validation_inputs_changed",
                                 next_retry_at=stamp(self.clock() + timedelta(seconds=STABLE_SECONDS)))
        run_id = progress.current().run_id if progress.current() else str(uuid.uuid4())
        job = self.save(job, status="running", input_key=input_key,
                        validation_context=context, index_generation=index["dirty"],
                        job_id=job.get("job_id") or str(uuid.uuid4()), run_id=run_id,
                        execution_mode="supervisor_in_process", execution_pid=os.getpid(),
                        attempt_count=int(job.get("attempt_count") or 0) + 1,
                        timeout_count=timeout_count, pending_since=job.get("pending_since") or stamp(now),
                        stable_since=stamp(stable_at), observed_at=stamp(now), boot_id=self.boot_id,
                        started_at=stamp(now), completed_at=None, error_code=None, validation_slow=False, slow_since=None,
                        full_validation_timeout_seconds=validation_process.settings(config)[0], slow_after_seconds=validation_process.settings(config)[1],
                        preflight_error=None, next_retry_at=None)
        payload: dict[str, Any] = {}
        code = None
        try:
            result = self.runner(config, run_id, descriptors)
            if "validation_progress_unavailable" in (result.stderr or "") and progress.current():
                progress.current().emit("child_diagnostics_incomplete")
            try:
                payload = json.loads(result.stdout)
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
            except (ValueError, TypeError):
                code = "validation_output"
                payload = {}
            if code is None:
                if payload.get("error_code") == "validation_inputs_changed" and payload.get("verdict") == "deferred":
                    code = "validation_inputs_changed"
                elif result.returncode != 0 or payload.get("verdict") != "pass" or payload.get("errors"):
                    supplied_code = str(payload.get("error_code") or "")
                    code = supplied_code if supplied_code in {
                        "validation_permission_denied", "validation_context_unavailable"} else "validation_failed"
        except subprocess.TimeoutExpired:
            code = "automatic_validation_timeout"
        except PermissionError:
            code = "validation_permission_denied"
        except validation_process.CleanupError as exc:
            code = str(exc)
        except OSError:
            code = "validation_command_unavailable"
        current_job = read_object(self.state_path)
        if current_job.get("run_id") == run_id:
            job = current_job
        # Check even a failure: source churn is not evidence of corruption.
        try:
            with progress.stage("receipt_snapshot"):
                after = evidence.context(self.root)
        except evidence.InputsChanged:
            code, after = "validation_inputs_changed", context
        except PermissionError:
            code, after = "validation_permission_denied", context
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            code, after = "validation_context_unavailable", context
        if code not in {"validation_permission_denied", "validation_context_unavailable", "validation_output", "validation_command_unavailable", "validation_process_cleanup_unproven", "validation_descendants_after_exit"} and (after != context or code == "validation_inputs_changed"):
            count = int(job.get("defer_count") or 0) + 1
            return self.save(job, status="waiting_stable", completed_at=stamp(self.clock()),
                             stable_since=None, observed_at=None, defer_count=count,
                             error_code="validation_inputs_changed",
                             next_retry_at=stamp(self.clock() + timedelta(seconds=DEFER_DELAYS[min(count - 1, len(DEFER_DELAYS) - 1)])))
        if code is None:
            with progress.stage("receipt_confirmation"):
                receipt = read_object(self.success_path)
            if (receipt.get("run_id") != run_id or not parsed(receipt.get("recorded_at"))
                    or not evidence.receipt_matches_context(receipt, index["dirty"], index["applied"], after)["current"]):
                code = "validation_receipt_unproven"
            else:
                return self.save(job, status="verified", completed_at=stamp(self.clock()), error_code=None,
                                 validation_receipt_at=receipt["recorded_at"], next_retry_at=None)
        timeout_count = int(job.get("timeout_count") or 0) + int(code == "automatic_validation_timeout")
        retrying = code == "automatic_validation_timeout" and timeout_count < MAX_ATTEMPTS
        job = self.save(job, status="retry_wait" if retrying else "failed", timeout_count=timeout_count, completed_at=stamp(self.clock()),
                        error_code=code, next_retry_at=stamp(self.clock() + timedelta(seconds=RETRY_SECONDS)) if retrying else None)
        self.publish_failure(job, code, payload.get("errors"))
        return job


def run_once() -> dict[str, Any]:
    # The native LaunchAgent owns this call.  It is not exposed as an arbitrary
    # command endpoint, and does not enqueue another background interpreter.
    from local_sync_repair import supervisor_heartbeat
    with supervisor_heartbeat():
        return AutomaticValidation().tick()
