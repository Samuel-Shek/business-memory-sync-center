#!/usr/bin/env python3
"""Sync DingTalk Minutes into governed BUSINESS business memory.

Raw audio, transcript text, task identifiers and model intermediates remain in
local .nosync storage.  Obsidian receives only a concise source note and
governed business-memory candidates.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import dingtalk_memory_automation as memory
import dingtalk_sync_v2 as syncv2
import business_page_router as business_router
import claim_reconciliation as reconciliation


os.umask(0o077)

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = BRIDGE_ROOT / "config.json"
MINUTES_ROOT = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync'))
SYNC_STATE = MINUTES_ROOT / ".state" / "sync-state.json"
RAW_ROOT = MINUTES_ROOT / ".raw"
CHUNK_PROMPT = BRIDGE_ROOT / "prompts" / "dingtalk_minutes_chunk.md"
CHUNK_SCHEMA = BRIDGE_ROOT / "schemas" / "dingtalk_minutes_chunk.schema.json"
FINAL_PROMPT = BRIDGE_ROOT / "prompts" / "dingtalk_minutes_final.md"
FINAL_SCHEMA = BRIDGE_ROOT / "schemas" / "dingtalk_minutes_final.schema.json"
SUMMARY_COMPARE_PROMPT = BRIDGE_ROOT / "prompts" / "dingtalk_minutes_summary_compare.md"
SUMMARY_COMPARE_SCHEMA = BRIDGE_ROOT / "schemas" / "dingtalk_minutes_summary_compare.schema.json"
TZ = ZoneInfo("Asia/Shanghai")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MinutesMemoryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = memory.load_config(path)
    minutes = config.get("dingtalk_minutes_memory")
    if not isinstance(minutes, dict):
        raise MinutesMemoryError("invalid_config", "dingtalk_minutes_memory config is missing")
    fixed = {
        "enabled": True,
        "source_sync_minutes": [2, 32],
        "semantic_minutes": [10, 40],
        "chunk_reasoning_effort": "high",
        "final_reasoning_effort": "high",
        "summary_comparison_reasoning_effort": "high",
        "summary_comparison_policy_version": 1,
        "subscription_only": True,
        "allow_paid_api_fallback": False,
        "allow_model_fallback": False,
        "audio_relisten": False,
        "secondary_transcription_review": True,
    }
    for key, expected in fixed.items():
        if minutes.get(key) != expected:
            raise MinutesMemoryError("invalid_config", f"governed minutes setting is invalid: {key}")
    if str(minutes.get("model") or "") not in memory.GOVERNED_ALLOWED_MODELS:
        raise MinutesMemoryError(
            "invalid_config",
            "governed model must be one of: " + ",".join(sorted(memory.GOVERNED_ALLOWED_MODELS)),
        )
    if int(minutes.get("chunk_chars", 0)) != 100000:
        raise MinutesMemoryError("invalid_config", "minutes chunk size must be 100000 characters")
    if int(minutes.get("max_model_calls_per_run", 0)) != 2:
        raise MinutesMemoryError("invalid_config", "minutes run must allow exactly two model calls")
    datetime.fromisoformat(str(minutes.get("cutover_at")))
    return config


def clean_text(value: Any, limit: int) -> str:
    text = CONTROL_RE.sub("", value if isinstance(value, str) else "")
    text = re.sub(r"\s+", " ", text).strip()
    if memory.SECRET_RE.search(text):
        text = memory.SECRET_RE.sub("[敏感凭据已省略]", text)
    return text[:limit].strip()


def clean_transcript_text(value: Any) -> str:
    """Normalize transcript text without dropping any source characters."""
    text = CONTROL_RE.sub("", value if isinstance(value, str) else "")
    text = re.sub(r"\s+", " ", text).strip()
    if memory.SECRET_RE.search(text):
        text = memory.SECRET_RE.sub("[敏感凭据已省略]", text)
    return text


def state_timestamp(value: Any) -> str:
    milliseconds = int(value)
    return datetime.fromtimestamp(milliseconds / 1000, TZ).isoformat(timespec="seconds")


def load_sync_entries() -> list[tuple[str, dict[str, Any]]]:
    if not SYNC_STATE.is_file():
        raise MinutesMemoryError("sync_state_missing", "DingTalk Minutes sync state is missing")
    try:
        payload = json.loads(SYNC_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MinutesMemoryError("sync_state_invalid", "DingTalk Minutes sync state is unreadable") from exc
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise MinutesMemoryError("sync_state_invalid", "DingTalk Minutes entries are invalid")
    return sorted(
        ((str(key), value) for key, value in entries.items() if isinstance(value, dict)),
        key=lambda item: int(item[1].get("start_time") or 0),
    )


def required_missing_artifacts(entry: dict[str, Any]) -> list[str]:
    """Todos are useful enrichment; transcript/media completeness is governed separately."""
    return [
        str(value) for value in (entry.get("missing_artifacts") or [])
        if value not in {"todos", "media"}
    ]


def transcript_payload(task_uuid: str) -> tuple[Path, dict[str, Any]]:
    path = RAW_ROOT / task_uuid / "transcript.json"
    if not path.is_file():
        raise MinutesMemoryError("transcript_missing", "complete transcript is not present locally")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MinutesMemoryError("transcript_invalid", "transcript JSON is unreadable") from exc
    paragraphs = payload.get("paragraphList")
    if payload.get("complete") is not True or not isinstance(paragraphs, list):
        raise MinutesMemoryError("transcript_incomplete", "transcript is not complete")
    if str(payload.get("taskUuid") or "") != task_uuid:
        raise MinutesMemoryError("transcript_identity", "transcript task identity does not match its source folder")
    if payload.get("paragraphCount") != len(paragraphs):
        raise MinutesMemoryError("transcript_coverage", "transcript paragraph count does not match the complete paragraph list")
    if not paragraphs or any(
        not isinstance(paragraph, dict) or not clean_transcript_text(paragraph.get("paragraph"))
        for paragraph in paragraphs
    ):
        raise MinutesMemoryError("transcript_coverage", "transcript contains a missing or empty paragraph")
    return path, payload


def native_summary_payload(task_uuid: str) -> dict[str, Any]:
    path = RAW_ROOT / task_uuid / "summary.md"
    if not path.is_file():
        return {"available": False, "text": "", "sha256": None}
    entry = dict(load_sync_entries()).get(task_uuid)
    if not isinstance(entry, dict):
        raise MinutesMemoryError("native_summary_provenance_missing", "Summary origin is not bound to a source entry")
    if (entry.get("native_summary_missing") or entry.get("summary_origin") == "local-ai"
            or "summary" in (entry.get("missing_artifacts") or [])):
        return {"available": False, "text": "", "sha256": None}
    try:
        lines = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = re.sub(r"^#{1,6}\s*", "", raw).strip()
            if line:
                lines.append(line)
        text = sanitize_native_summary("；".join(lines), task_uuid)
        return {
            "available": bool(text),
            "text": text,
            "sha256": syncv2.sha256_file(path),
        }
    except OSError as exc:
        raise MinutesMemoryError("native_summary_unreadable", "DingTalk native summary exists but is unreadable") from exc


def sanitize_native_summary(value: str, task_uuid: str) -> str:
    summary = memory.sanitize_note_text(clean_text(value, 6000), 1800)
    if task_uuid:
        summary = summary.replace(task_uuid, "[任务标识已省略]")
    return summary


def upsert_discovered_recording(
    task_uuid: str,
    entry: dict[str, Any],
    transcript_path: Path,
    transcript_sha256: str,
) -> tuple[sqlite3.Row, bool, bool]:
    syncv2.initialize()
    connection = syncv2.connect()
    try:
        existing = connection.execute("SELECT * FROM recordings WHERE task_uuid=?", (task_uuid,)).fetchone()
        stamp = syncv2.now_iso()
        if existing:
            if str(existing["transcript_sha256"]) != transcript_sha256:
                connection.execute("DELETE FROM recording_chunks WHERE recording_id=?", (str(existing["recording_id"]),))
                connection.execute(
                    """
                    UPDATE decisions SET status='invalidated',resolution='source_updated',resolved_at=?,updated_at=?
                    WHERE recording_id=? AND status IN ('pending','deferred')
                    """,
                    (stamp, stamp, str(existing["recording_id"])),
                )
                connection.execute("DELETE FROM decision_previews WHERE decision_id IN (SELECT decision_id FROM decisions WHERE recording_id=?)", (str(existing["recording_id"]),))
                connection.execute(
                    """
                    UPDATE recordings SET transcript_sha256=?,media_sha256=?,status='discovered',chunk_count=0,
                      transcript_version=transcript_version+1,review_status='pending',
                      reviewed_transcript_sha256=NULL,review_reconciled_at=NULL,
                      native_summary_sha256=NULL,independent_summary_sha256=NULL,
                      summary_comparison_sha256=NULL,summary_comparison_status='pending',
                      last_error_code=NULL,updated_at=?,completed_at=NULL WHERE recording_id=?
                    """,
                    (transcript_sha256, str(entry.get("media_sha256") or ""), stamp, str(existing["recording_id"])),
                )
                connection.commit()
                existing = connection.execute("SELECT * FROM recordings WHERE task_uuid=?", (task_uuid,)).fetchone()
                return existing, False, True
            incoming_media = str(entry.get("media_sha256") or "")
            stored_media = str(existing["media_sha256"] or "")
            if incoming_media and incoming_media != stored_media:
                if stored_media:
                    raise MinutesMemoryError("media_source_conflict", "Existing media binding differs from the source")
                # A text-first source can acquire its audio later. Bind only a
                # verified local file, without invalidating any text/model work.
                name = str(entry.get("media_filename") or "")
                if entry.get("media_storage") == "local" and name:
                    media_path = MINUTES_ROOT / "音频" / name
                    if (Path(name).name != name or media_path.is_symlink()
                            or not media_path.is_file() or media_path.parent.is_symlink()
                            or media_path.stat().st_size != entry.get("media_bytes")
                            or syncv2.sha256_file(media_path) != incoming_media
                            or syncv2.sha256_file(transcript_path) != transcript_sha256):
                        raise MinutesMemoryError("media_source_unverified", "Local media or transcript proof is invalid")
                    updated = connection.execute(
                        "UPDATE recordings SET media_sha256=?,updated_at=? WHERE recording_id=? AND transcript_sha256=? AND media_sha256=''",
                        (incoming_media, stamp, str(existing["recording_id"]), transcript_sha256),
                    )
                    if updated.rowcount != 1:
                        raise MinutesMemoryError("source_binding_changed", "Source binding changed during verification")
                    connection.commit()
                    existing = connection.execute("SELECT * FROM recordings WHERE task_uuid=?", (task_uuid,)).fetchone()
            return existing, False, False
        recording_id = str(uuid.uuid4())
        source_ref = f"dingtalk-minutes-batch:{recording_id}"
        connection.execute(
            """
            INSERT INTO recordings(
              recording_id,task_uuid,task_key_sha256,source_ref,title,start_time,source_filename,
              transcript_path,transcript_sha256,media_sha256,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'discovered',?,?)
            """,
            (
                recording_id,
                task_uuid,
                syncv2.sha256_text(task_uuid),
                source_ref,
                clean_text(entry.get("title"), 160) or "未命名听记",
                state_timestamp(entry.get("start_time")),
                str(entry.get("filename") or ""),
                str(transcript_path),
                transcript_sha256,
                str(entry.get("media_sha256") or ""),
                stamp,
                stamp,
            ),
        )
        connection.commit()
        return connection.execute("SELECT * FROM recordings WHERE recording_id=?", (recording_id,)).fetchone(), True, False
    finally:
        connection.close()


def _stored_independent_summary_hash(row: sqlite3.Row) -> Optional[str]:
    proposal_path = syncv2.RUNS_DIR / str(row["recording_id"]) / "final" / "proposal.json"
    if not proposal_path.is_file():
        return None
    try:
        payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    source_summary = payload.get("source_summary") if isinstance(payload, dict) else None
    if not isinstance(source_summary, dict):
        return None
    normalized = {
        "title": memory.sanitize_note_text(source_summary.get("title"), 100),
        "summary": memory.sanitize_note_text(source_summary.get("summary"), 1800),
        "evidence_boundary": memory.sanitize_note_text(source_summary.get("evidence_boundary"), 1200),
    }
    if not all(normalized.values()):
        return None
    return syncv2.sha256_text(syncv2.canonical_json(normalized))


def _stored_summary_comparison_hash(row: sqlite3.Row) -> Optional[str]:
    comparison_path = syncv2.RUNS_DIR / str(row["recording_id"]) / "summary-compare" / "summary-comparison.json"
    if not comparison_path.is_file():
        return None
    try:
        payload = json.loads(comparison_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return syncv2.sha256_text(syncv2.canonical_json(payload))


def _artifact_contract_matches(
    metadata: object,
    expected: dict[str, Any],
    *,
    allow_prior_prompt: bool = False,
) -> bool:
    """Verify cached-model metadata without conflating prompt age with data loss."""
    if not isinstance(metadata, dict):
        return False
    for key, value in expected.items():
        if key == "prompt_sha256":
            continue
        if metadata.get(key) != value:
            return False
    recorded_prompt_sha = metadata.get("prompt_sha256")
    return recorded_prompt_sha == expected.get("prompt_sha256") or bool(
        allow_prior_prompt
        and isinstance(recorded_prompt_sha, str)
        and re.fullmatch(r"[0-9a-f]{64}", recorded_prompt_sha)
    )


def invalidate_changed_summary_inputs(row: sqlite3.Row, native: dict[str, Any]) -> Optional[str]:
    """Requeue only records whose persisted comparison inputs or outputs changed."""
    if str(row["summary_comparison_status"] or "") != "compared":
        return None
    independent_changed = (
        not row["independent_summary_sha256"]
        or _stored_independent_summary_hash(row) != str(row["independent_summary_sha256"])
    )
    native_changed = native.get("sha256") != row["native_summary_sha256"]
    comparison_changed = (
        not row["summary_comparison_sha256"]
        or _stored_summary_comparison_hash(row) != str(row["summary_comparison_sha256"])
    )
    if not (independent_changed or native_changed or comparison_changed):
        return None
    stamp = syncv2.now_iso()
    connection = syncv2.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if independent_changed:
            connection.execute(
                """
                UPDATE decisions SET status='invalidated',resolution='independent_summary_changed',
                  resolved_at=?,updated_at=?
                WHERE recording_id=? AND status IN ('pending','deferred')
                """,
                (stamp, stamp, str(row["recording_id"])),
            )
            connection.execute(
                "DELETE FROM decision_previews WHERE decision_id IN (SELECT decision_id FROM decisions WHERE recording_id=?)",
                (str(row["recording_id"]),),
            )
            connection.execute(
                """
                UPDATE recordings SET status='ready_finalize',independent_summary_sha256=NULL,
                  summary_comparison_sha256=NULL,summary_comparison_status='pending',
                  last_error_code='independent_summary_changed',updated_at=?,completed_at=NULL
                WHERE recording_id=?
                """,
                (stamp, str(row["recording_id"])),
            )
            reason = "independent_summary_changed"
        else:
            reason = "native_summary_changed" if native_changed else "summary_comparison_changed"
            connection.execute(
                """
                UPDATE recordings SET status='comparison_refresh_pending',summary_comparison_sha256=NULL,
                  summary_comparison_status='pending',last_error_code=?,updated_at=?
                WHERE recording_id=?
                """,
                (reason, stamp, str(row["recording_id"])),
            )
        connection.commit()
        return reason
    finally:
        connection.close()


def run_source_sync(config: dict[str, Any]) -> dict[str, Any]:
    cutover = datetime.fromisoformat(str(config["dingtalk_minutes_memory"]["cutover_at"]))
    discovered = 0
    unchanged = 0
    requeued = 0
    failures: list[str] = []
    waiting_items: list[dict[str, Any]] = []
    entries = load_sync_entries()
    with syncv2.file_lock(syncv2.SOURCE_LOCK):
        for task_uuid, entry in entries:
            try:
                observed = datetime.fromisoformat(state_timestamp(entry.get("start_time")))
                if observed < cutover:
                    continue
                required_missing = required_missing_artifacts(entry)
                if required_missing:
                    waiting_items.append({"task_ref_sha256": syncv2.sha256_text(task_uuid),
                                          "reason": "source_incomplete", "missing": required_missing,
                                          "video_waiting": bool(entry.get("video_waiting_instruction"))})
                    continue
                if not entry.get("video_waiting_instruction") and (not entry.get("media_sha256") or entry.get("media_storage") not in {"local", "pssd"}):
                    waiting_items.append({"task_ref_sha256": syncv2.sha256_text(task_uuid),
                                          "reason": "media_incomplete", "missing": ["media"],
                                          "video_waiting": False})
                    continue
                path, _transcript = transcript_payload(task_uuid)
                row, created, changed = upsert_discovered_recording(task_uuid, entry, path, syncv2.sha256_file(path))
                discovered += int(created)
                invalidation = None
                if not created and not changed:
                    invalidation = invalidate_changed_summary_inputs(row, native_summary_payload(task_uuid))
                requeued += int(invalidation is not None)
                unchanged += int(not created and not changed and invalidation is None)
            except (MinutesMemoryError, memory.AutomationError, syncv2.SyncV2Error, OSError, sqlite3.Error) as exc:
                code = getattr(exc, "code", "source_sync_failed")
                failures.append(str(code))
    resolved_incidents = resolve_visible_incidents(entries)
    unique_failures = sorted(set(failures))
    hard_failures = source_hard_failures(unique_failures)
    syncv2.set_component_health(
        "recording_source",
        not hard_failures,
        hard_failures[0] if hard_failures else None,
    )
    return {
        "status": "partial" if failures else "waiting_source" if waiting_items else "success",
        "waiting_items": waiting_items,
        "waiting_count": len(waiting_items),
        "discovered": discovered,
        "source_notes_written": 0,
        "source_notes_updated": 0,
        "publication_gate": "full_transcript_summary_and_native_comparison",
        "requeued_after_hash_change": requeued,
        "unchanged": unchanged,
        "resolved_incidents": resolved_incidents,
        "failures": unique_failures,
    }


def source_hard_failures(failures: list[str]) -> list[str]:
    """Keep upstream media availability separate from source-writer health."""
    return [code for code in failures if code != "media_incomplete"]


def resolve_visible_incidents(entries: list[tuple[str, dict[str, Any]]]) -> int:
    observed: list[dict[str, Any]] = []
    for task_uuid, entry in entries:
        try:
            observed.append(
                {
                    "task_uuid": task_uuid,
                    "start_time": datetime.fromisoformat(state_timestamp(entry.get("start_time"))),
                    "title_fingerprint": syncv2.sha256_text(clean_text(entry.get("title"), 160).casefold()),
                }
            )
        except (TypeError, ValueError):
            continue
    connection = syncv2.connect()
    recovered_ids: list[str] = []
    try:
        rows = connection.execute(
            "SELECT * FROM incidents WHERE incident_type='recording_not_visible_to_source_api' AND status='open'"
        ).fetchall()
        for row in rows:
            try:
                detail = json.loads(str(row["safe_detail_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            expected_task = str(detail.get("task_uuid") or "")
            expected_title = str(detail.get("title_fingerprint") or "")
            expected_start_raw = str(detail.get("expected_start") or "")
            exact_match = bool(expected_task) and any(item["task_uuid"] == expected_task for item in observed)
            fingerprint_match = False
            if expected_title and expected_start_raw:
                try:
                    expected_start = datetime.fromisoformat(expected_start_raw)
                    fingerprint_match = any(
                        item["title_fingerprint"] == expected_title
                        and abs((item["start_time"] - expected_start).total_seconds()) <= 120
                        for item in observed
                    )
                except ValueError:
                    fingerprint_match = False
            if not exact_match and not fingerprint_match:
                continue
            stamp = syncv2.now_iso()
            connection.execute(
                "UPDATE incidents SET status='resolved',updated_at=?,resolved_at=? WHERE incident_id=?",
                (stamp, stamp, str(row["incident_id"])),
            )
            recovered_ids.append(str(row["incident_id"]))
        connection.commit()
    finally:
        connection.close()
    for incident_id in recovered_ids:
        syncv2.emit_outbox(
            "sync_recovery",
            f"missing-recording-recovered:{incident_id}",
            {"component": "recording_source_visibility"},
        )
    return len(recovered_ids)


def paragraph_documents(transcript: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for index, raw in enumerate(transcript.get("paragraphList") or [], start=1):
        if not isinstance(raw, dict):
            raise MinutesMemoryError("transcript_coverage", "transcript contains a non-object paragraph")
        text = clean_transcript_text(raw.get("paragraph"))
        if not text:
            raise MinutesMemoryError("transcript_coverage", "transcript contains an empty paragraph")
        speaker = clean_text(raw.get("speakerDisplay") or raw.get("nickName"), 80) or "发言人"
        result.append({"token": f"P{index:06d}", "speaker": speaker, "text": text})
    if len(result) != int(transcript.get("paragraphCount") or -1):
        raise MinutesMemoryError("transcript_coverage", "normalized transcript does not cover every source paragraph")
    return result


def split_chunks(paragraphs: list[dict[str, str]], limit: int) -> list[list[dict[str, str]]]:
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for paragraph in paragraphs:
        candidate = current + [paragraph]
        if current and len(syncv2.canonical_json(candidate)) > limit:
            chunks.append(current)
            current = []
        if len(syncv2.canonical_json([paragraph])) > limit:
            raise MinutesMemoryError(
                "transcript_paragraph_oversized",
                "one transcript paragraph exceeds the governed chunk limit; refusing to truncate it",
            )
        current.append(paragraph)
    if current:
        chunks.append(current)
    return chunks


def ensure_chunks(config: dict[str, Any], row: sqlite3.Row) -> int:
    _path, transcript = transcript_payload(str(row["task_uuid"]))
    paragraphs = paragraph_documents(transcript)
    if not paragraphs:
        raise MinutesMemoryError("transcript_empty", "transcript contains no usable paragraphs")
    chunks = split_chunks(paragraphs, int(config["dingtalk_minutes_memory"]["chunk_chars"]))
    connection = syncv2.connect()
    try:
        stored = connection.execute(
            "SELECT * FROM recording_chunks WHERE recording_id=? ORDER BY chunk_index",
            (str(row["recording_id"]),),
        ).fetchall()
        existing = len(stored)
        if existing:
            expected = [
                (
                    index,
                    int(chunk[0]["token"][1:]),
                    int(chunk[-1]["token"][1:]),
                    len(syncv2.canonical_json(chunk)),
                    syncv2.sha256_text(syncv2.canonical_json(chunk)),
                )
                for index, chunk in enumerate(chunks)
            ]
            actual = [
                (
                    int(item["chunk_index"]),
                    int(item["paragraph_start"]),
                    int(item["paragraph_end"]),
                    int(item["input_chars"]),
                    str(item["input_sha256"]),
                )
                for item in stored
            ]
            if actual != expected:
                raise MinutesMemoryError("chunk_state_mismatch", "stored chunks do not cover the current complete transcript")
        if not existing:
            stamp = syncv2.now_iso()
            for index, chunk in enumerate(chunks):
                document = syncv2.canonical_json(chunk)
                first = int(chunk[0]["token"][1:])
                last = int(chunk[-1]["token"][1:])
                connection.execute(
                    """
                    INSERT INTO recording_chunks(
                      recording_id,chunk_index,paragraph_start,paragraph_end,input_chars,input_sha256,status
                    ) VALUES(?,?,?,?,?,?,'pending')
                    """,
                    (str(row["recording_id"]), index, first, last, len(document), syncv2.sha256_text(document)),
                )
            connection.execute(
                "UPDATE recordings SET chunk_count=?,status='chunking',updated_at=? WHERE recording_id=?",
                (len(chunks), stamp, str(row["recording_id"])),
            )
            connection.commit()
        return len(chunks)
    finally:
        connection.close()


def chunk_payload(config: dict[str, Any], row: sqlite3.Row, chunk_row: sqlite3.Row) -> dict[str, Any]:
    _path, transcript = transcript_payload(str(row["task_uuid"]))
    paragraphs = paragraph_documents(transcript)
    chunks = split_chunks(paragraphs, int(config["dingtalk_minutes_memory"]["chunk_chars"]))
    chunk_index = int(chunk_row["chunk_index"])
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise MinutesMemoryError("chunk_state_mismatch", "stored chunk index is outside the complete transcript")
    selected = chunks[chunk_index]
    document = syncv2.canonical_json(selected)
    if (
        syncv2.sha256_text(document) != str(chunk_row["input_sha256"])
        or len(document) != int(chunk_row["input_chars"])
        or int(selected[0]["token"][1:]) != int(chunk_row["paragraph_start"])
        or int(selected[-1]["token"][1:]) != int(chunk_row["paragraph_end"])
    ):
        raise MinutesMemoryError("chunk_hash_changed", "transcript chunk changed after it was queued")
    return {
        "recording_ref": str(row["recording_id"]),
        "title": str(row["title"]),
        "observed_at": str(row["start_time"]),
        "chunk_index": int(chunk_row["chunk_index"]),
        "paragraphs": selected,
    }


def invoke_codex(
    config: dict[str, Any],
    *,
    prompt_path: Path,
    schema_path: Path,
    payload_tag: str,
    payload: dict[str, Any],
    effort: str,
    run_dir: Path,
    shared_ai_lock_fd: Optional[int] = None,
    validate: Any = None,
) -> Any:
    minutes = config["dingtalk_minutes_memory"]
    # No codex precondition: model_router calls MiMo and Kimi directly, so a
    # missing or hung codex only removes the GPT fallback.
    for path in (prompt_path, schema_path):
        if not path.is_file():
            raise MinutesMemoryError("codex_contract_missing", "minutes prompt or schema is missing")
    syncv2.ensure_private_dir(run_dir)
    prompt = (
        prompt_path.read_text(encoding="utf-8").rstrip()
        + f"\n\n<{payload_tag}>\n"
        + syncv2.canonical_json(payload)
        + f"\n</{payload_tag}>\n"
        + memory.mimo_schema_prompt(schema_path)
    )
    import model_router

    # The caller's own checks decide whether an answer is usable, so a reply
    # that fails them moves the chain on instead of failing the recording
    # (2020-01-02). Without a caller check, valid JSON is the bar.
    def accept(text: str) -> Any:
        payload = memory.parse_model_json(text)
        return validate(payload) if validate is not None else payload

    def run_model() -> Any:
        return model_router.complete(
            config, prompt=prompt, purpose="minutes_semantic",
            timeout=int(minutes["codex_timeout_seconds"]), workdir=run_dir,
            reasoning_effort=str(effort or "high"), validate=accept,
            request_context={"scope":"minutes-" + payload_tag},
        ).value

    try:
        if shared_ai_lock_fd is None:
            with syncv2.file_lock(syncv2.AI_LOCK):
                reply = run_model()
        else:
            try:
                inherited = os.fstat(int(shared_ai_lock_fd))
                current = syncv2.AI_LOCK.stat()
            except (OSError, TypeError, ValueError) as exc:
                raise MinutesMemoryError(
                    "shared_ai_lock_fd_invalid", "inherited shared AI lock handle is unavailable"
                ) from exc
            if (
                not stat.S_ISREG(inherited.st_mode)
                or (inherited.st_dev, inherited.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise MinutesMemoryError(
                    "shared_ai_lock_fd_invalid", "inherited shared AI lock handle does not match"
                )
            # A matching file descriptor alone is not proof that the parent already
            # owns the lock.  Probe through a fresh open-file description first: a
            # genuine inherited flock must block the probe, while an unlocked or
            # forged descriptor would let it acquire the slot.
            with syncv2.AI_LOCK.open("a+") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                    raise MinutesMemoryError(
                        "shared_ai_lock_fd_unheld", "inherited shared AI lock handle is not held"
                    )
            try:
                fcntl.flock(int(shared_ai_lock_fd), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                raise MinutesMemoryError(
                    "shared_ai_lock_fd_unheld", "inherited shared AI lock handle is not held"
                ) from exc
            reply = run_model()
    except syncv2.SyncV2Error as exc:
        raise MinutesMemoryError("chat_ai_priority", "chat semantic work currently owns the shared AI slot") from exc
    except model_router.RouterExhausted as exc:
        raise MinutesMemoryError(model_router.legacy_error_code(exc), f"minutes semantic call failed: {exc}") from exc
    return reply


def validate_chunk_result(payload: Any, row: sqlite3.Row, chunk_row: sqlite3.Row, source_tokens: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MinutesMemoryError("chunk_result_shape", "chunk result is not an object")
    if payload.get("recording_ref") != str(row["recording_id"]) or payload.get("chunk_index") != int(chunk_row["chunk_index"]):
        raise MinutesMemoryError("chunk_result_identity", "chunk result identity does not match")
    raw_summary = payload.get("chunk_summary")
    if not isinstance(raw_summary, dict):
        raise MinutesMemoryError("chunk_result_shape", "chunk full-transcript summary is missing")
    summary_tokens = [str(token) for token in raw_summary.get("source_tokens") or []]
    if len(summary_tokens) != len(set(summary_tokens)) or set(summary_tokens) != source_tokens:
        raise MinutesMemoryError("chunk_result_coverage", "chunk summary does not cover every source paragraph")
    chunk_summary = {
        "summary": memory.sanitize_note_text(raw_summary.get("summary"), 1800),
        "evidence_boundary": memory.sanitize_note_text(raw_summary.get("evidence_boundary"), 1200),
        "source_tokens": summary_tokens,
    }
    if not chunk_summary["summary"] or not chunk_summary["evidence_boundary"]:
        raise MinutesMemoryError("chunk_result_empty", "chunk full-transcript summary is empty")
    raw_points = payload.get("business_points")
    if not isinstance(raw_points, list) or len(raw_points) > 40:
        raise MinutesMemoryError("chunk_result_shape", "chunk business points are invalid")
    allowed_categories = {"business_cognition", "internal_material", "external_communication"}
    allowed_kinds = {"fact", "judgment", "decision", "status", "open_question"}
    points = []
    seen: set[str] = set()
    for raw in raw_points:
        if not isinstance(raw, dict):
            raise MinutesMemoryError("chunk_result_shape", "chunk point is invalid")
        tokens = [str(token) for token in raw.get("source_tokens") or []]
        if not tokens or not set(tokens).issubset(source_tokens):
            raise MinutesMemoryError("chunk_result_source", "chunk result cites an unknown paragraph")
        category = str(raw.get("category") or "")
        kind = str(raw.get("memory_kind") or "")
        if category not in allowed_categories or kind not in allowed_kinds:
            raise MinutesMemoryError("chunk_result_enum", "chunk result contains an invalid business type")
        point = {
            "dedupe_key": memory.sanitize_note_text(raw.get("dedupe_key"), 120),
            "title": memory.sanitize_note_text(raw.get("title"), 100),
            "category": category,
            "memory_kind": kind,
            "summary": memory.sanitize_note_text(raw.get("summary"), 1800),
            "evidence_boundary": memory.sanitize_note_text(raw.get("evidence_boundary"), 1200),
            "source_tokens": tokens,
        }
        if not all(point[key] for key in ("dedupe_key", "title", "summary", "evidence_boundary")):
            raise MinutesMemoryError("chunk_result_empty", "chunk result contains an empty field")
        if point["dedupe_key"] in seen:
            continue
        seen.add(str(point["dedupe_key"]))
        points.append(point)
    no_change = payload.get("no_business_change") is True
    if no_change != (len(points) == 0):
        raise MinutesMemoryError("chunk_result_shape", "chunk no-business flag is inconsistent")
    return {
        "recording_ref": str(row["recording_id"]),
        "chunk_index": int(chunk_row["chunk_index"]),
        "chunk_summary": chunk_summary,
        "business_points": points,
        "no_business_change": no_change,
    }


def validate_final_result(config: dict[str, Any], payload: Any, row: sqlite3.Row) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("recording_ref") != str(row["recording_id"]):
        raise MinutesMemoryError("final_result_identity", "final result identity does not match")
    source_summary = payload.get("source_summary")
    if not isinstance(source_summary, dict):
        raise MinutesMemoryError("final_result_shape", "source summary is required")
    source_summary = {
        "title": memory.sanitize_note_text(source_summary.get("title"), 100),
        "summary": memory.sanitize_note_text(source_summary.get("summary"), 1800),
        "evidence_boundary": memory.sanitize_note_text(source_summary.get("evidence_boundary"), 1200),
    }
    if not all(source_summary.values()):
        raise MinutesMemoryError("final_result_empty", "source summary contains an empty field")
    records = memory.memory_records(memory.runtime_paths(config)["memory_root"])
    by_id = {str(record["memory_id"]): record for record in records}
    allowed_categories = {"business_cognition", "internal_material", "external_communication"}
    allowed_kinds = {"fact", "judgment", "decision", "status", "open_question"}
    candidates = []
    seen: set[str] = set()
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) > 20:
        raise MinutesMemoryError("final_result_shape", "final candidates are invalid")
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            raise MinutesMemoryError("final_result_shape", "final candidate is invalid")
        action = str(raw.get("action") or "")
        category = str(raw.get("category") or "")
        kind = str(raw.get("memory_kind") or "")
        related = [str(value) for value in raw.get("related_memory_ids") or []]
        if action not in {"annotate_page", "link_existing", "conflict"} or category not in allowed_categories or kind not in allowed_kinds:
            raise MinutesMemoryError("final_result_enum", "final candidate contains an invalid type")
        if any(value not in by_id for value in related):
            raise MinutesMemoryError("final_result_reference", "final candidate references missing memory")
        if action == "link_existing":
            if len(related) != 1:
                raise MinutesMemoryError("final_result_reference", "link_existing requires one target")
            target = by_id[related[0]]
            if target["status"] != "active" or target["fact_status"] != "confirmed":
                raise MinutesMemoryError("final_result_reference", "link_existing target is not confirmed and active")
        conflict_reason = memory.sanitize_note_text(raw.get("conflict_reason"), 800) if raw.get("conflict_reason") else None
        if action == "conflict" and not conflict_reason:
            raise MinutesMemoryError("final_result_conflict", "conflict candidate lacks a reason")
        if action != "conflict" and conflict_reason:
            raise MinutesMemoryError("final_result_conflict", "non-conflict candidate has a conflict reason")
        candidate = {
            "action": action,
            "dedupe_key": memory.sanitize_note_text(raw.get("dedupe_key"), 120),
            "title": memory.sanitize_note_text(raw.get("title"), 100),
            "category": category,
            "memory_kind": kind,
            "summary": memory.sanitize_note_text(raw.get("summary"), 1800),
            "evidence_boundary": memory.sanitize_note_text(raw.get("evidence_boundary"), 1200),
            "related_memory_ids": list(dict.fromkeys(related)),
            "conflict_reason": conflict_reason,
        }
        if not all(candidate[key] for key in ("dedupe_key", "title", "summary", "evidence_boundary")):
            raise MinutesMemoryError("final_result_empty", "final candidate contains an empty field")
        if candidate["dedupe_key"] in seen:
            continue
        seen.add(str(candidate["dedupe_key"]))
        candidates.append(candidate)
    no_change = payload.get("no_business_change") is True
    if no_change != (len(candidates) == 0):
        raise MinutesMemoryError("final_result_shape", "final no-business flag is inconsistent")
    raw_relations = payload.get("claim_relations") or []
    try:
        relations = reconciliation.validate_relations(
            raw_relations,
            allowed_handles=reconciliation.open_claim_handles() if raw_relations else set(),
            allowed_tokens={"R001"},
        )
    except reconciliation.ReconciliationError as exc:
        raise MinutesMemoryError("final_claim_relation", exc.message) from exc
    return {
        "recording_ref": str(row["recording_id"]),
        "source_summary": source_summary,
        "candidates": candidates,
        "claim_relations": relations,
        "no_business_change": no_change,
    }


def _comparison_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 12:
        raise MinutesMemoryError("summary_comparison_shape", f"summary comparison {field} is invalid")
    result = [memory.sanitize_note_text(item, 240) for item in value]
    if any(not item for item in result):
        raise MinutesMemoryError("summary_comparison_empty", f"summary comparison {field} contains an empty item")
    return result


def validate_summary_comparison(payload: Any, row: sqlite3.Row, native: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("recording_ref") != str(row["recording_id"]):
        raise MinutesMemoryError("summary_comparison_identity", "summary comparison identity does not match")
    available = payload.get("native_summary_available") is True
    if available != bool(native.get("available")):
        raise MinutesMemoryError("summary_comparison_source", "summary comparison native-summary state changed")
    relation = str(payload.get("overall_relation") or "")
    if relation not in {"matched", "partially_matched", "discrepant", "native_missing"}:
        raise MinutesMemoryError("summary_comparison_enum", "summary comparison relation is invalid")
    agreements = _comparison_strings(payload.get("agreements"), "agreements")
    independent_only = _comparison_strings(payload.get("independent_only"), "independent_only")
    native_only = _comparison_strings(payload.get("native_only"), "native_only")
    raw_conflicts = payload.get("conflicts")
    if not isinstance(raw_conflicts, list) or len(raw_conflicts) > 12:
        raise MinutesMemoryError("summary_comparison_shape", "summary comparison conflicts are invalid")
    conflicts = []
    for raw in raw_conflicts:
        if not isinstance(raw, dict):
            raise MinutesMemoryError("summary_comparison_shape", "summary comparison conflict is invalid")
        conflict = {
            "topic": memory.sanitize_note_text(raw.get("topic"), 120),
            "independent_view": memory.sanitize_note_text(raw.get("independent_view"), 360),
            "native_view": memory.sanitize_note_text(raw.get("native_view"), 360),
            "handling": memory.sanitize_note_text(raw.get("handling"), 240),
        }
        if not all(conflict.values()) or "不晋升" not in conflict["handling"]:
            raise MinutesMemoryError("summary_comparison_policy", "summary comparison conflict lacks the conservative handling rule")
        conflicts.append(conflict)
    publication_note = memory.sanitize_note_text(payload.get("publication_note"), 480)
    evidence_boundary = memory.sanitize_note_text(payload.get("evidence_boundary"), 800)
    if not publication_note or not evidence_boundary:
        raise MinutesMemoryError("summary_comparison_empty", "summary comparison conclusion is empty")
    differences = bool(independent_only or native_only or conflicts)
    if not available:
        if relation != "native_missing" or agreements or differences:
            raise MinutesMemoryError("summary_comparison_missing_native", "missing native summary comparison is inconsistent")
    elif relation == "native_missing":
        raise MinutesMemoryError("summary_comparison_missing_native", "available native summary was marked missing")
    elif conflicts:
        relation = "discrepant"
    elif differences:
        relation = "partially_matched"
    else:
        relation = "matched"
    return {
        "recording_ref": str(row["recording_id"]),
        "native_summary_available": available,
        "overall_relation": relation,
        "agreements": agreements,
        "independent_only": independent_only,
        "native_only": native_only,
        "conflicts": conflicts,
        "publication_note": publication_note,
        "evidence_boundary": evidence_boundary,
    }


def missing_native_comparison(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "recording_ref": str(row["recording_id"]),
        "native_summary_available": False,
        "overall_relation": "native_missing",
        "agreements": [],
        "independent_only": [],
        "native_only": [],
        "conflicts": [],
        "publication_note": "已依据完整转写生成独立纪要；钉钉未提供可用纪要，因此本次没有原生纪要可对照。",
        "evidence_boundary": "本阶段完整读取钉钉转写文本，但未复听音频；独立纪要仍属于未经二次核对的内部来源摘要。",
    }


def comparison_section(comparison: dict[str, Any]) -> str:
    relation_labels = {
        "matched": "基本一致",
        "partially_matched": "部分一致",
        "discrepant": "存在明显差异",
        "native_missing": "无钉钉纪要可对照",
    }
    lines = [
        f"- **总体结论**：{relation_labels[comparison['overall_relation']]}。",
        f"- **阅读说明**：{comparison['publication_note']}",
    ]
    groups = (
        ("一致项", comparison["agreements"]),
        ("全文独立纪要独有", comparison["independent_only"]),
        ("钉钉纪要独有（未晋升）", comparison["native_only"]),
    )
    for label, values in groups:
        if values:
            lines.append(f"- **{label}**：" + "；".join(values))
    for conflict in comparison["conflicts"]:
        lines.append(
            "- **冲突保留**：{}｜全文纪要：{}｜钉钉纪要：{}｜{}".format(
                conflict["topic"], conflict["independent_view"], conflict["native_view"], conflict["handling"]
            )
        )
    return "\n".join(lines)


def comparison_source_check(comparison: dict[str, Any]) -> str:
    # Conversation source notes are active only when the local source package is matched.
    # Native-summary disagreement is recorded in the dedicated comparison section instead.
    return "matched"


def source_note_final_change(
    record: dict[str, Any],
    row: sqlite3.Row,
    summary: dict[str, str],
    comparison: dict[str, Any],
) -> str:
    stamp = syncv2.now_iso()
    marker = f"<!-- dingtalk-minutes:{row['recording_id']} -->"
    boundary = summary["evidence_boundary"] + " " + comparison["evidence_boundary"]
    body = (
        f"{marker}\n\n## 对话摘要\n\n{summary['summary']}\n\n"
        f"## 钉钉纪要对照\n\n{comparison_section(comparison)}\n\n"
        f"## 证据边界\n\n{boundary}\n\n"
        "## 来源\n\n"
        f"- `{row['source_ref']}`；原始任务标识、音频和全文只保存在本机 `.nosync` 来源库。\n\n"
        "## 更新记录\n\n"
        f"- {stamp}：先完整读取钉钉转写并独立生成纪要，再与钉钉原生纪要对照；二次核对状态独立记录，未核对前不升级为确认事实。\n"
    )
    return memory.rebuild_existing_note(
        str(record["raw_frontmatter"]),
        body,
        {
            "status": "active",
            "evidence_level": "internal_source",
            "source_check": comparison_source_check(comparison),
            "processed_at": stamp,
            "reviewed_at": None,
        },
    )


def source_note_publish_change(
    config: dict[str, Any],
    records: list[dict[str, Any]],
    row: sqlite3.Row,
    summary: dict[str, str],
    comparison: dict[str, Any],
) -> tuple[Path, str, str]:
    existing = [
        record for record in records
        if str(record.get("memory_id") or "") == str(row["source_memory_id"] or "")
    ]
    if not existing:
        existing = [
            record for record in records
            if record["source_ref"] == str(row["source_ref"])
            and str(record.get("memory_scope") or "") == "conversation_source"
        ]
    if len(existing) > 1:
        raise MinutesMemoryError("source_note_collision", "multiple source notes share one recording reference")
    if existing:
        record = existing[0]
        return (
            Path(record["path"]),
            source_note_final_change(record, row, summary, comparison),
            str(record["memory_id"]),
        )
    memory_root = memory.runtime_paths(config)["memory_root"]
    reserved: set[str] = set()
    date_key = str(row["start_time"])[:10].replace("-", "")
    memory_id = memory.next_memory_id(
        records,
        date_key,
        reserved,
        vault_root=memory_root.parent,
    )
    filename = f"{date_key}-钉钉听记-{memory.safe_filename(str(row['title']), 52)}.md"
    path = memory_root / "04_对话来源摘要" / filename
    if path.exists():
        path = memory_root / "04_对话来源摘要" / (
            f"{date_key}-钉钉听记-{memory.safe_filename(str(row['title']), 42)}-{str(row['recording_id'])[:8]}.md"
        )
    if path.exists():
        raise MinutesMemoryError("source_note_collision", "recording source note path already exists")
    stamp = syncv2.now_iso()
    body = (
        f"<!-- dingtalk-minutes:{row['recording_id']} -->\n\n"
        f"## 对话摘要\n\n{summary['summary']}\n\n"
        f"## 钉钉纪要对照\n\n{comparison_section(comparison)}\n\n"
        f"## 证据边界\n\n{summary['evidence_boundary']} {comparison['evidence_boundary']}\n\n"
        "## 来源\n\n"
        f"- `{row['source_ref']}`；原始任务标识、音频和全文只保存在本机 `.nosync` 来源库。\n\n"
        "## 更新记录\n\n"
        f"- {stamp}：先完整读取转写并独立生成纪要，再完成钉钉纪要对照后发布。\n"
    )
    frontmatter = [
        ("memory_id", memory_id),
        ("category", "conversation_summary"),
        ("memory_scope", "conversation_source"),
        ("memory_kind", "conversation_summary"),
        ("origin_type", "internal_communication"),
        ("status", "active"),
        ("evidence_level", "internal_source"),
        ("sensitivity", "confidential"),
        ("source_system", "dingtalk"),
        ("source_check", comparison_source_check(comparison)),
        ("fact_status", "not_applicable"),
        ("source_ref", str(row["source_ref"])),
        ("observed_at", str(row["start_time"])),
        ("processed_at", stamp),
        ("reviewed_at", None),
        ("valid_until", None),
        ("supersedes", []),
        ("content_hash", ""),
        ("applies_to", ["project:business-energy"]),
    ]
    return path, memory.render_note(frontmatter, body), memory_id


def minutes_pending_change(
    config: dict[str, Any],
    memory_root: Path,
    records: list[dict[str, Any]],
    reserved: set[str],
    row: sqlite3.Row,
    candidate: dict[str, Any],
    *,
    review_completed: bool = False,
    claim_id: Optional[str] = None,
    page_overrides: Optional[dict[Path, str]] = None,
) -> tuple[Path, str, str]:
    del memory_root, records, reserved
    conflict = candidate["action"] == "conflict"
    review_boundary = (
        " 本候选已根据二次核对稿生成；核对提高转写可靠性，但业务事实仍需责任人确认。"
        if review_completed
        else " 本候选基于钉钉原转写生成，二次核对尚未完成；不得直接视为确认事实。"
    )
    enriched = {
        **candidate,
        "evidence_boundary": f"{candidate['evidence_boundary']}{review_boundary}",
        "evidence_state": "conflicting" if conflict else "pending",
    }
    try:
        path, text, route = business_router.route_claim_change(
            config,
            enriched,
            source_system="dingtalk",
            source_ref=str(row["source_ref"]),
            observed_at=str(row["start_time"]),
            claim_id=claim_id,
            page_overrides=page_overrides,
        )
    except business_router.RoutingError as exc:
        quarantine = business_router.quarantine_candidate(
            config,
            {**enriched, "source_system": "dingtalk", "source_ref": str(row["source_ref"])},
            exc.code,
        )
        raise MinutesMemoryError(
            "business_routing_failed",
            f"minutes candidate was quarantined after routing failure: {quarantine.name}",
        ) from exc
    return path, text, route.claim_id


def apply_final_result(
    config: dict[str, Any],
    row: sqlite3.Row,
    proposal: dict[str, Any],
    comparison: dict[str, Any],
    native_sha256: Optional[str],
    *,
    publish_candidates: bool = True,
) -> dict[str, Any]:
    memory_root = memory.runtime_paths(config)["memory_root"]
    lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=True)
    try:
        records = memory.memory_records(memory_root)
        by_id = {str(record["memory_id"]): record for record in records}
        source_path, source_text, source_memory_id = source_note_publish_change(
            config, records, row, proposal["source_summary"], comparison
        )
        changes: dict[Path, str] = {source_path: source_text}
        reserved: set[str] = set()
        linked = 0
        for candidate in proposal["candidates"] if publish_candidates else []:
            if candidate["action"] == "link_existing":
                target = by_id[candidate["related_memory_ids"][0]]
                marker = f"<!-- {row['source_ref']}:{syncv2.sha256_text(candidate['dedupe_key'])[:16]} -->"
                entry = f"{marker}\n- {syncv2.now_iso()}｜{candidate['summary']}｜来源：`{row['source_ref']}`。未改变原条目的事实状态。"
                body = memory.append_section_entry(str(target["body"]), "钉钉听记来源补充", entry, marker)
                changes[Path(target["path"])] = memory.rebuild_existing_note(str(target["raw_frontmatter"]), body, {})
                linked += 1
            else:
                path, text, memory_id = minutes_pending_change(
                    config, memory_root, records, reserved, row, candidate, page_overrides=changes
                )
                changes[path] = text
        apply_result = memory.apply_changes(
            config,
            f"minutes-final-{row['recording_id']}",
            changes,
            lock_held=True,
        )
    finally:
        lock.close()
    connection = syncv2.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        created_decisions = 0
        updated_decisions = 0
        final_status = "no_business_change" if proposal["no_business_change"] else "completed"
        stamp = syncv2.now_iso()
        completed_at = (
            str(row["completed_at"])
            if not publish_candidates and row["completed_at"]
            else stamp
        )
        independent_sha256 = syncv2.sha256_text(syncv2.canonical_json(proposal["source_summary"]))
        comparison_sha256 = syncv2.sha256_text(syncv2.canonical_json(comparison))
        connection.execute(
            """
            UPDATE recordings SET source_memory_id=?,source_memory_path=?,status=?,
              native_summary_sha256=?,independent_summary_sha256=?,summary_comparison_sha256=?,
              summary_comparison_status='compared',summary_comparison_policy_version=?,
              last_error_code=NULL,updated_at=?,completed_at=? WHERE recording_id=?
            """,
            (
                source_memory_id,
                str(source_path),
                final_status,
                native_sha256,
                independent_sha256,
                comparison_sha256,
                int(config["dingtalk_minutes_memory"]["summary_comparison_policy_version"]),
                stamp,
                completed_at,
                str(row["recording_id"]),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    safe_payload = {
        "recording_id": str(row["recording_id"]),
        "title": str(row["title"]),
        "source_memory_id": source_memory_id,
        "candidates": len(proposal["candidates"]) if publish_candidates else 0,
        "linked_existing": linked,
        "decisions_created": created_decisions,
        "decisions_updated": updated_decisions,
        "conflicts": sum(1 for candidate in proposal["candidates"] if candidate["action"] == "conflict") if publish_candidates else 0,
        "summary_comparison": comparison["overall_relation"],
        "summary_comparison_backfill": not publish_candidates,
    }
    return {**apply_result, **safe_payload}


def next_recording() -> Optional[sqlite3.Row]:
    connection = syncv2.connect()
    try:
        return connection.execute(
            """
            SELECT * FROM recordings
            WHERE status IN (
              'discovered','source_written','chunking','ready_finalize','independent_ready','comparison_refresh_pending'
            )
            ORDER BY start_time,created_at LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()


