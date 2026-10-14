#!/usr/bin/env python3
"""Offline fixtures for the unified read-only sync dashboard."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import fcntl
import json
import multiprocessing
import os
import plistlib
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import business_sync_state as supervisor_state

from system_dashboard_status import (
    ABNORMAL_SOURCE_STATUS_CODES,
    CommandResult,
    DashboardPaths,
    SystemDashboard,
    fold_events,
    primary_overall,
    probe_lock,
)


CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(message)
    CHECKS += 1


def write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    path.chmod(mode)


def write_toml(path: Path, name: str, status: str, hour: int, minute: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'kind = "cron"\nname = "{name}"\nstatus = "{status}"\n'
        f'rrule = "RRULE:FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute}"\n',
        encoding="utf-8",
    )


def build_paths(root: Path) -> DashboardPaths:
    external = root / "external"
    state = root / "state"
    launch_agents = root / "LaunchAgents"
    automations = root / "automations"
    return DashboardPaths(
        root=root,
        state_root=state,
        external_root=external,
        launch_agents_root=launch_agents,
        automations_root=automations,
        email_database=external / "email-sync" / "state" / "email-sync.sqlite",
        email_safety_receipt=external / "email-sync" / "read-safety.json",
        email_runtime_error=external / "email-sync" / "state" / "last-runtime-error.json",
        win11_database=external / "machine-ledgers" / "win11.sqlite",
        win11_automation_log=root / "logs" / "win11-automation.jsonl",
        wechat_status=external / "wechat-chat" / "state" / "status.json",
        chatgpt_ledger=external / "machine-ledgers" / "对话同步台账.json",
        chatgpt_automation=automations / "chatgpt" / "automation.toml",
        win11_automation=automations / "win11" / "automation.toml",
        wechat_automation=automations / "wechat" / "automation.toml",
        dingtalk_chat_status=external / "dingtalk-chat" / "state" / "status.json",
        dingtalk_chat_incremental_status=external / "dingtalk-chat" / "state" / "incremental.json",
        dingtalk_automation_status=external / "dingtalk-chat" / "state" / "automation.json",
        minutes_sync_state=external / "minutes" / "sync-state.json",
        minutes_inventory=external / "minutes" / "inventory.json",
        bridge_database=state / "bridge.sqlite",
        folder_watcher_health=state / "folder-watcher-health.json",
        win11_health=state / "win11-health.json",
        receiver_health=state / "receiver-health.json",
        maintenance_state=state / "maintenance.json",
        validation_failure=state / "validation-failure.json",
        validation_success=state / "validation-success.json",
        supervisor_database=external / "dingtalk-sync-v2" / "state" / "sync.sqlite",
    )


def write_validation_success(paths: DashboardPaths, stamp: str) -> None:
    import validation_evidence
    memory_root = paths.root / "vault" / "memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    write_json(paths.root / "config.json", {"memory_root": str(memory_root)})
    for name in validation_evidence.CODE_PATHS:
        target = paths.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("isolated fixture", encoding="utf-8")
    write_json(paths.validation_success, {
        "schema_version": 2, "full_validation": True, "verdict": "pass", "recorded_at": stamp,
        "index_dirty_generation": "7", "index_applied_generation": "7",
        "validation_context": validation_evidence.context(paths.root),
    })


def initialize_databases(paths: DashboardPaths) -> None:
    supervisor_state.initialize(paths.supervisor_database)
    supervisor_state.update_runtime(
        paths.supervisor_database, status="idle", heartbeat_at="2020-01-02T13:41:30+08:00",
        power_source="ac", on_ac=True, idle_seconds=600, idle_ready=True,
        last_scan_at="2020-01-02T13:40:00+08:00", next_scan_at="2020-01-02T13:45:00+08:00",
        pending_count=0,
    )
    for source_id in supervisor_state.SOURCE_IDS:
        supervisor_state.update_source(
            source_id, paths.supervisor_database, status="healthy", attempt_count=0,
            last_probe_at="2020-01-02T13:40:00+08:00",
            last_success="2020-01-02T13:40:00+08:00",
            next_due="2020-01-02T13:45:00+08:00",
        )
    paths.email_database.parent.mkdir(parents=True, exist_ok=True)
    email = sqlite3.connect(paths.email_database)
    email.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE batches(
          status TEXT,created_at TEXT,completed_at TEXT,last_error_code TEXT,
          discovered_count INTEGER,archived_count INTEGER,memory_applied_count INTEGER,failed_count INTEGER
        );
        CREATE TABLE messages(
          archive_state TEXT,memory_state TEXT,report_state TEXT,last_error_code TEXT
        );
        CREATE TABLE attachments(status TEXT,original_name TEXT,message_key TEXT,attachment_id TEXT,
                                 sha256 TEXT,actual_size INTEGER,stored_name TEXT);
        INSERT INTO batches VALUES(
          'reported','2020-01-02T13:20:00+08:00','2020-01-02T13:21:00+08:00',NULL,1,1,1,0
        );
        INSERT INTO messages VALUES('archived','applied','reported',NULL);
        """
    )
    email.commit()
    email.close()

    paths.win11_database.parent.mkdir(parents=True, exist_ok=True)
    win11 = sqlite3.connect(paths.win11_database)
    win11.executescript(
        """
        CREATE TABLE runs(
          kind TEXT,status TEXT,created_at TEXT,completed_at TEXT,
          writes_count INTEGER,error_code TEXT
        );
        INSERT INTO runs VALUES(
          'incremental','failed','2020-01-02T12:00:00+08:00',
          '2020-01-02T12:01:00+08:00',0,'fixture_failure'
        );
        INSERT INTO runs VALUES(
          'incremental','completed','2020-01-02T13:00:00+08:00',
          '2020-01-02T13:01:00+08:00',1,NULL
        );
        """
    )
    win11.commit()
    win11.close()

    paths.bridge_database.parent.mkdir(parents=True, exist_ok=True)
    bridge = sqlite3.connect(paths.bridge_database)
    bridge.executescript(
        """
        CREATE TABLE maintenance_state(key TEXT,value TEXT,updated_at TEXT);
        CREATE TABLE source_events(status TEXT,source_id TEXT,processed_at TEXT);
        INSERT INTO maintenance_state VALUES('index_dirty_generation','7','2020-01-02T13:41:00+08:00');
        INSERT INTO maintenance_state VALUES('index_applied_generation','7','2020-01-02T13:41:00+08:00');
        INSERT INTO maintenance_state VALUES('index_last_error','','2020-01-02T13:41:00+08:00');
        INSERT INTO maintenance_state VALUES('index_last_refreshed_at','2020-01-02T13:41:00+08:00','2020-01-02T13:41:00+08:00');
        INSERT INTO source_events VALUES('stored','win11_business_memory','2020-01-02T13:40:00+08:00');
        """
    )
    bridge.commit()
    bridge.close()


