#!/usr/bin/env python3
"""Local-only decision center for governed DingTalk memory candidates."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import secrets
import sqlite3
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import dingtalk_memory_automation as memory
import dingtalk_review_policy as review_policy
import dingtalk_sync_v2 as syncv2
import business_sync_state as syncstate
import email_sync
import content_registry
import business_page_router as business_router
import claim_reconciliation as reconciliation
import case_conversation
import workbench_attention
import alert_workbench
import operational_workbench
import codex_handoff
import logging
from logging.handlers import RotatingFileHandler
import traceback
from system_dashboard_status import DashboardPaths, SystemDashboard


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "decision-center"
CONFIG_PATH = ROOT / "config.json"
MAINTENANCE_STATE = ROOT / "state" / "maintenance-mode.json"
MAINTENANCE_LOCK = ROOT / "state" / "maintenance-mode.lock"
MEMORY_ID_RE = re.compile(r"BUSINESS-[0-9]{8}-[0-9]{3}")
PREVIEW_TTL_MINUTES = 10
ORCHESTRATOR = ROOT / "bin" / "dingtalk_sync_orchestrator.py"
SYNC_CONTROLLER = ROOT / "bin" / "business_sync_controller.py"
CONTROL_LOCK = ROOT / "state" / "sync-center-control.lock"
CONTROL_START_LOCK = ROOT / "state" / "sync-center-start.lock"
REVIEW_RUNNER = ROOT / "bin" / "dingtalk_idle_review.py"
MINUTES_ROOT = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync'))
MINUTES_SYNC_STATE = MINUTES_ROOT / ".state" / "sync-state.json"
MINUTES_REMOTE_INVENTORY = MINUTES_ROOT / ".state" / "remote-inventory.json"
REVIEW_LEDGER = MINUTES_ROOT / "核对" / ".state" / "review-ledger.json"
MINUTES_NOTES_ROOT = MINUTES_ROOT / "听记"
MINUTES_RAW_ROOT = MINUTES_ROOT / ".raw"
QUESTION_PROMPT = ROOT / "prompts" / "dingtalk_decision_question.md"
QUESTION_SCHEMA = ROOT / "schemas" / "dingtalk_decision_question.schema.json"
PREFERENCES_KEY = "sync-center:preferences"
QUESTION_MAX_CHARS = 4000


class DecisionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return memory.load_config(path)


def confirm_email_read_safety(config: dict[str, Any], still_unread: bool) -> dict[str, Any]:
    try:
        result = email_sync.confirm_read_safety(config, still_unread)
    except email_sync.EmailSyncError as exc:
        raise DecisionError(str(exc.code), "安全确认尚未通过，请先完成另一台设备上的未读状态核对。") from exc
    current = datetime.now().astimezone()
    if result.get("cross_device_confirmed") is True:
        syncstate.update_source(
            "dingtalk_mail", status="pending", next_due=syncstate.now_iso(current),
            blocked_since=None, error_code=None,
        )
        return {
            "status": "confirmed",
            "verdict": "pass",
            "next_action": "后台将在下一轮自动续跑；只有新成功证据才会关闭原故障。",
        }
    syncstate.update_source(
        "dingtalk_mail", status="manual_required",
        next_due=syncstate.now_iso(current + timedelta(minutes=1)),
        blocked_since=syncstate.now_iso(current), error_code="read_safety_failed",
    )
    syncstate.open_alert(
        "dingtalk_mail", "sync_failure", "钉钉邮件安全确认未通过",
        "另一台设备上的未读状态已经变化；邮件同步继续安全暂停，不会自动代办。",
        severity="error", attempts=int(syncstate.source("dingtalk_mail").get("attempt_count") or 0),
        at=current,
    )
    return {
        "status": "not_confirmed",
        "verdict": "failed",
        "next_action": "邮件同步保持暂停，需重新完成服务器端安全测试后再确认。",
    }


def section_text(body: str, heading: str, limit: int = 1200) -> str:
    match = re.search(rf"^## {re.escape(heading)}[ \t]*\n+(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1).strip())[:limit]


def conflict_reason_text(body: str, limit: int = 800) -> str:
    section = section_text(body, "冲突说明", limit)
    if section:
        return section
    match = re.search(r"(?:^|\n)冲突说明[：:]\s*(.*?)(?=\n\s*\n|\n## |\Z)", body, re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1).strip())[:limit] if match else ""


def sync_pending_decisions(config: dict[str, Any]) -> dict[str, int]:
    """Retire legacy per-item decisions and expose inline claims read-only."""
    stamp = syncv2.now_iso()
    connection = syncv2.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE decisions
               SET status='resolved',resolution='migrated_inline',resolved_at=COALESCE(resolved_at,?),
                   snooze_until=NULL,updated_at=?
             WHERE status IN ('pending','deferred')
            """,
            (stamp, stamp),
        )
        connection.commit()
        outbox = reconciliation.drain_relation_outbox(config)
        claims = business_router.scan_claims(business_router.vault_root_from_config(config))
        plan = reconciliation.plan_reconciliation(config)
        reconciliation.apply_plan(
            config,
            plan,
            run_id=f"refresh-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
            require_maintenance=False,
        )
        return {
            "created": 0,
            "updated": int(cursor.rowcount or 0),
            "unchanged": sum(1 for item in claims if item.get("risk") == "high"),
            "exceptions": 0,
            "relation_outbox_applied": int(outbox["applied"]),
            "relation_outbox_deferred": int(outbox["deferred"]),
        }
    finally:
        connection.close()


