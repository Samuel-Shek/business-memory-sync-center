#!/usr/bin/env python3
"""Offline checks for manual ReviewCase notifications."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import business_page_router as router
import claim_reconciliation as reconciliation
import dingtalk_decision_notifier as notifier


def check(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def add_claim(config: dict, claim_id: str, title: str, source_ref: str, summary: str = "合成测试内容。") -> Path:
    path, text, _route = router.route_claim_change(
        config,
        {
            "action": "annotate_page",
            "title": title,
            "summary": summary,
            "evidence_boundary": "合成数据，不代表真实业务事实。",
            "category": "internal_material",
        },
        source_system="dingtalk",
        source_ref=source_ref,
        observed_at="2020-01-02T12:00:00+08:00",
        claim_id=claim_id,
    )
    router.verify_expected_hash(path, text)
    router.atomic_write(path, str(text))
    return path


def refresh(config: dict, run_id: str) -> None:
    reconciliation.apply_plan(
        config,
        reconciliation.plan_reconciliation(config),
        run_id=run_id,
        require_maintenance=False,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        vault = root / "✦ 示例企业"
        memory = vault / "00_业务记忆库"
        external = root / "external"
        memory.mkdir(parents=True)
        external.mkdir()
        config = {"memory_root": str(memory), "external_root": str(external)}
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        notifier.CONFIG_PATH = config_path
        notifier.STATE_FILE = root / "state" / "decision-notifier.json"
        reconciliation.DATABASE = root / "state" / "claim-reconciliation.sqlite"
        reconciliation.RESOLUTION_ARCHIVE = root / "state" / "claim-resolution-archive.jsonl"
        reconciliation.RELATION_OUTBOX = root / "state" / "reconciliation-outbox"

        add_claim(config, "BUSINESS-20260829-001", "认证高风险测试", "dingtalk:test:1")
        refresh(config, "refresh-1")
        snapshot = notifier.manual_case_snapshot(config)
        check(snapshot["count"] == 0, "ordinary high-risk evidence does not create a notification")

        add_claim(config, "BUSINESS-20260829-002", "船舶产品线归属与优先级待确认", "dingtalk:test:2")
        refresh(config, "refresh-2")
        snapshot = notifier.manual_case_snapshot(config)
        check(snapshot["count"] == 1, "true business choice is actionable")

        with mock.patch.object(notifier, "notify", return_value=True) as seeded_notify:
            check(notifier.main() == 0 and seeded_notify.call_count == 0, "first V2 run seeds without historical blast")
        check(notifier.read_state().get("schema_version") == 2, "V2 baseline persisted")

        with mock.patch.object(notifier.subprocess, "run", side_effect=subprocess.TimeoutExpired(["osascript"], 5)):
            check(not notifier.notify(snapshot), "notification timeout is contained")

        with mock.patch.object(notifier.subprocess, "run", return_value=subprocess.CompletedProcess(["osascript"], 0)) as osascript:
            check(notifier.notify(snapshot), "valid notification succeeds")
            command = osascript.call_args.args[0]
            check(
                "on run argv" in command[2]
                and 'with title "业务记忆 · 需要我"' in command[2]
                and "示例企业" not in command[2]
                and "需要你处理" in command[3],
                "notification is privacy bounded and uses the current app name",
            )

        add_claim(
            config,
            "BUSINESS-20260829-003",
            "船舶产品线归属与优先级待确认",
            "dingtalk:test:3",
            summary="同一业务选择出现了实质变化。",
        )
        refresh(config, "refresh-3")
        with mock.patch.object(notifier, "notify", return_value=False) as failed_notify:
            notifier.main()
            check(failed_notify.call_count == 1, "materially changed case attempts one notification")
        failed = notifier.read_state()
        check(failed.get("pending_fingerprint") and failed.get("next_retry_at"), "bounded retry persisted")

        with mock.patch.object(notifier, "notify", return_value=True) as early_retry:
            notifier.main()
            check(early_retry.call_count == 0, "backoff suppresses minute retry")

        failed["next_retry_at"] = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")
        notifier.write_state(failed)
        with mock.patch.object(notifier, "notify", return_value=True) as successful_retry:
            notifier.main()
            check(successful_retry.call_count == 1, "retry after delay")

    print('{"verdict":"pass","checks":11}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