def process_chunk(config: dict[str, Any], row: sqlite3.Row, chunk_row: sqlite3.Row) -> dict[str, Any]:
    payload = chunk_payload(config, row, chunk_row)
    run_dir = syncv2.RUNS_DIR / str(row["recording_id"]) / f"chunk-{int(chunk_row['chunk_index']):03d}"
    connection = syncv2.connect()
    try:
        connection.execute(
            "UPDATE recording_chunks SET status='running',started_at=?,last_error_code=NULL WHERE recording_id=? AND chunk_index=?",
            (syncv2.now_iso(), str(row["recording_id"]), int(chunk_row["chunk_index"])),
        )
        connection.commit()
    finally:
        connection.close()
    source_tokens = {item["token"] for item in payload["paragraphs"]}
    raw = invoke_codex(
        config,
        prompt_path=CHUNK_PROMPT,
        schema_path=CHUNK_SCHEMA,
        payload_tag="recording_chunk",
        payload=payload,
        effort=str(config["dingtalk_minutes_memory"]["chunk_reasoning_effort"]),
        run_dir=run_dir,
        validate=lambda parsed: (validate_chunk_result(parsed, row, chunk_row, source_tokens), parsed)[1],
    )
    connection = syncv2.connect()
    try:
        connection.execute(
            "UPDATE recordings SET model_calls=model_calls+1,updated_at=? WHERE recording_id=?",
            (syncv2.now_iso(), str(row["recording_id"])),
        )
        connection.commit()
    finally:
        connection.close()
    validated = validate_chunk_result(raw, row, chunk_row, source_tokens)
    result_json = syncv2.canonical_json(validated)
    connection = syncv2.connect()
    try:
        connection.execute(
            """
            UPDATE recording_chunks SET status='complete',result_json=?,result_sha256=?,completed_at=?,last_error_code=NULL
            WHERE recording_id=? AND chunk_index=?
            """,
            (result_json, syncv2.sha256_text(result_json), syncv2.now_iso(), str(row["recording_id"]), int(chunk_row["chunk_index"])),
        )
        connection.commit()
    finally:
        connection.close()
    return {"kind": "chunk", "chunk_index": int(chunk_row["chunk_index"]), "business_points": len(validated["business_points"])}


