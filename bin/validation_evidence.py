#!/usr/bin/env python3
"""Read-only full-validation bindings. Only aggregate hashes leave this module."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import errno
import hashlib
import json
import os
import sys
import sqlite3
import stat as stat_module
import validation_progress as progress
from pathlib import Path
from typing import Any

CODE_PATHS = ("bin/validate_memory.rb", "bin/memory_validation_contracts.rb", "bin/refresh_memory_indexes.rb",
              "bin/validation_evidence.py", "bin/automatic_memory_validation.py", "bin/business_sync_supervisor.py",
              "bin/business_sync_controller.py", "bin/receiver.py", "bin/dingtalk_memory_automation.py",
              "bin/local_sync_repair.py", "app-source/Info.plist")
CODE_PATHS += ("bin/dingtalk_batch_contract.py", "bin/dingtalk_conflict_recovery.py",
               "bin/claim_reconciliation.py", "bin/business_sync_state.py", "bin/sqlite_read_diagnostics.py")
CODE_PATHS += ("bin/business_page_integrity.py", "bin/business_page_router.py",
               "bin/operational_workbench.py", "bin/system_dashboard_status.py")
CODE_PATHS += ("bin/refresh_internal_memory_ledger.rb", "bin/source_inventory_maintenance.rb",
               "bin/email_sync.py", "bin/win11_semantic_merge.rb", "bin/win11_batch_selection.rb")
CODE_PATHS += ("bin/validation_retry.py", "bin/validation_failure_receipts.py", "bin/business_sync_state.py", "bin/sqlite_write_transactions.py")
CODE_PATHS += ("bin/dingtalk_commit_guard.py", "bin/dingtalk_prepared_recovery.py", "bin/chat_read_recovery.py", "bin/sync_dingtalk_chat.py",
               "bin/chat_reconcile_checkpoint.py", "bin/dingtalk_sync_orchestrator.py")
CODE_PATHS += ("bin/listener_manual_acceptance.py", "bin/listener_retry_budget.py", "bin/maintenance_mode.py")

CODE_PATHS += ("bin/supervisor_incidents.py", "bin/sqlite_attribution.py", "bin/sqlite_lock_probe.py", "bin/dingtalk_sync_v2.py")


CODE_PATHS += ("bin/validation_progress.py", "bin/validation_process.py", "bin/validation_progress.rb")
# Decides which provider writes memory, so it is bound like the callers that use it.
CODE_PATHS += ("bin/model_router.py", "bin/feishu_notify.py")

CODE_PATHS += ("bin/ledger_recovery.py", "bin/media_actions.py", "bin/media_sources.py", "bin/business_sync_supervisor_health.py")

CODE_PATHS += ("bin/email_phases.py", "bin/email_phase_worker.py", "bin/win11_mail_attachments.py")

CODE_PATHS += ("bin/mail_auto_recovery.py", "bin/mail_semantic_recovery.py", "bin/mail_transaction_recovery.py")

CODE_PATHS += ("bin/mail_legacy_recovery.py",)
CODE_PATHS += ("bin/win11_precommit_closure.py", "bin/win11_memory_automation.py")

CODE_PATHS += ("bin/supervisor_liveness.py", "bin/supervisor_worker_pool.py", "bin/supervisor_worker_progress.py")

CODE_PATHS += ("bin/sync_repair_rules.py", "bin/sync_repair_contract.py", "bin/sync_center_autonomy.py", "bin/image_frame_coverage.py", "bin/sync_rule_runtime.py", "bin/mail_attachment_reader.py", "bin/mail_attachment_worker.ps1")

class InputsChanged(ValueError):
    pass


def identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def unchanged(path, signature):
    try:
        same = identity(path.lstat()) == signature
    except (FileNotFoundError, NotADirectoryError):
        same = False
    if not same: raise InputsChanged("validation_inputs_changed")


def file_binding(path: Path, cache=None) -> tuple:
    try:
        st = path.lstat()
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise InputsChanged("validation_inputs_changed") from exc
    key = str(path)
    signature = identity(st)
    if stat_module.S_ISLNK(st.st_mode):
        result = (key, st.st_mode, "symlink", os.readlink(path))
        unchanged(path, signature)
        return result
    if path.suffix.lower() in {".md", ".json", ".jsonl", ".txt"}:
        # Opening again checks current ACL/permissions even on a cache hit.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                raise InputsChanged("validation_inputs_changed") from exc
            raise
        with os.fdopen(fd, "rb") as handle:
            if identity(os.fstat(handle.fileno())) != signature:
                raise InputsChanged("validation_inputs_changed")
            cached = cache.get(key) if cache is not None else None
            if cached and cached[0] == signature:
                result = cached[1]
                progress.count("cache_hits")
            else:
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    progress.count("file_bytes", len(chunk))
                progress.count("file_reads")
                result = (key, st.st_mode, digest.hexdigest())
            if identity(os.fstat(handle.fileno())) != signature:
                raise InputsChanged("validation_inputs_changed")
            unchanged(path, signature)
        if cache is not None: cache[key] = (signature, result)
        return result
    unchanged(path, signature)
    return (key, st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode)


def database_digest(path: Path, tables: tuple[str, ...]) -> str:
    if not path.is_file() or path.is_symlink():
        return "unavailable"
    digest = hashlib.sha256()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        digest.update(str(connection.execute("PRAGMA user_version").fetchone()).encode())
        for name in tables:
            schema = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            digest.update(str((name, schema)).encode())
            if schema:
                rows = connection.execute('SELECT * FROM "' + name + '"').fetchall()
                progress.count("database_rows", len(rows))
                # Source content stays inside the hash; nothing is logged.
                hashes = sorted(hashlib.sha256(repr(row).encode()).hexdigest() for row in rows)
                digest.update("".join(hashes).encode())
    return digest.hexdigest()


def context(root: Path, *, file_cache=None) -> dict[str, str]:
    config_path = root / "config.json"
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    code = hashlib.sha256(config_bytes)
    for name in CODE_PATHS:
        code.update(name.encode())
        code.update((root / name).read_bytes())
    if progress.current(): progress.current().code_sha256 = code.hexdigest()
    roots = {Path(config["memory_root"]).parent}
    for key in ("watch_notes_root",):
        if config.get(key):
            roots.add(Path(config[key]))
    rows = []
    # Validators default to an independent byte read. Display callers may reuse
    # only file digests whose inode/ctime/mtime/size/mode are unchanged; every
    # hit still reopens the file, checks access and checks identity twice.
    cache = {} if file_cache is None else file_cache
    if len(cache) > 4096:
        cache.clear()
    unstable = False
    def visit(path):
        nonlocal unstable
        try:
            rows.append(file_binding(path, cache))
        except InputsChanged:
            # Read the remaining files even when iCloud updates metadata during
            # one read. This entire snapshot still fails; no old hash is used.
            # Stopping at the first such file made each cycle warm only one file.
            unstable = True
            progress.count("unstable_files")
        progress.count("file_bindings")
    def unavailable(error: OSError) -> None:
        # os.walk otherwise silently omits denied directories and can bind a
        # successful receipt to an incomplete snapshot.
        raise error
    with progress.stage("snapshot_files"):
        for base in sorted(roots):
            if not base.is_dir():
                raise ValueError("validation_source_unavailable")
            for directory, dirs, files in os.walk(base, followlinks=False, onerror=unavailable):
                dirs[:] = sorted(name for name in dirs if not name.startswith("."))
                for name in sorted(files):
                    if name.startswith("."):
                        continue
                    path = Path(directory) / name
                    visit(path)
        # Bind the ledgers and transaction journals checked by the full validator,
        # not volatile supervisor heartbeats or unrelated provider databases.
        extra_paths = []
        if config.get("machine_ledger_root"):
            extra_paths.append(Path(config["machine_ledger_root"]) / "对话同步台账.json")
        governance = config.get("source_governance") or {}
        extra_paths.extend(Path(governance[key]) for key in ("legacy_ledger", "inventory", "derivation_receipts") if governance.get(key))
        if config.get("semantic_runs_root"):
            extra_paths.extend(Path(config["semantic_runs_root"]).glob("*/transaction.json"))
        import email_phases
        if config.get("email_sync"):
            extra_paths.extend(email_phases.snapshot_paths(config))
        for path in sorted(extra_paths):
            visit(path)
    if unstable:
        raise InputsChanged("validation_inputs_changed")
    encoded = json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":")).encode()
    with progress.stage("snapshot_databases"):
        databases = {"bridge": database_digest(root / "state/bridge.sqlite", ("events", "files", "watch_sources", "watch_entries", "watch_versions", "source_events", "maintenance_state"))}
        if config.get("semantic_database"):
            databases["semantic"] = database_digest(Path(config["semantic_database"]), ("runs", "items", "manual_seed", "id_reservations", "applied_transactions"))
    return {"code_sha256": code.hexdigest(), "source_snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
            "database_snapshot_sha256": hashlib.sha256(json.dumps(databases, sort_keys=True).encode()).hexdigest()}


def receipt_matches_context(receipt: dict[str, Any] | None, dirty: Any, applied: Any,
                            current_context: dict[str, str]) -> dict[str, Any]:
    """Use only with an independently acquired complete, current context."""
    if not receipt or receipt.get("schema_version") != 2 or receipt.get("verdict") != "pass" or receipt.get("full_validation") is not True:
        return {"current": False, "error_code": "validation_receipt_missing"}
    if dirty is None or str(dirty) != str(applied):
        return {"current": False, "error_code": "validation_index_mismatch"}
    if (str(receipt.get("index_dirty_generation")) != str(dirty)
            or str(receipt.get("index_applied_generation")) != str(applied)):
        return {"current": False, "error_code": "validation_index_mismatch"}
    current = receipt.get("validation_context") == current_context
    return {"current": current, "error_code": None if current else "validation_context_changed"}


def receipt_status(root: Path, receipt: dict[str, Any] | None, dirty: Any, applied: Any, *, file_cache=None) -> dict[str, Any]:
    # Check access independently of receipt age/index drift, so a permission
    # fault never masquerades as a data mismatch or an index-refresh request.
    try:
        current_context = context(root) if file_cache is None else context(root, file_cache=file_cache)
    except InputsChanged:
        return {"current": False, "error_code": "validation_inputs_changed"}
    except PermissionError:
        return {"current": False, "error_code": "validation_permission_denied"}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return {"current": False, "error_code": "validation_context_unavailable"}
    return receipt_matches_context(receipt, dirty, applied, current_context)


def receipt_is_current(root: Path, receipt: dict[str, Any] | None, dirty: Any, applied: Any) -> bool:
    return bool(receipt_status(root, receipt, dirty, applied)["current"])


if __name__ == "__main__":
    try:
        if os.environ.get("BUSINESS_AUTOMATIC_FULL_VALIDATION") == "1":
            with progress.Progress(Path(sys.argv[1]), os.environ.get("BUSINESS_VALIDATION_SNAPSHOT_PHASE", "snapshot"), os.environ.get("BUSINESS_VALIDATION_RUN_ID")):
                value = context(Path(sys.argv[1]))
        else:
            value = context(Path(sys.argv[1]))
        print(json.dumps(value, sort_keys=True))
    except Exception as exc:
        print(json.dumps({"error": "validation_inputs_changed" if isinstance(exc, InputsChanged) else "validation_permission_denied" if isinstance(exc, PermissionError) else "validation_context_unavailable"}))
        raise SystemExit(1)
