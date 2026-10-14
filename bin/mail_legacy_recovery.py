"""Retire superseded v1 journals against a verified successor, without rollback.

This is not schema conversion: observed current hashes never become after hashes.
All source bytes and old backups stay intact. A current full validation and an
applied, committed successor covering every old message are mandatory. Targets
not touched by that successor must be unchanged or contain only independently
attributed independent claim additions. Caller owns the memory writer lock.
The old scope must have no runnable group; unrelated model work may continue.
Creating/rebuilding semantic queues still requires the ordinary mail run lock.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import base64
import hashlib
import json
import re
from pathlib import Path

import mail_transaction_recovery as paths

STATE = 'superseded_after_reconciliation'
CLAIM = re.compile(r'<!-- business-claim:v[12] ([A-Za-z0-9_=+-/]+) -->.*?<!-- /business-claim:[^>]+ -->', re.S)
BASE = re.compile(r'<!-- business-claim-base:v1 [^>]+ -->')


def api():
    import email_sync
    return email_sync


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read(path):
    data, _ = paths._read(path)
    if data is None:
        raise ValueError('legacy_evidence_missing')
    return data


def later_additions_only(before, current, old_ref, created):
    """Check structural provenance only, never interpret or approve business facts."""
    if before == current:
        return True
    try:
        old, new = before.decode('utf-8'), current.decode('utf-8')
        old_blocks = [m.group(0) for m in CLAIM.finditer(old)]
        new_blocks = [m.group(0) for m in CLAIM.finditer(new)]
        if any(new_blocks.count(b) != old_blocks.count(b) for b in old_blocks):
            return False
        if BASE.sub('', CLAIM.sub('', old)).strip() != BASE.sub('', CLAIM.sub('', new)).strip():
            return False
        for match in CLAIM.finditer(new):
            if match.group(0) in old_blocks:
                continue
            encoded = match.group(1)
            meta = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            if (not meta.get('source_ref') or meta['source_ref'] == old_ref
                    or meta.get('link_status') != 'verified'):
                return False
        return len(new_blocks) > len(old_blocks)
    except (ValueError, KeyError, TypeError, UnicodeError):
        return False


def plan(config, journal_path, journal):
    s = api()
    if (journal.get('schema_version') != 1 or journal.get('state') not in {'prepared', 'applying'}
            or not journal.get('paths')):
        raise ValueError('legacy_journal_ineligible')
    sid = journal.get('semantic_id')
    root = s.runtime_paths(config)['runs']
    if not isinstance(sid, str) or not s.UUID_RE.fullmatch(sid):
        raise ValueError('legacy_id_invalid')
    if journal_path != root / sid / 'memory-transaction' / 'transaction.json':
        raise ValueError('legacy_journal_path_invalid')
    paths._path(journal_path)
    if json.loads(read(journal_path)) != journal:
        raise ValueError('legacy_journal_changed')
    with s.connect_database_readonly(config) as c:
        old = c.execute('SELECT * FROM semantic_groups WHERE semantic_id=?', (sid,)).fetchone()
        if not old or old['status'] != 'superseded':
            raise ValueError('legacy_not_superseded')
        old = dict(old)
        if c.execute("SELECT 1 FROM semantic_groups WHERE group_key=? AND status IN ('pending','failed') LIMIT 1", (old['group_key'],)).fetchone():
            raise ValueError('legacy_scope_active')
        members = sorted(r[0] for r in c.execute('SELECT message_key FROM semantic_group_messages WHERE semantic_id=?', (sid,)))
        if not members:
            raise ValueError('legacy_members_missing')
        candidates = [dict(r) for r in c.execute("SELECT * FROM semantic_groups WHERE group_key=? AND status='applied' AND completed_at>? ORDER BY completed_at DESC", (old['group_key'], journal['created_at']))]
        successor = None
        for candidate in candidates:
            covered = sorted(r[0] for r in c.execute('SELECT message_key FROM semantic_group_messages WHERE semantic_id=?', (candidate['semantic_id'],)))
            if not set(members).issubset(covered):
                continue
            successor_path = root / candidate['semantic_id'] / 'memory-transaction' / 'transaction.json'
            successor_bytes = read(successor_path)
            successor_journal = json.loads(successor_bytes)
            if (successor_journal.get('state') == 'committed'
                    and successor_journal.get('semantic_id') == candidate['semantic_id']
                    and successor_journal.get('committed_at') == candidate['completed_at']):
                successor = candidate
                break
        if not successor:
            raise ValueError('legacy_successor_unproven')
    memory_root = s.runtime_paths(config)['memory_root'].resolve()
    indexes = {memory_root/'00_记忆总览.md', memory_root/'99_规则与模板/AI检索总表.md', memory_root/'90_待确认/00_待确认总表.md'}
    successor_targets = {str(paths._path(e['target'])) for e in successor_journal['paths']}
    observations = []
    seen = set()
    backup_root = journal_path.parent / 'backup'
    for entry in journal['paths']:
        target = paths._path(entry['target'])
        if target in seen or not s.memory.safe_target(memory_root, target):
            raise ValueError('legacy_target_unsafe')
        seen.add(target)
        current = read(target)
        backup = paths._path(entry['backup']) if entry.get('backup') else None
        if not backup or backup_root not in backup.parents or entry.get('existed') is not True:
            raise ValueError('legacy_backup_unproven')
        before = read(backup)
        basis = 'derived_index' if target in indexes else 'successor_target'
        if target not in indexes and str(target) not in successor_targets:
            if not later_additions_only(before, current, old['source_ref'], journal['created_at']):
                raise ValueError('legacy_uncovered_target_changed')
            basis = 'unchanged_or_independent_later_additions'
        observations.append({'target': str(target), 'current_sha256': hashlib.sha256(current).hexdigest(),
                             'backup_sha256': hashlib.sha256(before).hexdigest(), 'basis': basis})
    return {'schema_version': 1, 'semantic_id': sid, 'successor_id': successor['semantic_id'],
            'successor_journal_sha256': hashlib.sha256(successor_bytes).hexdigest(),
            'successor_completed_at': successor['completed_at'], 'members': members,
            'original_journal': journal, 'original_journal_sha256': hashlib.sha256(read(journal_path)).hexdigest(),
            'observations': observations, 'source_files_modified': False, 'memory_application_claimed': False}


def reconcile(config, journal_path, journal, *, lock_held=False):
    if lock_held is not True:
        raise ValueError('exclusive_lock_required')
    s = api()
    if journal.get('state') == STATE:
        with s.connect_database_readonly(config) as c:
            groups = [dict(x) for x in c.execute('SELECT * FROM semantic_groups')]
        if not verify_receipt(config, journal_path, journal, groups):
            raise ValueError('legacy_retirement_receipt_unproven')
        evidence = json.loads(read(journal_path.with_name('legacy-reconciliation.json')))
        return finish_ledger(config, journal, evidence)
    evidence = plan(config, journal_path, journal)
    # Validation is real, fresh, under the same memory lock. Failure publishes no
    # retirement receipt; source files are never rewritten by this helper.
    validation = s.memory.run_refresh_and_validate(config, lock_held=True, attempts=1)
    if validation.get('validator', {}).get('verdict') != 'pass':
        raise ValueError('legacy_validation_unproven')
    checked = plan(config, journal_path, journal)
    def stable(e):
        return {**e, 'observations': [x for x in e['observations'] if x['basis'] != 'derived_index']}
    if stable(evidence) != stable(checked):
        raise ValueError('legacy_evidence_changed')
    evidence = checked
    evidence.update(reconciled_at=s.now_iso(), validation=validation)
    receipt = journal_path.with_name('legacy-reconciliation.json')
    s.atomic_write_text(receipt, json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    # Keep original journal, backups, business files and applied rows in evidence.
    updated = {**journal, 'state': STATE, 'reconciled_at': evidence['reconciled_at'],
               'reconciliation_sha256': s.sha256_file(receipt)}
    s.atomic_write_text(journal_path, json.dumps(updated, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    return finish_ledger(config, updated, evidence)


def finish_ledger(config, journal, evidence):
    s = api()
    proof = {'legacy_retired': True, 'source_files_modified': False,
             'successor_id': evidence['successor_id'], 'reconciliation_sha256': journal['reconciliation_sha256']}
    s.mail_auto_recovery.recovered(config, journal['semantic_id'], 'transaction', proof)
    with s.connect_database_readonly(config) as c:
        key = c.execute('SELECT group_key FROM semantic_groups WHERE semantic_id=?', (journal['semantic_id'],)).fetchone()[0]
    s.mail_auto_recovery.recovered(config, key, 'semantic_scope', proof)
    return proof


def verify_receipt(config, journal_path, journal, groups):
    """Queue gate: retired metadata alone never releases the old scope."""
    try:
        data = read(journal_path.with_name('legacy-reconciliation.json'))
        if hashlib.sha256(data).hexdigest() != journal.get('reconciliation_sha256'):
            return False
        evidence = json.loads(data)
        if (evidence.get('schema_version') != 1 or evidence.get('semantic_id') != journal['semantic_id']
                or evidence.get('source_files_modified') is not False
                or evidence.get('memory_application_claimed') is not False
                or evidence.get('validation', {}).get('validator', {}).get('verdict') != 'pass'
                or not evidence.get('observations')):
            return False
        original = evidence['original_journal']
        if {k:v for k,v in journal.items() if k not in {'state','reconciled_at','reconciliation_sha256'}} != {k:v for k,v in original.items() if k != 'state'}:
            return False
        sid = evidence['successor_id']
        if not any(g['semantic_id'] == sid and g['status'] == 'applied' for g in groups):
            return False
        if not any(g['semantic_id'] == journal['semantic_id'] and g['status'] == 'superseded' for g in groups):
            return False
        with api().connect_database_readonly(config) as c:
            old_members = sorted(x[0] for x in c.execute('SELECT message_key FROM semantic_group_messages WHERE semantic_id=?', (journal['semantic_id'],)))
            new_members = {x[0] for x in c.execute('SELECT message_key FROM semantic_group_messages WHERE semantic_id=?', (sid,))}
        if old_members != evidence['members'] or not set(old_members).issubset(new_members):
            return False
        successor = api().runtime_paths(config)['runs'] / sid / 'memory-transaction' / 'transaction.json'
        return hashlib.sha256(read(successor)).hexdigest() == evidence['successor_journal_sha256']
    except (OSError, ValueError, TypeError, KeyError):
        return False
