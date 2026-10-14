#!/usr/bin/env python3
"""Persistent operational conversations. Only explicit, fenced buttons execute.

Chat has no tool or business-memory write path. All operation receipts live next
to the existing action ledger; retrying an HTTP submission reuses its identity.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import dingtalk_sync_v2 as syncv2
import business_sync_state as syncstate
import email_sync

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "state" / "alert-workbench.lock"
UUID_RE = re.compile(r"[0-9a-fA-F-]{36}")
SOURCE_LABELS = {"dingtalk_mail": "钉钉邮件", "dingtalk_chat": "钉钉聊天", "dingtalk_minutes": "钉钉 AI 听记", "win11": "Win11", "memory_core": "记忆核心", "system": "同步中心"}


class AlertError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
      CREATE TABLE IF NOT EXISTS alert_threads(
        thread_id TEXT PRIMARY KEY, draft TEXT NOT NULL DEFAULT '',
        draft_revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS alert_messages(
        message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, alert_id TEXT NOT NULL,
        author TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
        reply_to TEXT, request_hash TEXT NOT NULL, ai_profile_json TEXT NOT NULL,
        run_id TEXT, error_code TEXT, worker_pid INTEGER,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE UNIQUE INDEX IF NOT EXISTS alert_answer_once ON alert_messages(reply_to)
        WHERE author='assistant';
      CREATE TABLE IF NOT EXISTS alert_message_retries(
        retry_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, run_id TEXT NOT NULL, created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS alert_action_receipts(
        action_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, alert_id TEXT NOT NULL,
        action TEXT NOT NULL, revision TEXT NOT NULL, request_hash TEXT NOT NULL,
        state TEXT NOT NULL, run_id TEXT, result_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
    """)


def digest(value: Any) -> str:
    return hashlib.sha256(syncv2.canonical_json(value).encode()).hexdigest()


def require_id(value: Any) -> str:
    value = str(value or "")
    if not UUID_RE.fullmatch(value):
        raise AlertError("submission_id_invalid", "提交编号无效，请重新打开窗口。")
    return value