def create_fixtures(paths: DashboardPaths) -> None:
    initialize_databases(paths)
    write_json(paths.email_safety_receipt, {
        "server_side_pass": True,
        "cross_device_confirmed": True,
        "verdict": "pass",
        "tested_at": "2020-01-02T12:00:00+08:00",
    })
    write_json(paths.wechat_status, {
        "status": "enrollment_required",
        "error_code": "enrollment_required",
        "started_at": "2020-01-02T11:00:00+08:00",
        "finished_at": "2020-01-02T11:00:01+08:00",
    })
    write_json(paths.chatgpt_ledger, {
        "last_checked_at": "2020-01-02T12:30:00+08:00",
        "last_success_at": "2020-01-02T23:30:00+08:00",
        "last_failure": {"at": "2020-01-02T12:30:00+08:00", "step": "list_threads"},
        "records": {"one": {}},
    })
    write_json(paths.dingtalk_chat_status, {
        "status": "success",
        "started_at": "2020-01-02T13:00:00+08:00",
        "completed_at": "2020-01-02T13:01:00+08:00",
    })
    write_json(paths.dingtalk_chat_incremental_status, {
        "status": "success",
        "started_at": "2020-01-02T13:30:00+08:00",
        "completed_at": "2020-01-02T13:31:00+08:00",
    })
    write_json(paths.dingtalk_automation_status, {"updated_at": "2020-01-02T13:31:00+08:00"})
    write_json(paths.minutes_sync_state, {"last_sync_at": "2020-01-02T13:00:00+08:00"})
    write_json(paths.minutes_inventory, {
        "last_attempt_at": "2020-01-02T13:00:00+08:00",
        "last_attempt_status": "complete",
    })
    write_json(paths.folder_watcher_health, {
        "status": "running",
        "updated_at": "2020-01-02T13:41:00+08:00",
        "queued_events": 0,
    })
    write_json(paths.win11_health, {
        "status": "idle",
        "merge_status": "noop",
        "pending_events": 0,
        "updated_at": "2020-01-02T13:41:00+08:00",
    })
    paths.win11_automation_log.parent.mkdir(parents=True, exist_ok=True)
    paths.win11_automation_log.write_text(
        json.dumps({"at": "2020-01-02T13:35:00+08:00", "event": "prepare_noop"}) + "\n",
        encoding="utf-8",
    )
    write_json(paths.receiver_health, {
        "status": "running",
        "updated_at": "2020-01-02T13:41:00+08:00",
    })
    write_json(paths.maintenance_state, {"status": "inactive", "exited_at": "2020-01-02T10:00:00+08:00"})
    write_json(paths.validation_failure, {
        "verdict": "failed",
        "recorded_at": "2020-01-02T12:00:00+08:00",
    })
    write_toml(paths.chatgpt_automation, "ChatGPT project", "ACTIVE", 23, 30)
    write_toml(paths.win11_automation, "Win11", "ACTIVE", 22, 30)
    write_toml(paths.wechat_automation, "WeChat", "PAUSED", 6, 30)

    labels = [
        "com.example.business-sync-supervisor",
        "com.example.business-dingtalk-memory-listener",
        "com.example.business-dingtalk-chat-sync",
        "com.example.business-dingtalk-memory-hourly",
        "com.example.dingtalk-minutes-sync",
        "com.example.dingtalk-minutes-memory-source",
        "com.example.dingtalk-minutes-memory-semantic",
        "com.example.dingtalk-minutes-review",
        "com.example.business-enterprise-email-sync",
        "com.example.business-win11-memory-hourly",
        "com.example.business-wechat-chat-sync",
        "com.example.business-memory-bridge",
        "com.example.business-memory-folder-watcher",
    ]
    paths.launch_agents_root.mkdir(parents=True, exist_ok=True)
    for label in labels:
        payload: dict[str, Any] = {"Label": label, "RunAtLoad": False}
        if label in {"com.example.business-memory-bridge", "com.example.business-memory-folder-watcher", "com.example.business-dingtalk-memory-listener"}:
            payload["KeepAlive"] = True
            payload["RunAtLoad"] = True
        elif label == "com.example.business-sync-supervisor":
            payload["RunAtLoad"] = True
            payload["StartInterval"] = 60
        else:
            payload["StartCalendarInterval"] = {"Minute": 25}
        with (paths.launch_agents_root / f"{label}.plist").open("wb") as handle:
            plistlib.dump(payload, handle)
    (paths.root / "inbox").mkdir()
    (paths.root / "watch_inbox").mkdir()
    for entrypoint in (
        paths.root / "bin" / "email_sync.py",
        paths.root / "bin" / "business_sync_now.sh",
        paths.root / "bin" / "run_wechat_pipeline.sh",
        paths.minutes_sync_state.parent.parent / "sync_minutes.py",
    ):
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("fixture\n", encoding="utf-8")


class FakeRunner:
    def __init__(
        self,
        disabled: set[str],
        timeout_launchd: bool = False,
        exit_codes: dict[str, int] | None = None,
    ):
        self.disabled = disabled
        self.timeout_launchd = timeout_launchd
        self.exit_codes = exit_codes or {}

    def __call__(self, arguments: list[str], timeout: float) -> CommandResult:
        if arguments[:2] == ["/bin/launchctl", "print-disabled"]:
            if self.timeout_launchd:
                return CommandResult(124, timed_out=True)
            lines = [
                f'"{label}" => {"disabled" if label in self.disabled else "enabled"}'
                for label in sorted(self.disabled | {
                    "com.example.business-sync-supervisor",
                    "com.example.business-dingtalk-memory-listener",
                    "com.example.business-dingtalk-chat-sync",
                    "com.example.business-dingtalk-memory-hourly",
                    "com.example.dingtalk-minutes-sync",
                    "com.example.dingtalk-minutes-memory-source",
                    "com.example.dingtalk-minutes-memory-semantic",
                    "com.example.dingtalk-minutes-review",
                    "com.example.business-win11-memory-hourly",
                    "com.example.business-memory-bridge",
                    "com.example.business-memory-folder-watcher",
                })
            ]
            return CommandResult(0, "\n".join(lines))
        if arguments[:2] == ["/bin/launchctl", "print"]:
            if self.timeout_launchd:
                return CommandResult(124, timed_out=True)
            label = arguments[-1].rsplit("/", 1)[-1]
            if label in self.disabled:
                return CommandResult(113)
            running = label in {
                "com.example.business-memory-bridge",
                "com.example.business-memory-folder-watcher",
                "com.example.business-dingtalk-memory-listener",
            }
            state = "running" if running else "not running"
            exit_code = int(self.exit_codes.get(label, 0))
            return CommandResult(0, f"state = {state}\nruns = 3\nlast exit code = {exit_code}\n")
        if arguments and arguments[0] == "/usr/sbin/lsof":
            return CommandResult(1)
        return CommandResult(1)


