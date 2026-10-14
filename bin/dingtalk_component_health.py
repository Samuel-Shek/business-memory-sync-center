#!/usr/bin/env python3
"""Privacy-bounded local bridge for deterministic component recovery."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import json

import dingtalk_sync_v2 as syncv2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile one DingTalk component health state")
    parser.add_argument("--component", required=True)
    parser.add_argument("--recover-if-error", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    event_id = syncv2.recover_component_if_error(args.component, args.recover_if_error)
    print(json.dumps({"status": "recovered" if event_id else "noop"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