def list_decisions(config: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        cases = reconciliation.list_cases("needs_user")["items"]
    except reconciliation.ReconciliationError:
        cases = []
    return [
        {
            "decision_id": str(item["case_id"]),
            "revision": int(item["case_version"]),
            "status": "pending",
            "created_at": str(item["updated_at"]),
            "updated_at": str(item["updated_at"]),
            "snooze_until": None,
            "source_kind": "review_case",
            "severity": "manual_required",
            "actionable": True,
            "action": "explicit_preview_required",
            "title": str(item["title"]),
            "category": str(item["dimension"]),
            "memory_kind": str(item["case_kind"]),
            "summary": str(item["assessment"]),
            "evidence_boundary": str(item["evidence_boundary"]),
            "conflict_reason": item.get("manual_reason"),
            "observed_at": str(item["updated_at"]),
            "applies_to": ["project:business-energy"],
            "related_memories": [],
        }
        for item in cases
    ]


def decision_detail(config: dict[str, Any], decision_id: str) -> dict[str, Any]:
    if decision_id.startswith("inline:"):
        matches = [item for item in list_decisions(config) if item["decision_id"] == decision_id]
        if not matches:
            raise DecisionError("decision_missing", "这条高风险核验说明已经不存在。", 404)
        item = matches[0]
        return {
            **item,
            "resolution": None,
            "resolved_at": None,
            "related_memories": [],
        }
    row = decision_row(decision_id)
    candidate = json.loads(str(row["candidate_json"]))
    related_ids = json.loads(str(row["related_memory_ids_json"]))
    memory_root = memory.runtime_paths(config)["memory_root"]
    by_id = {str(record["memory_id"]): record for record in memory.memory_records(memory_root)}
    related_memories = []
    for memory_id in related_ids:
        related = by_id.get(str(memory_id))
        if not related:
            related_memories.append({"title": "关联记忆不可用", "missing": True})
            continue
        related_memories.append({
            "title": str(related.get("title") or "关联记忆"),
            "summary": str(related.get("summary") or ""),
            "content": section_text(str(related.get("body") or ""), "记忆内容", 4000),
            "observed_at": str(related.get("observed_at") or ""),
            "missing": False,
        })
    severity = str(row["severity"] or "confirmation")
    status = str(row["status"])
    return {
        "decision_id": str(row["decision_id"]),
        "revision": int(row["revision"] or 1),
        "status": status,
        "resolution": row["resolution"],
        "created_at": str(row["created_at"]),
        "resolved_at": row["resolved_at"],
        "source_kind": str(row["source_kind"]),
        "severity": severity,
        "actionable": status in {"pending", "deferred"} and severity != "system_exception",
        "title": str(candidate.get("title") or "记忆决策"),
        "summary": str(candidate.get("summary") or ""),
        "evidence_boundary": str(candidate.get("evidence_boundary") or ""),
        "conflict_reason": candidate.get("conflict_reason"),
        "candidate_content": str(candidate.get("content") or ""),
        "observed_at": str(candidate.get("observed_at") or ""),
        "related_memories": related_memories,
    }


def decision_row(decision_id: str) -> sqlite3.Row:
    connection = syncv2.connect_readonly()
    try:
        row = connection.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if not row:
            raise DecisionError("decision_missing", "这个决策项已经不存在。", 404)
        return row
    finally:
        connection.close()


def preview_decision(
    row: sqlite3.Row,
    action: str,
    merge_text: str = "",
    snooze_days: Optional[int] = None,
) -> dict[str, Any]:
    if action not in {"reject", "accept_new", "merge", "defer"}:
        raise DecisionError("action_invalid", "不支持这个决策动作。")
    if str(row["severity"]) == "system_exception":
        raise DecisionError("decision_not_actionable", "这条记录结构不完整，只能在异常区查看，不能直接执行。", 409)
    candidate = json.loads(str(row["candidate_json"]))
    related = json.loads(str(row["related_memory_ids_json"]))
    if action == "merge" and len(merge_text.strip()) < 5:
        raise DecisionError("merge_text_required", "合并时请填写最终采用的简洁结论。")
    if action == "merge" and not related:
        raise DecisionError("merge_target_missing", "没有可供合并的关联记忆。", 409)
    if action == "defer" and snooze_days not in {1, 3, 7}:
        raise DecisionError("snooze_required", "请选择明天、3 天后或下周再提醒。")
    conflict = str(candidate.get("action") or "") == "conflict"
    effect = {
        "reject": "不写入这条候选；候选会移入本机可恢复归档，现有正式记忆不变。",
        "accept_new": (
            "确认新内容并将它写入正式记忆；列出的旧记忆会标记为已被替代。"
            if conflict
            else "确认这条候选并写入正式记忆。"
        ),
        "merge": "采用你填写的合并结论作为正式记忆，并把列出的旧冲突记忆标记为已被替代。",
        "defer": f"本次不改记忆文件；在 {snooze_days} 天内不重复提醒。",
    }[action]
    return {
        "decision_id": str(row["decision_id"]),
        "revision": int(row["revision"] or 1),
        "title": str(candidate.get("title") or row["candidate_memory_id"]),
        "action": action,
        "effect": effect,
        "candidate_memory_id": str(row["candidate_memory_id"]),
        "related_memory_ids": related,
        "recoverable": True,
    }


def decision_context_hash(decision_id: str, connection: Optional[sqlite3.Connection] = None) -> str:
    owns_connection = connection is None
    database = connection or syncv2.connect_readonly()
    try:
        context = [
            {
                "author": str(row["author"]),
                "kind": str(row["message_kind"]),
                "content": str(row["content"]),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
            }
            for row in database.execute(
                """
                SELECT author,message_kind,content,status,created_at
                FROM decision_messages WHERE decision_id=? ORDER BY created_at,message_id
                """,
                (decision_id,),
            )
        ]
        return syncv2.sha256_text(syncv2.canonical_json(context))
    finally:
        if owns_connection:
            database.close()


def create_preview(
    config: dict[str, Any],
    row: sqlite3.Row,
    action: str,
    merge_text: str = "",
    snooze_days: Optional[int] = None,
) -> dict[str, Any]:
    preview = preview_decision(row, action, merge_text, snooze_days)
    candidate_path = Path(str(row["candidate_path"]))
    if not candidate_path.is_file() or candidate_path.is_symlink():
        raise DecisionError("candidate_missing", "候选文件不存在或不是安全的普通文件。", 409)
    actual_candidate_hash = syncv2.sha256_file(candidate_path)
    if actual_candidate_hash != str(row["candidate_file_hash"]):
        raise DecisionError("candidate_changed", "候选内容已经变化，请刷新后重新预览。", 409)
    expected_related = json.loads(str(row["related_hashes_json"]))
    memory_root = memory.runtime_paths(config)["memory_root"]
    by_id = {str(record["memory_id"]): record for record in memory.memory_records(memory_root)}
    for memory_id, expected in expected_related.items():
        related = by_id.get(str(memory_id))
        if not related or syncv2.sha256_file(Path(related["path"])) != str(expected):
            raise DecisionError("related_memory_changed", f"关联记忆 {memory_id} 已变化，请刷新后重新预览。", 409)
    token = secrets.token_urlsafe(32)
    token_hash = syncv2.sha256_text(token)
    created_at = datetime.now().astimezone()
    expires_at = created_at + timedelta(minutes=PREVIEW_TTL_MINUTES)
    connection = syncv2.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        context_hash = decision_context_hash(str(row["decision_id"]), connection)
        connection.execute(
            "DELETE FROM decision_previews WHERE decision_id=? OR expires_at<? OR used_at IS NOT NULL",
            (str(row["decision_id"]), created_at.isoformat(timespec="seconds")),
        )
        connection.execute(
            """
            INSERT INTO decision_previews(
              token_hash,decision_id,revision,action,merge_text_hash,snooze_days,candidate_hash,
              candidate_file_hash,related_hashes_json,context_hash,created_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                token_hash,
                str(row["decision_id"]),
                int(row["revision"] or 1),
                action,
                syncv2.sha256_text(merge_text.strip()),
                snooze_days,
                str(row["candidate_hash"]),
                actual_candidate_hash,
                str(row["related_hashes_json"]),
                context_hash,
                created_at.isoformat(timespec="seconds"),
                expires_at.isoformat(timespec="seconds"),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {**preview, "preview_token": token, "expires_at": expires_at.isoformat(timespec="seconds")}


def validate_preview_token(
    row: sqlite3.Row,
    token: str,
    action: str,
    merge_text: str,
    snooze_days: Optional[int],
) -> str:
    if len(token) < 32:
        raise DecisionError("preview_required", "请先查看本次操作的影响预览。", 409)
    token_hash = syncv2.sha256_text(token)
    connection = syncv2.connect_readonly()
    try:
        preview = connection.execute(
            "SELECT * FROM decision_previews WHERE token_hash=?",
            (token_hash,),
        ).fetchone()
        current_context_hash = decision_context_hash(str(row["decision_id"]), connection)
    finally:
        connection.close()
    if not preview or preview["used_at"]:
        raise DecisionError("preview_invalid", "预览令牌不存在或已经使用，请重新预览。", 409)
    try:
        expired = datetime.fromisoformat(str(preview["expires_at"])) <= datetime.now().astimezone()
    except ValueError:
        expired = True
    expected = (
        str(row["decision_id"]),
        int(row["revision"] or 1),
        action,
        syncv2.sha256_text(merge_text.strip()),
        snooze_days,
        str(row["candidate_hash"]),
        str(row["candidate_file_hash"]),
        str(row["related_hashes_json"]),
        current_context_hash,
    )
    actual = (
        str(preview["decision_id"]),
        int(preview["revision"]),
        str(preview["action"]),
        str(preview["merge_text_hash"]),
        preview["snooze_days"],
        str(preview["candidate_hash"]),
        str(preview["candidate_file_hash"]),
        str(preview["related_hashes_json"]),
        str(preview["context_hash"] or ""),
    )
    if expired or actual != expected:
        raise DecisionError("preview_stale", "内容、动作或文件状态已经变化，请重新预览。", 409)
    return token_hash


def replace_section(body: str, heading: str, value: str) -> str:
    pattern = re.compile(rf"(^## {re.escape(heading)}[ \t]*\n+)(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(body)
    if match:
        replacement = match.group(1) + value.strip() + "\n\n"
        return body[: match.start()] + replacement + body[match.end() :].lstrip("\n")
    return f"## {heading}\n\n{value.strip()}\n\n" + body.lstrip("\n")


def set_frontmatter_list(raw: str, key: str, values: list[str]) -> str:
    lines = raw.splitlines()
    start: Optional[int] = None
    end: Optional[int] = None
    for index, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            start = index
            end = index + 1
            while end < len(lines) and (lines[end].startswith("  - ") or not lines[end].strip()):
                end += 1
            break
    rendered = [f"{key}:"] + [f"  - {json.dumps(value, ensure_ascii=False)}" for value in values]
    if not values:
        rendered = [f"{key}: []"]
    if start is None:
        return raw.rstrip() + "\n" + "\n".join(rendered)
    return "\n".join(lines[:start] + rendered + lines[end:])


def with_supersedes(text: str, values: list[str]) -> str:
    match = memory.FRONTMATTER_RE.match(text)
    if not match:
        raise DecisionError("candidate_invalid", "候选记忆格式无效。", 409)
    raw = set_frontmatter_list(match.group(1), "supersedes", values)
    return "---\n" + raw.rstrip() + "\n---" + memory.canonical_memory_body(match.group(2))


def snapshot_files(paths: list[Path]) -> dict[Path, tuple[bool, bytes, int]]:
    snapshot: dict[Path, tuple[bool, bytes, int]] = {}
    for path in paths:
        if path.exists() and (not path.is_file() or path.is_symlink()):
            raise DecisionError("unsafe_path", "决策目标不是普通文件。", 409)
        if path.is_file():
            snapshot[path] = (True, path.read_bytes(), path.stat().st_mode & 0o777)
        else:
            snapshot[path] = (False, b"", 0o644)
    return snapshot


def restore_snapshot(snapshot: dict[Path, tuple[bool, bytes, int]]) -> None:
    for path, (existed, content, mode) in snapshot.items():
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.rollback-{uuid.uuid4().hex}")
            temporary.write_bytes(content)
            temporary.chmod(mode)
            os.replace(temporary, path)
        elif path.exists() and path.is_file() and not path.is_symlink():
            path.unlink()


def prepare_journal(
    decision_run: Path,
    snapshot: dict[Path, tuple[bool, bytes, int]],
    *,
    transaction_id: str,
    decision_id: str,
    preview_token_hash: str,
    action: str,
    expected_status: str,
) -> Path:
    backup_root = decision_run / "backup"
    syncv2.ensure_private_dir(backup_root)
    entries = []
    for path, (existed, content, mode) in snapshot.items():
        backup: Optional[Path] = None
        if existed:
            backup = backup_root / f"{hashlib.sha256(str(path).encode()).hexdigest()}.md"
            memory.atomic_write(backup, content.decode("utf-8"), mode=0o600)
        entries.append({"target": str(path), "existed": existed, "backup": str(backup) if backup else None, "mode": mode})
    journal = decision_run / "journal.json"
    memory.atomic_write(
        journal,
        json.dumps(
            {
                "schema_version": 2,
                "state": "prepared",
                "created_at": syncv2.now_iso(),
                "transaction_id": transaction_id,
                "decision_id": decision_id,
                "preview_token_hash": preview_token_hash,
                "action": action,
                "expected_status": expected_status,
                "paths": entries,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        mode=0o600,
    )
    return journal


def update_journal(journal: Path, state: str) -> None:
    payload = json.loads(journal.read_text(encoding="utf-8"))
    payload["state"] = state
    payload[f"{state}_at"] = syncv2.now_iso()
    memory.atomic_write(journal, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def create_transaction(
    transaction_id: str,
    decision_id: str,
    preview_token_hash: str,
    action: str,
    journal: Path,
) -> None:
    stamp = syncv2.now_iso()
    connection = syncv2.connect()
    try:
        connection.execute(
            """
            INSERT INTO decision_transactions(
              transaction_id,decision_id,preview_token_hash,action,state,journal_path,created_at,updated_at
            ) VALUES(?,?,?,?, 'prepared',?,?,?)
            """,
            (transaction_id, decision_id, preview_token_hash, action, str(journal), stamp, stamp),
        )
        connection.commit()
    finally:
        connection.close()


def set_transaction_state(transaction_id: str, state: str, error_code: Optional[str] = None) -> None:
    stamp = syncv2.now_iso()
    connection = syncv2.connect()
    try:
        connection.execute(
            """
            UPDATE decision_transactions SET state=?,updated_at=?,last_error_code=?,
              file_committed_at=CASE WHEN ?='file_committed' THEN COALESCE(file_committed_at,?) ELSE file_committed_at END,
              database_committed_at=CASE WHEN ?='database_committed' THEN COALESCE(database_committed_at,?) ELSE database_committed_at END,
              notification_committed_at=CASE WHEN ? IN ('notification_committed','committed')
                THEN COALESCE(notification_committed_at,?) ELSE notification_committed_at END
            WHERE transaction_id=?
            """,
            (state, stamp, error_code, state, stamp, state, stamp, state, stamp, transaction_id),
        )
        connection.commit()
    finally:
        connection.close()


def restore_journal(journal: Path, config: dict[str, Any]) -> None:
    payload = json.loads(journal.read_text(encoding="utf-8"))
    memory_root = memory.runtime_paths(config)["memory_root"].resolve()
    archive_root = syncv2.ARCHIVE_DIR.resolve()
    for entry in payload.get("paths", []):
        target = Path(str(entry["target"]))
        resolved_parent = target.parent.resolve()
        if memory_root not in (resolved_parent, *resolved_parent.parents) and archive_root not in (resolved_parent, *resolved_parent.parents):
            raise DecisionError("journal_target_unsafe", "决策恢复目标超出允许目录。", 409)
        if entry.get("existed"):
            backup = Path(str(entry.get("backup") or ""))
            if not backup.is_file():
                raise DecisionError("journal_backup_missing", "决策恢复备份缺失。", 409)
            memory.atomic_write(target, backup.read_text(encoding="utf-8"), mode=int(entry.get("mode") or 0o644))
        elif target.exists() and target.is_file() and not target.is_symlink():
            target.unlink()
    memory.run_refresh_and_validate(config)
    update_journal(journal, "rolled_back")
    if payload.get("transaction_id"):
        set_transaction_state(str(payload["transaction_id"]), "rolled_back")


def recover_decision_journals(config: dict[str, Any]) -> int:
    root = syncv2.RUNS_DIR / "decisions"
    if not root.is_dir():
        return 0
    recovered = 0
    for journal in root.glob("*/*/journal.json"):
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        state = str(payload.get("state") or "")
        if state not in {"prepared", "applying", "file_committed", "database_committed", "notification_committed", "recovery_pending"}:
            continue
        transaction_id = str(payload.get("transaction_id") or "")
        decision_id = str(payload.get("decision_id") or "")
        expected_status = str(payload.get("expected_status") or "")
        if state in {"database_committed", "notification_committed"}:
            if transaction_id:
                set_transaction_state(transaction_id, "committed")
            update_journal(journal, "committed")
            recovered += 1
            continue
        if state in {"file_committed", "recovery_pending"} and decision_id and expected_status:
            connection = syncv2.connect_readonly()
            try:
                row = connection.execute(
                    "SELECT status FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
            finally:
                connection.close()
            if row and str(row["status"]) == expected_status:
                if transaction_id:
                    set_transaction_state(transaction_id, "committed")
                update_journal(journal, "committed")
                recovered += 1
                continue
        memory_lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=True)
        try:
            restore_journal(journal, config)
            recovered += 1
        finally:
            memory_lock.close()
    return recovered


def apply_decision(
    config: dict[str, Any],
    decision_id: str,
    action: str,
    merge_text: str = "",
    *,
    preview_token: str,
    snooze_days: Optional[int] = None,
) -> dict[str, Any]:
    row = decision_row(decision_id)
    preview = preview_decision(row, action, merge_text, snooze_days)
    if str(row["status"]) not in {"pending", "deferred"}:
        raise DecisionError("decision_resolved", "这个决策项已经处理。", 409)
    token_hash = validate_preview_token(row, preview_token, action, merge_text, snooze_days)
    transaction_id = str(uuid.uuid4())
    resolution = {
        "reject": "rejected",
        "accept_new": "accepted",
        "merge": "merged",
        "defer": "deferred",
    }[action]
    decision_run = syncv2.RUNS_DIR / "decisions" / decision_id / transaction_id
    syncv2.ensure_private_dir(decision_run)

    if action == "defer":
        journal = prepare_journal(
            decision_run,
            {},
            transaction_id=transaction_id,
            decision_id=decision_id,
            preview_token_hash=token_hash,
            action=action,
            expected_status=resolution,
        )
        create_transaction(transaction_id, decision_id, token_hash, action, journal)
        try:
            update_journal(journal, "applying")
            set_transaction_state(transaction_id, "file_committed")
            update_journal(journal, "file_committed")
            snooze_until = (datetime.now().astimezone() + timedelta(days=int(snooze_days or 1))).isoformat(timespec="seconds")
            stamp = syncv2.now_iso()
            connection = syncv2.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT status,revision FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                if not current or str(current["status"]) not in {"pending", "deferred"} or int(current["revision"] or 1) != int(row["revision"] or 1):
                    raise DecisionError("decision_changed", "决策项已经变化，请重新预览。", 409)
                connection.execute(
                    """
                    UPDATE decisions SET status='deferred',resolution='defer',resolved_at=NULL,
                      snooze_until=?,revision=revision+1,updated_at=? WHERE decision_id=?
                    """,
                    (snooze_until, stamp, decision_id),
                )
                connection.execute("UPDATE decision_previews SET used_at=? WHERE token_hash=?", (stamp, token_hash))
                connection.execute(
                    """
                    UPDATE decision_transactions SET state='database_committed',database_committed_at=?,updated_at=?
                    WHERE transaction_id=?
                    """,
                    (stamp, stamp, transaction_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            update_journal(journal, "database_committed")
            set_transaction_state(transaction_id, "committed")
            update_journal(journal, "committed")
            return {**preview, "status": "deferred", "snooze_until": snooze_until, "validated": True}
        except Exception as exc:
            update_journal(journal, "rolled_back")
            set_transaction_state(transaction_id, "rolled_back", getattr(exc, "code", type(exc).__name__))
            raise

    candidate_path = Path(str(row["candidate_path"]))
    if (
        not candidate_path.is_file()
        or candidate_path.is_symlink()
        or syncv2.sha256_file(candidate_path) != str(row["candidate_file_hash"])
    ):
        raise DecisionError("candidate_changed", "候选内容已经变化，请刷新后重新判断。", 409)
    if syncv2.sha256_text(str(row["candidate_json"])) != str(row["candidate_hash"]):
        raise DecisionError("candidate_state_changed", "候选状态已经变化，请刷新。", 409)
    candidate = json.loads(str(row["candidate_json"]))
    related_ids = json.loads(str(row["related_memory_ids_json"]))
    expected_hashes = json.loads(str(row["related_hashes_json"]))
    memory_root = memory.runtime_paths(config)["memory_root"]
    records = memory.memory_records(memory_root)
    by_id = {str(record["memory_id"]): record for record in records}
    for memory_id, expected in expected_hashes.items():
        related = by_id.get(memory_id)
        if not related or syncv2.sha256_file(Path(related["path"])) != expected:
            raise DecisionError("related_memory_changed", f"关联记忆 {memory_id} 已变化，请重新判断。", 409)

    category_dir = {
        "business_cognition": "01_关键业务认知",
        "internal_material": "02_内部业务资料",
        "external_communication": "03_外部沟通",
    }.get(str(candidate.get("category")))
    if not category_dir:
        raise DecisionError("candidate_category", "候选分类无效。", 409)
    target_path = memory_root / category_dir / candidate_path.name
    if target_path.exists() and target_path != candidate_path:
        target_path = memory_root / category_dir / f"{candidate_path.stem}-{decision_id[:8]}.md"
    index_paths = [
        memory_root / "00_记忆总览.md",
        memory_root / "99_规则与模板" / "AI检索总表.md",
        memory_root / "90_待确认" / "00_待确认总表.md",
    ]
    related_paths = [Path(by_id[value]["path"]) for value in related_ids if value in by_id]
    archive_path = syncv2.ARCHIVE_DIR / f"{row['candidate_memory_id']}-{decision_id}.md"
    changed_paths = [candidate_path, *related_paths, *index_paths]
    if action == "reject":
        changed_paths.append(archive_path)
    else:
        changed_paths.append(target_path)
    snapshot = snapshot_files(list(dict.fromkeys(changed_paths)))
    journal = prepare_journal(
        decision_run,
        snapshot,
        transaction_id=transaction_id,
        decision_id=decision_id,
        preview_token_hash=token_hash,
        action=action,
        expected_status=resolution,
    )
    create_transaction(transaction_id, decision_id, token_hash, action, journal)
    database_committed = False

    with syncv2.file_lock(syncv2.DECISION_LOCK):
        memory_lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=True)
        try:
            try:
                update_journal(journal, "applying")
                set_transaction_state(transaction_id, "applying")
                if action == "reject":
                    memory.atomic_write(archive_path, candidate_path.read_text(encoding="utf-8"), mode=0o600)
                    candidate_path.unlink()
                else:
                    candidate_record = memory.parse_memory_note(candidate_path, strict=True)
                    if not candidate_record:
                        raise DecisionError("candidate_invalid", "候选记忆无法解析。", 409)
                    body = str(candidate_record["body"])
                    if action == "merge":
                        merged = memory.sanitize_note_text(merge_text, 1800)
                        if len(merged) < 5:
                            raise DecisionError("merge_text_required", "合并结论太短。")
                        body = replace_section(body, "记忆内容", merged)
                    marker = f"<!-- decision:{decision_id} -->"
                    choice = "合并后确认" if action == "merge" else "确认写入"
                    entry = f"{marker}\n- {syncv2.now_iso()}：Example Owner 在本机同步中心明确选择“{choice}”。"
                    body = memory.append_section_entry(body, "更新记录", entry, marker)
                    text = memory.rebuild_existing_note(
                        str(candidate_record["raw_frontmatter"]),
                        body,
                        {
                            "status": "active",
                            "evidence_level": "internal_source",
                            "source_check": "matched",
                            "fact_status": "confirmed",
                            "reviewed_at": syncv2.now_iso(),
                        },
                    )
                    supersedes = related_ids if candidate.get("action") == "conflict" or action == "merge" else []
                    text = with_supersedes(text, supersedes)
                    memory.atomic_write(target_path, text, mode=0o644)
                    if target_path != candidate_path:
                        candidate_path.unlink()
                    for related_id in supersedes:
                        related = by_id.get(related_id)
                        if not related:
                            continue
                        related_body = memory.append_section_entry(
                            str(related["body"]),
                            "更新记录",
                            f"<!-- decision:{decision_id}:{related_id} -->\n- {syncv2.now_iso()}：经本机同步中心确认，由 {row['candidate_memory_id']} 替代。",
                            f"<!-- decision:{decision_id}:{related_id} -->",
                        )
                        related_text = memory.rebuild_existing_note(
                            str(related["raw_frontmatter"]),
                            related_body,
                            {"status": "superseded", "reviewed_at": syncv2.now_iso()},
                        )
                        memory.atomic_write(Path(related["path"]), related_text, mode=0o644)
                verification = memory.run_refresh_and_validate(config, lock_held=True)
                set_transaction_state(transaction_id, "file_committed")
                update_journal(journal, "file_committed")

                stamp = syncv2.now_iso()
                connection = syncv2.connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        "SELECT status,revision,candidate_hash FROM decisions WHERE decision_id=?",
                        (decision_id,),
                    ).fetchone()
                    if (
                        not current
                        or str(current["status"]) not in {"pending", "deferred"}
                        or int(current["revision"] or 1) != int(row["revision"] or 1)
                        or str(current["candidate_hash"]) != str(row["candidate_hash"])
                    ):
                        raise DecisionError("decision_changed", "决策项已经变化，请重新预览。", 409)
                    connection.execute(
                        """
                        UPDATE decisions SET status=?,resolution=?,resolved_at=?,snooze_until=NULL,updated_at=?
                        WHERE decision_id=?
                        """,
                        (resolution, action, stamp, stamp, decision_id),
                    )
                    connection.execute("UPDATE decision_previews SET used_at=? WHERE token_hash=?", (stamp, token_hash))
                    connection.execute(
                        """
                        UPDATE decision_transactions SET state='database_committed',database_committed_at=?,updated_at=?
                        WHERE transaction_id=?
                        """,
                        (stamp, stamp, transaction_id),
                    )
                    connection.commit()
                    database_committed = True
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
                update_journal(journal, "database_committed")
                set_transaction_state(transaction_id, "committed")
                update_journal(journal, "committed")
            except Exception as exc:
                if database_committed:
                    update_journal(journal, "recovery_pending")
                    set_transaction_state(transaction_id, "recovery_pending", getattr(exc, "code", type(exc).__name__))
                else:
                    restore_snapshot(snapshot)
                    memory.run_refresh_and_validate(config, lock_held=True)
                    update_journal(journal, "rolled_back")
                    set_transaction_state(transaction_id, "rolled_back", getattr(exc, "code", type(exc).__name__))
                raise
        finally:
            memory_lock.close()

    return {**preview, "status": resolution, "validated": verification["validator"].get("verdict") == "pass"}


def pssd_state(config: dict[str, Any]) -> dict[str, Any]:
    archive = config.get("dingtalk_pssd_archive") if isinstance(config.get("dingtalk_pssd_archive"), dict) else {}
    mount = Path(str(archive.get("mount_path") or "/Volumes/Example Archive"))
    expected_uuid = str(archive.get("volume_uuid") or '23256914-1a59-55b3-b128-98d3b86fad89')
    state: dict[str, Any] = {"online": mount.is_dir(), "uuid_verified": False, "writable": False, "free_bytes": None}
    if not mount.is_dir():
        return state
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", str(mount)],
            capture_output=True,
            timeout=10,
            check=False,
        )
        detail = plistlib.loads(result.stdout) if result.returncode == 0 and result.stdout else {}
        state["uuid_verified"] = str(detail.get("VolumeUUID") or "").upper() == expected_uuid.upper()
    except (OSError, subprocess.TimeoutExpired, plistlib.InvalidFileException):
        state["uuid_verified"] = False
    try:
        stat = os.statvfs(mount)
        state["free_bytes"] = int(stat.f_bavail * stat.f_frsize)
        state["writable"] = os.access(mount, os.W_OK)
    except OSError:
        pass
    return state


def read_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def maintenance_status() -> dict[str, Any]:
    if not MAINTENANCE_STATE.exists():
        return {"active": False, "status": "inactive"}
    try:
        value = json.loads(MAINTENANCE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active": True, "status": "unknown"}
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        return {"active": True, "status": "unknown"}
    status = str(value["status"])
    return {"active": status != "inactive", "status": status}


def maintenance_active() -> bool:
    return bool(maintenance_status()["active"])


@contextmanager
def maintenance_write_guard():
    """Serialize UI mutations with maintenance entry and fail closed while active."""

    MAINTENANCE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with MAINTENANCE_LOCK.open("a+") as handle:
        os.chmod(MAINTENANCE_LOCK, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            if maintenance_active():
                raise DecisionError(
                    "maintenance_active",
                    "系统正在维护，当前仅可查看；本次没有写入。",
                    HTTPStatus.LOCKED,
                )
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def metadata_json(key: str, default: Any) -> Any:
    connection = syncv2.connect_readonly()
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    finally:
        connection.close()
    if not row:
        return default
    try:
        return json.loads(str(row["value"]))
    except json.JSONDecodeError:
        return default


def set_metadata_json(key: str, value: Any) -> None:
    stamp = syncv2.now_iso()
    connection = syncv2.connect()
    try:
        connection.execute(
            """
            INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, syncv2.canonical_json(value), stamp),
        )
        connection.commit()
    finally:
        connection.close()


