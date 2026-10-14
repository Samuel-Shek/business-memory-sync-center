#!/usr/bin/env python3
"""Fail-closed maintenance control for every BUSINESS memory writer.

The controller preserves the pre-maintenance loaded state.  It never enables a
writer that was not loaded when ``enter`` began, and it refuses maintenance if
an unregistered LaunchAgent points at the governed memory bridge.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import json
import os
import plistlib
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("BUSINESS_BRIDGE_ROOT", _archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync')))
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "maintenance-mode.json"
LOCK_PATH = ROOT / "state" / "maintenance-mode.lock"
REVIEW_LOCK_PATHS = (
    Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-NonObsidian.nosync/dingtalk-sync-v2/state/review-stage.lock')),
    Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/核对/.state/dispatch.lock')),
)
QUEUE_DIRS = (ROOT / "watch_inbox", ROOT / "inbox")
PROCESS_MARKERS = (
    "/bin/receiver.py",
    "/bin/folder_watcher.py",
    "/bin/win11_semantic_merge.rb",
    "/bin/win11_memory_automation.py",
    "/bin/dingtalk_memory_automation.py",
    "/bin/dingtalk_minutes_memory.py",
    "/bin/dingtalk_sync_orchestrator.py",
    "/bin/business_sync_controller.py",
    "/bin/business_sync_supervisor.py",
    "business-sync-supervisor-launcher --run-once",
    "/bin/local_sync_repair.py",
    "/bin/sync_dingtalk_chat.py",
    "/bin/email_sync.py",
    "/bin/email_phase_worker.py",
    "/bin/dingtalk_idle_review.py",
    "/bin/dingtalk_review_reconcile.py",
    "/bin/run_dingtalk_memory_listener.sh",
    "/bin/run_dingtalk_memory_hourly.sh",
    "/bin/run_dingtalk_chat_sync.sh",
    "business-dingtalk-memory-hourly-launcher",
    "/bin/run_wechat_pipeline.sh",
    "/bin/collect_wechat_chat.py",
    "/bin/process_wechat_media.py",
    "/bin/wechat_ai_queue.py",
    "/bin/refresh_memory_indexes.rb",
    "/bin/refresh_internal_memory_ledger.rb",
    "/bin/validate_memory.rb",
    "business-memory-receiver-launcher",
    "business-win11-watcher-launcher",
    "/bin/business_sync_now.sh",
    "tailscale file get --loop",
)
EXTERNAL_PROCESS_MARKERS = (
    "/AI/LocalData/DingTalk-AI-Minutes.nosync/sync_minutes.py",
    "/DingTalk-AI-Minutes.nosync/核对/.pipeline/review_dispatch.py",
    "/DingTalk-AI-Minutes.nosync/核对/.pipeline/run_review.sh",
)
REVIEW_PROCESS_MARKERS = (
    "/DingTalk-AI-Minutes.nosync/核对/.pipeline/review_dispatch.py",
    "/DingTalk-AI-Minutes.nosync/核对/.pipeline/run_review.sh",
    "/bin/dingtalk_review_reconcile.py",
    "/transcription-review/scripts/transcribe_dashscope.py",
)
RECEIVER_HELPER_MARKERS = (
    "/bin/refresh_memory_indexes.rb",
    "/bin/validate_memory.rb",
)


class MaintenanceError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_config() -> dict[str, Any]:
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    writers = data.get("memory_writers")
    if not isinstance(writers, list) or not writers:
        raise MaintenanceError("config memory_writers must be a non-empty list")
    labels: set[str] = set()
    for writer in writers:
        if not isinstance(writer, dict):
            raise MaintenanceError("every memory writer must be an object")
        label = str(writer.get("label") or "")
        plist_path = Path(str(writer.get("plist_path") or ""))
        process_markers = writer.get("process_markers")
        if not label or label in labels:
            raise MaintenanceError(f"invalid or duplicate writer label: {label!r}")
        if not plist_path.is_file() or plist_path.is_symlink():
            raise MaintenanceError(f"writer plist is missing or unsafe: {plist_path}")
        if not isinstance(process_markers, list) or not process_markers or not all(
            isinstance(marker, str) and marker for marker in process_markers
        ):
            raise MaintenanceError(f"writer process markers are missing or invalid: {label}")
        labels.add(label)
    return data


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def launch_loaded(label: str) -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", f"{launch_domain()}/{label}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def parse_launch_enabled_states(output: str) -> dict[str, bool]:
    return {
        label: state == "enabled"
        for label, state in re.findall(r'"([^"]+)"\s*=>\s*(enabled|disabled)', output)
    }


def launch_enabled_states() -> dict[str, bool]:
    result = subprocess.run(
        ["/bin/launchctl", "print-disabled", launch_domain()],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MaintenanceError(f"cannot read launchd enable state: {result.stderr.strip()[:240]}")
    return parse_launch_enabled_states(result.stdout)


def launch_enabled(label: str, states: dict[str, bool] | None = None) -> bool:
    # launchd omits labels that have never had an override; omission means the
    # service is enabled by default.
    effective_states = launch_enabled_states() if states is None else states
    return effective_states.get(label, True)


def set_launch_enabled(label: str, enabled: bool) -> None:
    if enabled and __import__("owner_retirement").stopped():
        raise MaintenanceError("owner retirement forbids enabling corporate services")
    verb = "enable" if enabled else "disable"
    result = subprocess.run(
        ["/bin/launchctl", verb, f"{launch_domain()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or launch_enabled(label) != enabled:
        detail = (result.stderr or result.stdout).strip()[:240]
        raise MaintenanceError(f"failed to {verb} {label}: {detail}")


def bootout(writer: dict[str, Any]) -> None:
    label = str(writer["label"])
    if not launch_loaded(label):
        return
    result = subprocess.run(
        ["/bin/launchctl", "bootout", launch_domain(), str(writer["plist_path"])],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and launch_loaded(label):
        raise MaintenanceError(f"failed to stop {label}: {(result.stderr or result.stdout).strip()[:240]}")


def bootstrap(writer: dict[str, Any]) -> None:
    label = str(writer["label"])
    if launch_loaded(label):
        return
    result = subprocess.run(
        ["/bin/launchctl", "bootstrap", launch_domain(), str(writer["plist_path"])],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and not launch_loaded(label):
        raise MaintenanceError(f"failed to restore {label}: {(result.stderr or result.stdout).strip()[:240]}")


def plist_command(path: Path) -> tuple[str, str]:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return "", ""
    label = str(payload.get("Label") or "")
    arguments = payload.get("ProgramArguments") or []
    command = " ".join(str(value) for value in arguments) if isinstance(arguments, list) else ""
    return label, command


def discovered_writer_labels(config: dict[str, Any]) -> set[str]:
    explicit_markers = (
        "receiver.py",
        "folder_watcher.py",
        "win11_semantic_merge.rb",
        "win11_memory_automation.py",
        "dingtalk_memory_automation.py",
        "dingtalk_minutes_memory.py",
        "dingtalk_sync_orchestrator.py",
        "sync_dingtalk_chat.py",
        "dingtalk_decision_center.py",
        "dingtalk_idle_review.py",
        "business_sync_supervisor.py",
        "business-sync-supervisor-launcher --run-once",
        "business_sync_now.sh",
        "business-memory-receiver-launcher",
        "business-win11-watcher-launcher",
    )
    writer_source_markers = (
        '"memory_root"',
        "refresh_memory_indexes",
        "validate_memory",
        "win11_semantic_merge",
    )
    discovered: set[str] = set()
    launch_root = Path.home() / "Library" / "LaunchAgents"
    for path in launch_root.glob("*.plist"):
        label, command = plist_command(path)
        if not label:
            continue
        is_writer = any(marker in command for marker in explicit_markers)
        if not is_writer:
            for argument in command.split():
                candidate = Path(argument)
                try:
                    if not candidate.is_file() or ROOT not in candidate.parents:
                        continue
                    source = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if any(marker in source for marker in writer_source_markers):
                    is_writer = True
                    break
        if is_writer:
            discovered.add(label)
    return discovered


def unregistered_candidates(config: dict[str, Any], discovered: set[str] | None = None) -> set[str]:
    registered = {str(writer["label"]) for writer in config["memory_writers"]}
    return (discovered if discovered is not None else discovered_writer_labels(config)) - registered


def queue_counts() -> dict[str, int]:
    return {
        str(path): sum(1 for candidate in path.rglob("*") if candidate.is_file()) if path.exists() else 0
        for path in QUEUE_DIRS
    }


def wait_for_empty_queues(timeout: float = 60.0) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    while True:
        counts = queue_counts()
        if all(value == 0 for value in counts.values()):
            return counts
        if time.monotonic() >= deadline:
            raise MaintenanceError(f"receiver queues did not drain: {counts}")
        time.sleep(0.5)


def wait_for_receiver_helpers(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        helpers = [
            (pid, command)
            for pid, command in matching_processes()
            if any(marker in command for marker in RECEIVER_HELPER_MARKERS)
        ]
        if not helpers:
            return
        if time.monotonic() >= deadline:
            raise MaintenanceError(f"receiver helpers did not finish: {[pid for pid, _ in helpers]}")
        time.sleep(0.25)


def is_writer_process(command: str) -> bool:
    bridge_writer = str(ROOT) in command and any(marker in command for marker in PROCESS_MARKERS)
    external_writer = any(marker in command for marker in EXTERNAL_PROCESS_MARKERS)
    review_child = any(marker in command for marker in REVIEW_PROCESS_MARKERS) or (
        "codex exec" in command and "/DingTalk-AI-Minutes.nosync/核对/" in command
    )
    return bridge_writer or external_writer or review_child


def is_review_process(command: str) -> bool:
    return any(marker in command for marker in REVIEW_PROCESS_MARKERS) or (
        "codex exec" in command and "/DingTalk-AI-Minutes.nosync/核对/" in command
    )


def registered_process_labels(config: dict[str, Any], command: str) -> list[str]:
    labels = {
        str(writer["label"])
        for writer in config["memory_writers"]
        if any(str(marker) in command for marker in writer.get("process_markers") or [])
    }
    if is_review_process(command):
        labels.update(
            str(writer["label"])
            for writer in config["memory_writers"]
            if str(writer.get("label")) == "com.example.dingtalk-minutes-review"
        )
    return sorted(labels)


def matching_processes(include_receiver: bool = True) -> list[tuple[int, str]]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True
    )
    matches: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid() or not is_writer_process(command):
            continue
        if not include_receiver and (
            "receiver.py" in command
            or "receiver-launcher" in command
            or "tailscale file get" in command
            or any(marker in command for marker in RECEIVER_HELPER_MARKERS)
        ):
            continue
        matches.append((pid, command))
    return matches


def unregistered_writer_processes(
    config: dict[str, Any], processes: list[tuple[int, str]] | None = None
) -> list[tuple[int, str]]:
    return [
        (pid, command)
        for pid, command in (matching_processes() if processes is None else processes)
        if not registered_process_labels(config, command)
    ]


def terminate_processes(include_receiver: bool) -> None:
    matches = matching_processes(include_receiver=include_receiver)
    for pid, _command in matches:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        remaining = matching_processes(include_receiver=include_receiver)
        if not remaining:
            return
        time.sleep(0.25)
    remaining = matching_processes(include_receiver=include_receiver)
    for pid, _command in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.25)
    remaining = matching_processes(include_receiver=include_receiver)
    if remaining:
        raise MaintenanceError(f"writer processes remain after stop: {[pid for pid, _ in remaining]}")


def active_review_processes() -> list[tuple[int, str]]:
    return [(pid, command) for pid, command in matching_processes() if is_review_process(command)]


def acquire_review_quiescence_barriers(timeout: float = 4 * 3600):
    """Block new review work and wait for the current paid item to finish naturally."""
    deadline = time.monotonic() + timeout
    last_pids: list[int] = []
    while time.monotonic() < deadline:
        handles = []
        locked = True
        for path in REVIEW_LOCK_PATHS:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                locked = False
                break
            handles.append(handle)
        if not locked:
            for handle in reversed(handles):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            last_pids = [pid for pid, _ in active_review_processes()]
            time.sleep(1)
            continue
        active = active_review_processes()
        if not active:
            return handles
        last_pids = [pid for pid, _ in active]
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        time.sleep(1)
    raise MaintenanceError(
        "current paid review did not finish before maintenance timeout; "
        f"left running without force-stop: {last_pids}"
    )


def release_lock_handles(handles) -> None:
    for handle in reversed(handles):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def wait_for_stable_process_exit(timeout: float = 5.0, stable_for: float = 1.0) -> None:
    """Require a continuously quiet interval after stopping writer processes.

    A launchd parent or orchestration wrapper can disappear just before its
    final child does.  A single instantaneous process scan therefore produces
    false maintenance failures at exactly the point where enter should be
    idempotent.  This short stability window still fails closed if anything
    persists or respawns.
    """
    deadline = time.monotonic() + timeout
    quiet_since: float | None = None
    last_seen: list[tuple[int, str]] = []
    while time.monotonic() < deadline:
        last_seen = matching_processes()
        if last_seen:
            quiet_since = None
        else:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= stable_for:
                return
        time.sleep(0.1)
    raise MaintenanceError(f"writer processes remain after stop: {[pid for pid, _ in last_seen]}")


def database_health(config: dict[str, Any]) -> dict[str, str]:
    paths = {
        "bridge": ROOT / "state" / "bridge.sqlite",
        "semantic": Path(str(config["semantic_database"])),
    }
    health: dict[str, str] = {}
    for name, path in paths.items():
        if not path.is_file() or path.is_symlink():
            raise MaintenanceError(f"{name} database is missing or unsafe: {path}")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if name == "bridge":
                tables = {
                    str(row[0])
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "maintenance_state" not in tables:
                    raise MaintenanceError("bridge database lacks maintenance_state")
                state = {
                    str(row[0]): str(row[1])
                    for row in connection.execute("SELECT key,value FROM maintenance_state")
                }
                if state.get("index_dirty_generation") != state.get("index_applied_generation"):
                    raise MaintenanceError(
                        "bridge index generation is not fully applied: "
                        f"{state.get('index_dirty_generation')} != {state.get('index_applied_generation')}"
                    )
                if state.get("index_last_error"):
                    raise MaintenanceError(f"bridge index refresh failed: {state['index_last_error'][:240]}")
        finally:
            connection.close()
        if result != "ok":
            raise MaintenanceError(f"{name} database quick_check failed: {result}")
        health[name] = result
    return health


def bridge_index_state() -> dict[str, str]:
    """Read the receiver's index generation without mutating the database."""
    path = ROOT / "state" / "bridge.sqlite"
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT key,value FROM maintenance_state "
                "WHERE key IN ('index_dirty_generation','index_applied_generation','index_last_error')"
            )
        }
    finally:
        connection.close()


