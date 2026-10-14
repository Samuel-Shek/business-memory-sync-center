#!/usr/bin/env python3
"""Shared policy and readiness classification for DingTalk transcript review."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import hashlib
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


POLICY_VERSION = 3
RECONCILE_DEFERRED_STATUS = "review_done_reconcile_deferred"
RECONCILE_FAILED_STATUS = "review_done_reconcile_failed"

MINUTES_ROOT = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync'))
RAW_ROOT = MINUTES_ROOT / ".raw"
REVIEW_ROOT = MINUTES_ROOT / "核对"
DATABASE = Path(
    _archive_path('/Users/example/AI/LocalData/BUSINESS-NonObsidian.nosync/dingtalk-sync-v2/state/sync.sqlite')
)
BRIDGE_CONFIG = Path(
    _archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/config.json')
)

# Owner rule 2020-01-02: recordings longer than the gate threshold never start
# the paid ASR/review path automatically.  They wait for an explicit owner
# decision (ledger flag duration_gate_approved) or stay declined by default.
DURATION_GATE_DEFAULT_SECONDS = 7200
_DURATION_GATE_CACHE: dict[str, Any] | None = None


def duration_gate_seconds() -> int | None:
    """超长音频二查门禁阈值（秒）；返回 None 表示门禁关闭。

    读 bridge config 的 dingtalk_review.duration_gate_seconds（默认 7200），
    duration_gate_enabled=False 或读取失败时分别关闭/回退默认值。
    每进程缓存一次；测试可直接覆写 _DURATION_GATE_CACHE。
    """
    global _DURATION_GATE_CACHE
    if _DURATION_GATE_CACHE is None:
        enabled = True
        seconds = DURATION_GATE_DEFAULT_SECONDS
        try:
            config = json.loads(BRIDGE_CONFIG.read_text(encoding="utf-8"))
            review = config.get("dingtalk_review") if isinstance(config, dict) else {}
            if isinstance(review, dict):
                if review.get("duration_gate_enabled") is False:
                    enabled = False
                else:
                    raw_seconds = review.get("duration_gate_seconds")
                    if isinstance(raw_seconds, (int, float)) and raw_seconds > 0:
                        seconds = int(raw_seconds)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass
        _DURATION_GATE_CACHE = {"enabled": enabled, "seconds": seconds}
    gate = _DURATION_GATE_CACHE
    return int(gate["seconds"]) if gate["enabled"] else None


def recording_duration_seconds(task_uuid: str, raw_root: Path | None = None) -> int | None:
    """从 .raw/<uuid>/basic.json 读录音时长（秒）；缺失或非法返回 None。"""
    raw_root = RAW_ROOT if raw_root is None else Path(raw_root)
    try:
        payload = json.loads(
            (raw_root / str(task_uuid) / "basic.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("duration")
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return None
    if milliseconds <= 0:
        return None
    return milliseconds // 1000


def _value(row: Mapping[str, Any] | sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def observed_at(entry: Mapping[str, Any]) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(entry.get("start_time")) / 1000).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def policy_fields(entry: Mapping[str, Any], cutover: datetime) -> dict[str, Any]:
    title = str(entry.get("title") or "")
    observed = observed_at(entry)
    if "【示例】" in title:
        required = False
        reason = "example_exempt"
    elif observed is not None and observed < cutover:
        required = False
        reason = "historical_pre_cutover"
    else:
        # Missing or invalid timestamps fail closed: the item remains review-required.
        required = True
        reason = "post_cutover_all_recordings" if observed is not None else "timestamp_unresolved"
    return {
        "policy_version": POLICY_VERSION,
        "review_required": required,
        "requirement_reason": reason,
    }


def apply_policy(
    sync_entries: Mapping[str, Any],
    ledger: dict[str, Any],
    cutover: datetime,
) -> tuple[bool, dict[str, int]]:
    changed = False
    counts = {"required": 0, "exempt": 0}
    for task_uuid, raw_entry in sync_entries.items():
        if not isinstance(raw_entry, Mapping):
            continue
        row = ledger.get(task_uuid)
        if not isinstance(row, dict):
            row = {"attempts": 0}
            ledger[task_uuid] = row
            changed = True
        expected = policy_fields(raw_entry, cutover)
        for key, value in expected.items():
            if row.get(key) != value:
                row[key] = value
                changed = True
        counts["required" if expected["review_required"] else "exempt"] += 1
    return changed, counts


def load_recording_rows(database: Path | None = None) -> dict[str, dict[str, Any]]:
    database = DATABASE if database is None else Path(database)
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(4):
        connection: sqlite3.Connection | None = None
        try:
            if not database.is_file():
                if attempt < 3:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                return {}
            # The source database uses WAL.  A copied/replaced WAL database may
            # briefly have no -shm sidecar; mode=ro cannot create it and fails
            # with "unable to open database file".  mode=rw guarantees the
            # database already exists while allowing SQLite to create sidecars;
            # query_only still prevents application writes.
            connection = sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=rw",
                uri=True,
                timeout=5,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            return {
                str(row["task_uuid"]): dict(row)
                for row in connection.execute(
                    """
                    SELECT task_uuid,status,source_memory_id,source_memory_path,
                      summary_comparison_status,review_status
                    FROM recordings
                    """
                )
            }
        except sqlite3.OperationalError as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.1 * (attempt + 1))
                continue
        finally:
            if connection is not None:
                connection.close()
    if last_error is not None:
        raise last_error
    return {}


def transcript_ready(task_uuid: str, raw_root: Path | None = None) -> bool:
    raw_root = RAW_ROOT if raw_root is None else Path(raw_root)
    path = raw_root / task_uuid / "transcript.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    paragraphs = payload.get("paragraphList") if isinstance(payload, dict) else None
    return bool(
        payload.get("complete") is True
        and str(payload.get("taskUuid") or "") == task_uuid
        and isinstance(paragraphs, list)
        and paragraphs
        and int(payload.get("paragraphCount") or -1) == len(paragraphs)
    )


def media_readiness(
    entry: Mapping[str, Any],
    minutes_root: Path | None = None,
    *,
    verify_hash: bool = False,
) -> str:
    minutes_root = MINUTES_ROOT if minutes_root is None else Path(minutes_root)
    storage = str(entry.get("media_storage") or "local")
    filename = str(entry.get("media_filename") or "")
    expected_bytes = entry.get("media_bytes")
    expected_sha = str(entry.get("media_sha256") or "")
    if (
        storage == "pssd_corrupt"
        or not filename
        or Path(filename).name != filename
        or not isinstance(expected_bytes, int)
        or expected_bytes <= 0
        or len(expected_sha) != 64
    ):
        return "waiting_media"
    media_path = minutes_root / "音频" / filename
    try:
        if media_path.is_file() and media_path.stat().st_size == expected_bytes:
            if verify_hash:
                digest = hashlib.sha256()
                with media_path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                if digest.hexdigest() != expected_sha:
                    return "waiting_media"
            return "ready"
    except OSError:
        pass
    return "waiting_pssd" if storage == "pssd" else "waiting_media"


def source_ready(row: Mapping[str, Any] | sqlite3.Row | None) -> bool:
    status = str(_value(row, "status", ""))
    comparison = str(_value(row, "summary_comparison_status", ""))
    source_id = str(_value(row, "source_memory_id", ""))
    source_path = str(_value(row, "source_memory_path", ""))
    return bool(
        status in {"completed", "no_business_change"}
        and comparison == "compared"
        and source_id
        and source_path
        and Path(source_path).is_file()
    )


def resumable_artifacts(entry: Mapping[str, Any], review_root: Path | None = None) -> str:
    review_root = REVIEW_ROOT if review_root is None else Path(review_root)
    stem = Path(str(entry.get("filename") or "")).stem
    if not stem:
        return "none"
    meeting = review_root / stem
    result = meeting / "核对结果"
    reports = list(result.glob("*最终核对稿.md")) if result.is_dir() else []
    metadata_path = result / "核对运行元数据.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = None
    context_hash = str(metadata.get("memory_context_sha256") or "") if isinstance(metadata, dict) else ""
    valid_metadata = bool(
        isinstance(metadata, dict)
        and metadata.get("schema_version") == 2
        and metadata.get("review_controller") == "codex-cli"
        and metadata.get("review_model") in {"gpt-6-astra", "gpt-5.6-sol", "mimo-v2.5-pro"}
        and metadata.get("asr_model") == "qwen-audio-3.0-asr-flash-filetrans"
        and metadata.get("memory_policy") == "memory_inclination_only"
        and len(context_hash) == 64
        and all(character in "0123456789abcdef" for character in context_hash)
        and all(
            isinstance(metadata.get(field), int) and metadata[field] >= 0
            for field in (
                "memory_context_count",
                "difference_count",
                "memory_supported_count",
                "critical_doubt_count",
            )
        )
    )
    if valid_metadata and any(path.is_file() and path.stat().st_size > 0 for path in reports):
        return "report"
    reference = meeting / "参考稿.json"
    try:
        payload = json.loads(reference.read_text(encoding="utf-8"))
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if (
            isinstance(payload, dict)
            and payload.get("engine") == "qwen-audio-3.0-asr-flash-filetrans"
            and payload.get("language") == "zh"
            and isinstance(segments, list)
            and segments
            and all(
                isinstance(item, dict)
                and isinstance(item.get("begin_ms"), int)
                and isinstance(item.get("end_ms"), int)
                and item["end_ms"] >= item["begin_ms"]
                and bool(str(item.get("text") or "").strip())
                for item in segments
            )
        ):
            return "qwen_reference"
    except (OSError, json.JSONDecodeError):
        pass
    return "none"


def classify(
    task_uuid: str,
    entry: Mapping[str, Any],
    ledger_row: Mapping[str, Any] | None,
    recording_row: Mapping[str, Any] | sqlite3.Row | None,
    cutover: datetime,
    *,
    raw_root: Path | None = None,
    minutes_root: Path | None = None,
    review_root: Path | None = None,
    verify_media_hash: bool = False,
) -> dict[str, Any]:
    raw_root = RAW_ROOT if raw_root is None else Path(raw_root)
    minutes_root = MINUTES_ROOT if minutes_root is None else Path(minutes_root)
    review_root = REVIEW_ROOT if review_root is None else Path(review_root)
    ledger_row = ledger_row if isinstance(ledger_row, Mapping) else {}
    expected_policy = policy_fields(entry, cutover)
    required = bool(
        ledger_row.get("review_required")
        if ledger_row.get("policy_version") == POLICY_VERSION
        else expected_policy["review_required"]
    )
    reason = str(
        ledger_row.get("requirement_reason")
        if ledger_row.get("policy_version") == POLICY_VERSION
        else expected_policy["requirement_reason"]
    )
    status = str(ledger_row.get("status") or "pending")
    base = {
        "required": required,
        "requirement_reason": reason,
        "execution_status": status,
        "resumable": "none",
    }
    if status == "done":
        return {**base, "state": "done", "actionable": False}
    import video_hold
    if entry.get('video_waiting_instruction') or video_hold.classify(entry.get('media_filename',''),entry.get('media_content_type',''),unknown=False):
        return {**base, 'state':'waiting_instruction','actionable':False,'resumable':'none'}
    if not required:
        return {**base, "state": "exempt", "actionable": False}
    if status == RECONCILE_DEFERRED_STATUS:
        return {**base, "state": "reconcile_deferred", "actionable": False}
    if status in {"failed", RECONCILE_FAILED_STATUS}:
        return {**base, "state": "failed", "actionable": False}
    if status == "in_progress":
        return {**base, "state": "in_progress", "actionable": False}
    # 超长音频门禁只拦“尚未开跑的新任务”：interrupted 的条目已有可复用
    # 中间产物（ASR 费用已发生），续跑路径不受门禁影响。
    if status != "interrupted":
        if ledger_row.get("duration_gate_declined"):
            return {**base, "state": "duration_declined", "actionable": False}
        gate_seconds = duration_gate_seconds()
        if gate_seconds is not None and not ledger_row.get("duration_gate_approved"):
            duration = recording_duration_seconds(task_uuid, raw_root=raw_root)
            if duration is not None and duration > gate_seconds:
                return {
                    **base,
                    "state": "waiting_duration_decision",
                    "actionable": False,
                    "duration_seconds": duration,
                    "duration_gate_seconds": gate_seconds,
                }
    resumable = "none"
    if status == "interrupted":
        resumable = resumable_artifacts(entry, review_root=review_root)
        base = {**base, "resumable": resumable}
        if resumable == "report":
            if not source_ready(recording_row):
                return {**base, "state": "waiting_source", "actionable": False, "resumable": resumable}
            return {**base, "state": "ready", "actionable": True, "resumable": resumable}
    if not transcript_ready(task_uuid, raw_root=raw_root):
        return {**base, "state": "waiting_source", "actionable": False}
    media_state = media_readiness(
        entry,
        minutes_root=minutes_root,
        verify_hash=verify_media_hash,
    )
    if media_state != "ready":
        return {**base, "state": media_state, "actionable": False}
    if not source_ready(recording_row):
        return {**base, "state": "waiting_source", "actionable": False}
    if status == "interrupted":
        if resumable == "none":
            return {**base, "state": "interrupted_manual", "actionable": False}
        return {**base, "state": "ready", "actionable": True, "resumable": resumable}
    return {**base, "state": "ready", "actionable": True}


def aggregate(
    sync_entries: Mapping[str, Any],
    ledger: Mapping[str, Any],
    recording_rows: Mapping[str, Any],
    cutover: datetime,
) -> dict[str, int]:
    counts = {
        "reviewed": 0,
        "review_ready": 0,
        "review_waiting_source": 0,
        "review_exempt": 0,
        "review_in_progress": 0,
        "review_failed": 0,
        "review_reconcile_deferred": 0,
        "review_waiting_decision": 0,
        "review_duration_declined": 0,
        "pending_review": 0,
    }
    for task_uuid, entry in sync_entries.items():
        if not isinstance(entry, Mapping):
            continue
        state = classify(
            str(task_uuid),
            entry,
            ledger.get(task_uuid) if isinstance(ledger, Mapping) else None,
            recording_rows.get(task_uuid) if isinstance(recording_rows, Mapping) else None,
            cutover,
        )["state"]
        if state == "done":
            counts["reviewed"] += 1
            continue
        if state == "exempt":
            counts["review_exempt"] += 1
            continue
        if state == "duration_declined":
            counts["review_duration_declined"] += 1
            continue
        if state == "waiting_duration_decision":
            counts["review_waiting_decision"] += 1
            continue
        counts["pending_review"] += 1
        if state == "ready":
            counts["review_ready"] += 1
        elif state == "in_progress":
            counts["review_in_progress"] += 1
            readiness = classify(
                str(task_uuid), entry,
                {**(ledger.get(task_uuid) or {}), "status": "pending"},
                recording_rows.get(task_uuid) if isinstance(recording_rows, Mapping) else None,
                cutover,
            )["state"]
            if readiness in {"waiting_source", "waiting_media", "waiting_pssd"}:
                counts["review_waiting_source"] += 1
        elif state in {"failed", "interrupted_manual"}:
            counts["review_failed"] += 1
            readiness = classify(
                str(task_uuid), entry,
                {**(ledger.get(task_uuid) or {}), "status": "pending"},
                recording_rows.get(task_uuid) if isinstance(recording_rows, Mapping) else None,
                cutover,
            )["state"]
            if readiness in {"waiting_source", "waiting_media", "waiting_pssd"}:
                counts["review_waiting_source"] += 1
        elif state == "reconcile_deferred":
            counts["review_reconcile_deferred"] += 1
        else:
            counts["review_waiting_source"] += 1
    return counts


def process_alive(pid: Any) -> bool:
    try:
        value = int(pid)
        if value <= 1:
            return False
        os.kill(value, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False
