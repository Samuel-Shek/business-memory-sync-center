"""Deterministic collection once per run, then model review of eligible queue items."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import win11_model_router as router

CATEGORIES = ('verification_code','mailbox_security','advertising_marketing','automated_noise','not_business_related')

def validate(text, schema):
    obj = router.parse_json(text)
    if not isinstance(obj, dict): raise router.OutputInvalid('object_required')
    if set(obj) != set(schema['required']): raise router.OutputInvalid('fields_invalid')
    for name, rule in schema['properties'].items():
        v = obj[name]
        if rule['type'] == 'integer' and (type(v) is not int or v < 0): raise router.OutputInvalid('count_invalid', name)
        if rule['type'] == 'string' and not isinstance(v, str): raise router.OutputInvalid('type_invalid', name)
        if 'enum' in rule and v not in rule['enum']: raise router.OutputInvalid('enum_invalid', name)
    if obj['status'] == 'success' and obj['error_summary']: raise router.OutputInvalid('error_in_success','error_summary')
    if obj['report_path']:
        base = Path(os.environ['LOCALAPPDATA'])/'BUSINESS-Mail-Mirror/mirror/reports'
        path = Path(obj['report_path'])
        if path.parent.resolve() != base.resolve() or not re.fullmatch(r'sync_\d{8}-\d{6}\.json',path.name):
            raise router.OutputInvalid('report_path_invalid','report_path')
    return obj

def mail_command(workdir, command):
    env = os.environ.copy()
    # The coordinator is the only collection owner; only the model child gets review-only.
    env.pop('BUSINESS_MAIL_AGENT_PHASE', None)
    r = subprocess.run(['powershell','-NoProfile','-NonInteractive','-File',
        str(Path(workdir)/'work/run_mail_mirror.ps1'), command],cwd=workdir,
        capture_output=True,timeout=1200,env=env)
    if r.returncode: raise router.Uncertain('mail_'+command.replace('-','_')+'_failed')
    try: value = json.loads(r.stdout.decode('utf-8-sig'))
    except (ValueError, UnicodeError): raise router.Uncertain('mail_command_output_invalid') from None
    if not isinstance(value, dict): raise router.Uncertain('mail_command_output_invalid')
    return value

def verify_collection(value, run_id):
    base = Path(os.environ['LOCALAPPDATA'])/'BUSINESS-Mail-Mirror/mirror/reports'
    path = Path(value.get('report_path',''))
    if path.parent.resolve() != base.resolve() or not re.fullmatch(r'sync_\d{8}-\d{6}\.json',path.name):
        raise router.Uncertain('collection_report_path_invalid')
    raw = path.read_bytes()
    if value.get('report_sha256') and value['report_sha256'] != router.digest(raw):
        raise router.Uncertain('collection_report_changed')
    report = json.loads(raw.decode('utf-8-sig'))
    summary = report.get('summary') or {}
    if (report.get('run_id') != run_id or report.get('result') != 'completed'
        or summary.get('target_seen_flag_changes') != 0 or summary.get('failed_messages') != 0
        or any(report.get(k) is not False for k in ('forbidden_commands_issued','smtp_used','credentials_or_tokens_written'))
        or (report.get('cloud_attachment_download') or {}).get('urls_or_credentials_recorded') is not False):
        raise router.Uncertain('collection_safety_or_completion_failed')
    return {'result':'completed','report_path':str(path),'report_sha256':router.digest(raw),
            'run_id':run_id,'new_count':summary.get('new_unique_messages',0)}

def prepare(job, workdir, run_id, command=mail_command, verify=verify_collection):
    path = job/'preflight.json'
    binding = router.digest(router.canonical({'run_id':run_id,'workdir':str(Path(workdir).resolve()),
        'files':{n:router.digest((Path(workdir)/'work'/n).read_bytes()) for n in
            ('mail_mirror.py','mail_retry_policy.py','mail_attachment_refs.py','mail_cloud_attachments.py')}}).encode())
    state = json.loads(path.read_text('utf-8')) if path.exists() else {'schema_version':1,'binding':binding,'steps':{}}
    if state.get('binding') != binding: raise router.Uncertain('preflight_inputs_changed')
    for name in ('sync','retry-attachment-extraction','prefilter-nonbusiness'):
        old = state['steps'].get(name)
        if old:
            if old.get('status') != 'completed': raise router.Uncertain('preflight_step_outcome_uncertain')
            if name == 'sync': verify(old['result'],run_id)
            continue
        state['steps'][name] = {'status':'started'};router.atomic(path,state)
        value = command(workdir,name)
        if name == 'sync': value = verify(value,run_id)
        # These are numeric extractor/prefilter receipts, not mail bodies or model outputs.
        state['steps'][name] = {'status':'completed','result':value};router.atomic(path,state)
    return state

def assemble(preflight, queue, receipts, model_result=None, failure=None, model_invoked=False):
    collection = preflight['steps']['sync']['result']
    filtered = preflight['steps']['prefilter-nonbusiness']['result']
    deferred = queue['blocked_count'] + queue['waiting_count']
    status = 'success'; error = ''
    if failure:
        status,error='error',failure
    elif queue['hard_error_count']:
        status,error='error','附件存在需要技术核验的失败，已停止重复尝试并保留原始证据。'
    elif model_result and model_result.get('status') == 'error':
        status,error='error','模型报告本轮仍有处理故障；已完成数量以本机逐项回执为准。'
    elif queue['pending_count']:
        status='attention'
    result={'status':status,'sync_result':'completed','prefilter_checked':filtered.get('checked',0),
        'prefilter_ignored':receipts['prefilter_ignored'],'business_processed':receipts['business_processed'],
        'pending_remaining':queue['pending_count'],'report_path':collection['report_path'],'error_summary':error,
        'blocked_remaining':queue['blocked_count'],'waiting_remaining':queue['waiting_count'],
        'actionable_remaining':queue['actionable_count'],'model_invoked':model_invoked,
        'deferred_reasons':sorted({r for item in queue['deferred_items'] for r in item['reasons']}),
        'collection_verified':True,'result_authority':'local_receipts_v2'}
    result.update({'ignored_'+k:receipts['categories'].get(k,0) for k in CATEGORIES})
    return result

def execute(args, command=mail_command):
    if not re.fullmatch('[a-fA-F0-9-]{36}',args.run_id): raise ValueError('run_id_invalid')
    if os.environ.get('BUSINESS_MAIL_SYNC_RUN_ID') != args.run_id: raise ValueError('run_id_context_mismatch')
    schema=json.loads(Path(args.schema).read_text('utf-8-sig'))
    instruction=Path(args.prompt).read_text('utf-8-sig')
    policy=router.load_policy();job=router.HOME/'runs'/'mail'/args.run_id;job.mkdir(parents=True,exist_ok=True)
    command(args.workdir,'recover-commits')
    preflight=prepare(job,args.workdir,args.run_id,command)
    queue=command(args.workdir,'queue-status')
    session=job/'review-session.json'
    work=Path(args.workdir)/'work'
    # Host creates immutable item allowlist before the model sees any tool.
    r=subprocess.run([sys.executable,'-X','utf8',str(work/'mail_review_host.py'),'--session',str(session),'--prepare'],capture_output=True,timeout=30)
    if r.returncode: raise router.Uncertain('review_host_prepare_failed')
    assigned=json.loads(r.stdout.decode('utf-8-sig'))
    mcp=job/'mcp.json'
    router.atomic(mcp,{'mcpServers':{'mail':{'command':sys.executable,'args':['-X','utf8',str(work/'mail_review_host.py'),'--session',str(session)]}}})
    value=None; failure=None; receipt={}; invoked=assigned['items'] > 0
    if invoked:
        instruction+='\n本轮报告路径：'+preflight['steps']['sync']['result']['report_path']+'\n最终输出 JSON Schema：'+router.canonical(schema)
        def checkpoint():
            current=command(args.workdir,'queue-status')
            return {'queue_status_sha256':router.digest(router.canonical(current).encode()),'verified_at':__import__('time').time()}
        previous=os.environ.get('BUSINESS_MAIL_AGENT_PHASE');os.environ['BUSINESS_MAIL_AGENT_PHASE']='review-only'
        try:
            _,value,receipt=router.complete(instruction,purpose='win11_mail_memory',job=job,
                validate=lambda x:{'status':'success'},agent=True,cwd=args.workdir,timeout=2100,checkpoint=checkpoint,policy=policy,mcp_config=mcp)
        except router.Exhausted as exc:
            receipt=exc.receipt
            codes=[]
            for attempt in receipt.get('attempts',[]):
                code=attempt.get('category','unknown');detail=(attempt.get('diagnostic') or {}).get('reason')
                if detail: code+=' / '+detail
                codes.append(code)
            if receipt.get('outcome')=='client_security_failed':
                failure='客户端安全连接失败：'+('证书校验未通过' if any(a.get('category')=='tls_certificate' for a in receipt.get('attempts',[])) else '专用客户端完整性检查未通过')+'；本轮已停止重复尝试，待处理内容已保留。'
            else:
                failure='模型处理未完成：'+' → '.join(codes)
        finally:
            if previous is None: os.environ.pop('BUSINESS_MAIL_AGENT_PHASE',None)
            else: os.environ['BUSINESS_MAIL_AGENT_PHASE']=previous
    if invoked and not failure:
        reviewed=json.loads(session.read_text('utf-8'))
        if not reviewed.get('completed') and not reviewed.get('saved_parts'):
            failure='邮件审阅未保存有效进度；已保留待处理项。'
        if reviewed.get('fatal_errors'):
            failure='邮件审阅校验失败；未完成内容已保留。'
    queue=command(args.workdir,'queue-status');counts=command(args.workdir,'run-receipts')
    result=assemble(preflight,queue,counts,value,failure,invoked)
    router.atomic(args.result,result)
    router.atomic(job/'completion.json',{'schema_version':1,'run_id':args.run_id,'outcome':result['status'],
        'model_invoked':invoked,'pending_remaining':result['pending_remaining'],
        'blocked_remaining':result['blocked_remaining'],'business_processed':result['business_processed'],
        'result_sha256':router.digest(Path(args.result).read_bytes())})
    print(router.canonical({'status':result['status'],'provider':receipt.get('provider','local_controller'),
        'model':receipt.get('model','none'),'routing_version':policy['version'],
        'attempts':len(receipt.get('attempts',[])),'model_invoked':invoked}))
    return 2 if result['status']=='error' else 0

def main():
    p=argparse.ArgumentParser()
    for name in ('prompt','schema','result','workdir','run-id'): p.add_argument('--'+name,required=True)
    return execute(p.parse_args())
if __name__=='__main__':
    try: sys.exit(main())
    except Exception as exc:
        code=str(exc) if re.fullmatch('[a-z_]{1,100}',str(exc)) else type(exc).__name__
        print(router.canonical({'status':'failed','code':code}));sys.exit(3)
