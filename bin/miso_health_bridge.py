#!/usr/bin/env python3
"""Minimal typed bridge into Miso's governed memory-health outbox.

Only enum health transitions and the outbox event identifier cross this
boundary.  Alert titles, local paths, business identifiers and source content
are deliberately excluded.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import json
import re
import sys
from pathlib import Path


MISO_ROOT = Path(_archive_path('/Users/example/.hermes/miso-memory-notifier'))
MISO_APP = MISO_ROOT / "app"
MISO_OUTBOX = MISO_ROOT / "state" / "outbox.sqlite3"
HEALTH_STATUSES = {"healthy", "attention", "critical", "review_required", "unknown"}
HEALTH_CATEGORIES = {"storage", "retrieval", "quality", "backup", "access_control"}
EVENT_ID_RE = re.compile(r"miso_[0-9a-f]{64}")


def load_contract():
    if not MISO_APP.is_dir() or not MISO_OUTBOX.is_file():
        raise RuntimeError("miso_runtime_unavailable")
    sys.path.insert(0, str(MISO_APP))
    from miso_notifier.outbox import Outbox  # type: ignore
    from miso_notifier.producer import Producer  # type: ignore

    return Outbox, Producer


def emit(args: argparse.Namespace) -> dict[str, object]:
    Outbox, Producer = load_contract()
    result = Producer(Outbox(MISO_OUTBOX)).memory_health_change(
        observed_at=args.observed_at,
        previous_status=args.previous_status,
        current_status=args.current_status,
        category_status={args.category: args.category_status},
        sync_context=json.loads(args.sync_context) if args.sync_context else None,
        incident_ref=args.incident_ref,
        revision=args.revision,
    )
    return {
        "event_id": result.event_id,
        "created": bool(result.created),
        "duplicate_input": bool(result.duplicate_input),
        "status": str(result.status),
    }


def status(args: argparse.Namespace) -> dict[str, object]:
    Outbox, _Producer = load_contract()
    row = Outbox(MISO_OUTBOX, initialize=False).get_event(args.event_id, readonly=True)
    if not row:
        raise RuntimeError("miso_event_missing")
    return {
        "event_id": args.event_id,
        "status": str(row.get("status") or "unknown"),
        "attempt_count": int(row.get("attempt_count") or 0),
        "delivered_at": row.get("delivered_at"),
        "error_code": row.get("last_error_code"),
        "receipt_hash": row.get("receipt_hash"),
    }


def retire(args: argparse.Namespace) -> dict[str, object]:
    Outbox, _ = load_contract()
    outbox = Outbox(MISO_OUTBOX)
    # Only provably unsent events may be superseded. Never alter uncertainty.
    with outbox._connect() as connection:
        cursor = connection.execute(
            "UPDATE outbox_events SET status='superseded',superseded_at=datetime('now'),updated_at=datetime('now') "
            "WHERE event_id=? AND event_type='memory_health_change' AND status IN ('pending','blocked')", (args.event_id,),
        )
    return {"retired_unsent": cursor.rowcount == 1}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge Sync Center health events to Miso")
    commands = parser.add_subparsers(dest="command", required=True)
    emit_parser = commands.add_parser("emit")
    emit_parser.add_argument("--observed-at", required=True)
    emit_parser.add_argument("--previous-status", required=True, choices=sorted(HEALTH_STATUSES))
    emit_parser.add_argument("--current-status", required=True, choices=sorted(HEALTH_STATUSES))
    emit_parser.add_argument("--category", required=True, choices=sorted(HEALTH_CATEGORIES))
    emit_parser.add_argument("--category-status", required=True, choices=sorted(HEALTH_STATUSES))
    emit_parser.add_argument("--sync-context")
    emit_parser.add_argument("--incident-ref")
    emit_parser.add_argument("--revision", type=int, default=0)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--event-id", required=True, type=validated_event_id)
    retire_parser = commands.add_parser("retire")
    retire_parser.add_argument("--event-id", required=True, type=validated_event_id)
    return parser.parse_args()


def validated_event_id(value: str) -> str:
    if not EVENT_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("miso_event_id_invalid")
    return value


def main() -> int:
    args = parse_args()
    try:
        payload = {"emit": emit, "status": status, "retire": retire}[args.command](args)
    except Exception as exc:
        reason = str(exc)
        if not re.fullmatch(r"[a-z0-9_]{1,80}", reason):
            reason = type(exc).__name__.lower()[:80]
        print(json.dumps({"ok": False, "reason": reason}, separators=(",", ":")))
        return 2
    print(json.dumps({"ok": True, **payload}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
