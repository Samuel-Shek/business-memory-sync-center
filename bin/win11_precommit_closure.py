"""Durable cleanup of a failed, never-committed Win11 preparation.

Only explicit cleanup requests from a failed worker are eligible. The merger's
normal fail command owns the state change; sources, candidates and retry budgets
are retained. Missing or conflicting evidence remains blocked.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
import automatic_memory_validation as validation

RUN = re.compile(r"win11-\d{8}T\d{6}-[0-9a-f]{8}")


def request(root: Path, run_id: str, batch_sha256: str, error_code: str) -> None:
    if not RUN.fullmatch(run_id) or not re.fullmatch(r"[0-9a-f]{64}", batch_sha256):
        raise ValueError("win11_closure_binding_invalid")
    code = re.sub(r"[^a-zA-Z0-9_.-]", "_", error_code)[:64]
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}", code):
        code = "worker_failed"
    path = root / "state/win11-precommit-closures" / (run_id + ".json")
    if path.exists():
        old = validation.read_object(path)
        if old.get("run_id") != run_id or old.get("batch_sha256") != batch_sha256:
            raise ValueError("win11_closure_binding_changed")
        return
    validation.atomic_json(path, {"schema_version": 1, "run_id": run_id,
        "batch_sha256": batch_sha256, "error_code": code, "status": "pending",
        "requested_at": datetime.now().astimezone().isoformat(timespec="seconds")})


def drain(root: Path, *, runner=subprocess.run) -> dict:
    directory = root / "state/win11-precommit-closures"
    if not directory.exists():
        return {"status": "idle", "closed": 0}
    requests = [(path, validation.read_object(path)) for path in sorted(directory.glob("win11-*.json"))]
    requests = [(path, row) for path, row in requests if row.get("status") != "closed"]
    if not requests:
        return {"status": "idle", "closed": 0}
    if validation.read_object(root / "state/maintenance-mode.json").get("status") != "inactive":
        return {"status": "deferred", "error_code": "maintenance_active"}
    # Never compete with a live Win11 worker, or remove another owner's lock.
    with validation.try_lock(root / "state/win11_memory_merge.lock") as owner:
        if owner is None:
            return {"status": "deferred", "error_code": "win11_merge_busy"}
        config = validation.read_object(root / "config.json")
        external = Path(config["external_root"])
        database = Path(config.get("semantic_database") or external / "machine-ledgers/win11-semantic-merge.sqlite")
        runs = Path(config.get("semantic_runs_root") or external / "win11-semantic-merge/runs")
        closed = 0
        for path, row in requests[:5]:
            run_id = str(row.get("run_id") or "")
            if row.get("schema_version") != 1 or not RUN.fullmatch(run_id) or path.name != run_id + ".json":
                return {"status": "blocked", "error_code": "win11_closure_state_invalid"}
            batch = runs / run_id / "batch.json"
            if batch.is_symlink() or hashlib.sha256(batch.read_bytes()).hexdigest() != row.get("batch_sha256"):
                return {"status": "blocked", "error_code": "win11_closure_binding_changed"}
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
                connection.row_factory = sqlite3.Row
                run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                applied = connection.execute("SELECT 1 FROM applied_transactions WHERE run_id=?", (run_id,)).fetchone()
            if (not run or run["batch_sha256"] != row["batch_sha256"] or applied
                    or run["status"] not in {"prepared", "failed"} or run["transaction_id"]
                    or int(run["writes_count"] or 0) or (batch.parent / "transaction.json").exists()):
                return {"status": "blocked", "error_code": "win11_closure_commit_evidence"}
            # Probe cheaply; the actual command still acquires the normal lock.
            with validation.try_lock(root / "state/memory-index.lock") as memory:
                if memory is None:
                    return {"status": "deferred", "error_code": "memory_core_busy"}
            env = {**os.environ, "LC_ALL": "en_US.UTF-8", "BUSINESS_BRIDGE_ROOT": str(root),
                   "BUSINESS_MEMORY_LOCK_TIMEOUT_SECONDS": "1"}
            result = runner(["/usr/bin/ruby", str(root / "bin/win11_semantic_merge.rb"),
                "fail", run_id, row["error_code"], "Worker failed before commit; source events and candidate retained."],
                capture_output=True, text=True, timeout=15, env=env)
            receipt = json.loads(result.stdout or result.stderr)
            if result.returncode != 0:
                return {"status": "deferred" if receipt.get("code") == "memory_lock_timeout" else "blocked",
                        "error_code": "win11_closure_" + str(receipt.get("code") or "failed")}
            if receipt.get("status") != "failed" or receipt.get("run_id") != run_id:
                return {"status": "blocked", "error_code": "win11_closure_receipt_invalid"}
            validation.atomic_json(path, {**row, "status": "closed", "receipt": receipt,
                "closed_at": datetime.now().astimezone().isoformat(timespec="seconds")})
            closed += 1
        return {"status": "success", "closed": closed}
