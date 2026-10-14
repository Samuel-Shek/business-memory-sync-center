"""Fixed bridge adapter. The native supervisor owns export, verification and publishing.

Candidates contain a single pure module; no generated controller or test is run.
Business writes and object closure remain exclusively in the source controllers.
"""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

from pathlib import Path
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import sync_repair_contract

POLICY='sync-center-owner-approved-20260929-v1'
ROOT=Path(__file__).resolve().parents[1]
RULE='sync_repair_rules.py'
CONTROL=('sync_center_autonomy.py','sync_repair_contract.py','sync_rule_runtime.py')

def sha(data):return hashlib.sha256(data if isinstance(data,bytes) else data.encode()).hexdigest()
def packed(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def atomic(path,value):
    path=Path(path)
    if path.is_symlink():raise ValueError('unsafe_path')
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f:f.write(packed(value));f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
def safe(path):
    if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('unsafe_path')
    if path.exists() and path.stat().st_uid!=os.getuid():raise ValueError('wrong_owner')
    return path

def binding(root):return {name:sha((root/'bin'/name).read_bytes()) for name in (*CONTROL,RULE)}
def check(text):
    # AST excludes imports, recursion, attribute traversal, mutation and active code.
    return sync_repair_contract.check(text)

def export(root,database):
    directory=safe(root/'state/sync-autonomy');directory.mkdir(mode=0o700,exist_ok=True)
    candidates=safe(directory/'candidates');candidates.mkdir(mode=0o700,exist_ok=True)
    code=(root/'bin'/RULE).read_text();version=binding(root)
    try:health=check(code)
    except (ValueError,SyntaxError,TypeError):health={'passed':False,'error':'rule_contract_invalid'}
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2) as c:
        c.row_factory=sqlite3.Row
        rows=c.execute('SELECT issue_id,fingerprint,source_id,kind,status,attention_state,revision,observation_json FROM operational_issues ORDER BY issue_id LIMIT 5000').fetchall()
    issues=[]
    for row in rows:
        if not re.fullmatch('issue-[a-f0-9]{24}',row['issue_id']):continue
        obs=json.loads(row['observation_json'])
        reason=str(obs.get('reason_code') or obs.get('error_code') or '')
        if not re.fullmatch('[a-z0-9_]{0,100}',reason):reason='diagnostic_redacted'
        issues.append({'id':row['issue_id'],'revision':sha(packed([row['fingerprint'],row['revision']])),
                       'source':row['source_id'] if row['source_id'] in ('dingtalk_mail','dingtalk_minutes','dingtalk_chat','system','memory_core','win11','wechat') else 'system',
                       'kind':row['kind'] if re.fullmatch('[a-z0-9_]{1,80}',row['kind']) else 'unknown',
                       'state':row['attention_state'] if row['status']!='closed' else 'closed','reason':reason})
    request={'schema_version':1,'policy':POLICY,'generated_at':time.time(),'binding':version,'issues':issues,
             'probe':health,'probe_revision':sha(packed([version,health])),
             'candidate_scope':{'file':RULE,'contract':'fixed pure classification and receipt interpretation; no source writes or decisions'}}
    if not health['passed']:request['candidate_context']={'source':code,'contract_cases':sync_repair_contract.cases()}
    atomic(directory/'export.json',request)
    return request

def validate_candidate(root,candidate,request):
    if candidate.get('policy')!=POLICY or candidate.get('probe_revision')!=request['probe_revision']:raise ValueError('source_changed')
    if request['binding']!=binding(root):raise ValueError('code_changed')
    text=candidate.get('content')
    if not isinstance(text,str) or len(text.encode())>12000:raise ValueError('candidate_size')
    # Prove the current original fails and candidate passes the SAME fixed oracle.
    before=check((root/'bin'/RULE).read_text());after=check(text)
    if before['passed'] or not after['passed']:raise ValueError('reproduction_or_regression_failed')
    return {'old':before,'new':after,'candidate_sha256':sha(text),'binding':binding(root)}

@contextlib.contextmanager
def control(root):
    fd=os.open(root/'state/sync-center-control.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:os.close(fd)

def isolated_regressions(root,text,directory):
    stage=directory/'stage';stage.mkdir(mode=0o700)
    for folder in ('bin','prompts','schemas','assets','lib'):
        if (root/folder).exists():shutil.copytree(root/folder,stage/folder,ignore=shutil.ignore_patterns('__pycache__','*.app','*.log'),symlinks=True)
    shutil.copy2(root/'config.json',stage/'config.json')
    (stage/'bin'/RULE).write_text(text)
    profile=directory/'isolation.sb'
    profile.write_text('(version 1) (allow default) (deny network*) (deny file-write* (subpath '+json.dumps(str(Path.home()))+')) (allow file-write* (subpath '+json.dumps(str(directory))+') (subpath "/private/tmp")) (deny file-read* (subpath '+json.dumps(str(Path.home()/'.ssh'))+') (subpath '+json.dumps(str(Path.home()/'.aws'))+') (regex #".*(api_key|credentials|Keychains).*"))')
    logs=[]
    for name in ('selftest_autonomy_repair.py','selftest_email_sync.py','selftest_model_router.py','selftest_operational_workbench.py','selftest_index_runtime_recovery.py','selftest_index_failure_boundary.py','selftest_ledger_selfheal.py','selftest_supervisor_runtime_recovery.py','selftest_validation_retry.py','selftest_automatic_memory_validation.py','selftest_handoff_context.py','selftest_business_sync_supervisor.py','selftest_business_sync_supervisor_health.py'):
        log=directory/(name+'.log')
        with log.open('w') as sink:
            run=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),'/usr/bin/python3','-B',str(stage/'bin'/name)],cwd=stage,stdout=sink,stderr=subprocess.STDOUT,timeout=180)
        logs.append({'path':str(log),'sha256':sha(log.read_bytes()),'passed':run.returncode==0})
        if run.returncode:raise ValueError('fixed_regression_failed')
    return logs