def index_state_current(state: dict[str, str]) -> bool:
    return (
        bool(state.get("index_dirty_generation"))
        and state.get("index_dirty_generation") == state.get("index_applied_generation")
        and not state.get("index_last_error")
    )


def reconcile_indexes_offline() -> dict[str, str]:
    """Use the receiver's canonical refresh path after a blocked, fully quiet enter.

    A receiver can commit the final dequeued watch event just before maintenance
    stops it, leaving the debounced index generation one step behind.  Reusing
    receiver.refresh_dirty_indexes keeps the database transition identical to
    the live path and lets a repeated ``enter`` converge without restarting all
    producers.
    """
    if any(queue_counts().values()):
        raise MaintenanceError("cannot reconcile indexes offline while receiver queues are non-empty")
    import receiver as receiver_module

    receiver_lock = receiver_module.acquire_receiver_lock()
    try:
        with receiver_module.acquire_memory_write_lock():
            connection = receiver_module.connect_db()
            try:
                receiver_module.refresh_dirty_indexes(
                    connection, force=True, debounce_seconds=0
                )
            finally:
                connection.close()
    finally:
        receiver_lock.close()
    state = bridge_index_state()
    if not index_state_current(state):
        raise MaintenanceError(
            "offline index reconciliation did not converge: "
            f"dirty={state.get('index_dirty_generation')} "
            f"applied={state.get('index_applied_generation')} "
            f"error={state.get('index_last_error')!r}"
        )
    return state