def preferences() -> dict[str, Any]:
    stored = metadata_json(PREFERENCES_KEY, {})
    appearance = str(stored.get("appearance") or "system") if isinstance(stored, dict) else "system"
    if appearance not in {"system", "light", "dark"}:
        appearance = "system"
    return {"appearance": appearance}


def update_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    appearance = str(payload.get("appearance") or "")
    if appearance not in {"system", "light", "dark"}:
        raise DecisionError("preference_invalid", "外观设置无效。")
    value = {"appearance": appearance}
    set_metadata_json(PREFERENCES_KEY, value)
    return value


def opaque_item_id(kind: str, source_key: str) -> str:
    return f"{kind}-{syncv2.sha256_text(source_key)[:14]}"


def parse_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except (TypeError, ValueError):
        return None


def transcript_is_complete(task_uuid: str) -> bool:
    payload = read_json_file(MINUTES_RAW_ROOT / task_uuid / "transcript.json", {})
    return bool(payload.get("complete") is True and isinstance(payload.get("paragraphList"), list))


def media_is_secured(entry: dict[str, Any]) -> bool:
    storage = str(entry.get("media_storage") or "local")
    if storage == "pssd":
        relative = Path(str(entry.get("media_archive_relative_path") or ""))
        return bool(
            relative.parts
            and not relative.is_absolute()
            and ".." not in relative.parts
            and entry.get("media_archive_volume_uuid")
            and entry.get("media_sha256")
            and int(entry.get("media_bytes") or 0) > 0
        )
    if storage != "local":
        return False
    filename = str(entry.get("media_filename") or "")
    media_path = MINUTES_ROOT / "音频" / filename
    try:
        return bool(filename and Path(filename).name == filename and media_path.is_file() and media_path.stat().st_size > 0)
    except OSError:
        return False


def stage(label: str, status: str) -> dict[str, str]:
    return {"label": label, "status": status}


