"""Durable, bounded local ledger recovery; never replay business operations."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import contextlib, fcntl, hashlib, json, os, sqlite3, tempfile, time, sys
from pathlib import Path
from datetime import datetime, timedelta

VERSION = 1
DELAYS = (60, 120, 300)
BINDING_NAMES = ('ledger_recovery.py','sqlite_write_transactions.py','business_sync_state.py',
                 'business_sync_supervisor.py','business_sync_supervisor_health.py',
                 'operational_workbench.py','media_actions.py','media_sources.py','supervisor_incidents.py','supervisor_health_recovery.py')
LOADED_BINDING = {n:hashlib.sha256((Path(__file__).parent/n).read_bytes()).hexdigest()
                  for n in BINDING_NAMES if (Path(__file__).parent/n).exists()}

class Deferred(RuntimeError):
    code = 'ledger_retry_wait'


def now(): return datetime.now().astimezone()
def stamp(value=None): return (value or now()).isoformat(timespec='seconds')
def read(path):
    try: return json.loads(path.read_text())
    except FileNotFoundError: return {}

def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.ledger-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,ensure_ascii=False,sort_keys=True); f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def directory(db): return Path(db).parent/'ledger-recovery'
def key(phase, identity=''): return hashlib.sha256((phase+':'+identity).encode()).hexdigest()
@contextlib.contextmanager
def locked(path, blocking=True):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('a') as f:
        try: fcntl.flock(f.fileno(),fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False;return
        try:yield True
        finally:fcntl.flock(f.fileno(),fcntl.LOCK_UN)

def busy(exc):
    if not isinstance(exc,sqlite3.Error):return False
    e=getattr(exc,'safe_sqlite_evidence',{})
    if e.get('commit_outcome_ambiguous') or e.get('outcome')=='commit_outcome_ambiguous_no_replay':return False
    code=e.get('sqlite_code',getattr(exc,'sqlite_errorcode',None))
    return (type(code) is int and code & 255 == 5) or (code is None and str(exc) in ('database is locked','database is busy'))

def path_for(db,phase,identity=''):return directory(db)/(key(phase,identity)+'.json')

def due(db,phase,identity='',at=None):
    d=read(path_for(db,phase,identity));at=at or now()
    if d.get('status')=='blocked':return False
    return not d.get('next_retry_at') or datetime.fromisoformat(d['next_retry_at'])<=at

def native_owner():
    import subprocess
    try:
        # The independent health sentinel is launched directly by launchd.
        # Match its live job PID and exact entrypoint, not just a process name.
        if os.getppid()==1 and Path(sys.argv[0]).resolve()==Path(__file__).parent/'business_sync_supervisor_health.py':
            output=subprocess.check_output(['/bin/launchctl','print',f'gui/{os.getuid()}/com.example.business-sync-supervisor-health'],text=True,timeout=2)
            return any(line.strip()==f'pid = {os.getpid()}' for line in output.splitlines())
        fields=subprocess.check_output(['/bin/ps','-p',str(os.getppid()),'-o','ppid=','-o','comm='],text=True,timeout=2).strip().split(None,1)
        return len(fields)==2 and fields[0]=='1' and fields[1].endswith('/BUSINESS Sync Supervisor.app/Contents/MacOS/business-sync-supervisor-launcher')
    except (OSError,subprocess.SubprocessError):return False
_NATIVE = None

def native():
    global _NATIVE
    if _NATIVE is None:_NATIVE=native_owner()
    return _NATIVE

def failure(db,phase,exc,identity='',at=None):
    at=at or now();p=path_for(db,phase,identity)
    with locked(p.with_suffix('.lock')):
        d=read(p); old=d.get('last_failure_at');history=d.get('failure_times',[])
        history=[x for x in history if datetime.fromisoformat(x)>=at-timedelta(hours=1)]
        history.append(stamp(at));count=int(d.get('attempts',0))+1
        # A new recurrence starts a new observation episode, retaining history.
        start=d.get('first_failure_at') if d.get('status') in ('retry_wait','running','blocked') else stamp(at)
        consecutive=int(d.get('consecutive_failures',0))+1 if d.get('status') in ('retry_wait','running') else 1
        recoverable=busy(exc)
        d.update(schema_version=VERSION,phase=phase,identity=identity,status='retry_wait' if recoverable else 'blocked',
                 first_failure_at=start,last_failure_at=stamp(at),failure_times=history[-64:],attempts=count,consecutive_failures=consecutive,
                 next_retry_at=stamp(at+timedelta(seconds=DELAYS[min(consecutive-1,2)])) if recoverable else None,
                 error_code='sqlite_database_busy' if recoverable else 'ledger_result_uncertain_or_non_busy',
                 escalated=(at-datetime.fromisoformat(start)).total_seconds()>=900 or len(history)>=3,
                 binding=LOADED_BINDING)
        atomic(p,d)
    # Non-supervisor ledger writers must also enter the independent incident
    # inbox. The supervisor and sentinel already have their own durable recorder.
    root=Path(__file__).resolve().parents[1]
    if Path(sys.argv[0]).name not in ('business_sync_supervisor.py','business_sync_supervisor_health.py') and (root/'state/ledger-selfheal-policy.json').is_file():
        import supervisor_incidents as incidents
        from sqlite_read_diagnostics import error_evidence
        evidence=getattr(exc,'safe_sqlite_evidence',None) or error_evidence(exc,Path(db),'ledger_write')
        evidence=dict(evidence,operation=phase)
        incidents.retain(root,dict(recorded_at=stamp(at),error_type=type(exc).__name__,
            reason='sqlite_database_busy' if recoverable else 'ledger_write_blocked',pid=os.getpid(),sqlite=evidence))


def success(db,phase,identity='',at=None):
    at=at or now();p=path_for(db,phase,identity)
    # Retain evidence per operation, coalescing quiet heartbeats to avoid adding contention.
    d=read(p)
    if d.get('last_success_at') and d.get('binding')==LOADED_BINDING and d.get('status')=='observing' and (at-datetime.fromisoformat(d['last_success_at'])).total_seconds()<30 and (not native() or (d.get('last_native_success',{}).get('binding')==LOADED_BINDING and (at-datetime.fromisoformat(d['last_native_success']['at'])).total_seconds()<30)):return
    with locked(p.with_suffix('.lock')):
        d=read(p)
        if d.get('first_failure_at') and d.get('status')=='retry_wait':d['attempts']=int(d.get('attempts',0))+1
        receipt={'at':stamp(at),'pid':os.getpid(),'native':native(),'binding':LOADED_BINDING}
        d.update(schema_version=VERSION,phase=phase,identity=identity,status='observing',last_success_at=stamp(at),
                 next_retry_at=None,consecutive_failures=0,binding=LOADED_BINDING)
        d['successes']=(d.get('successes',[])+[receipt])[-16:]
        if receipt['native']:d['last_native_success']=receipt
        atomic(p,d)

def run(db,phase,callback,identity='',ignore_due=False):
    if not ignore_due and not due(db,phase,identity):raise Deferred(phase)
    try:value=callback()
    except sqlite3.Error as exc:
        try:failure(db,phase,exc,identity)
        except OSError:pass
        raise
    # Failure to write diagnostic evidence must never turn a committed operation into a retry.
    try:success(db,phase,identity)
    except (OSError,ValueError,TypeError,KeyError):pass
    return value

def spool(db,token,payload):
    p=directory(db)/'delivery-outbox'/(token+'.json')
    with locked(p.with_suffix('.lock')):
        if not p.exists():atomic(p,{'token':token,'status':'pending','payload':payload,'created_at':stamp()})
    return p

def finish_spool(path):
    with locked(path.with_suffix('.lock')):
        d=read(path);d.update(status='applied',applied_at=stamp());atomic(path,d)

def flush(db):
    import business_sync_state as state
    total=0
    for p in sorted((directory(db)/'delivery-outbox').glob('*.json')):
        d=read(p)
        if d.get('status')!='pending':continue
        try:state._apply_delivery_payload(d['payload'],db,d['token'])
        except (sqlite3.Error,Deferred):continue
        finish_spool(p);total+=1
    return total

def summary(db):
    rows=[read(p) for p in directory(db).glob('*.json')]
    active=[r for r in rows if r.get('first_failure_at') and r.get('status')!='recovered']
    if not active:return {}
    pending=[r for r in active if r.get('status') in ('retry_wait','running','blocked')]
    escalated=any(r.get('escalated') or (r.get('status')=='retry_wait' and (now()-datetime.fromisoformat(r['first_failure_at'])).total_seconds()>=900) for r in active)
    return {'phase':'blocked' if any(r.get('status')=='blocked' for r in active) else 'retry_wait' if pending else 'observing',
            'automatic_attempts':sum(r.get('attempts',0) for r in active),
            'last_attempt':max((r.get('last_failure_at','') for r in active),default=None),
            'last_success':max((r.get('last_success_at','') for r in active),default=None),
            'next_retry_at':min((r['next_retry_at'] for r in pending if r.get('next_retry_at')),default=None),
            'escalated':escalated,'requires_user':any(r.get('status')=='blocked' for r in active)}


def model_ledger_read_fault(e):
    sql=e.get('sqlite',{})
    return (sql.get('sqlite_code')==14 and sql.get('operation')=='workbench_items'
        and sql.get('phase')=='execute' and sql.get('outcome')=='failed'
        and sql.get('commit_outcome_ambiguous') is False
        and any(f.get('file')=='bin/model_call_ledger.py' and f.get('function')=='report' for f in e.get('frames',[])))

def incident_phase(e):
    if model_ledger_read_fault(e):return 'workbench_items'
    allowed={'initialize','update_source','update_runtime','open_alert','resolve_alert','record_recovery',
             'mark_all_due','claim_delivery','mark_delivery','reserve_notification','workbench_items',
             'workbench_groups','workbench_manual_links'}
    code=e.get('sqlite',{}).get('sqlite_code')
    if type(code) is not int or code & 255 != 5:return None
    operation=e.get('sqlite',{}).get('operation')
    if operation in allowed:return operation
    if e.get('sqlite',{}).get('phase')=='sentinel_initialize':return 'initialize'
    for frame in reversed(e.get('frames',[])):
        if frame.get('function') in allowed:return frame['function']
    return None


def verify_incidents(root,db):
    """Live operation receipts plus current full validation, not a PID or a probe."""
    import supervisor_incidents as incidents
    import automatic_memory_validation as validation
    import validation_evidence
    root=Path(root);db=Path(db);policy_path=root/'state/ledger-selfheal-policy.json'
    policy=read(policy_path)
    if policy.get('schema_version')!=VERSION:return 0
    binding=incidents.code_binding(root)
    # The tested recovery capability must remain unchanged. Other code changes
    # reset the natural observation window via their runtime bindings below.
    if any(binding.get(n)!=v for n,v in policy.get('capability_binding',{}).items()) or not policy.get('capability_binding'):return 0
    tests=policy.get('test_evidence',[])
    if not tests or any(hashlib.sha256(Path(t['path']).read_bytes()).hexdigest()!=t['sha256'] for t in tests):return 0
    if read(root/'state/maintenance-mode.json').get('status')!='inactive':return 0
    native_path=root/'bin/BUSINESS Sync Supervisor.app/Contents/MacOS/business-sync-supervisor-launcher'
    if hashlib.sha256(native_path.read_bytes()).hexdigest()!=policy.get('native_binary_sha256'):return 0
    current=now();deployed=datetime.fromisoformat(policy['deployed_at']);pending=[]
    for p in incidents.inbox(root).glob('*.json'):
        if len(p.stem)!=64:continue
        d=read(p)
        if d.get('root_cause_status')=='verified' or d.get('recovery_status')=='verified':continue
        e=d.get('evidence',{});phase=incident_phase(e)
        if hashlib.sha256(json.dumps(e,sort_keys=True).encode()).hexdigest()!=p.stem:phase=None
        pending.append((p,d,phase))
    if not pending:
        # Recovery evidence may have been saved just before the final alert
        # transaction failed. Retry that local closure without rerunning work.
        if incidents.unresolved(root)==0:
            import business_sync_state as state
            existing=read(root/'state/supervisor-recovery-status.json')
            if existing and existing.get('phase')!='verified':
                state.resolve_alert('system','supervisor_runtime_error',title='后台数据库冲突已自动恢复',
                    detail='逐项恢复凭据已经保存，已补齐问题状态登记；原失败记录保留。',database=db)
                existing.update(phase='verified',pending_incidents=0,missing_paths=[],requires_user=False,updated_at=stamp())
                atomic(root/'state/supervisor-recovery-status.json',existing)
        return 0
    latest_error=read(root/'state/sync-supervisor-last-error.json')
    latest_at=datetime.fromisoformat(latest_error['recorded_at']) if latest_error.get('recorded_at') else current
    operations=[read(p) for p in directory(db).glob('*.json')]
    after=max([deployed,latest_at]+[datetime.fromisoformat(x['last_failure_at']) for x in operations if x.get('last_failure_at')])
    cycles=[r for r in read(root/'state/supervisor-attention-receipts.json').get('receipts',[])
            if r.get('code_binding')==binding and r.get('attention_reconciliation')=='passed'
            and r.get('execution_owner')=='launchd_native_supervisor'
            and r.get('native_binary_sha256')==policy.get('native_binary_sha256')
            and datetime.fromisoformat(r['checked_at'])>after]
    cycles=sorted({r['pid']:r for r in cycles}.values(),key=lambda r:r['checked_at'])
    operations=[read(p) for p in directory(db).glob('*.json')]
    unresolved_writes=[r for r in operations if r.get('status') in ('retry_wait','running','blocked')]
    blocked_phases={r.get('phase') for r in unresolved_writes}
    pending_phases={phase for _,_,phase in pending}
    success_by_phase={}
    for r in operations:
        receipt=r.get('last_native_success',{})
        if r.get('phase') not in blocked_phases and receipt.get('binding')=={n:binding[n] for n in BINDING_NAMES} and receipt.get('at') and datetime.fromisoformat(receipt['at'])>after:
            success_by_phase[r['phase']]=receipt
    waiting=sorted({phase or 'unknown_failure_path' for _,_,phase in pending if phase not in success_by_phase})
    status=dict(schema_version=VERSION,updated_at=stamp(current),phase='observing',
                pending_incidents=len(pending),missing_paths=waiting,requires_user=any(phase is None for _,_,phase in pending),
                automatic_attempts=sum(r.get('attempts',0) for r in operations),
                last_attempt=max((r.get('last_failure_at','') for r in operations),default=None),
                last_success=max((r.get('last_success_at','') for r in operations),default=None),
                next_retry_at=min((r['next_retry_at'] for r in unresolved_writes if r.get('next_retry_at')),default=None),
                code_binding=binding)
    span=(datetime.fromisoformat(cycles[-1]['checked_at'])-datetime.fromisoformat(cycles[0]['checked_at'])).total_seconds() if len(cycles)>1 else 0
    status.update(native_cycles=len(cycles),observation_seconds=int(span),
                  next_check_at=stamp(current+timedelta(seconds=60)),full_validation_current=False)
    status['escalated']=any(r.get('escalated') for r in operations)
    def defer(reason):
        status.update(check_blocker=reason)
        atomic(root/'state/supervisor-recovery-status.json',status)
        return 0
    atomic(root/'state/supervisor-recovery-status.json',status)
    # A retained manual-only failure must not block unrelated proven paths.
    # Unknown/unregistered pending writes still fail closed for the whole group.
    if blocked_phases-pending_phases or (pending_phases and pending_phases<=blocked_phases):return defer('writes_pending')
    if len(cycles)<2 or span<600:return defer('stable_observation')
    # Reuse the established source-controller lock order. Never wait holding either.
    with validation.try_lock(root/'state/sync-center-control.lock') as control:
        if control is None:return defer('source_busy')
        with validation.try_lock(root/'state/memory-index.lock') as index_lock:
            if index_lock is None:return defer('index_busy')
            full=read(root/'state/last-memory-validation-success.json')
            failed=read(root/'state/last-memory-validation-failure.json')
            status['full_validation_at']=full.get('recorded_at')
            if not full.get('recorded_at') or datetime.fromisoformat(full['recorded_at'])<=after:return defer('validation_pending')
            if failed and (not failed.get('recorded_at') or failed['recorded_at']>=full['recorded_at']):return defer('validation_failed')
            index=validation.index_state(root)
            if not validation_evidence.receipt_is_current(root,full,index['dirty'],index['applied']):return defer('validation_changed')
            status['full_validation_current']=True
            if incidents.code_binding(root)!=binding or read(root/'state/sync-supervisor-last-error.json')!=latest_error:return defer('evidence_changed')
            verified=0
            for p,d,phase in pending:
                if phase not in success_by_phase:continue
                # Confirm the exact original event before attaching recovery evidence.
                if read(p)!=d:continue
                d.update(recovery_status='verified',recovery_kind='automatic_ledger_contention_recovery',
                         recovered_at=stamp(),historical_lock_owner=d['evidence'].get('sqlite',{}).get('attribution',{}).get('lock_owner','unknown'),
                         recovery_evidence={'operation':success_by_phase[phase], 'natural_cycles':[cycles[0],cycles[-1]],
                                            'full_validation_receipt_at':full['recorded_at'],
                                            'policy_sha256':hashlib.sha256(policy_path.read_bytes()).hexdigest()})
                incidents.atomic_json(p,d);verified+=1
            remaining=[(p,d,phase) for p,d,phase in pending if read(p).get('recovery_status')!='verified']
            status.update(pending_incidents=incidents.unresolved(root),
                          missing_paths=sorted({phase or 'unknown_failure_path' for _,_,phase in remaining}),
                          check_blocker='operation_receipt_pending' if remaining else None,
                          requires_user=any(phase is None for _,_,phase in remaining))
            atomic(root/'state/supervisor-recovery-status.json',status)
            if status['pending_incidents']==0:
                import business_sync_state as state
                state.resolve_alert('system','supervisor_runtime_error',title='后台数据库冲突已自动恢复',
                    detail='原失败路径已真实成功，两个自然周期与当前完整校验通过，观察期无新故障；原错误记录保留。',database=db)
                for p in directory(db).glob('*.json'):
                    with locked(p.with_suffix('.lock')):
                        d=read(p)
                        if d.get('status')=='observing':d.update(status='recovered',recovered_at=stamp());atomic(p,d)
                status.update(phase='verified',pending_incidents=0,missing_paths=[],requires_user=False,next_check_at=None)
                atomic(root/'state/supervisor-recovery-status.json',status)
            return verified


def verified_wait(db, phase, at=None):
    """Only a current, bounded busy episode with a real future due time is a wait."""
    try:
        from datetime import datetime
        if not isinstance(phase, str) or not phase or len(phase)>80:
            return None
        d=read(path_for(db,phase));at=at or now()
        retry=datetime.fromisoformat(d['next_retry_at'])
        failure_at=datetime.fromisoformat(d['last_failure_at'])
        count=d['consecutive_failures']
        if (d.get('schema_version')!=VERSION or d.get('phase')!=phase or d.get('identity')!=''
                or d.get('status')!='retry_wait' or d.get('error_code')!='sqlite_database_busy'
                or d.get('binding')!=LOADED_BINDING or type(count) is not int or count<1
                or retry.tzinfo is None or failure_at.tzinfo is None or failure_at>at or retry<=at
                or (retry-at).total_seconds()>max(DELAYS)
                or not 0<(retry-failure_at).total_seconds()<=max(DELAYS)):
            return None
        return {'reason':'ledger_retry_wait','next_retry_at':retry.isoformat(timespec='seconds')}
    except (OSError, ValueError, TypeError, KeyError):
        return None