def acquire_index_quiescence_barrier(timeout: float = 60.0):
    """Hold the shared writer lock only after queues and indexes are coherent.

    Queue emptiness alone is not a safe boundary: the receiver may already have
    dequeued an event and still be committing it.  Acquiring the same lock used
    for event commits and index refreshes closes that race.  The caller keeps
    the handle until the receiver has been stopped.
    """
    path = ROOT / "state" / "memory-index.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    last_state: dict[str, str] = {}
    last_queues: dict[str, int] = {}
    while time.monotonic() < deadline:
        handle = path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            time.sleep(0.25)
            continue
        last_queues = queue_counts()
        last_state = bridge_index_state()
        if not any(last_queues.values()) and index_state_current(last_state):
            return handle
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        time.sleep(0.25)
    raise MaintenanceError(
        "receiver did not reach an index-coherent stop boundary: "
        f"queues={last_queues} dirty={last_state.get('index_dirty_generation')} "
        f"applied={last_state.get('index_applied_generation')} "
        f"error={last_state.get('index_last_error')!r}"
    )


def lock_health() -> str:
    paths = [
        ROOT / "state" / name
        for name in ("memory-index.lock", "win11_memory_merge.lock", "receiver.lock", "folder_watcher.lock")
    ] + list(REVIEW_LOCK_PATHS)
    handles = []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                raise MaintenanceError(f"maintenance lock point is busy: {path}") from exc
            handles.append(handle)
        return "ok"
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def read_state() -> dict[str, Any] | None:
    if not STATE_PATH.is_file():
        return None
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"maintenance state is unreadable: {exc}") from exc
    return data if isinstance(data, dict) else None


