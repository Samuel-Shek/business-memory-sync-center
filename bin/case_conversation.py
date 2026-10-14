#!/usr/bin/env python3
"""Persistent, case-scoped AI conversations for the local Sync Center.

Raw conversation stays in the private runtime database.  A separate, reviewed
proposal is required before any text may enter governed business memory.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import business_page_router as business_router
import claim_reconciliation as reconciliation
import dingtalk_memory_automation as memory
import dingtalk_sync_v2 as syncv2
import workbench_attention as attention


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "prompts" / "case_conversation.md"
SCHEMA_PATH = ROOT / "schemas" / "case_conversation.schema.json"
WORKER_PATH = ROOT / "bin" / "dingtalk_decision_center.py"
CASE_ID_RE = re.compile(r"case-[0-9a-f]{20}")
MESSAGE_MAX_CHARS = 4000
ANSWER_MAX_CHARS = 8000
RECENT_MESSAGE_LIMIT = 24
SUMMARY_KEEP_RECENT = 16
CONVERSATION_STATES = {"clarifying", "proposal_ready", "waiting_evidence"}
HIGH_RISK_DIMENSIONS = {
    "approval_record", "authorization", "technical_signoff", "certification",
    "technical_specification", "price", "contract", "sent", "received",
    "completed", "delivery", "legal_identity",
}
HIGH_RISK_TERMS = (
    "批准", "审批", "授权", "签署", "签字", "认证", "证书", "价格", "报价",
    "合同", "付款", "回款", "发送", "已发", "交付", "发货", "完成", "验收",
    "技术参数", "质保", "客户承诺", "公司决定",
)
OWNER_STATEMENT_TERMS = (
    "我决定", "我的决定", "我希望", "我的偏好", "我要求", "以后都", "今后都",
    "我的规则", "我个人", "对我来说", "我选择",
)
# 2020-01-02: only enabled providers are selectable; retain paused catalogue entries for restoration.
AI_MODELS = {
    "deepseek-flash": {
        "label": "DeepSeek V4.1 Flash（默认，全模态）",
        "reasoning_efforts": ["low", "high", "max"],
    },
    "mimo-v2.5-pro": {
        "label": "MiMo V2.5 Pro（已暂停）",
        "reasoning_efforts": ["low", "medium", "high"],
    },
    "mimo-v2.5": {
        "label": "MiMo V2.5（全模态）",
        "reasoning_efforts": ["low", "medium", "high"],
    },
    "k3": {"label": "Kimi K3", "reasoning_efforts": ["low", "high", "max"]},
    "gpt-6-astra": {
        "label": "GPT-6 Astra",
        "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "gpt-5.6-sol": {
        "label": "5.6 Sol",
        "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "gpt-5.6-terra": {
        "label": "5.6 Terra",
        "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "gpt-5.6-luna": {
        "label": "5.6 Luna",
        "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "claude-opus-5": {
        "label": "Claude Opus 5",
        "reasoning_efforts": ["low", "medium", "high"],
    },
}
AI_REASONING_EFFORTS = {
    "low": "低",
    "medium": "中等",
    "high": "高",
    "xhigh": "很高",
    "max": "最高",
}
AI_SPEEDS = {
    "standard": {"label": "标准", "service_tier": None},
    "fast": {"label": "快速", "service_tier": "priority"},
}
DEFAULT_AI_PROFILE = {
    "model": "deepseek-flash",
    "reasoning_effort": "high",
    "speed": "standard",
}


class CaseConversationError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _enabled_models(config=None):
    import model_router
    if config is None:
        path = ROOT / "config.json"
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    enabled = model_router.routing(config)["enabled_providers"]
    return {name: value for name, value in AI_MODELS.items()
            if model_router.provider_for(name) in enabled}


def ai_configuration() -> dict[str, Any]:
    """Return enabled model choices exposed by the workbench."""

    return {
        "models": [
            {
                "id": model_id,
                "label": value["label"],
                "reasoning_efforts": list(value["reasoning_efforts"]),
            }
            for model_id, value in _enabled_models().items()
        ],
        "reasoning_efforts": [
            {"id": effort_id, "label": label}
            for effort_id, label in AI_REASONING_EFFORTS.items()
        ],
        "speeds": [
            {"id": speed_id, "label": value["label"]}
            for speed_id, value in AI_SPEEDS.items()
        ],
        "default": dict(DEFAULT_AI_PROFILE),
    }


def _validated_ai_profile(value: object, *, config=None, stored=False) -> dict[str, str]:
    if value is None:
        return dict(DEFAULT_AI_PROFILE)
    if not isinstance(value, dict) or set(value) != {"model", "reasoning_effort", "speed"}:
        raise CaseConversationError("ai_profile_contract", "AI 对话设置字段无效。")
    model = str(value.get("model") or "")
    effort = str(value.get("reasoning_effort") or "")
    speed = str(value.get("speed") or "")
    model_config = AI_MODELS.get(model)
    if model_config and model not in _enabled_models(config):
        if stored:
            return dict(DEFAULT_AI_PROFILE)
        raise CaseConversationError("ai_model_disabled", "该模型已暂时停用，请选择 DeepSeek 或 Kimi。")
    if not model_config:
        raise CaseConversationError("ai_model_invalid", "所选 AI 模型当前不可用。")
    if effort not in model_config["reasoning_efforts"]:
        raise CaseConversationError("ai_reasoning_invalid", "所选推理强度不适用于这个模型。")
    if speed not in AI_SPEEDS:
        raise CaseConversationError("ai_speed_invalid", "所选速度当前不可用。")
    return {"model": model, "reasoning_effort": effort, "speed": speed}


def retry_ai_profile(value: object) -> dict[str, str]:
    """Normalize a stored retry profile while retaining the original message as evidence."""
    return _validated_ai_profile(value, stored=True)


def _case_id(value: object) -> str:
    case_id = str(value or "")
    if not CASE_ID_RE.fullmatch(case_id):
        raise CaseConversationError("case_id_invalid", "问题编号无效。")
    return case_id


def should_archive_after_reply(content: object) -> bool:
    """Use only the final non-whitespace character to classify archive intent."""
    trimmed = str(content or "").rstrip()
    return bool(trimmed) and trimmed[-1] not in {"?", "？"}


def _messages_hash(rows: list[sqlite3.Row] | list[dict[str, Any]]) -> str:
    serializable = []
    for source in rows:
        row = dict(source)
        serializable.append({
            "message_id": str(row["message_id"]),
            "sequence": int(row["sequence"]),
            "case_version": int(row["case_version"]),
            "context_hash": str(row["context_hash"]),
            "evidence_watermark": str(row["evidence_watermark"]),
            "author": str(row["author"]),
            "message_kind": str(row["message_kind"]),
            "content_hash": str(row["content_hash"]),
            "status": str(row["status"]),
            "conversation_state": str(row.get("conversation_state") or ""),
            "focused_questions_json": str(row.get("focused_questions_json") or "[]"),
            "proposed_plan": str(row.get("proposed_plan") or ""),
            "reply_to": str(row.get("reply_to") or ""),
            "retry_of": str(row.get("retry_of") or ""),
            "ai_model": str(row.get("ai_model") or ""),
            "ai_reasoning_effort": str(row.get("ai_reasoning_effort") or ""),
            "ai_speed": str(row.get("ai_speed") or ""),
            "archive_after_reply": int(row.get("archive_after_reply") or 0),
            "error_code": str(row.get("error_code") or ""),
        })
    return syncv2.sha256_text(syncv2.canonical_json(serializable))


def _thread_hash(connection: sqlite3.Connection, case_id: str) -> str:
    rows = connection.execute(
        "SELECT * FROM case_messages WHERE case_id=? ORDER BY sequence", (case_id,)
    ).fetchall()
    return _messages_hash(rows)


def _refresh_thread(connection: sqlite3.Connection, case_id: str, *, stamp: Optional[str] = None) -> str:
    conversation_hash = _thread_hash(connection, case_id)
    connection.execute(
        "UPDATE case_threads SET conversation_hash=?,updated_at=? WHERE case_id=?",
        (conversation_hash, stamp or syncv2.now_iso(), case_id),
    )
    return conversation_hash


def _semantic_fingerprint(case: dict[str, Any]) -> str:
    return syncv2.sha256_text(syncv2.canonical_json({
        "subject_key": str(case.get("subject_key") or ""),
        "dimension_key": str(case.get("dimension_key") or ""),
    }))


def _append_event(
    connection: sqlite3.Connection,
    case_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    context_hash: str,
    evidence_watermark: str,
    stamp: str,
) -> None:
    thread = connection.execute(
        "SELECT current_episode_id,last_sequence FROM case_threads WHERE case_id=?", (case_id,)
    ).fetchone()
    if not thread:
        raise CaseConversationError("conversation_missing", "该问题的持久对话尚未建立。", 503)
    sequence = int(thread["last_sequence"] or 0) + 1
    connection.execute(
        """
        INSERT INTO case_events(
          event_id,case_id,episode_id,sequence,event_type,event_payload_json,
          context_hash,evidence_watermark,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid.uuid4()), case_id, thread["current_episode_id"], sequence, event_type,
            syncv2.canonical_json(payload), context_hash, evidence_watermark, stamp,
        ),
    )
    connection.execute(
        "UPDATE case_threads SET last_sequence=?,thread_revision=thread_revision+1 WHERE case_id=?",
        (sequence, case_id),
    )


