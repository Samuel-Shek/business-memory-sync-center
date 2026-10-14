#!/usr/bin/env python3
"""Small local-only bridge for governed DingTalk workers to publish UI progress."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import json
import sys

import dingtalk_sync_v2 as syncv2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update one DingTalk Sync Center action step")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--order", required=True, type=int)
    parser.add_argument("--label", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--current", type=int)
    parser.add_argument("--total", type=int)
    parser.add_argument("--item-label")
    parser.add_argument("--detail-json", default="{}")
    parser.add_argument("--error-code")
    parser.add_argument("--payload-stdin", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stdin_payload = {}
    if args.payload_stdin:
        try:
            stdin_payload = json.loads(sys.stdin.read())
        except json.JSONDecodeError:
            stdin_payload = {}
        if not isinstance(stdin_payload, dict):
            stdin_payload = {}
    try:
        detail = json.loads(args.detail_json)
    except json.JSONDecodeError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    syncv2.initialize()
    syncv2.set_action_step(
        args.run_id,
        args.key,
        args.order,
        args.label,
        args.status,
        current_count=stdin_payload.get("current", args.current),
        total_count=stdin_payload.get("total", args.total),
        item_label=stdin_payload.get("item_label", args.item_label),
        detail=stdin_payload.get("detail", detail),
        error_code=stdin_payload.get("error_code", args.error_code),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
