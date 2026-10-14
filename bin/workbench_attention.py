#!/usr/bin/env python3
"""User attention is durable and independent of evidence and AI run state.

Rows live with case_messages so acknowledging a saved reply is one transaction.
The rebuildable evidence index must never overwrite an owner's disposition.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import re
import sqlite3
import uuid
from typing import Any

import dingtalk_sync_v2 as syncv2

CLOSED = {"answered", "waiting_external", "superseded"}


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS workbench_materials (
          claim_handle TEXT PRIMARY KEY,case_id TEXT NOT NULL,artifact_kind TEXT NOT NULL,
          version TEXT NOT NULL,title TEXT NOT NULL,source_path TEXT NOT NULL DEFAULT '',observed_at TEXT NOT NULL,
          family_label TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS case_attention (
          case_id TEXT PRIMARY KEY,
          attention_state TEXT NOT NULL,
          reason TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          source_message_id TEXT,
          decision_handles_json TEXT NOT NULL DEFAULT '[]',
          decision_titles_json TEXT NOT NULL DEFAULT '[]',
          basis_context_hash TEXT NOT NULL,
          handled_at TEXT,
          updated_at TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1,
          display_title TEXT NOT NULL DEFAULT '',
          related_context_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS case_attention_events (
          event_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
          attention_state TEXT NOT NULL, reason TEXT NOT NULL,
          source_kind TEXT NOT NULL, source_message_id TEXT,
          context_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    columns = {value[1] for value in connection.execute("PRAGMA table_info(case_attention)")}
    if "decision_titles_json" not in columns:
        connection.execute("ALTER TABLE case_attention ADD COLUMN decision_titles_json TEXT NOT NULL DEFAULT '[]'")
    material_columns = {value[1] for value in connection.execute("PRAGMA table_info(workbench_materials)")}
    if "family_label" not in material_columns:
        connection.execute("ALTER TABLE workbench_materials ADD COLUMN family_label TEXT NOT NULL DEFAULT ''")


def row(case_id: str, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = connection is None
    connection = connection or syncv2.connect_readonly()
    try:
        result = connection.execute("SELECT * FROM case_attention WHERE case_id=?", (case_id,)).fetchone()
        return dict(result) if result else None
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None  # Compatible read while the old schema is being migrated.
        raise
    finally:
        if own:
            connection.close()


def handles(case: dict[str, Any]) -> list[str]:
    values = case.get("claim_handles") or [item.get("claim_handle") for item in case.get("evidence", [])]
    return sorted({str(value) for value in values if value})


def decision_titles(case: dict[str, Any]) -> list[str]:
    return sorted({re.sub(r"[\s，。；、：?？]+", "", str(item.get("title") or ""))
                   for item in case.get("evidence", []) if item.get("lifecycle", "open") == "open"
                   and item.get("claim_kind") in {"human_decision", "human_authorization"}})


def record(
    connection: sqlite3.Connection, case: dict[str, Any], *, state: str,
    reason: str, source_kind: str, source_message_id: str | None = None,
    title: str = "", related_context: list[dict[str, Any]] | None = None,
) -> None:
    if state not in CLOSED | {"needs_user"}:
        raise ValueError("invalid_attention_state")
    stamp = syncv2.now_iso()
    previous = row(str(case["case_id"]), connection)
    display_title = title or (previous or {}).get("display_title") or ""
    related = related_context if related_context is not None else json.loads((previous or {}).get("related_context_json") or "[]")
    connection.execute("""
        INSERT INTO case_attention(
          case_id,attention_state,reason,source_kind,source_message_id,
          decision_handles_json,decision_titles_json,basis_context_hash,handled_at,updated_at,
          display_title,related_context_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(case_id) DO UPDATE SET
          attention_state=excluded.attention_state,reason=excluded.reason,
          source_kind=excluded.source_kind,source_message_id=excluded.source_message_id,
          decision_handles_json=excluded.decision_handles_json,
          decision_titles_json=excluded.decision_titles_json,
          basis_context_hash=excluded.basis_context_hash,handled_at=excluded.handled_at,
          updated_at=excluded.updated_at,revision=case_attention.revision+1,
          display_title=excluded.display_title,related_context_json=excluded.related_context_json
    """, (case["case_id"], state, reason, source_kind, source_message_id,
          json.dumps(handles(case)), json.dumps(decision_titles(case), ensure_ascii=False), case["context_hash"], stamp if state in CLOSED else None,
          stamp, display_title, json.dumps(related, ensure_ascii=False)))
    connection.execute("""
        INSERT INTO case_attention_events VALUES(?,?,?,?,?,?,?,?)
    """, (str(uuid.uuid4()), case["case_id"], state, reason, source_kind,
          source_message_id, case["context_hash"], stamp))


def record_saved_reply(connection: sqlite3.Connection, case: dict[str, Any], message_id: str, archive: bool) -> None:
    if archive:
        record(connection, case, state="answered", reason="你的回复已保存，本轮无需再次确认。",
               source_kind="reply_saved", source_message_id=message_id)


def mark_waiting(case_id: str, message_id: str) -> None:
    connection = syncv2.connect()
    try:
        with connection:
            connection.execute("""
                UPDATE case_attention SET attention_state='waiting_external',
                  reason='你已答复；后续等待外部进展，不再重复要求确认。',updated_at=?
                WHERE case_id=? AND source_message_id=? AND attention_state='answered'
            """, (syncv2.now_iso(), case_id, message_id))
    finally:
        connection.close()


def refresh_case(connection: sqlite3.Connection, case: dict[str, Any]) -> bool:
    """Only a new explicit decision claim can reactivate an answered case.

    Timestamp, source refresh, file naming and context-hash changes do not count.
    Fact-only material does not count. Decisions retain the old context in events.
    """
    previous = row(str(case["case_id"]), connection)
    if not previous or previous["attention_state"] not in CLOSED or case.get("state") != "manual_required":
        return False
    old = set(json.loads(previous["decision_handles_json"]))
    current = set(handles(case))
    old_titles = set(json.loads(previous.get("decision_titles_json") or "[]"))
    new_titles = set(decision_titles(case)) - old_titles
    if old and current - old and new_titles:
        record(connection, case, state="needs_user",
               reason="新资料新增待确认事项：" + "；".join(sorted(new_titles)[:2])[:240] + "。此前的回复与处置仍保留。",
               source_kind="new_decision_evidence")
        return True
    return False


def title_for(case: dict[str, Any]) -> str:
    titles = [str(item.get("title") or "") for item in case.get("evidence", []) if item.get("lifecycle", "open") == "open"]
    if not titles:
        summary = str(case.get("evidence_summary") or "")
        if "代表性事项：" in summary:
            titles = summary.split("代表性事项：", 1)[1].split("；")
    if len(titles) == 1:
        return titles[0].replace("西班牙合作方项目优先级待确认", "西班牙合作方项目优先级确认")
    if titles and all("船舶" in value or "目录册" in value for value in titles):
        return "船舶目录册技术确认" if case.get("dimension_key") == "technical_signoff" else "船舶目录册后续安排"
    return str(case.get("title") or case.get("subject") or "待确认事项")


def read_context(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """One fresh read transaction per list, never a cross-request cache."""
    context: dict[str, Any] = {"attention": {}, "checked": None, "materials": {}}
    if not cases:
        return context
    connection = syncv2.connect_readonly()
    try:
        connection.execute("BEGIN")
        try:
            identifiers = sorted({str(case["case_id"]) for case in cases})
            placeholders = ",".join("?" for _ in identifiers)
            context["attention"] = {str(item["case_id"]): dict(item)
                                    for item in connection.execute(
                                        f"SELECT * FROM case_attention WHERE case_id IN ({placeholders})", identifiers)}
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
        try:
            context["checked"] = connection.execute(
                "SELECT value FROM metadata WHERE key='workbench:materials_checked_at'"
            ).fetchone()
            families = sorted({material_family(case.get("subject") or "") for case in cases})
            placeholders = ",".join("?" for _ in families)
            for item in connection.execute(
                f"SELECT * FROM workbench_materials WHERE family_label IN ({placeholders}) ORDER BY observed_at DESC",
                families,
            ):
                family = str(item["family_label"])
                if family in families:
                    context["materials"].setdefault(family, []).append(dict(item))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc) and "no such column" not in str(exc):
                raise
    finally:
        connection.close()
    return context


def decorate_many(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    context = read_context(cases)
    return [decorate(case, _read_context=context) for case in cases]


def decorate(case: dict[str, Any], *, _read_context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = _read_context if _read_context is not None else read_context([case])
    result = dict(case)
    attention = context["attention"].get(str(case["case_id"]))
    result["display_title"] = str((attention or {}).get("display_title") or title_for(case))
    result["title"] = result["display_title"]
    result["subject"] = result["display_title"]
    result["attention_state"] = (attention or {}).get("attention_state") or (
        "needs_user" if case.get("state") == "manual_required" else "automatic"
    )
    result["attention_reason"] = (attention or {}).get("reason") or ""
    result["source_state"] = case.get("state")
    result["materials_checked_at"] = max(
        (str(item.get("observed_at") or "") for item in case.get("evidence", [])), default=""
    ) or case.get("updated_at")
    result["related_context"] = json.loads((attention or {}).get("related_context_json") or "[]")
    checked = context["checked"]
    if checked:
        result["materials_checked_at"] = checked[0]
    material_rows = [item for item in context["materials"].get(material_family(case.get("subject") or ""), [])
                     if item["case_id"] != case["case_id"]][:16]
    known = {value.get("claim_handle") for value in result["related_context"]}
    for item in material_rows:
        if item["claim_handle"] not in known:
            result["related_context"].append({**item, "reason": "同一事项的后续资料索引；目录册与技术确认表分别核对，版本记录不代表已批准或已发送。"})
    if attention and attention["attention_state"] in CLOSED:
        result.update(state="archived", disposition="archive", archive_trigger=attention["source_kind"],
                      disposition_at=attention["handled_at"], resolved_at=attention["handled_at"],
                      attention_source_message_id=attention["source_message_id"])
    return result


def backfill_legacy_replies() -> dict[str, int]:
    import claim_reconciliation as reconciliation
    counts = {"examined": 0, "archived": 0, "question_kept": 0}
    connection = syncv2.connect()
    try:
        cases = connection.execute("SELECT DISTINCT case_id FROM case_messages WHERE author='user'").fetchall()
        with connection:
            for item in cases:
                case_id = str(item["case_id"])
                if row(case_id, connection):
                    continue
                message = connection.execute("""
                    SELECT * FROM case_messages WHERE case_id=? AND author='user'
                    ORDER BY sequence DESC LIMIT 1
                """, (case_id,)).fetchone()
                counts["examined"] += 1
                content = str(message["content"] or "").rstrip()
                if not content or content[-1] in {"?", "？"}:
                    counts["question_kept"] += 1
                    continue
                case = reconciliation.case_detail(case_id)
                if case["state"] not in {"manual_required", "open", "deferred"}:
                    continue
                thread = connection.execute("SELECT conversation_state FROM case_threads WHERE case_id=?", (case_id,)).fetchone()
                waiting = thread and thread[0] == "waiting_evidence"
                record(connection, case, state="waiting_external" if waiting else "answered",
                       reason="已按保存的历史回复补记处置，无需重复答复。", source_kind="legacy_reply",
                       source_message_id=str(message["message_id"]))
                counts["archived"] += 1
    finally:
        connection.close()
    return counts


def material_kind(title: str) -> str | None:
    """A catalogue and a technical checklist never share a version sequence."""
    if re.search(r"技术确认表|技术核对表|参数确认表|technical.*check", title, re.I):
        return "technical_checklist"
    if re.search(r"目录册|产品手册|catalog", title, re.I):
        return "catalogue"
    return None


def material_family(label: str) -> str:
    return re.sub(r"\s+", "", str(label)).casefold()


def store_material(connection: sqlite3.Connection, claim: dict[str, Any]) -> bool:
    title = str(claim.get("title") or "")
    kind, version = material_kind(title), material_version(title)
    if not kind or not version:
        return False
    connection.execute("""INSERT INTO workbench_materials(claim_handle,case_id,artifact_kind,version,title,source_path,observed_at,family_label)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(claim_handle) DO UPDATE SET
        case_id=excluded.case_id,artifact_kind=excluded.artifact_kind,version=excluded.version,
        title=excluded.title,source_path=excluded.source_path,observed_at=excluded.observed_at,family_label=excluded.family_label""",
        (claim["claim_handle"], claim["case_id"], kind, version, title, claim.get("source_path", ""), claim.get("observed_at", ""),
         material_family(claim.get("subject_label") or "")))
    return True


def material_version(title: str) -> str | None:
    match = re.search(r"(?i)(?<![A-Za-z0-9])V\s*(\d+(?:[._]\d+)*)", title)
    return "V" + match.group(1).replace("_", ".") if match else None


def refresh_after_index(plan: dict[str, Any] | None = None) -> dict[str, int]:
    """Recheck attention after relevant batches; no inference of approvals."""
    import claim_reconciliation as reconciliation
    connection = syncv2.connect()
    counts = {"checked": 0, "new_decisions": 0, "materials": 0}
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='case_attention'").fetchone():
            return counts
        with connection:
            for claim in (plan or {}).get("claims", []):
                counts["materials"] += int(store_material(connection, claim))
            tracked = connection.execute("SELECT case_id FROM case_attention").fetchall()
            for tracked_case in tracked:
                try:
                    case = reconciliation.case_detail(tracked_case["case_id"])
                except reconciliation.ReconciliationError as exc:
                    if exc.code == "case_missing":
                        continue
                    raise
                counts["checked"] += 1
                counts["new_decisions"] += int(refresh_case(connection, case))
            stamp = syncv2.now_iso()
            connection.execute("INSERT INTO metadata(key,value,updated_at) VALUES('workbench:materials_checked_at',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (stamp, stamp))
    finally:
        connection.close()
    return counts


def migrate_owner_corrections() -> dict[str, Any]:
    """Private deployment corrections are deliberately unavailable in this archive."""
    raise RuntimeError("Private history migration is excluded; no correction was applied.")