def hold_lock(path: str, ready: multiprocessing.synchronize.Event, stop: multiprocessing.synchronize.Event) -> None:
    with open(path, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        ready.set()
        stop.wait(5)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def lock_runner(pid: int, command: str):
    def run(arguments: list[str], _timeout: float) -> CommandResult:
        if arguments and arguments[0] == "/usr/sbin/lsof":
            return CommandResult(0, f"{pid}\n")
        if arguments[:2] == ["/bin/ps", "-p"]:
            return CommandResult(0, f"{command}\n")
        return CommandResult(1)
    return run


def main() -> int:
    fixed_now = datetime(2026, 8, 30, 13, 42, tzinfo=timezone.utc).astimezone(
        timezone.utc
    )
    # Fixture timestamps carry +08:00.  Use the equivalent aware instant.
    fixed_now = datetime.fromisoformat("2020-01-02T13:42:00+08:00")
    with tempfile.TemporaryDirectory(prefix="business-system-dashboard-") as temporary:
        root = Path(temporary)
        paths = build_paths(root)
        create_fixtures(paths)
        write_validation_success(paths, "2020-01-02T13:41:10+08:00")
        config = {"email_sync": {"enabled": True, "writer_registered": True, "poll_minutes": 30}}
        snapshot = {
            "generated_at": "2020-01-02T13:42:00+08:00",
            "sync": {"database_integrity": "ok"},
            "open_incidents": [{
                "incident_type": "fixture_old_failure",
                "created_at": "2020-01-02T12:00:00+08:00",
                "updated_at": "2020-01-02T12:01:00+08:00",
            }],
            "active_actions": [],
            "attention_counts": {},
            "decisions": [],
            "latest_actions": {
                "sync_now": {
                    "action_type": "sync_now",
                    "status": "success",
                    "started_at": "2020-01-02T13:30:00+08:00",
                    "completed_at": "2020-01-02T13:31:00+08:00",
                }
            },
        }
        runner = FakeRunner({
            "com.example.business-enterprise-email-sync",
            "com.example.business-wechat-chat-sync",
        })
        dashboard = SystemDashboard(paths, runner=runner, cache_seconds=0)
        state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        by_id = {pipeline["id"]: pipeline for pipeline in state["pipelines"]}
        sources = {source["id"]: source for source in state["sources"]}
        check(
            state["schema_version"] == 4 and state["legacy_schema_version"] == 3,
            "five-source control schema advances additively while retaining the version-2 compatibility marker",
        )
        check(len(state["pipelines"]) == 6, "all six pipeline adapters are present")
        check(
            list(sources) == ["dingtalk_chat", "dingtalk_minutes", "dingtalk_mail", "win11", "wechat"],
            "the primary dashboard always returns exactly the five requested sources in order",
        )
        check(
            sources["dingtalk_chat"]["name"] == "钉钉聊天"
            and sources["dingtalk_minutes"]["name"] == "钉钉 AI 听记",
            "DingTalk chat and AI Minutes are separate source cards",
        )
        check(
            all(
                {"manual_state", "automatic_state", "effective_status", "evidence_freshness", "automation_freshness"}.issubset(source)
                for source in sources.values()
            ),
            "every source exposes independent manual automatic effective and freshness states",
        )
        check(
            all(sources[source_id]["scheduler_owner"] == "unified_supervisor" for source_id in (
                "dingtalk_chat", "dingtalk_minutes", "dingtalk_mail", "win11"
            )) and sources["wechat"]["scheduler_owner"] == "legacy",
            "scheduler ownership distinguishes unified supervision from legacy automation",
        )
        write_json(paths.folder_watcher_health, {
            "status": "running",
            "updated_at": "2020-01-02T13:41:50+08:00",
            "queued_events": 0,
        })
        watcher_recovery_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(
            watcher_recovery_state["memory_core"]["last_success"] == "2020-01-02T13:41:10+08:00",
            "a newer watcher heartbeat cannot replace full validation evidence",
        )
        write_json(paths.folder_watcher_health, {
            "status": "running",
            "updated_at": "2020-01-02T13:41:00+08:00",
            "queued_events": 0,
        })
        mixed_failure = primary_overall(
            [
                {"name": "钉钉聊天", "display_status": {"code": "persistent_failure"}},
                {"name": "钉钉邮件", "display_status": {"code": "failed"}},
            ],
            {"display_status": {"code": "healthy"}},
            False,
            {"display_status": {"code": "healthy"}},
        )
        check(
            mixed_failure["code"] == "failed"
            and mixed_failure["detail"] == "钉钉聊天、钉钉邮件",
            "the overall failure detail lists every technical source instead of only the highest-priority class",
        )
        dingtalk_timer_labels = {
            "com.example.business-dingtalk-chat-sync",
            "com.example.business-dingtalk-memory-hourly",
            "com.example.dingtalk-minutes-sync",
            "com.example.dingtalk-minutes-memory-source",
            "com.example.dingtalk-minutes-memory-semantic",
        }
        disabled_timer_dashboard = SystemDashboard(
            paths,
            runner=FakeRunner(
                dingtalk_timer_labels | {
                    "com.example.business-enterprise-email-sync",
                    "com.example.business-wechat-chat-sync",
                },
                exit_codes={"com.example.business-dingtalk-chat-sync": 5},
            ),
            cache_seconds=0,
        )
        disabled_timer_state = disabled_timer_dashboard.collect(
            config, snapshot, force=True, now=fixed_now
        )
        disabled_timer_pipelines = {
            pipeline["id"]: pipeline for pipeline in disabled_timer_state["pipelines"]
        }
        disabled_timer_sources = {
            source["id"]: source for source in disabled_timer_state["sources"]
        }
        check(
            disabled_timer_pipelines["dingtalk"]["legacy_scheduler"]["code"] == "disabled"
            and disabled_timer_pipelines["dingtalk"]["legacy_next_run"] is None
            and disabled_timer_sources["dingtalk_chat"]["legacy_scheduler"]["code"] == "disabled"
            and disabled_timer_sources["dingtalk_minutes"]["legacy_scheduler"]["code"] == "disabled",
            "continuous listeners do not make disabled old independent timers appear enabled",
        )
        disabled_chat_timer = next(
            component
            for component in disabled_timer_sources["dingtalk_chat"]["components"]
            if component["id"] == "chat"
        )
        check(
            disabled_chat_timer["next_run"] is None
            and disabled_chat_timer["health"]["code"] == "healthy"
            and disabled_timer_sources["dingtalk_chat"]["effective_status"]["code"] != "failed",
            "a disabled old timer has no executable next run and its historical exit code is not a current fault",
        )

        supervisor_state.update_source(
            "dingtalk_chat", paths.supervisor_database,
            status="persistent_failure", attempt_count=3,
            last_failure="2020-01-02T13:41:30+08:00",
            error_code="fixture_chat_incomplete",
        )
        supervisor_state.open_alert(
            "dingtalk_chat", "sync_failure", "钉钉聊天持续失败", "尚无同源成功证据",
            severity="error", attempts=3, database=paths.supervisor_database, at=fixed_now,
        )
        persistent_chat_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        persistent_chat = next(
            source for source in persistent_chat_state["sources"] if source["id"] == "dingtalk_chat"
        )
        check(
            persistent_chat["effective_status"]["code"] == "persistent_failure"
            and persistent_chat["recovered"] is False,
            "the compatibility recovered field is derived from the final source status",
        )
        supervisor_state.update_source(
            "dingtalk_chat", paths.supervisor_database,
            status="healthy", attempt_count=0,
            last_success="2020-01-02T13:41:40+08:00", error_code=None,
        )
        contradictory_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        contradictory_chat = next(
            source for source in contradictory_state["sources"] if source["id"] == "dingtalk_chat"
        )
        check(
            contradictory_chat["effective_status"]["code"] == "persistent_failure"
            and contradictory_state["overall"]["code"] == "failed",
            "a still-open severe alert overrides a green source row until that same source resolves it",
        )
        supervisor_state.resolve_alert(
            "dingtalk_chat", "sync_failure", title="钉钉聊天已恢复",
            detail="同一来源获得新成功证据。", database=paths.supervisor_database, at=fixed_now,
        )
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="manual_required", attempt_count=14,
            last_failure="2020-01-02T13:41:30+08:00",
            error_code="read_safety_unconfirmed",
        )
        supervisor_state.open_alert(
            "dingtalk_mail", "sync_failure", "钉钉邮件需要人工安全确认",
            "跨设备未读安全凭证已失效。", severity="error", attempts=14,
            database=paths.supervisor_database, at=fixed_now,
        )
        supervisor_state.open_alert(
            "dingtalk_mail", "manual_attention", "钉钉邮件需要人工处理",
            "附件问题仍安全保留。", severity="warning",
            database=paths.supervisor_database, at=fixed_now + timedelta(seconds=1),
        )
        prioritized_alert_state = dashboard.collect(
            config, snapshot, force=True, now=fixed_now + timedelta(seconds=2)
        )
        prioritized_mail = next(
            source for source in prioritized_alert_state["sources"] if source["id"] == "dingtalk_mail"
        )
        check(
            prioritized_mail["effective_status"]["code"] == "persistent_failure"
            and prioritized_mail["effective_status"]["label"] == "需要人工安全确认"
            and prioritized_mail["alert"]["title"] == "钉钉邮件需要人工安全确认"
            and prioritized_mail["alert"]["action_required"] == "email_read_safety_confirmation",
            "an older severe safety gate outranks a newer warning for the same source",
        )
        supervisor_state.resolve_alert(
            "dingtalk_mail", "sync_failure", title="钉钉邮件安全门已恢复",
            detail="同一来源获得新成功证据。", database=paths.supervisor_database, at=fixed_now,
        )
        supervisor_state.resolve_alert(
            "dingtalk_mail", "manual_attention", title="钉钉邮件人工项已关闭",
            detail="人工项已完成。", database=paths.supervisor_database, at=fixed_now,
        )
        supervisor_state.update_source(
            "dingtalk_chat", paths.supervisor_database,
            status="running", attempt_count=0,
            last_failure=None, error_code=None,
        )
        automatic_action_snapshot = {
            **snapshot,
            "active_actions": [{
                "action_type": "dingtalk_chat", "run_id": "fixture-automatic",
                "trigger": "automatic", "status": "running",
                "started_at": "2020-01-02T13:41:30+08:00",
            }],
            "latest_actions": {
                "dingtalk_chat": {
                    "action_type": "dingtalk_chat", "run_id": "fixture-automatic",
                    "trigger": "automatic", "status": "running",
                    "started_at": "2020-01-02T13:41:30+08:00",
                }
            },
            "latest_manual_actions": {},
            "unknown_trigger_action_types": [],
        }
        automatic_action_state = dashboard.collect(
            config, automatic_action_snapshot, force=True, now=fixed_now
        )
        automatic_action_chat = next(
            source for source in automatic_action_state["sources"] if source["id"] == "dingtalk_chat"
        )
        check(
            automatic_action_chat["manual_state"]["code"] == "idle"
            and automatic_action_chat["automatic_state"]["code"] == "syncing",
            "an automatic controller run never masquerades as a manual task",
        )
        unknown_trigger_snapshot = {
            **snapshot,
            "latest_manual_actions": {},
            "unknown_trigger_action_types": ["dingtalk_chat"],
        }
        unknown_trigger_state = dashboard.collect(
            config, unknown_trigger_snapshot, force=True, now=fixed_now
        )
        unknown_trigger_chat = next(
            source for source in unknown_trigger_state["sources"] if source["id"] == "dingtalk_chat"
        )
        check(
            unknown_trigger_chat["manual_state"]["code"] == "unknown",
            "pre-upgrade action history stays unknown instead of being falsely labelled manual",
        )
        supervisor_state.update_source(
            "dingtalk_chat", paths.supervisor_database,
            status="healthy", attempt_count=0,
            last_failure=None, error_code=None,
        )
        email = sqlite3.connect(paths.email_database)
        email.execute("INSERT INTO messages VALUES('archived','applied','pending',NULL)")
        email.commit()
        local_mail = next(row for row in dashboard.collect(config, snapshot, force=True, now=fixed_now)["pipelines"] if row["id"] == "email")
        check(local_mail["attention_breakdown"]["pending"] == 0 and local_mail["reporting_pending"] == 1,
              "a pending historical report is separate from completed local mail processing")
        email.execute("DELETE FROM messages WHERE archive_state='archived' AND memory_state='applied' AND report_state='pending'")
        email.execute(
            "INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)",
            (
                "local_only_partial", "2020-01-02T13:40:00+08:00", None, None,
                1, 0, 0, 1,
            ),
        )
        email.execute(
            "INSERT INTO messages VALUES('failed','pending','pending','body_length_mismatch')"
        )
        email.commit()
        email.close()
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="healthy", last_success="2020-01-02T13:41:00+08:00",
        )
        unresolved_mail_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        unresolved_mail = next(
            source for source in unresolved_mail_state["sources"] if source["id"] == "dingtalk_mail"
        )
        check(
            unresolved_mail["attention_breakdown"]["technical_failed"] == 1
            and unresolved_mail["effective_status"]["code"] == "failed"
            and unresolved_mail["recovered"] is False,
            "a newer successful probe cannot hide unresolved technical email failures",
        )
        email = sqlite3.connect(paths.email_database)
        email.execute("DELETE FROM batches WHERE created_at='2020-01-02T13:40:00+08:00'")
        email.execute("DELETE FROM messages WHERE last_error_code='body_length_mismatch'")
        email.commit()
        email.close()
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="healthy", last_success="2020-01-02T13:40:00+08:00",
        )

        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="failed", heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason="fixture_runtime_failed",
        )
        runtime_failed_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(
            runtime_failed_state["automation"]["runtime_status"] == "failed"
            and runtime_failed_state["automation"]["display_status"]["code"] == "failed"
            and runtime_failed_state["overall"]["code"] == "failed",
            "a failed supervisor runtime cannot appear green even with a fresh heartbeat",
        )
        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="idle", heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason=None,
        )
        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="power_unknown", power_source="unknown", on_ac=False,
            power_error_code="power_probe_unknown", power_probe_failures=3,
            heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason="power_probe_unknown",
        )
        power_unknown_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(
            power_unknown_state["power"]["known"] is False
            and power_unknown_state["power"]["probe_failures"] == 3
            and power_unknown_state["automation"]["display_status"]["code"] == "power_unknown"
            and power_unknown_state["overall"]["code"] == "power_unknown",
            "unknown power is visible and is never presented as battery or waiting for AC",
        )
        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="idle", power_source="ac", on_ac=True,
            power_error_code=None, power_probe_failures=0,
            blocked_reason=None,
        )
        nonzero_dashboard = SystemDashboard(
            paths,
            runner=FakeRunner(
                {"com.example.business-enterprise-email-sync", "com.example.business-wechat-chat-sync"},
                exit_codes={"com.example.business-sync-supervisor": 2},
            ),
            cache_seconds=0,
        )
        nonzero_state = nonzero_dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(
            nonzero_state["automation"]["last_exit_code"] == 2
            and nonzero_state["automation"]["display_status"]["code"] == "failed"
            and nonzero_state["overall"]["code"] == "failed",
            "a nonzero supervisor exit code cannot be hidden by a fresh heartbeat",
        )
        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="idle", heartbeat_at="2020-01-02T13:35:00+08:00",
            blocked_reason=None,
        )
        stale_supervisor_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(
            stale_supervisor_state["automation"]["display_status"]["code"] == "failed"
            and stale_supervisor_state["overall"]["code"] == "failed",
            "an expired supervisor heartbeat is a visible technical failure rather than an amber source warning",
        )
        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="idle", heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason=None,
        )
        write_json(paths.dingtalk_chat_incremental_status, {
            "status": "partial",
            "started_at": "2020-01-02T13:32:00+08:00",
            "completed_at": "2020-01-02T13:33:00+08:00",
            "conversations_incomplete": 1,
        })
        partial_chat_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        partial_chat = next(
            source for source in partial_chat_state["sources"] if source["id"] == "dingtalk_chat"
        )
        check(
            partial_chat["health"]["code"] == "partial"
            and partial_chat["display_status"]["code"] == "partial"
            and partial_chat["acceptance"]["detail"] == "1 个会话待补齐",
            "an incomplete but normally exited chat cycle is partial rather than failed",
        )
        check(
            partial_chat_state["attention_count"] == 1
            and partial_chat_state["attention_by_source"]["dingtalk_chat"] == 1
            and partial_chat_state["attention_by_source"]["wechat"] == 1,
            "the hero counts one abnormal source while per-source object counts keep paused WeChat separate",
        )
        write_json(paths.dingtalk_chat_incremental_status, {
            "status": "success",
            "started_at": "2020-01-02T13:30:00+08:00",
            "completed_at": "2020-01-02T13:31:00+08:00",
        })
        supervisor_state.update_source(
            "win11", paths.supervisor_database,
            status="healthy", last_failure="2020-01-02T13:39:00+08:00",
            last_success="2020-01-02T13:41:00+08:00",
        )
        manual_failure_snapshot = {
            **snapshot,
            "latest_actions": {
                "win11": {
                    "action_type": "win11", "status": "failed",
                    "started_at": "2020-01-02T13:38:00+08:00",
                    "completed_at": "2020-01-02T13:39:00+08:00",
                    "error_code": "fixture_manual_failure",
                }
            },
        }
        recovered_source_state = dashboard.collect(
            config, manual_failure_snapshot, force=True, now=fixed_now
        )
        recovered_win11 = next(
            source for source in recovered_source_state["sources"] if source["id"] == "win11"
        )
        check(
            recovered_win11["manual_state"]["code"] == "failed"
            and recovered_win11["automatic_state"]["code"] == "healthy"
            and recovered_win11["effective_status"]["code"] == "recovered"
            and recovered_win11["last_failure"] == "2020-01-02T13:39:00+08:00",
            "a later automatic success recovers a manual failure without deleting failure history",
        )
        supervisor_state.update_source(
            "win11", paths.supervisor_database,
            status="healthy", last_failure=None,
            last_success="2020-01-02T13:40:00+08:00",
        )

        write_json(paths.dingtalk_chat_status, {
            "status": "success", "started_at": "2020-01-02T09:00:00+08:00",
            "completed_at": "2020-01-02T09:01:00+08:00",
        })
        write_json(paths.dingtalk_chat_incremental_status, {
            "status": "success", "started_at": "2020-01-02T10:00:00+08:00",
            "completed_at": "2020-01-02T10:01:00+08:00",
        })
        stale_evidence_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        stale_chat = next(
            source for source in stale_evidence_state["sources"] if source["id"] == "dingtalk_chat"
        )
        check(
            stale_chat["automation_freshness"]["code"] == "fresh"
            and stale_chat["evidence_freshness"]["code"] == "stale"
            and stale_chat["effective_status"]["code"] == "failed",
            "fresh supervisor heartbeat cannot hide stale source evidence without a valid execution blocker",
        )
        supervisor_state.update_source(
            "dingtalk_chat", paths.supervisor_database,
            status="waiting_idle", next_due="2020-01-02T13:40:00+08:00",
        )
        stale_waiting_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        stale_waiting_chat = next(
            source for source in stale_waiting_state["sources"] if source["id"] == "dingtalk_chat"
        )
        check(
            stale_waiting_chat["effective_status"]["code"] == "waiting_idle"
            and "证据也已过期" in stale_waiting_chat["effective_status"]["detail"],
            "stale source evidence stays blue-gray only when a current valid waiting condition is present",
        )
        supervisor_state.update_source(
            "dingtalk_chat", paths.supervisor_database,
            status="healthy", next_due="2020-01-02T13:45:00+08:00",
        )
        write_json(paths.dingtalk_chat_status, {
            "status": "success", "started_at": "2020-01-02T13:00:00+08:00",
            "completed_at": "2020-01-02T13:01:00+08:00",
        })
        write_json(paths.dingtalk_chat_incremental_status, {
            "status": "success", "started_at": "2020-01-02T13:30:00+08:00",
            "completed_at": "2020-01-02T13:31:00+08:00",
        })

        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="waiting_idle", heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason="user_active", next_eligible_at=None,
        )
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="waiting_idle", next_due="2020-01-02T13:40:00+08:00",
        )
        waiting_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        waiting_mail = next(
            source for source in waiting_state["sources"] if source["id"] == "dingtalk_mail"
        )
        check(
            waiting_mail["effective_status"]["code"] == "waiting_idle"
            and waiting_mail["next_run"] is None
            and waiting_mail["eligibility"]["planned_at"] == "2020-01-02T13:40:00+08:00"
            and waiting_mail["next_action"] == "已到期，空闲后立即执行",
            "an overdue source waiting for idle separates planned time from execution eligibility",
        )
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="manual_required", next_due="2020-01-02T13:40:00+08:00",
            attention_count=2,
        )
        manual_wait_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        manual_wait_mail = next(
            source for source in manual_wait_state["sources"] if source["id"] == "dingtalk_mail"
        )
        check(
            manual_wait_mail["effective_status"]["code"] == "manual_required"
            and manual_wait_mail["next_run"] is None
            and manual_wait_mail["next_action"] == "等待人工处理；后台继续探测",
            "a global idle wait cannot overwrite a source that genuinely requires manual handling",
        )
        check(
            manual_wait_state["attention_count"] == sum(
                1 for source in manual_wait_state["sources"]
                if source["display_status"]["code"] in ABNORMAL_SOURCE_STATUS_CODES
            ),
            "the abnormal-source count is derived only from technical abnormal states",
        )
        check(
            manual_wait_mail["effective_status"]["code"] not in ABNORMAL_SOURCE_STATUS_CODES,
            "a manual-required source is not classified as a technical abnormality",
        )
        check(
            manual_wait_state["attention_by_source"]["dingtalk_mail"] >= 2,
            "manual items stay in their source object count without inflating the technical abnormal-source count",
        )
        supervisor_state.update_source("dingtalk_mail", paths.supervisor_database,
            error_code="attachment_review_required", attention_count=37)
        fresh_mail_counts = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(next(row for row in fresh_mail_counts["sources"] if row["id"] == "dingtalk_mail")["attention"] == 0
              and fresh_mail_counts["attention_by_source"]["dingtalk_mail"] == 1,
              "known current attachment counts cannot inherit a stale historical report count")
        supervisor_state.update_source("dingtalk_mail", paths.supervisor_database, error_code=None)
        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="idle", heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason="core_lock_busy", next_eligible_at=None,
        )
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="deferred", next_due="2020-01-02T13:40:00+08:00",
            attention_count=0,
        )
        deferred_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        deferred_mail = next(
            source for source in deferred_state["sources"] if source["id"] == "dingtalk_mail"
        )
        check(
            deferred_mail["effective_status"]["code"] == "deferred"
            and deferred_mail["next_run"] is None
            and deferred_mail["next_action"] == "条件满足后立即执行",
            "a safety deferral is a condition-waiting state rather than a technical failure",
        )
        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="idle", heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason=None, next_eligible_at="2020-01-02T13:45:00+08:00",
        )
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="healthy", next_due="2020-01-02T13:45:00+08:00",
        )
        check(
            by_id["dingtalk"]["health"]["code"] == "recovered"
            and by_id["dingtalk"]["last_failure"] == "2020-01-02T12:01:00+08:00",
            "an older DingTalk incident is retained but a later success shows recovered instead of red",
        )
        check(
            by_id["email"]["lifecycle"]["label"] == "统一监督已接管"
            and by_id["email"]["legacy_scheduler"]["code"] == "disabled"
            and by_id["email"]["health"]["code"] == "healthy",
            "email unified scheduling stays enabled while the old independent timer is reported separately",
        )
        check(
            sources["dingtalk_mail"]["control"]["available"] is True
            and sources["dingtalk_mail"]["control"]["local_only"] is True,
            "a configured email processor remains manually available in local-only mode while scheduling is disabled",
        )
        check(
            by_id["wechat"]["lifecycle"]["code"] == "paused"
            and by_id["wechat"]["acceptance"]["code"] == "not_enrolled",
            "enrollment_required is shown as a paused not-yet-live WeChat pipeline",
        )
        check(
            sources["wechat"]["display_status"]["code"] == "paused"
            and sources["wechat"]["control"]["available"] is False
            and sources["wechat"]["control"]["reason_code"] == "enrollment_required"
            and sources["wechat"]["next_run"] is None
            and sources["wechat"]["next_action"] == "完成安全登记后启用",
            "WeChat stays visible and paused with no fabricated next run before enrollment",
        )
        check(
            by_id["win11"]["health"]["code"] == "recovered"
            and by_id["win11"]["last_failure"]
            and by_id["win11"]["last_success"],
            "a later Win11 success changes the main state to recovered without deleting failure history",
        )
        check(
            by_id["win11"]["last_attempt"] == "2020-01-02T13:35:00+08:00"
            and by_id["win11"]["last_success"] == "2020-01-02T13:35:00+08:00",
            "a successful no-op schedule check updates Win11 attempt and success without claiming writes",
        )
        win11_events = dashboard.events(config, snapshot, pipeline="win11", limit=50)["events"]
        check(
            any(event["status"] == "failed" for event in win11_events)
            and any(event["status"] == "recovered" for event in win11_events),
            "Win11 events retain both the old failure and the recovery",
        )
        check(
            any(event["kind"] == "schedule" and "无新内容" in event["detail"] for event in win11_events),
            "a no-op Win11 schedule check remains visible as a redacted event",
        )
        check(
            by_id["core"]["health"]["code"] == "recovered"
            and by_id["core"]["acceptance"]["code"] == "accepted",
            "a current full validation receipt recovers an old validation failure",
        )
        paths.validation_success.unlink()
        index_only = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(index_only["memory_core"]["acceptance"]["code"] == "blocked",
              "equal-generation index refresh alone never recovers a validation failure")
        write_json(paths.validation_failure, {
            "verdict": "fail",
            "recorded_at": "2020-01-02T13:41:30+08:00",
        })
        active_failure_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        active_failure_core = next(
            pipeline for pipeline in active_failure_state["pipelines"] if pipeline["id"] == "core"
        )
        check(
            active_failure_core["health"]["code"] == "failed"
            and active_failure_core["acceptance"]["code"] == "blocked",
            "a validation failure newer than the index remains blocking",
        )
        write_json(paths.validation_failure, {
            "verdict": "fail",
            "error_code": "validation_output",
            "errors": ["fixture page: claim-base hash mismatch"],
            "recorded_at": "2020-01-02T13:41:30+08:00",
        })
        claim_base_failure = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(
            claim_base_failure["memory_core"]["validation_error_code"] == "claim_base_mismatch"
            and claim_base_failure["memory_core"]["health"]["label"] == "业务页面校验基线失效",
            "claim-base validation output is retained as its own actionable root cause",
        )
        write_json(paths.validation_failure, {
            "verdict": "fail",
            "error_code": "validation_output",
            "errors": ["fixture page: claim_base_untracked_suffix"],
            "recorded_at": "2020-01-02T13:41:31+08:00",
        })
        suffix_failure = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(
            suffix_failure["memory_core"]["validation_error_code"] == "claim_base_untracked_suffix"
            and suffix_failure["memory_core"]["health"]["label"] == "业务页面校验基线失效",
            "post-base suffix failures retain their exact claim-base root cause",
        )
        write_json(paths.validation_success, {
            "verdict": "pass",
            "recorded_at": "2020-01-02T13:41:45+08:00",
        })
        weak_receipt = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(weak_receipt["memory_core"]["acceptance"]["code"] == "blocked",
              "legacy success without data and program bindings cannot clear failure")
        write_validation_success(paths, "2020-01-02T13:41:45+08:00")
        validation_recovered_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        validation_recovered_core = next(
            pipeline for pipeline in validation_recovered_state["pipelines"] if pipeline["id"] == "core"
        )
        check(
            validation_recovered_core["health"]["code"] == "recovered"
            and validation_recovered_core["acceptance"]["code"] == "accepted",
            "a later validation success recovers the core while retaining failure history",
        )
        freshly_rechecked_state = dashboard.collect(
            config,
            snapshot,
            force=True,
            now=datetime.fromisoformat("2020-01-02T13:48:00+08:00"),
        )
        check(
            freshly_rechecked_state["memory_core"]["display_status"]["code"] == "recovered",
            "a live current-receipt recheck keeps the core fresh when no business write was needed",
        )
        historical_failure = paths.validation_failure.read_bytes()
        new_note = paths.root / "vault" / "memory" / "new-input-fixture.txt"
        new_note.write_text("new source content after the earlier full pass", encoding="utf-8")
        new_input_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(new_input_state["memory_core"]["health"]["code"] == "unknown"
              and new_input_state["memory_core"]["acceptance"]["code"] == "pending"
              and new_input_state["memory_core"]["automatic_validation"]["status"] == "pending"
              and new_input_state["global_action"]["available"] is False,
              "new input waits for full validation without resurrecting a historical fault or opening writes")
        check(paths.validation_failure.read_bytes() == historical_failure,
              "ordinary new input leaves historical failure evidence intact")
        with sqlite3.connect(paths.bridge_database) as connection:
            connection.execute("UPDATE maintenance_state SET value='8' WHERE key='index_dirty_generation'")
        pending_index_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(pending_index_state["memory_core"]["acceptance"]["code"] == "pending"
              and pending_index_state["memory_core"]["health"]["code"] == "unknown"
              and pending_index_state["global_action"]["available"] is False,
              "normal index commit lag is pending, not a new corruption incident")
        with sqlite3.connect(paths.bridge_database) as connection:
            connection.execute("UPDATE maintenance_state SET value='7' WHERE key='index_dirty_generation'")
        write_validation_success(paths, "2020-01-02T13:41:46+08:00")
        new_input_pass = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(new_input_pass["memory_core"]["acceptance"]["code"] == "accepted",
              "a current full pass accepts the new input after the pending interval")
        automatic_path = paths.state_root / "automatic-memory-validation.json"
        write_json(automatic_path, {"schema_version": 1, "status": "running", "attempt_count": 1,
                                   "input_key": "a" * 64, "validation_context": {},
                                   "run_id": '64903b1e-a936-5c85-8607-ed41464746c0',
                                   "job_id": '1db2ae02-b5b7-588f-ac35-696bbe806480'})
        with patch('system_dashboard_status.active_lock', side_effect=lambda _locks, pipeline, _statuses: pipeline == 'core'):
            running_validation = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(running_validation["memory_core"]["acceptance"]["code"] == "pending"
              and running_validation["memory_core"]["activity"]["code"] == "syncing"
              and running_validation["global_action"]["available"] is False,
              "publishing a receipt does not open writes while its owning validation is still finishing")
        automatic_path.write_text("invalid accounting fixture", encoding="utf-8")
        damaged_accounting = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        check(damaged_accounting["memory_core"]["acceptance"]["code"] == "blocked"
              and damaged_accounting["memory_core"]["validation_error_code"] == "automatic_validation_state_invalid",
              "corrupt automatic validation accounting remains visible and fail closed")
        automatic_path.unlink()
        with patch('validation_evidence.context', side_effect=PermissionError):
            denied_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        denied_core = denied_state['memory_core']
        check(denied_core['validation_error_code'] == 'validation_permission_denied'
              and denied_core['health']['code'] == 'failed'
              and denied_core['acceptance']['code'] == 'blocked',
              'background permission denial is explicit and never an index refresh or recovery')
        check(
            state["memory_core"]["acceptance"]["code"] == "accepted"
            and state["global_action"]["available"] is True
            and state["global_action"]["local_only"] is True,
            "core acceptance and the local-only global action are exposed separately",
        )

        free_path = paths.state_root / "free.lock"
        free_path.write_text("", encoding="utf-8")
        check(probe_lock(free_path, runner)["status"] == "idle", "an unheld lock file is idle")
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        stop = context.Event()
        held_path = paths.state_root / "held.lock"
        held_path.write_text("", encoding="utf-8")
        process = context.Process(target=hold_lock, args=(str(held_path), ready, stop))
        process.start()
        try:
            check(ready.wait(2), "lock fixture acquired")
            known = probe_lock(
                held_path,
                lock_runner(process.pid, "/usr/bin/python3 receiver.py"),
                role="service",
                expected_markers=("receiver.py",),
            )
            check(
                known["status"] == "service_owned" and known["pid"] == process.pid,
                "a known service holding its dedicated lock is normal ownership",
            )
            known_writer = probe_lock(
                held_path,
                lock_runner(process.pid, "/usr/bin/python3 receiver.py"),
                role="write",
                expected_markers=("receiver.py", "business-memory-receiver"),
            )
            check(
                known_writer["status"] == "writing"
                and known_writer["pid"] == process.pid
                and known_writer["process"] == "receiver",
                "a known receiver holding the core lock is shown as a redacted active writer",
            )
            unknown = probe_lock(
                held_path,
                lock_runner(process.pid, "/usr/bin/sleep 5"),
                role="service",
                expected_markers=("receiver.py",),
            )
            check(
                unknown["status"] == "unknown" and unknown["pid"] == process.pid,
                "an unknown process holding a dedicated lock is not inferred healthy",
            )
        finally:
            stop.set()
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(1)

        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="maintenance", heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason="maintenance_active", next_eligible_at=None,
        )
        supervisor_state.open_alert(
            "memory_core", "core_failure", "记忆库核心需要处理", "等待维护后复验",
            severity="error", database=paths.supervisor_database, at=fixed_now,
        )
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="healthy", next_due="2020-01-02T13:40:00+08:00",
        )
        write_json(paths.maintenance_state, {"status": "active", "reason": "fixture"})
        maintenance_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        core = next(pipeline for pipeline in maintenance_state["pipelines"] if pipeline["id"] == "core")
        check(
            core["health"]["code"] == "maintenance"
            and core["display_status"]["code"] == "maintenance_pending",
            "expected process stops during maintenance are not reported as faults",
        )
        check(
            maintenance_state["automation"]["display_status"]["code"] == "maintenance",
            "an intentionally stopped supervisor is neutral maintenance rather than a false failure",
        )
        check(
            maintenance_state["memory_core"]["display_status"]["code"] == "maintenance_pending"
            and maintenance_state["alert"]["maintenance_pending_count"] == 1
            and maintenance_state["alert"]["open_count"] == 0,
            "an open core incident becomes maintenance-pending without being deleted or counted as current damage",
        )
        check(
            maintenance_state["global_action"]["available"] is False
            and all(source["control"]["available"] is False for source in maintenance_state["sources"]),
            "maintenance disables all five source controls without changing scheduler settings",
        )
        maintenance_mail = next(
            source for source in maintenance_state["sources"] if source["id"] == "dingtalk_mail"
        )
        check(
            maintenance_mail["next_run"] is None
            and maintenance_mail["eligibility"]["planned_at"] == "2020-01-02T13:40:00+08:00"
            and maintenance_mail["next_action"] == "维护结束后立即执行",
            "an overdue source blocked by maintenance keeps its plan but exposes immediate post-maintenance eligibility",
        )
        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="failed", heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason="fixture_failure_during_maintenance",
        )
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="failed", error_code="fixture_source_failure",
            last_failure="2020-01-02T13:41:30+08:00",
        )
        maintenance_failure_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        maintenance_failure_mail = next(
            source for source in maintenance_failure_state["sources"] if source["id"] == "dingtalk_mail"
        )
        check(
            maintenance_failure_state["maintenance"]["active"] is True,
            "maintenance remains visible while a technical failure is present",
        )
        check(
            maintenance_failure_state["automation"]["display_status"]["code"] == "failed",
            "maintenance cannot mask a supervisor runtime failure",
        )
        check(
            maintenance_failure_state["overall"]["code"] == "failed",
            "maintenance cannot mask the overall technical failure",
        )
        check(
            maintenance_failure_mail["effective_status"]["code"] == "failed",
            "maintenance cannot mask a source technical failure",
        )
        write_json(paths.maintenance_state, {"status": "inactive"})
        supervisor_state.update_runtime(
            paths.supervisor_database,
            last_maintenance_exit_at=supervisor_state.now_iso(fixed_now),
        )
        pending_core_state = dashboard.collect(
            config, snapshot, force=True, now=fixed_now + timedelta(seconds=30)
        )
        pending_core = next(
            pipeline for pipeline in pending_core_state["pipelines"] if pipeline["id"] == "core"
        )
        check(
            pending_core["display_status"]["code"] == "maintenance_pending"
            and pending_core_state["memory_core"]["display_status"]["code"] == "maintenance_pending",
            "post-maintenance core evidence stays pending during the bounded recovery window",
        )
        unresolved_core_state = dashboard.collect(
            config, snapshot, force=True, now=fixed_now + timedelta(minutes=3)
        )
        unresolved_core = next(
            pipeline for pipeline in unresolved_core_state["pipelines"] if pipeline["id"] == "core"
        )
        check(
            unresolved_core["display_status"]["code"] == "failed"
            and unresolved_core_state["memory_core"]["display_status"]["code"] == "failed",
            "an unresolved core incident uses one effective display status across the system API",
        )
        supervisor_state.resolve_alert(
            "memory_core", "core_failure", title="记忆库核心已恢复",
            detail="同一核心来源已获得新成功证据。", database=paths.supervisor_database,
            at=fixed_now,
        )
        supervisor_state.update_runtime(
            paths.supervisor_database,
            status="idle", heartbeat_at="2020-01-02T13:41:30+08:00",
            blocked_reason=None, next_eligible_at="2020-01-02T13:45:00+08:00",
        )
        supervisor_state.update_source(
            "dingtalk_mail", paths.supervisor_database,
            status="healthy", next_due="2020-01-02T13:45:00+08:00",
            error_code=None, last_failure=None,
        )

        running_snapshot = {
            **snapshot,
            "active_actions": [{
                "run_id": 'a2b81d52-6623-5666-90a1-f6551d14424d',
                "action_type": "dingtalk_chat",
                "status": "running",
                "started_at": "2020-01-02T13:40:00+08:00",
            }],
        }
        unrelated_state = dashboard.collect(config, {**snapshot, "active_actions": [{
            "run_id": "fixture-repair", "action_type": "local_repair", "status": "running"}]}, force=True, now=fixed_now)
        check(all(row["activity"]["code"] != "syncing" for row in unrelated_state["sources"]
                  if row["id"] in {"dingtalk_chat", "dingtalk_minutes"}),
              "an unrelated local repair does not masquerade as an active DingTalk sync")
        running_state = dashboard.collect(config, running_snapshot, force=True, now=fixed_now)
        check(
            running_state["global_action"]["running"] is True
            and running_state["global_action"]["available"] is False
            and next(source for source in running_state["sources"] if source["id"] == "dingtalk_chat")["activity"]["code"] == "syncing",
            "one active source owns the shared control and appears as syncing",
        )

        review_snapshot = {
            **snapshot,
            "active_actions": [{
                "run_id": '6978bb23-da8c-5398-b8b7-eefac31a92df',
                "action_type": "locked_review",
                "status": "running",
                "started_at": "2020-01-02T13:41:00+08:00",
            }],
        }
        review_state = dashboard.collect(config, review_snapshot, force=True, now=fixed_now)
        check(
            review_state["global_action"]["available"] is False
            and review_state["global_action"]["reason_code"] == "already_running"
            and all(source["control"]["available"] is False for source in review_state["sources"]),
            "an existing automatic review disables every manual control instead of creating a hidden queue",
        )

        paths.wechat_status.write_text("{broken", encoding="utf-8")
        paths.chatgpt_ledger.unlink()
        degraded = dashboard.collect(config, snapshot, force=True, now=fixed_now)
        degraded_by_id = {pipeline["id"]: pipeline for pipeline in degraded["pipelines"]}
        check(degraded_by_id["wechat"]["health"]["code"] == "unknown", "corrupt JSON degrades to unknown")
        check(degraded_by_id["chatgpt"]["health"]["code"] == "unknown", "missing ledger degrades to unknown")
        check(degraded_by_id["core"]["health"]["code"] != "unknown", "one broken source does not hide other pipelines")

        busy = sqlite3.connect(paths.email_database, timeout=0.1)
        busy.execute("BEGIN EXCLUSIVE")
        try:
            busy_state = dashboard.collect(config, snapshot, force=True, now=fixed_now)
            busy_email = next(pipeline for pipeline in busy_state["pipelines"] if pipeline["id"] == "email")
            check(busy_email["health"]["code"] == "unknown", "busy SQLite degrades to unknown")
        finally:
            busy.rollback()
            busy.close()

        timed_out = SystemDashboard(paths, runner=FakeRunner(set(), timeout_launchd=True), cache_seconds=0)
        timeout_state = timed_out.collect(config, snapshot, force=True, now=fixed_now)
        timeout_email = next(pipeline for pipeline in timeout_state["pipelines"] if pipeline["id"] == "email")
        check(
            timeout_email["lifecycle"]["label"] == "统一监督已接管"
            and timeout_email["legacy_scheduler"]["code"] == "unknown",
            "launchd timeout affects only the old independent timer rather than disabling unified supervision",
        )
        folded = fold_events([
            {"id": "a", "pipeline": "win11", "kind": "scan", "status": "success", "title": "检查完成", "detail": "无变化", "time": "2020-01-02T13:41:00+08:00"},
            {"id": "b", "pipeline": "win11", "kind": "scan", "status": "success", "title": "检查完成", "detail": "无变化", "time": "2020-01-02T13:40:00+08:00"},
            {"id": "c", "pipeline": "win11", "kind": "scan", "status": "failed", "title": "检查失败", "detail": "fixture", "time": "2020-01-02T13:39:00+08:00"},
        ])
        check(
            len(folded) == 2
            and folded[0]["repeat_count"] == 2
            and folded[0]["last_time"] == "2020-01-02T13:41:00+08:00"
            and folded[0]["first_time"] == "2020-01-02T13:40:00+08:00"
            and folded[1]["status"] == "failed",
            "only consecutive semantically identical no-change events fold while failures remain distinct",
        )
        source = Path(__file__).with_name("system_dashboard_status.py").read_text(encoding="utf-8")
        check("immutable=1" not in source and "PRAGMA query_only=ON" in source, "SQLite readers use mode=ro plus query_only without immutable")
        check(
            state["privacy"]["control_actions_added"] is True
            and state["privacy"]["local_only"] is True
            and state["privacy"]["wechat_raw_database_read"] is False
            and state["privacy"]["keychain_read"] is False,
            "new controls are explicitly local-only and preserve WeChat privacy boundaries",
        )

    print(json.dumps({"verdict": "pass", "checks": CHECKS}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