def publish(root,candidate,request,directory):
    with control(root):
        import business_sync_controller,email_sync
        blocker=business_sync_controller.global_safety_error(email_sync.load_config(root/"config.json"))
        if blocker:raise ValueError(blocker)
    proof=validate_candidate(root,candidate,request)
    logs=isolated_regressions(root,candidate['content'],directory)
    with control(root):
        proof=validate_candidate(root,candidate,request)
        # The current core must be fully validated before touching even pure rules.
        import system_dashboard_status
        import email_sync
        config=email_sync.load_config(root/'config.json')
        import business_sync_controller
        blocker=business_sync_controller.global_safety_error(config)
        if blocker:raise ValueError(blocker)
        target=root/'bin'/RULE;backup=directory/'before.py';shutil.copy2(target,backup)
        receipt={'policy':POLICY,'phase':'publishing','at':time.time(),'probe_revision':request['probe_revision'],'proof':proof,'tests':logs,'backup':str(backup),'business_data_restored':False}
        atomic(directory/'receipt.json',receipt)
        tmp=directory/'candidate.py';tmp.write_text(candidate['content']);tmp.chmod(0o600)
        # Same filesystem, one atomic module replacement. Recovery uses code only.
        os.replace(tmp,target)
        if sha(target.read_bytes())!=proof['candidate_sha256']:
            shutil.copy2(backup,target);raise ValueError('publish_readback_failed')
        import index_runtime_recovery
        policy_path=root/'state/index-recovery-policy.json'
        policy=json.loads(policy_path.read_text())
        atomic(directory/'policy-before.json',policy)
        policy.update(code_binding=index_runtime_recovery.binding(root),test_evidence=[{'path':x['path'],'sha256':x['sha256']} for x in logs],deployed_at=__import__('datetime').datetime.now().astimezone().isoformat(timespec='seconds'))
        atomic(policy_path,policy)
        receipt.update(phase='observing',installed_at=time.time(),observe_until=time.time()+86400)
        atomic(directory/'receipt.json',receipt)
        return receipt

def reconcile_interrupted(root):
    directory=root/'state/sync-autonomy'
    for path in directory.glob('repair-*/check-*/receipt.json'):
        receipt=json.loads(path.read_text())
        if receipt.get('phase')!='publishing':continue
        candidate_hash=receipt.get('proof',{}).get('candidate_sha256')
        original=receipt.get('proof',{}).get('binding',{}).get(RULE)
        backup=safe(Path(receipt.get('backup','')))
        if backup.parent!=path.parent or not backup.is_file() or sha(backup.read_bytes())!=original:
            raise ValueError('rollback_evidence_unavailable')
        with control(root):
            current=sha((root/'bin'/RULE).read_bytes())
            if current==candidate_hash:
                tmp=path.parent/'restore.py';shutil.copy2(backup,tmp);os.replace(tmp,root/'bin'/RULE)
                policy=path.parent/'policy-before.json'
                if policy.is_file():atomic(root/'state/index-recovery-policy.json',json.loads(policy.read_text()))
                receipt.update(phase='rolled_back',reason='interrupted_publish',business_data_restored=False)
            elif current==original:receipt.update(phase='not_published',reason='interrupted_before_replace')
            else:receipt.update(phase='investigating',reason='installed_code_changed')
            atomic(path,receipt)

def tick(root,database):
    root=Path(root);database=Path(database)
    registration=root/'state/sync-autonomy/release.json'
    if not registration.is_file() or json.loads(registration.read_text()).get('policy')!=POLICY:return {'status':'not_registered'}
    reconcile_interrupted(root)
    request=export(root,database);directory=root/'state/sync-autonomy'
    # A single request per code/contract revision; retries share the AGI ledger.
    path=directory/'candidates'/(request['probe_revision']+'.json')
    if path.is_file() and not path.is_symlink():
        job=directory/('repair-'+request['probe_revision'])
        prior=json.loads((job/'outcome.json').read_text()) if (job/'outcome.json').is_file() else {}
        if not job.exists() or (prior.get('phase')=='waiting' and time.time()>=prior.get('next_check',0)):
            candidate=json.loads(path.read_text()) if path.stat().st_size<=20000 else {}
            job.mkdir(mode=0o700,exist_ok=True)
            attempt=job/('check-'+str(time.time_ns()));attempt.mkdir(mode=0o700)
            try:result=publish(root,candidate,request,attempt)
            except BlockingIOError:result={'phase':'waiting','reason':'source_busy','attempt_consumed':False,'next_check':time.time()+300}
            except (ValueError,OSError,subprocess.SubprocessError) as e:
                reason=str(e) if isinstance(e,ValueError) else type(e).__name__
                waiting=reason in ('memory_core_busy','memory_core_unverified','maintenance_active')
                result={'phase':'waiting' if waiting else 'investigating','reason':reason,'next_check':time.time()+300,'attempt_consumed':not waiting}
            atomic(job/'outcome.json',result)
    return {'status':'healthy' if request['probe']['passed'] else 'investigating','issues':len(request['issues']),'probe_revision':request['probe_revision']}
