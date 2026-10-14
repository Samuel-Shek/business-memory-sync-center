"""Shared receiver checks for Windows route v2; no effects or provider calls."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib,json

def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(x):return hashlib.sha256(x).hexdigest()
def policy_for_receipt(route,policy):
    if policy.get('sha256') != digest(canonical(policy['payload']).encode()):raise ValueError('routing_policy_hash_mismatch')
    sha=route.get('policy_sha256')
    selected=policy if sha==policy['sha256'] else policy['payload'].get('receipt_policies',{}).get(sha)
    if not selected or selected.get('sha256')!=sha or digest(canonical(selected['payload']).encode())!=sha or route.get('routing_version')!=selected.get('version'):
        raise ValueError('summary_route_policy_invalid')
    return selected

def validate_route(audit,summary,text,policy):
    policy=policy_for_receipt(audit.get('route_receipt',{}),policy)
    route=audit.get('route_receipt',{})
    if audit.get('schema_version')!=2 or audit.get('text_sha256')!=digest(text.encode()) or audit.get('summary_sha256')!=digest(canonical(summary).encode()) or audit.get('tool_calls')!=0:raise ValueError('summary_route_binding_invalid')
    if route.get('schema_version')!=2 or route.get('policy_sha256')!=policy['sha256'] or route.get('routing_version')!=policy['version'] or route.get('outcome')!='ok':raise ValueError('summary_route_policy_invalid')
    model=summary['model'];provider=route.get('provider')
    if audit.get('model')!=model or route.get('model')!=model or provider+':'+model not in policy['payload']['routing']['text_chain']:raise ValueError('summary_route_model_invalid')
    attempts=route.get('attempts',[])
    chain=policy['payload']['routing']['text_chain'];expected=chain+[chain[0]]
    if not attempts or len(attempts)>len(expected):raise ValueError('summary_route_attempts_invalid')
    for index,a in enumerate(attempts):
        if a.get('provider','')+':'+a.get('model','')!=expected[index] or a.get('category')=='started' or a.get('cycle')!=(1 if index<len(chain) else 2):raise ValueError('summary_route_attempts_invalid')
        if index<len(attempts)-1 and a.get('category')=='ok':raise ValueError('summary_route_attempts_invalid')
    last=attempts[-1];evidence=last.get('evidence',{})
    if last.get('category')!='ok' or last.get('model')!=model or evidence.get('tool_calls')!=0:raise ValueError('summary_route_evidence_invalid')
    actual=evidence.get('actual_model','')
    if actual!=model and not actual.startswith(model+'-'):raise ValueError('summary_route_model_invalid')
    if provider in ('deepseek','mimo','kimi') and evidence.get('transport')!='http':raise ValueError('summary_route_transport_invalid')
    if provider in ('claude','gpt') and evidence.get('exit_code')!=0:raise ValueError('summary_route_exit_missing')
    return model