def recording_attention(config: dict[str, Any], pssd: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inventory = read_json_file(MINUTES_REMOTE_INVENTORY, {})
    sync_state = read_json_file(MINUTES_SYNC_STATE, {})
    entries = sync_state.get("entries") if isinstance(sync_state, dict) else {}
    entries = entries if isinstance(entries, dict) else {}
    remote_items = inventory.get("items") if isinstance(inventory, dict) else {}
    inventory_complete_at = parse_timestamp(inventory.get("last_complete_at")) if isinstance(inventory, dict) else None
    sync_complete_at = parse_timestamp(sync_state.get("last_sync_at")) if isinstance(sync_state, dict) else None
    sync_state_is_newer = bool(sync_complete_at and (not inventory_complete_at or sync_complete_at > inventory_complete_at))
    if not isinstance(remote_items, dict) or not remote_items or sync_state_is_newer:
        remote_items = {
            str(task_uuid): {
                "title": str(entry.get("title") or "未命名录音"),
                "start_time": entry.get("start_time"),
            }
            for task_uuid, entry in entries.items()
            if isinstance(entry, dict)
        }
        inventory = {
            **(inventory if isinstance(inventory, dict) else {}),
            "last_attempt_status": "complete" if not sync_state.get("pagination_warning") else "partial",
            "last_attempt_at": sync_state.get("last_sync_at"),
            "last_complete_at": sync_state.get("last_sync_at"),
            "last_complete_count": int(sync_state.get("last_list_count") or len(remote_items)),
            "last_complete_pages": int(sync_state.get("last_list_pages") or 0),
            "pagination_warning": sync_state.get("pagination_warning"),
        }
    ledger = read_json_file(REVIEW_LEDGER, {})
    ledger = ledger if isinstance(ledger, dict) else {}
    connection = syncv2.connect_readonly()
    try:
        recording_rows = {str(row["task_uuid"]): row for row in connection.execute("SELECT * FROM recordings")}
    finally:
        connection.close()
    cutover = datetime.fromisoformat(str(config["dingtalk_minutes_memory"]["cutover_at"]))
    items: list[dict[str, Any]] = []
    history_items: list[dict[str, Any]] = []
    for task_uuid, remote in sorted(
        remote_items.items(),
        key=lambda item: int((item[1] or {}).get("start_time") or 0),
        reverse=True,
    ):
        if not isinstance(remote, dict):
            continue
        entry = entries.get(task_uuid) if isinstance(entries.get(task_uuid), dict) else None
        title = str(remote.get("title") or (entry or {}).get("title") or "未命名录音")[:180]
        raw_start = remote.get("start_time") or (entry or {}).get("start_time")
        try:
            observed = datetime.fromtimestamp(int(raw_start) / 1000).astimezone()
            observed_at = observed.isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            observed = None
            observed_at = None
        row = recording_rows.get(task_uuid)
        discovered = True
        local_note = bool(entry and (MINUTES_NOTES_ROOT / str(entry.get("filename") or "")).is_file())
        transcript_ok = bool(entry and transcript_is_complete(task_uuid))
        media_ok = bool(entry and media_is_secured(entry))
        required_missing = [
            value for value in ((entry or {}).get("missing_artifacts") or [])
            if value not in {"todos", "media"}
        ]
        synced = bool(entry and local_note and transcript_ok and media_ok and not required_missing)
        post_cutover = bool(observed and observed >= cutover)
        source_written = bool(row and row["source_memory_id"] and row["source_memory_path"] and Path(str(row["source_memory_path"])).is_file())
        semantic_done = bool(row and str(row["status"]) in {"completed", "no_business_change"})
        review = ledger.get(task_uuid) if isinstance(ledger.get(task_uuid), dict) else {}
        review_status = str(review.get("status") or "pending")
        policy_state = review_policy.classify(
            task_uuid,
            entry or {},
            review,
            row,
            cutover,
            raw_root=MINUTES_RAW_ROOT,
            minutes_root=MINUTES_ROOT,
            review_root=MINUTES_ROOT / "核对",
        ) if entry else {"state": "waiting_source", "required": True}
        review_state = str(policy_state["state"])
        resume_mode = str(policy_state.get("resumable") or "none")
        manual_review_stop = bool(
            review_status in {"failed", "review_done_reconcile_failed"}
            or (review_status == "interrupted" and resume_mode == "none")
        )
        readiness_state = review_state
        if review_state in {"failed", "in_progress", "interrupted_manual"}:
            readiness_state = str(review_policy.classify(
                task_uuid,
                entry or {},
                {**review, "status": "pending"},
                row,
                cutover,
                raw_root=MINUTES_RAW_ROOT,
                minutes_root=MINUTES_ROOT,
                review_root=MINUTES_ROOT / "核对",
            )["state"])
        review_waiting = readiness_state in {"waiting_source", "waiting_media", "waiting_pssd"}
        review_done = review_state == "done"
        review_exempt = review_state == "exempt"
        review_declined = review_state == "duration_declined"
        review_gate_pending = review_state == "waiting_duration_decision"
        review_artifact_done = review_status in {
            "done",
            "review_done_reconcile_deferred",
            "review_done_reconcile_failed",
        }
        archived = bool(entry and str(entry.get("media_storage")) == "pssd" and media_ok)
        stages = [
            stage("已发现", "done" if discovered else "unknown"),
            stage("已同步", "done" if synced else "pending"),
            stage("已写来源", "done" if source_written else ("pending" if post_cutover else "unknown")),
            stage("已更新记忆", "done" if semantic_done else ("error" if row and str(row["status"]) == "failed" else ("pending" if post_cutover else "unknown"))),
            stage(
                "超长免二查" if review_declined
                else ("历史免二查" if review_exempt else "已二查"),
                "done" if (review_artifact_done or review_exempt or review_declined)
                else ("error" if manual_review_stop else "pending"),
            ),
            stage("已归档", "done" if archived else ("error" if entry and str(entry.get("media_storage")) == "pssd_corrupt" else "pending")),
        ]
        base = {
            "name": title,
            "time": observed_at,
            "last_attempt": (entry or {}).get("last_synced_at") or inventory.get("last_attempt_at"),
            "stages": stages,
            "source_kind": "recording",
        }
        if not entry:
            items.append({
                **base,
                "id": opaque_item_id("recording", task_uuid + ":sync"),
                "category": "unsynced",
                "severity": "warning",
                "stage": "尚未同步到本机",
                "reason": "可信钉钉清单中有这条录音，但本机尚无登记。",
                "next_condition": "下次半小时检测，或点击“立即同步”。",
            })
            continue
        if (
            not synced
            or (post_cutover and (not source_written or not semantic_done))
            or review_waiting
        ):
            if manual_review_stop and review_waiting:
                stopped = "二查中断，来源尚未恢复"
                reason = "上次二查未完成，且当前还缺来源或媒体；不会自动付费重试。"
            elif not transcript_ok or not local_note or required_missing:
                stopped = "录音文字尚未完整落地"
                reason = "必要文字产物不完整。"
            elif readiness_state == "waiting_pssd":
                stopped = "媒体在 T9，等待连接"
                reason = "归档记录完整，T9 离线不视为丢失。"
            elif not media_ok or readiness_state == "waiting_media":
                stopped = "录音媒体尚未校验"
                reason = "必要媒体未完成本机或 T9 校验。"
            elif readiness_state == "waiting_source" and row and str(row["summary_comparison_status"] or "") != "compared":
                stopped = "纪要对照尚未完成"
                reason = "完整逐字稿和来源就绪后，仍需完成纪要对照才能进入付费二查。"
            elif not source_written:
                stopped = "尚未写入来源笔记"
                reason = "录音已同步，但业务记忆来源阶段尚未完成。"
            else:
                stopped = "尚未更新业务记忆"
                reason = str(row["last_error_code"] or "语义记忆仍在队列中。") if row else "语义记忆仍在队列中。"
            items.append({
                **base,
                "id": opaque_item_id("recording", task_uuid + ":pipeline"),
                "category": "unsynced",
                "severity": (
                    "error" if row and str(row["status"]) == "failed"
                    else ("neutral" if readiness_state == "waiting_pssd" else "warning")
                ),
                "stage": stopped,
                "reason": reason,
                "next_condition": (
                    "先补齐来源或媒体，再在 App 中手动重试二查。"
                    if manual_review_stop
                    else (
                        "连接已绑定的三星 T9 后自动恢复。"
                        if readiness_state == "waiting_pssd"
                        else "下次自动阶段执行，或点击“立即同步”。"
                    )
                ),
            })
        elif not post_cutover and not row:
            history_items.append({
                **base,
                "id": opaque_item_id("recording", task_uuid + ":verify"),
                "category": "archived_informational",
                "severity": "neutral",
                "stage": "历史记录",
                "reason": "该录音早于业务记忆切换点；现有同步、二查和归档结果只保留审计，不要求你处理。",
                "next_condition": "仅在历史搜索中显示，不参与徽标或工作量统计。",
            })
        if review_gate_pending:
            gate_hours = (
                float(policy_state.get("duration_seconds") or 0) / 3600.0
            )
            gate_limit_hours = (
                float(policy_state.get("duration_gate_seconds") or 0) / 3600.0
            )
            items.append({
                **base,
                "id": opaque_item_id("review", task_uuid),
                "category": "unreviewed",
                "severity": "warning",
                "stage": "超长音频待决定",
                "reason": (
                    "音频约 {:.1f} 小时，超过 {:.0f} 小时门禁；默认不自动转写二查，"
                    "避免高额转写费用。".format(gate_hours, gate_limit_hours)
                ),
                "next_condition": (
                    "由 owner 确认“同意转写”后自动进入锁屏二查"
                    "（--approve-duration-gate）；保持默认或明确不转写"
                    "（--decline-duration-gate）则不产生转写费用。"
                ),
                "last_attempt": review.get("updated_at") or base["last_attempt"],
                "queue_state": "duration_decision",
            })
        elif review_state not in {"done", "exempt", "duration_declined"} and not review_waiting:
            if review_status == "review_done_reconcile_deferred":
                review_stage = "二查完成，记忆回写等待 AI 通路"
                review_reason = "共享模型通路上次被占用；不会重跑音频、转写或二查。"
                severity = "warning"
                next_condition = "邮件或聊天释放 AI 通路后自动交接；每 10 分钟调度兜底。"
                queue_state = "reconcile_deferred"
            elif review_status == "interrupted" and resume_mode != "none":
                review_stage = "等待锁屏安全续跑"
                review_reason = "已有可验证的核对稿或 Qwen 参考稿；续跑会复用中间产物，不重复 ASR。"
                severity = "warning"
                next_condition = "Mac 正常锁屏且接电后自动续跑；不会自动重试无中间产物的中断。"
                queue_state = "resumable"
            elif review_status in {"failed", "review_done_reconcile_failed", "interrupted"}:
                review_stage = "二查失败"
                review_reason = "上次二查中断或未完成，不会自动付费重试。"
                severity = "error"
                next_condition = "在 App 中查看失败原因后手动重试。"
                queue_state = "failed"
            elif review_status == "in_progress":
                review_stage = "正在二查"
                review_reason = "当前条目已经进入二查流程。"
                severity = "neutral"
                next_condition = "等待当前 Qwen ASR、Codex 核对及记忆对账完成。"
                queue_state = "processing"
            else:
                review_stage = "等待锁屏二查"
                review_reason = "正常排队，不阻塞同步，也不标记为故障。"
                severity = "neutral"
                next_condition = "Mac 正常锁屏且接电时自动连续处理；也可手动二查。"
                queue_state = "waiting_locked"
            items.append({
                **base,
                "id": opaque_item_id("review", task_uuid),
                "category": "unreviewed",
                "severity": severity,
                "stage": review_stage,
                "reason": review_reason,
                "next_condition": next_condition,
                "last_attempt": review.get("updated_at") or base["last_attempt"],
                "queue_state": queue_state,
            })
        if entry and str(entry.get("media_storage")) == "pssd_corrupt":
            items.append({
                **base,
                "id": opaque_item_id("recording", task_uuid + ":archive"),
                "category": "system_exception",
                "severity": "error",
                "stage": "归档校验异常",
                "reason": "T9 归档记录未通过完整性校验，Mac 原文件应继续保留。",
                "next_condition": "连接 T9 后重新校验；不要手工删除本机文件。",
            })
    inventory_status = {
        "last_attempt_status": str(inventory.get("last_attempt_status") or "unknown"),
        "last_attempt_at": inventory.get("last_attempt_at"),
        "last_complete_at": inventory.get("last_complete_at") or sync_state.get("last_sync_at"),
        "last_complete_count": int(inventory.get("last_complete_count") or len(remote_items)),
        "pagination_warning": inventory.get("pagination_warning"),
        "history_items": history_items,
    }
    if inventory_status["last_attempt_status"] != "complete" or inventory_status["pagination_warning"]:
        items.insert(0, {
            "id": opaque_item_id("inventory", str(inventory_status["last_attempt_at"])),
            "category": "system_exception",
            "severity": "warning",
            "source_kind": "recording",
            "name": "钉钉录音清单待刷新",
            "time": inventory_status["last_attempt_at"],
            "stage": "保留上次可信清单",
            "reason": "本次远端扫描失败或分页不完整，没有把结果误显示为 0 条。",
            "next_condition": "下次自动检测或“立即同步”时重试。",
            "last_attempt": inventory_status["last_attempt_at"],
            "stages": [],
        })
    return items, inventory_status


def chat_attention(config: dict[str, Any]) -> list[dict[str, Any]]:
    database = memory.runtime_paths(config)["database"]
    if not database.is_file():
        return [{
            "id": opaque_item_id("chat", "database-missing"),
            "category": "system_exception",
            "severity": "error",
            "source_kind": "chat",
            "name": "钉钉聊天台账不可用",
            "time": None,
            "stage": "无法检查覆盖",
            "reason": "权威聊天数据库不存在。",
            "next_condition": "修复数据库后重新检查。",
            "last_attempt": None,
            "stages": [],
        }]
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            WITH pending_counts AS (
              SELECT m.conversation_id,COUNT(*) AS memory_pending
              FROM semantic_queue q JOIN messages m ON m.message_id=q.message_id
              WHERE q.state IN ('pending','failed','deferred','claimed') GROUP BY m.conversation_id
            )
            SELECT c.*,(SELECT s.has_more FROM conversation_scans s
                        WHERE s.conversation_id=c.conversation_id ORDER BY s.completed_at DESC LIMIT 1)
                       AS latest_has_more,COALESCE(p.memory_pending,0) AS memory_pending
            FROM conversations c
            LEFT JOIN pending_counts p ON p.conversation_id=c.conversation_id
            ORDER BY c.last_scan_at
            """
        ).fetchall()
    finally:
        connection.close()
    now = datetime.now().astimezone()
    items = []
    for row in rows:
        last_scan = parse_timestamp(row["last_scan_at"])
        overdue = not last_scan or (now - last_scan).total_seconds() > 65 * 60
        incomplete = (
            not row["last_complete_scan_at"]
            or str(row["last_scan_status"]) in {"partial", "failed", "incomplete", "event"}
            or bool(row["latest_has_more"])
            or overdue
        )
        if not incomplete:
            continue
        if bool(row["latest_has_more"]):
            stopped = "分页尚未完成"
            reason = "钉钉仍返回 hasMore=true，不能标记为已覆盖。"
        elif not row["last_complete_scan_at"]:
            stopped = "从未完成历史扫描"
            reason = "目前只有实时事件或部分记录，较早消息可能遗漏。"
        elif str(row["last_scan_status"]) in {"partial", "failed", "incomplete"}:
            stopped = "最近对账未完整完成"
            reason = str(row["last_error_code"] or "最近一次扫描为部分完成。")
        else:
            stopped = "超过两个对账周期"
            reason = "最近一次增量对账已超过约 65 分钟。"
        items.append({
            "id": opaque_item_id("chat", str(row["conversation_id"])),
            "category": "chat_uncovered",
            "severity": "error" if str(row["last_scan_status"]) == "failed" else "warning",
            "source_kind": "chat",
            "name": str(row["conversation_name"] or "未命名会话")[:180],
            "time": row["last_message_at"],
            "stage": stopped,
            "reason": reason,
            "next_condition": "下一次半小时增量对账，或点击“立即同步”。",
            "last_attempt": row["last_scan_at"],
            "stages": [
                stage("实时监听", "done"),
                stage("历史覆盖", "done" if row["last_complete_scan_at"] else "pending"),
                stage("增量对账", "error" if str(row["last_scan_status"]) == "failed" else "pending"),
                stage("长期记忆", "pending" if int(row["memory_pending"] or 0) else "done"),
            ],
        })
    return items


def attention_state(config: dict[str, Any]) -> dict[str, Any]:
    pssd = pssd_state(config)
    recordings, inventory = recording_attention(config, pssd)
    history = list(inventory.pop("history_items", []))
    chats = chat_attention(config)
    connection = syncv2.connect_readonly()
    try:
        incidents = [
            {
                "id": opaque_item_id("incident", str(row["incident_id"])),
                "category": "system_exception",
                "severity": "error",
                "source_kind": "system",
                "name": "需要处理的同步事件",
                "time": str(row["created_at"]),
                "stage": str(row["incident_type"]),
                "reason": "事件仍未精确匹配或人工关闭。",
                "next_condition": "找到精确任务指纹后自动恢复，或由你人工确认。",
                "last_attempt": str(row["updated_at"]),
                "stages": [],
            }
            for row in connection.execute("SELECT * FROM incidents WHERE status='open' ORDER BY created_at")
        ]
    finally:
        connection.close()
    items = recordings + chats + incidents
    order = {"unsynced": 0, "chat_uncovered": 1, "unreviewed": 2, "unverified": 3, "system_exception": 4}
    items.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
    items.sort(key=lambda item: (order.get(str(item["category"]), 9), 0 if item["severity"] == "error" else 1))
    counts = {key: sum(1 for item in items if item["category"] == key) for key in order}
    return {
        "generated_at": syncv2.now_iso(),
        "data_as_of": inventory.get("last_complete_at"),
        "remote_inventory": inventory,
        "counts": counts,
        "items": items,
        "history": history,
    }


def action_detail(run_id: str) -> dict[str, Any]:
    connection = syncv2.connect_readonly()
    try:
        row = connection.execute("SELECT * FROM action_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise DecisionError("action_missing", "这个任务记录不存在。", 404)
        outbox = connection.execute(
            "SELECT status,delivered_at,receipt_hash,last_error_code FROM outbox WHERE dedupe_key=?",
            (f"sync-cycle:{run_id}",),
        ).fetchone()
    finally:
        connection.close()
    result = json.loads(str(row["result_json"] or "{}"))
    notification = None
    if outbox:
        notification = {
            "status": str(outbox["status"]),
            "delivered_at": outbox["delivered_at"],
            "receipt_confirmed": bool(outbox["receipt_hash"]),
            "error_code": outbox["last_error_code"],
        }
    steps = syncv2.list_action_steps(run_id)
    if notification:
        for value in steps:
            if value["key"] != "notification":
                continue
            if notification["status"] == "delivered" and notification["receipt_confirmed"]:
                value["status"] = "success"
            elif notification["status"] == "failed":
                value["status"] = "failed"
                value["error_code"] = notification["error_code"]
            elif notification["status"] in {"pending", "uncertain"}:
                value["status"] = "pending"
    display_status = str(row["status"])
    if notification and str(row["status"]) in {"success", "partial"}:
        if notification["status"] in {"pending", "uncertain"}:
            display_status = "notification_pending"
        elif notification["status"] == "failed":
            display_status = "notification_failed"
    current_step = next((value for value in steps if value["status"] == "running"), None)
    if current_step is None:
        current_step = next(
            (value for value in steps if value["status"] in {"pending", "deferred"}),
            None,
        )
    if current_step is None:
        current_step = next(
            (value for value in reversed(steps) if value["status"] not in {"queued", "skipped"}),
            None,
        )
    stage = None
    if current_step:
        stage = {
            "key": str(current_step["key"]),
            "label": str(current_step["label"]),
            "status": str(current_step["status"]),
        }
    if current_step and isinstance(current_step.get("current"), int) and isinstance(current_step.get("total"), int):
        progress = {
            "current": int(current_step["current"]),
            "total": int(current_step["total"]),
            "unit": "items",
        }
    else:
        terminal = {"success", "noop", "partial", "failed", "skipped", "deferred", "interrupted"}
        progress = {
            "current": sum(1 for value in steps if value["status"] in terminal),
            "total": len(steps),
            "unit": "steps",
        }
    return {
        "run_id": str(row["run_id"]),
        "action_type": str(row["action_type"]),
        "target": str(row["action_type"]),
        "trigger": str(row["trigger"] or "unknown"),
        "status": str(row["status"]),
        "display_status": display_status,
        "stage": stage,
        "progress": progress,
        "started_at": str(row["started_at"]),
        "updated_at": str(row["updated_at"]),
        "completed_at": row["completed_at"],
        "error_code": row["error_code"],
        "result": result,
        "notification": notification,
        "steps": steps,
    }


def active_actions() -> list[dict[str, Any]]:
    connection = syncv2.connect_readonly()
    try:
        run_ids = [
            str(row["run_id"])
            for row in connection.execute(
                "SELECT run_id FROM action_runs WHERE status='running' ORDER BY started_at"
            )
        ]
    finally:
        connection.close()
    return [action_detail(run_id) for run_id in run_ids]


def activity_records(limit: int = 80) -> list[dict[str, Any]]:
    connection = syncv2.connect_readonly()
    try:
        actions = [
            {
                "kind": "action",
                "time": str(row["updated_at"]),
                "title": {
                    "sync_now": "手动同步",
                    "dingtalk_chat": "钉钉聊天同步",
                    "dingtalk_minutes": "钉钉 AI 听记同步",
                    "dingtalk_mail": "钉钉邮件同步",
                    "win11": "Win11 记忆同步",
                    "wechat": "微信记忆同步",
                    "all": "一键同步全部",
                    "review_now": "手动二查",
                    "idle_review": "锁屏二查",
                    "locked_review": "锁屏二查",
                    "decision_question": "决策提问",
                    "case_conversation": "问题工作台 AI 对话",
                    "decision_notify": "决策提醒",
                }.get(str(row["action_type"]), "后台任务"),
                "status": str(row["status"]),
                "detail": str(row["error_code"] or ""),
            }
            for row in connection.execute(
                "SELECT action_type,status,error_code,updated_at FROM action_runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        ]
        decisions = []
        for row in connection.execute(
            """
            SELECT decision_id,status,resolution,resolved_at FROM decisions
            WHERE resolved_at IS NOT NULL ORDER BY resolved_at DESC LIMIT ?
            """,
            (limit,),
        ):
            decisions.append({
                "kind": "decision",
                "decision_id": str(row["decision_id"]),
                "time": str(row["resolved_at"]),
                "title": "记忆决策",
                "status": str(row["status"]),
                "detail": str(row["resolution"] or ""),
            })
        deliveries = [
            {
                "kind": "notification",
                "time": str(row["updated_at"]),
                "title": (
                    "故障恢复通知" if "recover" in str(row["event_type"])
                    else ("同步故障通知" if "fail" in str(row["event_type"]) else "飞书同步回执")
                ),
                "status": str(row["status"]),
                "detail": "回执已确认" if row["receipt_hash"] else "回执待确认",
            }
            for row in connection.execute(
                "SELECT event_type,status,receipt_hash,updated_at FROM outbox ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        ]
    finally:
        connection.close()
    return sorted(actions + decisions + deliveries, key=lambda item: str(item["time"]), reverse=True)[:limit]


def list_decision_messages(decision_id: str) -> list[dict[str, Any]]:
    decision_row(decision_id)
    connection = syncv2.connect_readonly()
    try:
        return [
            {
                "message_id": str(row["message_id"]),
                "author": str(row["author"]),
                "kind": str(row["message_kind"]),
                "content": str(row["content"]),
                "status": str(row["status"]),
                "reply_to": row["reply_to"],
                "run_id": row["run_id"],
                "error_code": row["error_code"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in connection.execute(
                "SELECT * FROM decision_messages WHERE decision_id=? ORDER BY created_at,message_id",
                (decision_id,),
            )
        ]
    finally:
        connection.close()


def decision_draft(decision_id: str) -> dict[str, Any]:
    row = decision_row(decision_id)
    connection = syncv2.connect_readonly()
    try:
        draft = connection.execute("SELECT * FROM decision_drafts WHERE decision_id=?", (decision_id,)).fetchone()
    finally:
        connection.close()
    if not draft or int(draft["decision_revision"]) != int(row["revision"] or 1):
        return {"kind": "note", "content": "", "updated_at": None}
    return {"kind": str(draft["message_kind"]), "content": str(draft["content"]), "updated_at": str(draft["updated_at"])}


def save_decision_draft(decision_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = decision_row(decision_id)
    if str(row["status"]) not in {"pending", "deferred"}:
        raise DecisionError("decision_read_only", "这条决策已经完成，对话现为只读。", 409)
    kind = str(payload.get("kind") or "note")
    if kind not in {"note", "question"}:
        raise DecisionError("message_kind", "输入类型无效。")
    content = str(payload.get("content") or "")[:QUESTION_MAX_CHARS]
    stamp = syncv2.now_iso()
    connection = syncv2.connect()
    try:
        connection.execute(
            """
            INSERT INTO decision_drafts(decision_id,decision_revision,message_kind,content,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(decision_id) DO UPDATE SET
              decision_revision=excluded.decision_revision,message_kind=excluded.message_kind,
              content=excluded.content,updated_at=excluded.updated_at
            """,
            (decision_id, int(row["revision"] or 1), kind, content, stamp),
        )
        connection.commit()
    finally:
        connection.close()
    return {"kind": kind, "content": content, "updated_at": stamp}


def initialize_question_steps(run_id: str) -> None:
    for key, order, label in (
        ("question_prepare", 10, "整理本条决策上下文"),
        ("question_ai", 20, "调用 AI 回答"),
        ("question_answer", 30, "保存回答"),
    ):
        syncv2.set_action_step(run_id, key, order, label, "queued")


def append_decision_message(config_path: Path, decision_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = decision_row(decision_id)
    if str(row["status"]) not in {"pending", "deferred"}:
        raise DecisionError("decision_read_only", "这条决策已经完成，对话现为只读。", 409)
    kind = str(payload.get("kind") or "")
    if kind not in {"note", "question"}:
        raise DecisionError("message_kind", "请选择提问或备注。")
    content = "\n".join(line.rstrip() for line in str(payload.get("content") or "").splitlines()).strip()
    if not content:
        raise DecisionError("message_empty", "请输入内容。")
    if len(content) > QUESTION_MAX_CHARS:
        raise DecisionError("message_too_long", "内容最多 4000 字。")
    retry_of = str(payload.get("retry_of") or "") or None
    run_id = syncv2.start_action_run("decision_question") if kind == "question" else None
    if run_id:
        initialize_question_steps(run_id)
    stamp = syncv2.now_iso()
    message_id = str(uuid.uuid4())
    with syncv2.file_lock(syncv2.DECISION_LOCK, blocking=True):
        connection = syncv2.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT status,revision FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if not current or str(current["status"]) not in {"pending", "deferred"}:
                raise DecisionError("decision_read_only", "这条决策已经完成，对话现为只读。", 409)
            if retry_of:
                prior = connection.execute(
                    "SELECT message_kind,status FROM decision_messages WHERE message_id=? AND decision_id=?",
                    (retry_of, decision_id),
                ).fetchone()
                if not prior or str(prior["message_kind"]) != "question" or str(prior["status"]) != "failed":
                    raise DecisionError("retry_invalid", "只能重试失败的问题。", 409)
            connection.execute(
                """
                INSERT INTO decision_messages(
                  message_id,decision_id,decision_revision,author,message_kind,content,status,
                  reply_to,run_id,model_called,created_at,updated_at
                ) VALUES(?,?,?,'user',?,?,?,?,?,?,?,?)
                """,
                (
                    message_id,
                    decision_id,
                    int(current["revision"] or 1),
                    kind,
                    content,
                    "pending" if kind == "question" else "completed",
                    retry_of,
                    run_id,
                    1 if kind == "question" else 0,
                    stamp,
                    stamp,
                ),
            )
            connection.execute("DELETE FROM decision_drafts WHERE decision_id=?", (decision_id,))
            connection.execute("DELETE FROM decision_previews WHERE decision_id=?", (decision_id,))
            connection.commit()
        except Exception:
            connection.rollback()
            if run_id:
                syncv2.finish_action_run(run_id, "failed", error_code="message_save_failed")
            raise
        finally:
            connection.close()
    if kind == "question" and run_id:
        try:
            subprocess.Popen(
                [
                    "/usr/bin/python3",
                    str(Path(__file__).resolve()),
                    "--config",
                    str(config_path),
                    "--answer-question",
                    message_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                env={**os.environ, "LC_ALL": "en_US.UTF-8"},
            )
        except OSError as exc:
            connection = syncv2.connect()
            try:
                connection.execute(
                    "UPDATE decision_messages SET status='failed',error_code='question_worker_unavailable',updated_at=? WHERE message_id=?",
                    (syncv2.now_iso(), message_id),
                )
                connection.commit()
            finally:
                connection.close()
            syncv2.finish_action_run(run_id, "failed", error_code="question_worker_unavailable")
            raise DecisionError("question_worker_unavailable", "AI 提问任务无法启动，备注和记忆均未改变。", 503) from exc
    return next(item for item in list_decision_messages(decision_id) if item["message_id"] == message_id)


def decision_question_document(config: dict[str, Any], decision_id: str) -> dict[str, Any]:
    row = decision_row(decision_id)
    candidate = json.loads(str(row["candidate_json"]))
    related_ids = json.loads(str(row["related_memory_ids_json"]))
    memory_root = memory.runtime_paths(config)["memory_root"]
    by_id = {str(record["memory_id"]): record for record in memory.memory_records(memory_root)}
    related = []
    for memory_id in related_ids:
        record = by_id.get(str(memory_id))
        if not record:
            continue
        related.append({
            "title": str(record.get("title") or "关联记忆")[:180],
            "summary": str(record.get("summary") or "")[:1800],
            "content": section_text(str(record.get("body") or ""), "记忆内容", 4000),
            "observed_at": str(record.get("observed_at") or ""),
        })
    discussion = [
        {
            "author": item["author"],
            "kind": item["kind"],
            "content": item["content"],
            "status": item["status"],
        }
        for item in list_decision_messages(decision_id)
    ]
    return {
        "decision": {
            "title": str(candidate.get("title") or "记忆决策")[:180],
            "severity": str(row["severity"]),
            "source_kind": str(row["source_kind"]),
            "summary": str(candidate.get("summary") or "")[:2400],
            "candidate_content": str(candidate.get("content") or "")[:5000],
            "evidence_boundary": str(candidate.get("evidence_boundary") or "")[:1800],
            "conflict_reason": str(candidate.get("conflict_reason") or "")[:1800],
            "observed_at": str(candidate.get("observed_at") or ""),
            "related_memories": related,
        },
        "discussion": discussion,
    }


def invoke_decision_question(config: dict[str, Any], document: dict[str, Any], run_id: str) -> dict[str, Any]:
    automation = config["dingtalk_automation"]
    # No codex precondition: model_router calls MiMo and Kimi directly, so a
    # missing or hung codex only removes the GPT fallback.
    if not QUESTION_PROMPT.is_file() or not QUESTION_SCHEMA.is_file():
        raise DecisionError("question_contract_missing", "AI 提问规则文件缺失。", 503)
    run_dir = syncv2.RUNS_DIR / "decision-questions" / run_id
    syncv2.ensure_private_dir(run_dir)
    prompt = QUESTION_PROMPT.read_text(encoding="utf-8").rstrip() + "\n\n<decision_context>\n" + syncv2.canonical_json(document) + "\n</decision_context>\n"
    import model_router
    prompt += memory.mimo_schema_prompt(QUESTION_SCHEMA)
    lock = memory.acquire_lock(syncv2.AI_LOCK, blocking=True)
    try:
        try:
            routed = model_router.complete(config, prompt=prompt, purpose="decision_question", workdir=run_dir,
                                           request_context={"scope":"decision-question"},
                                           timeout=int(automation.get("codex_timeout_seconds") or 900),
                                           validate=memory.parse_model_json)
        except model_router.RouterExhausted as exc:
            if exc.category == "timeout":
                raise DecisionError("codex_timeout", "AI 回答超时，没有修改记忆。", 503) from exc
            raise DecisionError(model_router.legacy_error_code(exc), "AI 暂时没有完成回答，没有修改记忆。", 503) from exc
    finally:
        lock.close()
    payload = routed.value
    answer = str(payload.get("answer") or "").strip()
    limitations = payload.get("limitations") or []
    if not answer or len(answer) > 8000 or not isinstance(limitations, list):
        raise DecisionError("codex_invalid_answer", "AI 回答内容无效，没有修改记忆。", 503)
    limitations = [str(value).strip()[:500] for value in limitations if str(value).strip()][:8]
    if limitations:
        answer += "\n\n回答边界：\n" + "\n".join(f"- {value}" for value in limitations)
    return {"answer": answer}


def answer_decision_question(config: dict[str, Any], message_id: str) -> dict[str, Any]:
    connection = syncv2.connect_readonly()
    try:
        question = connection.execute(
            "SELECT * FROM decision_messages WHERE message_id=? AND message_kind='question'",
            (message_id,),
        ).fetchone()
    finally:
        connection.close()
    if not question:
        raise DecisionError("question_missing", "问题不存在。", 404)
    run_id = str(question["run_id"] or "")
    decision_id = str(question["decision_id"])
    try:
        syncv2.set_action_step(run_id, "question_prepare", 10, "整理本条决策上下文", "running")
        document = decision_question_document(config, decision_id)
        syncv2.set_action_step(run_id, "question_prepare", 10, "整理本条决策上下文", "success")
        syncv2.set_action_step(run_id, "question_ai", 20, "调用 AI 回答", "running")
        response = invoke_decision_question(config, document, run_id)
        syncv2.set_action_step(run_id, "question_ai", 20, "调用 AI 回答", "success")
        syncv2.set_action_step(run_id, "question_answer", 30, "保存回答", "running")
        stamp = syncv2.now_iso()
        answer_id = str(uuid.uuid4())
        with syncv2.file_lock(syncv2.DECISION_LOCK, blocking=True):
            connection = syncv2.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO decision_messages(
                      message_id,decision_id,decision_revision,author,message_kind,content,status,
                      reply_to,run_id,model_called,created_at,updated_at
                    ) VALUES(?,?,?,'assistant','answer',?,'completed',?,?,1,?,?)
                    """,
                    (
                        answer_id,
                        decision_id,
                        int(question["decision_revision"]),
                        response["answer"],
                        message_id,
                        run_id,
                        stamp,
                        stamp,
                    ),
                )
                connection.execute(
                    "UPDATE decision_messages SET status='completed',updated_at=?,error_code=NULL WHERE message_id=?",
                    (stamp, message_id),
                )
                connection.execute("DELETE FROM decision_previews WHERE decision_id=?", (decision_id,))
                connection.commit()
            finally:
                connection.close()
        syncv2.set_action_step(run_id, "question_answer", 30, "保存回答", "success")
        syncv2.finish_action_run(run_id, "success", result={"answered": 1})
        return {"status": "success", "message_id": answer_id}
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__))
        connection = syncv2.connect()
        try:
            connection.execute(
                "UPDATE decision_messages SET status='failed',error_code=?,updated_at=? WHERE message_id=?",
                (code, syncv2.now_iso(), message_id),
            )
            connection.commit()
        finally:
            connection.close()
        if run_id:
            syncv2.finish_action_run(run_id, "failed", error_code=code)
        raise


