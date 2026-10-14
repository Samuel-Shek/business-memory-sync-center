"""Project policy and adapters. All business event writers remain independent."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json, sqlite3, time
from pathlib import Path
from content_identity import Registry, AccessDenied, canonical, digest, key

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_POLICY=ROOT/'policies/cross_source_dedup.v1.json'

def policy(config):
    p=Path(config.get('cross_source_policy') or DEFAULT_POLICY)
    if not p.exists():return {'version':'cross-source-v1','mode':'off'}
    if p.is_symlink():raise AccessDenied('policy_symlink')
    v=json.loads(p.read_text())
    if v.get('mode') not in {'off','observe','exact_reuse'} or v.get('version')!='cross-source-v1':raise AccessDenied('cross_source_policy_invalid')
    return v

def context(p):
    return {'authorized':p.get('owner_authorized') is True,'domain':p.get('domain'),'purpose':p.get('purpose'),'permission_epoch':p.get('permission_epoch')}

def registry(config):
    return Registry(Path(config['machine_ledger_root'])/'跨来源资料关联.sqlite')

def mail_begin(config,row,item):
    p=policy(config)
    if p['mode']=='off':return None
    ctx=context(p);Registry.context(ctx)
    source={'system':'email_attachment','mailbox':row['mailbox'],'message_id':row['message_id'],'attachment_ledger_id':digest(str(item['id']).encode())[:16]}
    try:
        r=registry(config)
        e=r.register(source,str(row['message_id']),ctx,{'name':item.get('name') or 'attachment','message_key':row['message_key'],'origin_ledger':'email-sync/messages+attachments','identity_quality':'existing_message_and_attachment_ledger_ids','provider_attachment_id':str(item['id']),'location':'Windows 原件','coverage':'awaiting_current_read'})
    except AccessDenied:raise
    except (OSError,sqlite3.Error):e=None
    return {'policy':p,'context':ctx,'event':e,'source':source}

def mail_request(handle,extension):
    if not handle:return None
    p=handle['policy']
    return {'version':p['version'],'mode':p['mode'],'eligible':bool(handle['event']) and extension in p.get('exact_formats',[]),
      'context':handle['context'],'source':handle['source'],'event_id':handle['source']['message_id'],
      'policy_sha256':key(p),'exclusions':'win11-direct-except-self-v1','redaction':'none-authorized-view-v1',
      'scope':'full machine text extraction and available OCR; not business verification'}

def mail_finish(config,handle,result):
    if not handle:return
    p=policy(config)
    if key(p)!=key(handle['policy']):raise AccessDenied('policy_changed_during_read')
    if not handle['event']:
        result['content_registry_status']='index_unavailable';return
    r=registry(config);ctx=context(p)
    r.receipt(handle['event'],ctx,'extraction_result',{
      'source_sha256':result['source_sha256'],'source_bytes':result['source_bytes'],
      'view_sha256':result.get('local_view_sha256'),'coverage':result.get('coverage'),
      'extract_status':result['extract_status'],'cache':result.get('content_cache'),
      'original_received_on_m2':False,'remote_path':result['remote_path'],
      'current_job_id':result['job_id'],'identity_sha256':result['identity_sha256'],
      'text_sha256':result.get('text_sha256'),'business_event_skipped':False})
    r.acknowledge(handle['event'],ctx)

def listing(config,limit=50,offset=0,identifier=None):
    p=policy(config)
    if not p.get('owner_authorized'):return {'items':[],'total':0,'mode':p['mode']}
    path=Path(config['machine_ledger_root'])/'跨来源资料关联.sqlite'
    if not path.exists():return {'items':[],'total':0,'mode':p['mode'],'reason':'尚未登记资料'}
    try:
        result=Registry(path,readonly=True).listing(context(p),limit,offset,identifier)
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
            hits=db.execute("SELECT COUNT(*) FROM receipts r JOIN events e ON e.id=r.event_id JOIN access a USING(source_key) WHERE r.action='extraction_result' AND json_extract(r.payload,'$.cache.hit')=1 AND a.state='allowed' AND a.domain=? AND a.purpose=? AND a.epoch=? AND e.epoch=a.epoch AND e.purpose=a.purpose",(p['domain'],p['purpose'],p['permission_epoch'])).fetchone()[0]
        result.update(mode=p['mode'],exact_formats=p.get('exact_formats',[]),reuse_hits=hits,avoided_parses=hits);return result
    except (sqlite3.Error,OSError):return {'items':[],'total':0,'mode':p['mode'],'reason':'关联索引暂不可用，原同步继续'}
