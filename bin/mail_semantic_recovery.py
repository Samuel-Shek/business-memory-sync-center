"""Reconcile mail semantic *queue* versions, never replay memory transactions.

Call under email_sync's existing run lock, after email_phases.reconcile. This
module owns no scheduler/worker and invokes no model, recovery or memory writer.
The caller must suppress the legacy ensure_semantic_groups creation path for
these conversations: its body-only key does not implement this version contract.

created/superseded count groups, blocked counts scopes. blocked_semantic_ids and
blocked_scopes expose only opaque keys, task IDs and error codes. block_all is a
fail-closed signal when the queue cannot be read reliably; callers must skip the
semantic processor in that case. Block reasons and replacement links are also
metadata in the existing mail meta table.
All applied history, message states, attachments, caches and retry budgets stay
unchanged. A failure of the audit write is logged without mail content.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import uuid
from typing import Any


VERSION = "mail-semantic-v1"
ACTIVE = {"pending", "failed"}
REVIEWABLE = ACTIVE | {"superseded"}
LOG = logging.getLogger(__name__)


class Blocked(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def api():
    import email_sync
    return email_sync


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _scope(row: dict[str, Any]) -> tuple[str, str, str]:
    return (row["mailbox"], "conversation" if row.get("conversation_id") else "message",
            str(row.get("conversation_id") or row["message_key"]))


def _group_key(scope: tuple[str, str, str]) -> str:
    mailbox, kind, identifier = scope
    return api().conversation_key(mailbox, identifier if kind == "conversation" else "", identifier)


def _discover(config):
    s = api()
    c = s.connect_database_readonly(config)
    try:
        c.execute("BEGIN")
        messages = [dict(r) for r in c.execute(
            "SELECT message_key,mailbox,conversation_id,archive_state,memory_state FROM messages")]
        groups = [dict(r) for r in c.execute(
            "SELECT semantic_id,group_key,status FROM semantic_groups WHERE status IN ('pending','failed')")]
        links = [dict(r) for r in c.execute(
            "SELECT l.semantic_id,l.message_key FROM semantic_group_messages l "
            "JOIN semantic_groups g ON g.semantic_id=l.semantic_id WHERE g.status IN ('pending','failed')")]
    finally:
        c.close()
    preapplied = set(config["email_sync"].get("preapplied_message_keys", []))
    by_key = {r["message_key"]: r for r in messages}
    by_group = {_group_key(_scope(r)): _scope(r) for r in messages}
    scopes = {_scope(r) for r in messages if r["archive_state"] == "archived"
              and r["memory_state"] == "pending" and r["message_key"] not in preapplied}
    orphans = []
    for group in groups:
        linked = [by_key[l["message_key"]] for l in links
                  if l["semantic_id"] == group["semantic_id"] and l["message_key"] in by_key]
        found = {_scope(r) for r in linked}
        if group["group_key"] in by_group:
            found.add(by_group[group["group_key"]])
        scopes.update(found)
        if not found:
            orphans.append(group["semantic_id"])
    return sorted(scopes), orphans


def _snapshot(c, scope):
    mailbox, kind, identifier = scope
    field = "conversation_id" if kind == "conversation" else "message_key"
    rows = [dict(r) for r in c.execute(
        f"SELECT * FROM messages WHERE mailbox=? AND {field}=? ORDER BY received_at,message_key",
        (mailbox, identifier))]
    keys = [r["message_key"] for r in rows]
    placeholders = ",".join("?" for _ in keys) or "NULL"
    groups = [dict(r) for r in c.execute(
        f"SELECT * FROM semantic_groups WHERE group_key=? OR semantic_id IN "
        f"(SELECT semantic_id FROM semantic_group_messages WHERE message_key IN ({placeholders})) ORDER BY semantic_id",
        (_group_key(scope), *keys))]
    gids = [g["semantic_id"] for g in groups]
    gp = ",".join("?" for _ in gids) or "NULL"
    links = [dict(r) for r in c.execute(
        f"SELECT * FROM semantic_group_messages WHERE semantic_id IN ({gp}) ORDER BY semantic_id,source_token", gids)]
    attachments = [dict(r) for r in c.execute(
        f"SELECT * FROM attachments WHERE message_key IN ({placeholders}) ORDER BY message_key,attachment_id", keys)]
    has_jobs = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachment_jobs'").fetchone()
    jobs = [dict(r) for r in c.execute(
        f"SELECT * FROM attachment_jobs WHERE message_key IN ({placeholders}) ORDER BY message_key,attachment_id_hash", keys)] if has_jobs else []
    batch_ids = sorted({r["first_batch_id"] for r in rows} | {g["batch_id"] for g in groups})
    bp = ",".join("?" for _ in batch_ids) or "NULL"
    # Only existence is relevant. Report delivery counters are not semantic input.
    batches = [r[0] for r in c.execute(f"SELECT batch_id FROM batches WHERE batch_id IN ({bp}) ORDER BY batch_id", batch_ids)]
    return {"rows": rows, "groups": groups, "links": links, "attachments": attachments,
            "jobs": jobs, "batches": batches}


def _read_snapshot(config, scope):
    c = api().connect_database_readonly(config)
    try:
        c.execute("BEGIN")
        return _snapshot(c, scope)
    finally:
        c.close()


def _eligible(config, snapshot):
    excluded = set(config["email_sync"].get("preapplied_message_keys", []))
    return [r for r in snapshot["rows"] if r["archive_state"] == "archived" and r["message_key"] not in excluded]


def _input(config, scope, snapshot):
    s = api()
    rows = _eligible(config, snapshot)
    if not rows:
        raise Blocked("semantic_sources_unavailable")
    all_keys = {r["message_key"] for r in rows}
    # A moved, deleted, unarchived or preapplied member cannot silently disappear.
    if any(l["message_key"] not in all_keys for l in snapshot["links"]
           if any(g["semantic_id"] == l["semantic_id"] and g["status"] in REVIEWABLE for g in snapshot["groups"])):
        raise Blocked("semantic_members_unavailable")
    s.email_phases.assert_current(config, rows)
    document, _ = s.build_semantic_document(config, rows, VERSION)
    stable_sha = s.semantic_input_sha({**document, "batch_id": VERSION})
    # Identities matter even when two different messages have identical bodies.
    identities = [{key: r.get(key) for key in
                   ("message_key", "mailbox", "message_id", "conversation_id", "body_sha256",
                    "manifest_sha256", "phase_fingerprint")} for r in rows]
    version = _hash({"contract": VERSION, "scope": scope, "members": identities, "input": stable_sha})
    semantic_id = str(uuid.uuid5(uuid.NAMESPACE_URL, VERSION + ":" + version))
    return rows, document, version, semantic_id


def _journals(config, groups, new_id):
    """Validate journal state only; never inspect targets, restore or reuse caches."""
    root = api().runtime_paths(config)["runs"]
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise Blocked("semantic_run_root_unsafe")
    known = {g["semantic_id"]: g for g in groups}
    for sid in sorted(set(known) | {new_id}):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            raise Blocked("semantic_run_id_unsafe")
        directory = root / sid
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise Blocked("semantic_run_path_unsafe")
        if sid not in known and directory.exists():
            raise Blocked("semantic_orphan_run_directory")
        transaction = directory / "memory-transaction"
        if transaction.is_symlink():
            raise Blocked("semantic_transaction_unproven")
        if not transaction.exists():
            continue
        journal = transaction / "transaction.json"
        if not transaction.is_dir() or journal.is_symlink() or not journal.is_file():
            raise Blocked("semantic_transaction_unproven")
        try:
            value = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise Blocked("semantic_transaction_unproven") from exc
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value.get("schema_version") not in {1, 2}
                or value.get("semantic_id") != sid):
            raise Blocked("semantic_transaction_unproven")
        state = value.get("state")
        if state == "committed":
            if known[sid]["status"] != "applied":
                raise Blocked("semantic_commit_unreconciled")
        elif state == 'superseded_after_reconciliation':
            import mail_legacy_recovery
            if not mail_legacy_recovery.verify_receipt(config, journal, value, groups):
                raise Blocked('semantic_legacy_receipt_unproven')
        elif state not in {"rolled_back", "rolled_back_after_recovery"}:
            raise Blocked("semantic_transaction_unfinished")


def _plan(config, scope, snapshot, rows, document, version, new_id):
    s = api()
    groups = snapshot["groups"]
    key = VERSION + ":" + version
    expected_links = [(r["message_key"], f"S{i:03d}") for i, r in enumerate(rows, 1)]
    matching = []
    for group in groups:
        links = [(l["message_key"], l["source_token"]) for l in snapshot["links"] if l["semantic_id"] == group["semantic_id"]]
        expected_sha = s.semantic_input_sha({**document, "batch_id": group["semantic_id"]})
        if links == expected_links and group["group_key"] == _group_key(scope) and group["input_sha256"] == expected_sha:
            matching.append(group)
    active = [g for g in groups if g["status"] in ACTIVE]
    pending = [r for r in rows if r["memory_state"] == "pending"]
    if not pending and not active:
        return None, []
    # Applied DB history plus pending source state is inconsistent, not permission
    # to repeat a committed write. It needs a separate evidence-based repair.
    if any(g["status"] == "applied" for g in matching):
        raise Blocked("semantic_applied_version_pending")
    valid = [g for g in matching if g["status"] in ACTIVE]
    if valid:
        # Prefer the content-version task; otherwise preserve one legacy task and
        # its failed retry/cache budget instead of repeatedly paying for a model.
        keeper = min(valid, key=lambda g: (g["semantic_key"] != key, g["created_at"], g["semantic_id"]))
        return None, [g for g in active if g["semantic_id"] != keeper["semantic_id"]]
    collision = next((g for g in groups if g["semantic_key"] == key or g["semantic_id"] == new_id), None)
    if collision:
        raise Blocked("semantic_version_retired_or_inconsistent")
    if not pending:
        # All messages already applied but a stale task still requests execution.
        # Supersede it only when an applied task proves this exact input above;
        # otherwise do not guess which business write actually happened.
        raise Blocked("semantic_pending_task_without_pending_mail")
    batch = pending[0]["first_batch_id"]
    if batch not in snapshot["batches"]:
        raise Blocked("semantic_batch_unavailable")
    return {"semantic_id": new_id, "semantic_key": key, "group_key": _group_key(scope),
            "source_ref": "email-conversation:" + _group_key(scope), "batch_id": batch,
            "input_sha256": s.semantic_input_sha({**document, "batch_id": new_id})}, active


def _audit(c, scope, payload):
    key = VERSION + ":audit:" + _hash(scope)
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, _json({"schema_version": 1, "recorded_at": api().now_iso(), **payload})))


def _blocked(config, scope, exc):
    code = str(getattr(exc, "code", type(exc).__name__))
    if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", code):
        code = "semantic_recovery_blocked"
    c = None
    try:
        c = api().connect_database(config)
        c.execute("BEGIN IMMEDIATE")
        _audit(c, scope, {"status": "blocked", "reason_code": code})
        c.commit()
    except (OSError, sqlite3.Error, RuntimeError):
        LOG.warning("mail semantic recovery audit unavailable: %s", code)
    finally:
        if c is not None:
            c.close()
    return code


def _reconcile_scope(config, scope):
    s = api()
    snapshot = _read_snapshot(config, scope)
    rows, document, version, new_id = _input(config, scope, snapshot)
    _journals(config, snapshot["groups"], new_id)
    new, retired = _plan(config, scope, snapshot, rows, document, version, new_id)
    if new is None and not retired:
        return {"created": 0, "superseded": 0, "blocked": 0}
    c = s.connect_database(config)
    try:
        c.execute("BEGIN IMMEDIATE")
        # Compare full relevant row versions, group states/membership and jobs;
        # timestamp-only checks miss same-second updates and ignored decisions.
        current = _snapshot(c, scope)
        if current != snapshot:
            raise Blocked("semantic_db_versions_changed")
        _, _, checked_version, checked_id = _input(config, scope, current)
        if checked_version != version or checked_id != new_id:
            raise Blocked("semantic_input_changed_during_recovery")
        _journals(config, current["groups"], new_id)
        stamp = s.now_iso()
        if new:
            c.execute("INSERT INTO semantic_groups(semantic_id,semantic_key,group_key,source_ref,batch_id,input_sha256,created_at) "
                      "VALUES(:semantic_id,:semantic_key,:group_key,:source_ref,:batch_id,:input_sha256,:created_at)",
                      {**new, "created_at": stamp})
            for i, row in enumerate(rows, 1):
                c.execute("INSERT INTO semantic_group_messages(semantic_id,message_key,source_token) VALUES(?,?,?)",
                          (new_id, row["message_key"], f"S{i:03d}"))
        for group in retired:
            count = c.execute("UPDATE semantic_groups SET status='superseded',last_error_code='semantic_input_rebuilt',completed_at=? "
                              "WHERE semantic_id=? AND status=? AND input_sha256=?",
                              (stamp, group["semantic_id"], group["status"], group["input_sha256"])).rowcount
            if count != 1:
                raise Blocked("semantic_db_versions_changed")
        _audit(c, scope, {"status": "queue_reconciled", "input_version": version,
                         "created_id": new_id if new else None,
                         "superseded_ids": [g["semantic_id"] for g in retired],
                         "previous_errors": {g["semantic_id"]: g["last_error_code"] for g in retired}})
        c.commit()
        return {"created": int(new is not None), "superseded": len(retired), "blocked": 0}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def reconcile(config) -> dict[str, Any]:
    """Build verified queue versions; blocked scopes retain their original records.

    Caller owns the normal mail run lock. No in-call retries: changed snapshots
    wait for a future ordinary cycle. A blocked result is not a recovery receipt;
    do not pass blocked scopes to the legacy queue/model/recovery path.
    """
    result = {"created": 0, "superseded": 0, "blocked": 0,
              "blocked_semantic_ids": [], "blocked_scopes": [], "block_all": False}
    try:
        scopes, orphans = _discover(config)
    except (OSError, sqlite3.Error, RuntimeError, ValueError, KeyError, TypeError):
        # No schema initialization or database creation as a recovery shortcut.
        LOG.warning("mail semantic recovery cannot read queue metadata")
        return {**result, "blocked": 1, "block_all": True,
                "blocked_scopes": [{"group_key": _hash([VERSION, "unavailable"]),
                                    "error_code": "semantic_queue_unavailable", "semantic_ids": []}]}
    for orphan in orphans:
        scope = ("orphan", orphan)
        code = _blocked(config, scope, Blocked("semantic_orphan_sources_unavailable"))
        result["blocked_semantic_ids"].append(orphan)
        result["blocked_scopes"].append({"group_key": _hash(scope), "error_code": code, "semantic_ids": [orphan]})
        result["blocked"] += 1
    for scope in scopes:
        try:
            outcome = _reconcile_scope(config, scope)
        except (OSError, sqlite3.Error, RuntimeError, ValueError, KeyError, TypeError) as exc:
            code = _blocked(config, scope, exc)
            try:
                # Re-read to also exclude a task inserted during the failed CAS.
                snapshot = _read_snapshot(config, scope)
                ids = sorted(g["semantic_id"] for g in snapshot["groups"] if g["status"] in REVIEWABLE)
            except (OSError, sqlite3.Error, RuntimeError, ValueError, KeyError, TypeError):
                ids = []
                result["block_all"] = True
            result["blocked_semantic_ids"].extend(ids)
            result["blocked_scopes"].append({"group_key": _group_key(scope), "error_code": code, "semantic_ids": ids})
            result["blocked"] += 1
        else:
            for key in ("created", "superseded", "blocked"):
                result[key] += outcome[key]
    result["blocked_semantic_ids"] = sorted(set(result["blocked_semantic_ids"]))
    return result