def minutes_operational_state(config: dict[str, Any]) -> dict[str, Any]:
    sync_state = read_json_file(MINUTES_SYNC_STATE, {})
    entries = sync_state.get("entries") if isinstance(sync_state, dict) else {}
    if not isinstance(entries, dict):
        entries = {}
    ledger = read_json_file(REVIEW_LEDGER, {})
    if not isinstance(ledger, dict):
        ledger = {}
    storage = {"local": 0, "pssd": 0, "pssd_corrupt": 0, "missing": 0}
    for task_uuid, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("media_storage") or "local")
        if kind not in storage:
            kind = "missing"
        storage[kind] += 1
    cutover = datetime.fromisoformat(str(config["dingtalk_minutes_memory"]["cutover_at"]))
    review_counts = review_policy.aggregate(
        entries,
        ledger,
        review_policy.load_recording_rows(),
        cutover,
    )
    return {
        "accessible": len(entries),
        "storage": storage,
        **review_counts,
        "last_sync_at": sync_state.get("last_sync_at") if isinstance(sync_state, dict) else None,
    }


def chat_operational_state(config: dict[str, Any]) -> dict[str, Any]:
    database = memory.runtime_paths(config)["database"]
    if not database.is_file():
        return {"known": 0, "historically_complete": 0, "currently_covered": 0, "pending_history": 0, "pending_reconciliation": 0, "messages": 0}
    uri = f"file:{database}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS known,
              SUM(CASE WHEN last_complete_scan_at IS NOT NULL THEN 1 ELSE 0 END) AS complete
            FROM conversations
            """
        ).fetchone()
        messages = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        known = int(row["known"] or 0)
        complete = int(row["complete"] or 0)
        current = datetime.now().astimezone()
        covered = 0
        for conversation in connection.execute(
            "SELECT last_scan_at,last_complete_scan_at,last_scan_status FROM conversations"
        ):
            scanned = parse_timestamp(conversation["last_scan_at"])
            if (
                conversation["last_complete_scan_at"]
                and str(conversation["last_scan_status"]) == "complete"
                and scanned
                and (current - scanned).total_seconds() <= 65 * 60
            ):
                covered += 1
        return {
            "known": known,
            "historically_complete": complete,
            "currently_covered": covered,
            "pending_history": max(0, known - complete),
            "pending_reconciliation": max(0, known - covered),
            "messages": messages,
        }
    finally:
        connection.close()


def latest_component_recovery(
    connection: sqlite3.Connection,
    component: str,
) -> Optional[dict[str, Any]]:
    for row in connection.execute(
        """
        SELECT status,created_at,delivered_at,safe_payload_json
        FROM outbox WHERE event_type='sync_recovery'
        ORDER BY created_at DESC
        """
    ):
        try:
            payload = json.loads(str(row["safe_payload_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or str(payload.get("component") or "") != component:
            continue
        return {
            "component": component,
            "error_code": str(payload.get("recovered_from_error_code") or "unknown"),
            "recovered_at": str(row["created_at"]),
            "delivery_status": str(row["status"]),
            "delivered_at": row["delivered_at"],
        }
    return None


def read_only_state(config: dict[str, Any]) -> dict[str, Any]:
    decisions = list_decisions(config)
    attention = attention_state(config)
    try:
        work_counts = reconciliation.work_counts(history_extra=len(attention.get("history") or []))
    except reconciliation.ReconciliationError:
        fallback = reconciliation.plan_reconciliation(config)["counts"]
        work_counts = {
            "needs_user": int(fallback["needs_user_cases"]),
            "automatic": int(fallback["automatic_cases"]),
            "resolved_7d": 0,
            "archived_questions": 0,
            "history": len(attention.get("history") or []),
        }
    connection = syncv2.connect_readonly()
    try:
        incidents = [
            {
                "incident_type": str(row["incident_type"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "detail": json.loads(str(row["safe_detail_json"])),
            }
            for row in connection.execute(
                "SELECT incident_type,created_at,updated_at,safe_detail_json FROM incidents WHERE status='open' ORDER BY created_at"
            )
        ]
        latest_delivery = connection.execute(
            """
            SELECT event_type,delivered_at,receipt_hash FROM outbox
            WHERE status='delivered' ORDER BY delivered_at DESC LIMIT 1
            """
        ).fetchone()
        latest_actions: dict[str, dict[str, Any]] = {}
        latest_manual_actions: dict[str, dict[str, Any]] = {}
        unknown_trigger_action_types: set[str] = set()
        for row in connection.execute(
            "SELECT * FROM action_runs ORDER BY started_at DESC"
        ):
            action_type = str(row["action_type"])
            trigger = str(row["trigger"] or "unknown")
            item = {
                "action_type": str(row["action_type"]),
                "run_id": str(row["run_id"]),
                "trigger": trigger,
                "status": str(row["status"]),
                "started_at": str(row["started_at"]),
                "completed_at": row["completed_at"],
                "error_code": row["error_code"],
                "result": json.loads(str(row["result_json"] or "{}")),
            }
            latest_actions.setdefault(action_type, item)
            if trigger == "manual":
                latest_manual_actions.setdefault(action_type, item)
            elif trigger == "unknown":
                unknown_trigger_action_types.add(action_type)
        latest_manual_sync_recovery = latest_component_recovery(connection, "manual_sync")
    finally:
        connection.close()
    return {
        "generated_at": syncv2.now_iso(),
        "maintenance": maintenance_status(),
        "decisions": decisions,
        "decision_counts": {
            category: sum(1 for item in decisions if item["severity"] == category)
            for category in ("conflict", "high_risk")
        },
        "work_counts": work_counts,
        "sync": syncv2.status_snapshot(),
        "latest_actions": latest_actions,
        "latest_manual_actions": latest_manual_actions,
        "unknown_trigger_action_types": sorted(unknown_trigger_action_types),
        "latest_manual_sync_recovery": latest_manual_sync_recovery,
        "active_actions": active_actions(),
        "minutes": minutes_operational_state(config),
        "chat": chat_operational_state(config),
        "pssd": pssd_state(config),
        "open_incidents": incidents,
        "attention_counts": attention["counts"],
        "attention_data_as_of": attention["data_as_of"],
        "activity": activity_records(30),
        "preferences": preferences(),
        "latest_feishu_delivery": (
            {
                "event_type": str(latest_delivery["event_type"]),
                "delivered_at": str(latest_delivery["delivered_at"]),
                "receipt_confirmed": bool(latest_delivery["receipt_hash"]),
            }
            if latest_delivery
            else None
        ),
    }


def review_process_present_in_scan(output: str, ignore_pids: set[int] | None = None) -> bool:
    ignored = set(ignore_pids or set())
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)", line)
        if not match or int(match.group(1)) in ignored:
            continue
        command = match.group(2)
        if any(marker in command for marker in (
            "/bin/dingtalk_idle_review.py",
            "/核对/.pipeline/review_dispatch.py",
            "/核对/.pipeline/run_review.sh",
            "/bin/dingtalk_review_reconcile.py",
            "/transcription-review/scripts/transcribe_dashscope.py",
        )) or ("codex exec" in command and "/DingTalk-AI-Minutes.nosync/核对/" in command):
            return True
    return False


def recover_stale_action_runs(ignore_review_pids: set[int] | None = None) -> int:
    import action_run_owner
    action_run_owner.recover(ROOT)
    ledger = read_json_file(REVIEW_LEDGER, {})
    ledger = ledger if isinstance(ledger, dict) else {}
    sync_state = read_json_file(MINUTES_SYNC_STATE, {})
    sync_entries = sync_state.get("entries", {}) if isinstance(sync_state, dict) else {}
    live_review_run_ids = {
        str(row.get("action_run_id"))
        for row in ledger.values()
        if isinstance(row, dict)
        and row.get("status") == "in_progress"
        and row.get("action_run_id")
        and review_policy.process_alive(row.get("worker_pid"))
    }
    review_process_present = False
    try:
        process_scan = subprocess.run(
            ["/bin/ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        review_process_present = process_scan.returncode == 0 and review_process_present_in_scan(
            process_scan.stdout,
            ignore_review_pids,
        )
    except (OSError, subprocess.TimeoutExpired):
        # A failed process scan must not manufacture an interruption.
        review_process_present = True
    ledger_changed = False
    if not review_process_present:
        for task_uuid, row in ledger.items():
            if (
                isinstance(row, dict)
                and row.get("status") == "in_progress"
                and not review_policy.process_alive(row.get("worker_pid"))
            ):
                entry = sync_entries.get(task_uuid) if isinstance(sync_entries, dict) else None
                resumable = review_policy.resumable_artifacts(
                    entry or {},
                    review_root=MINUTES_ROOT / "核对",
                ) if isinstance(entry, dict) else "none"
                row.update({
                    "status": "interrupted",
                    "reason": "review_worker_missing",
                    "resume_artifact": resumable,
                    "manual_intervention_required": resumable == "none",
                    "worker_pid": None,
                    "interrupted_at": syncv2.now_iso(),
                    "updated_at": syncv2.now_iso(),
                })
                ledger_changed = True
    if ledger_changed:
        memory.atomic_write(
            REVIEW_LEDGER,
            json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
            mode=0o600,
        )
    connection = syncv2.connect()
    try:
        stamp = syncv2.now_iso()
        running_reviews = list(connection.execute(
            "SELECT run_id,started_at FROM action_runs WHERE status='running' "
            "AND action_type IN ('review_now','locked_review','idle_review') ORDER BY started_at",
        ))
        if review_process_present and running_reviews and not live_review_run_ids:
            # Before the dispatcher claims a ledger row, the newest action is
            # represented only by its still-live launcher process.
            live_review_run_ids.add(str(running_reviews[-1]["run_id"]))
        interrupted_review_ids = [
            str(row["run_id"])
            for row in running_reviews
            if str(row["run_id"]) not in live_review_run_ids
        ]
        recovered = 0
        if interrupted_review_ids:
            placeholders = ",".join("?" for _ in interrupted_review_ids)
            cursor = connection.execute(
                f"""
                UPDATE action_runs SET status='interrupted',
                  error_code='interrupted_after_process_exit',completed_at=?,updated_at=?
                WHERE status='running' AND run_id IN ({placeholders})
                """,
                (stamp, stamp, *interrupted_review_ids),
            )
            recovered += int(cursor.rowcount)
            connection.execute(
                f"""
                UPDATE action_steps SET
                  status=CASE WHEN status='running' THEN 'interrupted' ELSE 'skipped' END,
                  error_code=CASE WHEN status='running'
                    THEN 'interrupted_after_process_exit' ELSE error_code END,
                  completed_at=COALESCE(completed_at,?),updated_at=?
                WHERE run_id IN ({placeholders}) AND status IN ('running','queued','pending')
                """,
                (stamp, stamp, *interrupted_review_ids),
            )
        threshold = (datetime.now().astimezone() - timedelta(hours=12)).isoformat(timespec="seconds")
        cursor = connection.execute(
            """
            UPDATE action_runs SET status='interrupted',error_code='interrupted_after_timeout',
              completed_at=?,updated_at=?
            WHERE status='running' AND action_type NOT IN ('review_now','locked_review','idle_review',
              'dingtalk_chat','dingtalk_minutes','dingtalk_mail','win11','wechat','all','dingtalk_all')
              AND started_at<?
            """,
            (stamp, stamp, threshold),
        )
        recovered += int(cursor.rowcount)
        connection.execute(
            """
            UPDATE action_steps SET
              status=CASE WHEN status='running' THEN 'interrupted' ELSE 'skipped' END,
              error_code=CASE WHEN status='running' THEN 'interrupted_after_timeout' ELSE error_code END,
              completed_at=COALESCE(completed_at,?),updated_at=?
            WHERE run_id IN (
              SELECT run_id FROM action_runs WHERE status='interrupted'
                AND error_code='interrupted_after_timeout' AND completed_at=?
            ) AND status IN ('running','queued','pending')
            """,
            (stamp, stamp, stamp),
        )
        connection.commit()
        return recovered
    finally:
        connection.close()


def control_lock_available() -> bool:
    CONTROL_LOCK.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(CONTROL_LOCK, flags, 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return True
        except BlockingIOError:
            return False
    finally:
        os.close(descriptor)


@contextmanager
def control_start_guard():
    CONTROL_START_LOCK.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(CONTROL_START_LOCK, flags, 0o600)
    except OSError as exc:
        raise DecisionError("control_lock_unavailable", "统一控制锁不可用。", 503) from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def start_control_action(config_path: Path, target: str, *, requested_run_id: Optional[str] = None) -> dict[str, Any]:
    allowed = {"dingtalk_chat", "dingtalk_minutes", "dingtalk_mail", "win11", "wechat", "all"}
    internal = target == "dingtalk_all"
    if target not in allowed and not internal:
        raise DecisionError("target_invalid", "同步来源无效。")
    if maintenance_active():
        return {"status": "deferred", "reason": "maintenance_active"}
    recover_stale_action_runs()
    with control_start_guard():
        connection = syncv2.connect_readonly()
        try:
            if requested_run_id:
                prior = connection.execute("SELECT run_id,status FROM action_runs WHERE run_id=?", (requested_run_id,)).fetchone()
                if prior:
                    return {"status": "already_running" if prior["status"] == "running" else prior["status"], "run_id": prior["run_id"], "target": target, "idempotent_replay": True}
            row = connection.execute(
                """
                SELECT run_id,action_type FROM action_runs
                WHERE status='running'
                ORDER BY started_at LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
        if row:
            return {
                "status": "already_running",
                "run_id": str(row["run_id"]),
                "target": str(row["action_type"]),
            }
        if not control_lock_available():
            return {"status": "already_running", "reason": "control_lock_busy"}
        action_type = "sync_now" if internal else target
        run_id = syncv2.start_action_run(action_type, requested_run_id=requested_run_id)
        command = [
            "/usr/bin/python3",
            str(SYNC_CONTROLLER),
            "--config",
            str(config_path),
            "--run-id",
            run_id,
            target,
        ]
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                env={**os.environ, "LC_ALL": "en_US.UTF-8"},
            )
        except OSError as exc:
            syncv2.finish_action_run(run_id, "failed", error_code="controller_unavailable")
            raise DecisionError("controller_unavailable", "同步任务无法启动。", 503) from exc
        return {"status": "started", "run_id": run_id, "target": target, "local_only": True}


