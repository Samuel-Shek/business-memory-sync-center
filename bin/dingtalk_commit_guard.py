"""Pre-write identity checks and durable evidence; caller holds the memory lock."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import re
from pathlib import Path
import dingtalk_batch_contract as contract
import business_page_integrity as integrity
RECOVERABLE = {'proposal_commit_content_conflict', 'proposal_commit_id_conflict'}

def memory():
    import dingtalk_memory_automation
    return dingtalk_memory_automation

def ids(text):
    result = []
    if text.startswith('---\n'):
        match = re.search(r'^memory_id:\s*[\'\"]?(BUSINESS-\d{8}-\d{3})[\'\"]?\s*$', text.split('---',2)[1], re.M)
        if match: result.append(match[1])
    for match in integrity.CLAIM_START_RE.finditer(text):
        result.append(str(integrity.decode_payload(match['payload'],code='claim_marker_malformed')['claim_id']))
    return result

def capture(directory, commit):
    m=memory();entries=[]
    for n,(name,expected) in enumerate(commit['before_hashes'].items()):
        path=Path(name)
        if path.is_symlink(): raise m.AutomationError('unsafe_memory_target','提交目标不是安全普通文件。')
        text=path.read_text() if path.exists() else None
        if (m.sha256_text(text) if text is not None else None)!=expected:
            raise m.AutomationError('memory_backup_concurrent_change','准备前像时目标发生变化。')
        backup=directory/'prewrite-backup'/f'{n}.txt'
        if text is not None:
            m.atomic_write(backup,text)
            m.fsync_directory(backup.parent)
        entries.append({'target':name,'backup':str(backup) if text is not None else None,'existed':text is not None,'before_sha256':expected})
    contract.save(directory/'prewrite-evidence.json',{'schema_version':1,'batch_id':commit['batch_id'],'commit_sha256':contract.digest(commit),'paths':entries,'state':'not_started'})

def prewrite_entries(directory, commit):
    m=memory()
    if (directory/'apply_journal.json').exists(): raise ValueError('prewrite_journal_exists')
    receipt=json.loads((directory/'prewrite-evidence.json').read_text())
    if receipt.get('state')!='not_started' or receipt.get('batch_id')!=commit['batch_id'] or receipt.get('commit_sha256')!=contract.digest(commit):
        raise ValueError('prewrite_evidence_invalid')
    entries={e['target']:e for e in receipt['paths']}
    if set(entries)!=set(commit['before_hashes']) or len(entries)!=len(receipt['paths']): raise ValueError('prewrite_evidence_invalid')
    for path,expected in commit['before_hashes'].items():
        e=entries[path]
        if e.get('before_sha256')!=expected: raise ValueError('prewrite_evidence_invalid')
        if e['existed']:
            p=Path(e['backup'])
            if p.is_symlink() or not p.resolve().is_relative_to((directory/'prewrite-backup').resolve()) or m.sha256_path(p)!=expected:
                raise ValueError('prewrite_backup_invalid')
        elif expected is not None or e.get('backup') is not None: raise ValueError('prewrite_evidence_invalid')
    return entries

def id_conflicts(config,planned,before_texts):
    m=memory();root=m.runtime_paths(config)['memory_root'];owners=set()
    owners.update(str(r['memory_id']) for r in m.memory_records(root,strict=False))
    owners.update(str(r['claim_id']) for r in m.business_router.scan_claims(root.parent))
    from collections import Counter
    additions=set();conflicts=set()
    for name,text in planned.items():
        delta = Counter(ids(text)) - Counter(ids(before_texts.get(name,'')))
        for mid, count in delta.items():
            if mid in owners or mid in additions or count > 1: conflicts.add(mid)
            additions.add(mid)
    return sorted(conflicts)

def record_failure(directory,code,detail,*,stage):
    m=memory();safe={k:detail[k] for k in ('phase','original_error_code','rollback_error_code','cause_category','cause_fingerprint','rollback_status','recovery_reason','child_batch_id') if k in detail}
    receipt={'schema_version':1,'batch_id':directory.name,'error_code':code,'stage':stage,'recorded_at':m.now_iso(),**safe}
    folder=directory/'failure-evidence';seq=len(list(folder.glob('*.json'))) if folder.exists() else 0
    try:
        contract.save(folder/f'{seq:06d}-{contract.digest(receipt)[:16]}.json',receipt)
        return True
    except OSError:
        return False  # Diagnostics must never prevent rollback or queue preservation.

def check(config,directory,commit,planned,journal):
    m=memory()
    if any(m.sha256_path(Path(p))!=h for p,h in commit['before_hashes'].items()):
        raise m.AutomationError('proposal_commit_content_conflict','准备期间目标内容发生变化，保留现版。')
    before={p:Path(p).read_text() if Path(p).exists() else '' for p in planned}
    conflicts=id_conflicts(config,planned,before)
    if conflicts:
        detail={'phase':'commit_preflight','cause_category':'identifier_occupied','cause_fingerprint':hashlib.sha256('|'.join(conflicts).encode()).hexdigest()}
        raise m.AutomationError('proposal_commit_id_conflict','旧提交新增的编号已被占用，未写入业务文件。',notification_detail=detail)

def mark_started(directory):
    path=directory/'prewrite-evidence.json'
    if path.is_file():
        receipt=json.loads(path.read_text())
        receipt['state']='write_started'
        contract.save(path,receipt)


def recovery_evidence(config, since=None, until=None):
    """Read-only bounded candidates; time proximity is not exact issue membership."""
    import sqlite3
    import uuid
    from datetime import datetime,timedelta
    from urllib.parse import quote
    try:
        root=Path(config['external_root'])/'dingtalk-chat'
        start=(datetime.fromisoformat(since)-timedelta(minutes=10)).isoformat() if since else ''
        stop=until or '9999'
        with sqlite3.connect('file:'+quote(str(root/'state/chat.sqlite'))+'?mode=ro',uri=True,timeout=1) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            rows=db.execute("SELECT batch_id,created_at,error_code FROM semantic_batches WHERE status='failed' AND error_code IN ('proposal_commit_content_conflict','proposal_commit_id_conflict') AND created_at>=? AND created_at<=? ORDER BY created_at DESC LIMIT 12",(start,stop)).fetchall()
            result=[]
            for row in rows:
                old=row['batch_id'];uuid.UUID(old)
                child=str(uuid.uuid5(uuid.NAMESPACE_URL,'business-prepared-recovery:'+old))
                item=dict(row);item.update(child_batch_id=child,association='time_window_candidate',recovery_verified=False)
                batch=db.execute('SELECT status,completed_at,message_count FROM semantic_batches WHERE batch_id=?',(child,)).fetchone()
                d=root/'automation/runs'/child
                if batch:
                    item.update(dict(batch))
                    try:
                        import dingtalk_prepared_recovery as recovery
                        verified = recovery.linked_recovery(config, old, connection=db)
                        item['recovery_verified'] = bool(verified and verified['status'] == 'recovered')
                        if verified and verified.get('completion_sha256'):
                            item['completion_sha256'] = verified['completion_sha256']
                    except (OSError, ValueError, TypeError, KeyError, recovery.RecoveryBlocked, contract.ContractError, memory().AutomationError):
                        item['evidence_gap']='recovery_receipt_unavailable'
                assessment=root/'automation/runs'/old/'recovery-assessment.json'
                if assessment.is_file():
                    obj=json.loads(assessment.read_text());reason=str(obj.get('recovery_reason',''))
                    if re.fullmatch(r'[a-z_]{1,100}',reason):item['recovery_reason']=reason
                failures=root/'automation/runs'/old/'failure-evidence'
                if failures.is_dir():
                    names=sorted(failures.glob('*.json'))
                    selected=names[:1]+(names[-1:] if len(names)>1 else [])
                    item['failures']=[]
                    for name in selected:
                        obj=json.loads(name.read_text());clean={}
                        for key in ('stage','error_code','original_error_code','rollback_error_code','cause_category','cause_fingerprint','rollback_status'):
                            value=str(obj.get(key,''))
                            if re.fullmatch(r'[A-Za-z0-9_]{1,100}',value):clean[key]=value
                        item['failures'].append(clean)
                result.append(item)
        return {'candidates':result,'boundary':'按故障时间窗口关联候选；各批次恢复依据独立核验，不代替当前全库校验。'}
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
        return {'candidates':[],'evidence_gap':'chat_recovery_evidence_unavailable'}