def status_payload(config: dict[str, Any]) -> dict[str, Any]:
    state = read_state() or {"status": "inactive"}
    loaded = {str(writer["label"]): launch_loaded(str(writer["label"])) for writer in config["memory_writers"]}
    enabled_states = launch_enabled_states()
    return {
        "status": state.get("status", "inactive"),
        "retirement_active": retirement_active(config),
        "owner_status": "owner_retired" if retirement_active(config) else None,
        "entered_at": state.get("entered_at"),
        "reason": state.get("reason"),
        "loaded": loaded,
        "enabled": {
            str(writer["label"]): launch_enabled(str(writer["label"]), enabled_states)
            for writer in config["memory_writers"]
        },
        "queues": queue_counts(),
        "writer_processes": [
            {"pid": pid, "labels": registered_process_labels(config, command), "command": command}
            for pid, command in matching_processes()
        ],
        "unregistered_writer_processes": [
            {"pid": pid, "command": command} for pid, command in unregistered_writer_processes(config)
        ],
        "unregistered_writers": sorted(unregistered_candidates(config)),
    }


def launcher_quiescence_drift(payload: dict[str, Any]) -> list[str]:
    """Return registered launchers that could still run during maintenance."""
    labels = set(payload.get("loaded") or {}) | set(payload.get("enabled") or {})
    return sorted(
        label
        for label in labels
        if bool((payload.get("loaded") or {}).get(label))
        or bool((payload.get("enabled") or {}).get(label))
    )


