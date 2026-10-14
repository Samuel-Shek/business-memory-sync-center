"""Existing DingTalk cycle extension. No timer, credentials, or email routing."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import copy
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from handoff_core import Ledger, Rejected,canonical,digest,validated_text,validated_summary
from attachment_queue import Queue,message_rows
from dingtalk_source import DingTalkSource
from local_read_probe import probe
import memory_adapter

PYTHON='C:/Users/example/AppData/Local/BusinessMemory/mail-attachments/runtime/Scripts/python.exe'
WORKER='C:/Users/example/AppData/Local/BusinessMemory/dingtalk-attachments/worker-v1/win11_worker.py'
JOBS='C:/Users/example/AppData/Local/BusinessMemory/dingtalk-attachments/jobs-v1'
FILES=('parse-result.json','full-text.txt','summary-result.json','summary-receipt.json')


def atomic(path,data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.pending-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if Path(tmp).exists(): Path(tmp).unlink()


def settings(config):
    value=config.get('dingtalk_attachment_handoff')
    if not isinstance(value,dict) or value.get('enabled') is not True: return None
    if (value.get('schema_version')!=1 or value.get('host')!='win-main' or value.get('windows_hostname')!='Example User'
        or value.get('model') not in ('gpt-6-astra','mimo-v2.5-pro','deepseek-flash') or value.get('new_scheduler') is not False
        or not re.fullmatch('[a-f0-9]{64}',value.get('reader_sha256',''))): raise Rejected('handoff_config_invalid')
    return value


def gate(writer,operator=False):
    root=writer.ROOT
    path=root/'state'/'maintenance-mode.json'
    state=json.loads(path.read_text('utf-8')) if path.is_file() else {}
    controlled=(operator and state.get('status')=='active' and state.get('reason')=='dingtalk-attachment-final-acceptance')
    if state.get('status')!='inactive' and not controlled:
        raise Rejected('maintenance_active')
    if any(any(p.is_file() for p in (root/name).iterdir()) for name in ('inbox','watch_inbox')):
        raise Rejected('memory_queue_busy')


def remote(request,action='run'):
    key=digest(canonical(request).encode())
    data={**request,'request_key':key,'action':action}
    try:
        p=subprocess.run(['/usr/bin/ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','ServerAliveInterval=15',
            '-o','ServerAliveCountMax=2','win-main',PYTHON+' '+WORKER],input=canonical(data).encode(),capture_output=True,timeout=1050 if action=='run' else 40)
    except subprocess.TimeoutExpired: raise Rejected('remote_result_uncertain')
    except OSError: raise Rejected('win11_offline')
    if p.returncode: raise Rejected('remote_result_uncertain')
    if len(p.stdout)>1024*1024: raise Rejected('receipt_invalid')
    result=json.loads(p.stdout)
    if not isinstance(result,dict): raise Rejected('receipt_invalid')
    return key,result


def fetch(root,remote_key,result):
    manifest=result.get('files')
    if not isinstance(manifest,dict) or set(manifest)!=set(FILES) or any(not re.fullmatch('[a-f0-9]{64}',str(v)) for v in manifest.values()):
        raise Rejected('transport_manifest_invalid')
    target=root/'results'/remote_key
    if target.exists():
        if any(digest((target/f).read_bytes())!=manifest[f] for f in FILES): raise Rejected('hash_mismatch')
        return target
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=Path(tempfile.mkdtemp(prefix='.receive-',dir=target.parent))
    # Keep failed transfers private for diagnostics; never publish partial data.
    for name in FILES:
        p=subprocess.run(['/usr/bin/scp','-q','-o','BatchMode=yes','-o','ConnectTimeout=8',
            'win-main:'+JOBS+'/'+remote_key[:32]+'/'+name,str(temporary/name)],capture_output=True,timeout=60)
        if p.returncode: raise Rejected('network_failed')
        os.chmod(temporary/name,0o600)
        if digest((temporary/name).read_bytes())!=manifest[name]: raise Rejected('hash_mismatch')
    atomic(temporary/'transport-manifest.json',canonical(manifest).encode())
    temporary.rename(target)
    return target


def receive(root,folder,pin,source,reason,expected_request=None):
    parsed=json.loads((folder/'parse-result.json').read_text('utf-8'))
    summary=json.loads((folder/'summary-result.json').read_text('utf-8'))
    audit=json.loads((folder/'summary-receipt.json').read_text('utf-8'))
    text=validated_text(parsed,parsed['source'],pin,'Example User')
    if (folder/'full-text.txt').read_bytes()!=text.encode() or parsed['source']['version']!=parsed['source_sha256']:
        raise Rejected('hash_mismatch')
    if audit.get('schema_version')==2:
        from routing_receipt import validate_route
        policy=json.loads((Path(__file__).resolve().parents[1]/'win11-model-routing'/'routing.json').read_text('utf-8'))
        try: actual_model=validate_route(audit,summary,text,policy)
        except (ValueError,KeyError,TypeError) as exc: raise Rejected('summary_audit_mismatch') from exc
        if audit.get('task_key')!=parsed['task_key']:raise Rejected('summary_audit_mismatch')
        validated_summary(summary,text,actual_model)
    else:
        validated_summary(summary,text,'gpt-6-astra')
        if not (audit.get('task_key')==parsed['task_key'] and audit.get('text_sha256')==parsed['text_sha256']
            and audit.get('summary_sha256')==digest(canonical(summary).encode()) and audit.get('model')=='gpt-6-astra'
            and audit.get('tool_calls')==0 and audit.get('subscription_only') is True
            and audit.get('completion_evidence')=='validated_turn_completed_and_structured_result'):
            raise Rejected('summary_audit_mismatch')
        # The one adopted, pre-existing canary is pinned separately; new jobs MUST
        # carry real process-returncode evidence.
        if audit.get('exit_code')!=0:
            approved=json.loads((root/'accepted-canary.json').read_text('utf-8')) if (root/'accepted-canary.json').exists() else {}
            if approved.get('task_key')!=parsed['task_key'] or approved.get('summary_receipt_sha256')!=digest((folder/'summary-receipt.json').read_bytes()):
                raise Rejected('model_exit_receipt_missing')
        actual_model='gpt-6-astra'
    binding=parsed['source']
    if expected_request is not None:
        expected=dict(organization=expected_request['organization'],profile=expected_request['profile'],
            message=expected_request['message'],conversation=expected_request['conversation'],
            resource_type=expected_request['resource']['type'],resource=expected_request['resource']['resourceId'])
        if any(binding.get(k)!=v for k,v in expected.items()): raise Rejected('identity_mismatch')
    if binding['organization']!=source.organization or binding['profile']!=source.profile: raise Rejected('identity_mismatch')
    raw=source.hydrate(binding['message'],binding['conversation'])
    if not any(r['type']==binding['resource_type'] and r['resourceId']==binding['resource'] for r in raw['resourceRefs']):
        raise Rejected('resource_changed')
    ledger=Ledger(root/'source-index')
    try:
        key,created=ledger.discover(binding,reason)
        row=ledger.get(key)
        if row['stage'] in ('m2_received','memory_applied'):
            if row['receipt']!=canonical(parsed) or row['summary']!=canonical(summary): raise Rejected('existing_result_changed')
            return key
        generation=ledger.claim(key)
        if generation is None: raise Rejected('worker_busy_or_uncertain')
        for before,after in [('discovered','queued'),('queued','win11_received')]:
            if ledger.get(key)['stage']==before: ledger.advance(key,generation,after)
        if ledger.get(key)['stage']=='win11_received': ledger.parsed(key,generation,parsed,pin,'Example User')
        if ledger.get(key)['stage']=='parsed': ledger.summarized(key,generation,summary,actual_model)
        if ledger.get(key)['stage']=='summarized': ledger.receive(key,generation,binding)
        ledger.await_writer(key,generation)
        return key
    finally: ledger.close()


def component_config(config):
    # Measured full archive validation exceeds the chat default. Keep every
    # check, use its existing supported upper timeout ONLY in this component.
    result=copy.deepcopy(config)
    result['dingtalk_automation']['memory_validation_timeout_seconds']=300
    result['dingtalk_automation']['local_verification_attempts']=1
    return result


def once(config,writer,*,initialize=False,adopt=None):
    config=component_config(config)
    cfg=settings(config)
    if cfg is None: return {'status':'disabled'}
    gate(writer,operator=bool(adopt))
    root=Path(config['external_root'])/'dingtalk-attachments'
    if not initialize and not adopt and not (root/'activation.json').is_file():
        return {'status':'deployed_not_enabled','writes':0,'model_calls':0}
    if not initialize and not adopt:
        active=json.loads((root/'activation.json').read_text('utf-8'))
        if active.get('release_manifest_sha256')!=cfg['release_manifest_sha256']:
            raise Rejected('activation_release_mismatch')
    lock=writer.acquire_lock(root/'cycle.lock')
    queue=None
    try:
        source=DingTalkSource(_archive_path('/Users/example/.local/bin/dws'),cfg['organization'],cfg['profile'])
        queue=Queue(root)
        rows=message_rows(writer.runtime_paths(config)['database'])
        if initialize:
            source.verify_profile()
            return {'status':'baseline_initialized','created':queue.initialize(rows)}
        added=queue.discover(rows,source.organization,source.profile)
        done=0; writes=0; model_calls=0
        pending=queue.pending()
        if not pending and not adopt: return {'status':'noop','discovered':added,'writes':0,'counts':queue.counts()}
        source.verify_profile()
        gate(writer,operator=bool(adopt))
        if not adopt:
            check=writer.acquire_lock(writer.MEMORY_LOCK)
            try: writer.run_refresh_and_validate(config,lock_held=True,refresh_indexes=False)
            finally: check.close()
        if adopt:
            # Exact existing receipt allowed only by an installed approval pin.
            accepted=json.loads((root/'accepted-canary.json').read_text('utf-8'))
            folder=Path(adopt)
            if str(folder.resolve())!=accepted['folder']: raise Rejected('canary_not_approved')
            for f,h in accepted['files'].items():
                if digest((folder/f).read_bytes())!=h: raise Rejected('hash_mismatch')
            key=receive(root,folder,accepted['reader_sha256'],source,'protected')
            ledger=Ledger(root/'source-index')
            try: return {'status':'adopted',**memory_adapter.apply(config,ledger,key,writer)}
            finally: ledger.close()
        for row in pending:
            key=row['key']; request=json.loads(row['request'])
            queue.attempted(key)
            try:
                gate(writer)
                dispatch=root/'dispatch'/key/'request.json'
                if row['stage']=='m2_received':
                    ledger=Ledger(root/'source-index')
                    try: result=memory_adapter.apply(config,ledger,row['task_key'],writer)
                    finally: ledger.close()
                    writes+=result['writes']; done+=1
                    queue.update(key,stage='memory_applied',status='complete'); continue
                if not dispatch.exists():
                    raw=source.hydrate(request['message'],request['conversation'])
                    ref=request['resource']
                    matches=[r for r in raw['resourceRefs'] if r.get('type')==ref.get('type') and r.get('resourceId')==ref.get('resourceId')]
                    if len(matches)!=1 or writer.chat_sync.sanitize_resource(matches[0])!=ref: raise Rejected('resource_changed')
                    request['resource']=matches[0]
                    evidence=probe(source,request,root/'probes')
                    if evidence['reason']=='readable_local':
                        queue.update(key,stage='local_readable',status='complete'); done+=1; continue
                    if evidence['reason']=='risk_file': raise Rejected('risk_file')
                    request.update(extension=evidence['extension'],reason=evidence['reason'])
                    atomic(dispatch.parent/'local-read-evidence.json',canonical(evidence).encode())
                    atomic(dispatch,canonical(request).encode())
                request=json.loads(dispatch.read_text('utf-8'))
                # Persist intent BEFORE dispatch. Lost SSH response is reconciled
                # with the same job; no blind replay or second model invocation.
                action='status' if row['status']=='remote_uncertain' else 'run'
                queue.update(key,stage='queued',status='remote_uncertain',delay=60)
                ai_lock=writer.acquire_lock(writer.SHARED_AI_LOCK) if action=='run' else None
                try: remote_key,result=remote(request,action)
                finally:
                    if ai_lock is not None: ai_lock.close()
                model_calls+=int(result.get('model_calls') or 0)
                if result.get('status') in ('running','uncertain'):
                    queue.update(key,stage='win11_received',status='remote_uncertain',delay=300); continue
                if result.get('status')=='not_received':
                    queue.update(key,status='retry',delay=60); continue
                if result.get('status')!='summarized': raise Rejected(result.get('code','remote_failed'))
                folder=fetch(root,remote_key,result)
                bound=receive(root,folder,cfg['reader_sha256'],source,request['reason'],request)
                queue.update(key,stage='m2_received',status='pending',task_key=bound)
                ledger=Ledger(root/'source-index')
                try: result=memory_adapter.apply(config,ledger,bound,writer)
                finally: ledger.close()
                writes+=result['writes']; done+=1
                queue.update(key,stage='memory_applied',status='complete')
            except Exception as exc:
                code=getattr(exc,'code',str(exc))
                if not re.fullmatch('[a-zA-Z0-9_]{1,100}',code): code=type(exc).__name__
                if code=='remote_result_uncertain': status='remote_uncertain'
                elif code in ('network_failed','source_unavailable','win11_offline','already_running','memory_queue_busy','maintenance_active','model_unavailable'): status='retry'
                else: status='review'
                if status=='retry' and code=='source_unavailable' and row['attempts']>=2: status='review'
                queue.update(key,status=status,error=code,delay=min(86400,60*2**min(row['attempts']+1,10)))
                # Reuse the existing deduplicated local alert mechanism. No
                # unsolicited message or new notification channel.
                if status=='review': writer.raise_alert(config,'failure','warning','attachment-'+key,
                    '钉钉附件需要人工介入',code)
        result={'status':'processed','discovered':added,'completed':done,'writes':writes,'model_calls':model_calls,'counts':queue.counts()}
        atomic(root/'last-cycle.json',canonical(result).encode())
        return result
    finally:
        if queue is not None: queue.close()
        lock.close()


def enable(config,writer):
    config=component_config(config)
    cfg=settings(config)
    if cfg is None: raise Rejected('handoff_disabled')
    gate(writer,operator=True)
    root=Path(config['external_root'])/'dingtalk-attachments'
    lock=writer.acquire_lock(root/'cycle.lock')
    try:
        accepted=json.loads((root/'accepted-canary.json').read_text('utf-8'))
        ledger=Ledger(root/'source-index')
        try:
            row=ledger.get(accepted['task_key'])
            if row['stage']!='memory_applied' or row['status']!='complete': raise Rejected('real_acceptance_missing')
            if ledger.db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise Rejected('index_invalid')
        finally: ledger.close()
        check=writer.acquire_lock(writer.MEMORY_LOCK)
        try:
            writer.run_refresh_and_validate(config,lock_held=True,refresh_indexes=False)
            atomic(root/'activation.json',canonical(dict(enabled=True,release_manifest_sha256=cfg['release_manifest_sha256'],
                real_task=accepted['task_key'],natural_event_acceptance='pending',at=writer.now_iso())).encode())
        finally: check.close()
        return {'enabled':True,'scheduler':'existing_dingtalk_cycle','natural_event_acceptance':'pending'}
    finally: lock.close()
