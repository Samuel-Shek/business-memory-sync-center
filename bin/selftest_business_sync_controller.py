#!/usr/bin/env python3
"""Model-free contract tests for the five-source local sync controller."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest import mock

import business_sync_controller as controller


class SyncControllerTests(unittest.TestCase):
    def test_manual_all_records_each_source_without_sending_notifications(self):
        import business_sync_supervisor as supervisor
        receipt = {"run_id": "fixture", "status": "partial", "sources": {
            "dingtalk_all": {"status": "partial", "recording_media_pending": 2, "failures": []},
            "dingtalk_mail": {"status": "success"}, "win11": {"status": "success"},
            "wechat": {"status": "skipped"}}}
        with mock.patch.object(supervisor, 'Supervisor') as service:
            controller.record_manual_chat_result(Path('fixture.json'), 'all', receipt)
            rows = {call.args[0]: call.args[1] for call in service.return_value.record_outcome.call_args_list}
            self.assertEqual(set(rows), {'dingtalk_chat', 'dingtalk_minutes', 'dingtalk_mail', 'win11'})
            self.assertEqual(rows['dingtalk_chat']['status'], 'success')
            self.assertEqual(supervisor.classify_receipt(rows['dingtalk_minutes'], 'dingtalk_minutes')['kind'], 'waiting_source')
            service.return_value.deliver.assert_not_called()

    def test_only_explicit_current_acceptance_opens_the_write_gate(self) -> None:
        core = {"id": "core", "display_status": {"code": "healthy"},
                "activity": {"code": "idle"}, "acceptance": {"code": "pending"}}
        with mock.patch.object(controller, "read_json", return_value={"status": "inactive"}), \
                mock.patch.object(controller.DashboardPaths, "from_config"), \
                mock.patch.object(controller, "SystemDashboard") as dashboard:
            dashboard.return_value.collect.return_value = {"pipelines": [core]}
            for code in ("pending", "attention", "partial", "blocked", "unknown", "unexpected"):
                core["acceptance"]["code"] = code
                self.assertEqual(controller.global_safety_error({}), "memory_core_unverified")
            core["acceptance"]["code"] = "accepted"
            self.assertIsNone(controller.global_safety_error({}))

    def setUp(self) -> None:
        controller.AUTOMATIC_MODE = False

    def tearDown(self) -> None:
        controller.AUTOMATIC_MODE = False

    def common_execute_patches(self):
        return (
            mock.patch.object(controller.chat_memory, "load_config", return_value={}),
            mock.patch.object(controller, "initialize_steps"),
            mock.patch.object(controller, "set_step"),
            mock.patch.object(controller, "mark_running_step_failed"),
        )

    def test_all_runs_in_order_continues_after_source_failure_and_skips_wechat(self) -> None:
        order: list[str] = []

        def dingtalk(*_args, **_kwargs):
            order.append("dingtalk")
            return {"status": "success", "memory_writes": 1, "failures": []}

        def email(*_args, **_kwargs):
            order.append("email")
            raise controller.ControllerError("email_fixture_failed")

        def win11(*_args, **_kwargs):
            order.append("win11")
            return {"status": "success"}

        def wechat(*_args, **_kwargs):
            order.append("wechat")
            return {"status": "skipped", "reason": "enrollment_required"}

        patches = self.common_execute_patches()
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(controller, "require_global_safety"), \
                mock.patch.object(controller, "run_dingtalk_scopes", side_effect=dingtalk), \
                mock.patch.object(controller, "run_email", side_effect=email), \
                mock.patch.object(controller, "run_win11", side_effect=win11), \
                mock.patch.object(controller, "run_wechat", side_effect=wechat):
            result = controller.execute(Path("fixture.json"), "run", "all")
        self.assertEqual(order, ["dingtalk", "email", "win11", "wechat"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_sources"], 2)
        self.assertTrue(result["local_only"])
        self.assertEqual(result["failures"], [{"target": "dingtalk_mail", "error_code": "email_fixture_failed"}])
        self.assertEqual(result["sources"]["wechat"]["status"], "skipped")

    def test_all_stops_remaining_sources_when_global_safety_changes(self) -> None:
        order: list[str] = []
        checks = {"count": 0}

        def safety(_config):
            checks["count"] += 1
            # Initial gate, DingTalk gate and email gate pass; Win11 gate stops the run.
            if checks["count"] == 4:
                raise controller.ControllerError("memory_core_busy", global_stop=True)

        def dingtalk(*_args, **_kwargs):
            order.append("dingtalk")
            raise controller.ControllerError("dingtalk_fixture_failed")

        def email(*_args, **_kwargs):
            order.append("email")
            return {"status": "success", "memory_writes": 1}

        patches = self.common_execute_patches()
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(controller, "require_global_safety", side_effect=safety), \
                mock.patch.object(controller.time, "monotonic", side_effect=iter(range(0, 1000, 100))), \
                mock.patch.object(controller, "run_dingtalk_scopes", side_effect=dingtalk), \
                mock.patch.object(controller, "run_email", side_effect=email), \
                mock.patch.object(controller, "run_win11") as win11, \
                mock.patch.object(controller, "run_wechat") as wechat:
            result = controller.execute(Path("fixture.json"), "run", "all")
        self.assertEqual(order, ["dingtalk", "email"])
        win11.assert_not_called()
        wechat.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sources"]["global"]["error_code"], "memory_core_busy")

    def test_source_boundary_waits_for_new_validation_but_never_bypasses_failure(self) -> None:
        with mock.patch.object(controller, 'require_global_safety', side_effect=[controller.ControllerError('memory_core_unverified'), None]) as gate, \
             mock.patch.object(controller.time, 'sleep') as pause:
            controller.wait_global_safety({})
        self.assertEqual(gate.call_count, 2)
        pause.assert_called_once()
        with mock.patch.object(controller, 'require_global_safety', side_effect=controller.ControllerError('memory_core_unavailable')) as gate, \
             mock.patch.object(controller.time, 'sleep') as pause:
            with self.assertRaises(controller.ControllerError):
                controller.wait_global_safety({})
        self.assertEqual(gate.call_count, 1)
        pause.assert_not_called()

    def test_each_direct_target_starts_only_its_own_operation(self) -> None:
        patches = self.common_execute_patches()
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(controller, "require_global_safety"), \
                mock.patch.object(
                    controller,
                    "run_dingtalk_scopes",
                    return_value={"status": "noop", "failures": []},
                ) as dingtalk, \
                mock.patch.object(controller, "run_email") as email, \
                mock.patch.object(controller, "run_win11") as win11, \
                mock.patch.object(controller, "run_wechat") as wechat:
            result = controller.execute(Path("fixture.json"), "run", "dingtalk_chat")
        dingtalk.assert_called_once_with(Path("fixture.json"), "run", ("chat",))
        email.assert_not_called()
        win11.assert_not_called()
        wechat.assert_not_called()
        self.assertEqual(result["target"], "dingtalk_chat")

    def test_minutes_media_gap_can_only_finish_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            flag = Path(temporary) / "manual.flag"
            with mock.patch.object(controller.chat_memory, "load_config", return_value={}), \
                    mock.patch.object(controller.chat_memory, "atomic_write"), \
                    mock.patch.object(controller.syncv2, "MANUAL_SYNC_FLAG", flag), \
                    mock.patch.object(controller.syncv2, "file_lock", return_value=nullcontext()), \
                    mock.patch.object(controller.syncv2, "set_component_health"), \
                    mock.patch.object(controller, "set_step"), \
                    mock.patch.object(controller, "run_minutes_part", return_value=True), \
                    mock.patch.object(controller, "finalize_dingtalk"):
                result = controller.run_dingtalk_scopes(
                    Path("fixture.json"), "run", ("minutes",)
                )
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["local_only"])

    def test_direct_minutes_partial_is_preserved_by_action_summary(self) -> None:
        patches = self.common_execute_patches()
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(controller, "require_global_safety"), \
                mock.patch.object(
                    controller,
                    "run_dingtalk_scopes",
                    return_value={
                        "status": "partial",
                        "recording_media_pending": 2,
                        "memory_writes": 5,
                        "failures": [],
                    },
                ):
            result = controller.execute(Path("fixture.json"), "run", "dingtalk_minutes")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sources"]["dingtalk_minutes"]["recording_media_pending"], 2)

    def test_email_controller_uses_sync_local_and_rejects_unconfirmed_policy(self) -> None:
        good = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "status": "completed", "report_policy": "local_only", "local_only": True,
                "discovered": 2, "semantic": {"writes": 1}, "reports_queued": 0,
                "delivery": {"delivered": 0, "failed": 0, "uncertain": 0},
            }),
            stderr="",
        )
        with mock.patch.object(controller, "set_step"), \
                mock.patch.object(controller, "run_process", return_value=good) as run:
            result = controller.run_email(Path("fixture.json"), "run")
        self.assertIn("sync-local", run.call_args.args[0])
        self.assertEqual(result["reports_queued"], 0)
        self.assertEqual(result["delivery_count"], 0)
        self.assertTrue(result["local_only"])

        bad = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"status": "completed", "report_policy": "normal", "local_only": False}),
            stderr="",
        )
        with mock.patch.object(controller, "set_step"), \
                mock.patch.object(controller, "run_process", return_value=bad):
            with self.assertRaises(controller.ControllerError) as raised:
                controller.run_email(Path("fixture.json"), "run")
        self.assertEqual(raised.exception.code, "email_local_only_unconfirmed")

    def test_email_controller_preserves_partial_technical_failure(self) -> None:
        partial = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "status": "completed", "report_policy": "local_only", "local_only": True,
                "archive": {"attempted": 3, "archived": 1, "failed": 2},
                "semantic": {"failed": 1, "writes": 0},
                "delivery": {"delivered": 0, "failed": 0, "uncertain": 0},
            }),
            stderr="",
        )
        with mock.patch.object(controller, "set_step") as step, \
                mock.patch.object(controller, "run_process", return_value=partial):
            result = controller.run_email(Path("fixture.json"), "run")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_items"], 3)
        self.assertEqual(
            result["failures"],
            [{"target": "dingtalk_mail", "error_code": "email_items_failed"}],
        )
        self.assertEqual(step.call_args.args[2], "partial")
        self.assertEqual(step.call_args.kwargs["error_code"], "email_items_failed")

    def test_email_controller_keeps_frozen_archive_items_unresolved(self) -> None:
        frozen = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "status": "noop", "report_policy": "local_only", "local_only": True,
                "archive": {
                    "attempted": 0, "archived": 0, "failed": 0,
                    "frozen": 2, "deferred": 0, "unresolved": 2,
                },
                "semantic": {"failed": 0, "writes": 0},
                "delivery": {"delivered": 0, "failed": 0, "uncertain": 0},
            }),
            stderr="",
        )
        with mock.patch.object(controller, "set_step") as step, \
                mock.patch.object(controller, "run_process", return_value=frozen):
            result = controller.run_email(Path("fixture.json"), "run")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_items"], 2)
        self.assertEqual(result["archive_frozen"], 2)
        self.assertEqual(step.call_args.kwargs["detail"]["archive_frozen"], 2)

    def test_wechat_enrollment_skip_never_starts_collector(self) -> None:
        with mock.patch.object(controller, "wechat_available", return_value=False), \
                mock.patch.object(controller, "set_step") as step, \
                mock.patch.object(controller, "run_process") as process:
            result = controller.run_wechat({}, "run", allow_skip=True)
        self.assertEqual(result, {"status": "skipped", "reason": "enrollment_required"})
        self.assertEqual(step.call_args.args[2], "skipped")
        process.assert_not_called()

    def test_control_lock_conflict_is_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(controller, "CONTROL_LOCK", Path(temporary) / "control.lock"), \
                mock.patch.object(controller.fcntl, "flock", side_effect=BlockingIOError):
            with self.assertRaises(controller.ControllerError) as raised:
                with controller.control_lock():
                    self.fail("busy lock entered")
        self.assertEqual(raised.exception.code, "already_running")
        self.assertTrue(raised.exception.global_stop)

    def test_dingtalk_manual_environment_is_local_only(self) -> None:
        environment = controller.dingtalk_environment("run")
        self.assertEqual(environment["DINGTALK_LOCAL_ONLY"], "1")
        self.assertEqual(environment["DINGTALK_SUPPRESS_COMPLETION_OUTBOX"], "1")
        self.assertEqual(environment["DINGTALK_ACTION_RUN_ID"], "run")

    def test_automatic_run_pauses_at_a_stage_boundary_after_power_loss(self) -> None:
        controller.AUTOMATIC_MODE = True
        with mock.patch.object(controller, "example_bd50c44d55d3", return_value=False):
            with self.assertRaises(controller.ControllerError) as raised:
                controller.require_automatic_power()
        self.assertEqual(raised.exception.code, "waiting_power")
        self.assertTrue(raised.exception.global_stop)

    def test_automatic_minutes_uses_scheduled_hash_ledger_mode(self) -> None:
        controller.AUTOMATIC_MODE = True
        result = {
            "recordings_new": 0, "recordings_updated": 0, "recording_media_pending": 0,
            "chat_messages_changed": 0, "memory_writes": 0, "model_calls": 0,
            "decision_created": 0, "decision_updated": 0,
        }
        with mock.patch.object(controller, "require_automatic_power"), \
                mock.patch.object(controller, "set_step"), \
                mock.patch.object(controller.syncv2, "set_component_health"), \
                mock.patch.object(controller.dingtalk, "recording_media_partial", return_value=False), \
                mock.patch.object(controller.dingtalk, "recording_source_media_partial", return_value=False), \
                mock.patch.object(controller.dingtalk, "recording_queue", return_value=[]), \
                mock.patch.object(
                    controller.dingtalk, "run_json",
                    side_effect=[{"selected": 0, "created": 0, "updated": 0}, {"source_notes_written": 0}],
                ) as run:
            partial = controller.run_minutes_part(Path("fixture.json"), "run", {}, result)
        self.assertFalse(partial)
        self.assertIn("--scheduled", run.call_args_list[0].args[0])
        self.assertNotIn("--manual", run.call_args_list[0].args[0])

    def test_automatic_dingtalk_defers_without_waiting_for_active_review(self) -> None:
        controller.AUTOMATIC_MODE = True
        lock_calls: list[tuple[Path, bool]] = []

        @contextmanager
        def fake_lock(path: Path, blocking: bool = False):
            lock_calls.append((path, blocking))
            if path == controller.syncv2.REVIEW_LOCK:
                raise controller.syncv2.SyncV2Error("already_running", "review active")
            yield

        with tempfile.TemporaryDirectory() as temporary:
            flag = Path(temporary) / "manual.flag"

            def write_flag(path: Path, content: str, *, mode: int) -> None:
                del mode
                path.write_text(content, encoding="utf-8")

            with mock.patch.object(controller.chat_memory, "load_config", return_value={}), \
                    mock.patch.object(controller.chat_memory, "atomic_write", side_effect=write_flag), \
                    mock.patch.object(controller.syncv2, "MANUAL_SYNC_FLAG", flag), \
                    mock.patch.object(controller.syncv2, "file_lock", side_effect=fake_lock), \
                    mock.patch.object(controller, "set_step") as step:
                with self.assertRaises(controller.ControllerError) as raised:
                    controller.run_dingtalk_scopes(
                        Path("fixture.json"), "run", ("minutes",)
                    )

        self.assertEqual(raised.exception.code, "already_running")
        self.assertIn((controller.syncv2.REVIEW_LOCK, False), lock_calls)
        self.assertFalse(flag.exists())
        step.assert_any_call(
            "run",
            "wait_review",
            "deferred",
            detail={"reason": "review_busy"},
            error_code="already_running",
        )

    def test_automatic_win11_uses_quiet_hourly_mode(self) -> None:
        controller.AUTOMATIC_MODE = True
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(controller, "require_automatic_power"), \
                mock.patch.object(controller, "set_step"), \
                mock.patch.object(controller, "run_process", return_value=completed) as run:
            result = controller.run_win11("run")
        self.assertEqual(run.call_args.args[0], [str(controller.WIN11_SYNC), "--hourly"])
        self.assertEqual(result["status"], "success")

    def test_manual_win11_preserves_force_scan_mode(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(controller, "require_automatic_power"), \
                mock.patch.object(controller, "set_step"), \
                mock.patch.object(controller, "run_process", return_value=completed) as run:
            controller.run_win11("run")
        self.assertEqual(run.call_args.args[0], [str(controller.WIN11_SYNC)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
