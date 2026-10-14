#!/usr/bin/env python3
"""Model-free contract test for non-exhausting AI Minutes media probes."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import importlib.util
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


SOURCE = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/sync_minutes.py'))


def check(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def main() -> int:
    spec = importlib.util.spec_from_file_location("business_minutes_daily_probe_fixture", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("minutes_source_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = datetime.now(module.TZ)
    with tempfile.TemporaryDirectory(prefix="minutes-daily-probe-") as temporary:
        path = Path(temporary) / "automation-state.json"
        path.write_text(json.dumps({
            "schema_version": 2,
            "items": {
                "fixture": {
                    "media_pending_since_at": module.iso_datetime(now - timedelta(days=3)),
                    "media_last_error_at": module.iso_datetime(now - timedelta(days=2)),
                    "media_next_retry_at": None,
                    "media_retry_stage": 4,
                    "media_retry_exhausted": True,
                }
            },
        }), encoding="utf-8")
        module.AUTOMATION_STATE_FILE = path
        migrated = module.load_automation_state()["items"]["fixture"]
        check(migrated["media_retry_exhausted"] is False, "legacy exhaustion migrates to a daily probe")
        check(module.parse_iso_datetime(migrated["media_next_retry_at"]) is not None, "migration assigns the next probe")

        entry = {"media_pending_since_at": module.iso_datetime(now - timedelta(days=3))}
        exhausted = module.schedule_media_pending(entry, now)
        next_probe = module.parse_iso_datetime(entry["media_next_retry_at"])
        check(exhausted is False, "media scheduling never reaches a permanent stopped state")
        check(entry["media_daily_probe"] is True, "daily probe mode is explicit")
        check(timedelta(hours=23, minutes=59) <= next_probe - now <= timedelta(hours=24, minutes=1), "post-24-hour probes repeat daily")
        since = entry['media_pending_since_at']
        entry['media_attempts'] = 8
        from unittest.mock import patch
        with patch.object(module, 'exception_diagnostics', return_value={
                'safe_error_code': 'media_unavailable', 'phase': 'media_lookup', 'hint': 'source unavailable'}):
            module.record_media_error(entry, {'mode': 'media_backfill'}, now, RuntimeError())
        check(entry['media_attempts'] == 9 and entry['media_pending_since_at'] == since,
              'manual media recheck preserves history')
        check(module.parse_iso_datetime(entry['media_next_retry_at']) - now >= timedelta(hours=23, minutes=59),
              'manual media recheck keeps daily cadence')
    print('{"verdict":"pass","checks":8}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
