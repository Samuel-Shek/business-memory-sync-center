"""Staged DingTalk/Win11 handoff core. Not a scheduler or production writer.

Only trusted adapters may call this module. Message text and model output are
data: neither may select transports, executables, credentials or output paths.
The live DingTalk and Win11 adapters must be accepted before this is enabled.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any


STAGES = ("discovered", "queued", "win11_received", "parsed", "summarized",
          "m2_received", "memory_applied")
IDENTITY_FIELDS = ("organization", "profile", "conversation", "message",
                   "resource_type", "resource", "version")
ROUTABLE = {"encrypted", "protected", "unsupported_format", "local_parse_failed"}
RETRYABLE = {"win11_offline", "network_failed", "source_expired", "model_unavailable"}
MANUAL = {"password_required", "access_denied", "source_missing", "corrupt_file",
          "risk_file", "partial_content", "hash_mismatch", "identity_mismatch",
          "receipt_invalid", "summary_invalid", "unknown_failure"}
SHA = re.compile(r"^[a-f0-9]{64}$")
MAX_TEXT = 32 * 1024 * 1024


class Rejected(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(IDENTITY_FIELDS):
        raise Rejected("identity_mismatch")
    for field in IDENTITY_FIELDS:
        item = value[field]
        if not isinstance(item, str) or not item.strip() or len(item) > 2048 or any(ord(c) < 32 for c in item):
            raise Rejected("identity_mismatch")
    if value["resource_type"] not in {"fileId", "mediaId"}:
        raise Rejected("identity_mismatch")
    return dict(value)


def task_key(value: dict) -> str:
    return digest(canonical(identity(value)).encode())


def route(reason: str | None) -> str:
    # A resourceRefs.download.ready field is a capability, not read evidence.
    if reason in ROUTABLE:
        return "win11"
    if reason in {"network_failed", "source_expired"}:
        return "retry_source"
    if reason in {"access_denied", "source_missing"}:
        return "source_blocked"
    if reason in {"risk_file", "password_required", "corrupt_file"}:
        return "review"
    return "needs_read_evidence"


def validated_text(receipt: dict, source: dict, reader_sha: str, host: str) -> str:
    if not SHA.fullmatch(reader_sha) or not host:
        raise Rejected("receipt_invalid")
    if receipt.get("source") != identity(source) or receipt.get("task_key") != task_key(source):
        raise Rejected("identity_mismatch")
    if receipt.get("host") != host or receipt.get("reader_sha256") != reader_sha:
        raise Rejected("identity_mismatch")
    for flag in ("original_received_on_m2", "macros_executed", "scripts_executed", "authorization_bypassed"):
        if receipt.get(flag) is not False:
            raise Rejected("receipt_invalid")
    if receipt.get("schema_version") != 1 or receipt.get("source_access_verified") is not True:
        raise Rejected("receipt_invalid")
    raw_hash, view_hash = receipt.get("source_sha256"), receipt.get("view_sha256")
    if not isinstance(raw_hash, str) or not SHA.fullmatch(raw_hash) or not isinstance(view_hash, str) or not SHA.fullmatch(view_hash):
        raise Rejected("hash_mismatch")
    for field in ("source_bytes", "view_bytes"):
        if type(receipt.get(field)) is not int or not 0 <= receipt[field] <= 512 * 1024 * 1024:
            raise Rejected("receipt_invalid")
    # Enterprise file filters may expose an authorized view unlike raw bytes.
    # Both views must be bound; never relabel view bytes as the raw original.
    binding = receipt.get("view_binding")
    if binding not in {"same_bytes", "authorized_enterprise_view"}:
        raise Rejected("receipt_invalid")
    if binding == "same_bytes" and (raw_hash != view_hash or receipt["source_bytes"] != receipt["view_bytes"]):
        raise Rejected("hash_mismatch")
    if binding == "authorized_enterprise_view" and receipt.get("raw_rechecked_after_read") is not True:
        raise Rejected("receipt_invalid")
    coverage = receipt.get("coverage")
    if (not isinstance(coverage, dict) or coverage.get("complete") is not True
            or coverage.get("truncated") is not False or not isinstance(coverage.get("nodes"), list)
            or not coverage["nodes"] or any(not isinstance(n, dict) or n.get("status") != "complete" for n in coverage["nodes"])):
        raise Rejected("partial_content")
    text = receipt.get("text")
    if not isinstance(text, str) or len(text.encode()) > MAX_TEXT:
        raise Rejected("receipt_invalid")
    if receipt.get("text_sha256") != digest(text.encode()) or receipt.get("text_bytes") != len(text.encode()):
        raise Rejected("hash_mismatch")
    return text


def validated_summary(summary: dict, text: str, expected_model: str) -> dict:
    # The schema has no tool calls, filesystem paths, commands or approval bit.
    if not isinstance(summary, dict) or set(summary) != {"model", "text_sha256", "items"}:
        raise Rejected("summary_invalid")
    if summary["model"] != expected_model or summary["text_sha256"] != digest(text.encode()):
        raise Rejected("summary_invalid")
    if not isinstance(summary["items"], list) or len(summary["items"]) > 100:
        raise Rejected("summary_invalid")
    for item in summary["items"]:
        if not isinstance(item, dict) or set(item) != {"summary", "start", "end", "quote"}:
            raise Rejected("summary_invalid")
        start, end = item["start"], item["end"]
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or item["quote"] != text[start:end] or not isinstance(item["summary"], str)
                or not 0 < len(item["summary"]) <= 2000):
            raise Rejected("summary_invalid")
    return summary


class Ledger:
    """Private durable phases; late results and uncertain work cannot advance."""
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise Rejected("unsafe_root")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        database = self.root / "handoff.sqlite"
        if database.is_symlink():
            raise Rejected("unsafe_root")
        # Create private before SQLite opens it, regardless of process umask.
        fd = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(database, 0o600)
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=DELETE;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS tasks(
            key TEXT PRIMARY KEY, source TEXT NOT NULL, reason TEXT NOT NULL,
            stage TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            generation INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            next_due REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            error TEXT, notified_error TEXT, receipt TEXT, summary TEXT, memory_receipt TEXT);
          CREATE TABLE IF NOT EXISTS events(
            seq INTEGER PRIMARY KEY, key TEXT NOT NULL REFERENCES tasks(key),
            generation INTEGER NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, at REAL NOT NULL);
          CREATE VIRTUAL TABLE IF NOT EXISTS documents USING fts5(key UNINDEXED, text, summary);
        """)

    def close(self):
        self.db.close()

    def get(self, key: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE key=?", (key,)).fetchone()
        if row is None:
            raise Rejected("unknown_task")
        return dict(row)

    def _event(self, row: dict):
        self.db.execute("INSERT INTO events(key,generation,stage,status,at) VALUES(?,?,?,?,?)",
                        (row["key"], row["generation"], row["stage"], row["status"], time.time()))

    def discover(self, source: dict, reason: str) -> tuple[str, bool]:
        if route(reason) != "win11":
            raise Rejected("not_routable")
        key = task_key(source)
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO tasks(key,source,reason,stage) VALUES(?,?,?,'discovered')",
                                  (key, canonical(source), reason))
            if cur.rowcount:
                self._event(self.get(key))
        return key, bool(cur.rowcount)

    def claim(self, key: str, now: float | None = None) -> int | None:
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.get(key)
            if row["status"] == "running":
                if row["lease_until"] <= now:
                    # An expired worker may already have downloaded or called a
                    # model. Require its same-key receipt, not a blind retry.
                    self.db.execute("UPDATE tasks SET status='uncertain',error='worker_receipt_unknown' WHERE key=?", (key,))
                    self._event(self.get(key))
                return None
            if row["status"] not in {"pending", "retry"} or row["next_due"] > now:
                return None
            self.db.execute("UPDATE tasks SET generation=generation+1,status='running',lease_until=?,attempts=attempts+1 WHERE key=?", (now + 1800, key))
            self._event(self.get(key))
            return self.get(key)["generation"]

    def _owned(self, key: str, generation: int, stage: str | None = None) -> dict:
        row = self.get(key)
        if row["generation"] != generation or row["status"] != "running":
            raise Rejected("stale_worker")
        if stage and row["stage"] != stage:
            raise Rejected("invalid_transition")
        return row

    def advance(self, key: str, generation: int, next_stage: str):
        # Content-bearing and writer phases require specialized validation.
        if next_stage not in {"queued", "win11_received"}:
            raise Rejected("validated_transition_required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._owned(key, generation)
            if STAGES.index(next_stage) != STAGES.index(row["stage"]) + 1:
                raise Rejected("invalid_transition")
            self.db.execute("UPDATE tasks SET stage=? WHERE key=?", (next_stage, key))
            self._event(self.get(key))

    def parsed(self, key: str, generation: int, receipt: dict, reader_sha: str, host: str):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._owned(key, generation, "win11_received")
            validated_text(receipt, json.loads(row["source"]), reader_sha, host)
            self.db.execute("UPDATE tasks SET stage='parsed',receipt=? WHERE key=?", (canonical(receipt), key))
            self._event(self.get(key))

    def summarized(self, key: str, generation: int, summary: dict, model: str):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._owned(key, generation, "parsed")
            validated_summary(summary, json.loads(row["receipt"])["text"], model)
            self.db.execute("UPDATE tasks SET stage='summarized',summary=? WHERE key=?", (canonical(summary), key))
            self._event(self.get(key))

    def receive(self, key: str, generation: int, current_source: dict):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._owned(key, generation, "summarized")
            if identity(current_source) != json.loads(row["source"]):
                raise Rejected("identity_mismatch")
            receipt = json.loads(row["receipt"])
            # Fulltext and stage become visible in the same SQLite transaction.
            self.db.execute("INSERT INTO documents(key,text,summary) VALUES(?,?,?)", (key, receipt["text"], row["summary"]))
            self.db.execute("UPDATE tasks SET stage='m2_received' WHERE key=?", (key,))
            self._event(self.get(key))

    def memory_applied(self, key: str, generation: int, writer_receipt: dict):
        # This is called ONLY by the existing trusted writer adapter after it
        # verifies its real commit. Model-produced JSON is not a writer receipt.
        expected = {"task_key", "input_sha256", "transaction_id", "committed", "validation_passed"}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._owned(key, generation, "m2_received")
            if (set(writer_receipt) != expected or writer_receipt["task_key"] != key
                    or writer_receipt["input_sha256"] != digest(row["summary"].encode())
                    or not isinstance(writer_receipt["transaction_id"], str) or not writer_receipt["transaction_id"].strip()
                    or writer_receipt["committed"] is not True or writer_receipt["validation_passed"] is not True):
                raise Rejected("receipt_invalid")
            self.db.execute("UPDATE tasks SET stage='memory_applied',status='complete',error=NULL,memory_receipt=? WHERE key=?", (canonical(writer_receipt), key))
            self._event(self.get(key))

    def await_writer(self, key: str, generation: int):
        """Release a finished receive stage; never invent a memory receipt."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._owned(key, generation, "m2_received")
            self.db.execute("UPDATE tasks SET status='pending',lease_until=0 WHERE key=?", (key,))
            self._event(self.get(key))

    def fail(self, key: str, generation: int, code: str, uncertain: bool = False):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._owned(key, generation)
            if code not in RETRYABLE | MANUAL:
                code = "unknown_failure"
            status = "uncertain" if uncertain else "retry" if code in RETRYABLE else "review"
            delay = min(86400, 60 * 2 ** min(row["attempts"], 10))
            self.db.execute("UPDATE tasks SET status=?,error=?,next_due=? WHERE key=?", (status, code, time.time() + delay, key))
            self._event(self.get(key))

    def attention(self) -> list[dict]:
        # Read-only. A notifier must record delivery separately, after receipt.
        return [dict(r) for r in self.db.execute("SELECT key,error FROM tasks WHERE status IN ('review','uncertain') AND (notified_error IS NULL OR notified_error!=error)")]

    def attention_delivered(self, key: str, error: str):
        with self.db:
            self.db.execute("UPDATE tasks SET notified_error=? WHERE key=? AND error=? AND status IN ('review','uncertain')", (error, key, error))

    def status(self) -> dict:
        return {"quick_check": self.db.execute("PRAGMA quick_check").fetchone()[0],
                "counts": [dict(r) for r in self.db.execute("SELECT stage,status,count(*) AS count FROM tasks GROUP BY stage,status")],
                "production_enabled": False}