def _upsert_thread(connection: sqlite3.Connection, case: dict[str, Any]) -> None:
    case_id = _case_id(case["case_id"])
    stamp = syncv2.now_iso()
    initial_hash = syncv2.sha256_text("[]")
    context_hash = str(case["context_hash"])
    evidence_watermark = str(case.get("evidence_watermark") or "")
    semantic_fingerprint = _semantic_fingerprint(case)
    is_closed = str(case["state"]) in {"plan_confirmed", "resolved", "archived"}
    existing = connection.execute("SELECT * FROM case_threads WHERE case_id=?", (case_id,)).fetchone()
    if not existing:
        lineage_id = str(uuid.uuid4())
        episode_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO case_threads(
              case_id,lineage_id,semantic_fingerprint,case_epoch,current_episode_id,
              case_version,context_hash,evidence_watermark,conversation_hash,
              conversation_state,created_at,updated_at,closed_at
            ) VALUES(?,?,?,1,?,?,?,?,?,'clarifying',?,?,?)
            """,
            (
                case_id, lineage_id, semantic_fingerprint, episode_id, int(case["case_version"]),
                context_hash, evidence_watermark, initial_hash, stamp, stamp, stamp if is_closed else None,
            ),
        )
        connection.execute(
            """
            INSERT INTO case_episodes(
              episode_id,case_id,case_epoch,opened_context_hash,opened_evidence_watermark,
              opened_at,closed_at,close_reason
            ) VALUES(?,?,1,?,?,?,?,?)
            """,
            (
                episode_id, case_id, context_hash, evidence_watermark, stamp,
                stamp if is_closed else None, str(case["state"]) if is_closed else None,
            ),
        )
        _append_event(
            connection, case_id, "case_thread_created", {"state": str(case["state"])},
            context_hash=context_hash, evidence_watermark=evidence_watermark, stamp=stamp,
        )
        return
    existing_fingerprint = str(existing["semantic_fingerprint"] or "")
    if existing_fingerprint and existing_fingerprint != semantic_fingerprint:
        raise CaseConversationError(
            "case_identity_reused",
            "检测到同一问题编号被用于不同业务事项，已停止绑定以保护原对话。",
            503,
        )
    lineage_id = str(existing["lineage_id"] or uuid.uuid4())
    episode_id = str(existing["current_episode_id"] or uuid.uuid4())
    case_epoch = max(1, int(existing["case_epoch"] or 1))
    previous_closed = bool(existing["closed_at"])
    context_changed = (
        str(existing["context_hash"]) != context_hash
        or str(existing["evidence_watermark"]) != evidence_watermark
    )
    if not existing["current_episode_id"]:
        connection.execute(
            """
            INSERT OR IGNORE INTO case_episodes(
              episode_id,case_id,case_epoch,opened_context_hash,opened_evidence_watermark,
              opened_at,closed_at,close_reason
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                episode_id, case_id, case_epoch, str(existing["context_hash"]),
                str(existing["evidence_watermark"]), str(existing["created_at"]),
                str(existing["closed_at"]) if previous_closed else None,
                "migrated_closed" if previous_closed else None,
            ),
        )
    if previous_closed and not is_closed:
        case_epoch += 1
        episode_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO case_episodes(
              episode_id,case_id,case_epoch,opened_context_hash,opened_evidence_watermark,opened_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (episode_id, case_id, case_epoch, context_hash, evidence_watermark, stamp),
        )
    closed_at = str(existing["closed_at"] or "") or None
    if is_closed and not previous_closed:
        closed_at = stamp
        connection.execute(
            "UPDATE case_episodes SET closed_at=?,close_reason=? WHERE episode_id=? AND closed_at IS NULL",
            (stamp, str(case["state"]), episode_id),
        )
    elif not is_closed:
        closed_at = None
    if is_closed and existing["active_message_id"]:
        active_message_id = str(existing["active_message_id"])
        active_message = connection.execute(
            "SELECT run_id FROM case_messages WHERE message_id=?", (active_message_id,)
        ).fetchone()
        connection.execute(
            """
            UPDATE case_messages SET status='cancelled',error_code='case_archived',updated_at=?
            WHERE message_id=? AND status IN ('pending','running')
            """,
            (stamp, active_message_id),
        )
        connection.execute(
            """
            UPDATE case_ai_runs SET state='superseded',last_error_code='case_archived',
              updated_at=?,completed_at=?
            WHERE message_id=? AND state IN ('queued','running')
            """,
            (stamp, stamp, active_message_id),
        )
        connection.execute(
            "UPDATE case_threads SET active_message_id=NULL,last_error_code=NULL WHERE case_id=?",
            (case_id,),
        )
        if active_message and active_message["run_id"]:
            active_run_id = str(active_message["run_id"])
            connection.execute(
                """
                UPDATE action_steps SET status='interrupted',error_code='case_archived',
                  completed_at=COALESCE(completed_at,?),updated_at=?
                WHERE run_id=? AND status='running'
                """,
                (stamp, stamp, active_run_id),
            )
            connection.execute(
                """
                UPDATE action_steps SET status='skipped',completed_at=COALESCE(completed_at,?),updated_at=?
                WHERE run_id=? AND status IN ('queued','pending')
                """,
                (stamp, stamp, active_run_id),
            )
            connection.execute(
                """
                UPDATE action_runs SET status='interrupted',result_json='{}',
                  error_code='case_archived',completed_at=?,updated_at=?
                WHERE run_id=? AND status='running'
                """,
                (stamp, stamp, active_run_id),
            )
        _refresh_thread(connection, case_id, stamp=stamp)
    connection.execute(
        """
        UPDATE case_threads SET lineage_id=?,semantic_fingerprint=?,case_epoch=?,current_episode_id=?,
          case_version=?,context_hash=?,evidence_watermark=?,updated_at=?,closed_at=?
        WHERE case_id=?
        """,
        (
            lineage_id, semantic_fingerprint, case_epoch, episode_id, int(case["case_version"]),
            context_hash, evidence_watermark, stamp, closed_at, case_id,
        ),
    )
    if previous_closed and not is_closed:
        _append_event(
            connection, case_id, "case_reopened", {"state": str(case["state"]), "case_epoch": case_epoch},
            context_hash=context_hash, evidence_watermark=evidence_watermark, stamp=stamp,
        )
    elif is_closed and not previous_closed:
        _append_event(
            connection, case_id, "case_closed", {"state": str(case["state"])},
            context_hash=context_hash, evidence_watermark=evidence_watermark, stamp=stamp,
        )
    elif context_changed:
        _append_event(
            connection, case_id, "evidence_context_changed", {"state": str(case["state"])},
            context_hash=context_hash, evidence_watermark=evidence_watermark, stamp=stamp,
        )


def sync_case_threads() -> int:
    """Bind stable case ids to formal conversation threads without copying evidence."""
    claim_connection = reconciliation.connect(readonly=True)
    try:
        cases = [reconciliation.public_case(row) for row in claim_connection.execute("SELECT * FROM cases")]
    finally:
        claim_connection.close()
    syncv2.initialize()
    with syncv2.file_lock(syncv2.DECISION_LOCK, blocking=True):
        connection = syncv2.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for case in cases:
                _upsert_thread(connection, case)
                attention.refresh_case(connection, reconciliation.case_detail(case["case_id"]))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    return len(cases)


def _thread_row(case_id: str) -> Optional[sqlite3.Row]:
    connection = syncv2.connect_readonly()
    try:
        return connection.execute("SELECT * FROM case_threads WHERE case_id=?", (case_id,)).fetchone()
    finally:
        connection.close()


