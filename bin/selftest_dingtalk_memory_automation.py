#!/usr/bin/env python3
"""Offline acceptance tests for DingTalk business-memory automation."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import os
import queue
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dingtalk_memory_automation as automation
import model_router
import sync_dingtalk_chat as chat_sync


MOCK_DWS = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]

if args[:2] == ["event", "+listen-im"]:
    kind = args[args.index("--kind") + 1]
    print("[event] ready event_key=im.message bus_pid=123 subscribe_id=mock-" + kind, file=sys.stderr, flush=True)
    print(json.dumps({
        "type": "message",
        "event_id": "listener-event-" + kind,
        "timestamp": "2020-01-02T10:00:00+08:00",
        "message_id": "listener-message-" + kind,
        "conversation_id": "listener-conversation-" + kind,
        "sender": "Listener",
        "sender_open_dingtalk_id": "listener-user",
        "content": "监听测试业务消息",
        "create_time": "2020-01-02 10:00:00"
    }, ensure_ascii=False), flush=True)
    sys.stdin.read()
    raise SystemExit(0)

if args[:2] == ["event", "status"]:
    print(json.dumps({"identity": {"user_id": "self-user"}}, ensure_ascii=False))
    raise SystemExit(0)

if args[:2] == ["chat", "+messages-mget"]:
    if os.environ.get("MOCK_DWS_FAIL") == "1":
        print(json.dumps({
            "complete": False,
            "hasMore": True,
            "failedCount": 1,
            "messages": []
        }))
        print(json.dumps({"error": {"reason": "mock_auth_failed", "message": "mock failure"}}), file=sys.stderr)
        raise SystemExit(1)
    message_ids = args[args.index("--msg-ids") + 1].split(",")
    messages = []
    for message_id in message_ids:
        if message_id == "msg-sensitive":
            messages.append({
                "messageId": message_id,
                "conversationId": "conv-direct",
                "createTime": "2020-01-02 08:01:00",
                "senderId": "user-a",
                "sender": "Alice",
                "text": "demo R4nd0m!Pass",
                "resourceRefs": []
            })
        elif message_id == "msg-group":
            messages.append({
                "messageId": message_id,
                "conversationId": "conv-group",
                "createTime": "2020-01-02 09:00:00",
                "senderId": "user-b",
                "sender": "Bob",
                "text": "项目计划初版",
                "resourceRefs": [{"fileId": "ref-only", "downloadUrl": "https://secret.invalid/file"}]
            })
    print(json.dumps({
        "complete": True,
        "hasMore": False,
        "failedCount": 0,
        "messages": messages
    }, ensure_ascii=False))
    raise SystemExit(0)

print(json.dumps({"error": {"reason": "unexpected_mock_command"}}), file=sys.stderr)
raise SystemExit(1)
'''


MOCK_CODEX = '#!/usr/bin/env python3\nimport json\nimport os\nimport re\nimport sys\nfrom pathlib import Path\n\nargs = sys.argv[1:]\nreceipt = os.environ.get("MOCK_CODEX_RECEIPT")\nif receipt:\n    Path(receipt).write_text(json.dumps({\n        "args": args,\n        "api_key_present": bool(os.environ.get("OPENAI_API_KEY")),\n        "base_url_present": bool(os.environ.get("OPENAI_BASE_URL"))\n    }), encoding="utf-8")\nif os.environ.get("MOCK_CODEX_FAIL") == "1":\n    print("usage limit reached", file=sys.stderr)\n    raise SystemExit(7)\nif "--json" in args:\n    print(json.dumps({"thread_id": "be7671b0-36f1-5bd3-954d-d5a090adc517", "type": "thread.started"}))\n    print(json.dumps({"type": "item.completed", "message": "recorded"}))\n    raise SystemExit(0)\nprompt = sys.stdin.read()\nmatch = re.search(r"<batch_data>\\n(.*?)\\n</batch_data>", prompt, re.S)\nif not match:\n    print("missing batch", file=sys.stderr)\n    raise SystemExit(8)\nbatch = json.loads(match.group(1))\nprint(json.dumps({\n    "batch_id": batch["batch_id"],\n    "source_summary": None,\n    "candidates": [],\n    "no_business_change": True\n}, ensure_ascii=False))\n'


def make_note(
    memory_id: str,
    title: str,
    *,
    status: str = "active",
    fact_status: str = "confirmed",
) -> str:
    body = "## 记忆内容\n\n现有已确认事实。\n\n## 证据边界\n\n由测试权威资料确认。\n"
    frontmatter = [
        ("memory_id", memory_id),
        ("category", "business_cognition"),
        ("memory_scope", "internal_nonpublic"),
        ("memory_kind", "fact"),
        ("origin_type", "internal_file"),
        ("status", status),
        ("evidence_level", "internal_source" if fact_status == "confirmed" else "unverified"),
        ("sensitivity", "internal"),
        ("source_system", "obsidian"),
        ("source_check", "matched" if fact_status == "confirmed" else "unverified"),
        ("fact_status", fact_status),
        ("source_ref", "test-authority"),
        ("observed_at", "2020-01-02T08:00:00+08:00"),
        ("reviewed_at", "2020-01-02T08:00:00+08:00" if fact_status == "confirmed" else None),
        ("reviewed_by", "test-owner" if fact_status == "confirmed" else None),
        ("review_mode", "human_review" if fact_status == "confirmed" else None),
        ("valid_until", None),
        ("supersedes", []),
        ("content_hash", ""),
        ("applies_to", ["project:business-energy"]),
    ]
    return automation.render_note(frontmatter, body)


def make_source_note(memory_id: str, source_ref: str, observed_at: str) -> str:
    body = "## 对话摘要\n\n测试来源摘要。\n\n## 证据边界\n\n仅用于离线回归。\n"
    frontmatter = [
        ("memory_id", memory_id),
        ("category", "conversation_summary"),
        ("memory_scope", "conversation_source"),
        ("memory_kind", "conversation_summary"),
        ("origin_type", "internal_communication"),
        ("status", "active"),
        ("evidence_level", "internal_source"),
        ("sensitivity", "internal"),
        ("source_system", "dingtalk"),
        ("source_check", "matched"),
        ("fact_status", "confirmed"),
        ("source_ref", source_ref),
        ("observed_at", observed_at),
        ("reviewed_at", None),
        ("valid_until", None),
        ("supersedes", []),
        ("content_hash", ""),
        ("applies_to", ["project:business-energy"]),
    ]
    return automation.render_note(frontmatter, body)


def insert_message(config: dict, message_id: str, conversation_id: str, text: str, create_time: str) -> None:
    connection = automation.connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        observed = automation.now_iso()
        automation.ensure_conversation(connection, conversation_id, observed, "测试业务会话")
        run_id = automation.begin_auxiliary_run(connection, "event", observed)
        normalized = chat_sync.normalize_message(
            {
                "messageId": message_id,
                "conversationId": conversation_id,
                "createTime": create_time,
                "senderId": "self-user",
                "sender": "Current User",
                "text": text,
                "resourceRefs": [],
            },
            conversation_id,
            20000,
        )
        inserted, updated, _kind = chat_sync.upsert_message(connection, normalized, observed, run_id)
        automation.complete_auxiliary_run(connection, run_id, "success", 1, inserted, updated)
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    checks: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)
        checks.append(message)

    with tempfile.TemporaryDirectory(prefix="business-dingtalk-memory-selftest-") as temporary:
        root = Path(temporary)
        external_root = root / "external"
        memory_root = root / "vault" / "00_业务记忆库"
        for directory in (
            "01_关键业务认知",
            "02_内部业务资料",
            "03_外部沟通",
            "04_对话来源摘要",
            "90_待确认",
            "99_规则与模板",
        ):
            (memory_root / directory).mkdir(parents=True, exist_ok=True)
        safe_name = automation.safe_filename("Head[疑似口令已脱敏]#内部^块", 18)
        check(not any(character in safe_name for character in "[]#^"),
              "memory filenames remove Markdown link and anchor syntax before truncation")
        confirmed_path = memory_root / "01_关键业务认知" / "已确认事实.md"
        confirmed_path.write_text(make_note("BUSINESS-20260825-001", "已确认事实"), encoding="utf-8")

        mock_dws = root / "mock_dws.py"
        mock_dws.write_text(MOCK_DWS, encoding="utf-8")
        mock_dws.chmod(0o700)
        mock_codex = root / "mock_codex.py"
        mock_codex.write_text(MOCK_CODEX, encoding="utf-8")
        mock_codex.chmod(0o700)
        codex_receipt = root / "codex_receipt.json"

        config = {
            "schema_version": 2,
            "external_root": str(external_root),
            "memory_root": str(memory_root),
            "dingtalk_chat": {
                "dws_bin": str(mock_dws),
                "timeout_seconds": 5,
                "max_text_chars": 20000,
                "incremental_overlap_minutes": 10,
                "incremental_initial_lookback_hours": 2,
            },
            "dingtalk_automation": {
                "enabled": True,
                "scope": "all_visible_direct_and_group_chats",
                "timezone": "Asia/Shanghai",
                "semantic_minutes": [5, 35],
                "max_batch_messages": 100,
                "max_batch_chars": 300,
                "context_messages": 2,
                "event_ready_timeout_seconds": 5,
                "event_hydration_interval_seconds": 1,
                "event_max_hydration_attempts": 3,
                "model": "deepseek-flash",
                "reasoning_effort": "high",
                "codex_bin": str(mock_codex),
                "codex_timeout_seconds": 10,
                "memory_index_refresh_timeout_seconds": 60,
                "memory_validation_timeout_seconds": 60,
                "local_verification_attempts": 3,
                "local_verification_retry_delay_seconds": 0,
                "subscription_only": True,
                "allow_paid_api_fallback": False,
                "allow_model_fallback": False,
                "semantic_session": "ephemeral",
                "semantic_sandbox": "read-only",
                "mac_notifications": False,
                "codex_alert_record": False,
                "alert_repeat_hours": 6,
                "attachment_policy": "resource_refs_only_no_download",
                "write_policy": "graded_trusted_writer",
                "live_acceptance": "live_acceptance_pending",
            },
        }
        # Route the semantic call to the mock codex binary only: the checks
        # below are about that transport, and nothing may reach live providers.
        model_router.isolate_for_tests(root / "model-router")
        config["model_routing"] = {"enabled_providers": ["gpt"], "text_chain": ["gpt:deepseek-flash"],
                                    "vision_chain": ["gpt:deepseek-flash", "kimi:k3"]}
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        loaded = automation.load_config(config_path)
        check(loaded["dingtalk_automation"]["model"] == "deepseek-flash", "governed config loads")
        claim_path, claim_text, _claim_route = automation.business_router.route_claim_change(
            config,
            {
                "action": "annotate_page",
                "dedupe_key": "allocator-inline-claim",
                "title": "分配器内联标识",
                "summary": "仅用于验证正式记忆不会复用内联标识。",
                "evidence_boundary": "自测数据。",
                "evidence_state": "pending",
                "category": "internal_material",
                "memory_kind": "status",
                "target_page": "船舶业务网站与品牌架构/01_决策与结论/产品命名与商标风险.md",
            },
            source_system="selftest",
            source_ref="selftest:allocator",
            observed_at="2020-01-02T12:00:00+08:00",
        )
        claim_path.parent.mkdir(parents=True, exist_ok=True)
        claim_path.write_text(claim_text, encoding="utf-8")
        check(
            automation.next_memory_id(
                automation.memory_records(memory_root),
                "20260823",
                set(),
                vault_root=memory_root.parent,
            )
            == "BUSINESS-20260823-002",
            "formal memory allocator reserves IDs already used by inline claims",
        )
        refresh_environments = []
        refresh_commands = []

        def fake_refresh_run(*args, **kwargs):
            command = [str(value) for value in (args[0] if args else [])]
            refresh_commands.append(command)
            refresh_environments.append(dict(kwargs.get("env") or {}))
            if str(automation.REFRESH_SCRIPT) in command:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout='{"verdict":"pass","errors":[]}', stderr="")

        with mock.patch.object(automation.subprocess, "run", side_effect=fake_refresh_run):
            automation.run_refresh_and_validate(config)
            check(
                refresh_environments
                and all("BUSINESS_MEMORY_INDEX_LOCK_HELD" not in item for item in refresh_environments),
                "standalone refresh and validation keep the shared index lock enabled",
            )
            refresh_environments.clear()
            automation.run_refresh_and_validate(config, lock_held=True)
            check(
                len(refresh_environments) == 2
                and all(item.get("BUSINESS_MEMORY_INDEX_LOCK_HELD") == "1" for item in refresh_environments),
                "refresh and validation bypass the Ruby lock only while the caller actually holds it",
            )
            refresh_environments.clear()
            refresh_commands.clear()
            validation = automation.run_refresh_and_validate(config, refresh_indexes=False)
            check(
                len(refresh_commands) == 1 and str(automation.VALIDATE_SCRIPT) in refresh_commands[0],
                "read-only validation skips the memory index writer",
            )
            check(
                validation["indexes_refreshed"] is False and validation["refresh_exit"] is None,
                "read-only validation reports that indexes were not refreshed",
            )
        recovered_result = SimpleNamespace(returncode=0, stdout='{"verdict":"pass","errors":[]}', stderr="")
        with mock.patch.object(
            automation.subprocess,
            "run",
            side_effect=[automation.subprocess.TimeoutExpired(["validator"], 60), recovered_result],
        ) as retried_run, mock.patch.object(automation.time, "sleep"):
            retried = automation.run_refresh_and_validate(config, refresh_indexes=False)
        check(retried_run.call_count == 2 and retried["validation_attempts"] == 2,
              "local validation timeout is retried automatically and records the successful attempt")
        timeout_error = None
        with mock.patch.object(
            automation.subprocess,
            "run",
            side_effect=[automation.subprocess.TimeoutExpired(["validator"], 60)] * 3,
        ), mock.patch.object(automation.time, "sleep"):
            try:
                automation.run_refresh_and_validate(config, refresh_indexes=False)
            except automation.AutomationError as exc:
                timeout_error = exc
        check(timeout_error is not None and timeout_error.code == "memory_validation_timeout",
              "exhausted local validation retries expose the exact failing phase")
        check(
            timeout_error is not None
            and timeout_error.notification_detail.get("auto_repair") == "retry_exhausted"
            and timeout_error.notification_detail.get("attempts") == 3,
            "failure detail reports the automatic repair attempts instead of a bare TimeoutExpired",
        )
        unreadable = memory_root / "02_内部业务资料" / "unreadable.md"
        unreadable.write_text("temporary", encoding="utf-8")
        unreadable.chmod(0o000)
        strict_error = ""
        try:
            automation.memory_records(memory_root, strict=True)
        except automation.AutomationError as exc:
            strict_error = exc.code
        finally:
            unreadable.chmod(0o600)
        check(strict_error == "memory_source_unreadable", "unreadable governed memory fails closed before model use")
        automation.MEMORY_LOCK = root / "state" / "memory-index.lock"
        automation.LISTENER_LOCK = root / "state" / "listener.lock"
        automation.HOURLY_LOCK = root / "state" / "hourly.lock"
        automation.SHARED_AI_LOCK = root / "state" / "shared-ai.lock"

        connection = automation.connect_database(config)
        try:
            connection.execute("BEGIN IMMEDIATE")
            observed = automation.now_iso()
            automation.ensure_conversation(connection, "conv-direct", observed, "测试单聊")
            run_id = automation.begin_auxiliary_run(connection, "baseline", observed)
            baseline = chat_sync.normalize_message(
                {
                    "messageId": "msg-baseline",
                    "conversationId": "conv-direct",
                    "createTime": "2020-01-02 08:00:00",
                    "senderId": "user-a",
                    "sender": "Alice",
                    "text": "服务器密码是多少？",
                    "resourceRefs": [],
                },
                "conv-direct",
                20000,
            )
            chat_sync.upsert_message(connection, baseline, observed, run_id, enqueue_changes=False)
            automation.complete_auxiliary_run(connection, run_id, "success", 1, 1, 0)
            connection.commit()
            check(connection.execute("PRAGMA user_version").fetchone()[0] == 3, "database migrated to v3")
            check(connection.execute("SELECT COUNT(*) FROM semantic_queue").fetchone()[0] == 0, "baseline is not queued")
            check(connection.execute("PRAGMA quick_check").fetchone()[0] == "ok", "baseline database quick_check passes")
        finally:
            connection.close()

        direct_event = {
            "event_id": "event-direct-1",
            "message_id": "msg-sensitive",
            "conversation_id": "conv-direct",
            "create_time": "2020-01-02 08:01:00",
            "event_time": "2020-01-02T08:01:00+08:00",
            "sender": "Alice",
            "sender_open_dingtalk_id": "user-a",
            "content": "demo R4nd0m!Pass",
        }
        first_event = automation.ingest_event(config, "direct", direct_event)
        duplicate_event = automation.ingest_event(config, "direct", direct_event)
        check(first_event["status"] == "staged" and first_event["queued"], "direct event is staged and queued")
        check(duplicate_event["status"] == "duplicate", "duplicate event is idempotent")
        connection = automation.connect_database(config)
        try:
            sensitive = connection.execute(
                "SELECT text_redacted,original_text_sha256 FROM messages WHERE message_id='msg-sensitive'"
            ).fetchone()
            check(sensitive["text_redacted"] == "[敏感回复已脱敏]", "event reply is context-redacted before storage")
            check(sensitive["original_text_sha256"] == "", "redacted event has no original fingerprint")
            check(connection.execute("SELECT COUNT(*) FROM event_inbox").fetchone()[0] == 1, "event inbox deduplicates by event ID")
        finally:
            connection.close()

        group_event = {
            "event_id": "event-group-1",
            "message_id": "msg-group",
            "conversation_id": "conv-group",
            "create_time": "2020-01-02 09:00:00",
            "event_time": "2020-01-02T09:00:00+08:00",
            "sender": "Bob",
            "sender_open_dingtalk_id": "user-b",
            "content": "项目计划初版",
        }
        automation.ingest_event(config, "group", group_event)
        hydration = automation.hydrate_pending_events(config)
        check(hydration["status"] == "success" and hydration["hydrated"] == 2, "direct and group events hydrate together")
        connection = automation.connect_database(config)
        try:
            refs = connection.execute(
                "SELECT resource_refs_json FROM messages WHERE message_id='msg-group'"
            ).fetchone()[0]
            check("secret.invalid" not in refs and "[已脱敏]" in refs, "attachment URL is removed while the resource reference remains")
            check(connection.execute("SELECT COUNT(*) FROM event_inbox WHERE state='hydrated'").fetchone()[0] == 2, "hydrated event state is durable")
        finally:
            connection.close()

        edited_group = dict(group_event)
        edited_group.update({"event_id": "event-group-edit", "content": "项目计划已调整"})
        edited = automation.ingest_event(config, "group", edited_group)
        check(edited["queued"], "edited message creates a new semantic version")
        connection = automation.connect_database(config)
        try:
            pending_group = connection.execute(
                "SELECT COUNT(*) FROM semantic_queue WHERE message_id='msg-group' AND state='pending'"
            ).fetchone()[0]
            superseded_group = connection.execute(
                "SELECT COUNT(*) FROM semantic_queue WHERE message_id='msg-group' AND state='no_business_change'"
            ).fetchone()[0]
            check(pending_group == 1 and superseded_group >= 1, "only the newest unprocessed message version remains pending")
        finally:
            connection.close()

        failed_event = {
            "event_id": "event-fail-1",
            "message_id": "msg-fail",
            "conversation_id": "conv-group",
            "create_time": "2020-01-02 10:00:00",
            "event_time": "2020-01-02T10:00:00+08:00",
            "sender": "Bob",
            "sender_open_dingtalk_id": "user-b",
            "content": "待补水业务消息",
        }
        automation.ingest_event(config, "group", failed_event)
        os.environ["MOCK_DWS_FAIL"] = "1"
        try:
            failure_results = [automation.hydrate_pending_events(config) for _ in range(3)]
        finally:
            os.environ.pop("MOCK_DWS_FAIL", None)
        check(failure_results[-1]["blocked"] >= 1, "repeated hydration failure becomes blocked without dropping the event")
        connection = automation.connect_database(config)
        try:
            check(connection.execute("SELECT COUNT(*) FROM event_inbox WHERE state='blocked'").fetchone()[0] >= 1, "blocked event remains in inbox")
            check(connection.execute("SELECT COUNT(*) FROM semantic_queue WHERE message_id='msg-fail'").fetchone()[0] == 1, "blocked event message remains queued")
            reconciliation_run = str(uuid.uuid4())
            stamp = automation.now_iso()
            connection.execute(
                "INSERT INTO sync_runs(run_id,started_at,completed_at,status,mode) VALUES(?,?,?,'success','incremental')",
                (reconciliation_run, stamp, stamp),
            )
            connection.execute("UPDATE messages SET last_run_id=? WHERE message_id='msg-fail'", (reconciliation_run,))
            connection.commit()
        finally:
            connection.close()
        reconciled = automation.reconcile_events_from_incremental(config, {"run_id": reconciliation_run})
        check(reconciled == 1, "successful incremental scan resolves the matching blocked event")

        event_lines: "queue.Queue[tuple[str, str]]" = queue.Queue()
        stop_event = threading.Event()
        consumer = automation.EventConsumer(config, "all-direct", event_lines, stop_event)
        consumer.start_with_budget()
        kind, event_line = event_lines.get(timeout=5)
        check(consumer.ready.is_set() and kind == "direct", "consumer waits for the explicit ready marker")
        check(json.loads(event_line)["type"] == "message", "consumer continuously drains NDJSON stdout")
        consumer.shutdown()
        check(consumer.process is not None and consumer.process.poll() == 0, "consumer exits gracefully on stdin EOF")
        retryable, retry_after = automation.find_retry_metadata({"error": {"retryable": True, "retry_after_seconds": 9}})
        check(retryable is True and retry_after == 9, "listener retry metadata follows the event contract")

        stale_batch = automation.prepare_batch(config)
        check(stale_batch is not None, "non-empty queue prepares one semantic batch")
        connection = automation.connect_database(config)
        try:
            old = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
            connection.execute("UPDATE semantic_batches SET created_at=? WHERE batch_id=?", (old, stale_batch["batch_id"]))
            connection.commit()
        finally:
            connection.close()
        check(automation.recover_stale_batches(config) == 1, "sleep or crash recovery releases one stale batch")
        connection = automation.connect_database(config)
        try:
            check(connection.execute("SELECT COUNT(*) FROM semantic_queue WHERE state='failed'").fetchone()[0] >= 1, "stale claimed messages remain retryable")
        finally:
            connection.close()

        batch = automation.prepare_batch(config)
        check(batch is not None and batch["message_count"] <= 100, "retry batch respects the 100-message cap")
        unsafe_error = ""
        try:
            automation.validate_proposal(
                config,
                batch["batch_id"],
                {
                    "batch_id": batch["batch_id"],
                    "source_summary": {
                        "title": "## injected heading",
                        "summary": "测试摘要",
                        "evidence_boundary": "仅供测试",
                        "source_tokens": ["S001"],
                    },
                    "candidates": [],
                    "no_business_change": False,
                },
            )
        except automation.AutomationError as exc:
            unsafe_error = exc.code
        check(unsafe_error == "proposal_markup", "trusted writer rejects structural Markdown injection")
        batch_json = automation.canonical_json(batch["document"])
        check("conv-direct" not in batch_json and "msg-sensitive" not in batch_json, "model input omits raw message and conversation IDs")
        check("R4nd0m" not in batch_json, "model input contains no source secret")
        check(batch["text_chars"] <= 300, "model input respects the configured character cap")
        os.environ["MOCK_CODEX_RECEIPT"] = str(codex_receipt)
        os.environ["OPENAI_API_KEY"] = "must-not-reach-mock"
        try:
            proposal = automation.invoke_codex(config, batch)
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        receipt = json.loads(codex_receipt.read_text(encoding="utf-8"))
        check(proposal["no_business_change"] is True, "structured model proposal validates")
        check(automation.semantic_model_called_this_slot(config), "semantic call timestamp enforces the one-call-per-half-hour gate")
        check("--ephemeral" in receipt["args"] and "read-only" in receipt["args"], "semantic Codex run is ephemeral and read-only")
        check("deepseek-flash" in receipt["args"] and "--ignore-rules" in receipt["args"], "semantic Codex run pins model and isolated rules")
        check(not receipt["api_key_present"] and not receipt["base_url_present"], "paid API environment is stripped from Codex")
        automation.release_batch_failure(config, batch["batch_id"], "injected_write_failure")
        reusable = automation.claim_reusable_proposal(config)
        check(reusable is not None and reusable[0]["batch_id"] == batch["batch_id"], "validated proposal is reused after a write-only failure")
        with mock.patch.object(automation, "run_refresh_and_validate", return_value={"validator": {"verdict": "pass"}}):
            applied_noop = automation.apply_proposal(config, reusable[0], reusable[1])
        check(applied_noop["writes"] == 0, "no-business proposal performs no memory write")

        insert_message(config, "msg-model-fail", "conv-group", "模型失败后必须保留", "2020-01-02 11:00:00")
        failing_batch = automation.prepare_batch(config)
        check(failing_batch is not None, "new message prepares a failure-injection batch")
        os.environ["MOCK_CODEX_FAIL"] = "1"
        model_error = ""
        try:
            automation.invoke_codex(config, failing_batch)
        except automation.AutomationError as exc:
            model_error = exc.code
        finally:
            os.environ.pop("MOCK_CODEX_FAIL", None)
        automation.release_batch_failure(config, failing_batch["batch_id"], model_error)
        check(model_error == "codex_subscription_limit", "subscription exhaustion is classified without fallback")
        connection = automation.connect_database(config)
        try:
            failed_state = connection.execute(
                "SELECT state FROM semantic_queue WHERE message_id='msg-model-fail' ORDER BY queue_id DESC LIMIT 1"
            ).fetchone()[0]
            check(failed_state == "failed", "model failure preserves the message for the next hourly retry")
            connection.execute(
                "UPDATE semantic_queue SET state='no_business_change',applied_at=? WHERE state IN ('pending','failed','deferred')",
                (automation.now_iso(),),
            )
            connection.commit()
        finally:
            connection.close()

        insert_message(config, "msg-long", "conv-group", "业务更新" * 500, "2020-01-02 12:00:00")
        long_batch = automation.prepare_batch(config)
        check(long_batch is not None and long_batch["document"]["messages"][0]["input_truncated"], "oversized model input is truncated and deferred by cap")
        check(long_batch["text_chars"] <= 300, "oversized input never exceeds the semantic character limit")
        long_proposal = automation.invoke_codex(config, long_batch)
        with mock.patch.object(automation, "run_refresh_and_validate", return_value={"validator": {"verdict": "pass"}}):
            automation.apply_proposal(config, long_batch, long_proposal)

        writer_batch = {
            "batch_id": str(uuid.uuid4()),
            "document": {
                "messages": [
                    {"source_token": "S001", "time": "2020-01-02T13:00:00+08:00"},
                    {"source_token": "S002", "time": "2020-01-02T13:05:00+08:00"},
                ]
            },
        }
        writer_proposal = {
            "batch_id": writer_batch["batch_id"],
            "source_summary": {
                "title": "当天钉钉业务摘要",
                "summary": "出现一项新业务状态和一项需立即核对的交期冲突。",
                "evidence_boundary": "仅来自单一钉钉聊天，尚未由责任人或权威文件确认。",
                "source_tokens": ["S001", "S002"],
            },
            "candidates": [
                {
                    "action": "link_existing",
                    "dedupe_key": "confirmed-existing-fact",
                    "title": "补充已确认事实来源",
                    "category": "business_cognition",
                    "memory_kind": "fact",
                    "summary": "聊天内容与现有已确认事实一致。",
                    "evidence_boundary": "本批只追加来源，不改写原事实。",
                    "source_tokens": ["S001"],
                    "existing_memory_id": "BUSINESS-20260825-001",
                    "conflict_reason": None,
                },
                {
                    "action": "annotate_page",
                    "dedupe_key": "new-project-status",
                    "title": "新项目状态待确认",
                    "category": "internal_material",
                    "memory_kind": "status",
                    "summary": "聊天提出一项新的项目状态。",
                    "evidence_boundary": "仅有单一聊天来源。",
                    "source_tokens": ["S001"],
                    "existing_memory_id": None,
                    "conflict_reason": None,
                },
                {
                    "action": "conflict",
                    "dedupe_key": "critical-delivery-conflict",
                    "title": "客户交期口径冲突",
                    "category": "external_communication",
                    "memory_kind": "open_question",
                    "summary": "聊天中的交期口径与现有确认信息不一致。",
                    "evidence_boundary": "必须由责任人与当前交付计划复核。",
                    "source_tokens": ["S002"],
                    "existing_memory_id": "BUSINESS-20260825-001",
                    "conflict_reason": "涉及客户交期承诺，不能自动采用聊天口径。",
                },
            ],
            "no_business_change": False,
        }
        changes, target_ids, conflicts = automation.build_memory_changes(config, writer_batch, writer_proposal)
        check(
            len(changes) == 4 and len(target_ids) == 4,
            f"trusted writer builds source, linked and inline claim changes ({len(changes)}/{len(target_ids)})",
        )
        check(conflicts == ["客户交期口径冲突"], "critical conflict is surfaced for alerting")
        result = automation.apply_changes(
            config,
            writer_batch["batch_id"],
            changes,
            verifier=lambda _config: {"verdict": "pass"},
        )
        check(result["writes"] == 4 and result["validated"], "trusted writer atomically applies and verifies all changes")
        written = "\n".join(path.read_text(encoding="utf-8") for path in changes)
        check("[!warning] 来源待核" in written and "business-claim:v2" in written, "new business claim is visibly pending on its page")
        check("[!danger] 存在冲突" in written and "客户交期口径冲突" in written, "critical contradiction is visibly conflicting")
        check(written.count("<!-- business-claim:v2 ") == 2, "two candidates retain distinct stable claim markers")
        check("status: active" in confirmed_path.read_text(encoding="utf-8") and "fact_status: confirmed" in confirmed_path.read_text(encoding="utf-8"), "linking a source preserves the confirmed active fact")
        check(f"dingtalk-batch:{writer_batch['batch_id']}" in written, "Obsidian source uses only the batch UUID")
        check("conv-group" not in written and "msg-group" not in written, "Obsidian output contains no raw chat identifiers")

        daily_chat_path = next(path for path in changes if path.parent.name == "04_对话来源摘要")
        minutes_source_path = memory_root / "04_对话来源摘要" / "20260825-钉钉听记-回归测试.md"
        minutes_source_path.write_text(
            make_source_note(
                "BUSINESS-20260825-097",
                f"dingtalk-minutes-batch:{uuid.uuid4()}",
                "2020-01-02T13:15:00+08:00",
            ),
            encoding="utf-8",
        )
        followup_batch = {
            "batch_id": str(uuid.uuid4()),
            "document": {"messages": [{"source_token": "S001", "time": "2020-01-02T13:20:00+08:00"}]},
        }
        followup_proposal = {
            "batch_id": followup_batch["batch_id"],
            "source_summary": {
                "title": "当天钉钉业务摘要补充",
                "summary": "当天聊天摘要新增一项测试状态。",
                "evidence_boundary": "仅来自测试聊天。",
                "source_tokens": ["S001"],
            },
            "candidates": [],
            "no_business_change": False,
        }
        followup_changes, followup_ids, _ = automation.build_memory_changes(
            config, followup_batch, followup_proposal
        )
        check(
            set(followup_changes) == {daily_chat_path} and len(followup_ids) == 1,
            "daily chat summary ignores same-day DingTalk Minutes source notes",
        )

        duplicate_chat_path = memory_root / "04_对话来源摘要" / "20260825-钉钉-重复聊天摘要.md"
        duplicate_chat_path.write_text(
            make_source_note(
                "BUSINESS-20260825-096",
                f"dingtalk-batch:{uuid.uuid4()}",
                "2020-01-02T13:16:00+08:00",
            ),
            encoding="utf-8",
        )
        duplicate_error = ""
        try:
            automation.build_memory_changes(config, followup_batch, followup_proposal)
        except automation.AutomationError as exc:
            duplicate_error = exc.code
        finally:
            duplicate_chat_path.unlink()
        check(
            duplicate_error == "memory_daily_summary_collision",
            "two real daily chat summaries still fail closed",
        )

        untrusted_path = memory_root / "90_待确认" / "untrusted-existing.md"
        untrusted_path.write_text(
            make_note("BUSINESS-20260825-099", "旧待确认", status="review_required", fact_status="pending"),
            encoding="utf-8",
        )
        untrusted_proposal = {
            "batch_id": str(uuid.uuid4()),
            "source_summary": None,
            "candidates": [{
                **writer_proposal["candidates"][0],
                "dedupe_key": "untrusted-link-downgrade",
                "existing_memory_id": "BUSINESS-20260825-099",
            }],
            "no_business_change": False,
        }
        untrusted_batch = {
            "batch_id": untrusted_proposal["batch_id"],
            "document": {"messages": [{"source_token": "S001", "time": "2020-01-02T13:30:00+08:00"}]},
        }
        downgraded_changes, _downgraded_ids, _ = automation.build_memory_changes(
            config, untrusted_batch, untrusted_proposal
        )
        downgraded_text = "\n".join(downgraded_changes.values())
        check(untrusted_path not in downgraded_changes and "原位核验说明" in downgraded_text,
              "link to an untrusted memory safely becomes an inline pending annotation")
        check("[!warning] 来源待核" in downgraded_text and "fact_status: confirmed" not in downgraded_text,
              "downgraded link remains visibly uncertain without claiming a confirmed fact")

        second_batch = {
            "batch_id": str(uuid.uuid4()),
            "document": {"messages": [{"source_token": "S001", "time": "2020-01-02T14:00:00+08:00"}]},
        }
        repeat_proposal = {
            "batch_id": second_batch["batch_id"],
            "source_summary": None,
            "candidates": [dict(writer_proposal["candidates"][1])],
            "no_business_change": False,
        }
        repeat_changes, repeat_ids, _repeat_conflicts = automation.build_memory_changes(config, second_batch, repeat_proposal)
        check(len(repeat_changes) == 1 and repeat_ids[0] in target_ids, "semantic dedupe updates the same pending note")

        existing_target = next(iter(repeat_changes))
        original = existing_target.read_text(encoding="utf-8")
        new_target = memory_root / "02_内部业务资料" / "20260825-rollback-new.md"
        check(
            automation.safe_target(memory_root, memory_root / "00_记忆总览.md"),
            "generated top-level overview is an exact rollback-safe target",
        )
        check(
            automation.safe_target(memory_root, memory_root / "90_待确认" / "00_待确认总表.md")
            and not automation.safe_target(memory_root, memory_root / "90_待确认" / "business-body.md"),
            "pending area allows only its generated index and rejects standalone business content",
        )
        check(
            not automation.safe_target(memory_root, memory_root / "unexpected.md"),
            "arbitrary top-level memory files remain outside the rollback allowlist",
        )
        failed = False
        try:
            automation.apply_changes(
                config,
                str(uuid.uuid4()),
                {existing_target: original + "\n破坏性测试内容\n", new_target: "temporary\n"},
                verifier=lambda _config: (_ for _ in ()).throw(
                    automation.AutomationError("validation_failed", "injected validator failure")
                ),
            )
        except automation.AutomationError:
            failed = True
        check(failed, "injected validation failure aborts the write")
        check(existing_target.read_text(encoding="utf-8") == original, "failed write restores the prior note byte-for-byte")
        check(not new_target.exists(), "failed write removes the uncommitted new note")

        concurrent_target = memory_root / "02_内部业务资料" / "concurrent-rollback-test.md"
        applied_existing = original + "\n事务中间态\n"
        concurrent_error = ""

        def concurrent_verifier(_config: dict[str, object]) -> dict[str, object]:
            concurrent_target.write_text("用户并发修改\n", encoding="utf-8")
            raise automation.AutomationError("validation_failed", "injected concurrent edit")

        try:
            automation.apply_changes(
                config,
                str(uuid.uuid4()),
                {existing_target: applied_existing, concurrent_target: "事务写入\n"},
                verifier=concurrent_verifier,
            )
        except automation.AutomationError as exc:
            concurrent_error = exc.code
        check(
            concurrent_error == "rollback_rollback_concurrent_change",
            "rollback refuses a target changed after the applied snapshot",
        )
        check(
            existing_target.read_text(encoding="utf-8") == applied_existing,
            "rollback preflight prevents a partial restore before the conflicting target",
        )
        check(
            concurrent_target.read_text(encoding="utf-8") == "用户并发修改\n",
            "rollback preserves the concurrent user edit",
        )
        existing_target.write_text(original, encoding="utf-8")
        concurrent_target.unlink()

        notify_calls: list[tuple[str, str]] = []
        original_notify = automation.notify_mac
        automation.notify_mac = lambda title, body: notify_calls.append((title, body)) is None or True
        config["dingtalk_automation"]["mac_notifications"] = True
        try:
            automation.raise_alert(config, "failure", "critical", "dedupe-test", "脱敏异常标题", "test_error")
            automation.raise_alert(config, "failure", "critical", "dedupe-test", "脱敏异常标题", "test_error")
        finally:
            automation.notify_mac = original_notify
        check(len(notify_calls) == 0, "legacy AppleScript notification is replaced by the governed alert owner")
        connection = automation.connect_database(config)
        try:
            check(connection.execute("SELECT COUNT(*) FROM automation_alerts WHERE dedupe_key='dedupe-test'").fetchone()[0] == 1, "alert ledger deduplicates the same exception")
        finally:
            connection.close()

        config["dingtalk_automation"]["mac_notifications"] = False
        config["dingtalk_automation"]["codex_alert_record"] = True
        before_alert_record = codex_receipt.read_bytes()
        first_record = automation.record_pending_alerts_in_codex(config)
        check(first_record["status"] == "recorded_local", "sanitized alert metadata is recorded locally without a record-only model task")
        automation.raise_alert(config, "failure", "critical", "dedupe-test", "脱敏异常标题", "test_error")
        second_record = automation.record_pending_alerts_in_codex(config)
        check(second_record["status"] == "recorded_local" and codex_receipt.read_bytes() == before_alert_record,
              "repeated alerts do not call a model or overwrite the model receipt")
        automation.resolve_alert(config, "dedupe-test")
        connection = automation.connect_database(config)
        try:
            check(connection.execute("SELECT status FROM automation_alerts WHERE dedupe_key='dedupe-test'").fetchone()[0] == "resolved", "recovered alert is marked resolved")
        finally:
            connection.close()

        status = automation.safe_status_snapshot(config)
        status_text = automation.canonical_json(status)
        check(status["database_quick_check"] == "ok", "privacy-safe status includes database integrity")
        check("msg-sensitive" not in status_text and "conv-direct" not in status_text and "R4nd0m" not in status_text, "status exposes no message text or raw IDs")
        database = automation.runtime_paths(config)["database"]
        check(database.stat().st_mode & 0o777 == 0o600, "source database permissions are 0600")
        check(database.parent.stat().st_mode & 0o777 == 0o700, "source database directory permissions are 0700")

        bad_database = root / "corrupt.sqlite"
        bad_database.write_bytes(b"not-a-sqlite-database")
        detected_corruption = False
        try:
            bad_connection = sqlite3.connect(bad_database)
            bad_connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError:
            detected_corruption = True
        finally:
            try:
                bad_connection.close()
            except (NameError, UnboundLocalError):
                pass
        check(detected_corruption, "SQLite integrity failure injection is detectable")

        handoff_receipt = {"status": "success", "processed": 1, "done": 1, "deferred": 0, "failed": 0}
        with mock.patch.object(
            automation.syncv2,
            "run_minutes_review_handoff",
            return_value=handoff_receipt,
        ) as handoff:
            skipped = automation.handoff_minutes_after_chat_model(
                automation.runtime_paths(config), False
            )
            yielded = automation.handoff_minutes_after_chat_model(
                automation.runtime_paths(config), True
            )
        check(skipped["status"] == "not_needed", "chat without a model call does not request a handoff")
        check(yielded == handoff_receipt and handoff.call_count == 1,
              "chat yields once to a pending reviewed recording after using the model slot")

        scheduled_args = SimpleNamespace(
            command="hourly",
            config=root / "config.json",
            dry_run=False,
            manual=False,
            skip_reconcile=False,
        )
        with mock.patch.object(automation, "parse_arguments", return_value=scheduled_args), \
                mock.patch.object(automation, "load_config", return_value={}), \
                mock.patch.object(automation, "runtime_paths", return_value={}), \
                mock.patch.object(
                    automation,
                    "run_hourly",
                    side_effect=automation.AutomationError("already_running", "fixture"),
                ), \
                mock.patch("builtins.print") as scheduled_print:
            scheduled_exit = automation.main()
        scheduled_receipt = json.loads(str(scheduled_print.call_args.args[0]))
        check(
            scheduled_exit == 0 and scheduled_receipt == {
                "status": "deferred", "reason": "already_running"
            },
            "a scheduled collision is an expected deferral rather than a launchd failure",
        )

        manual_args = SimpleNamespace(**{**vars(scheduled_args), "manual": True})
        with mock.patch.object(automation, "parse_arguments", return_value=manual_args), \
                mock.patch.object(automation, "load_config", return_value={}), \
                mock.patch.object(automation, "runtime_paths", return_value={}), \
                mock.patch.object(
                    automation,
                    "run_hourly",
                    side_effect=automation.AutomationError("already_running", "fixture"),
                ), \
                mock.patch("builtins.print") as manual_print:
            manual_exit = automation.main()
        manual_receipt = json.loads(str(manual_print.call_args.args[0]))
        check(
            manual_exit == 1 and manual_receipt.get("error_code") == "already_running",
            "a direct manual collision remains explicit to its caller",
        )

        os.environ.pop("MOCK_CODEX_RECEIPT", None)

    print(json.dumps({"verdict": "pass", "checks": len(checks)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
