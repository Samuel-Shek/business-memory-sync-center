#!/usr/bin/env python3
"""Offline acceptance tests for sync_dingtalk_chat.py."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path

_video_hold_temp = tempfile.TemporaryDirectory(prefix="chat-test-video-hold-")
os.environ["BUSINESS_VIDEO_HOLD_DB"] = str(Path(_video_hold_temp.name) / "holds.sqlite")

import sync_dingtalk_chat as sync_module


ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "bin" / "sync_dingtalk_chat.py"

MOCK_DWS = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
phase = os.environ.get("MOCK_PHASE", "1")

if args[:2] == ["chat", "+conversation-list"]:
    conversations = [
        {"conversationName": "测试业务群", "openConversationId": "conv-a"},
        {"conversationName": "范围回退空群", "openConversationId": "conv-b"},
        {"conversationName": "持续分页异常群", "openConversationId": "conv-c"}
    ]
    if phase == "4":
        conversations += [
            {"conversationName": "已列出未补历史群", "openConversationId": "conv-listed-old"},
            {"conversationName": "新建业务群", "openConversationId": "conv-listed-new"}
        ]
    print(json.dumps({
        "complete": True,
        "partial": False,
        "hasMore": False,
        "truncated": False,
        "failedCount": 0,
        "conversations": conversations
    }, ensure_ascii=False))
    raise SystemExit(0)

if args[:2] == ["chat", "+chat-messages"]:
    conversation = args[args.index("--group") + 1]
    if phase == "4":
        messages = []
        # History exists only before any bounded window; it is visible only to a read from the beginning.
        if conversation.startswith("conv-listed-") and args[args.index("--start") + 1].startswith("2000-01-01"):
            suffix = conversation.rsplit("-", 1)[-1]
            messages = [{
                "conversationId": conversation,
                "createTime": "2020-01-02 09:00:00",
                "messageId": "msg-listed-" + suffix,
                "resourceRefs": [],
                "sender": "历史成员",
                "senderId": "user-history",
                "text": "建群初期历史消息 " + suffix
            }]
        print(json.dumps({
            "complete": True,
            "partial": False,
            "hasMore": False,
            "truncated": False,
            "failedCount": 0,
            "pagesFetched": 1,
            "stopReason": "source_complete",
            "messages": messages
        }, ensure_ascii=False))
        raise SystemExit(0)
    if phase == "3":
        messages = []
        if conversation == "conv-a":
            messages = [{
                "conversationId": "conv-a",
                "createTime": "2020-01-02 09:00:00",
                "messageId": "msg-5",
                "resourceRefs": [],
                "sender": "Current User",
                "senderId": "self-user",
                "text": "本人发送的新业务消息"
            }]
        if conversation.startswith("conv-event-"):
            suffix = conversation.rsplit("-", 1)[-1]
            messages = [{
                "conversationId": conversation,
                "createTime": "2020-01-02 09:00:00",
                "messageId": "msg-event-" + suffix,
                "resourceRefs": [],
                "sender": "历史成员",
                "senderId": "user-history",
                "text": "事件会话历史消息 " + suffix
            }]
        print(json.dumps({
            "complete": True,
            "partial": False,
            "hasMore": False,
            "truncated": False,
            "failedCount": 0,
            "pagesFetched": 1,
            "stopReason": "source_complete",
            "messages": messages
        }, ensure_ascii=False))
        raise SystemExit(0)
    if conversation == "conv-a":
        text = "密码: hunter2 项目节点更新" if phase == "1" else "密码: changed9 项目节点已确认"
        messages = [{
            "conversationId": "conv-a",
            "createTime": "2020-01-02 08:00:00",
            "messageId": "msg-1",
            "resourceRefs": [{"fileId": "file-1", "downloadUrl": "https://secret.invalid/t"}],
            "sender": "Alice",
            "senderId": "user-a",
            "text": text
        }]
        if phase == "1":
            messages.append({
                "conversationId": "conv-a",
                "createTime": "2020-01-02 08:01:00",
                "messageId": "msg-2",
                "resourceRefs": [],
                "sender": "Bob",
                "senderId": "user-b",
                "text": "普通业务消息"
            })
        messages.extend([
            {
                "conversationId": "conv-a",
                "createTime": "2020-01-02 08:02:00",
                "messageId": "msg-3",
                "resourceRefs": [],
                "sender": "Alice",
                "senderId": "user-a",
                "text": "服务器账号是多少"
            },
            {
                "conversationId": "conv-a",
                "createTime": "2020-01-02 08:03:00",
                "messageId": "msg-4",
                "resourceRefs": [],
                "sender": "Bob",
                "senderId": "user-b",
                "text": "demo R4nd0m!Pass"
            }
        ])
        print(json.dumps({
            "complete": True,
            "partial": False,
            "hasMore": False,
            "truncated": False,
            "failedCount": 0,
            "pagesFetched": 1,
            "stopReason": "source_complete",
            "messages": messages
        }, ensure_ascii=False))
        raise SystemExit(0)
    if phase == "1" and conversation == "conv-b" and "--start" in args:
        print(json.dumps({
            "complete": True,
            "partial": False,
            "hasMore": False,
            "truncated": False,
            "failedCount": 0,
            "pagesFetched": 1,
            "stopReason": "source_complete",
            "messages": []
        }, ensure_ascii=False))
        raise SystemExit(0)
    if phase == "1":
        print(json.dumps({
            "complete": False,
            "partial": False,
            "hasMore": True,
            "truncated": False,
            "failedCount": 1,
            "pagesFetched": 1,
            "stopReason": "pagination_error",
            "messages": []
        }, ensure_ascii=False))
        print(json.dumps({"error": {
            "reason": "chat_messages_incomplete",
            "message": "mock pagination error"
        }}), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({
        "complete": True,
        "partial": False,
        "hasMore": False,
        "truncated": False,
        "failedCount": 0,
        "pagesFetched": 1,
        "stopReason": "source_complete",
        "messages": []
    }, ensure_ascii=False))
    raise SystemExit(0)

print(json.dumps({"error": {"reason": "unexpected_mock_command"}}), file=sys.stderr)
raise SystemExit(1)
'''


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_sync(config: Path, mock: Path, phase: str, *extra: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "MOCK_PHASE": phase}
    return subprocess.run(
        ["/usr/bin/python3", str(SYNC), "--config", str(config), "--dws-bin", str(mock), *extra],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        env=environment,
    )


def main() -> int:
    checks = 0
    require(
        sync_module.should_try_range_fallback(
            "incremental",
            True,
            {"complete": False, "hasMore": True, "failedCount": 1, "messages": [{"messageId": "kept"}]},
            1,
        ),
        "historical backfill retries by bounded range even when earlier pages returned messages",
    )
    checks += 1
    with tempfile.TemporaryDirectory(prefix="business-dingtalk-selftest-") as temporary:
        root = Path(temporary)
        external_root = root / "external"
        memory_root = root / "memory"
        (memory_root / "99_规则与模板").mkdir(parents=True)
        mock = root / "mock_dws.py"
        mock.write_text(MOCK_DWS, encoding="utf-8")
        mock.chmod(0o700)
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "external_root": str(external_root),
                    "memory_root": str(memory_root),
                    "dingtalk_chat": {
                        "timeout_seconds": 10,
                        "page_size": 100,
                        "page_limit": 5,
                        "max_items_per_conversation": 1000,
                        "max_text_chars": 20000,
                        "schedule_label": "测试计划"
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        first = run_sync(config, mock, "1")
        require(first.returncode == 2, f"partial run should exit 2: {first.stderr} {first.stdout}")
        first_result = json.loads(first.stdout)
        require(first_result["status"] == "partial", "first run must be partial")
        require(first_result["conversations_complete"] == 2, "business and range-fallback conversations should be complete")
        require(first_result["conversations_incomplete"] == 1, "one conversation should be incomplete")
        require(first_result["range_fallbacks"] == 1, "one empty-page anomaly should resolve through range fallback")
        require(first_result["messages_inserted"] == 4, "first run should insert four messages")
        require(first_result["database_secure_delete"] == 1, "secure deletion must be enabled")
        checks += 6

        database = external_root / "dingtalk-chat" / "state" / "chat.sqlite"
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        stored = connection.execute(
            "SELECT text_redacted,original_text_sha256,resource_refs_json,redaction_count FROM messages WHERE message_id='msg-1'"
        ).fetchone()
        require("hunter2" not in stored["text_redacted"], "password must not be stored")
        require("[已脱敏]" in stored["text_redacted"], "password placeholder is required")
        require("secret.invalid" not in stored["resource_refs_json"], "signed/download URL must be removed")
        require(stored["original_text_sha256"] == "", "redacted text must not retain an original fingerprint")
        require(stored["redaction_count"] >= 1, "redaction count must be recorded")
        contextual = connection.execute(
            "SELECT text_redacted FROM messages WHERE message_id='msg-4'"
        ).fetchone()[0]
        require(contextual == "[敏感回复已脱敏]", "reply to a credential prompt must be fully redacted")
        ordinary_hash = connection.execute(
            "SELECT original_text_sha256 FROM messages WHERE message_id='msg-2'"
        ).fetchone()[0]
        require(len(ordinary_hash) == 64, "non-sensitive source text may retain its SHA-256 fingerprint")
        require(connection.execute("PRAGMA quick_check").fetchone()[0] == "ok", "SQLite quick_check must pass")
        require(connection.execute("PRAGMA user_version").fetchone()[0] == 3, "SQLite schema version must be 3")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require(
            {"event_inbox", "semantic_queue", "semantic_batches", "batch_messages", "automation_alerts"}.issubset(tables),
            "automation queue tables must exist",
        )
        require(connection.execute("SELECT COUNT(*) FROM semantic_queue").fetchone()[0] == 4, "new messages must enter semantic queue")
        connection.close()
        checks += 11

        second = run_sync(config, mock, "2")
        require(second.returncode == 0, f"complete rerun should exit 0: {second.stderr} {second.stdout}")
        second_result = json.loads(second.stdout)
        require(second_result["status"] == "success", "second run must succeed")
        require(second_result["messages_inserted"] == 0, "rerun must deduplicate existing messages")
        require(second_result["messages_updated"] == 1, "changed message must create one update")
        checks += 4

        connection = sqlite3.connect(database)
        visible = dict(connection.execute("SELECT message_id,current_visible FROM messages").fetchall())
        versions = connection.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0]
        scans = connection.execute("SELECT status,COUNT(*) FROM conversation_scans GROUP BY status").fetchall()
        require(visible == {"msg-1": 1, "msg-2": 0, "msg-3": 1, "msg-4": 1}, "complete scan must tombstone only the missing current view")
        require(versions == 4, "redacted messages must not retain earlier source fingerprints")
        require(dict(scans) == {"complete": 5, "incomplete": 1}, "scan ledger must retain fallback and incomplete history")
        require(connection.execute("SELECT COUNT(*) FROM semantic_queue").fetchone()[0] == 6, "update and hidden changes must be queued once")
        connection.close()
        checks += 4

        connection = sqlite3.connect(database)
        for index in range(1, 5):
            conversation_id = f"conv-event-{index}"
            connection.execute(
                """
                INSERT INTO conversations(
                  conversation_id,conversation_name,first_seen_at,last_seen_at,last_scan_at,
                  last_complete_scan_at,last_scan_status,visible_message_count
                ) VALUES(?,?,?,?,?,NULL,'event',0)
                """,
                (conversation_id, f"事件会话 {index}", "2020-01-02T09:00:00+08:00", "2020-01-02T09:00:00+08:00", "2020-01-02T09:00:00+08:00"),
            )
        connection.execute(
            "INSERT INTO sync_runs(run_id,started_at,status,mode) VALUES('stale-run','2020-01-02T00:00:00+08:00','running','incremental')"
        )
        connection.commit()
        connection.close()

        incremental = run_sync(config, mock, "3", "--mode", "incremental", "--no-ledger")
        require(incremental.returncode == 0, f"incremental run should succeed: {incremental.stderr} {incremental.stdout}")
        incremental_result = json.loads(incremental.stdout)
        require(incremental_result["mode"] == "incremental", "incremental receipt must identify its mode")
        require(incremental_result["messages_inserted"] == 5, "incremental scan must capture self-sent and four event-only histories")
        require(incremental_result["messages_hidden"] == 0, "incremental scan must never hide older messages")
        require(incremental_result["stale_runs_recovered"] == 1, "stale running receipt must become interrupted")
        connection = sqlite3.connect(database)
        require(connection.execute("SELECT COUNT(*) FROM messages WHERE current_visible=1").fetchone()[0] == 8, "incremental scan must preserve prior visible messages and add four histories")
        require(connection.execute("SELECT COUNT(*) FROM semantic_queue").fetchone()[0] == 11, "incremental inserts must be queued exactly once")
        require(connection.execute("SELECT COUNT(*) FROM conversation_scans WHERE scan_mode='incremental'").fetchone()[0] == 3, "incremental scans must be labeled")
        require(connection.execute("SELECT COUNT(*) FROM conversation_scans WHERE scan_mode='historical_backfill' AND status='complete'").fetchone()[0] == 4, "four event-only conversations must receive full historical backfill")
        require(connection.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id LIKE 'conv-event-%' AND last_complete_scan_at IS NOT NULL").fetchone()[0] == 4, "event-only conversations must become historically complete")
        require(connection.execute("SELECT status FROM sync_runs WHERE run_id='stale-run'").fetchone()[0] == "interrupted", "stale run status must be explicit")
        connection.close()
        checks += 11

        # A listed conversation first read only by window (and a brand-new listed group)
        # has no full-history receipt; the next window must read it from the beginning.
        connection = sqlite3.connect(database)
        connection.execute(
            """
            INSERT INTO conversations(
              conversation_id,conversation_name,first_seen_at,last_seen_at,last_scan_at,
              last_complete_scan_at,last_scan_status,visible_message_count
            ) VALUES('conv-listed-old','已列出未补历史群',?,?,?,NULL,'complete',0)
            """,
            ("2020-01-02T09:00:00+08:00", "2020-01-02T09:00:00+08:00", "2020-01-02T09:00:00+08:00"),
        )
        connection.commit()
        connection.close()
        listed = run_sync(config, mock, "4", "--mode", "incremental", "--no-ledger")
        require(listed.returncode == 0, f"listed backfill run should succeed: {listed.stderr} {listed.stdout}")
        require(json.loads(listed.stdout)["messages_inserted"] == 2, "listed never-complete conversations must capture their history")
        connection = sqlite3.connect(database)
        backfills = dict(connection.execute(
            "SELECT conversation_id,COUNT(*) FROM conversation_scans WHERE scan_mode='historical_backfill' AND status='complete' AND conversation_id LIKE 'conv-listed-%' GROUP BY conversation_id"
        ).fetchall())
        require(backfills == {"conv-listed-old": 1, "conv-listed-new": 1}, "listed never-complete conversations must be backfilled once each")
        latest_window = connection.execute("SELECT window_id FROM chat_reconcile_windows ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()[0]
        targets = {row[0]: (row[1], row[2]) for row in connection.execute(
            "SELECT conversation_id,historical_backfill,range_start FROM chat_reconcile_targets WHERE window_id=?", (latest_window,)
        )}
        require(targets["conv-listed-old"] == (1, "2000-01-01T00:00:00+08:00"), "listed never-complete target must start from the beginning")
        require(targets["conv-listed-new"] == (1, "2000-01-01T00:00:00+08:00"), "brand-new listed target must start from the beginning")
        require(targets["conv-a"][0] == 0 and targets["conv-a"][1] != "2000-01-01T00:00:00+08:00", "historically complete conversation keeps its bounded window")
        require(connection.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id LIKE 'conv-listed-%' AND last_complete_scan_at IS NOT NULL").fetchone()[0] == 2, "backfilled listed conversations must become historically complete")
        connection.close()
        again = run_sync(config, mock, "4", "--mode", "incremental", "--no-ledger")
        require(again.returncode == 0, f"follow-up window should succeed: {again.stderr} {again.stdout}")
        require(json.loads(again.stdout)["messages_inserted"] == 0, "completed history must not be read from the beginning again")
        connection = sqlite3.connect(database)
        require(connection.execute("SELECT COUNT(*) FROM conversation_scans WHERE scan_mode='historical_backfill' AND conversation_id LIKE 'conv-listed-%'").fetchone()[0] == 2, "backfill must not repeat once complete")
        connection.close()
        checks += 9

        status_path = external_root / "dingtalk-chat" / "state" / "status.json"
        incremental_status_path = external_root / "dingtalk-chat" / "state" / "incremental_status.json"
        log_path = external_root / "dingtalk-chat" / "logs" / "sync.jsonl"
        ledger_path = memory_root / "99_规则与模板" / "钉钉聊天同步台账.md"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        require(status["database_quick_check"] == "ok", "status receipt must include quick_check")
        require(status["status"] == "success", "latest status receipt must be success")
        require(json.loads(incremental_status_path.read_text(encoding="utf-8"))["mode"] == "incremental", "incremental receipt must be separate")
        require("hunter2" not in log_path.read_text(encoding="utf-8"), "logs must not contain message secrets")
        require("conv-a" not in log_path.read_text(encoding="utf-8"), "logs must not contain conversation IDs")
        require("hunter2" not in ledger_path.read_text(encoding="utf-8"), "Obsidian ledger must not contain chat text")
        require(stat.S_IMODE(database.stat().st_mode) == 0o600, "database permissions must be 0600")
        require(stat.S_IMODE(database.parent.stat().st_mode) == 0o700, "database directory permissions must be 0700")
        checks += 8

    print(json.dumps({"verdict": "pass", "checks": checks}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