def _public_message(row: sqlite3.Row) -> dict[str, Any]:
    try:
        questions = json.loads(str(row["focused_questions_json"] or "[]"))
    except json.JSONDecodeError:
        questions = []
    profile = None
    if str(row["ai_model"] or ""):
        profile = {
            "model": str(row["ai_model"]),
            "reasoning_effort": str(row["ai_reasoning_effort"]),
            "speed": str(row["ai_speed"]),
        }
    return {
        "message_id": str(row["message_id"]),
        "sequence": int(row["sequence"]),
        "case_version": int(row["case_version"]),
        "author": str(row["author"]),
        "message_kind": str(row["message_kind"]),
        "content": str(row["content"]),
        "status": str(row["status"]),
        "conversation_state": row["conversation_state"],
        "focused_questions": [str(value) for value in questions[:3]],
        "proposed_plan": row["proposed_plan"],
        "reply_to": row["reply_to"],
        "retry_of": row["retry_of"],
        "retry_count": int(row["retry_count"] or 0),
        "ai_profile": profile,
        "archive_after_reply": bool(row["archive_after_reply"]),
        "error_code": row["error_code"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _public_proposal(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "proposal_id": str(row["proposal_id"]),
        "title": str(row["title"]),
        "text": str(row["proposal_text"]),
        "target_page": str(row["target_page"]),
        "source_attribution": str(row["source_attribution"]),
        "fact_status": str(row["fact_status"]),
        "evidence_boundary": str(row["evidence_boundary"]),
        "risk_level": str(row["risk_level"]),
        "proposal_hash": str(row["proposal_hash"]),
        "status": str(row["status"]),
        "claim_id": row["claim_id"],
        "created_at": str(row["created_at"]),
        "applied_at": row["applied_at"],
    }


def _public_plan_proposal(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "proposal_id": str(row["proposal_id"]),
        "revision": int(row["revision"]),
        "text": str(row["proposal_text"]),
        "proposal_hash": str(row["proposal_hash"]),
        "conversation_hash": str(row["conversation_hash"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
    }


def conversation_snapshot(case_id: str) -> dict[str, Any]:
    case_id = _case_id(case_id)
    case = reconciliation.case_detail(case_id)
    connection = syncv2.connect_readonly()
    try:
        thread = connection.execute("SELECT * FROM case_threads WHERE case_id=?", (case_id,)).fetchone()
        if not thread:
            thread = {"closed_at": None, "conversation_hash": "", "conversation_state": "clarifying",
                      "summary": "", "last_error_code": None}
        messages = connection.execute(
            "SELECT * FROM case_messages WHERE case_id=? ORDER BY sequence", (case_id,)
        ).fetchall()
        draft = connection.execute("SELECT * FROM case_drafts WHERE case_id=?", (case_id,)).fetchone()
        proposals = connection.execute(
            "SELECT * FROM case_memory_proposals WHERE case_id=? ORDER BY created_at DESC", (case_id,)
        ).fetchall()
        plan_proposal = connection.execute(
            "SELECT * FROM case_proposals WHERE case_id=? AND status='active' ORDER BY revision DESC LIMIT 1",
            (case_id,),
        ).fetchone()
    finally:
        connection.close()
    completed_exchanges = sum(
        1 for row in messages if str(row["author"]) == "assistant" and str(row["status"]) == "completed"
    )
    latest_plan = str(plan_proposal["proposal_text"] if plan_proposal else "")
    pending_proposal = next((row for row in proposals if str(row["status"]) == "pending"), None)
    disposition = attention.row(case_id) or {}
    read_only = bool(thread["closed_at"]) or str(case["state"]) in {"plan_confirmed", "resolved", "archived"} or disposition.get("attention_state") in attention.CLOSED
    active = next(
        (
            _public_message(row) for row in messages
            if str(row["author"]) == "user" and str(row["status"]) in {"pending", "running"}
        ),
        None,
    )
    return {
        "case_id": case_id,
        "case_version": int(case["case_version"]),
        "context_hash": str(case["context_hash"]),
        "evidence_watermark": str(case.get("evidence_watermark") or ""),
        "conversation_hash": str(thread["conversation_hash"]),
        "conversation_state": str(thread["conversation_state"]),
        "summary": str(thread["summary"] or ""),
        "messages": [_public_message(row) for row in messages],
        "draft": str(draft["content"] if draft else ""),
        "draft_updated_at": str(draft["updated_at"] if draft else ""),
        "active_message": active,
        "last_error_code": thread["last_error_code"],
        "completed_exchanges": completed_exchanges,
        "latest_proposed_plan": latest_plan,
        "plan_proposal": _public_plan_proposal(plan_proposal) if plan_proposal else None,
        "memory_proposal": _public_proposal(pending_proposal) if pending_proposal else None,
        "memory_proposals": [_public_proposal(row) for row in proposals],
        "ai_configuration": ai_configuration(),
        "read_only": read_only,
        "attention_state": disposition.get("attention_state", "needs_user"),
        "attention_reason": disposition.get("reason", ""),
        "actions_enabled": completed_exchanges >= 1 and not read_only and active is None,
    }


def save_draft(case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    case_id = _case_id(case_id)
    if set(payload) != {"content", "case_version", "context_hash"}:
        raise CaseConversationError("draft_contract", "草稿请求字段无效。")
    content = str(payload.get("content") or "")
    if len(content) > MESSAGE_MAX_CHARS:
        raise CaseConversationError("message_too_long", "输入内容不能超过 4,000 字。")
    case = reconciliation.case_detail(case_id)
    if int(payload.get("case_version") or 0) != int(case["case_version"]) or str(payload.get("context_hash") or "") != str(case["context_hash"]):
        raise CaseConversationError("case_stale", "证据或问题版本已经变化，请刷新后继续。", 409)
    thread = _thread_row(case_id)
    if (thread and thread["closed_at"]) or (attention.row(case_id) or {}).get("attention_state") in attention.CLOSED:
        raise CaseConversationError("conversation_read_only", "该问题已处理，对话记录只读。", 409)
    stamp = syncv2.now_iso()
    with syncv2.file_lock(syncv2.DECISION_LOCK, blocking=True):
        connection = syncv2.connect()
        try:
            if content:
                connection.execute(
                    """
                    INSERT INTO case_drafts(case_id,case_version,content,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(case_id) DO UPDATE SET case_version=excluded.case_version,
                      content=excluded.content,updated_at=excluded.updated_at
                    """,
                    (case_id, int(case["case_version"]), content, stamp),
                )
            else:
                connection.execute("DELETE FROM case_drafts WHERE case_id=?", (case_id,))
            connection.commit()
        finally:
            connection.close()
    return {"case_id": case_id, "content": content, "updated_at": stamp}


def _next_sequence(connection: sqlite3.Connection, case_id: str) -> int:
    return int(connection.execute(
        "SELECT COALESCE(MAX(sequence),0)+1 FROM case_messages WHERE case_id=?", (case_id,)
    ).fetchone()[0])


def _spawn_worker(config_path: Path, message_id: str) -> None:
    subprocess.Popen(
        [sys.executable, str(WORKER_PATH), "--config", str(config_path), "--answer-case-message", message_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _message_submit_result(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    idempotent_replay: bool,
) -> dict[str, Any]:
    ai_run = connection.execute(
        "SELECT ai_run_id FROM case_ai_runs WHERE message_id=?", (str(row["message_id"]),)
    ).fetchone()
    thread = connection.execute(
        "SELECT conversation_hash FROM case_threads WHERE case_id=?", (str(row["case_id"]),)
    ).fetchone()
    profile = {
        "model": str(row["ai_model"]),
        "reasoning_effort": str(row["ai_reasoning_effort"]),
        "speed": str(row["ai_speed"]),
    }
    return {
        "message_id": str(row["message_id"]),
        "client_message_id": str(row["message_id"]),
        "case_id": str(row["case_id"]),
        "status": str(row["status"]),
        "ai_run_id": str(ai_run["ai_run_id"]) if ai_run else "",
        "ai_profile": profile,
        "archive_after_reply": bool(row["archive_after_reply"]),
        "conversation_hash": str(thread["conversation_hash"] if thread else ""),
        "idempotent_replay": idempotent_replay,
        "saved": True,
        "attention_state": (attention.row(str(row["case_id"]), connection) or {}).get("attention_state", "needs_user"),
    }


def _validate_client_message_replay(
    row: sqlite3.Row,
    *,
    case_id: str,
    content_hash: str,
    case_version: int,
    context_hash: str,
    ai_profile: dict[str, str],
    archive_after_reply: bool,
    retry_of: Optional[str],
) -> None:
    actual = (
        str(row["case_id"]),
        str(row["content_hash"]),
        int(row["case_version"]),
        str(row["context_hash"]),
        str(row["ai_model"]),
        str(row["ai_reasoning_effort"]),
        str(row["ai_speed"]),
        bool(row["archive_after_reply"]),
        str(row["retry_of"] or ""),
    )
    expected = (
        case_id,
        content_hash,
        case_version,
        context_hash,
        ai_profile["model"],
        ai_profile["reasoning_effort"],
        ai_profile["speed"],
        archive_after_reply,
        str(retry_of or ""),
    )
    if actual != expected or str(row["author"]) != "user":
        raise CaseConversationError(
            "client_message_conflict",
            "这条客户端消息编号已经绑定其他内容，请刷新后重试。",
            409,
        )


def append_message(
    config_path: Path,
    case_id: str,
    payload: dict[str, Any],
    *,
    retry_of: Optional[str] = None,
    retry_count: int = 0,
    archive_after_reply_override: Optional[bool] = None,
) -> dict[str, Any]:
    case_id = _case_id(case_id)
    required_fields = {"content", "case_version", "context_hash"}
    allowed_fields = required_fields | {"ai_profile", "client_message_id"}
    if not required_fields.issubset(payload) or set(payload) - allowed_fields:
        raise CaseConversationError("message_contract", "消息请求字段无效。")
    ai_profile = _validated_ai_profile(payload.get("ai_profile"))
    raw_content = str(payload.get("content") or "")
    content = raw_content.strip()
    if not content:
        raise CaseConversationError("message_empty", "请输入要和 AI 讨论的内容。")
    if len(content) > MESSAGE_MAX_CHARS:
        raise CaseConversationError("message_too_long", "每条消息不能超过 4,000 字。")
    archive_after_reply = (
        bool(archive_after_reply_override)
        if archive_after_reply_override is not None
        else should_archive_after_reply(raw_content)
    )
    client_message_id = str(payload.get("client_message_id") or "")
    if client_message_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", client_message_id):
        raise CaseConversationError("client_message_id_invalid", "客户端消息编号无效。", 409)
    message_id = client_message_id or str(uuid.uuid4())
    content_hash = syncv2.sha256_text(content)
    if client_message_id:
        connection = syncv2.connect_readonly()
        try:
            existing = connection.execute(
                "SELECT * FROM case_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if existing:
                _validate_client_message_replay(
                    existing,
                    case_id=case_id,
                    content_hash=content_hash,
                    case_version=int(payload.get("case_version") or 0),
                    context_hash=str(payload.get("context_hash") or ""),
                    ai_profile=ai_profile,
                    archive_after_reply=archive_after_reply,
                    retry_of=retry_of,
                )
                return _message_submit_result(
                    connection, existing, idempotent_replay=True
                )
        finally:
            connection.close()
    ai_enabled = True
    if config_path.is_file():
        config = memory.load_config(config_path)
        ai_enabled = bool((config.get("case_workbench") or {}).get("ai_enabled", True))
    case = reconciliation.case_detail(case_id)
    if int(payload.get("case_version") or 0) != int(case["case_version"]) or str(payload.get("context_hash") or "") != str(case["context_hash"]):
        raise CaseConversationError("case_stale", "证据或问题版本已经变化，请刷新后继续。", 409)
    if str(case["state"]) in {"plan_confirmed", "resolved", "archived"}:
        raise CaseConversationError("conversation_read_only", "该问题已经结案，对话只读。", 409)
    if not retry_of and (attention.row(case_id) or {}).get("attention_state") in attention.CLOSED:
        raise CaseConversationError("conversation_read_only", "你的回复已保存，该事项已退出待确认。", 409)
    ai_run_id = str(uuid.uuid4())
    run_id = ""
    stamp = syncv2.now_iso()
    try:
        with syncv2.file_lock(syncv2.DECISION_LOCK, blocking=True):
            connection = syncv2.connect()
            try:
                existing = connection.execute(
                    "SELECT * FROM case_messages WHERE message_id=?", (message_id,)
                ).fetchone()
                if existing:
                    _validate_client_message_replay(
                        existing,
                        case_id=case_id,
                        content_hash=content_hash,
                        case_version=int(payload.get("case_version") or 0),
                        context_hash=str(payload.get("context_hash") or ""),
                        ai_profile=ai_profile,
                        archive_after_reply=archive_after_reply,
                        retry_of=retry_of,
                    )
                    return _message_submit_result(
                        connection, existing, idempotent_replay=True
                    )
                run_id = syncv2.start_action_run("case_conversation", trigger="manual")
                connection.execute("BEGIN IMMEDIATE")
                thread = connection.execute("SELECT * FROM case_threads WHERE case_id=?", (case_id,)).fetchone()
                if not thread:
                    _upsert_thread(connection, case)
                    thread = connection.execute("SELECT * FROM case_threads WHERE case_id=?", (case_id,)).fetchone()
                if thread["closed_at"]:
                    raise CaseConversationError("conversation_read_only", "该问题对话不可写。", 409)
                active = connection.execute(
                    """
                    SELECT 1 FROM case_messages WHERE case_id=? AND author='user'
                    AND message_kind='question' AND status IN ('pending','running')
                    """,
                    (case_id,),
                ).fetchone()
                if active:
                    raise CaseConversationError("answer_in_progress", "AI 正在回答当前问题，请等待完成。", 409)
                sequence = _next_sequence(connection, case_id)
                connection.execute(
                    """
                    INSERT INTO case_messages(
                      message_id,case_id,sequence,case_version,context_hash,evidence_watermark,
                      author,message_kind,content,content_hash,status,retry_of,retry_count,
                      ai_model,ai_reasoning_effort,ai_speed,archive_after_reply,run_id,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,'user','question',?,?,'pending',?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        message_id, case_id, sequence, int(case["case_version"]), str(case["context_hash"]),
                        str(case.get("evidence_watermark") or ""), content, content_hash,
                        retry_of, max(0, int(retry_count)), ai_profile["model"],
                        ai_profile["reasoning_effort"], ai_profile["speed"],
                        1 if archive_after_reply else 0, run_id, stamp, stamp,
                    ),
                )
                attention.record_saved_reply(connection, case, message_id, archive_after_reply)
                connection.execute("DELETE FROM case_drafts WHERE case_id=?", (case_id,))
                connection.execute(
                    "UPDATE case_threads SET active_message_id=?,last_error_code=NULL WHERE case_id=?",
                    (message_id, case_id),
                )
                connection.execute(
                    "UPDATE case_proposals SET status='superseded',superseded_at=? WHERE case_id=? AND status='active'",
                    (stamp, case_id),
                )
                conversation_hash = _refresh_thread(connection, case_id, stamp=stamp)
                connection.execute(
                    """
                    INSERT INTO case_ai_runs(
                      ai_run_id,action_run_id,message_id,case_id,case_version,context_hash,
                      evidence_watermark,basis_conversation_hash,basis_head_sequence,
                      fencing_token,attempt,ai_model,ai_reasoning_effort,ai_speed,state,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)
                    """,
                    (
                        ai_run_id, run_id, message_id, case_id, int(case["case_version"]),
                        str(case["context_hash"]), str(case.get("evidence_watermark") or ""),
                        conversation_hash, sequence, sequence, max(1, int(retry_count) + 1),
                        ai_profile["model"], ai_profile["reasoning_effort"], ai_profile["speed"],
                        stamp, stamp,
                    ),
                )
                _append_event(
                    connection, case_id, "user_message_submitted",
                    {
                        "message_id": message_id,
                        "ai_run_id": ai_run_id,
                        "retry": bool(retry_of),
                        "ai_profile": ai_profile,
                        "archive_after_reply": archive_after_reply,
                    },
                    context_hash=str(case["context_hash"]),
                    evidence_watermark=str(case.get("evidence_watermark") or ""), stamp=stamp,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        try:
            if ai_enabled:
                _spawn_worker(config_path, message_id)
            else:
                _mark_failed(message_id, "case_ai_disabled")
                syncv2.finish_action_run(run_id, "failed", error_code="case_ai_disabled")
        except OSError:
            _mark_failed(message_id, "case_worker_unavailable")
            syncv2.finish_action_run(run_id, "failed", error_code="case_worker_unavailable")
    except Exception:
        if run_id:
            connection = syncv2.connect()
            try:
                row = connection.execute("SELECT 1 FROM action_runs WHERE run_id=? AND status='running'", (run_id,)).fetchone()
            finally:
                connection.close()
            if row:
                syncv2.finish_action_run(run_id, "failed", error_code="message_submit_failed")
        raise
    connection = syncv2.connect_readonly()
    try:
        saved = connection.execute("SELECT * FROM case_messages WHERE message_id=?", (message_id,)).fetchone()
        return _message_submit_result(connection, saved, idempotent_replay=False)
    finally:
        connection.close()



def _question_document(case_id: str, message_id: str) -> dict[str, Any]:
    case = reconciliation.case_detail(case_id)
    connection = syncv2.connect_readonly()
    try:
        thread = connection.execute("SELECT * FROM case_threads WHERE case_id=?", (case_id,)).fetchone()
        rows = connection.execute(
            """
            SELECT * FROM case_messages WHERE case_id=? AND sequence>(
              SELECT MAX(0,MAX(sequence)-?) FROM case_messages WHERE case_id=?
            ) ORDER BY sequence
            """,
            (case_id, RECENT_MESSAGE_LIMIT, case_id),
        ).fetchall()
        question = connection.execute(
            "SELECT * FROM case_messages WHERE message_id=? AND case_id=? AND author='user'",
            (message_id, case_id),
        ).fetchone()
    finally:
        connection.close()
    if not thread or not question:
        raise CaseConversationError("message_missing", "待回答消息不存在。", 404)
    return {
        "case": {
            "case_id": case_id,
            "title": case["title"],
            "subject": case["subject"],
            "dimension": case["dimension"],
            "case_kind": case["case_kind"],
            "assessment": case["assessment"],
            "evidence_summary": case["evidence_summary"],
            "evidence_boundary": case["evidence_boundary"],
            "manual_reason": case["manual_reason"],
            "decision_question": case["decision_question"],
            "case_version": case["case_version"],
            "context_hash": case["context_hash"],
            "evidence_watermark": case["evidence_watermark"],
        },
        "history_summary": str(thread["summary"] or ""),
        "recent_messages": [
            {
                "sequence": int(row["sequence"]),
                "author": str(row["author"]),
                "content": str(row["content"]),
                "status": str(row["status"]),
                "conversation_state": row["conversation_state"],
                "focused_questions": json.loads(str(row["focused_questions_json"] or "[]")),
                "proposed_plan": str(row["proposed_plan"] or ""),
            }
            for row in rows
        ],
        "ai_profile": {
            "model": str(question["ai_model"]),
            "reasoning_effort": str(question["ai_reasoning_effort"]),
            "speed": str(question["ai_speed"]),
        },
        "current_message_id": message_id,
    }


def invoke_case_ai(config: dict[str, Any], document: dict[str, Any], run_id: str, *,
                   prompt_path: Path = PROMPT_PATH, schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    automation = config["dingtalk_automation"]
    ai_profile = _validated_ai_profile(document.get("ai_profile"), config=config, stored=True)
    # No codex precondition: model_router calls MiMo and Kimi directly, so a
    # missing or hung codex only removes the GPT route.
    if not prompt_path.is_file() or not schema_path.is_file():
        raise CaseConversationError("conversation_contract_missing", "AI 对话规则文件缺失。", 503)
    run_dir = syncv2.RUNS_DIR / "case-conversations" / run_id
    syncv2.ensure_private_dir(run_dir)
    prompt = (
        prompt_path.read_text(encoding="utf-8").rstrip()
        + "\n\n<case_context>\n"
        + syncv2.canonical_json(document)
        + "\n</case_context>\n"
    )
    service_tier = AI_SPEEDS[ai_profile["speed"]]["service_tier"]
    speed_flags = ["-c", f'service_tier="{service_tier}"'] if service_tier else []
    import model_router
    prompt += memory.mimo_schema_prompt(schema_path)
    ai_lock = memory.acquire_lock(syncv2.AI_LOCK, blocking=True)
    try:
        try:
            # The model picked in the workbench goes first; the usual chain backs it up.
            routed = model_router.complete(
                config, prompt=prompt, purpose="case_conversation", workdir=run_dir,
                request_context={"scope":"case-conversation"},
                timeout=int(automation.get("codex_timeout_seconds") or 900),
                chain=model_router.chain_preferring(config, ai_profile["model"]),
                reasoning_effort=ai_profile["reasoning_effort"], codex_extra_flags=speed_flags,
                validate=memory.parse_model_json,
            )
        except model_router.RouterExhausted as exc:
            if exc.category == "timeout":
                raise CaseConversationError("codex_timeout", "AI 回答超时，消息已保留，可重试。", 503) from exc
            raise CaseConversationError(model_router.legacy_error_code(exc),
                                        "AI 暂时没有完成回答，消息已保留，可重试。", 503) from exc
    finally:
        ai_lock.close()
    payload = routed.value
    answer = str(payload.get("answer") or "").strip()
    state = str(payload.get("conversation_state") or "")
    questions = payload.get("focused_questions")
    proposed_plan = str(payload.get("proposed_plan") or "").strip()
    limitations = payload.get("limitations")
    if (
        not answer or len(answer) > ANSWER_MAX_CHARS or state not in CONVERSATION_STATES
        or not isinstance(questions, list) or len(questions) > 3 or not isinstance(limitations, list)
    ):
        raise CaseConversationError("codex_invalid_answer", "AI 回答内容无效，消息已保留，可重试。", 503)
    clean_questions = [str(value).strip()[:500] for value in questions if str(value).strip()][:3]
    clean_limitations = [str(value).strip()[:500] for value in limitations if str(value).strip()][:8]
    if clean_limitations:
        answer += "\n\n回答边界：\n" + "\n".join(f"- {value}" for value in clean_limitations)
    candidate = payload.get("memory_candidate")
    if candidate is not None and not isinstance(candidate, dict):
        candidate = None
    return {
        "answer": answer,
        "conversation_state": state,
        "focused_questions": clean_questions,
        "proposed_plan": proposed_plan[:8000],
        "memory_candidate": candidate,
    }


def _is_high_risk(case: dict[str, Any], candidate: dict[str, Any]) -> bool:
    text = " ".join((
        str(case.get("dimension") or ""), str(candidate.get("title") or ""),
        str(candidate.get("text") or ""),
    )).casefold()
    return str(case.get("dimension") or "") in HIGH_RISK_DIMENSIONS or any(term.casefold() in text for term in HIGH_RISK_TERMS)


def _proposal_payload(
    case: dict[str, Any],
    source_message: sqlite3.Row,
    assistant_message_id: str,
    candidate: object,
) -> Optional[dict[str, Any]]:
    if not isinstance(candidate, dict):
        return None
    title = business_router.single_line(candidate.get("title"), 160)
    text = business_router.normalize_text(candidate.get("text"), 2000)
    boundary = business_router.single_line(candidate.get("evidence_boundary"), 1000)
    if not title or not text or not boundary:
        return None
    source_content = str(source_message["content"])
    stated_owner = str(candidate.get("statement_kind") or "") == "owner_statement"
    owner_language = any(term in source_content for term in OWNER_STATEMENT_TERMS)
    high_risk = _is_high_risk(case, candidate)
    fact_status = "owner_statement" if stated_owner and owner_language and not high_risk else "review_required"
    if fact_status == "owner_statement":
        boundary = (
            f"仅记录 Example Owner 于 {str(source_message['created_at'])} 表达的本人决定、偏好或规则；"
            "不代表公司审批、客户确认、技术签署或外部事实成立。"
        )
    else:
        boundary = f"{boundary}；当前仅为用户补充线索，须由受控权威证据复核，AI 回答不作为来源。"
    routing_candidate = {
        "title": title,
        "summary": text,
        "memory_content": text,
        "evidence_boundary": boundary,
        "category": "internal_material" if fact_status == "owner_statement" else "business_cognition",
    }
    try:
        target_page = business_router.infer_target_page(routing_candidate)
    except business_router.RoutingError:
        return None
    proposal_id = str(uuid.uuid4())
    source_attribution = f"Example Owner 本人于 {str(source_message['created_at'])} 在问题工作台补充"
    material = {
        "proposal_id": proposal_id,
        "case_id": case["case_id"],
        "source_message_id": str(source_message["message_id"]),
        "assistant_message_id": assistant_message_id,
        "title": title,
        "proposal_text": text,
        "target_page": target_page,
        "source_attribution": source_attribution,
        "fact_status": fact_status,
        "evidence_boundary": boundary,
        "risk_level": "high" if high_risk else "normal",
    }
    material["proposal_hash"] = syncv2.sha256_text(syncv2.canonical_json(material))
    return material


def _update_summary(connection: sqlite3.Connection, case_id: str) -> None:
    rows = connection.execute(
        "SELECT sequence,author,content,status FROM case_messages WHERE case_id=? ORDER BY sequence",
        (case_id,),
    ).fetchall()
    if len(rows) <= RECENT_MESSAGE_LIMIT:
        return
    older = rows[:-SUMMARY_KEEP_RECENT]
    lines = []
    for row in older:
        content = " ".join(str(row["content"]).split())[:500]
        lines.append(f"{row['sequence']}｜{row['author']}｜{row['status']}｜{content}")
    summary = "\n".join(lines)[-12000:]
    connection.execute(
        "UPDATE case_threads SET summary=?,summary_through_sequence=? WHERE case_id=?",
        (summary, int(older[-1]["sequence"]), case_id),
    )


def _mark_failed(message_id: str, error_code: str) -> None:
    stamp = syncv2.now_iso()
    with syncv2.file_lock(syncv2.DECISION_LOCK, blocking=True):
        connection = syncv2.connect()
        try:
            row = connection.execute(
                "SELECT case_id,context_hash,evidence_watermark,status FROM case_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if not row:
                return
            case_id = str(row["case_id"])
            connection.execute(
                "UPDATE case_messages SET status='failed',error_code=?,updated_at=? WHERE message_id=? AND status IN ('pending','running')",
                (error_code[:120], stamp, message_id),
            )
            run_state = "timed_out" if error_code == "codex_timeout" else "failed"
            connection.execute(
                """
                UPDATE case_ai_runs SET state=?,last_error_code=?,updated_at=?,completed_at=?
                WHERE message_id=? AND state IN ('queued','running')
                """,
                (run_state, error_code[:120], stamp, stamp, message_id),
            )
            connection.execute(
                "UPDATE case_threads SET active_message_id=NULL,last_error_code=? WHERE case_id=?",
                (error_code[:120], case_id),
            )
            _refresh_thread(connection, case_id, stamp=stamp)
            _append_event(
                connection, case_id, "ai_run_failed",
                {"message_id": message_id, "error_code": error_code[:120]},
                context_hash=str(row["context_hash"]),
                evidence_watermark=str(row["evidence_watermark"]), stamp=stamp,
            )
            connection.commit()
        finally:
            connection.close()


def answer_message(config: dict[str, Any], message_id: str, *, invoke: Optional[Callable[..., dict[str, Any]]] = None) -> dict[str, Any]:
    connection = syncv2.connect_readonly()
    try:
        question = connection.execute(
            "SELECT * FROM case_messages WHERE message_id=? AND author='user' AND message_kind='question'",
            (message_id,),
        ).fetchone()
    finally:
        connection.close()
    if not question:
        raise CaseConversationError("message_missing", "待回答消息不存在。", 404)
    case_id = str(question["case_id"])
    run_id = str(question["run_id"] or "")
    try:
        stamp = syncv2.now_iso()
        connection = syncv2.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ai_run = connection.execute(
                "SELECT * FROM case_ai_runs WHERE message_id=?", (message_id,)
            ).fetchone()
            if not ai_run or str(ai_run["state"]) != "queued":
                raise CaseConversationError("message_not_pending", "该问题已经在回答或已完成。", 409)
            cursor = connection.execute(
                "UPDATE case_messages SET status='running',model_called=1,updated_at=? WHERE message_id=? AND status='pending'",
                (stamp, message_id),
            )
            if cursor.rowcount != 1:
                raise CaseConversationError("message_not_pending", "该问题已经在回答或已完成。", 409)
            running_hash = _refresh_thread(connection, case_id, stamp=stamp)
            cursor = connection.execute(
                """
                UPDATE case_ai_runs SET state='running',basis_conversation_hash=?,updated_at=?
                WHERE ai_run_id=? AND state='queued'
                """,
                (running_hash, stamp, str(ai_run["ai_run_id"])),
            )
            if cursor.rowcount != 1:
                raise CaseConversationError("message_not_pending", "该问题已经在回答或已完成。", 409)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        syncv2.set_action_step(run_id, "case_prepare", 10, "整理问题上下文", "running")
        document = _question_document(case_id, message_id)
        syncv2.set_action_step(run_id, "case_prepare", 10, "整理问题上下文", "success")
        syncv2.set_action_step(run_id, "case_ai", 20, "调用 AI 协商", "running")
        response = (invoke or invoke_case_ai)(config, document, run_id)
        syncv2.set_action_step(run_id, "case_ai", 20, "调用 AI 协商", "success")
        case = reconciliation.case_detail(case_id)
        answer_id = str(uuid.uuid4())
        stamp = syncv2.now_iso()
        with syncv2.file_lock(syncv2.DECISION_LOCK, blocking=True):
            connection = syncv2.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                source_message = connection.execute("SELECT * FROM case_messages WHERE message_id=?", (message_id,)).fetchone()
                ai_run = connection.execute("SELECT * FROM case_ai_runs WHERE message_id=?", (message_id,)).fetchone()
                thread = connection.execute("SELECT * FROM case_threads WHERE case_id=?", (case_id,)).fetchone()
                head_sequence = int(connection.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM case_messages WHERE case_id=?", (case_id,)
                ).fetchone()[0])
                stale = (
                    not source_message or not ai_run or not thread
                    or str(source_message["status"]) != "running"
                    or str(ai_run["state"]) != "running"
                    or int(ai_run["case_version"]) != int(case["case_version"])
                    or str(ai_run["context_hash"]) != str(case["context_hash"])
                    or str(ai_run["evidence_watermark"]) != str(case.get("evidence_watermark") or "")
                    or str(ai_run["basis_conversation_hash"]) != str(thread["conversation_hash"])
                    or int(ai_run["basis_head_sequence"]) != head_sequence
                    or str(thread["active_message_id"] or "") != message_id
                )
                if stale:
                    connection.execute(
                        "UPDATE case_messages SET status='failed',error_code='ai_response_superseded',updated_at=? WHERE message_id=? AND status='running'",
                        (stamp, message_id),
                    )
                    connection.execute(
                        """
                        UPDATE case_ai_runs SET state='superseded',last_error_code='ai_response_superseded',
                          updated_at=?,completed_at=? WHERE message_id=? AND state='running'
                        """,
                        (stamp, stamp, message_id),
                    )
                    connection.execute(
                        "UPDATE case_threads SET active_message_id=NULL,last_error_code='ai_response_superseded' WHERE case_id=?",
                        (case_id,),
                    )
                    _refresh_thread(connection, case_id, stamp=stamp)
                    _append_event(
                        connection, case_id, "ai_response_superseded", {"message_id": message_id},
                        context_hash=str(case["context_hash"]),
                        evidence_watermark=str(case.get("evidence_watermark") or ""), stamp=stamp,
                    )
                    connection.commit()
                    raise CaseConversationError(
                        "ai_response_superseded",
                        "回答期间问题证据或上下文已经变化，本轮晚到回答未写入，可基于新状态重试。",
                        409,
                    )
                sequence = _next_sequence(connection, case_id)
                connection.execute(
                    """
                    INSERT INTO case_messages(
                      message_id,case_id,sequence,case_version,context_hash,evidence_watermark,
                      author,message_kind,content,content_hash,status,conversation_state,
                      focused_questions_json,proposed_plan,reply_to,ai_model,ai_reasoning_effort,
                      ai_speed,run_id,model_called,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,'assistant','answer',?,?,'completed',?,?,?,?,?,?,?,?,1,?,?)
                    """,
                    (
                        answer_id, case_id, sequence, int(case["case_version"]), str(case["context_hash"]),
                        str(case.get("evidence_watermark") or ""), response["answer"],
                        syncv2.sha256_text(response["answer"]), response["conversation_state"],
                        syncv2.canonical_json(response["focused_questions"]), response["proposed_plan"] or None,
                        message_id, str(source_message["ai_model"]),
                        str(source_message["ai_reasoning_effort"]), str(source_message["ai_speed"]),
                        run_id, stamp, stamp,
                    ),
                )
                connection.execute(
                    "UPDATE case_messages SET status='completed',error_code=NULL,updated_at=? WHERE message_id=?",
                    (stamp, message_id),
                )
                connection.execute(
                    """
                    UPDATE case_threads SET active_message_id=NULL,last_error_code=NULL,
                      conversation_state=? WHERE case_id=?
                    """,
                    (response["conversation_state"], case_id),
                )
                _update_summary(connection, case_id)
                conversation_hash = _refresh_thread(connection, case_id, stamp=stamp)
                plan_proposal_id = None
                proposed_plan = str(response.get("proposed_plan") or "").strip()
                if response["conversation_state"] == "proposal_ready" and proposed_plan:
                    revision = int(connection.execute(
                        "SELECT COALESCE(MAX(revision),0)+1 FROM case_proposals WHERE case_id=?", (case_id,)
                    ).fetchone()[0])
                    plan_proposal_id = str(uuid.uuid4())
                    plan_material = {
                        "proposal_id": plan_proposal_id,
                        "case_id": case_id,
                        "source_message_id": message_id,
                        "assistant_message_id": answer_id,
                        "revision": revision,
                        "proposal_text": proposed_plan,
                        "conversation_hash": conversation_hash,
                    }
                    proposal_hash = syncv2.sha256_text(syncv2.canonical_json(plan_material))
                    connection.execute(
                        "UPDATE case_proposals SET status='superseded',superseded_at=? WHERE case_id=? AND status='active'",
                        (stamp, case_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO case_proposals(
                          proposal_id,case_id,source_message_id,assistant_message_id,revision,
                          proposal_text,proposal_hash,conversation_hash,status,created_at
                        ) VALUES(?,?,?,?,?,?,?,?, 'active',?)
                        """,
                        (
                            plan_proposal_id, case_id, message_id, answer_id, revision,
                            proposed_plan, proposal_hash, conversation_hash, stamp,
                        ),
                    )
                proposal = _proposal_payload(case, source_message, answer_id, response.get("memory_candidate"))
                if proposal:
                    connection.execute(
                        """
                        INSERT INTO case_memory_proposals(
                          proposal_id,case_id,source_message_id,assistant_message_id,title,proposal_text,
                          target_page,source_attribution,fact_status,evidence_boundary,risk_level,
                          proposal_hash,status,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?)
                        """,
                        (
                            proposal["proposal_id"], case_id, proposal["source_message_id"], answer_id,
                            proposal["title"], proposal["proposal_text"], proposal["target_page"],
                            proposal["source_attribution"], proposal["fact_status"],
                            proposal["evidence_boundary"], proposal["risk_level"], proposal["proposal_hash"],
                            stamp, stamp,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE case_ai_runs SET state='completed',last_error_code=NULL,updated_at=?,completed_at=?
                    WHERE message_id=? AND state='running'
                    """,
                    (stamp, stamp, message_id),
                )
                _append_event(
                    connection, case_id, "ai_answer_completed",
                    {
                        "message_id": message_id,
                        "assistant_message_id": answer_id,
                        "conversation_state": response["conversation_state"],
                        "plan_proposal_id": plan_proposal_id,
                    },
                    context_hash=str(case["context_hash"]),
                    evidence_watermark=str(case.get("evidence_watermark") or ""), stamp=stamp,
                )
                archive_requested = bool(source_message["archive_after_reply"])
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        syncv2.finish_action_run(run_id, "success", result={"answered": 1})
        if archive_requested and response["conversation_state"] == "waiting_evidence":
            attention.mark_waiting(case_id, message_id)
        return {
            "status": "success", "message_id": answer_id,
            "conversation_hash": conversation_hash,
            "archive_after_reply": archive_requested,
            "archived": (attention.row(case_id) or {}).get("attention_state") in attention.CLOSED,
        }

    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__))
        connection = syncv2.connect_readonly()
        try:
            current_message = connection.execute(
                "SELECT status,error_code FROM case_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        finally:
            connection.close()
        archived_cancellation = bool(
            current_message
            and str(current_message["status"]) == "cancelled"
            and str(current_message["error_code"] or "") == "case_archived"
        )
        if code != "message_not_pending":
            if code != "ai_response_superseded":
                _mark_failed(message_id, code)
            if run_id and not archived_cancellation:
                syncv2.finish_action_run(run_id, "failed", error_code=code[:120])
        raise


def retry_message(config_path: Path, case_id: str, message_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    case_id = _case_id(case_id)
    if not {"case_version", "context_hash"}.issubset(payload) or set(payload) - {"case_version", "context_hash", "client_message_id"}:
        raise CaseConversationError("retry_contract", "重试请求字段无效。")
    connection = syncv2.connect_readonly()
    try:
        failed = connection.execute(
            """
            SELECT * FROM case_messages WHERE case_id=? AND message_id=? AND author='user'
              AND message_kind='question' AND status='failed'
            """,
            (case_id, message_id),
        ).fetchone()
        retry_count = int(connection.execute(
            "SELECT COUNT(*) FROM case_messages WHERE case_id=? AND retry_of=?", (case_id, message_id)
        ).fetchone()[0])
        submitted = connection.execute(
            "SELECT 1 FROM case_messages WHERE message_id=?", (str(payload.get("client_message_id") or ""),)
        ).fetchone()
    finally:
        connection.close()
    if not failed:
        raise CaseConversationError("retry_invalid", "只能重试失败的问题。", 409)
    if retry_count >= 3 and not submitted:
        raise CaseConversationError("retry_limit", "该问题已达到重试上限，请补充信息后重新发送。", 409)
    stored_profile = {
        "model": str(failed["ai_model"]),
        "reasoning_effort": str(failed["ai_reasoning_effort"]),
        "speed": str(failed["ai_speed"]),
    }
    if not all(stored_profile.values()):
        stored_profile = dict(DEFAULT_AI_PROFILE)
    enriched = {
        "content": str(failed["content"]),
        "case_version": payload["case_version"],
        "context_hash": payload["context_hash"],
        "ai_profile": retry_ai_profile(stored_profile),
        "client_message_id": payload.get("client_message_id") or str(uuid.uuid4()),
    }
    return append_message(
        config_path,
        case_id,
        enriched,
        retry_of=message_id,
        retry_count=retry_count + 1,
        archive_after_reply_override=bool(failed["archive_after_reply"]),
    )


def _memory_target_state(config: dict[str, Any], target_page: str) -> tuple[Path, str]:
    try:
        vault_root = business_router.vault_root_from_config(config)
        relative = business_router.safe_relative_page(target_page, vault_root)
        candidate = vault_root / relative
        target = candidate.resolve()
        if not any(target == root or root in target.parents for root in business_router.canonical_roots(vault_root)):
            raise business_router.RoutingError("target_page", "memory target escaped the allowlist")
        if candidate.is_symlink() or (target.exists() and not target.is_file()):
            raise business_router.RoutingError("target_page", "memory target is not a regular file")
        return target, business_router.sha256_file(target) if target.is_file() else "missing"
    except (KeyError, business_router.RoutingError) as exc:
        raise CaseConversationError("memory_target_invalid", "业务记忆目标当前不可安全访问。", 503) from exc


def preview_action(config: dict[str, Any], case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    flags = config.get("case_workbench") or {}
    if not bool(flags.get("actions_enabled", True)):
        raise CaseConversationError("case_actions_disabled", "问题处理动作已由服务端暂停。", 503)
    if str(payload.get("action") or "") == "write_memory" and not bool(flags.get("memory_write_enabled", True)):
        raise CaseConversationError("case_memory_disabled", "业务记忆写入已由服务端暂停。", 503)
    snapshot = conversation_snapshot(case_id)
    memory_proposal = snapshot["memory_proposal"]
    if str(payload.get("action") or "") == "write_memory" and memory_proposal:
        memory_proposal = dict(memory_proposal)
        _target, memory_proposal["target_before_hash"] = _memory_target_state(
            config, str(memory_proposal["target_page"])
        )
    return reconciliation.preview_case(
        case_id,
        payload,
        current_conversation_hash=snapshot["conversation_hash"],
        completed_exchanges=int(snapshot["completed_exchanges"]),
        current_proposed_plan=str(snapshot["latest_proposed_plan"] or ""),
        current_plan_proposal=snapshot["plan_proposal"],
        eligible_memory_proposal=memory_proposal,
    )


def _memory_proposal_row(proposal_id: str) -> sqlite3.Row:
    connection = syncv2.connect_readonly()
    try:
        row = connection.execute(
            "SELECT * FROM case_memory_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        raise CaseConversationError("memory_proposal_missing", "记忆候选不存在。", 404)
    return row


def apply_memory_proposal(config: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
    proposal_id = str(preview.get("memory_proposal_id") or "")
    proposal = _memory_proposal_row(proposal_id)
    if str(proposal["status"]) == "applied":
        return {"status": "already_applied", "proposal_id": proposal_id, "claim_id": proposal["claim_id"]}
    if str(proposal["status"]) != "pending":
        raise CaseConversationError("memory_proposal_ineligible", "记忆候选当前不可写入。", 409)
    params = json.loads(str(preview.get("action_params_json") or "{}"))
    if str(params.get("proposal_hash") or "") != str(proposal["proposal_hash"]):
        raise CaseConversationError("memory_proposal_stale", "记忆候选已经变化，请重新预览。", 409)
    _target, actual_target_hash = _memory_target_state(config, str(proposal["target_page"]))
    if actual_target_hash != str(preview.get("target_before_hash") or ""):
        raise CaseConversationError("memory_target_stale", "业务记忆目标在确认后发生变化，请重新预览。", 409)
    source_message = None
    connection = syncv2.connect_readonly()
    try:
        source_message = connection.execute(
            "SELECT * FROM case_messages WHERE message_id=?", (str(proposal["source_message_id"]),)
        ).fetchone()
    finally:
        connection.close()
    if not source_message:
        raise CaseConversationError("memory_source_missing", "记忆候选的用户来源消息不存在。", 409)
    candidate = {
        "title": str(proposal["title"]),
        "summary": str(proposal["proposal_text"]),
        "memory_content": str(proposal["proposal_text"]),
        "evidence_boundary": str(proposal["evidence_boundary"]),
        "evidence_state": "pending",
        "risk_level": str(proposal["risk_level"]),
        "fact_status": str(proposal["fact_status"]),
        "source_attribution": str(proposal["source_attribution"]),
        "category": "internal_material" if str(proposal["fact_status"]) == "owner_statement" else "business_cognition",
        "dedupe_key": f"case-memory-proposal:{proposal_id}",
    }
    derived_target = business_router.infer_target_page(candidate)
    if derived_target != str(proposal["target_page"]):
        raise CaseConversationError("memory_route_changed", "记忆归属页面已经变化，请重新生成候选。", 409)
    try:
        path, content, route = business_router.route_claim_change(
            config, candidate, source_system="case_conversation",
            source_ref=f"问题工作台 {proposal['case_id']} 用户消息 {proposal['source_message_id']}",
            observed_at=str(source_message["created_at"]),
        )
        result = memory.apply_changes(
            config, f"case-memory-{proposal_id}", {path: content}
        )
    except (business_router.RoutingError, memory.AutomationError) as exc:
        raise CaseConversationError(str(exc.code), "业务记忆写入失败，候选和对话已保留。", 503) from exc
    stamp = syncv2.now_iso()
    connection = syncv2.connect()
    try:
        connection.execute(
            """
            UPDATE case_memory_proposals SET status='applied',claim_id=?,last_error_code=NULL,
              applied_at=?,updated_at=? WHERE proposal_id=? AND status='pending'
            """,
            (route.claim_id, stamp, stamp, proposal_id),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "status": "applied", "proposal_id": proposal_id, "claim_id": route.claim_id,
        "target_page": route.target_page, "fact_status": str(proposal["fact_status"]),
        "writes": int(result.get("writes") or 0), "validated": bool(result.get("validated")),
    }


def _preview_metadata(token: str) -> dict[str, Any]:
    token_hash = reconciliation.sha256_text(token)
    connection = reconciliation.connect(readonly=True)
    try:
        row = connection.execute("SELECT * FROM case_previews WHERE token_hash=?", (token_hash,)).fetchone()
    finally:
        connection.close()
    if not row:
        raise CaseConversationError("preview_invalid", "预览不存在，请重新预览。", 409)
    return dict(row)


def _archive_params(trigger: str, source_message_id: Optional[str]) -> dict[str, Any]:
    if trigger not in {"manual", "reply_after_answer"}:
        raise CaseConversationError("archive_trigger_invalid", "归档触发方式无效。", 409)
    params: dict[str, Any] = {"trigger": trigger}
    if source_message_id:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(source_message_id)):
            raise CaseConversationError("archive_contract", "归档来源消息无效。", 409)
        params["source_message_id"] = str(source_message_id)
    return params


def archive_case(
    config: dict[str, Any],
    case_id: str,
    payload: dict[str, Any],
    *,
    trigger: str = "manual",
    source_message_id: Optional[str] = None,
) -> dict[str, Any]:
    """Archive one concrete case through a durable, replayable receipt."""
    case_id = _case_id(case_id)
    flags = config.get("case_workbench") or {}
    if not bool(flags.get("actions_enabled", True)):
        raise CaseConversationError("case_actions_disabled", "问题处理动作已由服务端暂停。", 503)
    required = {"case_version", "context_hash", "conversation_hash", "idempotency_key"}
    if set(payload) != required:
        raise CaseConversationError("archive_contract", "归档请求字段无效。", 409)
    idempotency_key = str(payload.get("idempotency_key") or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", idempotency_key):
        raise CaseConversationError("archive_contract", "归档请求缺少有效幂等键。", 409)
    params = _archive_params(trigger, source_message_id)
    params_json = syncv2.canonical_json(params)
    params_hash = syncv2.sha256_text(params_json)
    requested_hash = str(payload.get("conversation_hash") or "")
    with syncv2.file_lock(syncv2.DECISION_LOCK, blocking=True):
        case = reconciliation.case_detail(case_id)
        connection = syncv2.connect()
        try:
            receipt = connection.execute(
                "SELECT * FROM case_action_receipts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if receipt:
                actual = (
                    str(receipt["case_id"]),
                    str(receipt["action"]),
                    str(receipt["action_params_hash"]),
                    int(receipt["case_version"]),
                    str(receipt["context_hash"]),
                    str(receipt["conversation_hash"]),
                )
                expected = (
                    case_id,
                    "archive",
                    params_hash,
                    int(payload.get("case_version") or 0),
                    str(payload.get("context_hash") or ""),
                    requested_hash,
                )
                if actual != expected:
                    raise CaseConversationError(
                        "idempotency_conflict", "幂等键已绑定其他归档请求。", 409
                    )
                if str(receipt["state"]) == "committed":
                    return json.loads(str(receipt["result_json"] or "{}"))
                if str(receipt["state"]) == "failed":
                    raise CaseConversationError(
                        str(receipt["last_error_code"] or "archive_incomplete"),
                        "上一次归档未完成；证据或对话可能已经变化。",
                        409,
                    )
            else:
                thread = connection.execute(
                    "SELECT * FROM case_threads WHERE case_id=?", (case_id,)
                ).fetchone()
                if (
                    not thread
                    or thread["closed_at"]
                    or str(case["state"]) != "manual_required"
                    or int(payload.get("case_version") or 0) != int(case["case_version"])
                    or str(payload.get("context_hash") or "") != str(case["context_hash"])
                    or requested_hash != str(thread["conversation_hash"])
                ):
                    raise CaseConversationError(
                        "archive_stale", "证据、对话或事项状态已经变化，请刷新后重试。", 409
                    )
                stamp = syncv2.now_iso()
                connection.execute(
                    """
                    INSERT INTO case_action_receipts(
                      idempotency_key,case_id,preview_token_hash,action,action_params_json,
                      action_params_hash,case_version,context_hash,evidence_watermark,
                      conversation_hash,maintenance_epoch,state,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)
                    """,
                    (
                        idempotency_key,
                        case_id,
                        syncv2.sha256_text("direct-archive"),
                        "archive",
                        params_json,
                        params_hash,
                        int(case["case_version"]),
                        str(case["context_hash"]),
                        str(case.get("evidence_watermark") or ""),
                        requested_hash,
                        reconciliation.maintenance_epoch(),
                        stamp,
                        stamp,
                    ),
                )
                connection.commit()
        finally:
            connection.close()
        try:
            result = reconciliation.archive_case_direct(
                case_id,
                payload,
                current_conversation_hash=requested_hash,
                trigger=trigger,
                source_message_id=source_message_id,
            )
        except Exception as exc:
            if isinstance(exc, reconciliation.ReconciliationError):
                connection = syncv2.connect()
                try:
                    connection.execute(
                        """
                        UPDATE case_action_receipts SET state='failed',last_error_code=?,updated_at=?
                        WHERE idempotency_key=? AND state='prepared'
                        """,
                        (str(exc.code)[:120], syncv2.now_iso(), idempotency_key),
                    )
                    connection.commit()
                finally:
                    connection.close()
            raise
        archived_case = reconciliation.case_detail(case_id)
        connection = syncv2.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            thread = connection.execute(
                "SELECT active_message_id FROM case_threads WHERE case_id=?", (case_id,)
            ).fetchone()
            cancelled_active_reply = bool(thread and thread["active_message_id"])
            attention.record(connection, case, state="answered", reason="你已手动归档此事项；后续仍保留证据和处置记录。", source_kind="manual_archive")
            _upsert_thread(connection, archived_case)
            final_hash = _refresh_thread(connection, case_id)
            stored_result = {
                **result,
                "conversation_hash": final_hash,
                "cancelled_active_reply": cancelled_active_reply,
            }
            stamp = syncv2.now_iso()
            connection.execute(
                """
                UPDATE case_action_receipts SET state='committed',result_json=?,
                  last_error_code=NULL,updated_at=?,committed_at=?
                WHERE idempotency_key=?
                """,
                (syncv2.canonical_json(stored_result), stamp, stamp, idempotency_key),
            )
            connection.commit()
            return stored_result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def recover_pending_archives(config: dict[str, Any]) -> dict[str, int]:
    """Finish prepared receipts and completed reply-triggered archive intents."""
    counts: dict[str, int] = {
        "restored": 0,
        "already_archived": 0,
        "stale": 0,
        "failed": 0,
    }
    connection = syncv2.connect_readonly()
    try:
        prepared = connection.execute(
            "SELECT * FROM case_action_receipts WHERE action='archive' AND state='prepared' ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    for row in prepared:
        try:
            params = json.loads(str(row["action_params_json"] or "{}"))
            result = archive_case(
                config,
                str(row["case_id"]),
                {
                    "case_version": int(row["case_version"]),
                    "context_hash": str(row["context_hash"]),
                    "conversation_hash": str(row["conversation_hash"]),
                    "idempotency_key": str(row["idempotency_key"]),
                },
                trigger=str(params.get("trigger") or "manual"),
                source_message_id=str(params.get("source_message_id") or "") or None,
            )
            counts["already_archived" if result.get("state") == "archived" else "restored"] += 1
        except (CaseConversationError, reconciliation.ReconciliationError) as exc:
            counts["stale" if str(exc.code) in {"archive_stale", "case_stale"} else "failed"] += 1
    migrated = attention.backfill_legacy_replies()
    counts["restored"] += migrated["archived"]
    return counts


def apply_action(config: dict[str, Any], case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    case_id = _case_id(case_id)
    flags = config.get("case_workbench") or {}
    if not bool(flags.get("actions_enabled", True)):
        raise CaseConversationError("case_actions_disabled", "问题处理动作已由服务端暂停。", 503)
    if set(payload) != {"preview_token", "idempotency_key"}:
        raise CaseConversationError("apply_contract", "应用请求只能包含预览凭证和幂等键。", 409)
    token = str(payload["preview_token"])
    idempotency_key = str(payload["idempotency_key"])
    preview = _preview_metadata(token)
    if str(preview["case_id"]) != case_id:
        raise CaseConversationError("preview_invalid", "预览不属于当前问题。", 409)
    if str(preview["action"]) == "write_memory" and not bool(flags.get("memory_write_enabled", True)):
        raise CaseConversationError("case_memory_disabled", "业务记忆写入已由服务端暂停。", 503)
    token_hash = reconciliation.sha256_text(token)
    with syncv2.file_lock(syncv2.DECISION_LOCK, blocking=True):
        snapshot = conversation_snapshot(case_id)
        connection = syncv2.connect()
        try:
            existing = connection.execute(
                "SELECT * FROM case_action_receipts WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if str(existing["case_id"]) != case_id or str(existing["preview_token_hash"]) != token_hash:
                    raise CaseConversationError("idempotency_conflict", "幂等键已绑定其他动作。", 409)
                if str(existing["state"]) == "committed":
                    return json.loads(str(existing["result_json"] or "{}"))
                raise CaseConversationError("apply_incomplete", "上一次应用未完成，请重新预览。", 409)
            stamp = syncv2.now_iso()
            connection.execute(
                """
                INSERT INTO case_action_receipts(
                  idempotency_key,case_id,preview_token_hash,action,action_params_json,
                  action_params_hash,case_version,context_hash,evidence_watermark,
                  conversation_hash,maintenance_epoch,state,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'prepared',?,?)
                """,
                (
                    idempotency_key, case_id, token_hash, str(preview["action"]),
                    str(preview["action_params_json"]), str(preview["action_params_hash"]),
                    int(preview["case_version"]), str(preview["context_hash"]),
                    str(preview["evidence_watermark"]), str(preview["conversation_hash"]),
                    str(preview["maintenance_epoch"]), stamp, stamp,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        try:
            result = reconciliation.apply_case(
                case_id, payload, current_conversation_hash=str(snapshot["conversation_hash"]),
                memory_applier=lambda record: apply_memory_proposal(config, record),
            )
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))[:120]
            connection = syncv2.connect()
            try:
                connection.execute(
                    "UPDATE case_action_receipts SET state='failed',last_error_code=?,updated_at=? WHERE idempotency_key=?",
                    (code, syncv2.now_iso(), idempotency_key),
                )
                connection.commit()
            finally:
                connection.close()
            raise
        connection = syncv2.connect()
        try:
            stamp = syncv2.now_iso()
            connection.execute("BEGIN IMMEDIATE")
            applied_case = reconciliation.case_detail(case_id)
            if str(preview["action"]) in {"resolve_plan", "continue_automatic"}:
                attention.record(connection, applied_case,
                                 state="waiting_external" if preview["action"] == "continue_automatic" else "answered",
                                 reason="你已选择处理方式，本轮无需再次确认；新资料仍会继续核对。", source_kind="manual_choice")
            _upsert_thread(connection, reconciliation.case_detail(case_id))
            connection.execute(
                """
                UPDATE case_action_receipts SET state='committed',result_json=?,last_error_code=NULL,
                  updated_at=?,committed_at=? WHERE idempotency_key=?
                """,
                (syncv2.canonical_json(result), stamp, stamp, idempotency_key),
            )
            connection.commit()
        finally:
            connection.close()
    return result


def restore_durable_actions() -> dict[str, int]:
    """Restore committed manual dispositions from the non-rebuildable runtime DB."""
    connection = syncv2.connect_readonly()
    try:
        rows = connection.execute(
            """
            SELECT r.* FROM case_action_receipts r
            WHERE r.state='committed'
              AND r.action IN ('resolve_plan','defer','continue_automatic','archive')
              AND NOT EXISTS(
                SELECT 1 FROM case_action_receipts newer
                WHERE newer.case_id=r.case_id AND newer.state='committed'
                  AND (
                    COALESCE(newer.committed_at,newer.updated_at) > COALESCE(r.committed_at,r.updated_at)
                    OR (
                      COALESCE(newer.committed_at,newer.updated_at) = COALESCE(r.committed_at,r.updated_at)
                      AND newer.idempotency_key > r.idempotency_key
                    )
                  )
              )
            ORDER BY COALESCE(r.committed_at,r.updated_at),r.idempotency_key
            """
        ).fetchall()
    finally:
        connection.close()
    counts: dict[str, int] = {"restored": 0, "already_restored": 0, "stale": 0, "expired": 0, "missing": 0, "ignored": 0, "invalid": 0}
    for row in rows:
        result = reconciliation.restore_case_disposition(dict(row))
        status = str(result.get("status") or "invalid")
        counts[status] = counts.get(status, 0) + 1
    return counts