def independent_proposal(
    config: dict[str, Any], row: sqlite3.Row, *, allow_legacy: bool = False
) -> Optional[dict[str, Any]]:
    run_dir = syncv2.RUNS_DIR / str(row["recording_id"]) / "final"
    proposal_path = run_dir / "proposal.json"
    metadata_path = run_dir / "independent-summary-meta.json"
    if not proposal_path.is_file():
        return None
    try:
        proposal = validate_final_result(config, json.loads(proposal_path.read_text(encoding="utf-8")), row)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else None
    except (OSError, json.JSONDecodeError, MinutesMemoryError):
        # A previously valid proposal can become stale after the governed
        # action schema or the referenced business-memory set changes. Treat
        # it as a cache miss so normal semantic processing project_thetates it
        # against the current routing contract instead of leaving the
        # recording permanently failed on historical model output.
        return None
    expected_sha = syncv2.sha256_text(syncv2.canonical_json(proposal))
    contract = {
        "transcript_sha256": str(row["transcript_sha256"]),
        "proposal_sha256": expected_sha,
        "prompt_sha256": syncv2.sha256_file(FINAL_PROMPT),
        "schema_sha256": syncv2.sha256_file(FINAL_SCHEMA),
    }
    if metadata is not None:
        # Transcript, validated proposal and schema are semantic integrity
        # boundaries.  A prompt revision is also required during normal
        # processing, but an explicitly requested comparison-only backfill may
        # reuse a previously accepted independent summary.  This is the narrow
        # path used by the bounded rollout backfill; it never republishes
        # candidates and therefore must not pay for another summary call.
        if not _artifact_contract_matches(
            metadata,
            contract,
            allow_prior_prompt=allow_legacy,
        ):
            return None
    elif not allow_legacy:
        return None
    else:
        memory.atomic_write(
            metadata_path,
            json.dumps(
                {
                    "schema_version": 1,
                    **contract,
                    "adopted_legacy_proposal": True,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    return proposal


def process_independent_final(config: dict[str, Any], row: sqlite3.Row) -> dict[str, Any]:
    connection = syncv2.connect()
    try:
        chunks = connection.execute(
            "SELECT chunk_index,result_json FROM recording_chunks WHERE recording_id=? ORDER BY chunk_index",
            (str(row["recording_id"]),),
        ).fetchall()
    finally:
        connection.close()
    if not chunks or any(not chunk["result_json"] for chunk in chunks):
        raise MinutesMemoryError("chunks_incomplete", "recording chunks are not complete")
    chunk_results = [json.loads(str(chunk["result_json"])) for chunk in chunks]
    try:
        open_claims = reconciliation.open_claim_context(
            "\n".join(
                str((item.get("chunk_summary") or {}).get("summary") or "")
                for item in chunk_results
            ),
            limit=24,
        )
    except reconciliation.ReconciliationError:
        open_claims = []
    payload = {
        "recording_ref": str(row["recording_id"]),
        "source_token": "R001",
        "title": str(row["title"]),
        "observed_at": str(row["start_time"]),
        "chunk_results": chunk_results,
        "current_memory": memory.model_memory_context(memory.runtime_paths(config)["memory_root"]),
        "open_claims": open_claims,
    }
    run_dir = syncv2.RUNS_DIR / str(row["recording_id"]) / "final"
    proposal_path = run_dir / "proposal.json"
    metadata_path = run_dir / "independent-summary-meta.json"
    proposal = None if str(row["last_error_code"] or "") == "independent_summary_changed" else independent_proposal(config, row)
    model_called = proposal is None
    if proposal is None:
        raw = invoke_codex(
            config,
            prompt_path=FINAL_PROMPT,
            schema_path=FINAL_SCHEMA,
            payload_tag="recording_summary_data",
            payload=payload,
            effort=str(config["dingtalk_minutes_memory"]["final_reasoning_effort"]),
            run_dir=run_dir,
            validate=lambda parsed: (validate_final_result(config, parsed, row), parsed)[1],
        )
        connection = syncv2.connect()
        try:
            connection.execute(
                "UPDATE recordings SET model_calls=model_calls+1,updated_at=? WHERE recording_id=?",
                (syncv2.now_iso(), str(row["recording_id"])),
            )
            connection.commit()
        finally:
            connection.close()
        memory.atomic_write(
            run_dir / "model-result.unvalidated.json",
            json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        proposal = validate_final_result(config, raw, row)
        memory.atomic_write(proposal_path, json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        proposal_sha256 = syncv2.sha256_text(syncv2.canonical_json(proposal))
        memory.atomic_write(
            metadata_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "transcript_sha256": str(row["transcript_sha256"]),
                    "proposal_sha256": proposal_sha256,
                    "prompt_sha256": syncv2.sha256_file(FINAL_PROMPT),
                    "schema_sha256": syncv2.sha256_file(FINAL_SCHEMA),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        connection = syncv2.connect()
        try:
            connection.execute(
                """
                UPDATE recordings SET status='independent_ready',
                  independent_summary_sha256=?,summary_comparison_status='pending',last_error_code=NULL,updated_at=?
                WHERE recording_id=?
                """,
                (
                    syncv2.sha256_text(syncv2.canonical_json(proposal["source_summary"])),
                    syncv2.now_iso(),
                    str(row["recording_id"]),
                ),
            )
            connection.commit()
        finally:
            connection.close()
    else:
        connection = syncv2.connect()
        try:
            connection.execute(
                """
                UPDATE recordings SET status='independent_ready',independent_summary_sha256=?,
                  summary_comparison_status='pending',last_error_code=NULL,updated_at=? WHERE recording_id=?
                """,
                (
                    syncv2.sha256_text(syncv2.canonical_json(proposal["source_summary"])),
                    syncv2.now_iso(),
                    str(row["recording_id"]),
                ),
            )
            connection.commit()
        finally:
            connection.close()
    return {"kind": "independent_summary", "model_called": model_called}


def process_summary_comparison(
    config: dict[str, Any],
    row: sqlite3.Row,
    *,
    publish_candidates: bool = True,
    allow_legacy_proposal: bool = False,
) -> dict[str, Any]:
    proposal = independent_proposal(config, row, allow_legacy=allow_legacy_proposal)
    if proposal is None:
        raise MinutesMemoryError("independent_summary_missing", "independent full-transcript summary is unavailable")
    native = native_summary_payload(str(row["task_uuid"]))
    independent_sha256 = syncv2.sha256_text(syncv2.canonical_json(proposal["source_summary"]))
    run_dir = syncv2.RUNS_DIR / str(row["recording_id"]) / "summary-compare"
    comparison_path = run_dir / "summary-comparison.json"
    metadata_path = run_dir / "summary-comparison-meta.json"
    expected_meta = {
        "schema_version": 1,
        "transcript_sha256": str(row["transcript_sha256"]),
        "native_summary_sha256": native.get("sha256"),
        "independent_summary_sha256": independent_sha256,
        "policy_version": int(config["dingtalk_minutes_memory"]["summary_comparison_policy_version"]),
        "prompt_sha256": syncv2.sha256_file(SUMMARY_COMPARE_PROMPT),
        "schema_sha256": syncv2.sha256_file(SUMMARY_COMPARE_SCHEMA),
    }
    comparison = None
    if comparison_path.is_file() and metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            raw_comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
            raw_sha256 = syncv2.sha256_text(syncv2.canonical_json(raw_comparison))
            if (
                _artifact_contract_matches(
                    metadata,
                    expected_meta,
                    allow_prior_prompt=allow_legacy_proposal,
                )
                and metadata.get("comparison_sha256") == raw_sha256
            ):
                comparison = validate_summary_comparison(raw_comparison, row, native)
        except (OSError, json.JSONDecodeError, MinutesMemoryError):
            comparison = None
    model_called = False
    if comparison is None:
        connection = syncv2.connect()
        try:
            connection.execute(
                "UPDATE recordings SET summary_comparison_status='running',updated_at=? WHERE recording_id=?",
                (syncv2.now_iso(), str(row["recording_id"])),
            )
            connection.commit()
        finally:
            connection.close()
        if native["available"]:
            raw = invoke_codex(
                config,
                prompt_path=SUMMARY_COMPARE_PROMPT,
                schema_path=SUMMARY_COMPARE_SCHEMA,
                payload_tag="summary_comparison_data",
                payload={
                    "recording_ref": str(row["recording_id"]),
                    "independent_summary": proposal["source_summary"],
                    "native_summary_available": True,
                    "native_summary": native["text"],
                },
                effort=str(config["dingtalk_minutes_memory"]["summary_comparison_reasoning_effort"]),
                run_dir=run_dir,
                validate=lambda parsed: (validate_summary_comparison(parsed, row, native), parsed)[1],
            )
            connection = syncv2.connect()
            try:
                connection.execute(
                    "UPDATE recordings SET model_calls=model_calls+1,updated_at=? WHERE recording_id=?",
                    (syncv2.now_iso(), str(row["recording_id"])),
                )
                connection.commit()
            finally:
                connection.close()
            memory.atomic_write(
                run_dir / "model-result.unvalidated.json",
                json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            comparison = validate_summary_comparison(raw, row, native)
            model_called = True
        else:
            comparison = validate_summary_comparison(missing_native_comparison(row), row, native)
        memory.atomic_write(
            comparison_path,
            json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        comparison_sha256 = syncv2.sha256_text(syncv2.canonical_json(comparison))
        memory.atomic_write(
            metadata_path,
            json.dumps(
                {**expected_meta, "comparison_sha256": comparison_sha256},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    comparison_sha256 = syncv2.sha256_text(syncv2.canonical_json(comparison))
    already_applied = (
        str(row["summary_comparison_status"] or "") == "compared"
        and str(row["independent_summary_sha256"] or "") == independent_sha256
        and row["native_summary_sha256"] == native.get("sha256")
        and str(row["summary_comparison_sha256"] or "") == comparison_sha256
        and bool(row["source_memory_path"])
        and Path(str(row["source_memory_path"])).is_file()
    )
    if already_applied:
        return {
            "kind": "summary_comparison",
            "status": "noop",
            "model_called": model_called,
            "recording_id": str(row["recording_id"]),
            "summary_comparison": comparison["overall_relation"],
            "summary_comparison_backfill": not publish_candidates,
        }
    result = apply_final_result(
        config,
        row,
        proposal,
        comparison,
        native.get("sha256"),
        publish_candidates=publish_candidates,
    )
    transcript_root = str(row["transcript_sha256"] or "")[:32]
    relation_receipt = reconciliation.submit_relations(
        config,
        list(proposal.get("claim_relations") or []),
        source_system="dingtalk_minutes",
        source_material={
            "R001": {
                "material": proposal["source_summary"]["summary"],
                "root_fingerprint": transcript_root,
                "provenance_family": f"dingtalk-recording:{transcript_root}",
                "independence_group": transcript_root,
                "directness": "indirect",
                "verification_method": "model_proposed",
            }
        },
        observed_at=str(row["start_time"]),
    )
    try:
        refresh_receipt: dict[str, Any] = reconciliation.refresh_index(
            config,
            run_id=f"minutes-refresh-{row['recording_id']}-{uuid.uuid4().hex[:8]}",
        )
    except (OSError, sqlite3.Error, reconciliation.ReconciliationError, business_router.RoutingError) as exc:
        refresh_receipt = {
            "status": "deferred",
            "error_code": getattr(exc, "code", type(exc).__name__),
        }
    return {
        "kind": "summary_comparison",
        "model_called": model_called,
        "claim_relations": relation_receipt,
        "reconciliation": refresh_receipt,
        **result,
    }


def mark_semantic_failure(row: sqlite3.Row, code: str) -> None:
    connection = syncv2.connect()
    try:
        connection.execute(
            "UPDATE recording_chunks SET status='failed',last_error_code=? WHERE recording_id=? AND status='running'",
            (code, str(row["recording_id"])),
        )
        connection.execute(
            """
            UPDATE recordings SET status='failed',last_error_code=?,
              summary_comparison_status=CASE
                WHEN summary_comparison_status='running' THEN 'failed'
                ELSE summary_comparison_status
              END,
              updated_at=? WHERE recording_id=?
            """,
            (code, syncv2.now_iso(), str(row["recording_id"])),
        )
        connection.commit()
    finally:
        connection.close()
    syncv2.set_component_health("recording_semantic", False, code)


def recover_interrupted_chunks() -> int:
    """Requeue chunks left running when their worker was terminated.

    The caller must hold ``SEMANTIC_LOCK`` so a live model worker can never be
    mistaken for abandoned work.
    """
    connection = syncv2.connect()
    try:
        rows = connection.execute(
            "SELECT DISTINCT recording_id FROM recording_chunks WHERE status='running'"
        ).fetchall()
        if not rows:
            return 0
        stamp = syncv2.now_iso()
        recording_ids = [str(row["recording_id"]) for row in rows]
        for recording_id in recording_ids:
            connection.execute(
                """
                UPDATE recording_chunks
                SET status='pending',started_at=NULL,last_error_code='interrupted_process_exit'
                WHERE recording_id=? AND status='running'
                """,
                (recording_id,),
            )
            connection.execute(
                """
                UPDATE recordings
                SET status='chunking',last_error_code='interrupted_process_exit',updated_at=?
                WHERE recording_id=?
                """,
                (stamp, recording_id),
            )
        connection.commit()
        return len(recording_ids)
    finally:
        connection.close()


def run_semantic(config: dict[str, Any]) -> dict[str, Any]:
    max_calls = int(config["dingtalk_minutes_memory"]["max_model_calls_per_run"])
    results: list[dict[str, Any]] = []
    row = next_recording()
    if row is None:
        return {"status": "noop", "model_calls": 0, "recording": None}
    with syncv2.file_lock(syncv2.SEMANTIC_LOCK):
        recovered_chunks = recover_interrupted_chunks()
        row = next_recording()
        if row is None:
            return {"status": "noop", "model_calls": 0, "recording": None, "recovered_chunks": recovered_chunks}
        try:
            ensure_chunks(config, row)
            for _call in range(max_calls):
                connection = syncv2.connect()
                try:
                    current = connection.execute(
                        "SELECT * FROM recordings WHERE recording_id=?", (str(row["recording_id"]),)
                    ).fetchone()
                    chunk_row = connection.execute(
                        """
                        SELECT * FROM recording_chunks WHERE recording_id=? AND status='pending'
                        ORDER BY chunk_index LIMIT 1
                        """,
                        (str(row["recording_id"]),),
                    ).fetchone()
                    incomplete = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM recording_chunks WHERE recording_id=? AND status!='complete'",
                            (str(row["recording_id"]),),
                        ).fetchone()[0]
                    )
                finally:
                    connection.close()
                if chunk_row is not None:
                    results.append(process_chunk(config, row, chunk_row))
                    continue
                if incomplete:
                    raise MinutesMemoryError("chunk_state_blocked", "a chunk is neither pending nor complete")
                if str(current["status"]) in {"independent_ready", "comparison_refresh_pending"}:
                    results.append(
                        process_summary_comparison(
                            config,
                            current,
                            publish_candidates=str(current["status"]) != "comparison_refresh_pending",
                        )
                    )
                    break
                results.append(process_independent_final(config, current))
                continue
            if results and results[-1]["kind"] == "chunk":
                connection = syncv2.connect()
                try:
                    remaining = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM recording_chunks WHERE recording_id=? AND status!='complete'",
                            (str(row["recording_id"]),),
                        ).fetchone()[0]
                    )
                    connection.execute(
                        "UPDATE recordings SET status=?,updated_at=? WHERE recording_id=?",
                        ("chunking" if remaining else "ready_finalize", syncv2.now_iso(), str(row["recording_id"])),
                    )
                    connection.commit()
                finally:
                    connection.close()
            syncv2.set_component_health("recording_semantic", True)
            actual_calls = sum(1 for result in results if result["kind"] == "chunk" or result.get("model_called"))
            return {"status": "success", "recording": str(row["recording_id"]), "model_calls": actual_calls, "results": results, "recovered_chunks": recovered_chunks}
        except MinutesMemoryError as exc:
            if exc.code == "chat_ai_priority":
                return {"status": "deferred", "recording": str(row["recording_id"]), "model_calls": len(results), "reason": exc.code, "recovered_chunks": recovered_chunks}
            mark_semantic_failure(row, exc.code)
            return {"status": "failed", "recording": str(row["recording_id"]), "model_calls": len(results), "error_code": exc.code, "recovered_chunks": recovered_chunks}
        except (memory.AutomationError, syncv2.SyncV2Error, OSError, sqlite3.Error) as exc:
            code = str(getattr(exc, "code", "semantic_failed"))
            mark_semantic_failure(row, code)
            return {"status": "failed", "recording": str(row["recording_id"]), "model_calls": len(results), "error_code": code, "recovered_chunks": recovered_chunks}


def retry_recording(recording_id: str) -> dict[str, Any]:
    connection = syncv2.connect()
    try:
        row = connection.execute("SELECT * FROM recordings WHERE recording_id=?", (recording_id,)).fetchone()
        if not row:
            raise MinutesMemoryError("recording_missing", "recording does not exist")
        if row["status"] != "failed":
            return {"status": "unchanged", "recording": recording_id}
        connection.execute(
            "UPDATE recording_chunks SET status='pending',last_error_code=NULL,started_at=NULL WHERE recording_id=? AND status='failed'",
            (recording_id,),
        )
        chunk_count = int(connection.execute("SELECT COUNT(*) FROM recording_chunks WHERE recording_id=?", (recording_id,)).fetchone()[0])
        connection.execute(
            "UPDATE recordings SET status=?,last_error_code=NULL,updated_at=? WHERE recording_id=?",
            ("chunking" if chunk_count else "discovered", syncv2.now_iso(), recording_id),
        )
        connection.commit()
        return {"status": "retry_queued", "recording": recording_id}
    finally:
        connection.close()


def backfill_summary_comparison(config: dict[str, Any], recording_id: str) -> dict[str, Any]:
    connection = syncv2.connect()
    try:
        row = connection.execute("SELECT * FROM recordings WHERE recording_id=?", (recording_id,)).fetchone()
    finally:
        connection.close()
    if not row:
        raise MinutesMemoryError("recording_missing", "recording does not exist")
    cutover = datetime.fromisoformat(str(config["dingtalk_minutes_memory"]["cutover_at"]))
    if datetime.fromisoformat(str(row["start_time"])) < cutover:
        raise MinutesMemoryError("historical_backfill_blocked", "pre-cutover recordings cannot be reopened automatically")
    if str(row["status"]) not in {"completed", "no_business_change"}:
        raise MinutesMemoryError("backfill_state", "only an already completed recording can reuse its prior independent summary")
    result = process_summary_comparison(
        config,
        row,
        publish_candidates=False,
        allow_legacy_proposal=True,
    )
    return result if result.get("status") == "noop" else {"status": "success", **result}


def record_missing_window(
    start: str,
    end: str,
    note: str,
    *,
    task_uuid: Optional[str] = None,
    title: Optional[str] = None,
    expected_start: Optional[str] = None,
) -> dict[str, Any]:
    detail = {"start": start, "end": end, "note": clean_text(note, 240)}
    if task_uuid:
        detail["task_uuid"] = task_uuid
    if title and expected_start:
        detail["title_fingerprint"] = syncv2.sha256_text(clean_text(title, 160).casefold())
        detail["expected_start"] = expected_start
    dedupe = f"minutes-missing:{start}:{end}"
    incident_id = syncv2.record_incident("recording_not_visible_to_source_api", dedupe, detail)
    syncv2.emit_outbox(
        "sync_failure",
        dedupe,
        {"component": "recording_source", "error_code": "recording_not_visible", "window_start": start, "window_end": end},
    )
    return {"status": "recorded", "incident_id": incident_id}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Governed DingTalk Minutes business-memory sync")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("source-sync")
    subparsers.add_parser("semantic")
    subparsers.add_parser("status")
    retry = subparsers.add_parser("retry")
    retry.add_argument("--recording-id", required=True)
    backfill = subparsers.add_parser("backfill-summary-comparison")
    backfill.add_argument("--recording-id", required=True)
    missing = subparsers.add_parser("record-missing-window")
    missing.add_argument("--start", required=True)
    missing.add_argument("--end", required=True)
    missing.add_argument("--note", required=True)
    missing.add_argument("--task-uuid")
    missing.add_argument("--title")
    missing.add_argument("--expected-start")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    syncv2.initialize()
    try:
        if args.command == "status":
            result = syncv2.status_snapshot()
        elif args.command == "retry":
            result = retry_recording(str(args.recording_id))
        elif args.command == "record-missing-window":
            result = record_missing_window(
                str(args.start),
                str(args.end),
                str(args.note),
                task_uuid=str(args.task_uuid) if args.task_uuid else None,
                title=str(args.title) if args.title else None,
                expected_start=str(args.expected_start) if args.expected_start else None,
            )
        else:
            config = load_config(args.config)
            if syncv2.MANUAL_SYNC_FLAG.is_file() and os.environ.get("DINGTALK_MANUAL_SYNC") != "1":
                result = {"status": "deferred", "reason": "manual_sync_priority", "model_calls": 0}
            elif args.command == "backfill-summary-comparison":
                result = backfill_summary_comparison(config, str(args.recording_id))
            else:
                result = run_source_sync(config) if args.command == "source-sync" else run_semantic(config)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") not in {"failed", "partial"} else 2
    except (MinutesMemoryError, memory.AutomationError, syncv2.SyncV2Error) as exc:
        print(json.dumps({"status": "failed", "error_code": getattr(exc, "code", "failed")}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
