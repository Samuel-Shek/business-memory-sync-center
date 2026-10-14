#!/usr/bin/env python3
"""Deterministic evidence convergence for the BUSINESS memory sync center.

The canonical evidence remains in governed business pages.  This module keeps a
local, rebuildable case index and an append-only transition ledger.  It never
uses model confidence as evidence and never promotes a claim into a confirmed
business fact.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

import business_page_router as router


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DATABASE = ROOT / "state" / "claim-reconciliation.sqlite"
PRODUCTION_RESOLUTION_ARCHIVE = ROOT / "state" / "claim-resolution-archive.jsonl"
PRODUCTION_RELATION_OUTBOX = ROOT / "state" / "reconciliation-outbox"
DATABASE = PRODUCTION_DATABASE
RESOLUTION_ARCHIVE = PRODUCTION_RESOLUTION_ARCHIVE
RELATION_OUTBOX = PRODUCTION_RELATION_OUTBOX
MAINTENANCE_STATE = ROOT / "state" / "maintenance-mode.json"
POLICY_VERSION = 1
SCHEMA_VERSION = 2
PREVIEW_TTL_MINUTES = 10
RELATIONS = {"support", "refute", "supersede", "duplicate", "unrelated"}
CASE_STATES = {"open", "manual_required", "deferred", "plan_confirmed", "resolved", "archived"}
PROCESSING_STATES = {"idle", "queued", "running", "blocked", "failed"}

DIMENSION_LABELS = {
    "business_choice": "业务选择",
    "authorization": "批准与授权",
    "approval_record": "批准记录",
    "technical_signoff": "技术签署",
    "certification": "认证与证书",
    "technical_specification": "技术参数",
    "price": "价格与报价",
    "contract": "合同与条款",
    "sent": "发送状态",
    "received": "接收状态",
    "completed": "完成状态",
    "delivery": "交付与发货",
    "legal_identity": "主体与法务",
    "project_status": "项目状态",
    "ordinary_fact": "普通事实",
}

SOURCE_LABELS = {
    "dingtalk": "钉钉聊天",
    "dingtalk_chat": "钉钉聊天",
    "dingtalk_minutes": "钉钉听记",
    "minutes": "钉钉听记",
    "email": "企业邮件",
    "dingtalk_mail": "企业邮件",
    "win11": "Windows 资料",
    "chatgpt": "ChatGPT 项目",
    "wechat": "微信资料",
}


class ReconciliationError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def validate_write_scope(config: dict[str, Any]) -> None:
    """Keep alternate/test configs from mutating the production case store."""
    production_bindings = (
        _resolved(PRODUCTION_DATABASE),
        _resolved(PRODUCTION_RESOLUTION_ARCHIVE),
        _resolved(PRODUCTION_RELATION_OUTBOX),
    )
    active_bindings = (
        _resolved(DATABASE),
        _resolved(RESOLUTION_ARCHIVE),
        _resolved(RELATION_OUTBOX),
    )
    matches = tuple(active == production for active, production in zip(active_bindings, production_bindings))
    if any(matches) and not all(matches):
        raise ReconciliationError(
            "storage_scope_mixed",
            "证据收敛存储同时指向生产与隔离目录，本次没有写入。",
            503,
        )
    if not all(matches):
        return

    try:
        canonical = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconciliationError(
            "storage_scope_unavailable",
            "无法核对生产配置边界，本次没有写入。",
            503,
        ) from exc
    for key in ("memory_root", "external_root"):
        current_value = str(config.get(key) or "").strip()
        canonical_value = str(canonical.get(key) or "").strip()
        if not current_value or not canonical_value or _resolved(Path(current_value)) != _resolved(Path(canonical_value)):
            raise ReconciliationError(
                "storage_scope_mismatch",
                "当前配置不属于生产证据库，本次没有写入生产收敛状态。",
                409,
            )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def connect(*, readonly: bool = False) -> sqlite3.Connection:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        if not DATABASE.is_file():
            raise ReconciliationError("reconciliation_uninitialized", "证据收敛索引尚未建立。", 503)
        last_error: Optional[sqlite3.Error] = None
        for attempt in range(3):
            connection: Optional[sqlite3.Connection] = None
            try:
                # A normal handle lets SQLite recover a hot WAL created by an
                # interrupted writer.  query_only still makes every
                # application-level statement fail closed as read-only.
                connection = sqlite3.connect(DATABASE, timeout=5)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA query_only=ON")
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if not row or str(row["value"]) != str(SCHEMA_VERSION):
                    connection.close()
                    raise ReconciliationError(
                        "reconciliation_schema", "证据收敛索引版本不匹配。", 503
                    )
                return connection
            except ReconciliationError:
                raise
            except sqlite3.Error as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
        raise ReconciliationError(
            "reconciliation_uninitialized", "证据收敛索引暂时不可读，请重新加载。", 503
        ) from last_error
    connection = sqlite3.connect(DATABASE, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def _checkpoint(connection: sqlite3.Connection) -> None:
    """Best-effort WAL stabilization after a committed logical write."""

    try:
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    except sqlite3.Error:
        # A concurrent reader may keep the WAL alive; the read path above can
        # still consume it safely and will retry transient lock errors.
        pass


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _ensure_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def initialize() -> None:
    connection = connect()
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relation_commit_receipts (
              request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
              transaction_id TEXT NOT NULL, state TEXT NOT NULL,
              result_json TEXT NOT NULL, resolved_ids_json TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS migration_runs (
              run_id TEXT PRIMARY KEY,
              source_watermark TEXT NOT NULL,
              result_json TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS claims (
              claim_id TEXT PRIMARY KEY,
              claim_handle TEXT NOT NULL UNIQUE,
              case_id TEXT NOT NULL,
              subject_key TEXT NOT NULL,
              subject_label TEXT NOT NULL,
              dimension_key TEXT NOT NULL,
              claim_kind TEXT NOT NULL,
              governance_class TEXT NOT NULL,
              title TEXT NOT NULL,
              proposition_summary TEXT NOT NULL,
              evidence_boundary TEXT NOT NULL,
              source_label TEXT NOT NULL,
              source_fingerprint TEXT NOT NULL,
              root_fingerprint TEXT NOT NULL,
              proposition_fingerprint TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              risk TEXT NOT NULL,
              legacy_state TEXT NOT NULL,
              lifecycle TEXT NOT NULL,
              resolution_reason TEXT,
              migration_disposition TEXT NOT NULL,
              evidence_watermark TEXT NOT NULL,
              record_version INTEGER NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS claims_case_idx ON claims(case_id,lifecycle);
            CREATE TABLE IF NOT EXISTS cases (
              case_id TEXT PRIMARY KEY,
              subject_key TEXT NOT NULL,
              subject_label TEXT NOT NULL,
              dimension_key TEXT NOT NULL,
              dimension_label TEXT NOT NULL,
              case_kind TEXT NOT NULL,
              state TEXT NOT NULL,
              processing_state TEXT NOT NULL,
              title TEXT NOT NULL,
              assessment TEXT NOT NULL,
              evidence_summary TEXT NOT NULL,
              evidence_boundary TEXT NOT NULL,
              manual_reason TEXT,
              decision_question TEXT,
              allowed_choices_json TEXT NOT NULL,
              required_user_authority TEXT,
              policy_version INTEGER NOT NULL,
              evidence_watermark TEXT NOT NULL,
              context_hash TEXT NOT NULL,
              case_version INTEGER NOT NULL,
              manual_suppressed INTEGER NOT NULL DEFAULT 0,
              suppressed_context_hash TEXT,
              disposition TEXT NOT NULL DEFAULT 'unhandled',
              disposition_payload_json TEXT NOT NULL DEFAULT '{}',
              defer_until TEXT,
              disposition_context_hash TEXT,
              disposition_conversation_hash TEXT,
              disposition_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              resolved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS cases_state_idx ON cases(state,updated_at);
            CREATE TABLE IF NOT EXISTS case_claims (
              case_id TEXT NOT NULL,
              claim_id TEXT NOT NULL,
              relation TEXT NOT NULL,
              PRIMARY KEY(case_id,claim_id),
              FOREIGN KEY(case_id) REFERENCES cases(case_id) ON DELETE CASCADE,
              FOREIGN KEY(claim_id) REFERENCES claims(claim_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS evidence_relations (
              relation_id TEXT PRIMARY KEY,
              claim_id TEXT NOT NULL,
              case_id TEXT NOT NULL,
              relation TEXT NOT NULL,
              relation_status TEXT NOT NULL,
              source_system TEXT NOT NULL,
              source_label TEXT NOT NULL,
              root_fingerprint TEXT NOT NULL,
              provenance_family TEXT NOT NULL,
              independence_group TEXT NOT NULL,
              directness TEXT NOT NULL,
              authority_class TEXT NOT NULL,
              verification_method TEXT NOT NULL,
              reason TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(claim_id,relation,root_fingerprint),
              FOREIGN KEY(claim_id) REFERENCES claims(claim_id) ON DELETE CASCADE,
              FOREIGN KEY(case_id) REFERENCES cases(case_id)
            );
            CREATE TABLE IF NOT EXISTS transition_events (
              event_id TEXT PRIMARY KEY,
              object_type TEXT NOT NULL,
              object_id TEXT NOT NULL,
              previous_state TEXT NOT NULL,
              next_state TEXT NOT NULL,
              reason TEXT NOT NULL,
              actor TEXT NOT NULL,
              policy_version INTEGER NOT NULL,
              evidence_snapshot_hash TEXT NOT NULL,
              context_hash TEXT,
              transaction_id TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS transition_created_idx ON transition_events(created_at);
            CREATE TABLE IF NOT EXISTS case_previews (
              token_hash TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              case_version INTEGER NOT NULL,
              choice_id TEXT NOT NULL,
              action TEXT NOT NULL DEFAULT 'continue_automatic',
              action_params_json TEXT NOT NULL DEFAULT '{}',
              action_params_hash TEXT NOT NULL DEFAULT '',
              context_hash TEXT NOT NULL,
              evidence_watermark TEXT NOT NULL,
              conversation_hash TEXT NOT NULL DEFAULT '',
              memory_proposal_id TEXT,
              policy_version INTEGER NOT NULL DEFAULT 1,
              maintenance_epoch TEXT NOT NULL DEFAULT '',
              target_before_hash TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              used_at TEXT,
              FOREIGN KEY(case_id) REFERENCES cases(case_id)
            );
            CREATE TABLE IF NOT EXISTS idempotency_receipts (
              idempotency_key TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              choice_id TEXT NOT NULL,
              action_params_hash TEXT NOT NULL DEFAULT '',
              conversation_hash TEXT NOT NULL DEFAULT '',
              preview_token_hash TEXT NOT NULL DEFAULT '',
              receipt_state TEXT NOT NULL DEFAULT 'committed',
              error_code TEXT,
              result_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_jobs (
              job_id TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              subject_key TEXT NOT NULL,
              dimension_key TEXT NOT NULL,
              policy_version INTEGER NOT NULL,
              evidence_watermark TEXT NOT NULL,
              state TEXT NOT NULL,
              dirty INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(case_id,policy_version,evidence_watermark),
              FOREIGN KEY(case_id) REFERENCES cases(case_id)
            );
            """
        )
        for definition in (
            "disposition TEXT NOT NULL DEFAULT 'unhandled'",
            "disposition_payload_json TEXT NOT NULL DEFAULT '{}'",
            "defer_until TEXT",
            "disposition_context_hash TEXT",
            "disposition_conversation_hash TEXT",
            "disposition_at TEXT",
        ):
            _ensure_column(connection, "cases", definition)
        for definition in (
            "action TEXT NOT NULL DEFAULT 'continue_automatic'",
            "action_params_json TEXT NOT NULL DEFAULT '{}'",
            "action_params_hash TEXT NOT NULL DEFAULT ''",
            "conversation_hash TEXT NOT NULL DEFAULT ''",
            "memory_proposal_id TEXT",
            "policy_version INTEGER NOT NULL DEFAULT 1",
            "maintenance_epoch TEXT NOT NULL DEFAULT ''",
            "target_before_hash TEXT NOT NULL DEFAULT ''",
        ):
            _ensure_column(connection, "case_previews", definition)
        for definition in (
            "action_params_hash TEXT NOT NULL DEFAULT ''",
            "conversation_hash TEXT NOT NULL DEFAULT ''",
            "preview_token_hash TEXT NOT NULL DEFAULT ''",
            "receipt_state TEXT NOT NULL DEFAULT 'committed'",
            "error_code TEXT",
        ):
            _ensure_column(connection, "idempotency_receipts", definition)
        connection.execute(
            "UPDATE case_previews SET action=choice_id WHERE action='' OR action IS NULL"
        )
        stamp = now_iso()
        connection.execute(
            "INSERT INTO metadata(key,value,updated_at) VALUES('schema_version',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (str(SCHEMA_VERSION), stamp),
        )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
        _checkpoint(connection)
    finally:
        connection.close()


