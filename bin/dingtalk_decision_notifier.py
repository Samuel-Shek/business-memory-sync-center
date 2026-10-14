#!/usr/bin/env python3
"""Notify only for new or materially changed manual ReviewCases."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import business_page_router as business_router
import claim_reconciliation as reconciliation


ROOT = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync'))
CONFIG_PATH = ROOT / "config.json"
STATE_FILE = ROOT / "state" / "decision-notifier.json"
RETRY_DELAY = timedelta(minutes=15)
STATE_SCHEMA_VERSION = 2


def read_state() -> dict:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_state(value: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".decision-notifier-", dir=str(STATE_FILE.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_FILE)
        STATE_FILE.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def manual_case_snapshot(config: dict) -> dict:
    del config
    cases = reconciliation.list_cases("needs_user")["items"]
    rows = sorted(
        (
            {
                "case_id": str(item["case_id"]),
                "context_hash": str(item["context_hash"]),
                "case_version": int(item["case_version"]),
                "case_kind": str(item["case_kind"]),
            }
            for item in cases
        ),
        key=lambda item: item["case_id"],
    )
    fingerprint = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "count": len(rows),
        "fingerprint": fingerprint,
        "items": rows,
    }


def parse_stamp(value: object) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def notify(snapshot: dict) -> bool:
    body = (
        f"出现新的或内容已变化的业务事项，当前需要你处理 {int(snapshot['count'])} 项。"
        "请打开“业务记忆同步中心”的“需要我”。"
    )
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'on run argv\n'
                'display notification (item 1 of argv) with title "业务记忆 · 需要我"\n'
                'end run',
                body,
            ],
            check=False,
            timeout=5,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _material_change(previous: dict, current: dict) -> bool:
    old = {
        str(item.get("case_id")): str(item.get("context_hash"))
        for item in previous.get("items") or []
        if isinstance(item, dict)
    }
    return any(
        old.get(str(item["case_id"])) != str(item["context_hash"])
        for item in current["items"]
    )


def main() -> int:
    checked = datetime.now().astimezone()
    checked_at = checked.isoformat(timespec="seconds")
    previous = read_state()
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        snapshot = manual_case_snapshot(config)
    except (
        OSError,
        json.JSONDecodeError,
        business_router.RoutingError,
        reconciliation.ReconciliationError,
    ):
        write_state(
            {
                **previous,
                "checked_at": checked_at,
                "last_error_code": "manual_case_state_unavailable",
                "next_retry_at": (checked + RETRY_DELAY).isoformat(timespec="seconds"),
            }
        )
        return 0

    if int(previous.get("schema_version") or 0) != STATE_SCHEMA_VERSION:
        write_state(
            {
                **snapshot,
                "checked_at": checked_at,
                "seeded_at": checked_at,
                "last_error_code": None,
                "next_retry_at": None,
            }
        )
        return 0

    retry_at = parse_stamp(previous.get("next_retry_at"))
    retry_due = retry_at is None or retry_at <= checked
    pending_retry = previous.get("pending_fingerprint") == snapshot["fingerprint"]
    should_notify = (_material_change(previous, snapshot) or pending_retry) and snapshot["count"] > 0
    if should_notify and retry_due:
        if notify(snapshot):
            write_state(
                {
                    **snapshot,
                    "checked_at": checked_at,
                    "notified_at": checked_at,
                    "pending_fingerprint": None,
                    "last_error_code": None,
                    "next_retry_at": None,
                }
            )
        else:
            write_state(
                {
                    **previous,
                    "pending_fingerprint": snapshot["fingerprint"],
                    "pending_items": snapshot["items"],
                    "pending_count": snapshot["count"],
                    "checked_at": checked_at,
                    "last_error_code": "mac_notification_unavailable",
                    "next_retry_at": (checked + RETRY_DELAY).isoformat(timespec="seconds"),
                }
            )
    elif not should_notify:
        write_state(
            {
                **snapshot,
                "checked_at": checked_at,
                "pending_fingerprint": None,
                "last_error_code": None,
                "next_retry_at": None,
                "seeded_at": previous.get("seeded_at"),
                "notified_at": previous.get("notified_at"),
            }
        )
    else:
        write_state({**previous, "checked_at": checked_at})
    return 0


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
