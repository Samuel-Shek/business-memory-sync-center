#!/usr/bin/env python3
"""Reconcile a completed transcription review back into governed business memory."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

import dingtalk_memory_automation as memory
import dingtalk_minutes_memory as minutes
import dingtalk_sync_v2 as syncv2


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
PROMPT = ROOT / "prompts" / "dingtalk_review_reconcile.md"
SCHEMA = ROOT / "schemas" / "dingtalk_review_reconcile.schema.json"
MEMORY_ID_RE = re.compile(r"^BUSINESS-[0-9]{8}-[0-9]{3}$")


class ReconcileError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def governed_review_memory_context(config: dict[str, Any]) -> dict[str, Any]:
    allowed = {"memory_id", "title", "category", "memory_kind", "status", "fact_status", "source_check", "summary"}
    rows = []
    for raw in memory.model_memory_context(memory.runtime_paths(config)["memory_root"]):
        if not isinstance(raw, dict) or set(raw) != allowed:
            raise ReconcileError("review_memory_context_shape")
        memory_id = str(raw.get("memory_id") or "")
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ReconcileError("review_memory_context_identity")
        rows.append(
            {
                "memory_id": memory_id,
                "title": memory.sanitize_note_text(raw.get("title"), 160),
                "category": memory.sanitize_note_text(raw.get("category"), 80),
                "memory_kind": memory.sanitize_note_text(raw.get("memory_kind"), 80),
                "status": memory.sanitize_note_text(raw.get("status"), 80),
                "fact_status": memory.sanitize_note_text(raw.get("fact_status"), 80),
                "source_check": memory.sanitize_note_text(raw.get("source_check"), 80),
                # These summaries come from already-governed notes, not fresh model
                # output.  Normalize legacy Markdown/Obsidian syntax before applying
                # the strict proposal sanitizer so one old wikilink or heading cannot
                # block the entire bounded review context export.
                "summary": sanitize_existing_note_excerpt(raw.get("summary"), 1800),
            }
        )
    rows.sort(key=lambda item: item["memory_id"])
    core = {
        "schema_version": 1,
        "policy": "memory_inclination_only",
        "evidence_rule": "历史记忆只能支持哪版更可能正确的倾向；不得替代音频证据或自动定案。",
        "memories": rows,
    }
    return {
        **core,
        "memory_context_sha256": syncv2.sha256_text(syncv2.canonical_json(core)),
        "memory_count": len(rows),
    }


def write_review_memory_context(config: dict[str, Any], output_path: Path) -> dict[str, Any]:
    resolved = output_path.expanduser().resolve(strict=False)
    if not any(part.endswith(".nosync") for part in resolved.parts):
        raise ReconcileError("review_memory_context_outside_private_root")
    if output_path.is_symlink():
        raise ReconcileError("review_memory_context_symlink")
    payload = governed_review_memory_context(config)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output_path.is_file() and output_path.read_text(encoding="utf-8") == text:
        status = "noop"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.chmod(0o700)
        memory.atomic_write(output_path, text)
        status = "written"
    output_path.chmod(0o600)
    return {
        "status": status,
        "memory_context_sha256": payload["memory_context_sha256"],
        "memory_count": payload["memory_count"],
    }


def sanitize_existing_note_excerpt(value: Any, max_chars: int) -> str:
    """Make governed legacy Markdown safe for the model without relaxing output checks."""
    text = value if isinstance(value, str) else ""
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<(?:script|iframe)\b.*?</(?:script|iframe)\s*>", "[嵌入内容已省略]", text)
    text = re.sub(r"(?m)^#{1,6}[ \t]+", "", text)
    text = re.sub(r"(?m)^---[ \t]*$", "", text)
    text = re.sub(r"!\[\[[^\]]+\]\]", "[内部附件已省略]", text)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    return memory.sanitize_note_text(text, max_chars)


def replace_section(body: str, heading: str, value: str) -> str:
    pattern = re.compile(rf"(^## {re.escape(heading)}[ \t]*\n+)(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(body)
    if match:
        return body[: match.start()] + match.group(1) + value.strip() + "\n\n" + body[match.end() :].lstrip("\n")
    return f"## {heading}\n\n{value.strip()}\n\n" + body.lstrip("\n")


def recording_for_task(task_uuid: str) -> sqlite3.Row:
    connection = syncv2.connect_readonly()
    try:
        row = connection.execute("SELECT * FROM recordings WHERE task_uuid=?", (task_uuid,)).fetchone()
        if not row:
            raise ReconcileError("recording_not_registered")
        return row
    finally:
        connection.close()


def unresolved_payload(config: dict[str, Any], row: sqlite3.Row) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records = memory.memory_records(memory.runtime_paths(config)["memory_root"])
    by_id = {str(record["memory_id"]): record for record in records}
    connection = syncv2.connect_readonly()
    try:
        decisions = connection.execute(
            """
            SELECT * FROM decisions
            WHERE recording_id=? AND status IN ('pending','deferred','accepted','merged')
            ORDER BY created_at
            """,
            (str(row["recording_id"]),),
        ).fetchall()
    finally:
        connection.close()
    payload = []
    for decision in decisions:
        candidate = json.loads(str(decision["candidate_json"]))
        memory_id = str(decision["candidate_memory_id"])
        note = by_id.get(memory_id)
        related = []
        for related_id in json.loads(str(decision["related_memory_ids_json"])):
            record = by_id.get(str(related_id))
            related.append(
                {
                    "memory_id": str(related_id),
                    "status": str(record.get("status") or "missing") if record else "missing",
                    "summary": str(record.get("summary") or "") if record else "",
                    "content": (
                        sanitize_existing_note_excerpt(record.get("body"), 3000) if record else ""
                    ),
                }
            )
        payload.append(
            {
                "candidate_memory_id": memory_id,
                "decision_status": str(decision["status"]),
                "title": str(candidate.get("title") or (note or {}).get("title") or memory_id),
                "category": str(candidate.get("category") or (note or {}).get("category") or "business_cognition"),
                "memory_kind": str(candidate.get("memory_kind") or (note or {}).get("memory_kind") or "status"),
                "summary": str(candidate.get("summary") or (note or {}).get("summary") or ""),
                "evidence_boundary": str(candidate.get("evidence_boundary") or ""),
                "related_memory_ids": list(candidate.get("related_memory_ids") or []),
                "conflict_reason": candidate.get("conflict_reason"),
                "related_memories": related,
            }
        )
    return payload, by_id


def validate_proposal(proposal: dict[str, Any], expected_ids: list[str]) -> dict[str, Any]:
    if proposal.get("status") != "completed":
        raise ReconcileError("review_reconcile_incomplete")
    rows = proposal.get("candidates")
    if not isinstance(rows, list):
        raise ReconcileError("review_reconcile_candidates_invalid")
    returned = [str(item.get("candidate_memory_id") or "") for item in rows if isinstance(item, dict)]
    if sorted(returned) != sorted(expected_ids) or len(returned) != len(set(returned)):
        raise ReconcileError("review_reconcile_candidate_set_changed")
    revised = 0
    cleaned = []
    for item in rows:
        if not isinstance(item, dict) or item.get("disposition") not in {"unchanged", "revise"}:
            raise ReconcileError("review_reconcile_disposition_invalid")
        disposition = str(item["disposition"])
        revised += int(disposition == "revise")
        related = list(dict.fromkeys(str(value) for value in item.get("related_memory_ids") or []))
        conflict_reason = memory.sanitize_note_text(item.get("conflict_reason"), 800) or None
        category = str(item.get("category") or "")
        memory_kind = str(item.get("memory_kind") or "")
        if category not in {"business_cognition", "internal_material", "external_communication"}:
            raise ReconcileError("review_reconcile_category_invalid")
        if memory_kind not in {"fact", "judgment", "decision", "status", "open_question"}:
            raise ReconcileError("review_reconcile_kind_invalid")
        cleaned.append(
            {
                "candidate_memory_id": str(item["candidate_memory_id"]),
                "disposition": disposition,
                "title": memory.sanitize_note_text(item.get("title"), 160),
                "category": category,
                "memory_kind": memory_kind,
                "summary": memory.sanitize_note_text(item.get("summary"), 1800),
                "evidence_boundary": memory.sanitize_note_text(item.get("evidence_boundary"), 1200),
                "related_memory_ids": related,
                "conflict_reason": conflict_reason,
            }
        )
    no_change = bool(proposal.get("no_business_change"))
    if no_change != (revised == 0):
        raise ReconcileError("review_reconcile_change_flag_mismatch")
    return {
        "no_business_change": no_change,
        "source_summary": memory.sanitize_note_text(proposal.get("source_summary"), 1800),
        "candidates": cleaned,
    }


def candidate_note_text(
    record: dict[str, Any],
    candidate: dict[str, Any],
    source_ref: str,
    review_sha: str,
) -> str:
    conflict = bool(candidate["related_memory_ids"] and candidate.get("conflict_reason"))
    related_text = "、".join(candidate["related_memory_ids"]) or "无已指定关联条目"
    conflict_text = f"\n\n冲突说明：{candidate['conflict_reason']}" if conflict else ""
    marker = f"<!-- dingtalk-review-reconcile:{review_sha[:24]}:{record['memory_id']} -->"
    body = (
        f"## 记忆内容\n\n{candidate['summary']}\n\n"
        f"## 证据边界\n\n{candidate['evidence_boundary']} 二次核对已完成；业务事实仍需责任人确认。{conflict_text}\n\n"
        f"## 关联记忆\n\n{related_text}\n\n"
        f"## 来源\n\n- `{source_ref}`；二次核对稿和原始媒体仅保存在本机来源库。\n\n"
        f"## 更新记录\n\n{marker}\n- {syncv2.now_iso()}：依据二次核对稿生成候选新版本，旧预览已失效。\n"
    )
    return memory.rebuild_existing_note(
        str(record["raw_frontmatter"]),
        body,
        {
            "source_check": "discrepant" if conflict else "matched",
            "fact_status": "conflicting" if conflict else "pending",
            "evidence_level": "internal_source",
            "reviewed_at": None,
        },
    )


def deterministic_review_summary(report_path: Path) -> tuple[str, dict[str, int]]:
    """Build a bounded status summary from already-validated local review artifacts."""
    result_dir = report_path.parent
    metadata_path = result_dir / "核对运行元数据.json"
    differences_path = result_dir / "差异判断.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        differences = json.loads(differences_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconcileError("prepared_recovery_artifact_invalid") from exc
    items = differences.get("items") if isinstance(differences, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("memory_policy") != "memory_inclination_only"
        or not isinstance(items, list)
        or differences.get("policy") != "memory_inclination_only"
        or differences.get("difference_count") != len(items)
    ):
        raise ReconcileError("prepared_recovery_artifact_invalid")
    critical = 0
    memory_supported = 0
    for item in items:
        if not isinstance(item, dict):
            raise ReconcileError("prepared_recovery_artifact_invalid")
        related = item.get("related_memories")
        memory_supported += int(isinstance(related, list) and bool(related))
        if item.get("basis") == "memory_only" and item.get("decision") != "doubt":
            raise ReconcileError("prepared_recovery_memory_auto_decision")
        if item.get("category") in {"person", "number", "amount", "date", "commitment"}:
            clip = item.get("clip")
            if item.get("decision") != "doubt" or not isinstance(clip, str):
                raise ReconcileError("prepared_recovery_critical_not_preserved")
            clip_path = (result_dir / clip).resolve()
            clips_root = (result_dir / "clips").resolve()
            if clips_root not in clip_path.parents or not clip_path.is_file() or clip_path.stat().st_size == 0:
                raise ReconcileError("prepared_recovery_critical_not_preserved")
            critical += 1
    counts = {
        "difference_count": len(items),
        "memory_supported_count": memory_supported,
        "critical_doubt_count": critical,
    }
    if any(metadata.get(key) != value for key, value in counts.items()):
        raise ReconcileError("prepared_recovery_count_mismatch")
    summary = (
        f"本次二查已逐项对齐完整钉钉转写与 Qwen 全文，共记录 {counts['difference_count']} 处实质差异；"
        f"其中 {counts['critical_doubt_count']} 处人名、数字、金额、日期或承诺类关键差异继续保留存疑及音频切片。"
        f"{counts['memory_supported_count']} 处差异参考了受治理历史记忆，但记忆只用于判断倾向，未替代音频证据或自动定案。"
        "完整差异清单与媒体证据保存在本机二查目录。"
    )
    return summary, counts


def recover_prepared_no_change(
    config: dict[str, Any],
    row: sqlite3.Row,
    existing: sqlite3.Row,
    report_path: Path,
    report_sha: str,
) -> dict[str, Any]:
    """Finish a prepared no-business-change transaction without another model call.

    Eligibility is intentionally strict: the failed apply journal may contain only
    the conversation source note plus the three derived indexes, and the source
    note must already equal its pre-apply backup.  Any candidate-note path causes a
    fail-closed manual stop.
    """
    paths = memory.runtime_paths(config)
    memory_root = paths["memory_root"]
    run_id = f"review-reconcile-{row['recording_id']}-{report_sha[:12]}"
    journal_path = paths["runs"] / run_id / "apply_journal.json"
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconcileError("prepared_recovery_journal_missing") from exc
    if journal.get("state") not in {"prepared", "applying"}:
        raise ReconcileError("prepared_recovery_journal_state")
    source_path = Path(str(row["source_memory_path"]))
    allowed_indexes = {
        memory_root / "00_记忆总览.md",
        memory_root / "99_规则与模板" / "AI检索总表.md",
        memory_root / "90_待确认" / "00_待确认总表.md",
    }
    entries = journal.get("paths")
    if not isinstance(entries, list):
        raise ReconcileError("prepared_recovery_journal_shape")
    changed_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReconcileError("prepared_recovery_journal_shape")
        target = Path(str(entry.get("target") or ""))
        if target == source_path:
            changed_entries.append(entry)
        elif target not in allowed_indexes:
            raise ReconcileError("prepared_recovery_candidate_change")
    if len(changed_entries) != 1:
        raise ReconcileError("prepared_recovery_source_ambiguous")
    source_entry = changed_entries[0]
    backup = Path(str(source_entry.get("backup") or ""))
    if (
        not source_entry.get("existed")
        or not source_path.is_file()
        or source_path.is_symlink()
        or not backup.is_file()
        or source_path.read_bytes() != backup.read_bytes()
    ):
        raise ReconcileError("prepared_recovery_rollback_unverified")

    records = memory.memory_records(memory_root)
    source_record = next(
        (record for record in records if str(record["memory_id"]) == str(row["source_memory_id"])),
        None,
    )
    if not source_record or Path(source_record["path"]) != source_path:
        raise ReconcileError("source_note_missing")
    source_summary, counts = deterministic_review_summary(report_path)
    source_marker = f"<!-- dingtalk-review-reconcile:{report_sha[:24]} -->"
    source_body = replace_section(str(source_record["body"]), "二查核对摘要", source_summary)
    source_body = replace_section(
        source_body,
        "证据边界",
        "本机已完成钉钉全文与独立 ASR 全文的逐项对照。历史记忆只用于判断倾向；"
        "核对提高转写可靠性，但不自动确认会议中的业务事实。"
        + (
            f"仍有 {counts['critical_doubt_count']} 处关键差异保留存疑和音频切片。"
            if counts["critical_doubt_count"]
            else ""
        ),
    )
    source_body = memory.append_section_entry(
        source_body,
        "更新记录",
        f"{source_marker}\n- {syncv2.now_iso()}：复用已完成二查产物，确定性完成无业务候选变化的残留事务。",
        source_marker,
    )
    source_text = memory.rebuild_existing_note(
        str(source_record["raw_frontmatter"]),
        source_body,
        {
            "status": "active",
            "source_check": "matched",
            "evidence_level": "internal_source",
            "processed_at": syncv2.now_iso(),
            "reviewed_at": None,
            "reviewed_by": None,
            "review_mode": None,
        },
    )

    def scoped_verifier(_config: dict[str, Any]) -> dict[str, Any]:
        record = memory.parse_memory_note(source_path, strict=True)
        if not record or str(record["memory_id"]) != str(row["source_memory_id"]):
            raise ReconcileError("prepared_recovery_source_validation")
        expected = "sha256:" + syncv2.sha256_text(memory.canonical_memory_body(str(record["body"])))
        actual = str(memory.frontmatter_scalar(str(record["raw_frontmatter"]), "content_hash") or "")
        if actual != expected or source_marker not in str(record["body"]):
            raise ReconcileError("prepared_recovery_source_validation")
        return {"verdict": "pass", "scope": "single_conversation_source", "memory_id": str(row["source_memory_id"])}

    # The source note is already back at its exact backup.  Derived indexes may
    # legitimately have moved meanwhile, so mark the failed journal rolled back
    # without overwriting those concurrently refreshed index files.
    journal["state"] = "rolled_back"
    journal["rolled_back_at"] = syncv2.now_iso()
    journal["recovery_reason"] = "source_backup_exact; derived_indexes_preserved"
    memory.write_apply_journal(journal_path, journal)

    lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=True)
    try:
        apply_result = memory.apply_changes(
            config,
            f"review-reconcile-recovery-{row['recording_id']}-{report_sha[:12]}",
            {source_path: source_text},
            verifier=scoped_verifier,
            lock_held=True,
        )
    finally:
        lock.close()

    stamp = syncv2.now_iso()
    connection = syncv2.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE review_reconciliations SET status='review_no_business_change',decision_id=NULL,updated_at=? WHERE reconciliation_id=? AND status='prepared'",
            (stamp, str(existing["reconciliation_id"])),
        )
        if connection.total_changes != 1:
            raise ReconcileError("prepared_recovery_transaction_changed")
        connection.execute(
            """
            UPDATE recordings SET review_status='review_no_business_change',reviewed_transcript_sha256=?,
              review_reconciled_at=?,updated_at=? WHERE recording_id=?
            """,
            (report_sha, stamp, stamp, str(row["recording_id"])),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "status": "success",
        "model_called": False,
        "recording_id": str(row["recording_id"]),
        "reconciliation_status": "review_no_business_change",
        "business_changes": 0,
        "conflicts_created": 0,
        "writes": int(apply_result.get("writes") or 0),
        "deterministic_recovery": True,
    }


def prepared_without_apply_can_restart(
    config: dict[str, Any],
    row: sqlite3.Row,
    report_sha: str,
) -> bool:
    """Allow a fresh proposal only when the prepared row never reached file apply.

    A reconciliation row is inserted before ``apply_changes`` starts.  If the
    process dies in that narrow gap there is no apply journal to recover, but it
    is safe to ask the reconciliation model again only when neither the expected
    transaction directory nor any report-specific marker exists anywhere in the
    governed memory tree.  Any ambiguity remains fail-closed.
    """
    paths = memory.runtime_paths(config)
    run_id = f"review-reconcile-{row['recording_id']}-{report_sha[:12]}"
    transaction_dir = paths["runs"] / run_id
    if transaction_dir.exists():
        return False
    source_path = Path(str(row["source_memory_path"]))
    if not source_path.is_file() or source_path.is_symlink():
        return False
    marker = f"dingtalk-review-reconcile:{report_sha[:24]}"
    try:
        records = memory.memory_records(paths["memory_root"])
    except (OSError, memory.AutomationError):
        return False
    source_found = False
    for record in records:
        if marker in str(record.get("body") or ""):
            return False
        if (
            str(record.get("memory_id") or "") == str(row["source_memory_id"])
            and Path(record["path"]) == source_path
        ):
            source_found = True
    return source_found


def reconcile(
    config: dict[str, Any],
    task_uuid: str,
    report_path: Path,
    *,
    shared_ai_lock_fd: Optional[int] = None,
) -> dict[str, Any]:
    if not report_path.is_file() or report_path.is_symlink():
        raise ReconcileError("review_report_missing")
    report_sha = syncv2.sha256_file(report_path)
    row = recording_for_task(task_uuid)
    connection = syncv2.connect_readonly()
    try:
        existing = connection.execute(
            "SELECT reconciliation_id,status FROM review_reconciliations WHERE recording_id=? AND review_sha256=?",
            (str(row["recording_id"]), report_sha),
        ).fetchone()
    finally:
        connection.close()
    if existing and str(existing["status"]) not in {"prepared", "file_committed"}:
        return {
            "status": "noop",
            "model_called": False,
            "reconciliation_status": str(existing["status"]),
            "recording_id": str(row["recording_id"]),
        }
    if existing and str(existing["status"]) == "prepared":
        try:
            return recover_prepared_no_change(config, row, existing, report_path, report_sha)
        except ReconcileError as exc:
            if (
                exc.code != "prepared_recovery_journal_missing"
                or not prepared_without_apply_can_restart(config, row, report_sha)
            ):
                raise
            # No file transaction ever began. Reuse the existing reconciliation
            # identity and obtain a fresh bounded proposal under the same locks.

    candidates, by_id = unresolved_payload(config, row)
    report_text = report_path.read_text(encoding="utf-8", errors="replace")
    if len(report_text) > 180000:
        raise ReconcileError("review_report_too_large")
    payload = {
        "recording": {
            "title": str(row["title"]),
            "observed_at": str(row["start_time"]),
            "source_ref": str(row["source_ref"]),
        },
        "reviewed_transcript": report_text,
        "current_candidates": candidates,
    }
    run_dir = syncv2.RUNS_DIR / str(row["recording_id"]) / "review-reconcile" / report_sha[:16]
    raw = minutes.invoke_codex(
        config,
        prompt_path=PROMPT,
        schema_path=SCHEMA,
        payload_tag="review_reconcile_data",
        payload=payload,
        effort=str(config["dingtalk_minutes_memory"].get("review_reconcile_reasoning_effort") or "high"),
        run_dir=run_dir,
        shared_ai_lock_fd=shared_ai_lock_fd,
    )
    proposal = validate_proposal(raw, [item["candidate_memory_id"] for item in candidates])
    reconciliation_id = str(existing["reconciliation_id"]) if existing else str(uuid.uuid4())
    if not existing:
        stamp = syncv2.now_iso()
        connection = syncv2.connect()
        try:
            connection.execute(
                """
                INSERT INTO review_reconciliations(
                  reconciliation_id,recording_id,review_sha256,original_transcript_sha256,
                  reviewed_path,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,'prepared',?,?)
                """,
                (
                    reconciliation_id, str(row["recording_id"]), report_sha,
                    str(row["transcript_sha256"]), str(report_path), stamp, stamp,
                ),
            )
            connection.commit()
        finally:
            connection.close()
    memory_root = memory.runtime_paths(config)["memory_root"]
    source_record = by_id.get(str(row["source_memory_id"]))
    if not source_record:
        raise ReconcileError("source_note_missing")
    review_metadata = {}
    metadata_path = report_path.parent / "核对运行元数据.json"
    if metadata_path.is_file() and not metadata_path.is_symlink():
        try:
            review_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            review_metadata = {}
    unresolved_critical = int(review_metadata.get("critical_doubt_count") or 0)
    source_marker = f"<!-- dingtalk-review-reconcile:{report_sha[:24]} -->"
    source_body = replace_section(str(source_record["body"]), "二查核对摘要", proposal["source_summary"])
    source_body = replace_section(
        source_body,
        "证据边界",
        "本机已完成钉钉全文与独立 ASR 全文的逐项对照。历史记忆只用于判断倾向；"
        "核对提高转写可靠性，但不自动确认会议中的业务事实。"
        + (f"仍有 {unresolved_critical} 处关键差异保留存疑和音频切片。" if unresolved_critical else ""),
    )
    source_body = memory.append_section_entry(
        source_body,
        "更新记录",
        f"{source_marker}\n- {syncv2.now_iso()}：二次核对完成并已对业务候选进行版本对账。",
        source_marker,
    )
    changes: dict[Path, str] = {
        Path(source_record["path"]): memory.rebuild_existing_note(
            str(source_record["raw_frontmatter"]),
            source_body,
            {
                "status": "active",
                "source_check": "matched",
                "evidence_level": "internal_source",
                "processed_at": syncv2.now_iso(),
                "reviewed_at": None,
                "reviewed_by": None,
                "review_mode": None,
            },
        )
    }
    decision_by_id: dict[str, sqlite3.Row] = {}
    connection = syncv2.connect_readonly()
    try:
        for decision in connection.execute(
            "SELECT * FROM decisions WHERE recording_id=?",
            (str(row["recording_id"]),),
        ).fetchall():
            decision_by_id[str(decision["candidate_memory_id"])] = decision
    finally:
        connection.close()
    reserved: set[str] = set()
    registration_specs: list[tuple[dict[str, Any], Path, str]] = []
    business_changes = 0
    conflicts_created = 0
    records = list(by_id.values())
    for revised in proposal["candidates"]:
        if revised["disposition"] == "unchanged":
            continue
        business_changes += 1
        memory_id = str(revised["candidate_memory_id"])
        decision = decision_by_id[memory_id]
        candidate = {
            "action": "conflict" if revised["related_memory_ids"] and revised.get("conflict_reason") else "annotate_page",
            "dedupe_key": f"review-reconcile:{row['recording_id']}:{report_sha}:{memory_id}",
            "title": revised["title"],
            "category": revised["category"],
            "memory_kind": revised["memory_kind"],
            "summary": revised["summary"],
            "evidence_boundary": revised["evidence_boundary"],
            "related_memory_ids": revised["related_memory_ids"],
            "conflict_reason": revised.get("conflict_reason"),
            "content": revised["summary"],
        }
        if str(decision["status"]) in {"pending", "deferred"} and memory_id in by_id:
            record = by_id[memory_id]
            path = Path(record["path"])
            changes[path] = candidate_note_text(record, candidate, str(row["source_ref"]), report_sha)
            registration_specs.append((candidate, path, memory_id))
            continue
        conflict_candidate = {
            **candidate,
            "action": "conflict",
            "dedupe_key": f"review-confirmed-conflict:{row['recording_id']}:{report_sha}:{memory_id}",
            "related_memory_ids": [memory_id],
            "conflict_reason": "二次核对改变了已处理条目的业务含义；不得自动覆盖，需由 Example Owner 决策。",
        }
        path, text, new_memory_id = minutes.minutes_pending_change(
            config,
            memory_root,
            records,
            reserved,
            row,
            conflict_candidate,
            review_completed=True,
            page_overrides=changes,
        )
        changes[path] = text
        registration_specs.append((conflict_candidate, path, new_memory_id))
        conflicts_created += 1

    lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=True)
    try:
        apply_result = memory.apply_changes(
            config,
            f"review-reconcile-{row['recording_id']}-{report_sha[:12]}",
            changes,
            lock_held=True,
        )
    finally:
        lock.close()

    connection = syncv2.connect()
    try:
        connection.execute(
            "UPDATE review_reconciliations SET status='file_committed',updated_at=? WHERE reconciliation_id=?",
            (syncv2.now_iso(), reconciliation_id),
        )
        connection.commit()
    finally:
        connection.close()

    connection = syncv2.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        linked_decision_id = None
        refreshed = {str(record["memory_id"]): record for record in memory.memory_records(memory_root)}
        for candidate, path, memory_id in registration_specs:
            related_hashes = {
                related_id: syncv2.sha256_file(Path(refreshed[related_id]["path"]))
                for related_id in candidate["related_memory_ids"]
                if related_id in refreshed
            }
            registered = syncv2.register_decision(
                recording_id=str(row["recording_id"]),
                source_ref=str(row["source_ref"]),
                candidate=candidate,
                candidate_memory_id=memory_id,
                candidate_path=path,
                related_memory_ids=list(candidate["related_memory_ids"]),
                related_hashes=related_hashes,
                connection=connection,
            )
            linked_decision_id = linked_decision_id or str(registered["decision_id"])
        reconciliation_status = (
            "review_no_business_change"
            if business_changes == 0
            else ("review_conflict_created" if conflicts_created else "review_candidates_revised")
        )
        stamp = syncv2.now_iso()
        connection.execute(
            """
            UPDATE review_reconciliations
            SET status=?,decision_id=?,updated_at=?
            WHERE reconciliation_id=?
            """,
            (reconciliation_status, linked_decision_id, stamp, reconciliation_id),
        )
        connection.execute(
            """
            UPDATE recordings SET review_status=?,reviewed_transcript_sha256=?,review_reconciled_at=?,
              media_archive_status=CASE WHEN media_archive_status='archived_pending_review'
                THEN 'archived' ELSE media_archive_status END,updated_at=?
            WHERE recording_id=?
            """,
            (reconciliation_status, report_sha, stamp, stamp, str(row["recording_id"])),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "status": "success",
        "model_called": True,
        "recording_id": str(row["recording_id"]),
        "reconciliation_status": reconciliation_status,
        "business_changes": business_changes,
        "conflicts_created": conflicts_created,
        "writes": int(apply_result.get("writes") or 0),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile a reviewed DingTalk transcript with business memory")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--task-uuid")
    parser.add_argument("--shared-ai-lock-fd", type=int, help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", type=Path)
    mode.add_argument("--context-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    syncv2.initialize()
    try:
        config = minutes.load_config(args.config)
        if args.context_output:
            result = write_review_memory_context(config, args.context_output)
        else:
            if not args.task_uuid:
                raise ReconcileError("review_task_uuid_missing")
            result = reconcile(
                config,
                str(args.task_uuid),
                args.report,
                shared_ai_lock_fd=args.shared_ai_lock_fd,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except minutes.MinutesMemoryError as exc:
        if exc.code == "chat_ai_priority":
            print(json.dumps({
                "status": "deferred",
                "reason": "chat_ai_priority",
                "model_called": False,
            }, ensure_ascii=False, sort_keys=True))
            return 0
        print(json.dumps({"status": "failed", "error_code": exc.code}, ensure_ascii=False))
        return 2
    except (ReconcileError, memory.AutomationError, syncv2.SyncV2Error) as exc:
        print(json.dumps({"status": "failed", "error_code": getattr(exc, "code", "failed")}, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_code": type(exc).__name__}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
