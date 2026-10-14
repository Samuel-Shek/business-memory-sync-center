#!/usr/bin/env python3
"""Frozen per-batch evidence and output identifiers; contains no model runner."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

VERSION = 1


class ContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".batch-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_bindings(connection, batch_id: str) -> list[dict]:
    return [dict(row) for row in connection.execute(
        "SELECT b.queue_id,b.source_token,q.content_sha256,q.change_kind,"
        "m.content_sha256 AS current_sha256,m.current_visible "
        "FROM batch_messages b JOIN semantic_queue q ON q.queue_id=b.queue_id "
        "JOIN messages m ON m.message_id=q.message_id WHERE b.batch_id=? ORDER BY b.ordinal", (batch_id,),
    )]


def freeze(directory: Path, connection, batch_id: str, document: dict) -> str:
    manifest = {"schema_version": VERSION, "batch_id": batch_id, "document": document,
                "document_sha256": digest(document), "sources": source_bindings(connection, batch_id)}
    if (directory / "input-manifest.json").exists():
        previous = json.loads((directory / "input-manifest.json").read_text())
        if previous == manifest:
            return digest(previous)
        raise ContractError("proposal_context_already_frozen")
    save(directory / "input-manifest.json", manifest)
    return digest(manifest)


def load(directory: Path, connection, batch_id: str) -> dict | None:
    row = connection.execute("SELECT input_manifest_sha256 FROM semantic_batches WHERE batch_id=?", (batch_id,)).fetchone()
    expected = row[0] if row else None
    path = directory / "input-manifest.json"
    if not expected and not path.exists():
        return None  # Legacy input may only be reused when it has no relations.
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError("proposal_context_missing") from exc
    if (not expected or digest(manifest) != expected or manifest.get("schema_version") != VERSION
            or manifest.get("batch_id") != batch_id or digest(manifest.get("document")) != manifest.get("document_sha256")):
        raise ContractError("proposal_context_corrupt")
    if source_bindings(connection, batch_id) != manifest["sources"]:
        raise ContractError("proposal_source_drift")
    return manifest


def output_schema(base: dict, document: dict) -> dict:
    schema = json.loads(json.dumps(base))
    tokens = [str(row["source_token"]) for row in document["messages"]]
    handles = [str(row["claim_handle"]) for row in document.get("open_claims", [])]
    memories = [None] + [str(row["memory_id"]) for row in document.get("current_memory", [])]
    schema["properties"]["batch_id"] = {"type": "string", "enum": [document["batch_id"]]}
    def constrain(value):
        if isinstance(value, dict):
            properties = value.get("properties", {})
            if "source_tokens" in properties:
                properties["source_tokens"]["items"] = {"type": "string", "enum": tokens}
            if "claim_handle" in properties and handles:
                properties["claim_handle"] = {"type": "string", "enum": handles}
            if "existing_memory_id" in properties:
                properties["existing_memory_id"] = {"type": ["string", "null"], "enum": memories}
            for item in value.values():
                constrain(item)
        elif isinstance(value, list):
            for item in value:
                constrain(item)
    constrain(schema)
    if not handles:
        schema["properties"]["claim_relations"]["maxItems"] = 0
    return schema


def check_relations(raw: Any, document: dict | None, allowed_tokens: set[str]) -> set[str]:
    if not isinstance(raw, list) or len(raw) > 40:
        raise ContractError("proposal_relation_shape")
    if not raw:
        return set()
    if document is None:
        raise ContractError("proposal_context_missing")
    handles = {str(row["claim_handle"]) for row in document.get("open_claims", [])}
    seen = set()
    for relation in raw:
        if not isinstance(relation, dict):
            raise ContractError("proposal_relation_shape")
        handle = relation.get("claim_handle")
        if handle not in handles:
            raise ContractError("proposal_claim_unknown")
        tokens = relation.get("source_tokens")
        if (not isinstance(tokens, list) or not tokens or not all(isinstance(token, str) for token in tokens)
                or not set(tokens) <= allowed_tokens or len(tokens) != len(set(tokens))):
            raise ContractError("proposal_relation_source")
        key = (handle, relation.get("relation"), tuple(sorted(tokens)))
        if key in seen:
            raise ContractError("proposal_relation_duplicate")
        seen.add(key)
    return handles