def retirement_active(config: dict[str, Any]) -> bool:
    # A missing/corrupt marker after retirement is never an instruction to restore.
    return __import__("owner_retirement").stopped()


def retire(config: dict[str, Any], reason: str) -> dict[str, Any]:
    """Owner retirement: seal intake, preserve queues, wait for atomic work, never restore."""
    existing = read_state() or {}
    if existing.get("status") == "retired" and config.get("retirement_active") is True:
        current = status_payload(config)
        if not current["writer_processes"] and not launcher_quiescence_drift(current):
            return current
    unknown = unregistered_candidates(config)
    unknown_processes = unregistered_writer_processes(config)
    if unknown or unknown_processes:
        raise MaintenanceError("unregistered writer requires ownership verification before retirement")
    config["retirement_active"] = True
    config.setdefault("retirement", {}).update(status="owner_retired", reason=reason)
    atomic_json(CONFIG_PATH, config)
    state = dict(existing)
    state.update(status="retiring", retirement_active=True, reason=reason)
    state.setdefault("entered_at", now_iso())
    atomic_json(STATE_PATH, state)
    # Do not invoke the listener restart preflight or consume pending work.
    for writer in config["memory_writers"]:
        set_launch_enabled(str(writer["label"]), False)
    handles = []
    try:
        handles = acquire_review_quiescence_barriers(timeout=60)
        # Acquire the existing commit lock before stopping any writer. Pending
        # queues remain untouched. A helper already committing may finish naturally.
        index_lock = ROOT / "state" / "memory-index.lock"
        with index_lock.open("a+") as lock:
            deadline = time.monotonic() + 60
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise MaintenanceError("atomic commit busy; retirement sealed; retry after completion")
                    time.sleep(0.25)
            # No business writer can enter its atomic commit while launchd
            # unloads its owning process group. No queue drain or request replay.
            for writer in config["memory_writers"]:
                bootout(writer)
            # Only signal remaining owned loops after the transaction barrier.
            for pid, command in matching_processes():
                if any(m in command for m in RECEIVER_HELPER_MARKERS):
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 60
            while matching_processes() and time.monotonic() < deadline:
                time.sleep(0.25)
            if matching_processes():
                raise MaintenanceError("owned process still stopping; no force kill; retirement stays sealed")
        current = status_payload(config)
        if current["writer_processes"] or launcher_quiescence_drift(current):
            raise MaintenanceError("retirement runtime boundary is not yet quiescent")
        state.update(status="retired", retired_at=now_iso(), last_error=None)
        atomic_json(STATE_PATH, state)
        return status_payload(config)
    except Exception as exc:
        state.update(status="blocked", retirement_active=True, last_error=str(exc))
        atomic_json(STATE_PATH, state)
        raise
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def enter(config: dict[str, Any], reason: str) -> dict[str, Any]:
    if retirement_active(config):
        raise MaintenanceError("owner retirement cannot enter restartable maintenance")
    existing = read_state()
    resumable = bool(existing and existing.get("status") in {"entering", "active", "blocked"})
    if existing and existing.get("status") == "active":
        payload = status_payload(config)
        if (not payload["writer_processes"] and not any(payload["queues"].values())
                and not any(payload["loaded"].values()) and not any(payload["enabled"].values())):
            return payload
    unknown = unregistered_candidates(config)
    if unknown:
        raise MaintenanceError(f"unregistered memory writers: {sorted(unknown)}")
    unknown_processes = unregistered_writer_processes(config)
    if unknown_processes:
        raise MaintenanceError(f"unregistered writer processes: {[pid for pid, _ in unknown_processes]}")
    # Check before saving entering state or stopping any registered writer.
    import listener_manual_acceptance
    try:
        listener_manual_acceptance.maintenance_restart_preflight(config)
    except (listener_manual_acceptance.BudgetBlocked, OSError, ValueError) as exc:
        raise MaintenanceError("listener restart preflight failed: " + str(exc)) from exc
    writers = list(config["memory_writers"])
    if resumable:
        baseline = existing.get("baseline_loaded")
        if not isinstance(baseline, dict):
            raise MaintenanceError("resumable maintenance state lacks baseline_loaded")
        expected_labels = {str(writer["label"]) for writer in writers}
        if set(baseline) != expected_labels:
            raise MaintenanceError("maintenance baseline does not match configured memory_writers")
        baseline_enabled = existing.get("baseline_enabled")
        if baseline_enabled is None:
            current_enabled = launch_enabled_states()
            baseline_enabled = {
                label: launch_enabled(label, current_enabled) for label in sorted(expected_labels)
            }
        if not isinstance(baseline_enabled, dict) or set(baseline_enabled) != expected_labels:
            raise MaintenanceError("maintenance enabled baseline does not match configured memory_writers")
        state = dict(existing)
        state.update({"status": "entering", "retry_at": now_iso(),
                      "baseline_enabled": baseline_enabled})
        state.pop("blocked_at", None)
        state.pop("error", None)
    else:
        baseline = {str(writer["label"]): launch_loaded(str(writer["label"])) for writer in writers}
        current_enabled = launch_enabled_states()
        baseline_enabled = {
            str(writer["label"]): launch_enabled(str(writer["label"]), current_enabled)
            for writer in writers
        }
        state = {
            "schema_version": 1,
            "status": "entering",
            "reason": reason,
            "entered_at": now_iso(),
            "baseline_loaded": baseline,
            "baseline_enabled": baseline_enabled,
        }
    atomic_json(STATE_PATH, state)
    try:
        receiver = [writer for writer in writers if writer.get("role") == "sole_receiver"]
        if len(receiver) != 1:
            raise MaintenanceError("memory_writers must contain exactly one sole_receiver")
        other = [writer for writer in writers if writer.get("role") != "sole_receiver"]
        review_writers = [
            writer for writer in other
            if str(writer.get("label")) == "com.example.dingtalk-minutes-review"
        ]
        non_review_writers = [writer for writer in other if writer not in review_writers]
        if len(review_writers) != 1:
            raise MaintenanceError("memory_writers must contain exactly one DingTalk review writer")
        for writer in writers:
            set_launch_enabled(str(writer["label"]), False)
        for writer in sorted(non_review_writers, key=lambda item: int(item.get("stop_order", 0))):
            bootout(writer)
        review_barriers = acquire_review_quiescence_barriers()
        try:
            bootout(review_writers[0])
            terminate_processes(include_receiver=False)
        finally:
            release_lock_handles(review_barriers)
        queues = wait_for_empty_queues()
        wait_for_receiver_helpers()
        if not launch_loaded(str(receiver[0]["label"])) and not index_state_current(bridge_index_state()):
            reconcile_indexes_offline()
        barrier = acquire_index_quiescence_barrier()
        try:
            bootout(receiver[0])
            terminate_processes(include_receiver=True)
            wait_for_stable_process_exit()
        finally:
            fcntl.flock(barrier.fileno(), fcntl.LOCK_UN)
            barrier.close()
        # A concurrent bootstrap/reconciler can reload a scheduled job after
        # its process has stopped.  Process-zero alone is therefore not a safe
        # maintenance boundary: every registered launcher must also remain
        # unloaded and explicitly disabled before the state becomes active.
        launcher_state = status_payload(config)
        launcher_drift = launcher_quiescence_drift(launcher_state)
        if launcher_drift:
            raise MaintenanceError(
                f"registered launchers remain enabled or loaded after stop: {launcher_drift}"
            )
        databases = database_health(config)
        locks = lock_health()
        state.update({"status": "active", "quiesced_at": now_iso(), "queues": queues,
                      "databases": databases, "locks": locks})
        atomic_json(STATE_PATH, state)
        return status_payload(config)
    except Exception as exc:
        state.update({"status": "blocked", "blocked_at": now_iso(), "error": str(exc)[:500]})
        atomic_json(STATE_PATH, state)
        raise


