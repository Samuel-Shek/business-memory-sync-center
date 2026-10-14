#!/usr/bin/env python3
"""A private durable inbox independent of the supervisor's SQLite database."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import json
import os
import re
import tempfile
import subprocess
from pathlib import Path
from typing import Any

import business_sync_state as state
from sqlite_read_diagnostics import error_evidence


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".incident-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inbox(root: Path) -> Path:
    return root / "state" / "supervisor-incidents"


def retain(root: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    # Callers supply content-free evidence only; history import uses a separate
    # allowlist instead of copying an arbitrary historic record.
    identity = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    path = inbox(root) / (identity + ".json")
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    value = {"schema_version": 1, "incident_id": identity, "evidence": evidence,
             "status": "pending_registration", "root_cause_status": "pending",
             "notification_state": "durably_pending", "alert_id": None}
    atomic_json(path, value)
    return value


def import_history(root: Path) -> int:
    path = root / "state" / "sync-supervisor-errors.jsonl"
    if not path.is_file():
        return 0
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if len(line) > 64000:
                raise ValueError("supervisor_history_record_invalid")
            raw = json.loads(line)
            if not isinstance(raw, dict) or state.parse_time(raw.get("recorded_at")) is None:
                raise ValueError("supervisor_history_record_invalid")
            safe = {"recorded_at": raw["recorded_at"],
                    "error_type": re.sub(r"[^A-Za-z0-9_]", "_", str(raw.get("error_type") or "unknown"))[:80],
                    "reason": re.sub(r"[^A-Za-z0-9_]", "_", str(raw.get("reason") or "unknown"))[:80],
                    "pid": raw.get("pid") if type(raw.get("pid")) is int else None}
            frames = []
            for frame in raw.get("frames", [])[-16:]:
                name = str(frame.get("file") or "")
                frames.append({"file": name if re.fullmatch(r"bin/[A-Za-z0-9_]+\.py", name) else "external_dependency",
                               "function": re.sub(r"[^A-Za-z0-9_]", "_", str(frame.get("function") or ""))[:80],
                               "line": frame.get("line") if type(frame.get("line")) is int else None})
                digest = frame.get("loaded_code_sha256")
                if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                    frames[-1]["loaded_code_sha256"] = digest
            context = raw.get("process_context")
            if isinstance(context, dict):
                safe["process_context"] = {k: context[k] for k in ("pid", "ppid", "thread_id") if type(context.get(k)) is int}
                token = context.get("process_token")
                if isinstance(token, str) and re.fullmatch(r"[0-9a-f]{32}", token):
                    safe["process_context"]["process_token"] = token
            safe["frames"] = frames
            from index_incident_evidence import CODES, TYPES
            if raw.get('fault_code') in CODES:
                safe['fault_code'] = raw['fault_code']
            if raw.get('cause_type') in TYPES | {'OtherError'}:
                safe['cause_type'] = raw['cause_type']
            if type(raw.get('sqlite_code')) is int:
                safe['sqlite_code'] = raw['sqlite_code']
            # The main recorder owns this typed, content-free field.
            if isinstance(raw.get("sqlite"), dict):
                safe["sqlite"] = raw["sqlite"]
            retain(root, safe)
            count += 1
    return count


def retain_database_outage(root: Path, exc: Exception, database: Path) -> dict[str, Any]:
    path = inbox(root) / "database-outage.json"
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8"))
        identity = str(previous.get("incident_id") or "")
        incident_path = inbox(root) / (identity + ".json")
        # Deduplicate only the unresolved episode. A verified older outage
        # must not suppress durable evidence for a later database failure.
        if not re.fullmatch(r"[0-9a-f]{64}", identity) or not incident_path.is_file():
            return previous
        incident = json.loads(incident_path.read_text(encoding="utf-8"))
        if incident.get("root_cause_status") != "verified" and incident.get("recovery_status") != "verified":
            return previous
    safe = {"recorded_at": state.now_iso(), "error_type": type(exc).__name__,
            "reason": "sentinel_database_unavailable", "sqlite": error_evidence(exc, database, "sentinel_initialize")}
    value = retain(root, safe)
    atomic_json(path, {"incident_id": value["incident_id"], "recorded_at": safe["recorded_at"],
                       "status": "durably_pending", "database_available": False})
    return value


def register_pending(root: Path, database: Path | None = None) -> int:
    registered = 0
    for path in sorted(inbox(root).glob("*.json")):
        if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("status") != "pending_registration":
            continue
        evidence = value["evidence"]
        alert = state.open_alert(
            "system", "supervisor_runtime_error", "后台异常仍需闭环",
            "后台发生过异常；当前进程正常不代表该故障已修复。原记录已保留，正在核对原因与恢复证据。",
            severity="error", error_code="supervisor_runtime_error_pending", database=database,
            at=state.parse_time(evidence["recorded_at"]), incident_id=value["incident_id"],
        )
        value.update(status="registered", alert_id=alert["alert_id"],
                     registered_at=state.now_iso(), notification_state="registered_pending_delivery")
        atomic_json(path, value)
        registered += 1
    return registered


def unresolved(root: Path) -> int:
    return sum(1 for path in inbox(root).glob("*.json")
               if re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
               and json.loads(path.read_text(encoding="utf-8")).get("root_cause_status") != "verified"
               and json.loads(path.read_text(encoding="utf-8")).get("recovery_status") != "verified")


def code_binding(root: Path) -> dict[str, str]:
    names = ("business_sync_supervisor.py", "business_sync_state.py", "sqlite_read_diagnostics.py", "supervisor_incidents.py",
             "bounded_repair_actions.py", "dingtalk_sync_v2.py", "operational_workbench.py", "sqlite_write_transactions.py", "sqlite_attribution.py", "sqlite_lock_probe.py", "automatic_memory_validation.py", "validation_evidence.py",
             "validate_memory.rb", "validation_progress.py", "validation_process.py", "validation_progress.rb",
             "ledger_recovery.py", "business_sync_supervisor_health.py", "media_actions.py", "media_sources.py")
    names += ("supervisor_liveness.py", "supervisor_worker_pool.py", "supervisor_worker_progress.py", "business_sync_controller.py")
    names += tuple(n for n in ('index_runtime_recovery.py', 'index_incident_evidence.py', 'validation_retry.py', 'receiver.py', 'handoff_context.py', 'sync_repair_rules.py', 'sync_repair_contract.py', 'sync_center_autonomy.py', 'sync_rule_runtime.py', 'supervisor_health_recovery.py') if (root / 'bin' / n).is_file())
    return {name: hashlib.sha256((root / "bin" / name).read_bytes()).hexdigest() for name in names}


def record_attention_success(root: Path, *, loaded_supervisor_sha256=None) -> None:
    """A passed attention reconciliation, not just a live PID or heartbeat."""
    path = root / "state" / "supervisor-attention-receipts.json"
    previous = json.loads(path.read_text()) if path.is_file() else {"schema_version": 1, "receipts": []}
    receipt = {"checked_at": state.now_iso(), "pid": os.getpid(), "ppid": os.getppid(),
               "attention_reconciliation": "passed", "code_binding": code_binding(root)}
    import supervisor_liveness
    receipt["process_identity"] = supervisor_liveness.process_identity(os.getpid())
    receipt["loaded_supervisor_sha256"] = loaded_supervisor_sha256
    native = root / "bin/BUSINESS Sync Supervisor.app/Contents/MacOS/business-sync-supervisor-launcher"
    try:
        probe = subprocess.run(["/bin/ps", "-p", str(os.getppid()), "-o", "ppid=", "-o", "comm="],
                               capture_output=True, text=True, timeout=3, check=False)
        parent = probe.stdout.strip().split(None, 1)
        native_owner = probe.returncode == 0 and len(parent) == 2 and parent[0] == "1" and parent[1] == str(native)
        receipt["execution_owner"] = "launchd_native_supervisor" if native_owner else "other_or_unverified"
        receipt["native_binary_sha256"] = hashlib.sha256(native.read_bytes()).hexdigest() if native_owner else None
    except (OSError, subprocess.TimeoutExpired):
        receipt["execution_owner"] = "unverified"
    previous["receipts"] = (previous["receipts"] + [receipt])[-64:]
    atomic_json(path, previous)


def verify_correction(root: Path, database: Path | None = None) -> int:
    if (root / "state/ledger-selfheal-policy.json").is_file():
        import ledger_recovery
        try:
            if (root / 'state/index-recovery-policy.json').is_file():
                import index_runtime_recovery
                return index_runtime_recovery.verify(root, Path(database or state.DATABASE))
            return ledger_recovery.verify_incidents(root, Path(database or state.DATABASE))
        except (OSError, ValueError, KeyError, TypeError):
            # Missing/malformed evidence cannot close an event or stop the sentinel.
            return 0
    if (root / "state/supervisor-contention-correction-proof.json").is_file():
        return verify_contention_correction(root, database)
    proof_path = root / "state" / "supervisor-correction-proof.json"
    receipts_path = root / "state" / "supervisor-attention-receipts.json"
    if not proof_path.is_file() or not receipts_path.is_file():
        return 0
    proof = json.loads(proof_path.read_text())
    if (proof.get("schema_version") != 1 or proof.get("reason") != "wal_read_connection_corrected"
            or proof.get("code_binding") != code_binding(root)
            or proof.get("isolated_reproduction") is not True or proof.get("query_only_verified") is not True
            or proof.get("live_integrity_check") != "ok" or not re.fullmatch(r"[a-f0-9]{64}", str(proof.get("evidence_sha256") or ""))):
        return 0
    after = state.parse_time(proof.get("deployed_at"))
    if after is None:
        return 0
    last_error = root / "state" / "sync-supervisor-last-error.json"
    if last_error.is_file():
        error_at = state.parse_time(json.loads(last_error.read_text()).get("recorded_at"))
        if error_at and error_at > after:
            return 0
    observations = [row for row in json.loads(receipts_path.read_text()).get("receipts", [])
                    if row.get("attention_reconciliation") == "passed" and row.get("execution_owner") == "launchd_native_supervisor"
                    and row.get("native_binary_sha256") == proof.get("native_binary_sha256")
                    and row.get("code_binding") == proof["code_binding"]
                    and (state.parse_time(row.get("checked_at")) or after) > after]
    # Two separate natural launchd cycles, not two calls inside the same loop.
    by_pid = {row["pid"]: row for row in observations}
    if len(by_pid) < 2:
        return 0
    observations = sorted(by_pid.values(), key=lambda row: row["checked_at"])
    if (state.parse_time(observations[-1]["checked_at"]) - state.parse_time(observations[0]["checked_at"])).total_seconds() < 45:
        return 0
    verified = 0
    for identity in proof.get("incident_ids", []):
        if not re.fullmatch(r"[a-f0-9]{64}", str(identity)):
            continue
        path = inbox(root) / (identity + ".json")
        if not path.is_file():
            continue
        incident = json.loads(path.read_text())
        evidence = incident["evidence"]
        at = state.parse_time(evidence.get("recorded_at"))
        if (incident.get("root_cause_status") == "verified" or evidence.get("reason") != "sqlite_database_unavailable"
                or at is None or at >= after or not any(frame.get("function") == "alerts" for frame in evidence.get("frames", []))):
            continue
        incident.update(root_cause_status="verified", correction_proof_sha256=hashlib.sha256(proof_path.read_bytes()).hexdigest(),
                        verified_at=state.now_iso(), verification_receipts=observations[-2:])
        atomic_json(path, incident)
        verified += 1
    if verified and unresolved(root) == 0:
        state.resolve_alert("system", "supervisor_runtime_error", title="后台异常已取得修复证据",
                            detail="已复现并修正数据库读取方式；完整性检查通过，两个自然后台周期已完成原故障路径。旧失败记录保留。",
                            database=database)
    return verified


def verify_contention_correction(root: Path, database: Path | None = None) -> int:
    """Close only explicitly bound incidents after real post-deploy acceptance.

    Uses the existing health sentinel, with no new scheduler or business replay.
    Pending maintenance, stale evidence, missing files or a new error leave the
    incidents untouched. Historical lock ownership remains unknown.
    """
    import automatic_memory_validation as validation
    import validation_evidence
    proof_path = root / "state/supervisor-contention-correction-proof.json"
    try:
        proof = json.loads(proof_path.read_text())
        if (proof.get("schema_version") != 1 or proof.get("reason") != "bounded_ledger_contention_corrected"
                or proof.get("isolated_reproduction") is not True
                or proof.get("code_binding") != code_binding(root)):
            return 0
        maintenance = validation.read_object(root / "state/maintenance-mode.json")
        if maintenance.get("status") != "inactive":
            return 0
        connection = state._connect(database, readonly=True)
        try:
            alert = connection.execute("SELECT alert_id,status FROM sync_alerts WHERE dedupe_key='system:supervisor_runtime_error'").fetchone()
        finally:
            connection.close()
        if not alert or alert["alert_id"] != proof.get("alert_id") or alert["status"] != "open":
            return 0
        deployed = state.parse_time(proof.get("deployed_at"))
        if deployed is None:
            return 0
        tests = proof.get("test_evidence", [])
        if not tests or any(type(t.get("checks")) is not int or t["checks"] <= 0
                            or hashlib.sha256(Path(t["path"]).read_bytes()).hexdigest() != t.get("sha256") for t in tests):
            return 0
        latest_error = validation.read_object(root / "state/sync-supervisor-last-error.json")
        error_at = state.parse_time(latest_error.get("recorded_at"))
        if error_at is None or error_at > deployed:
            return 0
        rows = validation.read_object(root / "state/supervisor-attention-receipts.json").get("receipts", [])
        cycles = {r["pid"]: r for r in rows
                  if r.get("attention_reconciliation") == "passed"
                  and r.get("execution_owner") == "launchd_native_supervisor"
                  and r.get("native_binary_sha256") == proof.get("native_binary_sha256")
                  and r.get("code_binding") == proof["code_binding"]
                  and type(r.get("pid")) is int
                  and (state.parse_time(r.get("checked_at")) or deployed) > deployed}
        observations = sorted(cycles.values(), key=lambda r: r["checked_at"])
        if (len(observations) < 2 or
                (state.parse_time(observations[-1]["checked_at"]) - state.parse_time(observations[0]["checked_at"])).total_seconds() < 45):
            return 0
        identities = proof.get("incident_ids", [])
        if not identities or len(set(identities)) != len(identities):
            return 0
        targets = []
        for identity in identities:
            if not re.fullmatch(r"[a-f0-9]{64}", str(identity)):
                return 0
            path = inbox(root) / (identity + ".json")
            incident = json.loads(path.read_text())
            evidence = incident["evidence"]
            at = state.parse_time(evidence.get("recorded_at"))
            if (incident.get("alert_id") != proof.get("alert_id") or at is None or at >= deployed
                    or evidence.get("reason") != "sqlite_database_busy"
                    or hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest() != identity):
                return 0
            targets.append((path, incident))
        # Match source-controller lock order. Do not wait while holding a lock.
        with validation.try_lock(root / "state/sync-center-control.lock") as control:
            if control is None:
                return 0
            with validation.try_lock(root / "state/memory-index.lock") as index_lock:
                if index_lock is None:
                    return 0
                success = validation.read_object(root / "state/last-memory-validation-success.json")
                success_at = state.parse_time(success.get("recorded_at"))
                if success_at is None or success_at <= deployed:
                    return 0
                # A newer failed validation supersedes an older success even
                # when their input hashes happen to match. Malformed failure
                # evidence is likewise not permission to close an incident.
                failure = validation.read_object(root / "state/last-memory-validation-failure.json")
                failure_at = state.parse_time(failure.get("recorded_at"))
                if failure and (failure_at is None or failure_at >= success_at):
                    return 0
                index = validation.index_state(root)
                if not validation_evidence.receipt_is_current(root, success, index["dirty"], index["applied"]):
                    return 0
                current_error = validation.read_object(root / "state/sync-supervisor-last-error.json")
                if current_error != latest_error or code_binding(root) != proof["code_binding"]:
                    return 0
                native = root / "bin/BUSINESS Sync Supervisor.app/Contents/MacOS/business-sync-supervisor-launcher"
                if hashlib.sha256(native.read_bytes()).hexdigest() != proof.get("native_binary_sha256"):
                    return 0
                # A later incident on the same aggregate is never closed by an old proof.
                unresolved_ids = {p.stem for p in inbox(root).glob("*.json")
                                  if re.fullmatch(r"[a-f0-9]{64}", p.stem)
                                  and json.loads(p.read_text()).get("root_cause_status") != "verified"}
                if unresolved_ids - set(identities):
                    return 0
                connection = state._connect(database, readonly=True)
                try:
                    alert = connection.execute("SELECT alert_id,status FROM sync_alerts WHERE dedupe_key='system:supervisor_runtime_error'").fetchone()
                finally:
                    connection.close()
                if not alert or alert["alert_id"] != proof.get("alert_id") or alert["status"] != "open":
                    return 0
                verified = 0
                for path, incident in targets:
                    if incident.get("root_cause_status") == "verified":
                        continue
                    incident.update(root_cause_status="verified", verified_at=state.now_iso(),
                                    recovery_kind="bounded_ledger_contention_recovery_verified",
                                    historical_lock_owner="unknown", verification_receipts=[observations[0], observations[-1]],
                                    full_validation_receipt_at=success["recorded_at"],
                                    correction_proof_sha256=hashlib.sha256(proof_path.read_bytes()).hexdigest())
                    atomic_json(path, incident)
                    verified += 1
                state.resolve_alert("system", "supervisor_runtime_error", title="后台数据库争用已通过恢复验收",
                                    detail="局部事务重试与隔离回归通过，部署后的完整记忆校验及自然后台核对通过。原始失败保留，历史占锁者未记录。",
                                    database=database)
                return verified
    except (OSError, ValueError, KeyError, TypeError, validation.ValidationFault):
        return 0