def raw_alert(alert_id: str) -> dict[str, Any]:
    require_id(alert_id)
    connection = syncv2.connect_readonly()
    try:
        row = connection.execute("SELECT * FROM sync_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        if not row:
            # Recurrences rotate the old alert ID. Keep historic links usable.
            try:
                legacy = connection.execute("SELECT l.alert_snapshot_json,i.status,i.resolved_at FROM issue_alert_links l JOIN operational_issues i ON i.issue_id=l.issue_id WHERE l.alert_id=? ORDER BY i.status DESC LIMIT 1", (alert_id,)).fetchone()
            except sqlite3.OperationalError:
                legacy = None
            if legacy:
                value = json.loads(legacy["alert_snapshot_json"])
                value["status"] = "resolved" if legacy["status"] == "closed" else "open"
                value["resolved_at"] = legacy["resolved_at"]
                return value
            raise AlertError("alert_missing", "这条告警不存在；已保存的其他窗口不受影响。", 404)
        return dict(row)
    finally:
        connection.close()


def group_key(row: dict[str, Any]) -> str:
    category = str(row.get("category") or "")
    family = "source_health" if category in {"sync_failure", "sync_block", "sync_blocked", "persistent_block", "blocked"} else category
    return str(row.get("source_id")) + ":" + family


def conversation_thread(identifier: str) -> str:
    if identifier.startswith(("issue-", "group-")):
        import operational_workbench as workbench
        if identifier.startswith("group-"):
            workbench.raw_group(identifier)
            return "group:" + identifier
        workbench.raw_issue(identifier)
        return "issue:" + identifier
    return group_key(raw_alert(identifier))


def safe_safety(config: dict[str, Any]) -> dict[str, Any]:
    try:
        return email_sync.read_safety_status(config)
    except (OSError, KeyError, email_sync.EmailSyncError):
        return {"server_side_pass": False, "cross_device_confirmed": False, "binding_valid": False, "unavailable": True}


def snapshot(config: dict[str, Any], raw: dict[str, Any], *, maintenance: bool = False,
             safety: dict[str, Any] | None = None, source: dict[str, Any] | None = None) -> dict[str, Any]:
    source = source if source is not None else (syncstate.source(raw["source_id"]) if raw["source_id"] in syncstate.SOURCE_IDS else {})
    safety = safety if safety is not None else (safe_safety(config) if raw["source_id"] == "dingtalk_mail" else {})
    value = syncstate.public_alert(raw, maintenance_active=maintenance, source_state=source)
    valid_safety = safety.get("server_side_pass") is True and safety.get("cross_device_confirmed") is True
    safety_required = raw["source_id"] == "dingtalk_mail" and str(source.get("error_code") or "").startswith("read_safety") and not valid_safety
    value["action_required"] = "email_read_safety_confirmation" if safety_required and raw["status"] == "open" else None
    value["action_label"] = "核对另一台设备上的未读状态" if value["action_required"] else None
    value["requirement_history"] = []
    if valid_safety and ("安全确认" in str(raw.get("title")) or "安全凭证" in str(raw.get("detail"))):
        value["requirement_history"] = [{"title": str(raw.get("title")), "state": "superseded", "reason": "当前邮件安全凭证有效，原人工确认要求已撤销。", "at": safety.get("confirmed_at")}]
        value["title"] = "钉钉邮件同步状态"
        value["detail"] = "安全凭证已有效。" + ("邮件同步仍有故障，请查看当前原因。" if source.get("error_code") else "旧的人工确认要求已撤销。")
    value["thread_id"] = group_key(raw)
    value["source_status"] = source.get("status", "unknown")
    value["source_error_code"] = source.get("error_code")
    value["last_success"] = source.get("last_success")
    value["next_check"] = source.get("next_due")
    value["mail_safety"] = safety if raw["source_id"] == "dingtalk_mail" else None
    value["user_state"] = "acknowledged" if raw.get("acknowledged_at") else "unhandled"
    value["display_state"] = "resolved" if raw["status"] == "resolved" else "open"
    value["revision"] = digest({
        "id": raw["alert_id"], "status": raw["status"], "first_seen": raw.get("first_seen"),
        "resolved_at": raw.get("resolved_at"), "ack": raw.get("acknowledged_at"),
        "source_status": source.get("status"), "error": source.get("error_code"),
        "title": raw.get("title"), "detail": raw.get("detail"),
        "safety": safety, "maintenance": maintenance,
        "repair_status": raw.get("repair_status"), "repair_error": raw.get("repair_error_code"),
        "macos_status": raw.get("macos_status"), "notification_revision": raw.get("notification_revision"),
    })
    return value


def listing(config: dict[str, Any], *, maintenance: bool = False, limit: int = 100,
            status: str | None = None) -> dict[str, Any]:
    safety = safe_safety(config)
    sources = {name: syncstate.source(name) for name in syncstate.SOURCE_IDS}
    connection = syncv2.connect_readonly()
    try:
        raw_rows = [dict(row) for row in connection.execute("SELECT * FROM sync_alerts ORDER BY status='open' DESC,last_seen DESC")]
    finally:
        connection.close()
    rows = [snapshot(config, row, maintenance=maintenance, safety=safety if row["source_id"] == "dingtalk_mail" else {}, source=sources.get(row["source_id"], {}))
            for row in raw_rows]
    import operational_workbench as workbench
    import codex_handoff
    linked = workbench.alert_links()
    for row in rows:
        issues = row["issues"] = linked.get(row["alert_id"], [])
        if row["status"] == "open" and issues and all(
            item["status"] == "closed" and item.get("resolution_kind") in {"recovered", "disposed", "superseded", "media_resolved", "media_separated"}
            for item in issues
        ):
            # The user's disposition ends this alert's workload, not its source health.
            row["source_alert_status"] = row["status"]
            row["status"] = row["display_state"] = "resolved"
            row["resolved_at"] = max((item.get("resolved_at") or "" for item in issues), default="") or None
            row["resolution_kind"] = "disposed" if any(item.get("resolution_kind") in {"disposed", "media_resolved", "media_separated"} for item in issues) else "recovered"
            row["effective_state"] = "closed"
    codex_handoff.annotate_items(rows)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["status"] == "open":
            groups.setdefault(row["thread_id"], []).append(row)
    for members in groups.values():
        primary = sorted(members, key=lambda item: (item["effective_state"] == "critical", item.get("last_seen") or ""), reverse=True)[0]
        for row in members:
            row["group_primary_id"] = primary["alert_id"]
            row["group_member_ids"] = [item["alert_id"] for item in members]
    current = [row for row in rows if row["status"] == "open" and row.get("group_primary_id") == row["alert_id"]]
    selected = [row for row in rows if status is None or row["status"] == status]
    bounded = max(1, min(1000, limit))
    return {"schema_version": 3, "generated_at": syncv2.now_iso(), "total": len(selected),
            "has_more": len(selected) > bounded,
            "open_count": sum(row["effective_state"] in {"critical", "warning"} for row in current),
            "open_total": len(current), "maintenance_pending_count": sum(row["effective_state"] == "maintenance_pending" for row in current),
            "reporting_failed_count": sum((row.get("delivery") or {}).get("summary") == "failed" for row in current),
            "alerts": selected[:bounded]}


def available_actions(detail: dict[str, Any], control: dict[str, Any] | None = None, *, maintenance: bool = False) -> list[dict[str, Any]]:
    actions = [{"id": "recheck", "label": "重新检查", "available": not maintenance, "reason": "维护期间暂不可执行" if maintenance else "读取当前运行状态和凭证，不会把检查成功当成故障恢复。"}]
    if detail["status"] != "open":
        return actions
    if not detail.get("acknowledged_at"):
        actions.append({"id": "acknowledge", "label": "已知悉，继续监测", "available": not maintenance, "reason": "保留故障状态，后台继续检查。"})
    source_id = detail["source_id"]
    import local_sync_repair
    if local_sync_repair.eligible(detail):
        busy = detail.get("repair_status") in {"queued", "running"}
        actions.append({"id": "repair_local", "label": "执行安全本地修复", "available": not maintenance and not busy,
                        "reason": "仅恢复安全回退、重建索引和完整校验；内容冲突保留，不调用模型。"})
    if source_id in syncstate.SOURCE_IDS:
        selected = next((item.get("control") or {} for item in (control or {}).get("sources", []) if item.get("id") == source_id), {})
        enabled = selected.get("available") is True and not selected.get("running") and not maintenance
        actions.append({"id": "retry_source", "label": "重试该来源", "available": enabled, "reason": str(selected.get("reason") or ("按当前安全条件启动一次同步。" if enabled else "正在核对是否满足同步条件。"))})
    if source_id == "dingtalk_chat":
        code = " ".join(str(detail.get(key) or "") for key in ("error_code", "source_error_code", "repair_error_code"))
        if any(value in code for value in ("recovery_conflict", "rollback_concurrent", "rollback_rollback_concurrent")):
            actions.append({"id": "preserve_current", "label": "保留现版，只补缺项", "available": not maintenance,
                            "reason": "仅核对原冲突批次的来源并补缺；不覆盖现版，不移除旧失败记录。"})
        if detail.get("category") == "listener_failure":
            actions.append({"id": "manual_listener_acceptance", "label": "修正原因后验收一次", "available": False,
                            "reason": "需先取得原因已修正的证据；原重试额度不会重置。"})
    if not detail.get("acknowledged_at") and detail.get("macos_status") in {"permission_off", "permission_required"}:
        actions.append({"id": "recheck_macos", "label": "重新检查 Mac 通知", "available": not maintenance,
                        "reason": "仅恢复仍未解决且明确未发出的通知；送达不确定的通知不补发。"})
    if source_id == "dingtalk_mail":
        actions.append({"id": "attachments", "label": "查看待处理附件", "available": not maintenance, "reason": "查看保留的附件名称及待处理原因。"})
        safety = detail.get("mail_safety") or {}
        if detail.get("action_required") and safety.get("server_side_pass") is True:
            actions.append({"id": "confirm_mail", "label": "核对邮件安全状态", "available": not maintenance, "reason": "需你在另一台设备核对测试邮件是否仍未读；不会默认选择。"})
    return actions


def detail(config: dict[str, Any], alert_id: str, *, maintenance: bool = False,
           control: dict[str, Any] | None = None) -> dict[str, Any]:
    result = snapshot(config, raw_alert(alert_id), maintenance=maintenance)
    result["actions"] = available_actions(result, control, maintenance=maintenance)
    if result.get("category") == "listener_failure" and config.get("external_root") and config.get("dingtalk_automation"):
        import listener_manual_acceptance
        gate = listener_manual_acceptance.gate(config)
        for item in result["actions"]:
            if item["id"] == "manual_listener_acceptance":
                item["available"] = gate.get("available") is True and not maintenance
                item["reason"] = gate.get("message") or "原因修正证据有效；仅执行一次独立人工验收。"
    result["checked_at"] = syncv2.now_iso()
    connection = syncv2.connect_readonly()
    try:
        rows = connection.execute("SELECT * FROM alert_action_receipts WHERE thread_id=? ORDER BY created_at DESC LIMIT 30", (result["thread_id"],)).fetchall()
        result["action_history"] = [public_receipt(connection, row) for row in rows]
    finally:
        connection.close()
    return result


def public_receipt(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    result = {key: row[key] for key in ("action_id", "action", "state", "run_id", "created_at", "updated_at")}
    result["result"] = json.loads(row["result_json"])
    if row["run_id"]:
        run = connection.execute("SELECT status,error_code,updated_at FROM action_runs WHERE run_id=?", (row["run_id"],)).fetchone()
        if run:
            result["execution"] = dict(run)
            if run["status"] != "running":
                result["state"] = "completed" if run["status"] in {"success", "noop"} else "failed"
    return result


def _thread(connection: sqlite3.Connection, thread_id: str) -> None:
    connection.execute("INSERT OR IGNORE INTO alert_threads(thread_id,updated_at) VALUES(?,?)", (thread_id, syncv2.now_iso()))


def conversation(alert_id: str) -> dict[str, Any]:
    import case_conversation
    thread_id = conversation_thread(alert_id)
    connection = syncv2.connect_readonly()
    try:
        thread = connection.execute("SELECT * FROM alert_threads WHERE thread_id=?", (thread_id,)).fetchone()
        rows = connection.execute("SELECT message_id,author,content,status,reply_to,error_code,created_at,updated_at FROM alert_messages WHERE thread_id=? ORDER BY created_at,rowid", (thread_id,)).fetchall()
        return {"alert_id": alert_id, "thread_id": thread_id, "ai_configuration": case_conversation.ai_configuration(), "messages": [dict(row) for row in rows],
                "draft": str(thread["draft"] if thread else ""), "draft_revision": int(thread["draft_revision"] if thread else 0),
                "updated_at": syncv2.now_iso(), "active_message": next((dict(row) for row in rows if row["author"] == "user" and row["status"] in {"pending", "running"}), None)}
    finally:
        connection.close()


def save_draft(alert_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"content", "revision"} or not isinstance(payload["content"], str) or len(payload["content"]) > 4000:
        raise AlertError("draft_invalid", "草稿字段无效或超过 4,000 字。")
    thread_id = conversation_thread(alert_id)
    connection = syncv2.connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _thread(connection, thread_id)
            row = connection.execute("SELECT * FROM alert_threads WHERE thread_id=?", (thread_id,)).fetchone()
            if row["draft"] != payload["content"]:
                if int(payload["revision"]) != row["draft_revision"]:
                    raise AlertError("draft_conflict", "另一窗口已有新草稿；你的本地输入仍保留。", 409)
                connection.execute("UPDATE alert_threads SET draft=?,draft_revision=draft_revision+1,updated_at=? WHERE thread_id=?", (payload["content"], syncv2.now_iso(), thread_id))
            updated = connection.execute("SELECT draft,draft_revision FROM alert_threads WHERE thread_id=?", (thread_id,)).fetchone()
        return dict(updated)
    finally:
        connection.close()


def spawn_worker(config_path: Path, message_id: str) -> None:
    import dingtalk_memory_automation as memory
    config = memory.load_config(config_path)
    if not (config.get("case_workbench") or {}).get("ai_enabled", True):
        raise AlertError("case_ai_disabled", "ChatGPT 暂停，消息已保存。")
    subprocess.Popen([sys.executable, str(ROOT / "bin" / "dingtalk_decision_center.py"), "--config", str(config_path), "--answer-alert-message", message_id], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def submit_message(config_path: Path, alert_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    import case_conversation
    import dingtalk_memory_automation as memory
    if set(payload) != {"client_message_id", "content", "ai_profile"}:
        raise AlertError("message_contract", "消息字段无效。")
    message_id = require_id(payload["client_message_id"])
    content = str(payload["content"]).strip()
    if not content or len(content) > 4000:
        raise AlertError("message_invalid", "请输入 1 至 4,000 字的内容。")
    profile = case_conversation._validated_ai_profile(payload["ai_profile"])
    thread_id = conversation_thread(alert_id)
    request_hash = digest({"alert_id": alert_id, "content": content, "profile": profile})
    connection = syncv2.connect()
    run_id, replay = "", False
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT * FROM alert_messages WHERE message_id=?", (message_id,)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise AlertError("message_conflict", "此提交编号已绑定另一条消息。", 409)
                return {"saved": True, "message_id": message_id, "status": old["status"], "idempotent_replay": True}
            if connection.execute("SELECT 1 FROM alert_messages WHERE thread_id=? AND author='user' AND status IN ('pending','running')", (thread_id,)).fetchone():
                raise AlertError("answer_in_progress", "ChatGPT 正在回答，输入草稿已保留。", 409)
            _thread(connection, thread_id)
            run_id, stamp = str(uuid.uuid4()), syncv2.now_iso()
            connection.execute("INSERT INTO action_runs(run_id,action_type,trigger,status,started_at,updated_at) VALUES(?,'alert_conversation','manual','running',?,?)", (run_id, stamp, stamp))
            connection.execute("INSERT INTO alert_messages(message_id,thread_id,alert_id,author,content,status,request_hash,ai_profile_json,run_id,created_at,updated_at) VALUES(?,?,?,'user',?,'pending',?,?,?,?,?)", (message_id, thread_id, alert_id, content, request_hash, json.dumps(profile), run_id, stamp, stamp))
            connection.execute("UPDATE alert_threads SET draft='',draft_revision=draft_revision+1,updated_at=? WHERE thread_id=?", (stamp, thread_id))
    finally:
        connection.close()
    try:
        config = memory.load_config(config_path)
        if not (config.get("case_workbench") or {}).get("ai_enabled", True):
            raise AlertError("case_ai_disabled", "ChatGPT 暂停，回复已保存。")
        spawn_worker(config_path, message_id)
    except Exception as exc:
        fail_message(message_id, str(getattr(exc, "code", "alert_worker_unavailable")))
        return {"saved": True, "message_id": message_id, "status": "failed", "idempotent_replay": False}
    return {"saved": True, "message_id": message_id, "status": "pending", "idempotent_replay": False}


def retry_message(config_path: Path, alert_id: str, message_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"retry_id"}:
        raise AlertError("retry_invalid", "重试编号无效。")
    retry_id = require_id(payload["retry_id"])
    thread_id = conversation_thread(alert_id)
    connection = syncv2.connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute("SELECT * FROM alert_message_retries WHERE retry_id=?", (retry_id,)).fetchone()
            if receipt:
                if receipt["message_id"] != message_id:
                    raise AlertError("retry_conflict", "此重试编号已绑定其他消息。", 409)
                return {"saved": True, "message_id": message_id, "run_id": receipt["run_id"], "idempotent_replay": True}
            message = connection.execute("SELECT * FROM alert_messages WHERE message_id=? AND thread_id=? AND author='user'", (message_id, thread_id)).fetchone()
            if not message or message["status"] != "failed":
                raise AlertError("retry_invalid", "只有未完成的回答可以重试。", 409)
            if connection.execute("SELECT 1 FROM alert_messages WHERE thread_id=? AND status IN ('pending','running')", (thread_id,)).fetchone():
                raise AlertError("answer_in_progress", "请等待当前回答完成。", 409)
            run_id, stamp = str(uuid.uuid4()), syncv2.now_iso()
            connection.execute("INSERT INTO alert_message_retries VALUES(?,?,?,?)", (retry_id, message_id, run_id, stamp))
            connection.execute("INSERT INTO action_runs(run_id,action_type,trigger,status,started_at,updated_at) VALUES(?,'alert_conversation','manual','running',?,?)", (run_id, stamp, stamp))
            profile = case_conversation.retry_ai_profile(json.loads(message["ai_profile_json"]))
            connection.execute("UPDATE alert_messages SET status='pending',run_id=?,worker_pid=NULL,error_code=NULL,ai_profile_json=?,updated_at=? WHERE message_id=?", (run_id, json.dumps(profile), stamp, message_id))
    finally:
        connection.close()
    try:
        spawn_worker(config_path, message_id)
    except Exception as exc:
        fail_message(message_id, str(getattr(exc, "code", "alert_worker_unavailable")))
    return {"saved": True, "message_id": message_id, "run_id": run_id, "idempotent_replay": False}


def fail_message(message_id: str, code: str) -> None:
    connection = syncv2.connect()
    try:
        with connection:
            row = connection.execute("SELECT run_id FROM alert_messages WHERE message_id=?", (message_id,)).fetchone()
            connection.execute("UPDATE alert_messages SET status='failed',error_code=?,updated_at=? WHERE message_id=? AND status IN ('pending','running')", (code[:100], syncv2.now_iso(), message_id))
        if row and row["run_id"]:
            syncv2.finish_action_run(row["run_id"], "failed", error_code=code[:100])
    finally:
        connection.close()


def answer_message(config: dict[str, Any], message_id: str) -> dict[str, Any]:
    import case_conversation
    connection = syncv2.connect()
    try:
        with connection:
            changed = connection.execute("UPDATE alert_messages SET status='running',worker_pid=?,updated_at=? WHERE message_id=? AND status='pending'", (os.getpid(), syncv2.now_iso(), message_id)).rowcount
            if not changed:
                return {"status": "already_claimed"}
            row = connection.execute("SELECT * FROM alert_messages WHERE message_id=?", (message_id,)).fetchone()
        if row["alert_id"].startswith(("issue-", "group-")):
            import operational_workbench
            current = (
                operational_workbench.group_chat_context(config, row["alert_id"])
                if row["alert_id"].startswith("group-")
                else operational_workbench.chat_context(config, row["alert_id"])
            )
        else:
            current = snapshot(config, raw_alert(row["alert_id"]))
        history = conversation(row["alert_id"])["messages"][-24:]
        # Only operational status and this alert's conversation are provided.
        context = {"alert": current if row["alert_id"].startswith(("issue-", "group-")) else {key: current.get(key) for key in ("title", "detail", "status", "source_status", "source_error_code", "last_success", "next_check", "user_state")},
                   "recent_messages": history, "ai_profile": json.loads(row["ai_profile_json"]),
                   "execution_policy": "仅讨论。任何操作都由用户另行点击后台提供的按钮。"}
        response = case_conversation.invoke_case_ai(config, context, row["run_id"], prompt_path=ROOT / "prompts" / "alert_conversation.md", schema_path=ROOT / "schemas" / "alert_conversation.schema.json")
        with connection:
            stamp = syncv2.now_iso()
            connection.execute("INSERT INTO alert_messages(message_id,thread_id,alert_id,author,content,status,reply_to,request_hash,ai_profile_json,created_at,updated_at) VALUES(?,?,?,'assistant',?,'completed',?,'','{}',?,?)", (str(uuid.uuid4()), row["thread_id"], row["alert_id"], response["answer"], message_id, stamp, stamp))
            connection.execute("UPDATE alert_messages SET status='completed',error_code=NULL,updated_at=? WHERE message_id=?", (stamp, message_id))
        syncv2.finish_action_run(row["run_id"], "success", result={"answered": 1, "executed": 0})
        return {"status": "success", "executed": False}
    except Exception as exc:
        fail_message(message_id, str(getattr(exc, "code", "alert_answer_failed")))
        return {"status": "failed", "saved": True}
    finally:
        connection.close()


def pending_attachments(config: dict[str, Any]) -> list[dict[str, Any]]:
    import operational_workbench
    path = email_sync.runtime_paths(config)["database"]
    if not path.is_file():
        raise AlertError("mail_state_unavailable", "附件状态暂时无法读取；不会当作没有待办。", 503)
    connection = email_sync.connect_database_readonly(config)
    try:
        rows = operational_workbench.pending_attachment_rows(connection)
        return [{key: str(row.get(key) or "")[:240] for key in ("original_name", "status", "risk", "extract_status", "last_error_code")} for row in rows[:100]]
    finally:
        connection.close()


def execute_action(app: Any, alert_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    import dingtalk_decision_center as center
    if not {"action_id", "action", "revision"}.issubset(payload) or set(payload) - {"action_id", "action", "revision", "still_unread"}:
        raise AlertError("action_contract", "处理请求字段无效。")
    action_id, action = require_id(payload["action_id"]), str(payload["action"])
    request_hash = digest({"alert_id": alert_id, "payload": payload})
    with syncv2.file_lock(LOCK, blocking=True):
        connection = syncv2.connect()
        try:
            old = connection.execute("SELECT * FROM alert_action_receipts WHERE action_id=?", (action_id,)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise AlertError("action_conflict", "该提交编号已绑定另一项操作。", 409)
                if old["state"] == "prepared" and old["run_id"]:
                    known = connection.execute("SELECT status FROM action_runs WHERE run_id=?", (old["run_id"],)).fetchone()
                    if known:
                        with connection:
                            connection.execute("UPDATE alert_action_receipts SET state='running',updated_at=? WHERE action_id=?", (syncv2.now_iso(), action_id))
                        restored = connection.execute("SELECT * FROM alert_action_receipts WHERE action_id=?", (action_id,)).fetchone()
                        return {**public_receipt(connection, restored), "saved": True, "idempotent_replay": True}
                if old["state"] != "prepared":
                    return {**public_receipt(connection, old), "idempotent_replay": True, "saved": True}
            control_error = None
            try:
                control = center.current_control_state(app)
            except center.DecisionError as exc:
                control, control_error = None, exc
            current = detail(app.config, alert_id, maintenance=center.maintenance_active(), control=control)
            if str(payload["revision"]) != current["revision"]:
                raise AlertError("alert_stale", "告警或安全凭证已经变化，旧按钮已失效。请查看刷新后的处理选项。", 409)
            choice = next((item for item in current["actions"] if item["id"] == action), None)
            if not choice or not choice["available"]:
                raise AlertError("action_unavailable", str((choice or {}).get("reason") or "当前状态已不再提供此操作。"), 409)
            if action == "confirm_mail" and not isinstance(payload.get("still_unread"), bool):
                raise AlertError("choice_required", "请明确选择另一台设备上测试邮件的未读状态。")
            if not old:
                run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "business-alert-action:" + action_id)) if action in {"retry_source", "repair_local", "preserve_current", "manual_listener_acceptance"} else None
                with connection:
                    stamp = syncv2.now_iso()
                    connection.execute("INSERT INTO alert_action_receipts(action_id,thread_id,alert_id,action,revision,request_hash,state,run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,'prepared',?,?,?)", (action_id, current["thread_id"], alert_id, action, current["revision"], request_hash, run_id, stamp, stamp))
            else:
                run_id = old["run_id"]
            try:
                if action == "acknowledge":
                    for related in syncstate.alerts(status="open", limit=200):
                        if group_key(related) == current["thread_id"]:
                            stamp = syncv2.now_iso()
                            connection.execute(
                                "UPDATE sync_alerts SET acknowledged_at=?,next_reminder=NULL,updated_at=? WHERE alert_id=?",
                                (stamp, stamp, related["alert_id"]),
                            )
                    result = {"message": "已知悉，后台继续监测；故障尚未因此恢复。", "fault_cleared": False}
                elif action == "recheck":
                    if control_error:
                        raise control_error
                    result = {"message": "当前状态已重新检查。", "checked_at": syncv2.now_iso(), "source_status": current["source_status"], "error_code": current["source_error_code"], "fault_cleared": current["status"] == "resolved"}
                elif action == "attachments":
                    result = {"message": "附件已保留，以下为待处理清单。", "attachments": pending_attachments(app.config)}
                elif action == "confirm_mail":
                    result = center.confirm_email_read_safety(app.config, payload["still_unread"])
                elif action == "retry_source":
                    result = center.control_action_gate(center.current_control_state(app), current["source_id"]) or center.start_control_action(app.config_path, current["source_id"], requested_run_id=run_id)
                    run_id = result.get("run_id")
                elif action == "repair_local":
                    import local_sync_repair
                    result = local_sync_repair.start(app.config_path, alert_id, requested_run_id=run_id)
                    run_id = result.get("run_id")
                elif action in {"preserve_current", "manual_listener_acceptance"}:
                    import bounded_repair_actions
                    result = bounded_repair_actions.start(app.config_path, action, alert_id, action_id, run_id)
                elif action == "recheck_macos":
                    import business_sync_supervisor
                    result = business_sync_supervisor.recheck_macos_notifications(alert_id=alert_id)
                else:
                    raise AlertError("action_invalid", "操作无效。")
                state = "running" if result.get("status") in {"started", "already_running"} else "failed" if result.get("status") in {"blocked", "failed"} else "completed"
            except Exception as exc:
                state, result = "failed", {"error_code": str(getattr(exc, "code", "action_failed")), "message": str(getattr(exc, "message", "操作未完成；原始记录已保留，请重新检查。"))}
            with connection:
                connection.execute("UPDATE alert_action_receipts SET state=?,run_id=?,result_json=?,updated_at=? WHERE action_id=?", (state, run_id, json.dumps(result, ensure_ascii=False), syncv2.now_iso(), action_id))
            receipt = connection.execute("SELECT * FROM alert_action_receipts WHERE action_id=?", (action_id,)).fetchone()
            return {**public_receipt(connection, receipt), "saved": True, "idempotent_replay": False}
        finally:
            connection.close()


def recover_interrupted() -> int:
    connection = syncv2.connect_readonly()
    try:
        rows = connection.execute("SELECT message_id,worker_pid FROM alert_messages WHERE author='user' AND status IN ('pending','running')").fetchall()
    finally:
        connection.close()
    count = 0
    for row in rows:
        try:
            if row["worker_pid"]:
                os.kill(int(row["worker_pid"]), 0)
                continue
        except (ProcessLookupError, PermissionError):
            pass
        fail_message(row["message_id"], "worker_interrupted")
        count += 1
    return count