def control_action_gate(system_state: dict[str, Any], target: str) -> dict[str, Any] | None:
    """Apply the dashboard's fail-closed write eligibility contract at the API boundary."""
    allowed = {"dingtalk_chat", "dingtalk_minutes", "dingtalk_mail", "win11", "wechat", "all", "dingtalk_all"}
    if target not in allowed:
        raise DecisionError("target_invalid", "同步来源无效。")
    if not isinstance(system_state, dict):
        raise DecisionError("control_state_unknown", "无法确认当前是否可安全同步。", 503)
    if target in {"all", "dingtalk_all"}:
        control = system_state.get("global_action")
    else:
        source = next(
            (item for item in system_state.get("sources") or [] if isinstance(item, dict) and item.get("id") == target),
            None,
        )
        control = source.get("control") if isinstance(source, dict) else None
    if not isinstance(control, dict) or not isinstance(control.get("available"), bool):
        raise DecisionError("control_state_unknown", "无法确认当前是否可安全同步。", 503)
    run_id = str(control.get("run_id") or "")
    if control.get("running") is True or control.get("reason_code") == "already_running":
        if run_id:
            return {"status": "already_running", "run_id": run_id, "target": target, "local_only": True}
        raise DecisionError("already_running", str(control.get("reason") or "已有同步正在运行。"), 409)
    if control.get("available") is not True:
        raise DecisionError(
            str(control.get("reason_code") or "control_unavailable"),
            str(control.get("reason") or "当前不满足安全同步条件。"),
            409,
        )
    return None


def current_control_state(app: Any) -> dict[str, Any]:
    try:
        return app.system_dashboard.collect(
            app.config,
            read_only_state(app.config),
            force=True,
        )
    except DecisionError:
        raise
    except Exception as exc:
        raise DecisionError(
            "control_state_unknown", "无法确认当前是否可安全同步。", 503,
        ) from exc


def start_background_action(config_path: Path, action_type: str) -> dict[str, Any]:
    if maintenance_active():
        return {"status": "deferred", "reason": "maintenance_active"}
    if action_type == "sync_now":
        return start_control_action(config_path, "dingtalk_all")
    elif action_type == "review_now":
        command = [
            "/usr/bin/python3", str(REVIEW_RUNNER), "--config", str(config_path), "--run-id", "{run_id}", "--manual"
        ]
    else:
        raise DecisionError("action_unknown", "不支持这个后台动作。", 404)
    recover_stale_action_runs()
    connection = syncv2.connect_readonly()
    try:
        running = connection.execute(
            "SELECT run_id FROM action_runs WHERE action_type=? AND status='running' ORDER BY started_at DESC LIMIT 1",
            (action_type,),
        ).fetchone()
        placeholders = ",".join("?" for _ in syncv2.CONTROL_ACTION_TYPES)
        sync_running = connection.execute(
            f"SELECT run_id FROM action_runs WHERE action_type IN ({placeholders}) "
            "AND status='running' ORDER BY started_at DESC LIMIT 1",
            tuple(sorted(syncv2.CONTROL_ACTION_TYPES)),
        ).fetchone()
    finally:
        connection.close()
    if running:
        return {"status": "already_running", "run_id": str(running["run_id"])}
    if action_type == "review_now" and sync_running:
        return {"status": "deferred", "reason": "manual_sync_priority", "run_id": str(sync_running["run_id"])}
    run_id = syncv2.start_action_run(action_type)
    command = [run_id if value == "{run_id}" else value for value in command]
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "LC_ALL": "en_US.UTF-8"},
        )
    except OSError as exc:
        syncv2.finish_action_run(run_id, "failed", error_code="orchestrator_unavailable")
        raise DecisionError("orchestrator_unavailable", "同步任务无法启动。", 503) from exc
    return {"status": "started", "run_id": run_id}


