#!/usr/bin/env python3
"""Preserve-current resolution of an explicitly approved, source-bound journal."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import re
import uuid
from pathlib import Path
from typing import Any

import dingtalk_batch_contract as contract


class RecoveryError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.message = "现版、原失败批次和消息均已保留；本次恢复证据不足。"


def memory():
    import dingtalk_memory_automation
    return dingtalk_memory_automation


def approval_path(config: dict, old_batch: str) -> Path:
    return memory().runtime_paths(config)["runs"] / old_batch / "preserve-current.json"


def load_approval(config: dict, old_batch: str) -> dict | None:
    path = approval_path(config, old_batch)
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    connection = memory().connect_database(config)
    try:
        row = connection.execute("SELECT approval_sha256 FROM recovery_resolutions WHERE old_batch_id=?", (old_batch,)).fetchone()
    finally:
        connection.close()
    if not row or row[0] != contract.digest(value):
        raise RecoveryError("preserve_current_approval_corrupt")
    journal = path.with_name("apply_journal.json")
    if memory().sha256_path(journal) != value["journal_sha256"]:
        raise RecoveryError("preserve_current_journal_changed")
    return value


def completion_verified(config: dict, old_batch: str) -> bool:
    approval = load_approval(config, old_batch)
    if not approval:
        return False
    connection = memory().connect_database(config)
    try:
        row = connection.execute("SELECT state,completion_sha256 FROM recovery_resolutions WHERE old_batch_id=?", (old_batch,)).fetchone()
        if not row or row["state"] != "verified":
            return False
        path = approval_path(config, old_batch).with_name("preserve-current-result.json")
        result = json.loads(path.read_text())
        return (contract.digest(result) == row["completion_sha256"] and result.get("full_validation_passed") is True
                and result.get("child_batch_id") == approval["child_batch_id"])
    finally:
        connection.close()


def closure_evidence(config: dict, alert: dict, root: Path, current) -> dict | None:
    """Prove an existing conflict is resolved; an empty scan is not proof."""
    import automatic_memory_validation as validation
    import business_sync_state as state
    import validation_evidence
    m = memory()
    observed = [state.parse_time(alert.get(key)) for key in ('first_seen', 'last_seen')]
    after = max((value for value in observed if value is not None), default=None)
    if after is None or m.recovery_conflicts(config):
        return None
    verified = []
    connection = m.connect_database(config)
    try:
        rows = connection.execute('SELECT old_batch_id,child_batch_id FROM recovery_resolutions WHERE state=?', ('verified',)).fetchall()
        for row in rows:
            old = row['old_batch_id']
            if not completion_verified(config, old):
                return None
            path = approval_path(config, old).with_name('preserve-current-result.json')
            result = json.loads(path.read_text())
            completed = state.parse_time(result.get('completed_at'))
            if completed is None or completed > current:
                return None
            if completed <= after:
                continue
            batch = connection.execute('SELECT status,message_count FROM semantic_batches WHERE batch_id=?', (row['child_batch_id'],)).fetchone()
            queue = connection.execute('SELECT q.state,q.batch_id FROM batch_messages b JOIN semantic_queue q ON q.queue_id=b.queue_id WHERE b.batch_id=?', (row['child_batch_id'],)).fetchall()
            if (not batch or batch['status'] != 'applied' or len(queue) != batch['message_count']
                    or any(item['state'] not in {'applied', 'no_business_change'} or item['batch_id'] != row['child_batch_id'] for item in queue)):
                return None
            verified.append({'old_batch_id': old, 'child_batch_id': row['child_batch_id'],
                             'completion_sha256': m.sha256_path(path), 'completed_at': result['completed_at']})
    finally:
        connection.close()
    if not verified:
        return None
    success_path = root / 'state/last-memory-validation-success.json'
    try:
        success = validation.read_object(success_path)
        failure = validation.read_object(root / 'state/last-memory-validation-failure.json')
    except validation.ValidationFault:
        return None
    succeeded = state.parse_time(success.get('recorded_at'))
    failed = state.parse_time(failure.get('recorded_at'))
    newest_commit = max(state.parse_time(row['completed_at']) for row in verified)
    if (succeeded is None or succeeded > current or succeeded <= after or succeeded < newest_commit
            or (failure and failed is None) or (failed and succeeded <= failed)):
        return None
    try:
        indexes = validation.index_state(root)
    except validation.ValidationFault:
        return None
    if not validation_evidence.receipt_is_current(root, success, indexes['dirty'], indexes['applied']):
        return None
    return {'schema_version': 1, 'checked_at': state.now_iso(current), 'alert_id': alert['alert_id'],
            'completion_receipts': verified, 'full_validation_receipt_sha256': m.sha256_path(success_path),
            'full_validation_at': success['recorded_at'], 'current_validation': True,
            'original_failure_retained': True}


def mapped_targets(config: dict, old_batch: str) -> set[Path]:
    approval = load_approval(config, old_batch)
    return {Path(row["current"]).resolve() for row in approval["mapping"]} if approval else set()


def permits_child(config: dict, old_batch: str, child_batch: str, targets: set[Path]) -> bool:
    approval = load_approval(config, old_batch)
    if not approval or approval["child_batch_id"] != child_batch:
        return False
    m = memory()
    if m.batch_source_refs(config, child_batch) != approval["source_refs"]:
        raise RecoveryError("preserve_current_source_changed")
    expected = approval["target_hashes"]
    if not targets <= {Path(path).resolve() for path in expected}:
        raise RecoveryError("preserve_current_target_changed")
    if any(m.sha256_path(Path(path)) != value for path, value in expected.items()):
        raise RecoveryError("preserve_current_content_changed")
    for row in approval["mapping"]:
        if row.get("index") and m.sha256_path(Path(row["index"])) != row["index_sha256"]:
            raise RecoveryError("preserve_current_mapping_changed")
    return True


def candidate_override(config: dict, child_batch: str, candidate: dict) -> dict:
    connection = memory().connect_database(config)
    try:
        row = connection.execute("SELECT old_batch_id FROM recovery_resolutions WHERE child_batch_id=?", (child_batch,)).fetchone()
    finally:
        connection.close()
    if not row:
        return candidate
    approval = load_approval(config, row[0])
    if len(approval["mapping"]) != 1 or candidate["action"] != "annotate_page":
        raise RecoveryError("preserve_current_manual_review")
    vault = memory().runtime_paths(config)["memory_root"].parent.resolve()
    return {**candidate, "target_page": str(Path(approval["mapping"][0]["current"]).relative_to(vault))}


def resolve_moved_target(config: dict, target: Path) -> dict:
    m = memory()
    if target.is_file() and not target.is_symlink():
        return {"old": str(target), "current": str(target), "index": None, "index_sha256": None}
    index = target.parent.parent / "00_索引.md"
    if not index.is_file() or index.is_symlink():
        raise RecoveryError("preserve_current_mapping_missing")
    candidates = set()
    for line in index.read_text().splitlines():
        # A filename match alone is not authority: require the project's
        # explicit current-entry table and a unique valid link in that row.
        if not line.startswith("|") or "当前" not in line:
            continue
        for match in re.finditer(r"\[\[([^]|]+)(?:\|[^]]*)?\]\]", line):
            relative = match.group(1)
            path = (index.parent / relative).resolve()
            if path.name == target.name and path.is_file() and not path.is_symlink() and m.safe_target(m.runtime_paths(config)["memory_root"], path):
                candidates.add(path)
    if len(candidates) != 1:
        raise RecoveryError("preserve_current_mapping_ambiguous")
    return {"old": str(target), "current": str(next(iter(candidates))),
            "index": str(index), "index_sha256": m.sha256_path(index)}


def approve_current(config: dict, old_batch: str, action_id: str) -> dict:
    m = memory()
    orphan = None
    try:
        previous = load_approval(config, old_batch)
    except RecoveryError as exc:
        if exc.code != "preserve_current_approval_corrupt":
            raise
        connection = m.connect_database(config)
        try:
            if connection.execute("SELECT 1 FROM recovery_resolutions WHERE old_batch_id=?", (old_batch,)).fetchone():
                raise
        finally:
            connection.close()
        orphan = json.loads(approval_path(config, old_batch).read_text())
        if orphan.get("action_id") != action_id:
            raise
        previous = None
    if previous:
        return previous
    journal_path = m.runtime_paths(config)["runs"] / old_batch / "apply_journal.json"
    journal = json.loads(journal_path.read_text())
    if journal.get("state") != "recovery_conflict":
        raise RecoveryError("preserve_current_not_conflicted")
    indexes = m.derived_index_targets(m.runtime_paths(config)["memory_root"])
    mapping, hashes = [], {}
    for entry in journal["paths"]:
        target = Path(entry["target"])
        if not m.safe_target(m.runtime_paths(config)["memory_root"], target):
            raise RecoveryError("preserve_current_unsafe_target")
        if entry.get("existed"):
            backup = Path(entry["backup"])
            if (backup.is_symlink() or not backup.resolve().is_relative_to((journal_path.parent / "backup").resolve())
                    or m.sha256_path(backup) != entry.get("before_sha256")):
                raise RecoveryError("preserve_current_backup_invalid")
        if target.resolve() in indexes:
            continue
        if entry.get("recovery_status") == "conflict_preserved":
            item = resolve_moved_target(config, target)
            mapping.append(item)
            target = Path(item["current"])
        hashes[str(target)] = m.sha256_path(target)
    if len(mapping) != 1:
        raise RecoveryError("preserve_current_manual_review")
    source_refs = m.batch_source_refs(config, old_batch)
    if not source_refs or source_refs != journal.get("source_refs"):
        raise RecoveryError("preserve_current_source_changed")
    journal_hash = m.sha256_path(journal_path)
    approval = {"schema_version": 1, "policy": "preserve_current_only_missing", "old_batch_id": old_batch,
                "child_batch_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "preserve-current:" + old_batch + ":" + journal_hash)),
                "action_id": action_id, "approved_at": m.now_iso(), "journal_sha256": journal_hash,
                "mapping": mapping, "target_hashes": hashes, "source_refs": source_refs}
    if orphan:
        approval["approved_at"] = orphan.get("approved_at")
        if approval != orphan:
            raise RecoveryError("preserve_current_approval_corrupt")
    connection = m.connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        contract.save(approval_path(config, old_batch), approval)
        connection.execute("INSERT INTO recovery_resolutions(old_batch_id,child_batch_id,approval_sha256,state,created_at) VALUES(?,?,?,'approved',?)",
                           (old_batch, approval["child_batch_id"], contract.digest(approval), m.now_iso()))
        connection.commit()
    finally:
        connection.close()
    return approval


def source_placeholder_kind(text: str, resource_refs_json: str) -> str | None:
    if text.startswith("发到聊天里的文件已保存"):
        return "system_notice_no_business_change"
    match = re.fullmatch(r"\[图片消息\]\(mediaId=([^\s()]+)\)", text)
    if match:
        refs = json.loads(resource_refs_json or "[]")
        if any(isinstance(row, dict) and row.get("type") == "mediaId"
               and row.get("resourceId") == match.group(1) for row in refs):
            return "media_reference_only_content_not_inferred"
    return None


def prepare_child(config: dict, approval: dict) -> tuple[dict, dict]:
    m = memory()
    old, child = approval["old_batch_id"], approval["child_batch_id"]
    connection = m.connect_database(config)
    try:
        existing = connection.execute("SELECT * FROM semantic_batches WHERE batch_id=?", (child,)).fetchone()
        directory = m.runtime_paths(config)["runs"] / child
        if existing:
            manifest = contract.load(directory, connection, child)
            proposal = json.loads((directory / "proposal.json").read_text())
            if m.sha256_path(directory / "proposal.json") != existing["proposal_sha256"]:
                raise RecoveryError("preserve_current_proposal_invalid")
            if existing["status"] != "applied":
                queue = connection.execute("SELECT q.state,q.batch_id FROM batch_messages b JOIN semantic_queue q ON q.queue_id=b.queue_id WHERE b.batch_id=?", (child,)).fetchall()
                if len(queue) != existing["message_count"] or any(row["state"] not in {"claimed", "failed"} or row["batch_id"] not in {None, child} for row in queue):
                    raise RecoveryError("preserve_current_source_changed")
                connection.execute("UPDATE semantic_queue SET state='claimed',batch_id=? WHERE queue_id IN (SELECT queue_id FROM batch_messages WHERE batch_id=?)", (child, child))
                connection.execute("UPDATE semantic_batches SET status='model_complete' WHERE batch_id=?", (child,))
                connection.commit()
            return {"batch_id": child, "document": manifest["document"], "message_count": existing["message_count"]}, proposal
        original = connection.execute("SELECT proposal_sha256 FROM semantic_batches WHERE batch_id=?", (old,)).fetchone()
        proposal_path = directory.parent / old / "proposal.json"
        if not original or m.sha256_path(proposal_path) != original[0]:
            raise RecoveryError("preserve_current_proposal_invalid")
        proposal = json.loads(proposal_path.read_text())
        if proposal.get("claim_relations") or any(item["action"] != "annotate_page" for item in proposal["candidates"]):
            raise RecoveryError("preserve_current_manual_review")
        rows = connection.execute(
            "SELECT q.queue_id,q.message_id,q.content_sha256,q.change_kind,q.queued_at,q.state,"
            "m.content_sha256 AS current_sha256,m.conversation_id,m.create_time,m.sender_name,m.text_redacted,m.resource_refs_json,m.current_visible,c.conversation_name "
            "FROM batch_messages b JOIN semantic_queue q ON q.queue_id=b.queue_id JOIN messages m ON m.message_id=q.message_id "
            "JOIN conversations c ON c.conversation_id=m.conversation_id WHERE b.batch_id=? ORDER BY b.ordinal", (old,),
        ).fetchall()
        if not rows or any(row["state"] != "deferred" or row["content_sha256"] != row["current_sha256"] for row in rows):
            raise RecoveryError("preserve_current_source_changed")
        used = set((proposal.get("source_summary") or {}).get("source_tokens", []))
        used.update(token for item in proposal["candidates"] for token in item["source_tokens"])
        coverage = []
        for ordinal, row in enumerate(rows, 1):
            token = f"S{ordinal:03d}"
            placeholder = source_placeholder_kind(str(row["text_redacted"]), row["resource_refs_json"])
            if token not in used and not placeholder:
                raise RecoveryError("preserve_current_source_coverage_missing")
            coverage.append({"source_token": token, "content_sha256": row["content_sha256"],
                             "handling": "evidence_reference" if token in used else placeholder})
        current_text = Path(approval["mapping"][0]["current"]).read_text()
        proposal["candidates"] = [item for item in proposal["candidates"]
                                 if not (item["summary"] in current_text and item["evidence_boundary"] in current_text)]
        proposal["batch_id"] = child
        proposal["no_business_change"] = not proposal["candidates"] and proposal.get("source_summary") is None
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO semantic_batches(batch_id,created_at,status,message_count,text_chars) VALUES(?,?,'prepared',?,0)",
                           (child, m.now_iso(), len(rows)))
        for ordinal, row in enumerate(rows, 1):
            connection.execute("INSERT INTO batch_messages VALUES(?,?,?,?)", (child, row["queue_id"], ordinal, f"S{ordinal:03d}"))
            connection.execute("UPDATE semantic_queue SET state='claimed',batch_id=?,last_attempt_at=? WHERE queue_id=?", (child, m.now_iso(), row["queue_id"]))
        document, chars = m.build_batch_document(config, connection, child, rows, set())
        frozen = directory / "input-manifest.json"
        if frozen.is_file():
            prior = json.loads(frozen.read_text())
            if (prior.get("batch_id") != child or prior.get("sources") != contract.source_bindings(connection, child)
                    or contract.digest(prior.get("document")) != prior.get("document_sha256")):
                raise RecoveryError("preserve_current_source_changed")
            document = prior["document"]
        manifest_hash = contract.freeze(directory, connection, child, document)
        text = json.dumps(proposal, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        m.atomic_write(directory / "proposal.json", text)
        contract.save(directory / "source-coverage.json", {"old_batch_id": old, "sources": coverage})
        connection.execute("UPDATE semantic_batches SET status='model_complete',input_manifest_sha256=?,proposal_sha256=?,text_chars=? WHERE batch_id=?",
                           (manifest_hash, m.sha256_text(text), chars, child))
        connection.commit()
        return {"batch_id": child, "document": document, "message_count": len(rows)}, proposal
    finally:
        connection.close()


def run(config: dict, action_id: str) -> dict[str, Any]:
    m = memory()
    conflicts = m.recovery_conflicts(config)
    if not conflicts:
        return {"status": "noop", "message": "没有尚未处理的回退冲突。"}
    if len(conflicts) != 1:
        raise RecoveryError("preserve_current_multiple_conflicts")
    old = str(conflicts[0]["batch_id"])
    lock = m.acquire_lock(m.HOURLY_LOCK)
    try:
        approval = approve_current(config, old, action_id)
        batch, proposal = prepare_child(config, approval)
        m.validate_proposal(config, batch["batch_id"], proposal, check_context=False)
        result = m.apply_proposal(config, batch, proposal)
        completion = {"schema_version": 1, "policy": approval["policy"], "old_batch_id": old,
                      "child_batch_id": batch["batch_id"], "full_validation_passed": result.get("validated") is True,
                      "completed_at": m.now_iso(), "result": result,
                      "original_journal_sha256": m.sha256_path(approval_path(config, old).with_name("apply_journal.json"))}
        if not completion["full_validation_passed"] or completion["original_journal_sha256"] != approval["journal_sha256"]:
            raise RecoveryError("preserve_current_completion_unverified")
        contract.save(approval_path(config, old).with_name("preserve-current-result.json"), completion)
        connection = m.connect_database(config)
        try:
            connection.execute("UPDATE recovery_resolutions SET state='verified',completion_sha256=?,completed_at=? WHERE old_batch_id=?",
                               (contract.digest(completion), m.now_iso(), old))
            connection.commit()
        finally:
            connection.close()
        m.recover_apply_journals(config)
        return {"status": "success", "messages": batch["message_count"], "writes": result["writes"],
                "original_failure_retained": True, "policy": approval["policy"], "batch_id": batch["batch_id"]}
    finally:
        lock.close()