def maintenance_active() -> bool:
    try:
        payload = json.loads(MAINTENANCE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("status") == "active"


def maintenance_epoch() -> str:
    try:
        payload = json.loads(MAINTENANCE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"status": "inactive", "source": "missing_or_invalid"}
    return sha256_text(canonical_json(payload))


def subject_label(item: dict[str, Any]) -> str:
    relative = str(item.get("relative_path") or item.get("target_page") or "")
    stem = Path(relative).stem.replace("_", " ").strip()
    if stem and stem not in {"项目说明", "终版结论"}:
        return stem[:80]
    title = router.single_line(item.get("title"), 80)
    return title or "业务事项"


def case_kind(item: dict[str, Any]) -> str:
    claim_kind = str(item.get("claim_kind") or "fact")
    if claim_kind == "human_decision":
        return "business_choice"
    if claim_kind == "human_authorization":
        return "authorization"
    if str(item.get("state") or "") == "conflicting" and int(item.get("authoritative_conflict_count") or 0) >= 2:
        return "authoritative_conflict"
    return "fact_resolution"


def manual_reason(item: dict[str, Any]) -> Optional[str]:
    kind = case_kind(item)
    if kind == "business_choice":
        return "系统可以整理证据和比较方案，但不能替负责人选择业务方向、优先级或资源投入顺序。"
    if kind == "authorization":
        return "系统可以核对资料是否一致，但不能替技术负责人签字、批准或承担对外承诺。"
    if kind == "authoritative_conflict":
        return "两份仍然有效的权威来源互相冲突，系统不能自行选择。"
    return None


def case_id_for(subject_key: str, dimension_key: str) -> str:
    return f"case-{sha256_text(f'{subject_key}|{dimension_key}')[:20]}"


def source_label(item: dict[str, Any]) -> str:
    source = str(item.get("source_system") or "unknown")
    return SOURCE_LABELS.get(source, "受治理来源")


def source_watermark(claims: Iterable[dict[str, Any]]) -> str:
    rows = [
        (
            str(item.get("claim_id") or ""),
            str(item.get("record_version") or 1),
            str(item.get("evidence_watermark") or ""),
            str(item.get("lifecycle") or "open"),
        )
        for item in claims
    ]
    return sha256_text(canonical_json(sorted(rows)))


def _safe_summary(value: object, limit: int) -> str:
    text = router.single_line(value, limit * 2)
    text = re.sub(r"<!--.*?-->", "", text)
    text = re.sub(r"`[^`]{48,}`", "[内部标记已隐藏]", text)
    return text[:limit]


def decision_guidance(*, kind: str, dimension_key: str, titles: list[str]) -> dict[str, str]:
    matters = [router.single_line(value, 120) for value in titles if router.single_line(value, 120)]
    focus = "；".join(matters[:2]) or "当前待确认事项"
    if kind == "authorization" or dimension_key in {"technical_signoff", "approval_record", "authorization"}:
        return {
            "question": f"请确认“{focus}”是否已经完成最终批准或技术签署；如果尚未完成，由谁补齐、满足什么条件后再确认？",
            "system_finding": "系统找到了修订意见或过程性说明，但没有找到足以证明最终签字、批准或可对外承诺的受控记录。",
            "system_can_help": "系统可以汇总修订意见、比较版本、追踪新证据，并提醒缺失的签署记录。",
            "owner_must_decide": "你需要确认当前是否已有有效签署；若没有，需要指定责任人、补齐条件和下一次复核时间。",
            "why_not_automatic": "签字、批准和承诺会产生明确责任，聊天意见或 AI 推断不能代替有权限的人作出授权。",
            "after_decision": "形成的方案只记录处理路径，不会自动写成“技术已签署”或“公司已批准”。",
        }
    if kind == "authoritative_conflict":
        return {
            "question": f"请确认“{focus}”应以哪一份仍然有效的权威口径为准？",
            "system_finding": "系统发现了两份仍然有效、但结论互相冲突的权威来源。",
            "system_can_help": "系统可以列出冲突点、来源时间和影响范围，并继续等待新证据。",
            "owner_must_decide": "你需要指定采用哪一份口径，或明确由谁进一步核定。",
            "why_not_automatic": "系统没有权限在两份权威来源之间自行取舍，也不能把较新的时间简单当成业务批准。",
            "after_decision": "确认后会保留选择依据和证据边界，不会扩大为来源中未写明的事实。",
        }
    return {
        "question": f"请确认“{focus}”应采用什么业务口径和优先顺序；如果现在还不能决定，需要等待什么信息？",
        "system_finding": "系统找到了一个待定的业务方向，但现有材料只说明有人提出过方案，不能证明负责人已经选定。",
        "system_can_help": "系统可以整理备选方案、比较已有证据、记录你的考虑，并继续追踪后续变化。",
        "owner_must_decide": "你需要说明倾向采用的方向、优先级和前提条件，或者明确延期与继续观察的条件。",
        "why_not_automatic": "业务方向、优先级和资源投入会改变后续行动，必须由负责人作出选择，不能由证据数量或 AI 自动代选。",
        "after_decision": "确认后只形成可执行方案；除非另有权威证据，不会自动写成公司战略、正式批准或已承诺事实。",
    }


def plan_reconciliation(config: dict[str, Any]) -> dict[str, Any]:
    vault_root = router.vault_root_from_config(config)
    scanned = router.scan_claims(vault_root)
    active = [item for item in scanned if str(item.get("lifecycle") or "open") == "open"]
    ordered = sorted(active, key=lambda item: (str(item.get("observed_at") or ""), str(item.get("claim_id") or "")))
    duplicate_seen: dict[tuple[str, str], str] = {}
    dispositions: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in ordered:
        duplicate_key = (
            str(item.get("source_fingerprint") or ""),
            str(item.get("proposition_fingerprint") or ""),
        )
        duplicate_of = duplicate_seen.get(duplicate_key)
        if duplicate_of:
            disposition = "archived_duplicate"
        else:
            duplicate_seen[duplicate_key] = str(item["claim_id"])
            disposition = "manual_required" if manual_reason(item) else "automatic_observation"
        cid = case_id_for(str(item["subject_key"]), str(item["dimension_key"]))
        row = {
            "claim_id": str(item["claim_id"]),
            "claim_handle": str(item["claim_handle"]),
            "case_id": cid,
            "subject_key": str(item["subject_key"]),
            "subject_label": subject_label(item),
            "dimension_key": str(item["dimension_key"]),
            "claim_kind": str(item["claim_kind"]),
            "governance_class": str(item["governance_class"]),
            "title": _safe_summary(item.get("title"), 160),
            "proposition_summary": _safe_summary(item.get("summary"), 700),
            "evidence_boundary": _safe_summary(item.get("evidence_boundary"), 500),
            "source_label": source_label(item),
            "source_path": str(item.get("relative_path") or item.get("target_page") or ""),
            "source_fingerprint": str(item["source_fingerprint"]),
            "root_fingerprint": str(item["root_fingerprint"]),
            "proposition_fingerprint": str(item["proposition_fingerprint"]),
            "observed_at": str(item.get("observed_at") or ""),
            "risk": str(item.get("risk") or "normal"),
            "legacy_state": str(item.get("state") or "pending"),
            "lifecycle": "archived" if disposition == "archived_duplicate" else "open",
            "resolution_reason": "duplicate" if disposition == "archived_duplicate" else None,
            "migration_disposition": disposition,
            "evidence_watermark": str(item["evidence_watermark"]),
            "record_version": int(item.get("record_version") or 1),
            "manual_reason": manual_reason(item),
            "duplicate_of": router.opaque_claim_handle(duplicate_of) if duplicate_of else None,
        }
        dispositions.append(row)
        if disposition != "archived_duplicate":
            grouped.setdefault(cid, []).append(row)

    cases: list[dict[str, Any]] = []
    stamp = now_iso()
    for cid, rows in sorted(grouped.items()):
        primary = rows[0]
        reasons = [str(row["manual_reason"]) for row in rows if row.get("manual_reason")]
        state = "manual_required" if reasons else "open"
        titles = list(dict.fromkeys(str(row["title"]) for row in rows if row.get("title")))
        evidence = f"已聚合 {len(rows)} 条证据，来自 {len(set(row['source_fingerprint'] for row in rows))} 个已登记来源。"
        boundaries = list(dict.fromkeys(str(row["evidence_boundary"]) for row in rows if row.get("evidence_boundary")))
        watermark = sha256_text(canonical_json(sorted(row["evidence_watermark"] for row in rows)))[:32]
        context = {
            "case_id": cid,
            "state": state,
            "policy_version": POLICY_VERSION,
            "watermark": watermark,
            "claims": sorted(row["claim_handle"] for row in rows),
        }
        kind = case_kind({**primary, "claim_kind": primary["claim_kind"]})
        guidance = decision_guidance(kind=kind, dimension_key=str(primary["dimension_key"]), titles=titles)
        cases.append(
            {
                "case_id": cid,
                "subject_key": primary["subject_key"],
                "subject_label": primary["subject_label"],
                "dimension_key": primary["dimension_key"],
                "dimension_label": DIMENSION_LABELS.get(primary["dimension_key"], "事实核验"),
                "case_kind": kind,
                "state": state,
                "processing_state": "idle",
                "title": f"{primary['subject_label']} · {DIMENSION_LABELS.get(primary['dimension_key'], '事实核验')}",
                "assessment": (
                    guidance["system_finding"]
                    if state == "manual_required"
                    else "系统会继续吸收后续证据；当前不需要逐条人工查看。"
                ),
                "evidence_summary": evidence + (f" 代表性事项：{'；'.join(titles[:3])}" if titles else ""),
                "evidence_boundary": "；".join(boundaries[:2]) or "现有来源不足以升级为已确认事实。",
                "manual_reason": guidance["why_not_automatic"] if reasons else None,
                "decision_question": guidance["question"] if state == "manual_required" else None,
                "allowed_choices": (
                    [{"choice_id": "continue_automatic", "label": "继续自动观察", "effect": "不确认任何业务事实，只从“需要我”移回自动观察。"}]
                    if state == "manual_required" else []
                ),
                "required_user_authority": "local_owner" if state == "manual_required" else None,
                "policy_version": POLICY_VERSION,
                "evidence_watermark": watermark,
                "context_hash": sha256_text(canonical_json(context)),
                "case_version": 1,
                "claim_ids": [row["claim_id"] for row in rows],
                "claim_handles": [row["claim_handle"] for row in rows],
                "created_at": min((str(row["observed_at"]) for row in rows if row["observed_at"]), default=stamp),
                "updated_at": max((str(row["observed_at"]) for row in rows if row["observed_at"]), default=stamp),
            }
        )

    counts = {
        "total_claims": len(scanned),
        "active_claims": len(active),
        "automatic_observation": sum(1 for row in dispositions if row["migration_disposition"] == "automatic_observation"),
        "manual_required_claims": sum(1 for row in dispositions if row["migration_disposition"] == "manual_required"),
        "archived_duplicate": sum(1 for row in dispositions if row["migration_disposition"] == "archived_duplicate"),
        "needs_user_cases": sum(1 for row in cases if row["state"] == "manual_required"),
        "automatic_cases": sum(1 for row in cases if row["state"] == "open"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "generated_at": stamp,
        "source_watermark": source_watermark(scanned),
        "counts": counts,
        "claims": dispositions,
        "cases": cases,
    }


def _insert_transition(
    connection: sqlite3.Connection,
    *,
    object_type: str,
    object_id: str,
    previous_state: str,
    next_state: str,
    reason: str,
    actor: str,
    evidence_snapshot_hash: str,
    context_hash: Optional[str],
    transaction_id: str,
) -> None:
    connection.execute(
        """
        INSERT INTO transition_events(
          event_id,object_type,object_id,previous_state,next_state,reason,actor,
          policy_version,evidence_snapshot_hash,context_hash,transaction_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid.uuid4()), object_type, object_id, previous_state, next_state, reason,
            actor, POLICY_VERSION, evidence_snapshot_hash, context_hash, transaction_id, now_iso(),
        ),
    )


def apply_plan(
    config: dict[str, Any],
    plan: dict[str, Any],
    *,
    run_id: Optional[str] = None,
    require_maintenance: bool = True,
) -> dict[str, Any]:
    validate_write_scope(config)
    if require_maintenance and not maintenance_active():
        raise ReconciliationError("maintenance_required", "迁移必须在维护模式下执行。", 409)
    current = plan_reconciliation(config)
    if current["source_watermark"] != plan["source_watermark"]:
        raise ReconciliationError("source_changed", "业务页面在 dry-run 后发生变化，本次没有写入。", 409)
    initialize()
    migration_id = run_id or f"reconcile-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    stamp = now_iso()
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior_cases = {
            str(row["case_id"]): dict(row)
            for row in connection.execute("SELECT * FROM cases")
        }
        prior_claim_states = {
            str(row["claim_id"]): (str(row["lifecycle"]), str(row["resolution_reason"] or ""))
            for row in connection.execute("SELECT claim_id,lifecycle,resolution_reason FROM claims")
        }
        connection.execute("DELETE FROM case_claims")
        for row in plan["claims"]:
            connection.execute(
                """
                INSERT INTO claims(
                  claim_id,claim_handle,case_id,subject_key,subject_label,dimension_key,claim_kind,
                  governance_class,title,proposition_summary,evidence_boundary,source_label,
                  source_fingerprint,root_fingerprint,proposition_fingerprint,observed_at,risk,
                  legacy_state,lifecycle,resolution_reason,migration_disposition,evidence_watermark,
                  record_version,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(claim_id) DO UPDATE SET
                  claim_handle=excluded.claim_handle,case_id=excluded.case_id,subject_key=excluded.subject_key,
                  subject_label=excluded.subject_label,dimension_key=excluded.dimension_key,
                  claim_kind=excluded.claim_kind,governance_class=excluded.governance_class,title=excluded.title,
                  proposition_summary=excluded.proposition_summary,evidence_boundary=excluded.evidence_boundary,
                  source_label=excluded.source_label,source_fingerprint=excluded.source_fingerprint,
                  root_fingerprint=excluded.root_fingerprint,proposition_fingerprint=excluded.proposition_fingerprint,
                  observed_at=excluded.observed_at,risk=excluded.risk,legacy_state=excluded.legacy_state,
                  lifecycle=excluded.lifecycle,resolution_reason=excluded.resolution_reason,
                  migration_disposition=excluded.migration_disposition,evidence_watermark=excluded.evidence_watermark,
                  record_version=excluded.record_version,updated_at=excluded.updated_at
                """,
                (
                    row["claim_id"], row["claim_handle"], row["case_id"], row["subject_key"],
                    row["subject_label"], row["dimension_key"], row["claim_kind"], row["governance_class"],
                    row["title"], row["proposition_summary"], row["evidence_boundary"], row["source_label"],
                    row["source_fingerprint"], row["root_fingerprint"], row["proposition_fingerprint"],
                    row["observed_at"], row["risk"], row["legacy_state"], row["lifecycle"],
                    row["resolution_reason"], row["migration_disposition"], row["evidence_watermark"],
                    row["record_version"], stamp,
                ),
            )
        active_case_ids = {str(row["case_id"]) for row in plan["cases"]}
        for old_id in set(prior_cases) - active_case_ids:
            connection.execute(
                "UPDATE cases SET state=CASE WHEN state IN ('plan_confirmed','resolved') THEN state ELSE 'archived' END,processing_state='idle',updated_at=?,resolved_at=COALESCE(resolved_at,?) WHERE case_id=?",
                (stamp, stamp, old_id),
            )
        for row in plan["cases"]:
            prior = prior_cases.get(str(row["case_id"]))
            desired_state = str(row["state"])
            manual_suppressed = int(prior.get("manual_suppressed") or 0) if prior else 0
            suppressed_hash = str(prior.get("suppressed_context_hash") or "") if prior else ""
            disposition = str(prior.get("disposition") or "unhandled") if prior else "unhandled"
            disposition_payload = str(prior.get("disposition_payload_json") or "{}") if prior else "{}"
            defer_until = str(prior.get("defer_until") or "") if prior else ""
            disposition_context = str(prior.get("disposition_context_hash") or "") if prior else ""
            disposition_conversation = str(prior.get("disposition_conversation_hash") or "") if prior else ""
            disposition_at = str(prior.get("disposition_at") or "") if prior else ""
            if manual_suppressed and suppressed_hash == str(row["context_hash"]):
                desired_state = "open"
            elif manual_suppressed:
                manual_suppressed = 0
                suppressed_hash = ""
            if prior and disposition_context == str(row["context_hash"]):
                if disposition == "defer" and defer_until:
                    try:
                        still_deferred = datetime.fromisoformat(defer_until) > datetime.now().astimezone()
                    except ValueError:
                        still_deferred = False
                    if still_deferred:
                        desired_state = "deferred"
                    else:
                        disposition = "unhandled"
                        disposition_payload = "{}"
                        defer_until = ""
                        disposition_context = ""
                        disposition_conversation = ""
                        disposition_at = ""
                elif disposition == "resolve_plan":
                    desired_state = "plan_confirmed"
                elif disposition == "continue_automatic":
                    desired_state = "open"
                    manual_suppressed = 1
                    suppressed_hash = str(row["context_hash"])
                elif disposition == "archive":
                    desired_state = "archived"
            elif prior and disposition not in {"unhandled", "write_memory"}:
                disposition = "unhandled"
                disposition_payload = "{}"
                defer_until = ""
                disposition_context = ""
                disposition_conversation = ""
                disposition_at = ""
            version = int(prior.get("case_version") or 0) if prior else 0
            changed = not prior or any(
                str(prior.get(key) or "") != str(value or "")
                for key, value in (
                    ("state", desired_state),
                    ("context_hash", row["context_hash"]),
                    ("evidence_watermark", row["evidence_watermark"]),
                )
            )
            version = max(1, version + (1 if changed and prior else 0))
            connection.execute(
                """
                INSERT INTO cases(
                  case_id,subject_key,subject_label,dimension_key,dimension_label,case_kind,state,
                  processing_state,title,assessment,evidence_summary,evidence_boundary,manual_reason,
                  decision_question,allowed_choices_json,required_user_authority,policy_version,
                  evidence_watermark,context_hash,case_version,manual_suppressed,suppressed_context_hash,
                  disposition,disposition_payload_json,defer_until,disposition_context_hash,
                  disposition_conversation_hash,disposition_at,created_at,updated_at,resolved_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(case_id) DO UPDATE SET
                  subject_label=excluded.subject_label,dimension_label=excluded.dimension_label,
                  case_kind=excluded.case_kind,state=excluded.state,title=excluded.title,
                  assessment=excluded.assessment,evidence_summary=excluded.evidence_summary,
                  evidence_boundary=excluded.evidence_boundary,manual_reason=excluded.manual_reason,
                  decision_question=excluded.decision_question,allowed_choices_json=excluded.allowed_choices_json,
                  required_user_authority=excluded.required_user_authority,policy_version=excluded.policy_version,
                  evidence_watermark=excluded.evidence_watermark,context_hash=excluded.context_hash,
                  case_version=excluded.case_version,manual_suppressed=excluded.manual_suppressed,
                  suppressed_context_hash=excluded.suppressed_context_hash,
                  disposition=excluded.disposition,disposition_payload_json=excluded.disposition_payload_json,
                  defer_until=excluded.defer_until,disposition_context_hash=excluded.disposition_context_hash,
                  disposition_conversation_hash=excluded.disposition_conversation_hash,
                  disposition_at=excluded.disposition_at,updated_at=excluded.updated_at,
                  resolved_at=excluded.resolved_at
                """,
                (
                    row["case_id"], row["subject_key"], row["subject_label"], row["dimension_key"],
                    row["dimension_label"], row["case_kind"], desired_state, "idle", row["title"],
                    row["assessment"], row["evidence_summary"], row["evidence_boundary"], row["manual_reason"],
                    row["decision_question"], canonical_json(row["allowed_choices"]), row["required_user_authority"],
                    row["policy_version"], row["evidence_watermark"], row["context_hash"], version,
                    manual_suppressed, suppressed_hash or None, disposition, disposition_payload,
                    defer_until or None, disposition_context or None, disposition_conversation or None,
                    disposition_at or None, row["created_at"], stamp,
                    (
                        prior.get("resolved_at")
                        if prior and desired_state in {"plan_confirmed", "resolved", "archived"}
                        else None
                    ),
                ),
            )
            for claim_id in row["claim_ids"]:
                connection.execute(
                    "INSERT INTO case_claims(case_id,claim_id,relation) VALUES(?,?,'member')",
                    (row["case_id"], claim_id),
                )
            if prior and str(prior.get("state")) != desired_state:
                _insert_transition(
                    connection,
                    object_type="case",
                    object_id=row["case_id"],
                    previous_state=str(prior.get("state") or "unknown"),
                    next_state=desired_state,
                    reason="evidence_reconciled",
                    actor="policy",
                    evidence_snapshot_hash=row["evidence_watermark"],
                    context_hash=row["context_hash"],
                    transaction_id=migration_id,
                )
        for row in plan["claims"]:
            if (
                row["migration_disposition"] == "archived_duplicate"
                and prior_claim_states.get(str(row["claim_id"])) != ("archived", "duplicate")
            ):
                _insert_transition(
                    connection,
                    object_type="claim",
                    object_id=row["claim_handle"],
                    previous_state="open",
                    next_state="archived",
                    reason="duplicate",
                    actor="migration",
                    evidence_snapshot_hash=row["evidence_watermark"],
                    context_hash=None,
                    transaction_id=migration_id,
                )
        result = {"counts": plan["counts"], "source_watermark": plan["source_watermark"]}
        connection.execute(
            "INSERT INTO migration_runs(run_id,source_watermark,result_json,status,created_at,completed_at) VALUES(?,?,?,?,?,?)",
            (migration_id, plan["source_watermark"], canonical_json(result), "completed", stamp, now_iso()),
        )
        connection.execute(
            "INSERT INTO metadata(key,value,updated_at) VALUES('migration_epoch',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (migration_id, stamp),
        )
        connection.commit()
        _checkpoint(connection)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"status": "applied", "run_id": migration_id, **result}


def _defer_is_due(value: object) -> bool:
    if not value:
        return True
    try:
        return datetime.fromisoformat(str(value)) <= datetime.now().astimezone()
    except ValueError:
        return True


def public_case(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    state = str(value["state"])
    if state == "deferred" and _defer_is_due(value.get("defer_until")):
        state = "manual_required"
    try:
        disposition_detail = json.loads(str(value.get("disposition_payload_json") or "{}"))
    except json.JSONDecodeError:
        disposition_detail = {}
    return {
        "case_id": str(value["case_id"]),
        "subject_key": str(value["subject_key"]),
        "title": str(value["title"]),
        "subject": str(value["subject_label"]),
        "dimension": str(value["dimension_label"]),
        "dimension_key": str(value["dimension_key"]),
        "case_kind": str(value["case_kind"]),
        "state": state,
        "processing_state": "deferred" if str(value["state"]) == "deferred" and state == "deferred" else str(value["processing_state"]),
        "assessment": str(value["assessment"]),
        "evidence_summary": str(value["evidence_summary"]),
        "evidence_boundary": str(value["evidence_boundary"]),
        "manual_reason": value.get("manual_reason"),
        "decision_question": value.get("decision_question"),
        "allowed_choices": json.loads(str(value.get("allowed_choices_json") or "[]")),
        "policy_version": int(value["policy_version"]),
        "context_hash": str(value["context_hash"]),
        "case_version": int(value["case_version"]),
        "evidence_watermark": str(value["evidence_watermark"]),
        "disposition": str(value.get("disposition") or "unhandled"),
        "disposition_detail": disposition_detail,
        "defer_until": value.get("defer_until"),
        "disposition_conversation_hash": value.get("disposition_conversation_hash"),
        "disposition_at": value.get("disposition_at"),
        "archive_trigger": (
            str(disposition_detail.get("trigger") or "")
            if str(value.get("disposition") or "") == "archive"
            else ""
        ),
        "updated_at": str(value["updated_at"]),
        "resolved_at": value.get("resolved_at"),
    }


def list_cases(scope: str) -> dict[str, Any]:
    import workbench_attention as attention
    if scope not in {"needs_user", "automatic", "archived_questions", "history"}:
        raise ReconciliationError("scope_invalid", "证据收敛范围无效。")
    connection = connect(readonly=True)
    try:
        if scope == "needs_user":
            rows = connection.execute(
                "SELECT * FROM cases WHERE state IN ('manual_required','deferred') ORDER BY updated_at DESC,title"
            ).fetchall()
            items = [item for item in attention.decorate_many([public_case(row) for row in rows]) if item["state"] == "manual_required"]
        elif scope == "automatic":
            rows = connection.execute(
                "SELECT * FROM cases WHERE state IN ('open','deferred') ORDER BY processing_state!='running',updated_at DESC,title"
            ).fetchall()
            items = [item for item in attention.decorate_many([public_case(row) for row in rows]) if item["state"] in {"open", "deferred"}]
        elif scope == "archived_questions":
            rows = connection.execute(
                """
                SELECT * FROM cases
                WHERE 1=1
                ORDER BY COALESCE(disposition_at,resolved_at,updated_at) DESC,title
                """
            ).fetchall()
            items = [value for value in attention.decorate_many([public_case(row) for row in rows])
                     if value["state"] == "archived" and value["disposition"] == "archive"]
        else:
            rows = connection.execute(
                """
                SELECT * FROM cases
                WHERE state IN ('plan_confirmed','resolved','archived')
                  AND NOT (state='archived' AND disposition='archive')
                ORDER BY COALESCE(resolved_at,updated_at) DESC
                """
            ).fetchall()
            items = [item for item in attention.decorate_many([public_case(row) for row in rows])
                     if not (item["state"] == "archived" and item["disposition"] == "archive")]
            archived = connection.execute(
                "SELECT claim_handle,title,resolution_reason,observed_at,source_label FROM claims WHERE lifecycle='archived' ORDER BY observed_at DESC"
            ).fetchall()
            items.extend(
                {
                    "case_id": str(row["claim_handle"]),
                    "title": str(row["title"]),
                    "subject": "历史证据",
                    "dimension": "审计记录",
                    "case_kind": "evidence_history",
                    "state": "archived",
                    "processing_state": "idle",
                    "assessment": "该证据已退出当前工作量，但仍保留审计记录。",
                    "evidence_summary": f"来源：{row['source_label']}",
                    "evidence_boundary": "归档不等于删除，也不升级为已确认事实。",
                    "manual_reason": None,
                    "decision_question": None,
                    "allowed_choices": [],
                    "policy_version": POLICY_VERSION,
                    "context_hash": "",
                    "case_version": 1,
                    "updated_at": str(row["observed_at"]),
                    "resolved_at": str(row["observed_at"]),
                    "resolution_reason": str(row["resolution_reason"] or "archived"),
                }
                for row in archived
            )
        return {"scope": scope, "generated_at": now_iso(), "items": items, "count": len(items)}
    finally:
        connection.close()


def case_detail(case_id: str) -> dict[str, Any]:
    connection = connect(readonly=True)
    try:
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise ReconciliationError("case_missing", "这个事项已经不存在。", 404)
        claims = connection.execute(
            """
            SELECT claim_handle,claim_kind,title,proposition_summary,evidence_boundary,source_label,observed_at,
                   risk,legacy_state,lifecycle,resolution_reason
            FROM claims WHERE case_id=? ORDER BY observed_at,claim_handle
            """,
            (case_id,),
        ).fetchall()
        relations = connection.execute(
            """
            SELECT relation,relation_status,source_label,directness,reason,observed_at
            FROM evidence_relations WHERE case_id=? ORDER BY created_at DESC LIMIT 40
            """,
            (case_id,),
        ).fetchall()
        case = public_case(row)
        guidance = decision_guidance(
            kind=str(case["case_kind"]),
            dimension_key=str(case["dimension_key"]),
            titles=[str(item["title"]) for item in claims],
        )
        return {
            **case,
            "decision_guidance": guidance,
            "evidence": [
                {
                    "claim_handle": str(item["claim_handle"]),
                    "claim_kind": str(item["claim_kind"]),
                    "title": str(item["title"]),
                    "summary": str(item["proposition_summary"]),
                    "boundary": str(item["evidence_boundary"]),
                    "source_label": str(item["source_label"]),
                    "observed_at": str(item["observed_at"]),
                    "risk": str(item["risk"]),
                    "evidence_state": str(item["legacy_state"]),
                    "lifecycle": str(item["lifecycle"]),
                    "resolution_reason": item["resolution_reason"],
                }
                for item in claims
            ],
            "relations": [dict(item) for item in relations],
        }
    finally:
        connection.close()


def work_counts(*, history_extra: int = 0) -> dict[str, int]:
    import workbench_attention as attention
    connection = connect(readonly=True)
    try:
        cases = attention.decorate_many([public_case(row) for row in connection.execute("SELECT * FROM cases")])
        case_states = [case["state"] for case in cases]
        states = {state: case_states.count(state) for state in CASE_STATES}
        seven_days = (datetime.now().astimezone() - timedelta(days=7)).isoformat(timespec="seconds")
        resolved_7d = int(
            connection.execute(
                "SELECT COUNT(*) FROM transition_events WHERE next_state IN ('resolved','archived') AND created_at>=?",
                (seven_days,),
            ).fetchone()[0]
        )
        archived_claims = int(connection.execute("SELECT COUNT(*) FROM claims WHERE lifecycle='archived'").fetchone()[0])
        archived_questions = sum(1 for case in cases if case["state"] == "archived" and case["disposition"] == "archive")
        return {
            "needs_user": states.get("manual_required", 0),
            "automatic": states.get("open", 0) + states.get("deferred", 0),
            "resolved_7d": resolved_7d,
            "archived_questions": archived_questions,
            "history": states.get("plan_confirmed", 0) + states.get("resolved", 0) + max(0, states.get("archived", 0) - archived_questions) + archived_claims + int(history_extra),
        }
    finally:
        connection.close()


CASE_ACTIONS = {"resolve_plan", "defer", "continue_automatic", "write_memory"}


def _future_datetime(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ReconciliationError("defer_until_invalid", "请选择有效的延期日期。") from exc
    if parsed.tzinfo is None:
        raise ReconciliationError("defer_until_invalid", "延期日期必须带时区。")
    now = datetime.now().astimezone()
    parsed = parsed.astimezone()
    if parsed <= now or parsed > now + timedelta(days=366):
        raise ReconciliationError("defer_until_invalid", "延期日期必须在未来一年内。")
    return parsed.isoformat(timespec="seconds")


def _preview_effect(action: str, params: dict[str, Any]) -> dict[str, Any]:
    if action == "resolve_plan":
        return {
            "state_change": {"from": "manual_required", "to": "plan_confirmed"},
            "effect": "保存明确方案和本轮对话校验值并移入历史；这不代表公司批准、技术签署或业务事实成立。",
            "audit_event": "user_confirmed_resolution_plan",
            "governed_memory_write": False,
        }
    if action == "defer":
        return {
            "state_change": {"from": "manual_required", "to": "deferred"},
            "effect": f"延期至 {params['defer_until']}；到期会重新进入“需要我”，证据提前变化也会立即重新出现。",
            "audit_event": "user_deferred_case",
            "governed_memory_write": False,
        }
    if action == "continue_automatic":
        return {
            "state_change": {"from": "manual_required", "to": "open"},
            "effect": "不确认、推翻或覆盖任何业务事实；该事项回到自动观察，证据发生实质变化时重新进入“需要我”。",
            "audit_event": "user_requested_automatic_observation",
            "governed_memory_write": False,
        }
    return {
        "state_change": {"from": "manual_required", "to": "manual_required"},
        "effect": "按预览内容写入受治理业务记忆；AI 回复不是事实来源，写入状态仍受来源和风险规则约束。",
        "audit_event": "user_confirmed_governed_memory_proposal",
        "governed_memory_write": True,
    }


def preview_case(
    case_id: str,
    payload: dict[str, Any],
    *,
    current_conversation_hash: str,
    completed_exchanges: int,
    current_proposed_plan: Optional[str] = None,
    current_plan_proposal: Optional[dict[str, Any]] = None,
    eligible_memory_proposal: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    action = str(payload.get("action") or "")
    expected_hash = str(payload.get("context_hash") or "")
    expected_conversation_hash = str(payload.get("conversation_hash") or "")
    try:
        expected_version = int(payload.get("case_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError("preview_contract", "事项版本无效。") from exc
    if action not in CASE_ACTIONS:
        raise ReconciliationError("action_invalid", "这个事项不支持该动作。")
    if completed_exchanges < 1:
        raise ReconciliationError("conversation_required", "请先与 AI 完成至少一轮对话，再应用处理动作。", 409)
    if not current_conversation_hash or expected_conversation_hash != current_conversation_hash:
        raise ReconciliationError("preview_stale", "对话已经变化，请刷新后再预览。", 409)
    raw_params = payload.get("params") or {}
    if not isinstance(raw_params, dict):
        raise ReconciliationError("action_params_invalid", "动作参数格式无效。")
    if action == "resolve_plan":
        plan_proposal = current_plan_proposal or {}
        if plan_proposal and (
            str(plan_proposal.get("status") or "") != "active"
            or str(plan_proposal.get("conversation_hash") or "") != current_conversation_hash
        ):
            raise ReconciliationError("plan_stale", "AI 方案已经过期，请基于当前对话重新形成方案。", 409)
        plan_text = str(plan_proposal.get("text") or current_proposed_plan or "").strip()
        if not plan_text:
            raise ReconciliationError("plan_missing", "AI 尚未形成可确认方案。", 409)
        params = {"plan": plan_text[:8000]}
        if plan_proposal:
            params.update({
                "proposal_id": str(plan_proposal.get("proposal_id") or ""),
                "proposal_revision": int(plan_proposal.get("revision") or 0),
                "proposal_hash": str(plan_proposal.get("proposal_hash") or ""),
            })
            if not params["proposal_id"] or not params["proposal_hash"] or params["proposal_revision"] < 1:
                raise ReconciliationError("plan_invalid", "AI 方案缺少受控版本信息。", 409)
        proposal_id = None
        target_before_hash = ""
    elif action == "defer":
        params = {
            "defer_until": _future_datetime(raw_params.get("defer_until")),
            "reason": str(raw_params.get("reason") or "").strip()[:1000],
        }
        proposal_id = None
        target_before_hash = ""
    elif action == "continue_automatic":
        if raw_params:
            raise ReconciliationError("action_params_invalid", "继续自动观察不接受额外参数。")
        params = {}
        proposal_id = None
        target_before_hash = ""
    else:
        requested_id = str(raw_params.get("proposal_id") or "")
        proposal = eligible_memory_proposal or {}
        if not requested_id or requested_id != str(proposal.get("proposal_id") or ""):
            raise ReconciliationError("memory_proposal_invalid", "记忆候选不存在、已变化或不可写入。", 409)
        params = {
            "proposal_id": requested_id,
            "proposal_hash": str(proposal.get("proposal_hash") or ""),
        }
        if not params["proposal_hash"]:
            raise ReconciliationError("memory_proposal_invalid", "记忆候选缺少受控校验值。", 409)
        proposal_id = requested_id
        target_before_hash = str(proposal.get("target_before_hash") or "")
        if not target_before_hash:
            raise ReconciliationError("memory_proposal_invalid", "记忆目标缺少写入前校验值。", 409)
    params_json = canonical_json(params)
    params_hash = sha256_text(params_json)
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise ReconciliationError("case_missing", "这个事项已经不存在。", 404)
        stored_state = str(row["state"])
        due_deferred = stored_state == "deferred" and _defer_is_due(row["defer_until"])
        if stored_state != "manual_required" and not due_deferred:
            raise ReconciliationError("case_not_actionable", "这个事项已不再需要人工处理。", 409)
        if expected_hash != str(row["context_hash"]) or expected_version != int(row["case_version"]):
            raise ReconciliationError("preview_stale", "证据或事项版本已经变化，请刷新后再预览。", 409)
        if due_deferred:
            connection.execute(
                """
                UPDATE cases SET state='manual_required',disposition='unhandled',
                  disposition_payload_json='{}',defer_until=NULL,disposition_context_hash=NULL,
                  disposition_conversation_hash=NULL,disposition_at=NULL,updated_at=? WHERE case_id=?
                """,
                (now_iso(), case_id),
            )
        token = secrets.token_urlsafe(32)
        token_hash = sha256_text(token)
        created = datetime.now().astimezone()
        expires = created + timedelta(minutes=PREVIEW_TTL_MINUTES)
        created_iso = created.isoformat(timespec="seconds")
        connection.execute(
            "DELETE FROM case_previews WHERE case_id=? OR expires_at<? OR used_at IS NOT NULL",
            (case_id, created_iso),
        )
        connection.execute(
            """
            INSERT INTO case_previews(
              token_hash,case_id,case_version,choice_id,action,action_params_json,
              action_params_hash,context_hash,evidence_watermark,conversation_hash,
              memory_proposal_id,policy_version,maintenance_epoch,target_before_hash,
              created_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                token_hash, case_id, int(row["case_version"]), action, action, params_json,
                params_hash, str(row["context_hash"]), str(row["evidence_watermark"]),
                current_conversation_hash, proposal_id, int(row["policy_version"]),
                maintenance_epoch(), target_before_hash,
                created_iso, expires.isoformat(timespec="seconds"),
            ),
        )
        connection.commit()
        return {
            "case_id": case_id,
            "action": action,
            "action_params": params,
            "action_params_hash": params_hash,
            "conversation_hash": current_conversation_hash,
            "preview_token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            **_preview_effect(action, params),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _validated_preview(
    connection: sqlite3.Connection,
    *,
    case_id: str,
    token_hash: str,
    current_conversation_hash: str,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    preview = connection.execute("SELECT * FROM case_previews WHERE token_hash=?", (token_hash,)).fetchone()
    row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if not preview or not row or preview["used_at"]:
        raise ReconciliationError("preview_invalid", "预览不存在或已经使用，请重新预览。", 409)
    try:
        expired = datetime.fromisoformat(str(preview["expires_at"])) <= datetime.now().astimezone()
    except ValueError:
        expired = True
    actual = (
        str(preview["case_id"]), int(preview["case_version"]), str(preview["context_hash"]),
        str(preview["evidence_watermark"]), str(preview["conversation_hash"]),
        int(preview["policy_version"]), str(preview["maintenance_epoch"]),
    )
    expected = (
        case_id, int(row["case_version"]), str(row["context_hash"]),
        str(row["evidence_watermark"]), current_conversation_hash,
        int(row["policy_version"]), maintenance_epoch(),
    )
    state = str(row["state"])
    if expired or actual != expected or state != "manual_required":
        raise ReconciliationError("preview_stale", "证据、对话或事项状态已经变化，请重新预览。", 409)
    if str(preview["action"]) not in CASE_ACTIONS:
        raise ReconciliationError("action_invalid", "预览动作无效。", 409)
    return preview, row


def apply_case(
    case_id: str,
    payload: dict[str, Any],
    *,
    current_conversation_hash: str,
    memory_applier: Optional[Any] = None,
) -> dict[str, Any]:
    if set(payload) != {"preview_token", "idempotency_key"}:
        raise ReconciliationError("apply_contract", "应用请求只能包含预览凭证和幂等键。", 409)
    token = str(payload.get("preview_token") or "")
    idempotency_key = str(payload.get("idempotency_key") or "")
    if len(token) < 32 or not re.fullmatch(r"[0-9a-fA-F-]{36}", idempotency_key):
        raise ReconciliationError("apply_contract", "应用请求缺少有效预览或幂等键。", 409)
    token_hash = sha256_text(token)
    connection = connect()
    external_result: Optional[dict[str, Any]] = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        receipt = connection.execute(
            "SELECT * FROM idempotency_receipts WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if receipt:
            if str(receipt["case_id"]) != case_id or str(receipt["preview_token_hash"]) != token_hash:
                raise ReconciliationError("idempotency_conflict", "幂等键已绑定其他应用请求。", 409)
            stored_result = json.loads(str(receipt["result_json"]))
            if str(receipt["receipt_state"]) == "committed":
                connection.commit()
                return stored_result
            raise ReconciliationError(
                str(receipt["error_code"] or "apply_incomplete"),
                "上一次应用未完成，请重新生成预览后重试。",
                409,
            )
        preview, row = _validated_preview(
            connection,
            case_id=case_id,
            token_hash=token_hash,
            current_conversation_hash=current_conversation_hash,
        )
        action = str(preview["action"])
        prepared = {"status": "prepared", "case_id": case_id, "action": action}
        stamp = now_iso()
        connection.execute(
            """
            INSERT INTO idempotency_receipts(
              idempotency_key,case_id,choice_id,action_params_hash,conversation_hash,
              preview_token_hash,receipt_state,error_code,result_json,created_at
            ) VALUES(?,?,?,?,?,?,'prepared',NULL,?,?)
            """,
            (
                idempotency_key, case_id, action, str(preview["action_params_hash"]),
                str(preview["conversation_hash"]), token_hash, canonical_json(prepared), stamp,
            ),
        )
        if action == "write_memory":
            if memory_applier is None:
                raise ReconciliationError("memory_applier_missing", "受治理记忆写入器不可用。", 503)
            connection.commit()
            try:
                external_result = memory_applier(dict(preview))
            except Exception as exc:
                error_code = str(getattr(exc, "code", type(exc).__name__))[:120]
                failure = {"status": "failed", "case_id": case_id, "action": action, "error_code": error_code}
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE idempotency_receipts SET receipt_state='failed',error_code=?,result_json=? WHERE idempotency_key=?",
                    (error_code, canonical_json(failure), idempotency_key),
                )
                connection.commit()
                raise
            connection.execute("BEGIN IMMEDIATE")
            preview, row = _validated_preview(
                connection,
                case_id=case_id,
                token_hash=token_hash,
                current_conversation_hash=current_conversation_hash,
            )
        params = json.loads(str(preview["action_params_json"] or "{}"))
        action = str(preview["action"])
        stamp = now_iso()
        transaction_id = str(uuid.uuid4())
        next_state = {
            "resolve_plan": "plan_confirmed",
            "defer": "deferred",
            "continue_automatic": "open",
            "write_memory": "manual_required",
        }[action]
        reason = _preview_effect(action, params)["audit_event"]
        resolved_at = stamp if action == "resolve_plan" else None
        manual_suppressed = 1 if action == "continue_automatic" else 0
        suppressed_hash = str(row["context_hash"]) if manual_suppressed else None
        defer_until = str(params.get("defer_until") or "") or None
        connection.execute(
            """
            UPDATE cases SET state=?,processing_state='idle',case_version=case_version+1,
              manual_suppressed=?,suppressed_context_hash=?,disposition=?,
              disposition_payload_json=?,defer_until=?,disposition_context_hash=context_hash,
              disposition_conversation_hash=?,disposition_at=?,updated_at=?,resolved_at=?
            WHERE case_id=?
            """,
            (
                next_state, manual_suppressed, suppressed_hash, action, canonical_json(params),
                defer_until, current_conversation_hash, stamp, stamp, resolved_at, case_id,
            ),
        )
        _insert_transition(
            connection,
            object_type="case", object_id=case_id, previous_state="manual_required", next_state=next_state,
            reason=reason, actor="user", evidence_snapshot_hash=str(row["evidence_watermark"]),
            context_hash=str(row["context_hash"]), transaction_id=transaction_id,
        )
        connection.execute("UPDATE case_previews SET used_at=? WHERE token_hash=?", (stamp, token_hash))
        result = {
            "status": "applied",
            "case_id": case_id,
            "action": action,
            "state": next_state,
            "business_fact_changed": False,
            "governed_memory_write": action == "write_memory",
            "memory_result": external_result,
            "transaction_id": transaction_id,
        }
        connection.execute(
            """
            UPDATE idempotency_receipts SET receipt_state='committed',error_code=NULL,result_json=?
            WHERE idempotency_key=?
            """,
            (canonical_json(result), idempotency_key),
        )
        connection.commit()
        return result
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def archive_case_direct(
    case_id: str,
    payload: dict[str, Any],
    *,
    current_conversation_hash: str,
    trigger: str,
    source_message_id: Optional[str] = None,
) -> dict[str, Any]:
    """Archive one concrete question without mutating claims or governed memory."""
    required = {"case_version", "context_hash", "conversation_hash", "idempotency_key"}
    if set(payload) != required:
        raise ReconciliationError("archive_contract", "归档请求字段无效。", 409)
    if trigger not in {"manual", "reply_after_answer"}:
        raise ReconciliationError("archive_trigger_invalid", "归档触发方式无效。", 409)
    idempotency_key = str(payload.get("idempotency_key") or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", idempotency_key):
        raise ReconciliationError("archive_contract", "归档请求缺少有效幂等键。", 409)
    requested_conversation_hash = str(payload.get("conversation_hash") or "")
    if not requested_conversation_hash or requested_conversation_hash != current_conversation_hash:
        raise ReconciliationError("archive_stale", "对话已经变化，请刷新后重试。", 409)
    params: dict[str, Any] = {"trigger": trigger}
    if source_message_id:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(source_message_id)):
            raise ReconciliationError("archive_contract", "归档来源消息无效。", 409)
        params["source_message_id"] = str(source_message_id)
    params_json = canonical_json(params)
    params_hash = sha256_text(params_json)
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        receipt = connection.execute(
            "SELECT * FROM idempotency_receipts WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if receipt:
            if (
                str(receipt["case_id"]) != case_id
                or str(receipt["choice_id"]) != "archive"
                or str(receipt["action_params_hash"]) != params_hash
                or str(receipt["conversation_hash"]) != requested_conversation_hash
            ):
                raise ReconciliationError(
                    "idempotency_conflict", "幂等键已绑定其他归档请求。", 409
                )
            result = json.loads(str(receipt["result_json"] or "{}"))
            if str(receipt["receipt_state"]) == "committed":
                connection.commit()
                return result
            raise ReconciliationError(
                str(receipt["error_code"] or "archive_incomplete"),
                "上一次归档未完成，请刷新后重试。",
                409,
            )
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise ReconciliationError("case_missing", "这个事项已经不存在。", 404)
        if (
            int(payload.get("case_version") or 0) != int(row["case_version"])
            or str(payload.get("context_hash") or "") != str(row["context_hash"])
            or str(row["state"]) != "manual_required"
        ):
            raise ReconciliationError(
                "archive_stale", "证据、对话或事项状态已经变化，请刷新后重试。", 409
            )
        stamp = now_iso()
        transaction_id = str(uuid.uuid4())
        prepared = {
            "status": "prepared",
            "case_id": case_id,
            "action": "archive",
            "trigger": trigger,
        }
        connection.execute(
            """
            INSERT INTO idempotency_receipts(
              idempotency_key,case_id,choice_id,action_params_hash,conversation_hash,
              preview_token_hash,receipt_state,error_code,result_json,created_at
            ) VALUES(?,?,?,?,?,'direct-archive','prepared',NULL,?,?)
            """,
            (
                idempotency_key,
                case_id,
                "archive",
                params_hash,
                requested_conversation_hash,
                canonical_json(prepared),
                stamp,
            ),
        )
        connection.execute(
            """
            UPDATE cases SET state='archived',processing_state='idle',case_version=case_version+1,
              manual_suppressed=0,suppressed_context_hash=NULL,disposition='archive',
              disposition_payload_json=?,defer_until=NULL,disposition_context_hash=context_hash,
              disposition_conversation_hash=?,disposition_at=?,updated_at=?,resolved_at=?
            WHERE case_id=?
            """,
            (params_json, requested_conversation_hash, stamp, stamp, stamp, case_id),
        )
        _insert_transition(
            connection,
            object_type="case",
            object_id=case_id,
            previous_state="manual_required",
            next_state="archived",
            reason=(
                "reply_after_answer_archive"
                if trigger == "reply_after_answer"
                else "manual_archive"
            ),
            actor="system" if trigger == "reply_after_answer" else "user",
            evidence_snapshot_hash=str(row["evidence_watermark"]),
            context_hash=str(row["context_hash"]),
            transaction_id=transaction_id,
        )
        result = {
            "status": "applied",
            "case_id": case_id,
            "action": "archive",
            "state": "archived",
            "archive_trigger": trigger,
            "archived_at": stamp,
            "business_fact_changed": False,
            "governed_memory_write": False,
            "transaction_id": transaction_id,
        }
        connection.execute(
            """
            UPDATE idempotency_receipts
            SET receipt_state='committed',error_code=NULL,result_json=?
            WHERE idempotency_key=?
            """,
            (canonical_json(result), idempotency_key),
        )
        connection.commit()
        return result
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def restore_case_disposition(receipt: dict[str, Any]) -> dict[str, Any]:
    """Reapply the latest committed human disposition after a derived case DB rebuild."""
    action = str(receipt.get("action") or "")
    if str(receipt.get("state") or "") != "committed" or action not in {
        "resolve_plan", "defer", "continue_automatic", "archive",
    }:
        return {"status": "ignored", "action": action}
    case_id = str(receipt.get("case_id") or "")
    try:
        params = json.loads(str(receipt.get("action_params_json") or "{}"))
    except json.JSONDecodeError:
        return {"status": "invalid", "case_id": case_id, "action": action}
    if not isinstance(params, dict):
        return {"status": "invalid", "case_id": case_id, "action": action}
    if action == "defer" and _defer_is_due(params.get("defer_until")):
        return {"status": "expired", "case_id": case_id, "action": action}
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            connection.commit()
            return {"status": "missing", "case_id": case_id, "action": action}
        if (
            str(row["context_hash"]) != str(receipt.get("context_hash") or "")
            or str(row["evidence_watermark"]) != str(receipt.get("evidence_watermark") or "")
        ):
            connection.commit()
            return {"status": "stale", "case_id": case_id, "action": action}
        params_json = canonical_json(params)
        if (
            str(row["disposition"]) == action
            and str(row["disposition_payload_json"]) == params_json
            and str(row["disposition_context_hash"] or "") == str(row["context_hash"])
            and str(row["state"])
            == {
                "resolve_plan": "plan_confirmed",
                "defer": "deferred",
                "continue_automatic": "open",
                "archive": "archived",
            }[action]
        ):
            connection.commit()
            return {"status": "already_restored", "case_id": case_id, "action": action}
        next_state = {
            "resolve_plan": "plan_confirmed",
            "defer": "deferred",
            "continue_automatic": "open",
            "archive": "archived",
        }[action]
        stamp = str(receipt.get("committed_at") or receipt.get("updated_at") or now_iso())
        manual_suppressed = 1 if action == "continue_automatic" else 0
        suppressed_hash = str(row["context_hash"]) if manual_suppressed else None
        defer_until = str(params.get("defer_until") or "") or None
        connection.execute(
            """
            UPDATE cases SET state=?,processing_state='idle',case_version=case_version+1,
              manual_suppressed=?,suppressed_context_hash=?,disposition=?,
              disposition_payload_json=?,defer_until=?,disposition_context_hash=context_hash,
              disposition_conversation_hash=?,disposition_at=?,updated_at=?,resolved_at=?
            WHERE case_id=?
            """,
            (
                next_state, manual_suppressed, suppressed_hash, action, params_json, defer_until,
                str(receipt.get("conversation_hash") or ""), stamp, stamp,
                stamp if action in {"resolve_plan", "archive"} else None, case_id,
            ),
        )
        _insert_transition(
            connection,
            object_type="case", object_id=case_id, previous_state=str(row["state"]), next_state=next_state,
            reason="durable_action_restored", actor="system",
            evidence_snapshot_hash=str(row["evidence_watermark"]), context_hash=str(row["context_hash"]),
            transaction_id=str(uuid.uuid4()),
        )
        connection.commit()
        return {"status": "restored", "case_id": case_id, "action": action, "state": next_state}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def open_claim_context(source_text: str = "", *, limit: int = 24) -> list[dict[str, Any]]:
    connection = connect(readonly=True)
    try:
        rows = connection.execute(
            """
            SELECT c.claim_handle,c.title,c.proposition_summary,c.evidence_boundary,c.governance_class,
                   c.risk,c.legacy_state,c.observed_at,k.case_id,k.subject_label,k.dimension_label,k.state,
                   c.record_version,c.evidence_watermark AS claim_watermark,k.context_hash,k.case_version,c.lifecycle
            FROM claims c JOIN cases k ON k.case_id=c.case_id
            WHERE c.lifecycle='open' AND k.state IN ('open','manual_required')
            ORDER BY k.state='manual_required' DESC,c.observed_at DESC
            """
        ).fetchall()
    finally:
        connection.close()
    terms = {term for term in re.findall(r"[A-Za-z0-9]{3,}|[\u4e00-\u9fff]{2,}", source_text.casefold())}

    def score(row: sqlite3.Row) -> tuple[int, str]:
        haystack = " ".join(str(row[key]).casefold() for key in ("title", "proposition_summary", "subject_label", "dimension_label"))
        return (sum(1 for term in terms if term in haystack), str(row["observed_at"]))

    selected = sorted(rows, key=score, reverse=True)
    if terms:
        matched = [row for row in selected if score(row)[0] > 0]
        selected = matched or selected[: min(6, limit)]
    return [
        {
            "claim_handle": str(row["claim_handle"]),
            "case_handle": str(row["case_id"]),
            "subject": str(row["subject_label"]),
            "dimension": str(row["dimension_label"]),
            "title": str(row["title"]),
            "proposition_summary": str(row["proposition_summary"]),
            "evidence_boundary": str(row["evidence_boundary"]),
            "governance_class": str(row["governance_class"]),
            "risk_tag": str(row["risk"]),
            "evidence_state": str(row["legacy_state"]),
            "case_state": str(row["state"]),
            "observed_at": str(row["observed_at"]),
            "context_fingerprint": claim_context_fingerprint(row),
        }
        for row in selected[: max(1, min(limit, 40))]
    ]


def claim_context_fingerprint(row: Any) -> str:
    return sha256_text(canonical_json({key: row[key] for key in (
        "claim_handle", "title", "proposition_summary", "evidence_boundary", "governance_class",
        "risk", "legacy_state", "observed_at", "case_id", "subject_label", "dimension_label", "state",
        "record_version", "claim_watermark", "context_hash", "case_version", "lifecycle",
    )}))


def claim_context_hashes(handles: set[str], connection: Optional[sqlite3.Connection] = None) -> dict[str, str]:
    if not handles:
        return {}
    owned = connection is None
    connection = connection or connect(readonly=True)
    try:
        rows = connection.execute(
            "SELECT c.claim_handle,c.title,c.proposition_summary,c.evidence_boundary,c.governance_class,"
            "c.risk,c.legacy_state,c.observed_at,k.case_id,k.subject_label,k.dimension_label,k.state,"
            "c.record_version,c.evidence_watermark AS claim_watermark,k.context_hash,k.case_version,c.lifecycle "
            "FROM claims c JOIN cases k ON k.case_id=c.case_id WHERE c.claim_handle IN ("
            + ",".join("?" for _ in handles) + ") AND c.lifecycle='open' AND k.state IN ('open','manual_required')",
            tuple(sorted(handles)),
        ).fetchall()
        return {str(row["claim_handle"]): claim_context_fingerprint(row) for row in rows}
    finally:
        if owned:
            connection.close()


def open_claim_handles() -> set[str]:
    """Return every currently valid public handle for fail-closed validation."""
    connection = connect(readonly=True)
    try:
        return {
            str(row["claim_handle"])
            for row in connection.execute(
                "SELECT claim_handle FROM claims WHERE lifecycle='open'"
            )
        }
    finally:
        connection.close()


def validate_relations(raw: Any, *, allowed_handles: set[str], allowed_tokens: Optional[set[str]] = None) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 40:
        raise ReconciliationError("relation_shape", "claim_relations 必须是不超过 40 项的数组。")
    result = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ReconciliationError("relation_shape", "claim relation 必须是对象。")
        handle = str(item.get("claim_handle") or "")
        relation = str(item.get("relation") or "")
        tokens = tuple(str(value) for value in (item.get("source_tokens") or []))
        reason = router.single_line(item.get("reason"), 500)
        if handle not in allowed_handles or relation not in RELATIONS or not reason:
            raise ReconciliationError("relation_invalid", "claim relation 引用了未知事项或无效关系。")
        if allowed_tokens is not None and (not tokens or not set(tokens).issubset(allowed_tokens)):
            raise ReconciliationError("relation_source", "claim relation 引用了未知来源 token。")
        key = (handle, relation, tokens)
        if key in seen:
            raise ReconciliationError("relation_duplicate", "claim relation 重复。")
        seen.add(key)
        result.append({"claim_handle": handle, "relation": relation, "source_tokens": list(tokens), "reason": reason})
    return result


def _source_descriptor(value: Any, source_system: str) -> dict[str, str]:
    if isinstance(value, dict):
        material = router.normalize_text(value.get("material"), 10000)
        root = str(value.get("root_fingerprint") or "")
        provenance = router.single_line(value.get("provenance_family"), 120)
        independence = router.single_line(value.get("independence_group"), 120)
        directness = str(value.get("directness") or "unknown")
        authority = str(value.get("authority_class") or "ordinary")
        verification = str(value.get("verification_method") or "model_proposed")
    else:
        material = router.normalize_text(value, 10000)
        root = ""
        provenance = ""
        independence = ""
        directness = "unknown"
        authority = "ordinary"
        verification = "model_proposed"
    content_root = sha256_text(material.casefold())[:32]
    if not re.fullmatch(r"[0-9a-f]{32}", root):
        root = content_root
    provenance = provenance or f"{source_system}:{root}"
    independence = independence or root
    if directness not in {"direct", "indirect", "unknown"}:
        directness = "unknown"
    if authority not in {"ordinary", "controlled_original", "receipt"}:
        authority = "ordinary"
    if verification not in {
        "model_proposed",
        "deterministic",
        "controlled_original_hash",
        "transport_receipt",
    }:
        verification = "model_proposed"
    return {
        "material": material,
        "root_fingerprint": root,
        "provenance_family": provenance,
        "independence_group": independence,
        "directness": directness,
        "authority_class": authority,
        "verification_method": verification,
    }


def _relation_status(relation: str, descriptor: dict[str, str], claim: sqlite3.Row) -> str:
    if relation == "duplicate" and descriptor["root_fingerprint"] == str(claim["root_fingerprint"]):
        return "policy_accepted"
    if descriptor["verification_method"] in {
        "deterministic",
        "controlled_original_hash",
        "transport_receipt",
    }:
        return "policy_accepted"
    return "proposed"


def _resolution_for_claim(connection: sqlite3.Connection, claim: sqlite3.Row) -> Optional[str]:
    rows = connection.execute(
        """
        SELECT relation,root_fingerprint,independence_group,directness,authority_class
        FROM evidence_relations
        WHERE claim_id=? AND relation_status='policy_accepted'
        """,
        (str(claim["claim_id"]),),
    ).fetchall()
    if any(
        str(row["relation"]) == "duplicate"
        and str(row["root_fingerprint"]) == str(claim["root_fingerprint"])
        for row in rows
    ):
        return "duplicate"
    authoritative = {"controlled_original", "receipt"}
    if any(
        str(row["relation"]) == "supersede"
        and str(row["authority_class"]) in authoritative
        for row in rows
    ):
        return "superseded"
    # Supersession retires an old question; it must never approve its business claim.
    if str(claim["claim_kind"]) in {"human_decision", "human_authorization"}:
        return None
    high_risk = str(claim["governance_class"]) in {
        "certification", "technical_specification", "price", "contract",
        "approval_record", "sent", "received", "completed",
    }

    def enough(relation: str) -> bool:
        selected = [row for row in rows if str(row["relation"]) == relation]
        if any(str(row["authority_class"]) in authoritative for row in selected):
            return True
        if high_risk:
            return False
        independent = {
            str(row["independence_group"])
            for row in selected
            if str(row["directness"]) == "direct"
        }
        return len(independent) >= 2

    if enough("refute"):
        return "refuted"
    if enough("support"):
        return "confirmed"
    return None


def _evaluate_case(connection: sqlite3.Connection, case_id: str, transaction_id: str) -> list[str]:
    resolved: list[str] = []
    claims = connection.execute(
        "SELECT * FROM claims WHERE case_id=? AND lifecycle='open'",
        (case_id,),
    ).fetchall()
    for claim in claims:
        reason = _resolution_for_claim(connection, claim)
        if not reason:
            continue
        lifecycle = "archived" if reason in {"duplicate", "superseded"} else "resolved"
        connection.execute(
            "UPDATE claims SET lifecycle=?,resolution_reason=?,record_version=record_version+1,updated_at=? WHERE claim_id=?",
            (lifecycle, reason, now_iso(), str(claim["claim_id"])),
        )
        _insert_transition(
            connection,
            object_type="claim",
            object_id=str(claim["claim_handle"]),
            previous_state="open",
            next_state=lifecycle,
            reason=reason,
            actor="policy",
            evidence_snapshot_hash=str(claim["evidence_watermark"]),
            context_hash=None,
            transaction_id=transaction_id,
        )
        resolved.append(str(claim["claim_id"]))
    remaining = int(
        connection.execute(
            "SELECT COUNT(*) FROM claims WHERE case_id=? AND lifecycle='open'",
            (case_id,),
        ).fetchone()[0]
    )
    case = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if case:
        next_state = str(case["state"])
        if remaining == 0 and str(case["state"]) != "manual_required":
            next_state = "resolved"
        connection.execute(
            "UPDATE cases SET state=?,processing_state='idle',case_version=case_version+?,updated_at=?,resolved_at=CASE WHEN ?='resolved' THEN COALESCE(resolved_at,?) ELSE resolved_at END WHERE case_id=?",
            (
                next_state,
                int(next_state != str(case["state"])),
                now_iso(),
                next_state,
                now_iso(),
                case_id,
            ),
        )
        if next_state != str(case["state"]):
            _insert_transition(
                connection,
                object_type="case",
                object_id=case_id,
                previous_state=str(case["state"]),
                next_state=next_state,
                reason="evidence_policy_satisfied",
                actor="policy",
                evidence_snapshot_hash=str(case["evidence_watermark"]),
                context_hash=str(case["context_hash"]),
                transaction_id=transaction_id,
            )
    return resolved


def record_relations(
    relations: list[dict[str, Any]],
    *,
    source_system: str,
    source_material: dict[str, Any],
    observed_at: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    expected_context: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    if not relations:
        return {"recorded": 0, "queued": 0, "resolved": 0}
    if config is not None:
        validate_write_scope(config)
    initialize()
    connection = connect()
    recorded = 0
    queued = 0
    resolved_claim_ids: list[str] = []
    stamp = observed_at or now_iso()
    transaction_id = str(uuid.uuid4())
    request_hash = sha256_text(canonical_json({"relations": relations, "source_system": source_system,
                                             "source_material": source_material, "observed_at": stamp,
                                             "expected_context": expected_context}))
    try:
        connection.execute("BEGIN IMMEDIATE")
        if request_id:
            previous = connection.execute("SELECT * FROM relation_commit_receipts WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise ReconciliationError("relation_request_conflict", "同一关系提交不能更换来源或内容。", 409)
                connection.rollback()
                return resume_relation_commit(config, dict(previous))
        if expected_context is not None:
            # Check in the same write transaction. An already committed request
            # above resumes its own receipt, even if its resolution changed the
            # context; an unrelated change must still stop a new transaction.
            actual = claim_context_hashes(set(expected_context), connection)
            if actual != expected_context:
                raise ReconciliationError("proposal_context_drift", "关系登记前旧事项发生变化；没有重复覆盖。", 409)
        for relation in relations:
            claim = connection.execute(
                "SELECT * FROM claims WHERE claim_handle=? AND lifecycle='open'",
                (relation["claim_handle"],),
            ).fetchone()
            if not claim:
                raise ReconciliationError("relation_stale", "关联的旧证据已经变化，本次关系没有写入。", 409)
            tokens = relation.get("source_tokens") or ["source"]
            descriptors = [
                _source_descriptor(source_material.get(str(token), str(token)), source_system)
                for token in tokens
            ]
            root = (
                descriptors[0]["root_fingerprint"]
                if len(descriptors) == 1
                else sha256_text("|".join(sorted(item["root_fingerprint"] for item in descriptors)))[:32]
            )
            descriptor = {
                "root_fingerprint": root,
                "provenance_family": "|".join(sorted(set(item["provenance_family"] for item in descriptors)))[:300],
                "independence_group": "|".join(sorted(set(item["independence_group"] for item in descriptors)))[:300],
                "directness": "direct" if all(item["directness"] == "direct" for item in descriptors) else "unknown",
                "authority_class": (
                    "receipt" if all(item["authority_class"] == "receipt" for item in descriptors)
                    else "controlled_original" if all(item["authority_class"] == "controlled_original" for item in descriptors)
                    else "ordinary"
                ),
                "verification_method": (
                    descriptors[0]["verification_method"]
                    if len({item["verification_method"] for item in descriptors}) == 1
                    else "model_proposed"
                ),
            }
            relation_status = _relation_status(str(relation["relation"]), descriptor, claim)
            rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{relation['claim_handle']}|{relation['relation']}|{root}"))
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO evidence_relations(
                  relation_id,claim_id,case_id,relation,relation_status,source_system,source_label,
                  root_fingerprint,provenance_family,independence_group,directness,authority_class,
                  verification_method,reason,observed_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rid, str(claim["claim_id"]), str(claim["case_id"]), relation["relation"], relation_status,
                    router.single_line(source_system, 40), SOURCE_LABELS.get(source_system, "受治理来源"), root,
                    descriptor["provenance_family"], descriptor["independence_group"],
                    descriptor["directness"], descriptor["authority_class"],
                    descriptor["verification_method"], relation["reason"], stamp, now_iso(),
                ),
            )
            recorded += int(cursor.rowcount or 0)
            case = connection.execute("SELECT * FROM cases WHERE case_id=?", (str(claim["case_id"]),)).fetchone()
            if case:
                job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{case['case_id']}|{POLICY_VERSION}|{case['evidence_watermark']}"))
                cursor = connection.execute(
                    """
                    INSERT INTO reconciliation_jobs(
                      job_id,case_id,subject_key,dimension_key,policy_version,evidence_watermark,state,dirty,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,'queued',0,?,?)
                    ON CONFLICT(case_id,policy_version,evidence_watermark)
                    DO UPDATE SET state='queued',dirty=1,updated_at=excluded.updated_at
                    """,
                    (
                        job_id, str(case["case_id"]), str(case["subject_key"]), str(case["dimension_key"]),
                        POLICY_VERSION, str(case["evidence_watermark"]), now_iso(), now_iso(),
                    ),
                )
                queued += int(cursor.rowcount or 0)
                connection.execute("UPDATE cases SET processing_state='queued',updated_at=? WHERE case_id=?", (now_iso(), str(case["case_id"])))
        for case_id in sorted({
            str(row["case_id"])
            for row in connection.execute("SELECT DISTINCT case_id FROM reconciliation_jobs WHERE state='queued'")
        }):
            resolved_claim_ids.extend(_evaluate_case(connection, case_id, transaction_id))
            connection.execute(
                "UPDATE reconciliation_jobs SET state='completed',dirty=0,updated_at=? WHERE case_id=? AND state='queued'",
                (now_iso(), case_id),
            )
        if request_id:
            connection.execute("INSERT INTO relation_commit_receipts VALUES(?,?,?,'recorded',?,?,?,?)",
                               (request_id, request_hash, transaction_id,
                                canonical_json({"recorded": recorded, "queued": queued, "resolved": len(set(resolved_claim_ids)), "persisted": 0}),
                                canonical_json(sorted(set(resolved_claim_ids))), now_iso(), now_iso()))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    persisted = 0
    if resolved_claim_ids and config is not None:
        resolved_claim_ids = sorted(set(resolved_claim_ids))
        try:
            persisted = persist_resolved_claims(config, resolved_claim_ids, transaction_id)
        except Exception:
            # The derived database must never advertise a resolution whose
            # durable archive/page transaction did not commit.  Keep the
            # accepted relation receipts for a later retry, but put workload
            # state back into a fail-closed, visible processing state.
            recovery = connect()
            try:
                recovery.execute("BEGIN IMMEDIATE")
                case_ids = {
                    str(row["case_id"])
                    for claim_id in resolved_claim_ids
                    for row in recovery.execute(
                        "SELECT case_id FROM claims WHERE claim_id=?", (claim_id,)
                    )
                }
                for claim_id in resolved_claim_ids:
                    row = recovery.execute(
                        "SELECT claim_handle,evidence_watermark,lifecycle FROM claims WHERE claim_id=?",
                        (claim_id,),
                    ).fetchone()
                    if not row or str(row["lifecycle"]) not in {"resolved", "archived"}:
                        continue
                    previous = str(row["lifecycle"])
                    recovery.execute(
                        "UPDATE claims SET lifecycle='open',resolution_reason=NULL,record_version=record_version+1,updated_at=? WHERE claim_id=?",
                        (now_iso(), claim_id),
                    )
                    _insert_transition(
                        recovery,
                        object_type="claim",
                        object_id=str(row["claim_handle"]),
                        previous_state=previous,
                        next_state="open",
                        reason="resolution_persistence_failed",
                        actor="system",
                        evidence_snapshot_hash=str(row["evidence_watermark"]),
                        context_hash=None,
                        transaction_id=transaction_id,
                    )
                for case_id in case_ids:
                    case = recovery.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
                    if not case:
                        continue
                    prior = recovery.execute(
                        """
                        SELECT previous_state FROM transition_events
                        WHERE transaction_id=? AND object_type='case' AND object_id=?
                          AND next_state='resolved'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (transaction_id, case_id),
                    ).fetchone()
                    next_state = str(prior["previous_state"]) if prior else str(case["state"])
                    recovery.execute(
                        "UPDATE cases SET state=?,processing_state='failed',resolved_at=NULL,updated_at=? WHERE case_id=?",
                        (next_state, now_iso(), case_id),
                    )
                    if next_state != str(case["state"]):
                        _insert_transition(
                            recovery,
                            object_type="case",
                            object_id=case_id,
                            previous_state=str(case["state"]),
                            next_state=next_state,
                            reason="resolution_persistence_failed",
                            actor="system",
                            evidence_snapshot_hash=str(case["evidence_watermark"]),
                            context_hash=str(case["context_hash"]),
                            transaction_id=transaction_id,
                        )
                recovery.commit()
            except Exception:
                recovery.rollback()
                raise
            finally:
                recovery.close()
            if request_id:
                mark_relation_commit(request_id, "blocked", {"error_code": "relation_resolution_persistence_failed"})
            raise
        refresh_index(config, run_id=f"relation-{transaction_id}")
    result = {
        "recorded": recorded,
        "queued": queued,
        "resolved": len(set(resolved_claim_ids)),
        "persisted": persisted,
    }
    if request_id:
        mark_relation_commit(request_id, "complete", result)
    return result


def mark_relation_commit(request_id: str, status: str, result: dict[str, Any]) -> None:
    connection = connect()
    try:
        connection.execute("UPDATE relation_commit_receipts SET state=?,result_json=?,updated_at=? WHERE request_id=?",
                           (status, canonical_json(result), now_iso(), request_id))
        connection.commit()
    finally:
        connection.close()


def resume_relation_commit(config: Optional[dict[str, Any]], receipt: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(receipt["result_json"])
    if receipt["state"] == "complete":
        return result
    if receipt["state"] != "recorded":
        raise ReconciliationError("relation_resolution_pending", "关系收口存在未解决的写入问题；保留记录，不重复覆盖。", 409)
    resolved = json.loads(receipt["resolved_ids_json"])
    if resolved and config is not None:
        import dingtalk_memory_automation as memory
        transaction_id = str(receipt["transaction_id"])
        path = memory.runtime_paths(config)["runs"] / ("claim-resolution-" + transaction_id) / "apply_journal.json"
        journal = json.loads(path.read_text()) if path.is_file() else {}
        if journal.get("state") == "committed":
            indexes = memory.derived_index_targets(memory.runtime_paths(config)["memory_root"])
            if any(memory.sha256_path(Path(row["target"])) != row.get("expected_applied_sha256")
                   for row in journal["paths"] if Path(row["target"]).resolve() not in indexes):
                raise ReconciliationError("relation_resolution_drift", "已收口文件后来发生变化，不能覆盖重做。", 409)
            archived, _keys = _archive_entries()
            covered = {row["claim_id"] for row in archived if row.get("transaction_id") == transaction_id}
            if not set(resolved) <= covered:
                raise ReconciliationError("relation_resolution_receipt_missing", "收口档案缺少本批回执。", 409)
            result["persisted"] = len(resolved)
        elif journal.get("state") in {None, "rolled_back"}:
            result["persisted"] = persist_resolved_claims(config, resolved, transaction_id)
        else:
            raise ReconciliationError("relation_resolution_pending", "先完成原文件事务恢复，再续办关系收口。", 409)
        refresh_index(config, run_id="relation-" + transaction_id)
    mark_relation_commit(str(receipt["request_id"]), "complete", result)
    return result


def _archive_entries() -> tuple[list[dict[str, Any]], set[str]]:
    if not RESOLUTION_ARCHIVE.is_file():
        return [], set()
    rows: list[dict[str, Any]] = []
    keys: set[str] = set()
    for line in RESOLUTION_ARCHIVE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not value.get("receipt_id"):
            raise ReconciliationError("resolution_archive_invalid", "核验收口档案无法验证。", 503)
        rows.append(value)
        keys.add(str(value["receipt_id"]))
    return rows, keys


def persist_resolved_claims(config: dict[str, Any], claim_ids: list[str], transaction_id: str) -> int:
    """Archive evidence receipts, then remove resolved callouts transactionally.

    Archive-first ordering is intentional: a failed page write can be retried,
    while a removed callout can never precede its durable resolution receipt.
    """
    wanted = set(claim_ids)
    if not wanted:
        return 0
    vault_root = router.vault_root_from_config(config)
    scanned = {str(item["claim_id"]): item for item in router.scan_claims(vault_root)}
    connection = connect(readonly=True)
    try:
        rows = {
            str(row["claim_id"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM claims WHERE claim_id IN ({})".format(
                    ",".join("?" for _value in wanted)
                ),
                tuple(sorted(wanted)),
            )
        }
        relations = {
            claim_id: [dict(item) for item in connection.execute(
                """
                SELECT relation,relation_status,source_label,root_fingerprint,provenance_family,
                       independence_group,directness,authority_class,verification_method,reason,observed_at
                FROM evidence_relations WHERE claim_id=? ORDER BY created_at
                """,
                (claim_id,),
            ).fetchall()]
            for claim_id in wanted
        }
    finally:
        connection.close()
    if set(rows) != wanted or set(scanned).intersection(wanted) != wanted:
        raise ReconciliationError("resolution_source_changed", "待收口证据在写入前发生变化。", 409)
    if any(str(rows[claim_id]["lifecycle"]) not in {"resolved", "archived"} for claim_id in wanted):
        raise ReconciliationError("resolution_policy_missing", "存在没有政策收据的证据，本次没有移出。", 409)

    archive_existed = RESOLUTION_ARCHIVE.is_file()
    archive_before = RESOLUTION_ARCHIVE.read_text(encoding="utf-8") if archive_existed else ""
    archive_rows, existing = _archive_entries()
    for claim_id in sorted(wanted):
        item = scanned[claim_id]
        row = rows[claim_id]
        receipt_id = sha256_text(
            f"{claim_id}|{row['resolution_reason']}|{row['record_version']}|{transaction_id}"
        )[:32]
        if receipt_id in existing:
            continue
        marker_payload = {
            key: value
            for key, value in item.items()
            if key not in {"path", "block"}
        }
        archive_rows.append(
            {
                "schema_version": 1,
                "receipt_id": receipt_id,
                "transaction_id": transaction_id,
                "claim_id": claim_id,
                "claim_handle": str(row["claim_handle"]),
                "case_id": str(row["case_id"]),
                "lifecycle": str(row["lifecycle"]),
                "resolution_reason": str(row["resolution_reason"]),
                "policy_version": POLICY_VERSION,
                "resolved_at": now_iso(),
                "source_page": str(item["relative_path"]),
                "claim": marker_payload,
                "visible_evidence": router.claim_visible_fields(str(item["block"])),
                "relations": relations[claim_id],
            }
        )
        existing.add(receipt_id)
    atomic_write(
        RESOLUTION_ARCHIVE,
        "".join(canonical_json(item) + "\n" for item in archive_rows),
        0o600,
    )

    changes: dict[Path, str] = {}
    by_path: dict[Path, list[str]] = {}
    for claim_id in wanted:
        by_path.setdefault(Path(scanned[claim_id]["path"]), []).append(claim_id)
    for path, page_claim_ids in by_path.items():
        text = path.read_text(encoding="utf-8")
        changes[path] = router.remove_claim_blocks(text, page_claim_ids)
    import dingtalk_memory_automation as memory

    lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=True)
    try:
        try:
            memory.apply_changes(
                config,
                f"claim-resolution-{transaction_id}",
                changes,
                lock_held=True,
            )
        except Exception:
            if archive_existed:
                atomic_write(RESOLUTION_ARCHIVE, archive_before, 0o600)
            elif RESOLUTION_ARCHIVE.is_file():
                RESOLUTION_ARCHIVE.unlink()
            raise
    finally:
        lock.close()
    return len(wanted)


def refresh_index(config: dict[str, Any], *, run_id: Optional[str] = None) -> dict[str, Any]:
    plan = plan_reconciliation(config)
    result = apply_plan(
        config,
        plan,
        run_id=run_id or f"refresh-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
        require_maintenance=False,
    )
    import workbench_attention as attention
    if DATABASE == PRODUCTION_DATABASE:
        result["attention"] = attention.refresh_after_index(plan)
    return result


def submit_relations(
    config: dict[str, Any],
    relations: list[dict[str, Any]],
    *,
    source_system: str,
    source_material: dict[str, Any],
    observed_at: Optional[str] = None,
    request_id: Optional[str] = None,
    expected_context: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Persist a privacy-bounded outbox receipt before applying relations."""
    if not relations:
        return {"status": "noop", "recorded": 0, "queued": 0, "resolved": 0}
    validate_write_scope(config)
    RELATION_OUTBOX.mkdir(parents=True, exist_ok=True)
    RELATION_OUTBOX.chmod(0o700)
    if request_id is not None:
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ReconciliationError("relation_request_invalid", "关系提交编号无效。") from exc
    receipt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source_system + ":" + request_id)) if request_id else str(uuid.uuid4())
    descriptors = {
        str(token): {
            **_source_descriptor(value, source_system),
            # The outbox needs identity metadata, never source prose.
            "material": "",
        }
        for token, value in source_material.items()
    }
    payload = {
        "schema_version": 2,
        "receipt_id": receipt_id,
        "source_system": router.single_line(source_system, 40),
        "observed_at": observed_at or now_iso(),
        "relations": relations,
        "source_material": descriptors,
        "created_at": now_iso(),
        "expected_context": expected_context,
    }
    pending = RELATION_OUTBOX / f"{receipt_id}.pending.json"
    done = RELATION_OUTBOX / f"{receipt_id}.done.json"
    payload["request_hash"] = sha256_text(canonical_json({key: payload[key] for key in ("source_system", "observed_at", "relations", "source_material", "expected_context")}))
    with (RELATION_OUTBOX / (receipt_id + ".lock")).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = done if done.is_file() else pending
        if existing.is_file():
            previous = json.loads(existing.read_text())
            if previous.get("request_hash") != payload["request_hash"]:
                raise ReconciliationError("relation_request_conflict", "同一关系提交不能更换来源或内容。", 409)
            payload = previous
            if existing == done:
                return {"status": "applied", "receipt_id": receipt_id, **payload["result"]}
        else:
            atomic_write(pending, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
        if payload.get("state") == "blocked" or int(payload.get("attempt_count", 0)) >= 3:
            return {"status": "deferred", "receipt_id": receipt_id,
                    "error_code": payload.get("last_error_code") or "relation_retry_budget_exhausted"}
        payload["attempt_count"] = int(payload.get("attempt_count", 0)) + 1
        atomic_write(pending, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
        try:
            result = record_relations(
                relations, source_system=source_system, source_material=descriptors,
                observed_at=str(payload["observed_at"]), config=config, request_id=receipt_id,
                expected_context=expected_context,
            )
        except (OSError, sqlite3.Error, ReconciliationError, router.RoutingError) as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            payload.update(state="deferred" if "timeout" in code and payload["attempt_count"] < 3 else "blocked", last_error_code=code)
            atomic_write(pending, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
            return {"status": "deferred", "receipt_id": receipt_id,
                    "error_code": code}
        payload["result"] = result
        atomic_write(pending, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
        os.replace(pending, done)
        return {"status": "applied", "receipt_id": receipt_id, **result}


def drain_relation_outbox(config: dict[str, Any], *, limit: int = 20) -> dict[str, int]:
    if not RELATION_OUTBOX.is_dir():
        return {"attempted": 0, "applied": 0, "deferred": 0}
    attempted = 0
    applied = 0
    deferred = 0
    for pending in sorted(RELATION_OUTBOX.glob("*.pending.json"))[: max(1, limit)]:
        try:
            payload = json.loads(pending.read_text(encoding="utf-8"))
            if payload.get("schema_version") == 2:
                # The submit path owns both the lock and the durable timeout
                # budget. Draining must not create a second retry mechanism.
                if payload.get("state") == "blocked" or int(payload.get("attempt_count", 0)) >= 3:
                    deferred += 1
                    continue
                attempted += 1
                receipt = _resume_outbox_payload(config, payload)
                applied += int(receipt.get("status") == "applied")
                deferred += int(receipt.get("status") != "applied")
                continue
            attempted += 1
            result = record_relations(
                list(payload["relations"]),
                source_system=str(payload["source_system"]),
                source_material=dict(payload["source_material"]),
                observed_at=str(payload["observed_at"]),
                config=config,
                request_id=str(payload["receipt_id"]) if payload.get("schema_version") == 2 else None,
            )
            if payload.get("schema_version") == 2:
                payload["result"] = result
                atomic_write(pending, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
            os.replace(pending, pending.with_name(pending.name.replace(".pending.json", ".done.json")))
            applied += 1
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error, ReconciliationError, router.RoutingError):
            deferred += 1
    return {"attempted": attempted, "applied": applied, "deferred": deferred}


def _resume_outbox_payload(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Resume a v2 receipt with exactly the same identity, payload and lock."""
    receipt_id = str(payload["receipt_id"])
    uuid.UUID(receipt_id)
    pending = RELATION_OUTBOX / (receipt_id + ".pending.json")
    done = RELATION_OUTBOX / (receipt_id + ".done.json")
    with (RELATION_OUTBOX / (receipt_id + ".lock")).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads((done if done.is_file() else pending).read_text())
        if current.get("request_hash") != payload.get("request_hash"):
            raise ReconciliationError("relation_request_conflict", "关系回执内容发生变化。")
        if done.is_file():
            return {"status": "applied", **current["result"]}
        if current.get("state") == "blocked" or int(current.get("attempt_count", 0)) >= 3:
            return {"status": "deferred"}
        current["attempt_count"] = int(current.get("attempt_count", 0)) + 1
        atomic_write(pending, json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
        try:
            result = record_relations(list(current["relations"]), source_system=str(current["source_system"]),
                                      source_material=dict(current["source_material"]), observed_at=str(current["observed_at"]),
                                      config=config, request_id=receipt_id, expected_context=current.get("expected_context"))
        except (OSError, sqlite3.Error, ReconciliationError, router.RoutingError) as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            current.update(state="deferred" if "timeout" in code and current["attempt_count"] < 3 else "blocked", last_error_code=code)
            atomic_write(pending, json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
            return {"status": "deferred", "error_code": code}
        current["result"] = result
        atomic_write(pending, json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
        os.replace(pending, done)
        return {"status": "applied", **result}


def upgrade_pages(config: dict[str, Any]) -> tuple[str, dict[Path, str]]:
    vault_root = router.vault_root_from_config(config)
    claims = router.scan_claims(vault_root)
    paths = sorted({Path(item["path"]) for item in claims})
    changes: dict[Path, str] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(vault_root).as_posix()
        updated = router.upgrade_claim_markers(text, relative)
        if str(updated) != text:
            changes[path] = updated
    return source_watermark(claims), changes


def run_migration(config: dict[str, Any], *, output: Optional[Path] = None, apply: bool = False) -> dict[str, Any]:
    plan = plan_reconciliation(config)
    if output:
        atomic_write(output, json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
    if not apply:
        return {"status": "dry_run", "output": str(output) if output else None, "counts": plan["counts"], "source_watermark": plan["source_watermark"]}
    if not maintenance_active():
        raise ReconciliationError("maintenance_required", "迁移必须在维护模式下执行。", 409)
    original_watermark, changes = upgrade_pages(config)
    if original_watermark != plan["source_watermark"]:
        raise ReconciliationError("source_changed", "生成迁移计划后业务页面发生变化。", 409)
    migration_id = f"reconcile-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    journal_path: Optional[Path] = None
    memory = None
    if changes:
        import dingtalk_memory_automation as memory

        lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=True)
        try:
            memory.apply_changes(config, migration_id, changes, lock_held=True)
            journal_path = memory.runtime_paths(config)["runs"] / migration_id / "apply_journal.json"
        finally:
            lock.close()
    try:
        upgraded_plan = plan_reconciliation(config)
        if upgraded_plan["counts"]["total_claims"] != plan["counts"]["total_claims"]:
            raise ReconciliationError("migration_accounting", "V2 升级后的 claim 数量发生变化。", 409)
        return apply_plan(config, upgraded_plan, run_id=migration_id)
    except Exception:
        if journal_path is not None and memory is not None and journal_path.is_file():
            lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=True)
            try:
                memory.restore_from_journal(config, journal_path, lock_held=True)
            except Exception as rollback_exc:
                raise ReconciliationError(
                    "migration_rollback_failed",
                    f"迁移索引失败，页面回滚也未能完成：{type(rollback_exc).__name__}",
                    503,
                ) from rollback_exc
            finally:
                lock.close()
        raise


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ReconciliationError("config_invalid", "配置文件无效。")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="示例企业证据收敛与 ReviewCase 迁移")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("command", choices=("dry-run", "apply", "status"))
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == "status":
            initialize()
            payload = {"status": "ready", "work_counts": work_counts(), "database": str(DATABASE)}
        else:
            payload = run_migration(config, output=args.output, apply=args.command == "apply")
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, router.RoutingError, ReconciliationError) as exc:
        print(json.dumps({"status": "failed", "error_code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