def reconciliation_scope_payload(config: dict[str, Any], scope: str) -> dict[str, Any]:
    payload = reconciliation.list_cases(scope)
    if scope not in {"automatic", "history"}:
        return payload
    attention = attention_state(config)
    source = attention.get("items") if scope == "automatic" else attention.get("history")
    if scope == "automatic":
        raw_cases = list(payload["items"])
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in raw_cases:
            key = (str(item.get("dimension") or "事实核验"), str(item.get("case_kind") or "fact_resolution"))
            grouped.setdefault(key, []).append(item)
        automatic_items = []
        state_priority = {"failed": 4, "running": 3, "blocked": 2, "queued": 1, "idle": 0}
        for (dimension, kind), rows in sorted(grouped.items()):
            processing = max(
                (str(item.get("processing_state") or "idle") for item in rows),
                key=lambda value: state_priority.get(value, 0),
            )
            updated = max((str(item.get("updated_at") or "") for item in rows), default="")
            digest = hashlib.sha256(f"{dimension}|{kind}".encode("utf-8")).hexdigest()[:16]
            automatic_items.append({
                "case_id": f"automatic-group-{digest}",
                "title": f"{dimension} · {len(rows)} 个主题",
                "subject": "证据收敛",
                "dimension": dimension,
                "case_kind": "automatic_group",
                "state": "open",
                "processing_state": processing,
                "assessment": f"系统正在持续观察 {len(rows)} 个相关业务主题；这里显示聚合进度，不要求逐条浏览。",
                "evidence_summary": f"底层共 {len(rows)} 个 ReviewCase，原始证据保留在审计层。",
                "evidence_boundary": "聚合状态不确认、推翻或覆盖任何业务事实。",
                "manual_reason": None,
                "decision_question": None,
                "allowed_choices": [],
                "policy_version": reconciliation.POLICY_VERSION,
                "context_hash": "",
                "case_version": max(int(item.get("case_version") or 1) for item in rows),
                "updated_at": updated,
                "resolved_at": None,
                "next_action": "收到相关聊天、听记、邮件或资料后自动再次核验。",
                "underlying_count": len(rows),
            })

        operational_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in source or []:
            processing = "failed" if item.get("severity") == "error" else "queued"
            operational_groups.setdefault((str(item.get("source_kind") or "system"), processing), []).append(item)
        operational_names = {
            "recording": "钉钉听记流程",
            "chat": "钉钉聊天流程",
            "system": "本机同步流程",
        }
        for (source_kind, processing), rows in sorted(operational_groups.items()):
            digest = hashlib.sha256(f"operation|{source_kind}|{processing}".encode("utf-8")).hexdigest()[:16]
            next_steps = list(dict.fromkeys(str(item.get("next_condition") or "") for item in rows if item.get("next_condition")))
            updated = max(
                (str(item.get("time") or item.get("last_attempt") or "") for item in rows),
                default="",
            )
            automatic_items.append({
                "case_id": f"automatic-operation-{digest}",
                "title": f"{operational_names.get(source_kind, '本机同步流程')} · {len(rows)} 项",
                "subject": "运行状态",
                "dimension": "后台处理",
                "case_kind": "automatic_operation_group",
                "state": "open",
                "processing_state": processing,
                "assessment": (
                    f"已把 {len(rows)} 条后台状态聚合显示；异常细节保留在系统诊断与运行记录中。"
                ),
                "evidence_summary": f"聚合 {len(rows)} 条运行状态，不参与“需要我”徽标。",
                "evidence_boundary": "这是运行状态，不会升级为业务事实。",
                "manual_reason": None,
                "decision_question": None,
                "allowed_choices": [],
                "policy_version": reconciliation.POLICY_VERSION,
                "context_hash": "",
                "case_version": 1,
                "updated_at": updated,
                "resolved_at": None,
                "next_action": next_steps[0] if len(next_steps) == 1 else "系统会按各自条件继续推进；异常可在系统区域查看。",
                "underlying_count": len(rows),
            })
        return {
            **payload,
            "items": automatic_items,
            "count": len(raw_cases),
            "group_count": len(automatic_items),
            "operational_count": len(source or []),
        }

    operational = []
    for item in source or []:
        operational.append({
            "case_id": str(item.get("id") or ""),
            "title": str(item.get("name") or "自动处理事项")[:180],
            "subject": "钉钉与本机同步" if item.get("source_kind") != "system" else "系统状态",
            "dimension": str(item.get("stage") or "运行状态")[:120],
            "case_kind": "operational_history" if scope == "history" else "automatic_operation",
            "state": "archived" if scope == "history" else "open",
            "processing_state": "idle" if scope == "history" else (
                "failed" if item.get("severity") == "error" else "queued"
            ),
            "assessment": str(item.get("reason") or "")[:600],
            "evidence_summary": str(item.get("stage") or "")[:300],
            "evidence_boundary": "这是运行或历史状态，不会升级为业务事实，也不计入“需要我”。",
            "manual_reason": None,
            "decision_question": None,
            "allowed_choices": [],
            "policy_version": reconciliation.POLICY_VERSION,
            "context_hash": "",
            "case_version": 1,
            "updated_at": str(item.get("time") or item.get("last_attempt") or ""),
            "resolved_at": str(item.get("time") or "") if scope == "history" else None,
            "next_action": str(item.get("next_condition") or "")[:500],
            "stages": item.get("stages") or [],
        })
    payload["items"].extend(operational)
    payload["count"] = len(payload["items"])
    return payload


API_LOG_LOCK = threading.Lock()


def log_api_error(exc: BaseException, method: str, path: str) -> str:
    request_id = uuid.uuid4().hex[:12]
    try:
        logger = logging.getLogger("sync-center-api-errors")
        with API_LOG_LOCK:
            if not logger.handlers:
                log_dir = ROOT / "state" / "logs"
                log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                log_file = log_dir / "api-errors.jsonl"
                handler = RotatingFileHandler(log_file, maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
                logger.addHandler(handler)
                logger.setLevel(logging.ERROR)
                logger.propagate = False
        # No request body, query, headers, credentials or exception text is logged.
        route = re.sub(r"case-[0-9a-f]{20}|[0-9a-fA-F-]{36}", ":id", urlparse(path).path)[:180]
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(exc.__traceback__)[-8:]]
        logger.error(json.dumps({"request_id": request_id, "at": syncv2.now_iso(), "method": method,
                                 "route": route, "exception": type(exc).__name__, "frames": frames}))
    except Exception:
        # A full or unavailable log directory must not disconnect the error response.
        pass
    return request_id


