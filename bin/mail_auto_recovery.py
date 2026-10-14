"""Bounded mail recovery under the existing controller; no standalone scheduler.

Only metadata is stored here. A probe grants one attempt, never success. Actual
archive/attachment/semantic writers still own verification and commits.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

POLICY = 'mail-auto-recovery-v1'
DELAYS = (60, 300, 900, 3600)
PROBE_SECONDS = 21600
MANUAL = ('authentication', 'permission', 'denied', 'unauthorized', 'forbidden', 'login_required', 'auth_expired',
          'permission_changed', 'policy_changed', 'content_policy_unavailable', 'read_safety', 'approval_required')
TRANSIENT = ('timeout', 'unavailable', 'connection_', 'network_', 'http_429', 'http_50',
             'rate_limit', 'service_busy', 'service_error', 'bad_gateway', 'dws_invalid_json',
             'win11_download_failed', 'keychain_timeout', 'deadline_exceeded', 'resource_exhausted', 'internal_server_error', 'ratelimit', 'too_many_requests')


def api():
    import email_sync
    return email_sync


ATTACHMENT_REPAIR_POLICY = 'owner-bound-attachment-reanalysis-v1'

def attachment_repair_code_binding():
    from pathlib import Path
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('attachment_analysis.py','mail_auto_recovery.py')}

def attachment_repair_context(config, binding, previous_operation):
    """A fixed generation replaces only an explicitly authorized exhausted call.

    No hash miss, deployment, changed batch id or restart grants a new budget.
    The old operation and its responses remain untouched.
    """
    s=api()
    try:database=s.runtime_paths(config)['database']
    except KeyError:return None  # Isolated callers may not have a mail runtime.
    if not database.is_file():return None
    with s.connect_database_readonly(config) as c:
        row=c.execute('SELECT value FROM meta WHERE key=?',('attachment-reanalysis:'+previous_operation,)).fetchone()
    if not row:return None
    value=json.loads(row[0])
    import model_call_ledger as ledger
    if (value.get('policy')!=ATTACHMENT_REPAIR_POLICY or value.get('binding')!=binding
        or value.get('previous_operation')!=previous_operation
        or value.get('code_binding')!=attachment_repair_code_binding()
        or not value.get('authorization_sha256') or not value.get('test_receipts')
        or value.get('generation')!=ledger.digest([ATTACHMENT_REPAIR_POLICY,previous_operation,value.get('fix_version'),value.get('authorization_sha256')])):
        raise s.EmailSyncError('attachment_reanalysis_binding_changed','Retain repair grant and investigate its changed binding')
    from pathlib import Path
    if any(not Path(t['path']).is_file() or Path(t['path']).is_symlink() or s.sha256_file(Path(t['path']))!=t['sha256'] for t in value['test_receipts']):
        raise s.EmailSyncError('attachment_reanalysis_tests_changed','Retain repair grant and its test evidence')
    return {'scope':binding['scope'],'source_version':digest([binding,value['generation']]),
            'contract':binding['contract'],'repair_generation':value['generation']}

def authorize_attachment_reanalysis(config, semantic_id, document, source_rows, *, authorization, fix_version, test_receipts, only_previous_operations=None):
    """Maintenance entry point, called under the existing mail run lock.

    Records actual owner instructions and agent identity, not a forged human
    approval. It cannot grant uncertain, successful, uncalled or unrelated work.
    """
    from pathlib import Path
    selected = None if only_previous_operations is None else frozenset(only_previous_operations)
    import attachment_analysis as aa
    import email_batched_input as batches
    import model_call_ledger as ledger
    import model_router
    s=api()
    if not authorization or not fix_version or not test_receipts:raise ValueError('repair_authorization_missing')
    for test in test_receipts:
        p=Path(test['path'])
        if p.is_symlink() or not p.is_file() or s.sha256_file(p)!=test['sha256'] or test.get('passed') is not True:
            raise ValueError('repair_tests_unbound')
    with s.connect_database_readonly(config) as c:
        group=c.execute('SELECT * FROM semantic_groups WHERE semantic_id=?',(semantic_id,)).fetchone()
    if not group or group['status'] not in ('pending','failed'):raise ValueError('repair_task_not_pending')
    if len(source_rows)!=len(document['messages']):raise ValueError('repair_members_unbound')
    if [str(r['message_key']) for r in s.semantic_rows(config,semantic_id)] != [str(r['message_key']) for r in source_rows]:
        raise ValueError('repair_members_changed')
    scopes=[ledger.digest([config['email_sync']['mailbox'],row['message_key']]) for row in source_rows]
    root=model_router.STATE_DIR/'model-calls';grants=[]
    if not (root/'usage.sqlite').is_file():return []
    with sqlite3.connect((root/'usage.sqlite').absolute().as_uri()+'?mode=ro',uri=True) as usage:
        for mi,message in enumerate(document['messages']):
            for attachment in message.get('attachments',[]):
                text=attachment.get('extracted_text','')
                batches.require_file_permission(text,attachment.get('name','attachment'))
                if len(text)<aa.MIN_CHARS:continue
                scope=ledger.digest([scopes[mi],attachment.get('name'),attachment.get('inline')])
                for start,end,chunk in batches.text_chunks(text):
                    binding={'scope':scope,'text_sha256':ledger.digest(text),'start':start,'end':end,
                             'chunk_sha256':ledger.digest(chunk),'original_text_sha256':attachment.get('source_text_sha256',ledger.digest(text)),'contract':aa.CONTRACT}
                    context={'scope':scope,'source_version':ledger.digest(binding),'contract':aa.CONTRACT}
                    old=ledger.Admission(root,'',[],'email_attachment_analysis',context).key
                    if selected is not None and old not in selected:continue
                    row=usage.execute('SELECT status,request_sha FROM operations WHERE operation_id=?',(old,)).fetchone()
                    counts=usage.execute("SELECT count(*),sum(status IN ('started','uncertain','validated')) FROM attempts WHERE operation_id=?",(old,)).fetchone()
                    if not row or row[0]!='exhausted' or counts[0]!=3 or counts[1]:continue
                    generation=ledger.digest([ATTACHMENT_REPAIR_POLICY,old,fix_version,ledger.digest(authorization)])
                    value={'policy':ATTACHMENT_REPAIR_POLICY,'generation':generation,'binding':binding,
                           'previous_operation':old,'previous_request_sha256':row[1],'semantic_id':semantic_id,
                           'members_sha256':ledger.digest([r['message_key'] for r in source_rows]),
                           'authorization_sha256':ledger.digest(authorization),'authorization_source':'current_owner_request',
                           'executor':'codex','fix_version':fix_version,'code_binding':attachment_repair_code_binding(),
                           'test_receipts':test_receipts,'created_at':stamp()}
                    with s.connect_database(config) as c:
                        c.execute('BEGIN IMMEDIATE')
                        existing=c.execute('SELECT value FROM meta WHERE key=?',('attachment-reanalysis:'+old,)).fetchone()
                        if existing:
                            prior=json.loads(existing[0])
                            if any(prior.get(k)!=value[k] for k in ('generation','binding','code_binding')):
                                raise ValueError('repair_grant_already_bound')
                            value=prior
                        else:
                            c.execute('INSERT INTO meta(key,value) VALUES(?,?)',('attachment-reanalysis:'+old,json.dumps(value,sort_keys=True)))
                    grants.append(value)
    if grants:
        with s.connect_database(config) as c:
            initialize(c)
            for task in c.execute("SELECT task_key FROM mail_recovery_tasks WHERE object_key=? AND stage='semantic' AND state='needs_maintenance'",(semantic_id,)).fetchall():
                c.execute("UPDATE mail_recovery_tasks SET state='pending',next_due=?,updated_at=? WHERE task_key=?",(stamp(),stamp(),task['task_key']))
                c.execute('INSERT INTO mail_recovery_events(task_key,state,error_code,at) VALUES(?,?,?,?)',(task['task_key'],'tested_fix_granted','owner_bound_source_reanalysis',stamp()))
    return grants


def enabled(config):
    return config.get('email_sync', {}).get('auto_recovery_enabled', True) is True


def repair_storage(config):
    """Owned maintenance step for missing WAL sidecars, never a read fallback.

    Apple SQLite 3.54's readonly open can reject a WAL database last closed by
    the image worker's SQLite 3.45. Reproduce the exact failure, quiesce mail
    writers, let SQLite recreate its own sidecars, then independently reread.
    No immutable read, lock removal, table update or journal-mode change.
    """
    import fcntl, os, stat
    from pathlib import Path
    if not enabled(config): return {'status':'disabled'}
    s=api()
    try: paths=s.runtime_paths(config); path=paths['database']
    except (KeyError,ValueError):return {'status':'unavailable'}
    receipt_path=paths['state_root']/'state/mail-storage-recovery.json'
    history_path=receipt_path.with_suffix('.jsonl')
    lock_paths=[s.RUN_LOCK]+[paths['state_root']/'state'/('phase-'+kind+'.lock') for kind in ('image','win11')]
    def safe_path(candidate):
        return (candidate.is_absolute() and not any(p.is_symlink() for p in (candidate,*candidate.parents))
                and (not candidate.exists() or candidate.is_file()))
    if not all(safe_path(p) for p in (path,receipt_path,history_path,*lock_paths)):
        return {'status':'blocked','error_code':'mail_storage_unsafe_path'}
    if not path.is_file() or path.is_symlink(): return {'status':'unavailable'}
    def read_check():
        c=None
        try:
            c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)
            c.execute('SELECT message_key FROM messages LIMIT 1').fetchall()
        finally:
            if c is not None:c.close()
    try:
        read_check();return {'status':'healthy'}
    except sqlite3.OperationalError as exc:
        if str(exc)!='unable to open database file': return {'status':'blocked','error_code':'mail_storage_unverified'}
    except sqlite3.Error:
        return {'status':'blocked','error_code':'mail_storage_unverified'}
    handles=[]
    try:
        for lock_path in lock_paths:
            if lock_path.is_symlink():return {'status':'blocked','error_code':'mail_storage_unsafe_lock'}
            f=os.fdopen(os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600),'a+');handles.append(f)
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'status':'waiting','error_code':'mail_storage_writer_active'}
        try:
            read_check();return {'status':'healthy'}
        except sqlite3.OperationalError as exc:
            if str(exc)!='unable to open database file':return {'status':'blocked','error_code':'mail_storage_unverified'}
        if not all(safe_path(p) for p in (path,receipt_path,history_path,*lock_paths)):
            return {'status':'blocked','error_code':'mail_storage_unsafe_path'}
        # Both sidecars must be absent. Existing inaccessible/invalid sidecars
        # and permission failures are not repaired under this narrow contract.
        if any(os.path.lexists(str(path)+suffix) for suffix in ('-wal','-shm')):
            return {'status':'blocked','error_code':'mail_storage_sidecars_present'}
        for candidate in (path,path.parent):
            info=candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid!=os.getuid() or not os.access(candidate,os.R_OK|os.W_OK):
                return {'status':'blocked','error_code':'mail_storage_permissions'}
        with path.open('rb') as f:header=f.read(20)
        if header[:16]!=b'SQLite format 3\x00' or header[18:20]!=b'\x02\x02':
            return {'status':'blocked','error_code':'mail_storage_format'}
        before=s.sha256_file(path)
        c=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=1)
        try:
            c.execute('PRAGMA query_only=ON')
            if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                return {'status':'blocked','error_code':'mail_storage_integrity'}
        finally:c.close()
        read_check()
        if s.sha256_file(path)!=before:return {'status':'blocked','error_code':'mail_storage_snapshot_changed'}
        old=s.read_json(receipt_path,{})
        receipt={'status':'verified','at':stamp(),'sqlite_version':sqlite3.sqlite_version,
                 'database_sha256':before,'database_unchanged':True,'readonly_verified':True,
                 'repair_count':int(old.get('repair_count') or 0)+1}
        s.atomic_write_text(receipt_path,json.dumps(receipt,sort_keys=True)+'\n')
        with os.fdopen(os.open(history_path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600),'a') as f:
            f.write(json.dumps(receipt,sort_keys=True)+'\n')
        return receipt
    except (OSError,sqlite3.Error,ValueError):
        return {'status':'blocked','error_code':'mail_storage_repair_unavailable'}
    finally:
        for f in reversed(handles):f.close()


def now():
    return datetime.now().astimezone()


def stamp(value=None):
    return (value or now()).isoformat(timespec='seconds')


def parse(value):
    if not value: return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else None
    except (ValueError, TypeError): return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def category(code):
    code = str(code or '').lower()
    if code in ('image_download_retryable','model_quota_unavailable','model_timeout','model_unavailable','model_chain_exhausted','model_output_invalid','model_context_changed'): return 'transient'
    if code == 'model_source_changed': return 'input_changed'
    if code == 'image_download_auth_required': return 'manual'
    if code in ('image_download_blocked', 'image_download_unclassified'): return 'maintenance'
    if code.startswith('semantic_orphan_'): return 'maintenance'
    if code == 'content_source_access_changed': return 'probe_required'
    if any(x in code for x in MANUAL): return 'manual'
    if code in ('waiting_win11_power', 'waiting_power', 'already_running', 'memory_lock_busy',
                'memory_core_unverified', 'memory_core_busy', 'validation_inputs_changed', 'validation_failed'):
        return 'waiting'
    if code.startswith('semantic_input_'): return 'input_changed'
    if any(x in code for x in TRANSIENT): return 'transient'
    return 'maintenance'


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS mail_recovery_tasks(
      task_key TEXT PRIMARY KEY, object_key TEXT NOT NULL, stage TEXT NOT NULL,
      input_version TEXT NOT NULL, error_code TEXT NOT NULL, category TEXT NOT NULL,
      state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
      probe_count INTEGER NOT NULL DEFAULT 0, next_due TEXT, receipt_json TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS mail_recovery_events(
      sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL,
      state TEXT NOT NULL, error_code TEXT NOT NULL, at TEXT NOT NULL)''')


def observe(config, object_key, stage, version, code, *, attempted=False, encountered=False, at=None, retry_not_before=None):
    if not enabled(config): return None
    s = api(); current = at or now(); kind = category(code)
    key = digest([POLICY, object_key, stage, version])
    with s.connect_database(config) as c:
        initialize(c)
        old = c.execute('SELECT * FROM mail_recovery_tasks WHERE task_key=?', (key,)).fetchone()
        count = (int(old['attempt_count']) if old else 0) + int(attempted)
        if old and not attempted and not encountered: return dict(old)
        state = {'manual':'needs_user', 'maintenance':'needs_maintenance', 'waiting':'waiting',
                 'input_changed':'pending', 'probe_required':'waiting_probe', 'transient':'pending'}[kind]
        due = None
        if kind in ('transient','probe_required','waiting','input_changed'):
            seconds = DELAYS[max(0, count-1)] if count <= len(DELAYS) else PROBE_SECONDS
            if kind == 'probe_required' or count > len(DELAYS): state = 'waiting_probe'
            # Stable per-task jitter cannot reset the budget on a process restart.
            jitter = int(key[:4],16) % max(1, seconds//10)
            due = stamp(current + timedelta(seconds=seconds+jitter))
            floor = parse(retry_not_before)
            if floor and floor > parse(due): due = stamp(floor)
            old_due = parse(old['next_due']) if old else None
            if old_due and old_due > parse(due): due = stamp(old_due)
        c.execute('''INSERT INTO mail_recovery_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(task_key) DO UPDATE SET state=excluded.state,
          attempt_count=excluded.attempt_count,error_code=excluded.error_code,category=excluded.category,next_due=excluded.next_due,updated_at=excluded.updated_at''',
          (key,object_key,stage,version,str(code),kind,state,count,int(old['probe_count']) if old else 0,
           due,None,old['created_at'] if old else stamp(current),stamp(current)))
        c.execute('INSERT INTO mail_recovery_events(task_key,state,error_code,at) VALUES(?,?,?,?)',
                  (key,state,str(code),stamp(current)))
        return dict(c.execute('SELECT * FROM mail_recovery_tasks WHERE task_key=?',(key,)).fetchone())


def probe_source(config, row):
    """No model, download, unread-state change or business write; body stays in RAM.

    This proves only that the same message is readable now. The actual operation
    must recheck per-attachment authorization before accepting its output.
    """
    s = api()
    payload = s.run_dws(config, ['mail','message','get','--email',str(row['mailbox']),
                               '--id',str(row['message_id'])], timeout=30)
    message = payload.get('message') if isinstance(payload,dict) else None
    if not isinstance(message,dict) or str(message.get('id') or '') != str(row['message_id']):
        raise s.EmailSyncError('message_identity','Recovery source identity not verified')
    return {'source_identity_verified':True}


def grant_probe(config, task, row, *, at=None):
    s = api(); current=at or now()
    if task['state'] in ('needs_user','needs_maintenance','recovered','superseded'): return False
    if not parse(task['next_due']) or parse(task['next_due']) > current: return False
    proof=None; code=task['error_code']
    try: proof=probe_source(config,row)
    except Exception as exc: code=str(getattr(exc,'code',type(exc).__name__))
    kind=category(code)
    state='repairing' if proof else ('needs_user' if kind=='manual' else 'waiting_probe')
    with s.connect_database(config) as c:
        changed=c.execute('''UPDATE mail_recovery_tasks SET state=?,probe_count=probe_count+1,
          next_due=?,receipt_json=?,updated_at=? WHERE task_key=? AND updated_at=?''',
          (state,None if state=='needs_user' else stamp(current+timedelta(seconds=PROBE_SECONDS)),json.dumps(proof) if proof else None,
           stamp(current),task['task_key'],task['updated_at'])).rowcount
        if changed: c.execute('INSERT INTO mail_recovery_events(task_key,state,error_code,at) VALUES(?,?,?,?)',
                              (task['task_key'],state,code,stamp(current)))
    return bool(changed and proof)


def archive_grants(config, rows, *, at=None):
    """Revisit frozen transient/legacy failures without resetting original counters."""
    if not enabled(config): return set()
    grants=set(); s=api(); probes=0
    for row in rows:
        if row['archive_state']!='failed': continue
        code=str(row['last_error_code'] or '')
        task=observe(config,str(row['message_key']),'archive',s.archive_source_fingerprint(row),code,at=at)
        if category(code) not in ('transient','probe_required','waiting'): continue
        frozen=bool(row['archive_retry_exhausted_at']) or row['archive_failure_class']!='transient'
        if task['state'] in ('needs_user','needs_maintenance') or not parse(task['next_due']) or parse(task['next_due']) > (at or now()): continue
        if frozen or task['state']=='waiting_probe':
            if probes >= 3: continue
            probes += 1
            if grant_probe(config,task,row,at=at): grants.add(str(row['message_key']))
        else:
            grants.add(str(row['message_key']))
    return grants


def recovered(config, object_key, stage, receipt):
    if not enabled(config): return
    s=api()
    with s.connect_database(config) as c:
        initialize(c)
        keys=[r[0] for r in c.execute("SELECT task_key FROM mail_recovery_tasks WHERE object_key=? AND stage=? AND state!='recovered'",(object_key,stage))]
        for index,key in enumerate(keys):
            c.execute("UPDATE mail_recovery_tasks SET state='recovered',attempt_count=attempt_count+?,next_due=NULL,receipt_json=?,updated_at=? WHERE task_key=?",(int(index==0),json.dumps(receipt,sort_keys=True),stamp(),key))
            c.execute("INSERT INTO mail_recovery_events(task_key,state,error_code,at) VALUES(?,'recovered','',?)",(key,stamp()))


def summary(config):
    s=api(); empty={'enabled':enabled(config),'states':{},'actual_attempts':0,'probe_count':0,'next_due':None}
    if not s.runtime_paths(config)['database'].is_file(): return empty
    with s.connect_database_readonly(config) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='mail_recovery_tasks'").fetchone(): return empty
        counts={r[0]:r[1] for r in c.execute('SELECT state,count(*) FROM mail_recovery_tasks GROUP BY state')}
        row=c.execute('SELECT COALESCE(sum(attempt_count),0),COALESCE(sum(probe_count),0),min(next_due) FROM mail_recovery_tasks').fetchone()
    return {**empty,'states':counts,'actual_attempts':row[0],'probe_count':row[1],'next_due':row[2]}


def generation(config):
    """Only meaningful source changes wake mail, not timestamps/failure rereads."""
    s=api()
    with s.connect_database_readonly(config) as c:
        messages=[tuple(r) for r in c.execute('SELECT message_key,body_sha256,manifest_sha256 FROM messages ORDER BY message_key')]
        jobs=[tuple(r) for r in c.execute("SELECT job_key,result_sha256,CASE WHEN state='ignored' THEN 1 ELSE 0 END FROM attachment_jobs ORDER BY job_key")]
    return digest([messages,jobs])


def observe_generation(config):
    if not enabled(config):return
    s=api(); value=generation(config)
    with s.connect_database(config) as c: s.set_meta(c,'mail_recovery_observed_generation',value)


def scheduling_hint(config):
    s=api()
    if not enabled(config) or not s.runtime_paths(config)['database'].is_file():return None
    value=generation(config)
    with s.connect_database_readonly(config) as c:
        old=s.get_meta(c,'mail_recovery_observed_generation')
        # A new deployment reconciles current receipts once, even when mail is applied.
        due=c.execute("SELECT min(next_retry_at) FROM attachment_jobs WHERE state IN ('retry','waiting_power')").fetchone()[0]
        has_tasks=c.execute("SELECT 1 FROM sqlite_master WHERE name='mail_recovery_tasks'").fetchone()
        if has_tasks:
            ledger=c.execute("SELECT min(next_due) FROM mail_recovery_tasks WHERE state IN ('pending','waiting','waiting_probe','repairing')").fetchone()[0]
            due=min([x for x in (due,ledger) if x],default=None)
    if old!=value:return {'changed':True,'next_due':stamp()}
    return {'changed':False,'next_due':due}


def workbench_view(config):
    result=summary(config); states=result['states']
    result['phase']=('needs_user' if states.get('needs_user') else 'needs_maintenance' if states.get('needs_maintenance')
        else 'repairing' if states.get('repairing') else 'waiting' if any(states.get(k) for k in ('waiting_probe','waiting','waiting_input','blocked_input'))
        else 'pending' if states.get('pending') else 'idle')
    return result


def phase_ready(config, job):
    """After bounded expensive attempts, a cheap source probe gates each retry."""
    if not enabled(config): return True
    s=api(); code=job.get('last_error_code')
    if not code:return True
    kind=category(code)
    if kind in ('manual','maintenance'):
        with s.connect_database_readonly(config) as c:
            row=c.execute("SELECT state FROM mail_recovery_tasks WHERE object_key=? AND stage='attachment' AND input_version=?",(job['job_key'],digest(job['item_json']))).fetchone()
        if not (code in ('image_multiframe_requires_review','incomplete_content','RouterExhausted') and row and row['state']=='pending'):return False
    if kind=='waiting':return True
    task=observe(config,job['job_key'],'attachment',digest(job['item_json']),code)
    if kind=='probe_required' or int(task['attempt_count'])>=len(DELAYS):
        with s.connect_database_readonly(config) as c:
            row=c.execute('SELECT * FROM messages WHERE message_key=?',(job['message_key'],)).fetchone()
        return bool(row and grant_probe(config,task,row))
    return True


def reconcile_ledger(config):
    if not enabled(config):return
    s=api()
    with s.connect_database(config) as c:
        initialize(c)
        rows=c.execute("SELECT * FROM mail_recovery_tasks WHERE state NOT IN ('recovered','superseded')").fetchall()
        for row in rows:
            state=None;proof=None
            if row['stage']=='semantic':
                obj=c.execute('SELECT status,input_sha256,completed_at FROM semantic_groups WHERE semantic_id=?',(row['object_key'],)).fetchone()
                if obj and obj['status']=='superseded':state='superseded'
                if obj and obj['status']=='applied' and obj['completed_at']:
                    state='recovered';proof={'semantic_applied':True,'input_sha256':obj['input_sha256'],'at':obj['completed_at']}
            elif row['stage']=='attachment':
                obj=c.execute('SELECT * FROM attachment_jobs WHERE job_key=?',(row['object_key'],)).fetchone()
                if obj and obj['state']=='ignored':state='superseded'
                if obj and obj['state']=='needs_review':
                    state='needs_user' if category(obj['last_error_code'])=='manual' else 'needs_maintenance'
                    proof={'attachment_review_required':True,'error_code':obj['last_error_code']}
                if obj and obj['state']=='complete':
                    s.email_phases.verified_result(config,dict(obj));state='recovered';proof={'result_sha256':obj['result_sha256'],'committed':True}
            elif row['stage']=='archive':
                obj=c.execute('SELECT archive_state,archive_path,message_key,manifest_sha256 FROM messages WHERE message_key=?',(row['object_key'],)).fetchone()
                if obj and obj['archive_state']=='archived':
                    from pathlib import Path
                    if s.verify_archive(Path(obj['archive_path']),obj['message_key'])[0]:
                        state='recovered';proof={'manifest_sha256':obj['manifest_sha256'],'archive_verified':True}
            if state:
                c.execute('UPDATE mail_recovery_tasks SET state=?,receipt_json=?,next_due=NULL,updated_at=? WHERE task_key=?',(state,json.dumps(proof),stamp(),row['task_key']))
                c.execute('INSERT INTO mail_recovery_events(task_key,state,error_code,at) VALUES(?,?,?,?)',(row['task_key'],state,'',stamp()))



def record_semantic_blocks(config, result):
    if not enabled(config):return
    s=api();current=set()
    for item in result.get('blocked_scopes',[]):
        key=item['group_key'];current.add(key)
        observe(config,key,'semantic_scope',POLICY,item['error_code'])
        park(config,key,'semantic_scope','blocked_input')
    with s.connect_database(config) as c:
        initialize(c)
        blocked = result.get('blocked_semantic_ids') or []
        if result.get('block_all'):
            blocked = [row[0] for row in c.execute("SELECT semantic_id FROM semantic_groups WHERE status IN ('pending','failed')")]
    for sid in blocked:
        park(config,sid,'semantic','blocked_input')
    if not result.get('block_all'):
        with s.connect_database(config) as c:
            initialize(c)
            rows=c.execute("SELECT task_key,object_key FROM mail_recovery_tasks WHERE stage='semantic_scope' AND state NOT IN ('recovered','superseded')").fetchall()
            for row in rows:
                if row['object_key'] not in current:
                    c.execute("UPDATE mail_recovery_tasks SET state='superseded',next_due=NULL,updated_at=? WHERE task_key=?",(stamp(),row['task_key']))
                    c.execute("INSERT INTO mail_recovery_events(task_key,state,error_code,at) VALUES(?,'superseded','scope_reconciled',?)",(row['task_key'],stamp()))


def park(config, object_key, stage, state='waiting_input'):
    """No due for work whose prerequisite is unchanged; source generation wakes it."""
    if not enabled(config): return
    with api().connect_database(config) as c:
        initialize(c)
        rows=c.execute("SELECT task_key FROM mail_recovery_tasks WHERE object_key=? AND stage=? AND state IN ('pending','waiting','waiting_probe','repairing')",(object_key,stage)).fetchall()
        for row in rows:
            c.execute('UPDATE mail_recovery_tasks SET state=?,next_due=NULL,updated_at=? WHERE task_key=?',(state,stamp(),row['task_key']))
            c.execute('INSERT INTO mail_recovery_events(task_key,state,error_code,at) VALUES(?,?,?,?)',(row['task_key'],state,'prerequisite_unchanged',stamp()))


def release_repairs(config):
    """One version-bound grant after a tested deployment; preserve attempt counts."""
    from pathlib import Path
    s=api();root=Path(s.BRIDGE_ROOT)
    manifest=root/'state/sync-autonomy/release.json'
    if not manifest.is_file() or manifest.is_symlink():return 0
    value=s.read_json(manifest,{})
    if value.get('policy')!='sync-center-owner-approved-20260929-v1' or value.get('tests_passed') is not True:return 0
    bindings=value.get('code_binding',{})
    required=('email_sync.py','email_phases.py','sync_repair_rules.py','image_frame_coverage.py','mail_auto_recovery.py','mail_attachment_reader.py','mail_attachment_worker.ps1')
    if any(bindings.get(n)!=s.sha256_file(root/'bin'/n) for n in required):return 0
    tests=value.get('tests',[])
    if not tests or any(not Path(t['path']).is_file() or s.sha256_file(Path(t['path']))!=t['sha256'] for t in tests):return 0
    release=value.get('release_id');grants=0
    # Backfill only missing ledger rows from existing incomplete jobs; no attempts invented.
    with s.connect_database_readonly(config) as c:
        missing=[dict(r) for r in c.execute("SELECT j.* FROM attachment_jobs j WHERE j.state='needs_review' AND j.last_error_code='incomplete_content' AND NOT EXISTS (SELECT 1 FROM mail_recovery_tasks r WHERE r.object_key=j.job_key AND r.stage='attachment')")]
    for job in missing:observe(config,job['job_key'],'attachment',digest(job['item_json']),'incomplete_content')
    with s.connect_database(config) as c:
        initialize(c)
        rows=c.execute("SELECT * FROM mail_recovery_tasks WHERE state='needs_maintenance' AND error_code IN ('source_note_collision','image_multiframe_requires_review','incomplete_content','RouterExhausted')").fetchall()
        for row in rows:
            key='tested_fix:'+row['task_key']
            grant=release+':gif_visible_object-v1' if row['error_code']=='RouterExhausted' else release
            if s.get_meta(c,key)==grant:continue
            if row['error_code']=='source_note_collision' and row['stage']!='semantic':continue
            if row['error_code'] in ('image_multiframe_requires_review','incomplete_content') and row['stage']!='attachment':continue
            if row['error_code']=='RouterExhausted':
                job=c.execute('SELECT * FROM attachment_jobs WHERE job_key=?',(row['object_key'],)).fetchone()
                if row['stage']!='attachment' or not job or job['kind']!='image':continue
                message=c.execute('SELECT * FROM messages WHERE message_key=?',(job['message_key'],)).fetchone()
                expected=s.email_phases.image_source_identity(message,json.loads(job['item_json']))
                matched=False
                for receipt in (s.email_phases.root(config)/job['job_key']).glob('*/download.json'):
                    source=receipt.parent/'original.image';data=s.read_json(receipt,{})
                    if source.is_file() and not source.is_symlink() and data.get('source_identity_sha256')==expected and data.get('source_bytes')==source.stat().st_size and s.sha256_file(source)==data.get('source_sha256'):
                        with source.open('rb') as f:matched=f.read(6) in (b'GIF87a',b'GIF89a')
                    if matched:break
                if not matched:continue
            if row['error_code']=='incomplete_content':
                job=c.execute('SELECT * FROM attachment_jobs WHERE job_key=?',(row['object_key'],)).fetchone()
                if not job or job['kind']!='win11':continue
                result=s.email_phases.verified_result(config,dict(job))
                old=s.read_json(Path(job['result_path']).parent/'win11.json',{})
                nodes=old.get('coverage',{}).get('nodes',[])
                gaps=[n for n in nodes[1:] if n.get('status')!='complete']
                if not gaps or not all(n.get('status')=='deferred_media' and str(n.get('name','')).endswith('.bin') for n in gaps):continue
                if result.get('worker_sha256')==config['email_sync']['attachment_processing']['worker_script_sha256']:continue
            c.execute("UPDATE mail_recovery_tasks SET state='pending',next_due=?,updated_at=? WHERE task_key=? AND state='needs_maintenance'",(stamp(),stamp(),row['task_key']))
            if row['stage']=='attachment':
                c.execute("UPDATE attachment_jobs SET state='retry',next_retry_at=? WHERE job_key=? AND state='needs_review' AND last_error_code IN ('image_multiframe_requires_review','incomplete_content','RouterExhausted')",(stamp(),row['object_key']))
            s.set_meta(c,key,grant)
            c.execute('INSERT INTO mail_recovery_events(task_key,state,error_code,at) VALUES(?,?,?,?)',(row['task_key'],'tested_fix_granted',row['error_code'],stamp()))
            grants+=1
    return grants
