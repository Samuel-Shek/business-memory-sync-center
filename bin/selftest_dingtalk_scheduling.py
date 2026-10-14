#!/usr/bin/env python3
"""Offline checks for lock-screen review gates and one-click sync orchestration."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import contextlib
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dingtalk_idle_review as idle
import dingtalk_sync_orchestrator as orchestrator
import dingtalk_sync_v2 as syncv2


def check(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


class FakeLockHandle:
    def fileno(self) -> int:
        return 91


def main() -> int:
    progress_receipt = orchestrator.decode_component_receipt(
        "[1/1] 正在同步听记...\n{\n  \"status\": \"success\",\n  \"created\": 1\n}\n"
    )
    check(progress_receipt["created"] == 1, "one-click sync accepts the final JSON receipt after progress lines")
    try:
        orchestrator.decode_component_receipt('{"status":"success"}\n非回执尾部')
        raise AssertionError("trailing non-receipt output was accepted")
    except orchestrator.OrchestratorError as exc:
        check(exc.code == "component_invalid_receipt", "one-click sync rejects ambiguous trailing output")
    check(
        not orchestrator.chat_reconcile_complete({"incremental": {"status": "partial", "conversations_incomplete": 2}}),
        "one-click sync does not report success while chat history remains incomplete",
    )
    check(
        orchestrator.chat_reconcile_complete({"incremental": {"status": "success", "conversations_incomplete": 0}}),
        "one-click sync accepts a complete chat reconciliation receipt",
    )

    review_config = {
        "dingtalk_review": {
            "trigger": "screen_locked",
            "drain_while_locked": True,
            "batch_limit": 2,
            "check_interval_seconds": 60,
            "finish_current_on_unlock": True,
            "manual_limit": 2,
            "retry_failed_automatically": False,
            "require_ac_power": True,
        }
    }
    finished = []
    with mock.patch.object(idle.chat_memory, "load_config", return_value=review_config), \
            mock.patch.object(idle.syncv2, "finish_action_run", side_effect=lambda *a, **k: finished.append((a, k))), \
            mock.patch.object(idle.syncv2, "set_action_step"), \
            mock.patch.object(idle.syncv2, "MANUAL_SYNC_FLAG", Path("/nonexistent-dingtalk-manual-flag")), \
            mock.patch.object(idle.syncv2, "minutes_review_handoff_pending", return_value=False), \
            mock.patch.object(idle, "maintenance_active", return_value=False), \
            mock.patch.object(idle, "screen_is_locked", return_value=False), \
            mock.patch.object(subprocess, "run") as run:
        result = idle.run_review(Path("config"), "locked-unlocked", manual=False)
        check(result["reason"] == "screen_unlocked" and not run.called, "automatic review requires the screen to be locked")

    with mock.patch.object(idle.chat_memory, "load_config", return_value=review_config), \
            mock.patch.object(idle.syncv2, "finish_action_run"), \
            mock.patch.object(idle.syncv2, "set_action_step"), \
            mock.patch.object(idle.syncv2, "MANUAL_SYNC_FLAG", Path("/nonexistent-dingtalk-manual-flag")), \
            mock.patch.object(idle.syncv2, "minutes_review_handoff_pending", return_value=False), \
            mock.patch.object(idle, "maintenance_active", return_value=False), \
            mock.patch.object(idle, "screen_is_locked", return_value=True), \
            mock.patch.object(idle, "power_allows_review", return_value=False), \
            mock.patch.object(subprocess, "run") as run:
        result = idle.run_review(Path("config"), "idle-battery", manual=False)
        check(result["reason"] == "not_on_ac_power" and not run.called, "automatic review waits for AC power")

    commands = []
    dispatch_environments = []
    progress_calls = []
    def fake_dispatch(command, **kwargs):
        commands.append(command)
        dispatch_environments.append(kwargs.get("env") or {})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(idle.chat_memory, "load_config", return_value=review_config), \
            mock.patch.object(idle.syncv2, "finish_action_run"), \
            mock.patch.object(idle.syncv2, "set_action_step", side_effect=lambda *a, **k: progress_calls.append((a, k))), \
            mock.patch.object(idle.syncv2, "MANUAL_SYNC_FLAG", Path("/nonexistent-dingtalk-manual-flag")), \
            mock.patch.object(idle.syncv2, "file_lock", side_effect=lambda *_a, **_k: contextlib.nullcontext(FakeLockHandle())), \
            mock.patch.object(idle, "read_dispatch_status", return_value={"done": 1, "failed": 0, "candidates": 3}), \
            mock.patch.object(subprocess, "run", side_effect=fake_dispatch):
        result = idle.run_review(Path("config"), "manual", manual=True)
        check(
            result["processed"] == 1
            and commands[0][commands[0].index("--limit") + 1] == "2",
            "manual review processes at most two",
        )
        check("--retry-failed" not in commands[0], "failed paid review is not retried automatically")
        check(
            commands[0][commands[0].index("--shared-ai-lock-fd") + 1] == "91",
            "manual review passes the inherited shared AI lock handle",
        )
        check(dispatch_environments[0].get("DINGTALK_ACTION_RUN_ID") == "manual", "review worker receives its persistent progress run id")
        check(any(call[0][1] == "review_wait_lock" and call[0][4] == "running" for call in progress_calls), "manual review exposes lock waiting as a named step")

    commands.clear()
    dispatch_statuses = [
        {"done": 2, "failed": 0, "deferred": 0, "candidates": 4},
        {"done": 1, "failed": 0, "deferred": 0, "candidates": 1},
        {"done": 0, "failed": 0, "deferred": 0, "candidates": 0},
    ]
    with mock.patch.object(idle.chat_memory, "load_config", return_value=review_config), \
            mock.patch.object(idle.syncv2, "finish_action_run"), \
            mock.patch.object(idle.syncv2, "set_action_step"), \
            mock.patch.object(idle.syncv2, "MANUAL_SYNC_FLAG", Path("/nonexistent-dingtalk-manual-flag")), \
            mock.patch.object(idle.syncv2, "minutes_review_handoff_pending", return_value=False), \
            mock.patch.object(idle.syncv2, "file_lock", side_effect=lambda *_a, **_k: contextlib.nullcontext(FakeLockHandle())), \
            mock.patch.object(idle, "maintenance_active", return_value=False), \
            mock.patch.object(idle, "screen_is_locked", return_value=True), \
            mock.patch.object(idle, "power_allows_review", return_value=True), \
            mock.patch.object(idle, "keep_awake", return_value=contextlib.nullcontext()), \
            mock.patch.object(idle, "read_dispatch_status", side_effect=dispatch_statuses), \
            mock.patch.object(subprocess, "run", side_effect=fake_dispatch):
        result = idle.run_review(Path("config"), "locked-drain", manual=False)
    check(result["done"] == 3 and len(commands) == 3, "locked review drains consecutive batches until no ready work remains")
    check(all(command[command.index("--limit") + 1] == "2" for command in commands), "every automatic batch is capped at two")
    check(all("--automatic" in command for command in commands), "automatic batches recheck the live gate between items")

    commands.clear()
    dispatch_kwargs = []
    def fake_handoff_dispatch(command, **kwargs):
        commands.append(command)
        dispatch_kwargs.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(idle.chat_memory, "load_config", return_value=review_config), \
            mock.patch.object(idle.syncv2, "finish_action_run"), \
            mock.patch.object(idle.syncv2, "set_action_step"), \
            mock.patch.object(idle.syncv2, "MANUAL_SYNC_FLAG", Path("/nonexistent-dingtalk-manual-flag")), \
            mock.patch.object(idle.syncv2, "minutes_review_handoff_pending", return_value=True), \
            mock.patch.object(idle.syncv2, "file_lock", side_effect=lambda *_a, **_k: contextlib.nullcontext(FakeLockHandle())), \
            mock.patch.object(idle, "screen_is_locked") as locked, \
            mock.patch.object(idle, "power_allows_review") as power, \
            mock.patch.object(idle, "read_dispatch_status", return_value={"done": 1, "deferred": 0, "failed": 0, "candidates": 1}), \
            mock.patch.object(subprocess, "run", side_effect=fake_handoff_dispatch):
        result = idle.run_review(Path("config"), "handoff", manual=False)
    check(result["mode"] == "handoff" and result["done"] == 1, "scheduled fallback enters handoff-only mode")
    check("--reconcile-deferred-only" in commands[0], "handoff-only mode cannot invoke a paid transcript review")
    check(dispatch_kwargs[0].get("pass_fds") == (91,), "handoff inherits the already-held shared AI lock")
    check(not locked.called and not power.called, "handoff bypasses only the lock-screen and AC gates")

    start_action = mock.Mock(return_value="should-not-start")
    args = SimpleNamespace(config=Path("config"), manual=False, handoff=False, run_id=None)
    with mock.patch.object(idle, "parse_args", return_value=args), \
            mock.patch.object(idle, "maintenance_active", return_value=False), \
            mock.patch.object(idle.syncv2, "initialize"), \
            mock.patch.object(idle, "recover_stale_runtime", return_value={"status": "success", "recovered": 0}), \
            mock.patch.object(idle.syncv2, "minutes_review_handoff_pending", return_value=False), \
            mock.patch.object(idle.syncv2, "start_action_run", start_action), \
            mock.patch.object(idle.chat_memory, "load_config", return_value=review_config), \
            mock.patch.object(idle, "automatic_gate_reason", return_value="screen_unlocked"):
        check(idle.main() == 0, "unlocked preflight exits cleanly")
    check(not start_action.called, "unlocked checks do not create redundant activity rows")

    start_action.reset_mock()
    with mock.patch.object(idle, "parse_args", return_value=args), \
            mock.patch.object(idle, "maintenance_active", return_value=False), \
            mock.patch.object(idle.syncv2, "initialize"), \
            mock.patch.object(idle, "recover_stale_runtime", return_value={"status": "success", "recovered": 0}), \
            mock.patch.object(idle.syncv2, "minutes_review_handoff_pending", return_value=False), \
            mock.patch.object(idle.syncv2, "start_action_run", start_action), \
            mock.patch.object(idle.chat_memory, "load_config", return_value=review_config), \
            mock.patch.object(idle, "automatic_gate_reason", side_effect=idle.IdleReviewError("screen_lock_state_unavailable")):
        check(idle.main() == 2, "unreadable lock state fails closed")
    check(not start_action.called, "state-read failures do not create a paid-review activity row")

    start_action.reset_mock()
    policy = mock.Mock()
    with mock.patch.object(idle, "parse_args", return_value=args), \
            mock.patch.object(idle, "maintenance_active", return_value=False), \
            mock.patch.object(idle.syncv2, "initialize"), \
            mock.patch.object(idle, "recover_stale_runtime", return_value={"status": "success", "recovered": 0}), \
            mock.patch.object(idle.syncv2, "minutes_review_handoff_pending", return_value=False), \
            mock.patch.object(idle.syncv2, "start_action_run", start_action), \
            mock.patch.object(idle.chat_memory, "load_config", return_value=review_config), \
            mock.patch.object(idle, "automatic_gate_reason", return_value=""), \
            mock.patch.object(idle, "automatic_slots_available", return_value=False), \
            mock.patch.object(idle, "policy_snapshot", policy):
        check(idle.main() == 0, "busy model or sync lanes defer cleanly")
    check(not start_action.called and not policy.called, "busy-lane polling creates no redundant activity rows")

    with tempfile.TemporaryDirectory() as gate_temporary:
        gate_root = Path(gate_temporary)
        manual_flag = gate_root / "manual.active"
        manual_flag.write_text("active\n", encoding="utf-8")
        with mock.patch.object(idle.chat_memory, "load_config", return_value=review_config), \
                mock.patch.object(idle.syncv2, "finish_action_run"), \
                mock.patch.object(idle.syncv2, "set_action_step"), \
                mock.patch.object(idle.syncv2, "MANUAL_SYNC_FLAG", manual_flag), \
                mock.patch.object(subprocess, "run") as run:
            gated = idle.run_review(Path("config"), "handoff-gated", manual=False, handoff=True)
        check(gated["reason"] == "manual_sync_priority" and not run.called,
              "handoff still yields to an explicit manual sync")
        maintenance = gate_root / "maintenance.json"
        maintenance.write_text('{"status":"active"}', encoding="utf-8")
        with mock.patch.object(idle, "MAINTENANCE_STATE", maintenance):
            check(idle.maintenance_active(), "handoff recognizes unified maintenance mode before writing state")
        maintenance.write_text('{invalid', encoding="utf-8")
        with mock.patch.object(idle, "MAINTENANCE_STATE", maintenance):
            try:
                idle.maintenance_active()
                raise AssertionError("invalid maintenance state was accepted")
            except idle.IdleReviewError as exc:
                check(exc.code == "maintenance_state_unavailable", "invalid maintenance state fails closed")

    with tempfile.TemporaryDirectory() as handoff_temporary:
        handoff_root = Path(handoff_temporary)
        ledger = handoff_root / "review-ledger.json"
        entry = handoff_root / "dingtalk_idle_review.py"
        entry.write_text("# test entry\n", encoding="utf-8")
        ledger.write_text(
            '{"legacy":{"status":"review_done_reconcile_failed",'
            '"reason":"核对稿已生成，但记忆回写失败：chat_ai_priority"}}',
            encoding="utf-8",
        )
        child = SimpleNamespace(
            returncode=0,
            stdout='diagnostic\n{"status":"success","processed":1,"done":1,"deferred":0,"failed":0}\n',
            stderr="private child detail",
        )
        with mock.patch.object(syncv2, "MINUTES_REVIEW_LEDGER", ledger), \
                mock.patch.object(syncv2, "MINUTES_IDLE_REVIEW", entry), \
                mock.patch.object(syncv2.subprocess, "run", return_value=child):
            check(syncv2.minutes_review_handoff_pending(), "exact legacy AI collision is eligible for safe handoff")
            receipt = syncv2.run_minutes_review_handoff()
        check(
            receipt == {"status": "success", "reason": "", "processed": 1, "done": 1, "deferred": 0, "failed": 0},
            "handoff exposes only the privacy-safe receipt fields",
        )
        ledger.write_text(
            '{"real":{"status":"review_done_reconcile_failed",'
            '"reason":"核对稿已生成，但记忆回写失败：codex_timeout"}}',
            encoding="utf-8",
        )
        with mock.patch.object(syncv2, "MINUTES_REVIEW_LEDGER", ledger):
            check(not syncv2.minutes_review_handoff_pending(), "real model failures never enter automatic handoff")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        replacements = {
            "ROOT": root / "state-root", "STATE_DIR": root / "state-root" / "state",
            "RUNS_DIR": root / "state-root" / "runs", "ARCHIVE_DIR": root / "state-root" / "archive",
            "DATABASE": root / "state-root" / "state" / "sync.sqlite",
            "AI_LOCK": root / "state-root" / "state" / "ai.lock",
            "SOURCE_LOCK": root / "state-root" / "state" / "source.lock",
            "SEMANTIC_LOCK": root / "state-root" / "state" / "semantic.lock",
            "DECISION_LOCK": root / "state-root" / "state" / "decision.lock",
            "MEDIA_LOCK": root / "state-root" / "state" / "media.lock",
            "MANUAL_SYNC_LOCK": root / "state-root" / "state" / "manual.lock",
            "MANUAL_SYNC_FLAG": root / "state-root" / "state" / "manual.active",
            "REVIEW_LOCK": root / "state-root" / "state" / "review.lock",
        }
        config = {"external_root": str(root / "external"), "memory_root": str(root / "memory")}
        commands = []
        changed = {"value": False}
        validation_modes = []

        def fake_validate(_config, **kwargs):
            validation_modes.append(bool(kwargs.get("refresh_indexes", True)))
            return {"validator": {"verdict": "pass"}}

        def fake_component(command, **_kwargs):
            commands.append([str(value) for value in command])
            text = " ".join(str(value) for value in command)
            if "sync_minutes.py" in text:
                return {"created": 0, "updated": 0}
            if "dingtalk_minutes_memory.py" in text:
                return {"status": "success", "source_notes_written": 0}
            if "dingtalk_memory_automation.py" in text:
                inserted = 1 if changed["value"] else 0
                return {"status": "noop", "incremental": {"messages_inserted": inserted, "messages_updated": 0}, "writes": 0, "model_called": False}
            if "dingtalk_decision_center.py" in text:
                return {"status": "success", "registered": {"created": 0, "updated": 0}}
            raise AssertionError(text)

        with mock.patch.multiple(syncv2, **replacements), \
                mock.patch.object(orchestrator.chat_memory, "load_config", return_value=config), \
                mock.patch.object(orchestrator.chat_memory, "eligible_queue_count", return_value=0), \
                mock.patch.object(orchestrator.chat_memory, "run_refresh_and_validate", side_effect=fake_validate), \
                mock.patch.object(orchestrator, "recording_queue", return_value=[]), \
                mock.patch.object(orchestrator, "run_json", side_effect=fake_component), \
                mock.patch.object(syncv2, "emit_outbox", wraps=syncv2.emit_outbox) as emit:
            syncv2.initialize()
            abandoned = syncv2.start_action_run("sync_now")
            protected = syncv2.start_action_run("sync_now")
            syncv2.set_action_step(abandoned, "recording_source", 50, "写入录音来源", "running")
            syncv2.set_action_step(abandoned, "chat_reconcile", 60, "核对聊天覆盖", "queued")
            orchestrator.chat_memory.atomic_write(syncv2.MANUAL_SYNC_FLAG, "stale\n", mode=0o600)
            check(
                orchestrator.recover_abandoned_manual_runs(exclude_run_id=protected) == 1,
                "crashed one-click sync is marked interrupted on restart",
            )
            connection = syncv2.connect_readonly()
            try:
                abandoned_status = connection.execute(
                    "SELECT status,error_code FROM action_runs WHERE run_id=?", (abandoned,)
                ).fetchone()
                protected_status = connection.execute(
                    "SELECT status,error_code FROM action_runs WHERE run_id=?", (protected,)
                ).fetchone()
                abandoned_steps = {
                    row["step_key"]: row["status"]
                    for row in connection.execute(
                        "SELECT step_key,status FROM action_steps WHERE run_id=?", (abandoned,)
                    )
                }
            finally:
                connection.close()
            check(
                abandoned_status["status"] == "interrupted"
                and abandoned_status["error_code"] == "interrupted_after_process_exit"
                and not syncv2.MANUAL_SYNC_FLAG.exists(),
                "crash recovery clears the stale priority flag without claiming success",
            )
            check(
                protected_status["status"] == "running" and protected_status["error_code"] is None,
                "crash recovery never marks the App's current one-click run interrupted",
            )
            check(
                abandoned_steps == {"recording_source": "interrupted", "chat_reconcile": "skipped"},
                "crash recovery closes running and queued steps consistently",
            )
            first_run = protected
            result = orchestrator.run_sync_now(Path("config"), first_run)
            check(result["status"] == "noop" and result["model_calls"] == 0, "empty one-click sync is a no-model no-op")
            steps = {item["key"]: item for item in syncv2.list_action_steps(first_run)}
            check(steps["wait_review"]["status"] == "success" and steps["notification"]["status"] == "skipped", "no-change sync persists lock and notification step outcomes")
            check(emit.call_count == 0, "empty one-click sync does not create a duplicate notification")
            check(not any("review" in " ".join(command).lower() for command in commands), "sync button never invokes review")
            check(not syncv2.MANUAL_SYNC_FLAG.exists(), "manual priority flag is always cleared")
            check(validation_modes == [False], "no-change sync validates without rewriting memory indexes")

            changed["value"] = True
            commands.clear()
            second_run = syncv2.start_action_run("sync_now")
            result = orchestrator.run_sync_now(Path("config"), second_run)
            check(result["status"] == "success", "changed one-click sync reaches success")
            check(validation_modes == [False, True], "changed sync refreshes indexes before validation")
            connection = syncv2.connect_readonly()
            try:
                events = connection.execute("SELECT event_type,COUNT(*) FROM outbox GROUP BY event_type").fetchall()
            finally:
                connection.close()
            check(
                [(row[0], row[1]) for row in events] == [],
                "one changed manual cycle remains local-only and creates no external notification event",
            )

    print('{"verdict":"pass","checks":38}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