class DecisionHandler(SimpleHTTPRequestHandler):
    server_version = "BUSINESSMemorySyncCenter/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def app(self) -> "DecisionServer":
        return self.server  # type: ignore[return-value]

    def security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")

    def authorized(self) -> bool:
        cookies = self.headers.get("Cookie", "")
        expected_host = urlparse(self.app.origin).netloc
        return (
            self.client_address[0] == "127.0.0.1"
            and self.headers.get("Host", "") == expected_host
            and f"dingtalk_sync_session={self.app.session_token}" in cookies
        )

    def json_response(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.security_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parsed = parsed._replace(path=re.sub(r"^/api/workbench/business-(case-[0-9a-f]{20})(?=/|$)", r"/api/reconciliation/\1", parsed.path))
        if self.client_address[0] != "127.0.0.1" or self.headers.get("Host", "") != urlparse(self.app.origin).netloc:
            self.json_response(403, {"error_code": "host_rejected"})
            return
        if parsed.path == "/":
            template = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
            body = template.replace("__CSRF_TOKEN__", self.app.csrf_token).encode("utf-8")
            self.send_response(200)
            self.security_headers("text/html; charset=utf-8")
            self.send_header("Set-Cookie", f"dingtalk_sync_session={self.app.session_token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in {"/app.css", "/app.js", "/alert-workbench.js", "/workbench.js", "/status-contract.js", "/codex-handoff.js", "/content-relations.js", "/favicon.svg", "/model-usage.js", "/model-usage.css", "/alerts-overview.js", "/alerts-overview.css"}:
            path = STATIC_ROOT / parsed.path.lstrip("/")
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            content_type = {
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml; charset=utf-8",
            }[path.suffix]
            self.security_headers(content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.authorized():
            self.json_response(403, {"error_code": "unauthorized"})
            return
        try:
            if parsed.path == "/model-usage":
                body = (STATIC_ROOT / "model-usage.html").read_bytes()
                self.send_response(200); self.security_headers("text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif parsed.path == "/alerts-overview":
                body = (STATIC_ROOT / "alerts-overview.html").read_bytes()
                self.send_response(200); self.security_headers("text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif parsed.path == "/api/alerts-overview":
                import alerts_overview
                self.json_response(200, alerts_overview.collect(self.app.config, maintenance=maintenance_active()))
            elif parsed.path == "/api/model-usage":
                import model_usage, model_router
                query = parse_qs(parsed.query)
                try:
                    data = model_usage.filtered(model_router.STATE_DIR / 'model-calls',
                        (query.get('date') or [None])[0], (query.get('purpose') or [None])[0], (query.get('hour') or [None])[0])
                except ValueError:
                    self.json_response(400, {'error_code':'usage_filter_invalid'}); return
                if query.get('format') == ['csv']:
                    import model_call_ledger
                    body = ('\ufeff' + model_call_ledger.export_csv(data)).encode('utf-8')
                    self.send_response(200); self.security_headers('text/csv; charset=utf-8')
                    self.send_header('Content-Disposition','attachment; filename="model-usage.csv"')
                    self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
                else:
                    self.json_response(200,data)
            elif parsed.path == "/api/content-relations":
                query=parse_qs(parsed.query)
                try: limit,offset=int(query.get('limit',['50'])[0]),int(query.get('offset',['0'])[0])
                except ValueError: self.json_response(400,{'error_code':'pagination_invalid'});return
                self.json_response(200,content_registry.listing(self.app.config,limit,offset))
            elif re.fullmatch(r'/api/content-relations/[0-9a-f]{64}',parsed.path):
                result=content_registry.listing(self.app.config,identifier=parsed.path.rsplit('/',1)[-1])
                self.json_response(200 if result['items'] else 404,result)
            elif parsed.path == "/api/state":
                self.json_response(200, read_only_state(self.app.config))
            elif parsed.path == "/api/system-state":
                query = parse_qs(parsed.query)
                snapshot = read_only_state(self.app.config)
                self.json_response(
                    200,
                    self.app.system_dashboard.collect(
                        self.app.config,
                        snapshot,
                        force=query.get("force") == ["1"],
                    ),
                )
            elif parsed.path == "/api/system-events":
                query = parse_qs(parsed.query)
                pipeline = str((query.get("pipeline") or [""])[0] or "") or None
                try:
                    limit = int((query.get("limit") or ["50"])[0])
                except ValueError as exc:
                    raise DecisionError("limit_invalid", "活动数量参数无效。") from exc
                try:
                    payload = self.app.system_dashboard.events(
                        self.app.config,
                        read_only_state(self.app.config),
                        pipeline=pipeline,
                        limit=limit,
                    )
                except ValueError as exc:
                    raise DecisionError("pipeline_invalid", "流程筛选无效。") from exc
                self.json_response(200, payload)
            elif (match := codex_handoff.ROUTE.fullmatch(parsed.path)) and match.group(2) == "handoff":
                self.json_response(200, self.app.handoffs.status(match.group(1)))
            elif (match := codex_handoff.ROUTE.fullmatch(parsed.path)) and match.group(2) == "handoff/context":
                self.json_response(200, codex_handoff.preview(self.app.config, match.group(1)))
            elif re.fullmatch(r"/api/media/[0-9a-f]{24}/frames/[0-9]+",parsed.path):
                import media_processor
                parts=parsed.path.split("/")
                try: body=media_processor.serve_frame(parts[3],int(parts[5]))
                except (OSError,ValueError,KeyError,IndexError):
                    self.json_response(404,{"error_code":"media_frame_unavailable"});return
                self.send_response(200)
                self.security_headers("image/jpeg")
                self.send_header("Cache-Control","no-store")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers();self.wfile.write(body)
            elif parsed.path == "/api/workbench":
                query = parse_qs(parsed.query)
                try:
                    limit, offset = int((query.get("limit") or ["200"])[0]), int((query.get("offset") or ["0"])[0])
                except ValueError as exc:
                    raise DecisionError("limit_invalid", "问题列表分页参数无效。") from exc
                self.json_response(200, operational_workbench.listing(
                    self.app.config, scope=(query.get("scope") or ["all"])[0],
                    limit=limit, offset=offset,
                    issue_ids=set(query["ids"][0].split(",")) if query.get("ids") else None))
            elif re.fullmatch(r"/api/workbench/(?:issue|group)-[0-9a-f]{24}/conversation", parsed.path):
                identifier = parsed.path.split("/")[3]
                result = alert_workbench.conversation(identifier)
                result["legacy_conversations"] = operational_workbench.legacy_conversation(identifier)
                self.json_response(200, result)
            elif re.fullmatch(r"/api/workbench/(?:issue|group)-[0-9a-f]{24}", parsed.path):
                identifier = parsed.path.split("/")[3]
                media_row = operational_workbench.raw_group(identifier) if identifier.startswith("group-") else operational_workbench.raw_issue(identifier)
                is_media = media_row.get("kind_family",media_row.get("kind")) == "video_hold"
                try:
                    control = None if is_media else current_control_state(self.app)
                except DecisionError:
                    control = None
                self.json_response(200, (
                    operational_workbench.group_detail(
                        self.app.config, identifier, maintenance=maintenance_active(), control=control
                    ) if identifier.startswith("group-") else operational_workbench.detail(
                        self.app.config, identifier, maintenance=maintenance_active(), control=control
                    )
                ))
            elif parsed.path == "/api/alerts":
                query = parse_qs(parsed.query)
                alert_status = str((query.get("status") or [""])[0] or "") or None
                if alert_status not in {None, "open", "resolved"}:
                    raise DecisionError("alert_status_invalid", "告警状态筛选无效。")
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError as exc:
                    raise DecisionError("limit_invalid", "告警数量参数无效。") from exc
                payload = alert_workbench.listing(self.app.config, maintenance=maintenance_active(), limit=limit, status=alert_status)
                linked = operational_workbench.alert_links()
                for alert in payload["alerts"]:
                    alert["issues"] = linked.get(alert["alert_id"], [])
                self.json_response(200, payload)
            elif re.fullmatch(r"/api/alerts/[0-9a-fA-F-]{36}/conversation", parsed.path):
                self.json_response(200, alert_workbench.conversation(parsed.path.split("/")[3]))
            elif re.fullmatch(r"/api/alerts/[0-9a-fA-F-]{36}", parsed.path):
                try:
                    control = current_control_state(self.app)
                except DecisionError:
                    control = None
                result = alert_workbench.detail(self.app.config, parsed.path.split("/")[3], maintenance=maintenance_active(), control=control)
                result["issues"] = operational_workbench.alert_links().get(result["alert_id"], [])
                self.json_response(200, result)
            elif parsed.path == "/api/attention":
                self.json_response(200, attention_state(self.app.config))
            elif parsed.path == "/api/reconciliation":
                query = parse_qs(parsed.query)
                scope = str((query.get("scope") or ["needs_user"])[0])
                self.json_response(200, reconciliation_scope_payload(self.app.config, scope))
            elif re.fullmatch(r"/api/reconciliation/case-[0-9a-f]{20}", parsed.path):
                self.json_response(200, workbench_attention.decorate(reconciliation.case_detail(parsed.path.rsplit("/", 1)[-1])))
            elif re.fullmatch(r"/api/reconciliation/case-[0-9a-f]{20}/conversation", parsed.path):
                case_id = parsed.path.split("/")[3]
                self.json_response(200, case_conversation.conversation_snapshot(case_id))
            elif parsed.path == "/api/preferences":
                self.json_response(200, preferences())
            elif re.fullmatch(r"/api/actions/[0-9a-fA-F-]{36}", parsed.path):
                self.json_response(200, action_detail(parsed.path.rsplit("/", 1)[-1]))
            elif re.fullmatch(r"/api/decisions/[0-9a-fA-F-]{36}/messages", parsed.path):
                decision_id = parsed.path.split("/")[3]
                self.json_response(
                    200,
                    {"messages": list_decision_messages(decision_id), "draft": decision_draft(decision_id)},
                )
            elif re.fullmatch(r"/api/decisions/[0-9a-fA-F-]{36}/draft", parsed.path):
                decision_id = parsed.path.split("/")[3]
                self.json_response(200, decision_draft(decision_id))
            elif re.fullmatch(r"/api/decisions/[0-9a-fA-F-]{36}", parsed.path):
                decision_id = parsed.path.split("/")[3]
                self.json_response(200, decision_detail(self.app.config, decision_id))
            else:
                self.json_response(404, {"error_code": "not_found"})
        except Exception as exc:
            self.handle_error_response(exc)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 20000:
            raise DecisionError("request_size", "请求大小无效。")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DecisionError("request_json", "请求格式无效。") from exc
        if not isinstance(payload, dict):
            raise DecisionError("request_json", "请求格式无效。")
        return payload

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parsed = parsed._replace(path=re.sub(r"^/api/workbench/business-(case-[0-9a-f]{20})(?=/|$)", r"/api/reconciliation/\1", parsed.path))
        if not self.authorized():
            self.json_response(403, {"error_code": "unauthorized"})
            return
        origin = self.headers.get("Origin")
        if origin != self.app.origin or self.headers.get("X-CSRF-Token") != self.app.csrf_token:
            self.json_response(403, {"error_code": "csrf_rejected"})
            return
        try:
            with maintenance_write_guard():
                payload = self.read_json()
                if codex_handoff.ROUTE.fullmatch(parsed.path):
                    result = codex_handoff.handle_post(self.app.handoffs, self.app.config, parsed.path, payload)
                elif parsed.path == "/api/preview":
                    sync_pending_decisions(self.app.config)
                    row = decision_row(str(payload.get("decision_id") or ""))
                    action = str(payload.get("action") or "")
                    merge_text = str(payload.get("merge_text") or "")
                    snooze_days = int(payload["snooze_days"]) if payload.get("snooze_days") is not None else None
                    result = create_preview(self.app.config, row, action, merge_text, snooze_days)
                elif parsed.path == "/api/apply":
                    row = decision_row(str(payload.get("decision_id") or ""))
                    action = str(payload.get("action") or "")
                    merge_text = str(payload.get("merge_text") or "")
                    snooze_days = int(payload["snooze_days"]) if payload.get("snooze_days") is not None else None
                    result = apply_decision(
                        self.app.config,
                        str(row["decision_id"]),
                        action,
                        merge_text,
                        preview_token=str(payload.get("preview_token") or ""),
                        snooze_days=snooze_days,
                    )
                elif parsed.path == "/api/actions/run":
                    if set(payload) != {"target"}:
                        raise DecisionError("request_fields", "请求只能包含同步来源。")
                    target = str(payload.get("target") or "")
                    system_state = current_control_state(self.app)
                    result = control_action_gate(system_state, target) or start_control_action(
                        self.app.config_path,
                        target,
                    )
                elif parsed.path == "/api/actions/sync-now":
                    system_state = current_control_state(self.app)
                    result = control_action_gate(system_state, "dingtalk_all") or start_background_action(
                        self.app.config_path, "sync_now",
                    )
                elif parsed.path == "/api/actions/review-now":
                    result = start_background_action(self.app.config_path, "review_now")
                elif parsed.path == "/api/actions/confirm-mail-read-safety":
                    if set(payload) != {"still_unread"} or not isinstance(payload.get("still_unread"), bool):
                        raise DecisionError("request_fields", "必须明确选择另一台设备上的未读状态。")
                    result = confirm_email_read_safety(
                        self.app.config, bool(payload["still_unread"])
                    )
                elif re.fullmatch(r"/api/reconciliation/case-[0-9a-f]{20}/preview", parsed.path):
                    case_id = parsed.path.split("/")[3]
                    result = case_conversation.preview_action(self.app.config, case_id, payload)
                elif re.fullmatch(r"/api/reconciliation/case-[0-9a-f]{20}/apply", parsed.path):
                    case_id = parsed.path.split("/")[3]
                    result = case_conversation.apply_action(self.app.config, case_id, payload)
                elif re.fullmatch(r"/api/reconciliation/case-[0-9a-f]{20}/archive", parsed.path):
                    case_id = parsed.path.split("/")[3]
                    result = case_conversation.archive_case(self.app.config, case_id, payload)
                elif re.fullmatch(r"/api/reconciliation/case-[0-9a-f]{20}/messages", parsed.path):
                    case_id = parsed.path.split("/")[3]
                    result = case_conversation.append_message(self.app.config_path, case_id, payload)
                elif re.fullmatch(r"/api/reconciliation/case-[0-9a-f]{20}/messages/[0-9a-fA-F-]{36}/retry", parsed.path):
                    parts = parsed.path.split("/")
                    case_id = parts[3]
                    result = case_conversation.retry_message(self.app.config_path, case_id, parts[5], payload)
                elif re.fullmatch(r"/api/workbench/(?:issue|group)-[0-9a-f]{24}/actions", parsed.path):
                    identifier = parsed.path.split("/")[3]
                    result = (
                        operational_workbench.execute_group_action(self.app, identifier, payload)
                        if identifier.startswith("group-")
                        else operational_workbench.execute_action(self.app, identifier, payload)
                    )
                elif re.fullmatch(r"/api/workbench/(?:issue|group)-[0-9a-f]{24}/messages", parsed.path):
                    result = alert_workbench.submit_message(self.app.config_path, parsed.path.split("/")[3], payload)
                elif re.fullmatch(r"/api/workbench/(?:issue|group)-[0-9a-f]{24}/messages/[0-9a-fA-F-]{36}/retry", parsed.path):
                    parts = parsed.path.split("/")
                    result = alert_workbench.retry_message(self.app.config_path, parts[3], parts[5], payload)
                elif re.fullmatch(r"/api/alerts/[0-9a-fA-F-]{36}/messages", parsed.path):
                    result = alert_workbench.submit_message(self.app.config_path, parsed.path.split("/")[3], payload)
                elif re.fullmatch(r"/api/alerts/[0-9a-fA-F-]{36}/messages/[0-9a-fA-F-]{36}/retry", parsed.path):
                    parts = parsed.path.split("/")
                    result = alert_workbench.retry_message(self.app.config_path, parts[3], parts[5], payload)
                elif re.fullmatch(r"/api/alerts/[0-9a-fA-F-]{36}/actions", parsed.path):
                    result = alert_workbench.execute_action(self.app, parsed.path.split("/")[3], payload)
                elif re.fullmatch(r"/api/alerts/[0-9a-fA-F-]{36}/ack", parsed.path):
                    if payload:
                        raise DecisionError("request_fields", "确认告警不接受其他字段。")
                    alert_id = parsed.path.split("/")[3]
                    row = syncstate.acknowledge(alert_id)
                    if row is None:
                        raise DecisionError("alert_missing", "告警不存在。", 404)
                    result = {"alert": syncstate.public_alert(row), "fault_cleared": False}
                elif re.fullmatch(r"/api/decisions/[0-9a-fA-F-]{36}/messages", parsed.path):
                    decision_id = parsed.path.split("/")[3]
                    result = append_decision_message(self.app.config_path, decision_id, payload)
                else:
                    self.json_response(404, {"error_code": "not_found"})
                    return
            self.json_response(200, result)
        except Exception as exc:
            self.handle_error_response(exc)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        parsed = parsed._replace(path=re.sub(r"^/api/workbench/business-(case-[0-9a-f]{20})(?=/|$)", r"/api/reconciliation/\1", parsed.path))
        if not self.authorized():
            self.json_response(403, {"error_code": "unauthorized"})
            return
        origin = self.headers.get("Origin")
        if origin != self.app.origin or self.headers.get("X-CSRF-Token") != self.app.csrf_token:
            self.json_response(403, {"error_code": "csrf_rejected"})
            return
        try:
            with maintenance_write_guard():
                payload = self.read_json()
                if parsed.path == "/api/preferences":
                    result = update_preferences(payload)
                elif re.fullmatch(r"/api/decisions/[0-9a-fA-F-]{36}/draft", parsed.path):
                    decision_id = parsed.path.split("/")[3]
                    result = save_decision_draft(decision_id, payload)
                elif re.fullmatch(r"/api/workbench/(?:issue|group)-[0-9a-f]{24}/draft", parsed.path):
                    result = alert_workbench.save_draft(parsed.path.split("/")[3], payload)
                elif re.fullmatch(r"/api/alerts/[0-9a-fA-F-]{36}/draft", parsed.path):
                    result = alert_workbench.save_draft(parsed.path.split("/")[3], payload)
                elif re.fullmatch(r"/api/reconciliation/case-[0-9a-f]{20}/draft", parsed.path):
                    case_id = parsed.path.split("/")[3]
                    result = case_conversation.save_draft(case_id, payload)
                else:
                    self.json_response(404, {"error_code": "not_found"})
                    return
            self.json_response(200, result)
        except Exception as exc:
            self.handle_error_response(exc)

    def handle_error_response(self, exc: Exception) -> None:
        request_id = log_api_error(exc, self.command, self.path)
        if isinstance(exc, (DecisionError, alert_workbench.AlertError, operational_workbench.WorkbenchError, codex_handoff.HandoffError)):
            self.json_response(exc.status, {"error_code": exc.code, "message": exc.message, "request_id": request_id})
        elif isinstance(exc, reconciliation.ReconciliationError):
            self.json_response(exc.status, {"error_code": exc.code, "message": exc.message})
        elif isinstance(exc, case_conversation.CaseConversationError):
            self.json_response(exc.status, {"error_code": exc.code, "message": exc.message})
        else:
            self.json_response(500, {"error_code": "internal_error", "message": "此部分暂时无法加载，其他内容和草稿仍保留。请稍后重试。", "request_id": request_id})


class DecisionServer(ThreadingHTTPServer):
    daemon_threads = True
    # The dashboard starts more requests than socketserver's default backlog.
    # Admit that local burst while the shared status collector is running.
    request_queue_size = 32

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys
        error = sys.exc_info()[1]
        if error:
            log_api_error(error, "CONNECTION", "/")

    def __init__(self, address: tuple[str, int], config: dict[str, Any], config_path: Path = CONFIG_PATH):
        self.config = config
        self.config_path = config_path
        self.handoffs = codex_handoff.Queue()
        self.system_dashboard = SystemDashboard(DashboardPaths.from_config(config))
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.origin = f"http://{address[0]}:{address[1]}"
        super().__init__(address, DecisionHandler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local BUSINESS memory sync center")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17626)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--sync-only", action="store_true")
    parser.add_argument("--answer-question")
    parser.add_argument("--answer-case-message")
    parser.add_argument("--answer-alert-message")
    parser.add_argument("--recover-stale-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ignore-review-pid", action="append", type=int, default=[], help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    from owner_retirement import stopped
    if stopped():
        print(json.dumps({"status": "owner_retired", "error_code": "company_automation_stopped"}))
        return 0
    args = parse_args()
    if args.host != "127.0.0.1":
        raise SystemExit("decision center may only bind to 127.0.0.1")
    config = load_config(args.config)
    if args.answer_question or args.answer_alert_message or args.answer_case_message:
        print(json.dumps({"status": "disabled", "error_code": "legacy_chat_retired"}))
        return 2
    if args.recover_stale_only:
        try:
            with maintenance_write_guard():
                syncv2.initialize()
                recovered = recover_stale_action_runs(set(args.ignore_review_pid))
            print(json.dumps({"status": "success", "recovered": recovered}, ensure_ascii=False, sort_keys=True))
            return 0
        except DecisionError as exc:
            print(json.dumps({"status": "deferred", "error_code": exc.code}, ensure_ascii=False, sort_keys=True))
            return 3
    if args.answer_question:
        try:
            with maintenance_write_guard():
                syncv2.initialize()
                result = answer_decision_question(config, str(args.answer_question))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "failed", "error_code": getattr(exc, "code", type(exc).__name__)}, ensure_ascii=False))
            return 2
    if args.answer_alert_message:
        try:
            with maintenance_write_guard():
                syncv2.initialize()
                result = alert_workbench.answer_message(config, str(args.answer_alert_message))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        except Exception as exc:
            log_api_error(exc, "WORKER", "/alerts/answer")
            return 2
    if args.answer_case_message:
        try:
            with maintenance_write_guard():
                syncv2.initialize()
                reconciliation.initialize()
                result = case_conversation.answer_message(config, str(args.answer_case_message))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "failed", "error_code": getattr(exc, "code", type(exc).__name__)}, ensure_ascii=False))
            return 2
    if args.sync_only:
        try:
            with maintenance_write_guard():
                syncv2.initialize()
                reconciliation.initialize()
                recover_stale_action_runs()
                recover_decision_journals(config)
                inserted = sync_pending_decisions(config)
                case_conversation.sync_case_threads()
                case_conversation.recover_pending_archives(config)
                case_conversation.restore_durable_actions()
                case_conversation.sync_case_threads()
            print(json.dumps({"status": "success", "registered": inserted, "pending": len(list_decisions(config))}, ensure_ascii=False))
            return 0
        except DecisionError as exc:
            print(json.dumps({"status": "deferred", "error_code": exc.code}, ensure_ascii=False, sort_keys=True))
            return 3
    startup_mode = "read_write"
    try:
        with maintenance_write_guard():
            syncv2.initialize()
            syncstate.initialize()
            reconciliation.initialize()
            recover_stale_action_runs()
            recover_decision_journals(config)
            sync_pending_decisions(config)
            case_conversation.sync_case_threads()
            case_conversation.recover_pending_archives(config)
            case_conversation.restore_durable_actions()
            case_conversation.sync_case_threads()
            operational_workbench.reconcile(config)
            operational_workbench.recover_actions()
    except DecisionError as exc:
        if exc.code != "maintenance_active" or not syncv2.DATABASE.is_file():
            raise
        startup_mode = "maintenance_read_only"
    if startup_mode == "read_write":
        alert_workbench.recover_interrupted()
    server = DecisionServer((args.host, args.port), config, args.config)
    server.system_dashboard.warm_file_bindings()
    server.handoffs.start(maintenance_active)
    if startup_mode == "read_write":
        import media_actions,media_classification
        if media_classification.worker_idle() and any(d["state"] in {"queued","running"} for d in media_actions.decisions().values()):
            media_actions.spawn(args.config)
    import media_classification
    # Recovery is owned by the native supervisor, including when this UI is closed.
    media_routing_stop=None
    if args.open:
        threading.Timer(0.5, lambda: subprocess.Popen(["/usr/bin/open", server.origin])).start()
    print(json.dumps({"status": "ready", "mode": startup_mode, "url": server.origin, "pending": len(list_decisions(config))}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if media_routing_stop is not None:media_routing_stop.set()
        server.handoffs.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
