"""Verify index recovery without replaying source work or claiming a historical cause."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib,json,os,time
from datetime import datetime,timedelta
from pathlib import Path
import automatic_memory_validation as validation
import index_incident_evidence as evidence
import supervisor_incidents as incidents
import validation_evidence
import ledger_recovery as ledger
import supervisor_liveness as liveness
NAMES=('index_runtime_recovery.py','action_run_owner.py','index_incident_evidence.py','automatic_memory_validation.py','validation_retry.py','receiver.py','handoff_context.py','business_sync_supervisor.py','supervisor_incidents.py','ledger_recovery.py','validation_evidence.py')
LOADED={n:hashlib.sha256((Path(__file__).parent/n).read_bytes()).hexdigest() for n in NAMES}

CLOCK_KIND='system_clock_monotonic_v1'
def system_monotonic():
    # Python 3.9 on macOS offsets time.monotonic() separately in each process.
    return time.clock_gettime(time.CLOCK_MONOTONIC)

def read(path):return validation.read_object(Path(path))
def parsed(s):return validation.parsed(s)
def binding(root):
    return {**incidents.code_binding(root),**{n:hashlib.sha256((root/'bin'/n).read_bytes()).hexdigest() for n in NAMES}}
def pending(root):
    out=[]
    for p in sorted((root/'state/supervisor-incidents').glob('*.json')):
        if len(p.stem)!=64:continue
        d=read(p)
        if d.get('root_cause_status')=='verified' or d.get('recovery_status')=='verified':continue
        out.append((p,d))
    return out

def classification(path,d,policy):
    e=d.get('evidence',{})
    if evidence.digest(e)!=path.stem or d.get('incident_id')!=path.stem:return None
    frames=e.get('frames',[])
    if not frames or frames[-1].get('file')!='bin/automatic_memory_validation.py' or frames[-1].get('function')!='index_state':return None
    if e.get('error_type')!='ValidationFault':return None
    if e.get('fault_code') in evidence.CODES:return e['fault_code']
    legacy=policy.get('legacy_incidents',{}).get(path.stem,{})
    if legacy.get('evidence_sha256')!=path.stem:return None
    if legacy.get('frames_sha256')!=evidence.digest(frames):return None
    if legacy.get('function_sha256')!=frames[-1].get('loaded_code_sha256'):return None
    return legacy.get('fault_code') if legacy.get('fault_code') in evidence.CODES else None

def policy_current(root,policy):
    if policy.get('schema_version')!=1 or not parsed(policy.get('deployed_at')):return False
    actual=binding(root)
    if policy.get('code_binding')!=actual:return False
    if any(actual.get(n)!=h for n,h in LOADED.items()):return False
    tests=policy.get('test_evidence',[])
    if not tests or any(hashlib.sha256(Path(t['path']).read_bytes()).hexdigest()!=t['sha256'] for t in tests):return False
    proof=policy.get('legacy_verification',{})
    if policy.get('legacy_incidents'):
        if not proof.get('path') or hashlib.sha256(Path(proof['path']).read_bytes()).hexdigest()!=proof.get('sha256'):return False
        if read(proof['path']).get('legacy_incidents')!=policy['legacy_incidents']:return False
    native=root/'bin/BUSINESS Sync Supervisor.app/Contents/MacOS/business-sync-supervisor-launcher'
    return hashlib.sha256(native.read_bytes()).hexdigest()==policy.get('native_binary_sha256')

def failure_snapshot(root,rows):
    values={p.name:evidence.digest(d) for p,d in rows}
    values['latest_runtime']=evidence.digest(read(root/'state/sync-supervisor-last-error.json'))
    values['latest_validation_failure']=evidence.digest(read(root/'state/last-memory-validation-failure.json'))
    for p in sorted((root/'state/index-failures').glob('*.json')):
        d=read(p);sha=d.pop('evidence_sha256',None)
        if sha!=evidence.digest(d):raise ValueError('index_failure_evidence_invalid')
        values['index:'+p.name]=sha
    return evidence.digest(values)

def verify(root,database):
    root=Path(root);database=Path(database)
    with validation.try_lock(root/'state/index-recovery.lock') as owner:
        if owner is None:return 0
        policy=read(root/'state/index-recovery-policy.json')
        if not policy_current(root,policy):
            prior=read(root/'state/index-recovery-status.json')
            evidence.atomic(root/'state/index-recovery-status.json',{**prior,'schema_version':1,'phase':'blocked','check_blocker':'recovery_policy_changed','full_validation_current':False,'requires_user':False,'decision_required':False,'technical_investigation_required':True,'evidence_gaps':['恢复程序与测试凭据不匹配，暂停闭环'],'updated_at':datetime.now().astimezone().isoformat(timespec='seconds')})
            return 0
        # Preserve the tested ledger implementation and its independent receipts.
        incidents.import_history(root)
        ledger.verify_incidents(root,database)
        return _verify(root,database,policy)

def _verify(root,database,policy):
    rows=pending(root);known=[(p,d) for p,d in rows if classification(p,d,policy)]
    ledger_view=read(root/'state/supervisor-recovery-status.json')
    old=read(root/'state/index-recovery-status.json')
    current=datetime.now().astimezone();stamp=current.isoformat(timespec='seconds')
    fingerprint=failure_snapshot(root,rows)
    ctx_binding=binding(root)
    same=old.get('failure_snapshot')==fingerprint and old.get('code_binding')==ctx_binding
    view={'schema_version':1,'phase':'observing','updated_at':stamp,'pending_incidents':len(rows),'index_pending_incidents':len(known),
          'failure_snapshot':fingerprint,'code_binding':ctx_binding,'check_blocker':None,'full_validation_current':False,
          'requires_user':False,'decision_required':False,'technical_investigation_required':any(not classification(p,d,policy) and ledger.incident_phase(d.get('evidence',{})) is None for p,d in rows),
          'last_attempt':stamp,'last_success':old.get('last_success') if same else None,
          'event_verification_count':old.get('event_verification_count',0)+1 if same else 1,
          'actual_repair_count':0,'historical_database_attempts':sum(r.get('attempts',0) for r in (read(p) for p in ledger.directory(database).glob('*.json'))),
          'next_check_at':(current+timedelta(seconds=60)).isoformat(timespec='seconds'),'cycles':old.get('cycles',[]) if same else []}
    def save(reason):
        view['check_blocker']=reason
        view['evidence_gaps']=(['原索引故障恢复凭据待核验'] if known else [])+(['存在尚未识别的技术故障'] if view['technical_investigation_required'] else [])
        evidence.atomic(root/'state/index-recovery-status.json',view)
        # Overlay only diagnostics; never claim a source operation succeeded.
        merged={**ledger_view,**{k:v for k,v in view.items() if k not in {'cycles','failure_snapshot'}}}
        merged['missing_paths']=(['index_state'] if known else [])+(['unknown_failure_path'] if view['technical_investigation_required'] else [])
        merged['automatic_attempts']=view['event_verification_count']
        evidence.atomic(root/'state/supervisor-recovery-status.json',merged)
        return 0
    if not rows:
        # Incident receipts are durable before any alert or UI update; this repairs interrupted closure.
        if incidents.unresolved(root)==0:
            import business_sync_state as state
            state.resolve_alert('system','supervisor_runtime_error',title='后台异常已核验恢复',detail='原失败环节、当前完整校验和自然运行观察均已取得恢复凭据；历史根因未补写。',database=database)
            view.update(phase='verified',next_check_at=None,full_validation_current=None,full_validation_at=old.get('full_validation_at'),evidence_gaps=[],missing_paths=[],index_pending_incidents=0)
            view['historical_database_attempts']=old.get('historical_database_attempts',view['historical_database_attempts'])
            return save(None)
    if not known:return save(ledger_view.get('check_blocker') or 'technical_investigation')
    if read(root/'state/maintenance-mode.json').get('status')!='inactive':return save('maintenance_active')
    if not ledger.native_owner():return save('native_cycle_pending')
    identity=liveness.process_identity(os.getpid())
    if not identity:return save('native_cycle_pending')
    native_receipts=read(root/'state/supervisor-attention-receipts.json').get('receipts',[])
    if not any(x.get('pid')==os.getpid() and x.get('process_identity')==identity and x.get('loaded_supervisor_sha256')==ctx_binding.get('business_sync_supervisor.py') and x.get('attention_reconciliation')=='passed' and x.get('code_binding')==incidents.code_binding(root) and x.get('execution_owner')=='launchd_native_supervisor' for x in native_receipts):return save('native_cycle_pending')
    with validation.try_lock(root/'state/sync-center-control.lock') as control:
        if control is None:return save('source_busy')
        with validation.try_lock(root/'state/memory-index.lock') as index_lock:
            if index_lock is None:return save('index_busy')
            try:index=validation.index_state(root)
            except validation.ValidationFault as exc:
                if exc.code not in evidence.CODES:raise
                view['cycles']=[]
                return save(exc.code)
            if not index['ready']:
                view['cycles']=[]
                return save('memory_index_update_pending')
            success=read(root/'state/last-memory-validation-success.json');failure=read(root/'state/last-memory-validation-failure.json')
            times=[parsed(policy['deployed_at'])]
            for _,d in rows:
                t=parsed(d.get('evidence',{}).get('recorded_at'))
                if not t:return save('failure_time_unavailable')
                times.append(t)
            latest=read(root/'state/sync-supervisor-last-error.json')
            if latest:
                t=parsed(latest.get('recorded_at'))
                if not t:return save('failure_time_unavailable')
                times.append(t)
            for p in (root/'state/index-failures').glob('*.json'):
                t=parsed(read(p).get('recorded_at'))
                if not t:return save('failure_time_unavailable')
                times.append(t)
            if failure:
                t=parsed(failure.get('recorded_at'))
                if not t:return save('failure_time_unavailable')
                times.append(t)
            after=max(times);success_at=parsed(success.get('recorded_at'))
            if not success_at or success_at<=after:
                view['cycles']=[];return save('validation_pending')
            if not validation_evidence.receipt_is_current(root,success,index['dirty'],index['applied']):
                view['cycles']=[];return save('validation_changed')
            active=read(root/'state/index-runtime-active-fault.json')
            if active.get('status')=='pending':
                evidence.atomic(root/'state/index-runtime-active-fault.json',{**active,'status':'recovered','recovered_at':stamp})
            view.update(full_validation_current=True,full_validation_at=success['recorded_at'],last_success=stamp)
            index_stat=(root/'state/bridge.sqlite').stat()
            instance={'device':index_stat.st_dev,'inode':index_stat.st_ino}
            context=evidence.digest([success.get('validation_context'),index,instance,ctx_binding])
            boot=validation.boot_identity()
            if boot=='unknown':return save('boot_identity_unavailable')
            cycles=[r for r in view['cycles'] if r.get('clock')==CLOCK_KIND and r.get('context')==context and r.get('boot')==boot and parsed(r.get('at')) and parsed(r['at'])>after]
            if not any(r['pid']==os.getpid() for r in cycles):cycles.append({'pid':os.getpid(),'process_identity':identity,'at':stamp,'monotonic':system_monotonic(),'clock':CLOCK_KIND,'boot':boot,'context':context,'index_state':'passed','execution_owner':'launchd_native_supervisor'})
            view['cycles']=cycles[-128:];view['native_cycles']=len(cycles);view['observation_seconds']=max(0,cycles[-1]['monotonic']-cycles[0]['monotonic'])
            if len(cycles)<2 or cycles[-1]['monotonic']-cycles[0]['monotonic']<600:return save('stable_observation')
            if failure_snapshot(root,pending(root))!=fingerprint or binding(root)!=ctx_binding:return save('evidence_changed')
            if not policy_current(root,policy) or read(root/'state/index-recovery-policy.json')!=policy:return save('evidence_changed')
            if not validation_evidence.receipt_is_current(root,success,index['dirty'],index['applied']):return save('validation_changed')
            recovered=0
            for p,d in known:
                if read(p)!=d:return save('evidence_changed')
                receipt={'schema_version':1,'incident_id':p.stem,'evidence_sha256':evidence.digest(d['evidence']),
                         'recovered_at':stamp,'recovery_kind':'index_state_recovered','root_cause_status':d.get('root_cause_status','pending'),
                         'index_instance':instance,'full_validation':success,'natural_cycles':[cycles[0],cycles[-1]],'code_binding':ctx_binding,
                         'policy_sha256':evidence.digest(policy),'failure_snapshot':fingerprint,'business_operations_replayed':False}
                receipt_path=root/'state/index-recovery-receipts'/(p.stem+'-'+evidence.digest(receipt)+'.json')
                evidence.atomic(receipt_path,receipt)
                d.update(recovery_status='verified',recovery_kind='index_state_recovered',recovered_at=stamp,
                         recovery_evidence={'receipt':str(receipt_path),'sha256':hashlib.sha256(receipt_path.read_bytes()).hexdigest()})
                incidents.atomic_json(p,d);recovered+=1
            remaining=pending(root)
            if not remaining:
                import business_sync_state as state
                state.resolve_alert('system','supervisor_runtime_error',title='后台异常已核验恢复',detail='索引原失败环节及当前完整校验通过，两个自然周期观察不少于十分钟；历史根因保持待查。',database=database)
                view.update(phase='verified',pending_incidents=0,index_pending_incidents=0,next_check_at=None)
                known=[]
            save(None if not remaining else 'operation_receipt_pending')
            return recovered

def retain_expected_failure(service,exc):
    root=service.config_path.parent
    active=root/'state/index-runtime-active-fault.json'
    previous=read(active)
    if previous.get('fault_code')==exc.code and previous.get('status')=='pending':return
    import business_sync_supervisor
    event=business_sync_supervisor.record_runtime_error(exc,root=root,database=service.database)
    incidents.import_history(root)
    evidence.atomic(active,{'status':'pending','fault_code':exc.code,'recorded_at':event['recorded_at']})

def workbench_view(progress):
    reasons={'index_busy':'等待索引访问锁，释放后自动核验。','source_busy':'等待当前同步退出写入区间。',
             'stable_observation':'索引与完整校验已通过，正在完成十分钟自然运行观察。',
             'validation_pending':'等待晚于故障和本次部署的完整校验回执。','validation_changed':'资料或程序已变化，等待匹配当前版本的完整校验。',
             'technical_investigation':'故障暂未识别，保留原始记录继续技术排查。',
             'memory_index_update_failed':'索引仍有更新错误，保留失败证据等待原流程恢复。',
             'memory_index_update_pending':'索引版本尚未一致，等待原流程更新。',
             'memory_index_state_unavailable':'暂时无法读取索引状态，等待技术核验。',
             'memory_index_state_invalid':'索引状态不一致，等待技术核验。',
             'operation_receipt_pending':'索引故障已核验，其他失败环节仍待成功回执。',
             'maintenance_active':'维护期间暂缓恢复核验。','native_cycle_pending':'等待原生后台自然运行核验。',
             'evidence_changed':'证据已变化，保留问题并在下一周期重新核验。'}
    reason='后台异常已核验恢复，历史根因记录保留。' if progress.get('phase')=='verified' else reasons.get(progress.get('check_blocker'),'后台正在核对原失败环节的恢复凭据。')
    if progress.get('technical_investigation_required') and progress.get('check_blocker')!='technical_investigation':reason+=' 尚有未识别的技术故障需继续排查。'
    gaps=progress.get('evidence_gaps',[])
    if gaps:reason+=' '+ '；'.join(gaps)+'。'
    keys=('event_verification_count','actual_repair_count','historical_database_attempts','technical_investigation_required','evidence_gaps')
    return {**{k:progress.get(k) for k in keys},'current_reason':reason,'current_blocker':progress.get('check_blocker'),'decision_required':False}