def exit_maintenance(config: dict[str, Any]) -> dict[str, Any]:
    if retirement_active(config):
        raise MaintenanceError("owner retirement cannot be cleared by maintenance exit")
    state = read_state()
    if not state or state.get("status") == "inactive":
        return status_payload(config)
    if state.get("status") not in {"active", "blocked"}:
        raise MaintenanceError(f"cannot exit maintenance from state {state.get('status')!r}")
    unknown = unregistered_candidates(config)
    if unknown:
        raise MaintenanceError(f"unregistered memory writers: {sorted(unknown)}")
    baseline = state.get("baseline_loaded") or {}
    baseline_enabled = state.get("baseline_enabled") or {}
    expected_labels = {str(writer["label"]) for writer in config["memory_writers"]}
    if set(baseline) != expected_labels or set(baseline_enabled) != expected_labels:
        raise MaintenanceError("maintenance restore baseline is incomplete")
    writers = sorted(config["memory_writers"], key=lambda item: int(item.get("resume_order", 0)))
    state["status"] = "exiting"
    atomic_json(STATE_PATH, state)
    try:
        for writer in writers:
            label = str(writer["label"])
            set_launch_enabled(label, bool(baseline_enabled[label]))
        for writer in writers:
            if baseline.get(str(writer["label"])) is True:
                bootstrap(writer)
        missing = [label for label, expected in baseline.items() if expected and not launch_loaded(label)]
        if missing:
            raise MaintenanceError(f"writers failed to restore: {missing}")
        enabled_drift = [
            label for label, expected in baseline_enabled.items()
            if launch_enabled(label) != bool(expected)
        ]
        if enabled_drift:
            raise MaintenanceError(f"writer enable state failed to restore: {enabled_drift}")
        state.update({"status": "inactive", "exited_at": now_iso()})
        atomic_json(STATE_PATH, state)
        return status_payload(config)
    except Exception as exc:
        state.update({"status": "blocked", "blocked_at": now_iso(), "error": str(exc)[:500]})
        atomic_json(STATE_PATH, state)
        raise


