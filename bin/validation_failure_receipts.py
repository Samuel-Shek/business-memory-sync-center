"""Durable redacted failure history beside the compatible latest receipt."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import os
import re
import uuid
from pathlib import Path


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as out:
            os.chmod(temporary, 0o600)
            json.dump(value, out, ensure_ascii=False, sort_keys=True, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    finally:
        if temporary.exists(): temporary.unlink()


def archive(path, receipt):
    receipt = {k: v for k, v in receipt.items() if k != "failure_receipt_id"}
    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()
    fingerprint = hashlib.sha256(canonical).hexdigest()
    safe = {key: receipt.get(key) for key in (
        'recorded_at', 'run_id', 'origin', 'phase', 'verdict', 'error_code',
        'returncode', 'input_key', 'index_generation', 'attempt_count', 'validation_context')}
    # Validator errors can contain filenames and source excerpts. Persist only
    # a bounded vocabulary plus a hash that ties the check to the original.
    errors = receipt.get('errors') or []
    safe['checks'] = [{'terms': sorted(set(re.findall(
        r'\b(?:missing|mismatch|invalid|uncovered|hash|checksum|index|generation|source|inventory|permission|timeout|database|journal|conflict|duplicate|count|internal|error|failed|unavailable|stale|unsafe|reference)\b', str(e).lower()))),
        'detail_sha256': hashlib.sha256(str(e).encode()).hexdigest()} for e in errors[:50]]
    safe.update(schema_version=1, original_receipt_sha256=fingerprint, error_count=len(errors))
    destination = path.parent/'validation-failures'/f'{fingerprint}.json'
    if not destination.exists(): _atomic(destination, safe)
    return fingerprint


def publish(path: Path, receipt: dict):
    if path.exists():
        try: previous = json.loads(path.read_text())
        except (ValueError, OSError): previous = None
        if isinstance(previous, dict): archive(path, previous)
    reference = archive(path, receipt)
    _atomic(path, {**receipt, 'failure_receipt_id': reference})
