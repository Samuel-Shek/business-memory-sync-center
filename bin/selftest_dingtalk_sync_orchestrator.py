#!/usr/bin/env python3
"""Offline checks for partial recording recovery and maintenance gating."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import dingtalk_sync_orchestrator as orchestrator


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        component = root / "partial_component.py"
        component.write_text(
            "import json\n"
            "print(json.dumps({'status':'partial','error_code':'media_pending','failed':0,'media_missing':1}))\n"
            "raise SystemExit(4)\n",
            encoding="utf-8",
        )
        try:
            orchestrator.run_json(
                [sys.executable, str(component)],
                timeout=10,
                environment=dict(os.environ),
                component="recording_sync",
                allow_partial=True,
            )
            raise AssertionError("nonzero partial component was accepted")
        except orchestrator.OrchestratorError as exc:
            check(exc.code == "recording_sync_media_pending", "component preserves a stable media error code")
            check(orchestrator.recording_media_partial(exc.receipt), "partial receipt remains available for recovery")

        maintenance = root / "maintenance.json"
        maintenance.write_text('{"status":"active"}\n', encoding="utf-8")
        with mock.patch.object(orchestrator, "MAINTENANCE_STATE", maintenance):
            check(orchestrator.maintenance_active(), "active maintenance is detected")
        maintenance.write_text('{"status":"inactive"}\n', encoding="utf-8")
        with mock.patch.object(orchestrator, "MAINTENANCE_STATE", maintenance):
            check(not orchestrator.maintenance_active(), "inactive maintenance does not block work")
        check(
            {
                "discovered",
                "source_written",
                "chunking",
                "ready_finalize",
                "independent_ready",
                "comparison_refresh_pending",
            }
            == orchestrator.PROCESSABLE_RECORDING_STATES,
            "manual orchestration drains every unfinished recording stage",
        )

        receipt = {
            "status": "partial",
            "created": 1,
            "updated": 0,
            "selected": 1,
            "failed": 0,
            "media_missing": 1,
            "media_attempt_failures": 1,
            "phase": "media_lookup",
            "error_code": "media_pending",
            "original_error_code": "media_unavailable",
            "reason": "钉钉当前未提供可下载的原始录音。",
            "auto_repair": "scheduled_retry",
            "attempts": 1,
            "next_retry_at": "2020-01-02T09:44:44+08:00",
        }
        components: list[str] = []
        health_calls: list[tuple[str, bool, str | None]] = []
        finish_calls: list[tuple[str, str | None]] = []
        validation_calls: list[bool] = []
        component_environments: list[dict[str, str]] = []
        manual_flag = root / "manual-sync.active"

        def fake_run_json(_command, *, timeout, environment, component, allow_partial=False):
            components.append(component)
            component_environments.append(environment)
            if component == "recording_sync":
                raise orchestrator.OrchestratorError("recording_sync_media_pending", receipt=receipt)
            if component == "recording_source":
                raise orchestrator.OrchestratorError(
                    "recording_source_exit_2",
                    receipt={"status": "partial", "source_notes_written": 0, "failures": ["media_incomplete"]},
                )
            if component == "chat_reconcile":
                return {
                    "status": "success",
                    "writes": 0,
                    "model_called": False,
                    "incremental": {"status": "success", "read_attempted": True, "run_id": "fixture",
                        "conversations_discovered": 0, "conversations_complete": 0, "conversations_incomplete": 0,
                        "reconciliation": {"status": "complete", "list_complete": True, "window_id": "fixture-window",
                            "last_run_id": "fixture", "window_start": "2020-01-02T00:00:00+08:00", "window_end": "2020-01-02T01:00:00+08:00",
                            "conversations_discovered": 0, "conversations_complete": 0, "conversations_incomplete": 0}},
                }
            if component == "decision_registration":
                return {"status": "success", "registered": {"created": 0, "updated": 0}}
            raise AssertionError(f"unexpected component: {component}")

        def fake_health(component, healthy, error_code=None, **_kwargs):
            health_calls.append((component, healthy, error_code))
            return "event" if not healthy else None

        def fake_finish(_run_id, status, *, result=None, error_code=None):
            finish_calls.append((status, error_code))

        def fake_validate(_config, refresh_indexes=False):
            validation_calls.append(bool(refresh_indexes))
            return {"validator": {"verdict": "pass"}}

        patchers = (
            mock.patch.object(orchestrator, "run_json", fake_run_json),
            mock.patch.object(orchestrator, "recording_queue", lambda: []),
            mock.patch.object(orchestrator.syncv2, "initialize", lambda: None),
            mock.patch.object(orchestrator.syncv2, "MANUAL_SYNC_FLAG", manual_flag),
            mock.patch.object(orchestrator.syncv2, "MANUAL_SYNC_LOCK", root / "manual.lock"),
            mock.patch.object(orchestrator.syncv2, "REVIEW_LOCK", root / "review.lock"),
            mock.patch.object(orchestrator.syncv2, "file_lock", lambda *_args, **_kwargs: contextlib.nullcontext()),
            mock.patch.object(orchestrator.syncv2, "set_action_step", lambda *_args, **_kwargs: None),
            mock.patch.object(orchestrator.syncv2, "set_component_health", fake_health),
            mock.patch.object(orchestrator.syncv2, "finish_action_run", fake_finish),
            mock.patch.object(orchestrator.syncv2, "emit_outbox", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("partial run emitted a success event"))),
            mock.patch.object(orchestrator.chat_memory, "load_config", lambda _path: {}),
            mock.patch.object(orchestrator.chat_memory, "atomic_write", lambda path, value, mode=0o600: path.write_text(value, encoding="utf-8")),
            mock.patch.object(orchestrator.chat_memory, "eligible_queue_count", lambda _config: 0),
            mock.patch.object(orchestrator.chat_memory, "run_refresh_and_validate", fake_validate),
        )
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            result = orchestrator.run_sync_now(root / "config.json", "test-run")

        check(result["status"] == "partial" and result["memory_validation"] == "pass", "media delay no longer aborts memory validation")
        check(orchestrator.recording_source_media_partial({"status": "partial", "failures": ["media_incomplete"]}), "source stage recognizes the same upstream media delay")
        check(not orchestrator.recording_source_media_partial({"status": "partial", "failures": ["source_incomplete"]}), "independent source defects remain fatal")
        check({"recording_source", "chat_reconcile", "decision_registration"}.issubset(components), "downstream stages continue after media delay")
        check(("recording_media", False, "recording_media_pending") in health_calls, "media delay enters a recoverable component state")
        check(("manual_sync", True, None) in health_calls, "completed downstream work does not remain a manual-sync failure")
        check(finish_calls[-1] == ("partial", "recording_media_pending"), "action remains visibly partial until media recovers")
        check(validation_calls == [True], "changed source still receives full memory validation")
        check(
            component_environments
            and all(environment.get("DINGTALK_LOCAL_ONLY") == "1" for environment in component_environments)
            and os.environ.get("DINGTALK_LOCAL_ONLY") == "1",
            "manual DingTalk orchestration keeps every child and health event in local-only mode",
        )
        components.clear()
        def waiting_run_json(*args, **kwargs):
            if kwargs['component'] == 'recording_semantic':
                components.append('recording_semantic')
                return {'status': 'deferred', 'reason': 'chat_ai_priority', 'model_calls': 0}
            return fake_run_json(*args, **kwargs)
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(orchestrator, 'run_json', waiting_run_json))
            stack.enter_context(mock.patch.object(orchestrator, 'recording_queue', lambda: ['fixture']))
            stack.enter_context(mock.patch.object(orchestrator, 'recording_status', lambda _id: 'chunking'))
            try:
                orchestrator.run_sync_now(root / 'config.json', 'wait-run')
                raise AssertionError('wait receipt was counted as success')
            except orchestrator.OrchestratorError as exc:
                check(exc.code == 'chat_ai_priority', 'shared AI slot stays an explicit wait')
        check(components.count('recording_semantic') == 1, 'one semantic call per wait')
        check(finish_calls[-1] == ('deferred', 'chat_ai_priority'), 'waiting action never becomes failed')
        os.environ.pop("DINGTALK_LOCAL_ONLY", None)

    print(json.dumps({"verdict": "pass", "checks": 17}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