def self_test() -> dict[str, Any]:
    import tempfile
    from unittest import mock

    tests = 0
    sample = {
        "memory_writers": [{"label": "known", "process_markers": ["/bin/dingtalk_sync_orchestrator.py"]}]
    }
    if unregistered_candidates(sample, {"known"}) != set():
        raise AssertionError("known writer rejected")
    tests += 1
    if unregistered_candidates(sample, {"known", "unknown"}) != {"unknown"}:
        raise AssertionError("unknown writer was not detected")
    tests += 1
    if not is_writer_process(f"python3 {ROOT}/bin/dingtalk_sync_orchestrator.py sync-now"):
        raise AssertionError("manual DingTalk orchestrator was not detected")
    tests += 1
    if is_writer_process(f"python3 {ROOT}/bin/dingtalk_decision_center.py --port 17626"):
        raise AssertionError("read-only DingTalk decision server was misclassified as a writer")
    tests += 1
    if is_writer_process(
        _archive_path('/Users/example/Applications/业务记忆同步中心.app/Contents/MacOS/DingTalkSyncCenter')
    ):
        raise AssertionError("DingTalk Sync Center UI was misclassified as a writer")
    tests += 1
    if not is_writer_process(
        "/usr/bin/python3 /Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/sync_minutes.py --manual"
    ):
        raise AssertionError("external DingTalk source writer was not detected")
    tests += 1
    review_command = (
        "/usr/bin/python3 /Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/"
        "核对/.pipeline/review_dispatch.py --limit 2"
    )
    if not is_writer_process(review_command) or not is_review_process(review_command):
        raise AssertionError("DingTalk paid-review dispatcher was not detected")
    tests += 1
    asr_command = (
        "/usr/bin/python3 /Users/example/AI/shared-skills/transcription-review/"
        "scripts/transcribe_dashscope.py meeting.m4a"
    )
    if not is_writer_process(asr_command) or not is_review_process(asr_command):
        raise AssertionError("DingTalk paid ASR child was not detected")
    tests += 1
    review_sample = {
        "memory_writers": [{
            "label": "com.example.dingtalk-minutes-review",
            "process_markers": ["/核对/.pipeline/review_dispatch.py"],
        }]
    }
    codex_command = (
        "codex exec --cd /Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/"
        "核对/2020-01-02-review"
    )
    if registered_process_labels(review_sample, codex_command) != ["com.example.dingtalk-minutes-review"]:
        raise AssertionError("DingTalk review Codex child was not attributed")
    tests += 1
    if is_writer_process("/usr/local/bin/tailscale file get --loop /tmp/unrelated"):
        raise AssertionError("unrelated Tailscale receiver was misclassified")
    tests += 1
    if registered_process_labels(
        sample, f"python3 {ROOT}/bin/dingtalk_sync_orchestrator.py sync-now"
    ) != ["known"]:
        raise AssertionError("registered one-shot writer was not attributed")
    tests += 1
    email_sample = {
        "memory_writers": [
            {"label": "email", "process_markers": ["/bin/email_sync.py"]}
        ]
    }
    email_command = f"python3 {ROOT}/bin/email_sync.py sync-once"
    if not is_writer_process(email_command):
        raise AssertionError("manual enterprise-email writer was not detected")
    tests += 1
    if registered_process_labels(email_sample, email_command) != ["email"]:
        raise AssertionError("registered enterprise-email writer was not attributed")
    tests += 1
    synthetic_unknown = f"/usr/bin/ruby {ROOT}/bin/refresh_memory_indexes.rb"
    if unregistered_writer_processes(sample, [(99999, synthetic_unknown)]) != [(99999, synthetic_unknown)]:
        raise AssertionError("unregistered writer process was not rejected")
    tests += 1
    parsed_states = parse_launch_enabled_states(
        'disabled services = {\n"known" => enabled\n"paused" => disabled\n}'
    )
    if parsed_states != {"known": True, "paused": False}:
        raise AssertionError("launchd enable state parse failed")
    tests += 1
    if not launch_enabled("omitted-default", parsed_states):
        raise AssertionError("omitted launchd state must default to enabled")
    tests += 1
    if launcher_quiescence_drift(
        {"loaded": {"quiet": False, "reloaded": True},
         "enabled": {"quiet": False, "reenabled": True}}
    ) != ["reenabled", "reloaded"]:
        raise AssertionError("loaded or enabled launcher drift was not rejected")
    tests += 1
    if not index_state_current(
        {"index_dirty_generation": "7", "index_applied_generation": "7", "index_last_error": ""}
    ):
        raise AssertionError("coherent index state was rejected")
    tests += 1
    if index_state_current(
        {"index_dirty_generation": "8", "index_applied_generation": "7", "index_last_error": ""}
    ):
        raise AssertionError("stale index state was accepted")
    tests += 1
    original_review_locks = REVIEW_LOCK_PATHS
    try:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            globals()["REVIEW_LOCK_PATHS"] = (
                temporary_root / "review.lock",
                temporary_root / "dispatch.lock",
            )
            with mock.patch(
                __name__ + ".active_review_processes",
                side_effect=[[(321, "paid review")], []],
            ) as active, mock.patch(__name__ + ".time.sleep"):
                handles = acquire_review_quiescence_barriers(timeout=2)
            release_lock_handles(handles)
            if active.call_count != 2:
                raise AssertionError("maintenance did not wait for the current paid review")
    finally:
        globals()["REVIEW_LOCK_PATHS"] = original_review_locks
    tests += 1
    return {"verdict": "pass", "tests_passed": tests}


def main() -> int:
    parser = argparse.ArgumentParser(description="示例企业业务记忆维护模式")
    parser.add_argument("command", choices=("enter", "retire", "status", "exit", "self-test"))
    parser.add_argument("--reason", default="manual-maintenance")
    args = parser.parse_args()
    if args.command == "self-test":
        print(json.dumps(self_test(), ensure_ascii=False, sort_keys=True))
        return 0
    config = load_config()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.command == "retire":
            payload = retire(config, args.reason)
        elif args.command == "enter":
            payload = enter(config, args.reason)
        elif args.command == "exit":
            payload = exit_maintenance(config)
        else:
            payload = status_payload(config)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MaintenanceError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
