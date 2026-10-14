"""Rebuild a rolled-back chat submission only across additive governed claims.

Does not merge business prose or call a model. Keeps the original batch and its
files immutable, and transfers the existing queue rows to one deterministic
child. Called under the ordinary chat/control locks; uses the memory lock and
one SQLite transaction for admission. A child is never automatically rebased.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import copy
import json
import sqlite3
import uuid
import dingtalk_commit_guard as guard
from pathlib import Path
import business_page_integrity as integrity
import dingtalk_batch_contract as contract

class RecoveryBlocked(RuntimeError):
    pass

def memory():
    import dingtalk_memory_automation
    return dingtalk_memory_automation

def prove_additive(before: str, current: str) -> bool:
    if before == current:
        return False
    integrity.require_valid(before)
    integrity.require_valid(current)
    old, new = integrity.claim_blocks(before), integrity.claim_blocks(current)
    # Both byte-preservation and order are required. Base prose edits, changed
    # evidence, removed claims and malformed boundaries remain independent faults.
    remaining = iter(new)
    preserved = all(any(block == candidate for candidate in remaining) for block in old)
    if (integrity.business_base_text(before) != integrity.business_base_text(current)
            or not old or len(new) <= len(old) or not preserved):
        raise RecoveryBlocked('prepared_recovery_not_additive')
    return True

def inspect(config: dict, old: str) -> dict:
    m = memory()
    directory = m.runtime_paths(config)['runs'] / old
    if (directory / 'prepared-recovery-origin.json').exists():
        raise RecoveryBlocked('prepared_recovery_limit_reached')
    connection = m.connect_database(config)
    try:
        row = connection.execute('SELECT * FROM semantic_batches WHERE batch_id=?', (old,)).fetchone()
        if not row or row['status'] != 'failed' or row['error_code'] not in guard.RECOVERABLE:
            raise RecoveryBlocked('prepared_recovery_not_eligible')
        manifest = contract.load(directory, connection, old)
        if not manifest:
            raise RecoveryBlocked('prepared_recovery_manifest_missing')
        queue = connection.execute('SELECT q.queue_id,q.state,q.batch_id FROM batch_messages b JOIN semantic_queue q ON q.queue_id=b.queue_id WHERE b.batch_id=? ORDER BY b.ordinal',(old,)).fetchall()
        if len(queue) != row['message_count'] or any(q['state'] != 'failed' or q['batch_id'] is not None for q in queue):
            raise RecoveryBlocked('prepared_recovery_queue_changed')
        proposal_path = directory / 'proposal.json'
        if m.sha256_path(proposal_path) != row['proposal_sha256']:
            raise RecoveryBlocked('prepared_recovery_proposal_changed')
        raw = json.loads(proposal_path.read_text())
    finally:
        connection.close()
    proposal = m.validate_proposal(config, old, raw)  # Includes live relation-context checks.
    if any(c['action'] != 'annotate_page' for c in proposal['candidates']):
        raise RecoveryBlocked('prepared_recovery_action_not_additive')
    commit = json.loads((directory/'commit-receipt.json').read_text())
    journal_path = directory/'apply_journal.json'
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
    planned = json.loads((directory/'planned-writes.json').read_text())
    if (commit.get('batch_id') != old or commit.get('phase') != 'prepared'
            or commit.get('proposal_sha256') != contract.digest(proposal)
            or (journal is not None and (journal.get('batch_id') != old or journal.get('state') != 'rolled_back'
                or journal.get('validation_status') != 'passed'
                or m.batch_source_refs(config, old) != journal.get('source_refs')))
            or contract.digest(planned) != commit.get('planned_writes_sha256')
            or {p:m.sha256_text(t) for p,t in planned.get('writes',{}).items()} != commit.get('file_hashes')
            or set(commit.get('before_hashes',{})) != set(commit.get('file_hashes',{}))):
        raise RecoveryBlocked('prepared_recovery_evidence_invalid')
    try:
        entries = ({e['target']:e for e in journal['paths']} if journal is not None
                   else guard.prewrite_entries(directory, commit))
    except (OSError, ValueError, KeyError, TypeError):
        raise RecoveryBlocked('prepared_recovery_prewrite_unverified')
    hashes, changed, before_texts = {}, 0, {}
    for path, expected in commit['before_hashes'].items():
        target = Path(path); entry = entries.get(path)
        if not m.safe_target(m.runtime_paths(config)['memory_root'],target) or target.is_symlink() or not entry:
            raise RecoveryBlocked('prepared_recovery_target_unsafe')
        if entry.get('existed'):
            backup = Path(entry.get('backup',''))
            backup_root = directory / ('backup' if journal is not None else 'prewrite-backup')
            if (entry.get('before_sha256') != expected or backup.is_symlink()
                    or not backup.resolve().is_relative_to(backup_root.resolve()) or m.sha256_path(backup) != expected):
                raise RecoveryBlocked('prepared_recovery_backup_invalid')
            before_texts[path] = backup.read_text()
        else:
            if expected is not None or entry.get('before_sha256') is not None:
                raise RecoveryBlocked('prepared_recovery_backup_invalid')
            before_texts[path] = ''
        hashes[path] = m.sha256_path(target)
        if hashes[path] != expected:
            if not entry.get('existed') or not target.is_file():
                raise RecoveryBlocked('prepared_recovery_not_additive')
            changed += int(prove_additive(before_texts[path],target.read_text()))
    collisions = guard.id_conflicts(config, planned['writes'], before_texts)
    if not changed and not collisions:
        raise RecoveryBlocked('prepared_recovery_no_change')
    names = ['proposal.json','input-manifest.json','commit-receipt.json','planned-writes.json']
    names += ['apply_journal.json'] if journal is not None else ['prewrite-evidence.json']
    original_files = {n:m.sha256_path(directory/n) for n in names}
    for entry in entries.values():
        if entry.get('backup'):
            backup = Path(entry['backup'])
            original_files[str(backup.relative_to(directory))] = m.sha256_path(backup)
    child = str(uuid.uuid5(uuid.NAMESPACE_URL,'business-prepared-recovery:'+old))
    return {'old_batch_id':old,'child_batch_id':child,'target_hashes':hashes,'original_files':original_files,
            'proposal':proposal,'manifest':manifest,'message_count':row['message_count'],'text_chars':row['text_chars'],
            'changed_targets':changed,'recovery_reason':'identifier_occupied' if collisions else 'additive_target_change'}

def prepare(config: dict, old: str) -> dict:
    m = memory()
    lock = m.acquire_lock(m.MEMORY_LOCK, blocking=True)
    connection = None
    try:
        proof = inspect(config,old)
        child = proof['child_batch_id']; directory = m.runtime_paths(config)['runs']/child
        # A filesystem-only orphan is not permission to overwrite arbitrary files.
        document=copy.deepcopy(proof['manifest']['document']);document['batch_id']=child
        proposal=copy.deepcopy(proof['proposal']);proposal['batch_id']=child
        encoded=json.dumps(proposal,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
        if directory.exists():
            allowed={'input-manifest.json','proposal.json','prepared-recovery-origin.json'}
            if directory.is_symlink() or any(p.name not in allowed or p.is_symlink() or not p.is_file() for p in directory.iterdir()):
                raise RecoveryBlocked('prepared_recovery_orphan_conflict')
            proposal_path=directory/'proposal.json'
            if proposal_path.exists() and proposal_path.read_text()!=encoded:
                raise RecoveryBlocked('prepared_recovery_orphan_conflict')
            origin_path=directory/'prepared-recovery-origin.json'
            if origin_path.exists():
                origin=json.loads(origin_path.read_text())
                if any(origin.get(k)!=v for k,v in {'old_batch_id':old,'child_batch_id':child,'original_files':proof['original_files'],'target_hashes':proof['target_hashes']}.items()):
                    raise RecoveryBlocked('prepared_recovery_orphan_conflict')
        connection = m.connect_database(config)
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM semantic_batches WHERE batch_id=?',(child,)).fetchone():
            raise RecoveryBlocked('prepared_recovery_child_exists')
        # Source and ownership are rechecked inside the write transaction.
        original = m.runtime_paths(config)['runs']/old
        if contract.load(original,connection,old) != proof['manifest']:
            raise RecoveryBlocked('prepared_recovery_source_changed')
        rows = connection.execute('SELECT b.*,q.state,q.batch_id AS owner FROM batch_messages b JOIN semantic_queue q ON q.queue_id=b.queue_id WHERE b.batch_id=? ORDER BY b.ordinal',(old,)).fetchall()
        if len(rows)!=proof['message_count'] or any(r['state']!='failed' or r['owner'] is not None for r in rows):
            raise RecoveryBlocked('prepared_recovery_queue_changed')
        if any(m.sha256_path(Path(p)) != h for p,h in proof['target_hashes'].items()):
            raise RecoveryBlocked('prepared_recovery_target_changed')
        if any(m.sha256_path(original/n) != h for n,h in proof['original_files'].items()):
            raise RecoveryBlocked('prepared_recovery_original_changed')
        stamp=m.now_iso();document=copy.deepcopy(proof['manifest']['document']);document['batch_id']=child
        proposal=copy.deepcopy(proof['proposal']);proposal['batch_id']=child
        encoded=json.dumps(proposal,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
        connection.execute("INSERT INTO semantic_batches(batch_id,created_at,status,message_count,text_chars) VALUES(?,?,'model_complete',?,?)",(child,stamp,proof['message_count'],proof['text_chars']))
        for row in rows:
            connection.execute('INSERT INTO batch_messages(batch_id,queue_id,ordinal,source_token) VALUES(?,?,?,?)',(child,row['queue_id'],row['ordinal'],row['source_token']))
        manifest_hash=contract.freeze(directory,connection,child,document)
        contract.save(directory/'prepared-recovery-origin.json',{'schema_version':1,'policy':'preserve_additive_claims','recovery_reason':proof['recovery_reason'],'old_batch_id':old,'child_batch_id':child,'created_at':stamp,'original_files':proof['original_files'],'target_hashes':proof['target_hashes'],'model_called':False})
        m.atomic_write(directory/'proposal.json',encoded)
        connection.execute('UPDATE semantic_batches SET proposal_sha256=?,input_manifest_sha256=? WHERE batch_id=?',(m.sha256_text(encoded),manifest_hash,child))
        connection.execute("UPDATE semantic_queue SET state='claimed',batch_id=?,last_error_code=NULL WHERE queue_id IN (SELECT queue_id FROM batch_messages WHERE batch_id=?)",(child,old))
        if hasattr(m,'fsync_directory'):
            m.fsync_directory(directory)
            m.fsync_directory(directory.parent)
        connection.commit()
        return {'status':'prepared','old_batch_id':old,'child_batch_id':child,'message_count':proof['message_count'],'changed_targets':proof['changed_targets'],'model_called':False,'recovery_reason':proof['recovery_reason']}
    finally:
        if connection is not None:connection.close()
        lock.close()

def assessment(config, old, status, reason, child=None):
    m=memory(); directory=m.runtime_paths(config)['runs']/old
    value={'schema_version':1,'old_batch_id':old,'status':status,'recovery_reason':reason,
           'child_batch_id':child,'model_called':False}
    path=directory/'recovery-assessment.json'
    previous=json.loads(path.read_text()) if path.is_file() else {}
    if all(previous.get(k)==v for k,v in value.items()): return
    contract.save(path,{**value,'recorded_at':m.now_iso()})


def linked_recovery(config: dict, old: str, *, connection=None) -> dict | None:
    """Read a deterministic child without changing the original failure evidence."""
    m = memory()
    child = str(uuid.uuid5(uuid.NAMESPACE_URL, 'business-prepared-recovery:' + old))
    directory = m.runtime_paths(config)['runs'] / child
    owns_connection = connection is None
    if owns_connection:
        connection = m.connect_database(config)
    try:
        row = connection.execute('SELECT status,message_count,proposal_sha256 FROM semantic_batches WHERE batch_id=?', (child,)).fetchone()
        if not row:
            return None
        origin = json.loads((directory / 'prepared-recovery-origin.json').read_text())
        if origin.get('old_batch_id') != old or origin.get('child_batch_id') != child:
            raise RecoveryBlocked('prepared_recovery_child_evidence_invalid')
        original = directory.parent / old
        expected = origin.get('original_files') or {}
        required = {'proposal.json','input-manifest.json','commit-receipt.json','planned-writes.json'}
        evidence = set(expected) & {'apply_journal.json', 'prewrite-evidence.json'}
        allowed = required | evidence
        extras = set(expected) - allowed
        safe_backups = all(len(Path(n).parts) >= 2 and Path(n).parts[0] in {'backup','prewrite-backup'}
                           and '..' not in Path(n).parts and not (original/n).is_symlink()
                           and (original/n).resolve().is_relative_to((original/Path(n).parts[0]).resolve()) for n in extras)
        if (not required <= set(expected) or len(evidence) != 1 or not safe_backups
                or any(m.sha256_path(original/n) != h for n,h in expected.items())):
            raise RecoveryBlocked('prepared_recovery_original_changed')
        # Check the exact queue/token mapping, not merely counts or a child label.
        mappings = []
        for batch_id in (old, child):
            mappings.append([tuple(r) for r in connection.execute('SELECT queue_id,ordinal,source_token FROM batch_messages WHERE batch_id=? ORDER BY ordinal', (batch_id,))])
        if not mappings[0] or mappings[0] != mappings[1] or len(mappings[1]) != row['message_count']:
            raise RecoveryBlocked('prepared_recovery_queue_changed')
        if not contract.load(directory, connection, child):
            raise RecoveryBlocked('prepared_recovery_manifest_missing')
        if m.sha256_path(directory/'proposal.json') != row['proposal_sha256']:
            raise RecoveryBlocked('prepared_recovery_proposal_changed')
        queue = connection.execute('SELECT q.state,q.batch_id FROM batch_messages b JOIN semantic_queue q ON q.queue_id=b.queue_id WHERE b.batch_id=?', (child,)).fetchall()
        result = {'status': 'child_pending', 'child_batch_id': child, 'message_count': row['message_count']}
        if row['status'] == 'applied':
            commit = json.loads((directory/'commit-receipt.json').read_text())
            proposal = m.validate_proposal(config, child, json.loads((directory/'proposal.json').read_text()), check_context=False)
            planned = json.loads((directory/'planned-writes.json').read_text())
            journal = json.loads((directory/'apply_journal.json').read_text())
            targets = set(commit.get('file_hashes') or {})
            canonical_entries = {str(Path(e['target']).resolve()): e for e in journal.get('paths', [])}
            entries = {t: canonical_entries[str(Path(t).resolve())] for t in targets if str(Path(t).resolve()) in canonical_entries}
            if (planned.get('batch_id') != child or commit.get('planned_writes_sha256') != contract.digest(planned)
                    or not targets or targets != set(planned.get('writes') or {})
                    or targets != set(commit.get('before_hashes') or {}) or commit.get('write_count') != len(targets)
                    or journal.get('batch_id') != child or journal.get('state') != 'committed'
                    or journal.get('source_refs') != m.batch_source_refs(config, child)
                    or not targets <= set(entries)
                    or any(entries[t].get('expected_applied_sha256') != m.sha256_text(planned['writes'][t]) or not entries[t].get('applied_sha256') for t in targets)):
                raise RecoveryBlocked('prepared_recovery_completion_unverified')
            if (len(queue) != row['message_count'] or any(q['state'] not in {'applied','no_business_change'} or q['batch_id'] != child for q in queue)
                    or commit.get('batch_id') != child or commit.get('phase') != 'complete'
                    or commit.get('proposal_sha256') != contract.digest(proposal)
                    or (commit.get('result') or {}).get('validated') is not True
                    or ((commit.get('verification') or {}).get('validator') or {}).get('verdict') != 'pass'):
                raise RecoveryBlocked('prepared_recovery_completion_unverified')
            result.update(status='recovered', completed_at=commit.get('completed_at'),
                          completion_sha256=m.sha256_path(directory/'commit-receipt.json'))
        return result
    finally:
        if owns_connection:
            connection.close()


def prepare_pending(config: dict, *, only_old=None) -> dict:
    """One recovery at most; held/conflicting originals never fall into replay."""
    m = memory()
    path = m.runtime_paths(config)['status'].parent / 'prepared-recovery-status.json'
    previous = json.loads(path.read_text()) if path.is_file() else {}
    if not isinstance(previous, dict) or not isinstance(previous.get('batches', {}), dict):
        raise RecoveryBlocked('prepared_recovery_status_invalid')
    prior = previous.get('batches', {})
    connection = m.connect_database(config)
    try:
        rows = connection.execute("SELECT b.batch_id FROM semantic_batches b JOIN semantic_recovery_holds h ON h.batch_id=b.batch_id ORDER BY b.created_at").fetchall()
    finally:
        connection.close()
    results = dict(prior) if only_old else {}
    attempted = bool(config.get("_prepared_recovery_attempted"))
    for row in rows:
        old = str(row['batch_id'])
        if only_old and old != only_old:
            continue
        try:
            linked = linked_recovery(config, old)
            if linked:
                results[old] = linked
                continue
            if prior.get(old, {}).get('status') in {'technical_failure', 'preparing'}:
                # Persisted before mutation: a crash cannot silently grant a retry.
                results[old] = {**prior[old], 'automatic_retry': False}
                continue
            inspect(config, old)
            if attempted:
                results[old] = {'status': 'eligible', 'automatic_retry': False}
                continue
            attempted = True
            config["_prepared_recovery_attempted"] = True
            intent = {'status': 'preparing', 'started_at': m.now_iso(), 'automatic_retry': False}
            contract.save(path, {'schema_version': 1, 'batches': {**prior, **results, old: intent}})
            result = prepare(config, old)
            results[old] = {**result, 'status': 'child_pending', 'automatic_retry': False}
        except (RecoveryBlocked, contract.ContractError, m.AutomationError, integrity.IntegrityError) as exc:
            # Do not copy arbitrary provider text, paths, message bodies or IDs.
            code = getattr(exc, 'code', str(exc) if isinstance(exc, RecoveryBlocked) else type(exc).__name__)
            results[old] = {'status': 'blocked', 'reason_code': code, 'automatic_retry': False}
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            results[old] = {'status': 'technical_failure', 'reason_code': type(exc).__name__, 'automatic_retry': False}
    for old, item in results.items():
        assessment(config, old, item['status'], item.get('reason_code', item.get('recovery_reason', item['status'])), item.get('child_batch_id'))
    # This is a diagnostic projection, not an approval or completion marker.
    result = {'schema_version': 1, 'batches': results}
    if result != previous:
        contract.save(path, result)
    return result


def pending_summary(config: dict) -> dict:
    path = memory().runtime_paths(config)['status'].parent / 'prepared-recovery-status.json'
    value = json.loads(path.read_text()) if path.is_file() else {}
    batches = value.get('batches', {})
    pending = [row for row in batches.values() if row.get('status') != 'recovered']
    return {'pending_batches': len(pending), 'technical_failure_batches': sum(row.get('status') in {'technical_failure','preparing'} for row in pending),
            'blocked_batches': sum(row.get('status') == 'blocked' for row in pending),
            'child_pending_batches': sum(row.get('status') == 'child_pending' for row in pending),
            'automatic_retry': False}



def try_prepare(config, old):
    result = prepare_pending(config, only_old=old)['batches'].get(old, {})
    return result if result.get('status') == 'child_pending' else None
