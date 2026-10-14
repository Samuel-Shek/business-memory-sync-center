#!/usr/bin/env python3
"""Shared local state, locks and notification outbox for DingTalk sync v2."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import fcntl
import hashlib
import json
import os
import sqlite3
import sqlite_attribution
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional


os.umask(0o077)

ROOT = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-NonObsidian.nosync/dingtalk-sync-v2'))
STATE_DIR = ROOT / "state"
RUNS_DIR = ROOT / "runs"
ARCHIVE_DIR = ROOT / "decision-archive"
DATABASE = STATE_DIR / "sync.sqlite"
AI_LOCK = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/state/dingtalk-shared-ai.lock'))
SOURCE_LOCK = STATE_DIR / "source-sync.lock"
SEMANTIC_LOCK = STATE_DIR / "semantic.lock"
DECISION_LOCK = STATE_DIR / "decision.lock"
MEDIA_LOCK = STATE_DIR / "media.lock"
MANUAL_SYNC_LOCK = STATE_DIR / "manual-sync.lock"
MANUAL_SYNC_FLAG = STATE_DIR / "manual-sync.active"
REVIEW_LOCK = STATE_DIR / "review-stage.lock"
MINUTES_REVIEW_LEDGER = Path(
    _archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/核对/.state/review-ledger.json')
)
MINUTES_IDLE_REVIEW = Path(
    _archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/bin/dingtalk_idle_review.py')
)
REVIEW_RECONCILE_DEFERRED = "review_done_reconcile_deferred"
LEGACY_AI_PRIORITY_REASON = "核对稿已生成，但记忆回写失败：chat_ai_priority"
CONTROL_ACTION_TYPES = frozenset({
    "sync_now",
    "dingtalk_chat",
    "dingtalk_minutes",
    "dingtalk_mail",
    "win11",
    "wechat",
    "all",
})
BOUNDED_REPAIR_ACTION_TYPES = frozenset({"preserve_current", "manual_listener_acceptance"})


class SyncV2Error(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _ensure_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def classify_source_kind(recording_id: Any, source_ref: str) -> str:
    if recording_id:
        return "recording"
    normalized = str(source_ref or "").strip()
    if (
        normalized.startswith("dingtalk-minutes-batch:")
        or normalized.startswith("核对/")
        or "钉钉听记" in normalized
        or "会议纪要" in normalized
    ):
        return "recording"
    return "chat"


def classify_decision_severity(candidate_json: str) -> str:
    try:
        candidate = json.loads(candidate_json)
    except (TypeError, json.JSONDecodeError):
        return "system_exception"
    if not isinstance(candidate, dict):
        return "system_exception"
    action = str(candidate.get("action") or "")
    if action == "conflict":
        related = candidate.get("related_memory_ids")
        reason = str(candidate.get("conflict_reason") or "").strip()
        return "conflict" if isinstance(related, list) and bool(related) and bool(reason) else "system_exception"
    if str(candidate.get("memory_kind") or "") == "open_question":
        return "open_question"
    return "confirmation"


def initialize() -> None:
    # Cold imports can load the mail/UI stack. Complete them before opening
    # the ledger so no database write lock is held during module loading.
    import workbench_attention
    import alert_workbench
    import operational_workbench

    for path in (ROOT, STATE_DIR, RUNS_DIR, ARCHIVE_DIR, AI_LOCK.parent):
        ensure_private_dir(path)
    connection = connect()
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS recordings (
              recording_id TEXT PRIMARY KEY,
              task_uuid TEXT NOT NULL UNIQUE,
              task_key_sha256 TEXT NOT NULL UNIQUE,
              source_ref TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              start_time TEXT NOT NULL,
              source_filename TEXT NOT NULL,
              transcript_path TEXT NOT NULL,
              transcript_sha256 TEXT NOT NULL,
              media_sha256 TEXT NOT NULL,
              source_memory_id TEXT,
              source_memory_path TEXT,
              status TEXT NOT NULL,
              chunk_count INTEGER NOT NULL DEFAULT 0,
              model_calls INTEGER NOT NULL DEFAULT 0,
              last_error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS recording_chunks (
              recording_id TEXT NOT NULL,
              chunk_index INTEGER NOT NULL,
              paragraph_start INTEGER NOT NULL,
              paragraph_end INTEGER NOT NULL,
              input_chars INTEGER NOT NULL,
              input_sha256 TEXT NOT NULL,
              status TEXT NOT NULL,
              result_json TEXT,
              result_sha256 TEXT,
              started_at TEXT,
              completed_at TEXT,
              last_error_code TEXT,
              PRIMARY KEY(recording_id, chunk_index),
              FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
            );
            CREATE TABLE IF NOT EXISTS decisions (
              decision_id TEXT PRIMARY KEY,
              recording_id TEXT,
              source_ref TEXT NOT NULL,
              candidate_json TEXT NOT NULL,
              candidate_hash TEXT NOT NULL,
              candidate_memory_id TEXT NOT NULL,
              candidate_path TEXT NOT NULL,
              candidate_file_hash TEXT NOT NULL,
              related_memory_ids_json TEXT NOT NULL,
              related_hashes_json TEXT NOT NULL,
              status TEXT NOT NULL,
              resolution TEXT,
              created_at TEXT NOT NULL,
              resolved_at TEXT,
              FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
            );
            CREATE INDEX IF NOT EXISTS decisions_status_idx ON decisions(status, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS decisions_candidate_memory_idx ON decisions(candidate_memory_id);
            CREATE TABLE IF NOT EXISTS outbox (
              event_id TEXT PRIMARY KEY,
              event_type TEXT NOT NULL,
              dedupe_key TEXT NOT NULL UNIQUE,
              safe_payload_json TEXT NOT NULL,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              delivered_at TEXT,
              receipt_hash TEXT,
              last_error_code TEXT
            );
            CREATE INDEX IF NOT EXISTS outbox_status_idx ON outbox(status, created_at);
            CREATE TABLE IF NOT EXISTS incidents (
              incident_id TEXT PRIMARY KEY,
              incident_type TEXT NOT NULL,
              dedupe_key TEXT NOT NULL UNIQUE,
              safe_detail_json TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decision_previews (
              token_hash TEXT PRIMARY KEY,
              decision_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              action TEXT NOT NULL,
              merge_text_hash TEXT NOT NULL,
              snooze_days INTEGER,
              candidate_hash TEXT NOT NULL,
              candidate_file_hash TEXT NOT NULL,
              related_hashes_json TEXT NOT NULL,
              context_hash TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              used_at TEXT,
              FOREIGN KEY(decision_id) REFERENCES decisions(decision_id)
            );
            CREATE INDEX IF NOT EXISTS decision_previews_decision_idx
              ON decision_previews(decision_id,created_at);
            CREATE TABLE IF NOT EXISTS decision_transactions (
              transaction_id TEXT PRIMARY KEY,
              decision_id TEXT NOT NULL,
              preview_token_hash TEXT NOT NULL,
              action TEXT NOT NULL,
              state TEXT NOT NULL,
              journal_path TEXT NOT NULL,
              file_committed_at TEXT,
              database_committed_at TEXT,
              notification_committed_at TEXT,
              last_error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(decision_id) REFERENCES decisions(decision_id)
            );
            CREATE INDEX IF NOT EXISTS decision_transactions_state_idx
              ON decision_transactions(state,updated_at);
            CREATE TABLE IF NOT EXISTS action_runs (
              run_id TEXT PRIMARY KEY,
              action_type TEXT NOT NULL,
              trigger TEXT NOT NULL DEFAULT 'unknown',
              status TEXT NOT NULL,
              result_json TEXT,
              error_code TEXT,
              started_at TEXT NOT NULL,
              completed_at TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS action_runs_status_idx ON action_runs(status,started_at);
            CREATE TABLE IF NOT EXISTS action_steps (
              run_id TEXT NOT NULL,
              step_key TEXT NOT NULL,
              step_order INTEGER NOT NULL,
              label TEXT NOT NULL,
              status TEXT NOT NULL,
              current_count INTEGER,
              total_count INTEGER,
              safe_item_label TEXT,
              safe_detail_json TEXT NOT NULL DEFAULT '{}',
              error_code TEXT,
              started_at TEXT,
              completed_at TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(run_id,step_key),
              FOREIGN KEY(run_id) REFERENCES action_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS action_steps_run_idx ON action_steps(run_id,step_order);
            CREATE TABLE IF NOT EXISTS decision_messages (
              message_id TEXT PRIMARY KEY,
              decision_id TEXT NOT NULL,
              decision_revision INTEGER NOT NULL,
              author TEXT NOT NULL,
              message_kind TEXT NOT NULL,
              content TEXT NOT NULL,
              status TEXT NOT NULL,
              reply_to TEXT,
              run_id TEXT,
              model_called INTEGER NOT NULL DEFAULT 0,
              error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(decision_id) REFERENCES decisions(decision_id),
              FOREIGN KEY(reply_to) REFERENCES decision_messages(message_id),
              FOREIGN KEY(run_id) REFERENCES action_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS decision_messages_decision_idx
              ON decision_messages(decision_id,created_at);
            CREATE TABLE IF NOT EXISTS decision_drafts (
              decision_id TEXT PRIMARY KEY,
              decision_revision INTEGER NOT NULL,
              message_kind TEXT NOT NULL,
              content TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(decision_id) REFERENCES decisions(decision_id)
            );
            CREATE TABLE IF NOT EXISTS case_threads (
              case_id TEXT PRIMARY KEY,
              lineage_id TEXT NOT NULL DEFAULT '',
              semantic_fingerprint TEXT NOT NULL DEFAULT '',
              case_epoch INTEGER NOT NULL DEFAULT 1,
              current_episode_id TEXT,
              thread_revision INTEGER NOT NULL DEFAULT 1,
              last_sequence INTEGER NOT NULL DEFAULT 0,
              case_version INTEGER NOT NULL,
              context_hash TEXT NOT NULL,
              evidence_watermark TEXT NOT NULL,
              conversation_hash TEXT NOT NULL,
              conversation_state TEXT NOT NULL DEFAULT 'clarifying',
              summary TEXT NOT NULL DEFAULT '',
              summary_through_sequence INTEGER NOT NULL DEFAULT 0,
              active_message_id TEXT,
              last_error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS case_episodes (
              episode_id TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              case_epoch INTEGER NOT NULL,
              opened_context_hash TEXT NOT NULL,
              opened_evidence_watermark TEXT NOT NULL,
              opened_at TEXT NOT NULL,
              closed_at TEXT,
              close_reason TEXT,
              UNIQUE(case_id,case_epoch),
              FOREIGN KEY(case_id) REFERENCES case_threads(case_id)
            );
            CREATE TABLE IF NOT EXISTS case_events (
              event_id TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              episode_id TEXT,
              sequence INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              event_payload_json TEXT NOT NULL DEFAULT '{}',
              context_hash TEXT NOT NULL,
              evidence_watermark TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(case_id,sequence),
              FOREIGN KEY(case_id) REFERENCES case_threads(case_id),
              FOREIGN KEY(episode_id) REFERENCES case_episodes(episode_id)
            );
            CREATE INDEX IF NOT EXISTS case_events_case_idx
              ON case_events(case_id,sequence);
            CREATE TABLE IF NOT EXISTS case_messages (
              message_id TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              sequence INTEGER NOT NULL,
              case_version INTEGER NOT NULL,
              context_hash TEXT NOT NULL,
              evidence_watermark TEXT NOT NULL,
              author TEXT NOT NULL,
              message_kind TEXT NOT NULL,
              content TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              conversation_state TEXT,
              focused_questions_json TEXT NOT NULL DEFAULT '[]',
              proposed_plan TEXT,
              reply_to TEXT,
              retry_of TEXT,
              retry_count INTEGER NOT NULL DEFAULT 0,
              ai_model TEXT NOT NULL DEFAULT '',
              ai_reasoning_effort TEXT NOT NULL DEFAULT '',
              ai_speed TEXT NOT NULL DEFAULT '',
              archive_after_reply INTEGER NOT NULL DEFAULT 0,
              run_id TEXT,
              model_called INTEGER NOT NULL DEFAULT 0,
              error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(case_id,sequence),
              FOREIGN KEY(case_id) REFERENCES case_threads(case_id),
              FOREIGN KEY(reply_to) REFERENCES case_messages(message_id),
              FOREIGN KEY(retry_of) REFERENCES case_messages(message_id),
              FOREIGN KEY(run_id) REFERENCES action_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS case_messages_case_idx
              ON case_messages(case_id,sequence);
            CREATE UNIQUE INDEX IF NOT EXISTS case_messages_active_ai_idx
              ON case_messages(case_id)
              WHERE author='user' AND message_kind='question' AND status IN ('pending','running');
            CREATE TABLE IF NOT EXISTS case_ai_runs (
              ai_run_id TEXT PRIMARY KEY,
              action_run_id TEXT NOT NULL,
              message_id TEXT NOT NULL UNIQUE,
              case_id TEXT NOT NULL,
              case_version INTEGER NOT NULL,
              context_hash TEXT NOT NULL,
              evidence_watermark TEXT NOT NULL,
              basis_conversation_hash TEXT NOT NULL,
              basis_head_sequence INTEGER NOT NULL,
              fencing_token INTEGER NOT NULL,
              attempt INTEGER NOT NULL DEFAULT 1,
              ai_model TEXT NOT NULL DEFAULT '',
              ai_reasoning_effort TEXT NOT NULL DEFAULT '',
              ai_speed TEXT NOT NULL DEFAULT '',
              state TEXT NOT NULL,
              last_error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              FOREIGN KEY(case_id) REFERENCES case_threads(case_id),
              FOREIGN KEY(message_id) REFERENCES case_messages(message_id),
              FOREIGN KEY(action_run_id) REFERENCES action_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS case_ai_runs_case_idx
              ON case_ai_runs(case_id,created_at);
            CREATE TABLE IF NOT EXISTS case_proposals (
              proposal_id TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              source_message_id TEXT NOT NULL,
              assistant_message_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              proposal_text TEXT NOT NULL,
              proposal_hash TEXT NOT NULL,
              conversation_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              superseded_at TEXT,
              UNIQUE(case_id,revision),
              FOREIGN KEY(case_id) REFERENCES case_threads(case_id),
              FOREIGN KEY(source_message_id) REFERENCES case_messages(message_id),
              FOREIGN KEY(assistant_message_id) REFERENCES case_messages(message_id)
            );
            CREATE INDEX IF NOT EXISTS case_proposals_case_idx
              ON case_proposals(case_id,status,revision);
            CREATE TABLE IF NOT EXISTS case_drafts (
              case_id TEXT PRIMARY KEY,
              case_version INTEGER NOT NULL,
              content TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(case_id) REFERENCES case_threads(case_id)
            );
            CREATE TABLE IF NOT EXISTS case_action_receipts (
              idempotency_key TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              preview_token_hash TEXT NOT NULL,
              action TEXT NOT NULL,
              action_params_json TEXT NOT NULL DEFAULT '{}',
              action_params_hash TEXT NOT NULL,
              case_version INTEGER NOT NULL,
              context_hash TEXT NOT NULL,
              evidence_watermark TEXT NOT NULL DEFAULT '',
              conversation_hash TEXT NOT NULL,
              maintenance_epoch TEXT NOT NULL DEFAULT '',
              state TEXT NOT NULL,
              result_json TEXT,
              last_error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              committed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS case_action_receipts_case_idx
              ON case_action_receipts(case_id,created_at);
            CREATE TABLE IF NOT EXISTS case_memory_proposals (
              proposal_id TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              source_message_id TEXT NOT NULL,
              assistant_message_id TEXT NOT NULL,
              title TEXT NOT NULL,
              proposal_text TEXT NOT NULL,
              target_page TEXT NOT NULL,
              source_attribution TEXT NOT NULL,
              fact_status TEXT NOT NULL,
              evidence_boundary TEXT NOT NULL,
              risk_level TEXT NOT NULL,
              proposal_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              claim_id TEXT,
              last_error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              applied_at TEXT,
              FOREIGN KEY(case_id) REFERENCES case_threads(case_id),
              FOREIGN KEY(source_message_id) REFERENCES case_messages(message_id),
              FOREIGN KEY(assistant_message_id) REFERENCES case_messages(message_id)
            );
            CREATE INDEX IF NOT EXISTS case_memory_proposals_case_idx
              ON case_memory_proposals(case_id,status,created_at);
            CREATE TABLE IF NOT EXISTS review_reconciliations (
              reconciliation_id TEXT PRIMARY KEY,
              recording_id TEXT NOT NULL,
              review_sha256 TEXT NOT NULL,
              original_transcript_sha256 TEXT NOT NULL,
              reviewed_path TEXT NOT NULL,
              status TEXT NOT NULL,
              decision_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(recording_id,review_sha256),
              FOREIGN KEY(recording_id) REFERENCES recordings(recording_id),
              FOREIGN KEY(decision_id) REFERENCES decisions(decision_id)
            );
            """
        )
        for definition in (
            "transcript_version INTEGER NOT NULL DEFAULT 1",
            "review_status TEXT NOT NULL DEFAULT 'pending'",
            "reviewed_transcript_sha256 TEXT",
            "review_reconciled_at TEXT",
            "media_archive_status TEXT NOT NULL DEFAULT 'local'",
            "native_summary_sha256 TEXT",
            "independent_summary_sha256 TEXT",
            "summary_comparison_sha256 TEXT",
            "summary_comparison_status TEXT NOT NULL DEFAULT 'pending'",
            "summary_comparison_policy_version INTEGER NOT NULL DEFAULT 1",
        ):
            _ensure_column(connection, "recordings", definition)
        for definition in (
            "revision INTEGER NOT NULL DEFAULT 1",
            "source_kind TEXT NOT NULL DEFAULT 'unknown'",
            "severity TEXT NOT NULL DEFAULT 'unclassified'",
            "snooze_until TEXT",
            "current_content_hash TEXT NOT NULL DEFAULT ''",
            "updated_at TEXT",
        ):
            _ensure_column(connection, "decisions", definition)
        _ensure_column(connection, "decision_previews", "context_hash TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "action_runs", "trigger TEXT NOT NULL DEFAULT 'unknown'")
        for definition in (
            "lineage_id TEXT NOT NULL DEFAULT ''",
            "semantic_fingerprint TEXT NOT NULL DEFAULT ''",
            "case_epoch INTEGER NOT NULL DEFAULT 1",
            "current_episode_id TEXT",
            "thread_revision INTEGER NOT NULL DEFAULT 1",
            "last_sequence INTEGER NOT NULL DEFAULT 0",
        ):
            _ensure_column(connection, "case_threads", definition)
        for definition in (
            "ai_model TEXT NOT NULL DEFAULT ''",
            "ai_reasoning_effort TEXT NOT NULL DEFAULT ''",
            "ai_speed TEXT NOT NULL DEFAULT ''",
        ):
            _ensure_column(connection, "case_messages", definition)
            _ensure_column(connection, "case_ai_runs", definition)
        _ensure_column(
            connection,
            "case_messages",
            "archive_after_reply INTEGER NOT NULL DEFAULT 0",
        )
        for definition in (
            "action_params_json TEXT NOT NULL DEFAULT '{}'",
            "evidence_watermark TEXT NOT NULL DEFAULT ''",
            "maintenance_epoch TEXT NOT NULL DEFAULT ''",
        ):
            _ensure_column(connection, "case_action_receipts", definition)
        stamp = now_iso()
        connection.execute(
            "INSERT INTO metadata(key,value,updated_at) VALUES('case_conversation_schema_version','4',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
            "WHERE metadata.value<>excluded.value",
            (stamp,),
        )
        rows = connection.execute(
            """
            SELECT decision_id,recording_id,source_ref,candidate_json,candidate_file_hash,
                   source_kind,severity,current_content_hash,created_at,updated_at
            FROM decisions
            """
        ).fetchall()
        for row in rows:
            source_kind = classify_source_kind(row["recording_id"], str(row["source_ref"]))
            severity = classify_decision_severity(str(row["candidate_json"]))
            content_hash = str(row["current_content_hash"] or row["candidate_file_hash"])
            updated_at = str(row["updated_at"] or row["created_at"] or stamp)
            if (
                source_kind != str(row["source_kind"] or "")
                or severity != str(row["severity"] or "")
                or content_hash != str(row["current_content_hash"] or "")
                or updated_at != str(row["updated_at"] or "")
            ):
                connection.execute(
                    """
                    UPDATE decisions
                    SET source_kind=?,severity=?,current_content_hash=?,updated_at=?
                    WHERE decision_id=?
                    """,
                    (source_kind, severity, content_hash, updated_at, str(row["decision_id"])),
                )
        normalized = connection.execute(
            "SELECT value FROM metadata WHERE key='migration:outbox-local-timezone'"
        ).fetchone()
        if not normalized:
            for row in connection.execute(
                "SELECT event_id,created_at,updated_at,delivered_at FROM outbox"
            ).fetchall():
                values: list[Optional[str]] = []
                for key in ("created_at", "updated_at", "delivered_at"):
                    raw = row[key]
                    if raw is None:
                        values.append(None)
                        continue
                    try:
                        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                        values.append(parsed.astimezone().isoformat(timespec="seconds"))
                    except ValueError:
                        values.append(str(raw))
                connection.execute(
                    "UPDATE outbox SET created_at=?,updated_at=?,delivered_at=? WHERE event_id=?",
                    (*values, str(row["event_id"])),
                )
            connection.execute(
                "INSERT INTO metadata(key,value,updated_at) VALUES('migration:outbox-local-timezone','done',?)",
                (stamp,),
            )
        workbench_attention.initialize(connection)
        alert_workbench.initialize(connection)
        operational_workbench.initialize(connection)
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) < 11:
            connection.execute("PRAGMA user_version=11")
        connection.commit()
    finally:
        connection.close()
    DATABASE.chmod(0o600)


