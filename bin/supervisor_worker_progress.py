"""Bounded, content-free source progress. Completion remains the controller receipt."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path


def read(database, config, source, started_at):
    result = {"source_id": source, "started_at": started_at,
              "progress_at": None, "stage": "awaiting_receipt", "progress": "unknown"}
    try:
        with sqlite3.connect(Path(database).absolute().as_uri()+"?mode=ro", uri=True, timeout=0.2) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT run_id,updated_at FROM action_runs WHERE action_type=? AND started_at>=? ORDER BY started_at DESC LIMIT 1",
                            (source, started_at)).fetchone()
            if row:
                result["run_id"] = row["run_id"]
                step = c.execute("SELECT step_key,updated_at FROM action_steps WHERE run_id=? AND started_at IS NOT NULL ORDER BY updated_at DESC,step_order DESC LIMIT 1", (row["run_id"],)).fetchone()
                result.update(progress_at=row["updated_at"], progress="observed")
                if step:
                    result.update(stage=step["step_key"], progress_at=max(result["progress_at"], step["updated_at"]))
        if source != "dingtalk_mail" or not config.get("email_sync"):
            return result
        root = Path(config["email_sync"]["state_root"])
        mail_db = root/"state/email-sync.sqlite"
        with sqlite3.connect(mail_db.absolute().as_uri()+"?mode=ro", uri=True, timeout=0.2) as c:
            # IDs only, never load mail content, proposals or model responses.
            ids = [r[0] for r in c.execute("SELECT semantic_id FROM semantic_groups WHERE status='pending' ORDER BY created_at DESC LIMIT 32")]
        newest = None
        for identifier in ids:
            if not re.fullmatch(r"[a-f0-9-]{36}", identifier):
                continue
            path = root/"runs"/identifier/"model/batch-coverage.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024*1024:
                continue
            stamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            if stamp < started_at:
                continue
            data = json.loads(path.read_text())
            files = data.get("files", [])
            chunks = [x for f in files for x in f.get("chunks", []) if x.get("status") == "validated"]
            value = {"progress_at": stamp, "stage": "email_fragments", "progress": "observed",
                     "validated_chunks": len(chunks), "covered_chars": sum(int(x.get("chars",0)) for x in chunks),
                     "observed_files": len(files), "fragment_stage_complete": data.get("status") == "complete"}
            if newest is None or stamp > newest["progress_at"]:
                newest = value
        if newest:
            result.update(newest)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        result["progress"] = "unavailable"
    return result
