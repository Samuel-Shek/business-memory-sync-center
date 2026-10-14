"""Read existing ledgers only. Never crawl new cloud sources or modify business facts."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import argparse, json, sqlite3, time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import content_registry as bridge
from content_identity import snapshot, key, StaleWork, AccessDenied

AUDIT=Path(_archive_path('/Users/example/AI/LocalData/DingTalk-Document-Audit.nosync/云文档读取核验_260914'))

def rows(path,sql):
    if not Path(path).is_file():return []
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as d:
        d.row_factory=sqlite3.Row;return [dict(x) for x in d.execute(sql)]
def stamp(v):
    try:return datetime.fromisoformat(str(v).replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError):return 0

def observe(config,*,baseline=False):
    p=bridge.policy(config)
    if p['mode']=='off':return {'mode':'off','registered':0}
    r=bridge.registry(config);ctx=bridge.context(p);external=Path(config['external_root']);root=Path(config.get('bridge_root') or bridge.ROOT)
    with r.connect() as db:known={x[0] for x in db.execute('SELECT id FROM events')}
    counts=Counter();candidates=defaultdict(list)
    # Snapshots are REFERENCES, never new copies of originals.
    def add(source,event,metadata,ordinal=None,observed=None,lane='backfill',snap=None):
        eid=key([key([ctx['domain'],source]),str(event)])
        if eid not in known:
            try:r.register(source,event,ctx,metadata,ordinal=ordinal,observed=observed,lane=lane)
            except AccessDenied:return
            known.add(eid);counts[source['system']]+=1
        if snap and snap.get('sha256') and snap.get('bytes') is not None:candidates[(snap['sha256'],snap['bytes'])].append((eid,snap))
    versions={x['event_id']:x for x in rows(root/'state/bridge.sqlite','SELECT * FROM watch_versions')}
    exclusions={s['source_id']:set(s.get('excluded_relative_paths',[])) for s in config.get('watch_sources',[]) if isinstance(s,dict) and 'source_id' in s} if isinstance(config.get('watch_sources'),list) else {}
    # Follow the existing exact path policy, including a future authorized removal.
    for x in rows(root/'state/bridge.sqlite','SELECT * FROM source_events ORDER BY processed_at,transition_sequence'):
        if x['source_relative_path'] in exclusions.get(x['source_id'],set()):continue
        v=versions.get(x['event_id'],{});ref=v.get('destination_ref','');path=None
        if ref.startswith('external:'):path=external/ref[len('external:'):]
        elif ref.startswith('obsidian:'):path=Path(config['memory_root'])/ref[len('obsidian:'):]
        if path and not (path.resolve().is_relative_to(external.resolve()) or path.resolve().is_relative_to(Path(config['memory_root']).resolve())):raise AccessDenied('snapshot_outside_archive')
        snap={'sha256':v.get('sha256'),'bytes':v.get('size_bytes'),'path':str(path)} if path else None
        add({'system':'win11_watch','source_id':x['source_id'],'entry_id':x['source_entry_id']},x['event_id'],
            {'name':Path(x['source_relative_path']).name,'location':x['source_relative_path'],'archive_ref':ref,'snapshot':snap,'operation':x['operation'],'source_version':v.get('version_number'),'coverage':'原接收回执；解析范围未认证','origin_ledger':str(root/'state/bridge.sqlite'),'identity_quality':'registered_entry_id'},ordinal=x['transition_sequence'],observed=stamp(x['processed_at']),lane='live',snap=snap)
    mail_root=Path(config['email_sync']['state_root'])/'mail.sqlite'
    if not mail_root.exists():
        # Resolve the existing writer's configured database, not an invented location.
        import email_sync;mail_root=email_sync.runtime_paths(config)['database']
    for x in rows(mail_root,'SELECT a.*,m.mailbox,m.message_id,m.archived_at FROM attachments a JOIN messages m USING(message_key)'):
        add({'system':'email_attachment','mailbox':x['mailbox'],'message_id':x['message_id'],'attachment_ledger_id':x['attachment_id']},x['message_id'],
            {'name':x['original_name'],'location':x['stored_name'],'origin_ledger':str(mail_root),'message_key':x['message_key'],'sha256':x['sha256'],'bytes':x['actual_size'],'coverage':x['extract_status']+'；历史状态不作为完整缓存','identity_quality':'existing_attachment_ledger_id'},observed=stamp(x['archived_at']))
    chat=external/'dingtalk-chat/state/chat.sqlite'
    for x in rows(chat,'SELECT v.rowid version_row_id,v.message_id,v.observed_at,v.redaction_count,m.conversation_id FROM message_versions v JOIN messages m USING(message_id)'):
        add({'system':'dingtalk_chat','conversation_id':x['conversation_id'],'message_id':x['message_id']},'message_versions:'+str(x['version_row_id']),
            {'name':'聊天消息','origin_ledger':str(chat),'version_row_id':x['version_row_id'],'coverage':'仅登记消息版本与原台账；不复制正文或被脱敏的原文指纹','redaction_count':x['redaction_count']},ordinal=x['version_row_id'],observed=stamp(x['observed_at']))
    minutes=external/'dingtalk-sync-v2/state/sync.sqlite'
    if minutes.is_file():
        with sqlite3.connect('file:'+str(minutes)+'?mode=ro',uri=True) as d:cols={x[1] for x in d.execute('PRAGMA table_info(recordings)')}
        if {'recording_id','source_ref','title','updated_at'}<=cols:
            for x in rows(minutes,'SELECT recording_id,source_ref,title,updated_at FROM recordings'):
                add({'system':'dingtalk_minutes','recording_id':x['recording_id']},x['recording_id']+':'+x['updated_at'],{'name':x['title'],'origin_ledger':str(minutes),'source_ref':x['source_ref'],'coverage':'原听记处理事件；不按相同文字合并会议'},observed=stamp(x['updated_at']))
    if baseline:
        inventory=Path(config['source_governance']['inventory']);inv=json.loads(inventory.read_text())
        for x in inv['records']:
            path=Path(x['path'])
            # Entries come from the approved inventory; no new directory crawl.
            snap={'path':str(path),'sha256':x['sha256'],'bytes':x['size']}
            add({'system':'registered_local','ledger':str(inventory),'locator':str(path)},str(inv['generated_at'])+':'+str(x['modified_at']),
                {'name':path.name,'location':str(path),'snapshot':snap,'coverage':'登记指纹；旧解析未提供完整覆盖依据','identity_quality':'locator_only_no_rename_inference','origin_ledger':str(inventory)},observed=stamp(inv['generated_at']),snap=snap)
        audit=json.loads((AUDIT/'audit-result.json').read_text());extractions={x['nodeId']:x for x in json.loads((AUDIT/'extraction-results.json').read_text())}
        for x in audit['rows']:
            z=extractions.get(x['nodeId'],{});path=AUDIT/'files'/(x['nodeId']+'.'+x['extension']);snap=None
            if z.get('sha256') and path.is_file():snap={'path':str(path),'sha256':z['sha256'],'bytes':z['bytes']}
            add({'system':'dingtalk_audit_archive','node_id':x['nodeId'],'archive_scope':str(AUDIT)},str(audit['checked_at']),
                {'name':x['name'],'location':x['url'],'snapshot':snap,'coverage':x['status'],'reason':x['notes'],'category':x['category'],'origin_ledger':str(AUDIT/'audit-result.json'),'permission_scope':'合法留存测试档案；不声明当前云端权限','parsing_receipt':str(AUDIT/'extraction-results.json') if z else None},observed=stamp(audit['checked_at']),snap=snap)
    # Include earlier registered references so new events can relate to the baseline.
    with r.connect() as db:
        for e in db.execute('SELECT id,metadata FROM events WHERE domain=?',(ctx['domain'],)):
            m=json.loads(e['metadata']);snap=m.get('snapshot')
            if snap and snap.get('sha256') and snap.get('bytes') is not None:
                group=candidates[(snap['sha256'],snap['bytes'])]
                if all(a!=e['id'] for a,b in group):group.append((e['id'],snap))
    with r.connect() as db:
        confirmed={(x[0],x[1]) for x in db.execute("SELECT left_id,right_id FROM relations WHERE kind='bytes_equal' AND id NOT IN (SELECT relation_id FROM corrections)")}
    pairs=0
    for group in candidates.values():
        # Star edges preserve all origins without quadratic duplicate comparisons.
        if len(group)<2:continue
        a=group[0]
        for b in group[1:]:
            if (a[0],b[0]) in confirmed:continue
            try:r.compare(a[0],b[0],ctx,a[1],b[1]);pairs+=1
            except AccessDenied:continue
    return {'mode':p['mode'],'registered':sum(counts.values()),'by_source':dict(counts),'candidate_pairs_checked':pairs,'business_events_skipped':0,'new_cloud_requests':0}

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,default=bridge.ROOT/'config.json');p.add_argument('--baseline',action='store_true');a=p.parse_args()
    print(json.dumps(observe(json.loads(a.config.read_text()),baseline=a.baseline),ensure_ascii=False))
if __name__=='__main__':main()