def connect() -> sqlite3.Connection:
    ensure_private_dir(STATE_DIR)
    connection = sqlite_attribution.attach(sqlite3.connect(str(DATABASE), timeout=30), DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def connect_readonly() -> sqlite3.Connection:
    if not DATABASE.is_file():
        raise SyncV2Error("database_missing", "DingTalk sync state database is missing")
    # WAL readers may need SQLite to create its -shm sidecar. Open normally,
    # then fail closed at the connection layer before issuing any query.
    connection = sqlite3.connect(str(DATABASE), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


@contextmanager
def file_lock(path: Path, blocking: bool = False) -> Iterator[Any]:
    ensure_private_dir(path.parent)
    handle = path.open("a+")
    path.chmod(0o600)
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError as exc:
        handle.close()
        raise SyncV2Error("already_running", f"lock is already held: {path.name}") from exc
    try:
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def minutes_review_handoff_pending() -> bool:
    """Return whether one reviewed transcript is waiting only for the shared AI slot."""
    try:
        payload = json.loads(MINUTES_REVIEW_LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    for row in payload.values():
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "")
        reason = str(row.get("reason") or "")
        if status == REVIEW_RECONCILE_DEFERRED:
            return True
        if status == "review_done_reconcile_failed" and reason == LEGACY_AI_PRIORITY_REASON:
            return True
    return False


def run_minutes_review_handoff(timeout_seconds: int = 3700) -> dict[str, Any]:
    """Yield the shared model slot to at most one already-reviewed recording."""
    empty = {"status": "noop", "processed": 0, "done": 0, "deferred": 0, "failed": 0}
    if not minutes_review_handoff_pending():
        return empty
    if not MINUTES_IDLE_REVIEW.is_file() or MINUTES_IDLE_REVIEW.is_symlink():
        return {**empty, "status": "failed", "reason": "handoff_entry_missing", "failed": 1}
    try:
        process = subprocess.run(
            ["/usr/bin/python3", str(MINUTES_IDLE_REVIEW), "--handoff"],
            text=True,
            capture_output=True,
            timeout=max(60, int(timeout_seconds)),
            check=False,
            env={**os.environ, "LC_ALL": "en_US.UTF-8"},
        )
    except subprocess.TimeoutExpired:
        return {**empty, "status": "failed", "reason": "handoff_timeout", "failed": 1}
    except OSError:
        return {**empty, "status": "failed", "reason": "handoff_unavailable", "failed": 1}
    receipt: dict[str, Any] = {}
    for line in reversed(process.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            receipt = candidate
            break
    allowed_statuses = {"success", "noop", "deferred", "partial"}
    if process.returncode != 0 or str(receipt.get("status") or "") not in allowed_statuses:
        return {**empty, "status": "failed", "reason": "handoff_failed", "failed": 1}
    reason = str(receipt.get("reason") or "")
    safe_reasons = {
        "chat_ai_priority",
        "manual_sync_priority",
        "maintenance_active",
        "sync_or_review_busy",
    }
    return {
        "status": str(receipt["status"]),
        "reason": reason if reason in safe_reasons else "",
        "processed": max(0, int(receipt.get("processed") or 0)),
        "done": max(0, int(receipt.get("done") or 0)),
        "deferred": max(0, int(receipt.get("deferred") or 0)),
        "failed": max(0, int(receipt.get("failed") or 0)),
    }


def emit_outbox(event_type: str, dedupe_key: str, safe_payload: dict[str, Any]) -> tuple[str, bool]:
    payload = canonical_json(safe_payload)
    if len(payload) > 12000:
        raise SyncV2Error("outbox_payload_too_large", "safe notification payload is too large")
    if os.environ.get("DINGTALK_LOCAL_ONLY") == "1":
        return f"local-only-{sha256_text(f'{event_type}|{dedupe_key}')[:16]}", False
    initialize()
    event_id = str(uuid.uuid4())
    stamp = now_iso()
    connection = connect()
    try:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO outbox(
              event_id,event_type,dedupe_key,safe_payload_json,status,created_at,updated_at
            ) VALUES(?,?,?,?,'pending',?,?)
            """,
            (event_id, event_type, dedupe_key, payload, stamp, stamp),
        )
        connection.commit()
        if cursor.rowcount:
            return event_id, True
        row = connection.execute("SELECT event_id FROM outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        return str(row["event_id"]), False
    finally:
        connection.close()


def mark_outbox(
    event_id: str,
    status: str,
    *,
    receipt_hash: Optional[str] = None,
    error_code: Optional[str] = None,
) -> None:
    if status not in {"pending", "delivered", "uncertain", "failed", "superseded"}:
        raise SyncV2Error("outbox_status", "invalid outbox status")
    stamp = now_iso()
    connection = connect()
    try:
        connection.execute(
            """
            UPDATE outbox SET status=?,attempts=attempts+1,updated_at=?,
              delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END,
              receipt_hash=COALESCE(?,receipt_hash),last_error_code=?
            WHERE event_id=? AND (status!='delivered' OR ?='delivered')
            """,
            (status, stamp, status, stamp, receipt_hash, error_code, event_id, status),
        )
        connection.commit()
    finally:
        connection.close()


def register_decision(
    *,
    recording_id: Optional[str],
    source_ref: str,
    candidate: dict[str, Any],
    candidate_memory_id: str,
    candidate_path: Path,
    related_memory_ids: list[str],
    related_hashes: dict[str, str],
    connection: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Create or refresh an unresolved decision and invalidate stale previews."""
    owns_connection = connection is None
    if owns_connection:
        initialize()
    database = connection or connect()
    try:
        candidate_json = canonical_json(candidate)
        candidate_hash = sha256_text(candidate_json)
        file_hash = sha256_file(candidate_path)
        related_ids_json = canonical_json(related_memory_ids)
        related_hashes_json = canonical_json(related_hashes)
        source_kind = classify_source_kind(recording_id, source_ref)
        severity = classify_decision_severity(candidate_json)
        stamp = now_iso()
        row = database.execute(
            "SELECT * FROM decisions WHERE candidate_memory_id=?",
            (candidate_memory_id,),
        ).fetchone()
        if not row:
            decision_id = str(uuid.uuid4())
            database.execute(
                """
                INSERT INTO decisions(
                  decision_id,recording_id,source_ref,candidate_json,candidate_hash,candidate_memory_id,
                  candidate_path,candidate_file_hash,related_memory_ids_json,related_hashes_json,status,
                  created_at,revision,source_kind,severity,snooze_until,current_content_hash,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,1,?,?,NULL,?,?)
                """,
                (
                    decision_id,
                    recording_id,
                    source_ref,
                    candidate_json,
                    candidate_hash,
                    candidate_memory_id,
                    str(candidate_path),
                    file_hash,
                    related_ids_json,
                    related_hashes_json,
                    stamp,
                    source_kind,
                    severity,
                    file_hash,
                    stamp,
                ),
            )
            if owns_connection:
                database.commit()
            return {"decision_id": decision_id, "created": True, "updated": False, "revision": 1}
        decision_id = str(row["decision_id"])
        if str(row["status"]) not in {"pending", "deferred"}:
            return {
                "decision_id": decision_id,
                "created": False,
                "updated": False,
                "revision": int(row["revision"] or 1),
                "resolved": True,
            }
        desired = (
            recording_id,
            source_ref,
            candidate_json,
            candidate_hash,
            str(candidate_path),
            file_hash,
            related_ids_json,
            related_hashes_json,
            source_kind,
            severity,
        )
        current = (
            row["recording_id"],
            str(row["source_ref"]),
            str(row["candidate_json"]),
            str(row["candidate_hash"]),
            str(row["candidate_path"]),
            str(row["candidate_file_hash"]),
            str(row["related_memory_ids_json"]),
            str(row["related_hashes_json"]),
            str(row["source_kind"]),
            str(row["severity"]),
        )
        if desired == current:
            return {
                "decision_id": decision_id,
                "created": False,
                "updated": False,
                "revision": int(row["revision"] or 1),
            }
        revision = int(row["revision"] or 1) + 1
        database.execute(
            """
            UPDATE decisions SET recording_id=?,source_ref=?,candidate_json=?,candidate_hash=?,
              candidate_path=?,candidate_file_hash=?,related_memory_ids_json=?,related_hashes_json=?,
              status='pending',resolution=NULL,resolved_at=NULL,revision=?,source_kind=?,severity=?,
              snooze_until=NULL,current_content_hash=?,updated_at=?
            WHERE decision_id=?
            """,
            (
                recording_id,
                source_ref,
                candidate_json,
                candidate_hash,
                str(candidate_path),
                file_hash,
                related_ids_json,
                related_hashes_json,
                revision,
                source_kind,
                severity,
                file_hash,
                stamp,
                decision_id,
            ),
        )
        database.execute("DELETE FROM decision_previews WHERE decision_id=?", (decision_id,))
        if owns_connection:
            database.commit()
        return {"decision_id": decision_id, "created": False, "updated": True, "revision": revision}
    finally:
        if owns_connection:
            database.close()


def start_action_run(action_type: str, *, trigger: str = "manual", requested_run_id: Optional[str] = None) -> str:
    if action_type not in {
        "sync_now", "review_now", "locked_review", "idle_review",
        "decision_notify", "decision_question", "case_conversation", "alert_conversation", "workbench_extract", "local_repair",
    } | CONTROL_ACTION_TYPES | BOUNDED_REPAIR_ACTION_TYPES:
        raise SyncV2Error("action_type", "invalid action type")
    if trigger not in {"manual", "automatic", "system"}:
        raise SyncV2Error("action_trigger", "invalid action trigger")
    initialize()
    run_id = str(uuid.UUID(requested_run_id)) if requested_run_id else str(uuid.uuid4())
    stamp = now_iso()
    connection = connect()
    try:
        connection.execute(
            "INSERT INTO action_runs(run_id,action_type,trigger,status,started_at,updated_at) VALUES(?,?,?,'running',?,?)",
            (run_id, action_type, trigger, stamp, stamp),
        )
        connection.commit()
        return run_id
    finally:
        connection.close()


def set_action_trigger(run_id: str, trigger: str) -> None:
    if trigger not in {"manual", "automatic", "system"}:
        raise SyncV2Error("action_trigger", "invalid action trigger")
    initialize()
    connection = connect()
    try:
        cursor = connection.execute(
            "UPDATE action_runs SET trigger=?,updated_at=? WHERE run_id=?",
            (trigger, now_iso(), run_id),
        )
        if cursor.rowcount != 1:
            raise SyncV2Error("action_missing", "action run not found")
        connection.commit()
    finally:
        connection.close()


def finish_action_run(
    run_id: str,
    status: str,
    *,
    result: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
    expected_status: Optional[str] = None,
) -> None:
    if status not in {"success", "noop", "partial", "failed", "deferred", "interrupted"}:
        raise SyncV2Error("action_status", "invalid action status")
    stamp = now_iso()
    connection = connect()
    try:
        if expected_status is not None:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT status FROM action_runs WHERE run_id=?', (run_id,)).fetchone()
            if not current or current['status'] != expected_status:
                connection.rollback()
                return
        if status in {"failed", "interrupted"}:
            connection.execute(
                """
                UPDATE action_steps SET status=?,error_code=COALESCE(error_code,?),
                  completed_at=COALESCE(completed_at,?),updated_at=?
                WHERE run_id=? AND status='running'
                """,
                (status, error_code, stamp, stamp, run_id),
            )
            connection.execute(
                """
                UPDATE action_steps SET status='skipped',completed_at=COALESCE(completed_at,?),updated_at=?
                WHERE run_id=? AND status IN ('queued','pending')
                """,
                (stamp, stamp, run_id),
            )
        elif status == "deferred":
            connection.execute(
                """
                UPDATE action_steps SET status='deferred',completed_at=COALESCE(completed_at,?),updated_at=?
                WHERE run_id=? AND status='running'
                """,
                (stamp, stamp, run_id),
            )
            connection.execute(
                """
                UPDATE action_steps SET status='skipped',completed_at=COALESCE(completed_at,?),updated_at=?
                WHERE run_id=? AND status='queued'
                """,
                (stamp, stamp, run_id),
            )
        elif status in {"success", "noop", "partial"}:
            connection.execute(
                """
                UPDATE action_steps SET status=?,completed_at=COALESCE(completed_at,?),updated_at=?
                WHERE run_id=? AND status='running'
                """,
                (status, stamp, stamp, run_id),
            )
            connection.execute(
                """
                UPDATE action_steps SET status='skipped',completed_at=COALESCE(completed_at,?),updated_at=?
                WHERE run_id=? AND status='queued'
                """,
                (stamp, stamp, run_id),
            )
        connection.execute(
            """
            UPDATE action_runs SET status=?,result_json=?,error_code=?,completed_at=?,updated_at=?
            WHERE run_id=?
            """,
            (status, canonical_json(result or {}), error_code, stamp, stamp, run_id),
        )
        connection.commit()
    finally:
        connection.close()


ACTION_STEP_STATUSES = {
    "queued",
    "pending",
    "running",
    "success",
    "noop",
    "partial",
    "failed",
    "skipped",
    "deferred",
    "interrupted",
}


def set_action_step(
    run_id: str,
    step_key: str,
    step_order: int,
    label: str,
    status: str,
    *,
    current_count: Optional[int] = None,
    total_count: Optional[int] = None,
    item_label: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
) -> None:
    """Persist privacy-bounded progress for the local Sync Center UI."""
    if status not in ACTION_STEP_STATUSES:
        raise SyncV2Error("action_step_status", "invalid action step status")
    if not step_key or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in step_key):
        raise SyncV2Error("action_step_key", "invalid action step key")
    if current_count is not None and int(current_count) < 0:
        raise SyncV2Error("action_step_count", "invalid current count")
    if total_count is not None and int(total_count) < 0:
        raise SyncV2Error("action_step_count", "invalid total count")
    safe_label = " ".join(str(item_label or "").split())[:160] or None
    safe_detail = canonical_json(detail or {})
    if len(safe_detail.encode("utf-8")) > 4096:
        raise SyncV2Error("action_step_detail", "action step detail is too large")
    stamp = now_iso()
    started_at = stamp if status == "running" else None
    completed_at = stamp if status in {"success", "noop", "partial", "failed", "skipped", "deferred", "interrupted"} else None
    connection = connect()
    try:
        connection.execute(
            """
            INSERT INTO action_steps(
              run_id,step_key,step_order,label,status,current_count,total_count,safe_item_label,
              safe_detail_json,error_code,started_at,completed_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,step_key) DO UPDATE SET
              step_order=excluded.step_order,label=excluded.label,status=excluded.status,
              current_count=COALESCE(excluded.current_count,action_steps.current_count),
              total_count=COALESCE(excluded.total_count,action_steps.total_count),
              safe_item_label=excluded.safe_item_label,
              safe_detail_json=excluded.safe_detail_json,error_code=excluded.error_code,
              started_at=CASE
                WHEN excluded.status='running' THEN COALESCE(action_steps.started_at,excluded.started_at)
                ELSE action_steps.started_at
              END,
              completed_at=CASE
                WHEN excluded.completed_at IS NOT NULL THEN excluded.completed_at
                WHEN excluded.status IN ('queued','pending','running') THEN NULL
                ELSE action_steps.completed_at
              END,
              updated_at=excluded.updated_at
            """,
            (
                run_id,
                step_key,
                int(step_order),
                " ".join(str(label).split())[:120],
                status,
                int(current_count) if current_count is not None else None,
                int(total_count) if total_count is not None else None,
                safe_label,
                safe_detail,
                error_code,
                started_at,
                completed_at,
                stamp,
            ),
        )
        connection.execute("UPDATE action_runs SET updated_at=? WHERE run_id=?", (stamp, run_id))
        connection.commit()
    finally:
        connection.close()


def list_action_steps(run_id: str) -> list[dict[str, Any]]:
    connection = connect_readonly()
    try:
        return [
            {
                "key": str(row["step_key"]),
                "order": int(row["step_order"]),
                "label": str(row["label"]),
                "status": str(row["status"]),
                "current": row["current_count"],
                "total": row["total_count"],
                "item_label": row["safe_item_label"],
                "detail": json.loads(str(row["safe_detail_json"] or "{}")),
                "error_code": row["error_code"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "updated_at": str(row["updated_at"]),
            }
            for row in connection.execute(
                "SELECT * FROM action_steps WHERE run_id=? ORDER BY step_order,step_key",
                (run_id,),
            )
        ]
    finally:
        connection.close()


def record_incident(incident_type: str, dedupe_key: str, safe_detail: dict[str, Any]) -> str:
    initialize()
    incident_id = str(uuid.uuid4())
    stamp = now_iso()
    connection = connect()
    try:
        connection.execute(
            """
            INSERT INTO incidents(incident_id,incident_type,dedupe_key,safe_detail_json,status,created_at,updated_at)
            VALUES(?,?,?,?, 'open',?,?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
              safe_detail_json=excluded.safe_detail_json,status='open',updated_at=excluded.updated_at,resolved_at=NULL
            """,
            (incident_id, incident_type, dedupe_key, canonical_json(safe_detail), stamp, stamp),
        )
        connection.commit()
        row = connection.execute("SELECT incident_id FROM incidents WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        return str(row["incident_id"])
    finally:
        connection.close()


HEALTH_DETAIL_FIELDS = {
    "phase",
    "reason",
    "auto_repair",
    "attempts",
    "timeout_seconds",
    "next_retry_at",
    "original_error_code",
    "rollback_error_code",
}


def safe_health_detail(value: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    detail: dict[str, Any] = {}
    for key in HEALTH_DETAIL_FIELDS:
        item = value.get(key)
        if isinstance(item, bool):
            detail[key] = item
        elif isinstance(item, int) and not isinstance(item, bool):
            detail[key] = max(0, min(item, 86400))
        elif isinstance(item, str) and item:
            detail[key] = item[:300]
    return detail


def set_component_health(
    component: str,
    healthy: bool,
    error_code: Optional[str] = None,
    *,
    safe_detail: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Emit only the first failure and the first subsequent recovery."""
    if not component or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in component):
        raise SyncV2Error("component_name", "invalid component name")
    initialize()
    key = f"health:{component}"
    connection = connect()
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        previous = json.loads(str(row["value"])) if row else {"status": "unknown"}
        current_status = "healthy" if healthy else "failed"
        stamp = now_iso()
        payload = {"status": current_status, "error_code": error_code if not healthy else None, "changed_at": stamp}
        if not healthy:
            payload.update(safe_health_detail(safe_detail))
        if previous.get("status") == current_status:
            if healthy:
                return None
            refreshed = dict(previous)
            refreshed.update(payload)
            refreshed["changed_at"] = previous.get("changed_at") or stamp
            refreshed.update(safe_health_detail(safe_detail))
            payload = refreshed
        connection.execute(
            """
            INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, canonical_json(payload), stamp),
        )
        connection.commit()
    finally:
        connection.close()
    if healthy and previous.get("status") == "failed":
        recovery_payload = {
            "component": component,
            "recovered_from_error_code": str(previous.get("error_code") or "unknown"),
            "repair_result": "recovered",
        }
        for key in ("phase", "reason", "auto_repair", "attempts", "timeout_seconds", "next_retry_at"):
            if key in previous:
                recovery_payload[key] = previous[key]
        event_id, _created = emit_outbox(
            "sync_recovery",
            f"component-recovery:{component}:{stamp}",
            recovery_payload,
        )
        return event_id
    if not healthy and previous.get("status") != "failed":
        failure_payload = {
            "component": component,
            "error_code": str(error_code or "unknown"),
        }
        failure_payload.update(safe_health_detail(safe_detail))
        event_id, _created = emit_outbox(
            "sync_failure",
            f"component-failure:{component}:{stamp}",
            failure_payload,
        )
        return event_id
    return None


def component_health_state(component: str) -> dict[str, Any]:
    if not component or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in component):
        raise SyncV2Error("component_name", "invalid component name")
    if not DATABASE.is_file():
        return {"status": "unknown"}
    connection = connect_readonly()
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key=?", (f"health:{component}",)).fetchone()
    finally:
        connection.close()
    if not row:
        return {"status": "unknown"}
    try:
        value = json.loads(str(row["value"]))
    except json.JSONDecodeError:
        return {"status": "unknown"}
    return value if isinstance(value, dict) else {"status": "unknown"}


def recover_component_if_error(component: str, expected_error_code: str) -> Optional[str]:
    """Recover only the named failure, never an unrelated newer incident."""
    current = component_health_state(component)
    if current.get("status") != "failed" or current.get("error_code") != expected_error_code:
        return None
    return set_component_health(component, True)


def status_snapshot() -> dict[str, Any]:
    if not DATABASE.is_file():
        initialize()
    connection = connect_readonly()
    try:
        return {
            "recordings": {
                str(row["status"]): int(row["count"])
                for row in connection.execute("SELECT status,COUNT(*) AS count FROM recordings GROUP BY status")
            },
            "summary_comparisons": {
                str(row["summary_comparison_status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT summary_comparison_status,COUNT(*) AS count FROM recordings GROUP BY summary_comparison_status"
                )
            },
            "publication_stages": {
                "full_transcript_summary_complete": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM recordings WHERE independent_summary_sha256 IS NOT NULL"
                    ).fetchone()[0]
                ),
                "native_summary_comparison_complete": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM recordings WHERE summary_comparison_status='compared'"
                    ).fetchone()[0]
                ),
                "publishable": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM recordings
                        WHERE status IN ('completed','no_business_change')
                          AND independent_summary_sha256 IS NOT NULL
                          AND summary_comparison_status='compared'
                          AND summary_comparison_sha256 IS NOT NULL
                        """
                    ).fetchone()[0]
                ),
            },
            "decisions": {
                str(row["status"]): int(row["count"])
                for row in connection.execute("SELECT status,COUNT(*) AS count FROM decisions GROUP BY status")
            },
            "decision_categories": {
                str(row["severity"]): int(row["count"])
                for row in connection.execute(
                    "SELECT severity,COUNT(*) AS count FROM decisions WHERE status IN ('pending','deferred') GROUP BY severity"
                )
            },
            "outbox": {
                str(row["status"]): int(row["count"])
                for row in connection.execute("SELECT status,COUNT(*) AS count FROM outbox GROUP BY status")
            },
            "open_incidents": int(
                connection.execute("SELECT COUNT(*) FROM incidents WHERE status='open'").fetchone()[0]
            ),
            "database_integrity": str(connection.execute("PRAGMA quick_check").fetchone()[0]),
            "secure_delete": int(connection.execute("PRAGMA secure_delete").fetchone()[0]),
            "latest_actions": {
                str(row["action_type"]): {
                    "status": str(row["status"]),
                    "started_at": str(row["started_at"]),
                    "completed_at": row["completed_at"],
                    "error_code": row["error_code"],
                }
                for row in connection.execute(
                    """
                    SELECT a.* FROM action_runs a
                    JOIN (
                      SELECT action_type,MAX(started_at) AS started_at FROM action_runs GROUP BY action_type
                    ) latest ON latest.action_type=a.action_type AND latest.started_at=a.started_at
                    """
                )
            },
        }
    finally:
        connection.close()
