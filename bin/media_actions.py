"""Explicit, version-bound media decisions. Ordinary video guards remain closed."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json, os, sqlite3, subprocess, uuid, hashlib
from pathlib import Path
import video_hold
ROOT=video_hold.ROOT
ACTIONS={'process_media','ignore_media','restore_media'}
LABELS={'pending':'等待选择','queued':'已排队','running':'处理中','failed':'处理未完成','completed':'已处理','ignored':'已忽略'}

def connect():
    c=video_hold._connect()
    c.executescript('''CREATE TABLE IF NOT EXISTS media_decisions(
      hold_id TEXT PRIMARY KEY,version TEXT NOT NULL,issue_id TEXT NOT NULL,state TEXT NOT NULL,
      revision INTEGER NOT NULL DEFAULT 1,action_id TEXT,stage TEXT NOT NULL DEFAULT '',
      result_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS media_operations(
      action_id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,hold_id TEXT NOT NULL,
      action TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL);
    ''')
    if "source_binding" not in {r[1] for r in c.execute("PRAGMA table_info(media_decisions)")}:
        c.execute("ALTER TABLE media_decisions ADD COLUMN source_binding TEXT NOT NULL DEFAULT ''")
    if "decision_mode" not in {r[1] for r in c.execute("PRAGMA table_info(media_decisions)")}:
        c.execute("ALTER TABLE media_decisions ADD COLUMN decision_mode TEXT NOT NULL DEFAULT 'user'")
    if "automatic_retries" not in {r[1] for r in c.execute("PRAGMA table_info(media_decisions)")}:
        c.execute("ALTER TABLE media_decisions ADD COLUMN automatic_retries INTEGER NOT NULL DEFAULT 0")
    return c

def decisions():
    if not video_hold.DATABASE.exists(): return {}
    with sqlite3.connect(video_hold.DATABASE.as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='media_decisions'").fetchone(): return {}
        return {r['hold_id']:dict(r) for r in c.execute('SELECT * FROM media_decisions')}

def revision(card):
    return video_hold.digest([card['hold_id'],card['version'],card['state'],card['decision_revision'],card['source_binding'],card.get('needs_confirmation')])

def cards_for(row, held, config=None, classifications=None, saved=None, descriptions=None):
    # classifications/saved let a caller building many rows share one snapshot.
    # Each omitted argument falls back to its own read, so existing callers keep
    # the original behaviour; passing them avoids reopening and rescanning both
    # tables once per row, which dominated workbench listing time.
    import media_sources,media_classification
    if classifications is None: classifications=media_classification.records()
    out=[]
    if saved is None: saved=decisions()
    if not held and row['kind']=='video_hold':
        ref=json.loads(row['source_ref_json'])
        held=[{'hold_id':ref.get('hold_id'),'source_id':row['source_id'],'version':row['fingerprint'],
               'name':'','kind':'unknown_media','first_seen':row['first_seen'],'missing':True}]
    for h in held:
        kind=media_classification.effective(h,classifications)
        h={**h,'kind':kind}
        meta=descriptions.get(h["hold_id"]) if descriptions is not None else None
        if meta is None:meta=media_sources.describe(h,config,classifications=classifications)
        d=saved.get(h['hold_id'],{})
        if d and d['version']!=h['version']: d={}
        state=d.get('state','pending')
        card={**video_hold.public_item(h),**meta,'issue_id':row['issue_id'],
              'state':state,'state_label':LABELS[state],'decision_revision':d.get('revision',0),
              'stage':d.get('stage',''),'result':json.loads(d.get('result_json','{}'))}
        card['needs_confirmation']=kind=='video' and not h.get('missing')
        if card['needs_confirmation'] and state in {'pending','failed'}:card['state_label']='视频待命'
        card['decision_mode']=d.get('decision_mode','user')
        card['kind_label']={'image':'图片','audio':'音频','document':'文件','video':'视频'}.get(kind,meta['kind_label'])
        if not card['needs_confirmation'] and state not in {'ignored','completed'}:
            card['state_label']='系统核验类型' if kind=='unknown_media' else '自动处理中' if state in {'pending','queued','running'} else '等待系统排查'
            if kind=='unknown_media':
                info=classifications.get(h['hold_id'],{})
                card['routing_wait']=info.get('state')=='retry' or h.get('missing') or not meta['available']
                routing=json.loads(info.get('evidence_json','{}'))
                card['routing_message']=routing.get('reason','系统自动核验媒体类型，无需你确认。')
                card.update(repair_phase=routing.get('repair_phase','scheduled'),current_blocker=routing.get('reason_code',''),
                            next_check_at=info.get('next_attempt'),attempt_count=info.get('attempts',0),
                            no_progress_count=routing.get('no_progress_count',0),evidence_ref=routing.get('evidence_ref'))
                if routing.get('repair_phase')=='investigating':
                    card['routing_wait']=False;card['state_label']='系统调查中'
                elif routing.get('repair_phase')=='verifying_original':card['state_label']='核验原件'
                if h.get('missing'):card['unavailable_reason']='原媒体登记已缺失，系统保留线索继续核查。'
            else:card['routing_message']='已确认不是视频，按现有内容处理和归档规则自动执行。'
        card['revision']=revision(card)
        card['can_process']=card['needs_confirmation'] and bool(meta['available']) and state in {'pending','failed'}
        card['can_ignore']=card['needs_confirmation'] and state in {'pending','failed'}
        card['can_restore']=state=='ignored'
        out.append(card)
    return out

def decorate(value,row,held,classifications=None,saved=None):
    if row['kind']!='video_hold':
        if row.get('resolution_kind')=='media_separated':
            value.update(title='视频已转为逐条处理',reason='视频已有独立待处理条目；原附件读取回执保留，未标记为已完整读取。',video_waiting=False)
        return value
    cards=cards_for(row,held,classifications=classifications,saved=saved)
    if not cards:return value
    value['media_cards']=cards
    active=[c for c in cards if c['state'] not in {'ignored','completed'}]
    shown=active or cards
    value['title']=shown[0]['display_name'] if len(shown)==1 else f"{len(shown)} 项媒体待处理"
    value['object_label']='；'.join(dict.fromkeys(c['source_description'] for c in shown))[:240]
    value['attention_label']=shown[0]['state_label'] if len(shown)==1 else '等待逐项选择'
    value['reason']='先查看具体对象，再选择处理或忽略。'
    value['recovery_boundary']='处理后保留理解结果；忽略仅结束此版本的提醒，原件保留。'
    value['held_media']=[]
    route_value(value,shown)
    return value

def decorate_group(value,members):
    cards=[c for m in members for c in m.get('media_cards',[])]
    active=[c for c in cards if c['state'] not in {'completed','ignored'}]
    shown=active if value['status']=='open' and active else cards
    if shown:
        value['media_cards']=shown
        value['held_media']=[]
        value['title']=shown[0]['display_name'] if len(shown)==1 else f"{len(shown)} 项媒体待处理" if active else f"{len(shown)} 项媒体处理记录"
        value['object_label']='；'.join(c['display_name'] for c in shown)[:240] if len(shown)>1 else shown[0]['source_description']
        value['attention_label']=shown[0]['state_label'] if len(shown)==1 else '等待逐项选择'
        value['reason']='每条媒体分别选择处理或忽略。'
        value['recovery_boundary']='视频等你选择处理；其他媒体自动核验、处理并保留结果。'
        route_value(value,shown)
    return value

def route_value(value,cards):
    active=[c for c in cards if c['state'] not in {'ignored','completed'}]
    if not active:return
    manual=[c for c in active if c['needs_confirmation'] and c['state'] in {'pending','failed'}]
    if manual and len(manual)==len(active):
        value.update(attention_state='waiting',attention_label='视频待命',
                     title=cards[0]['display_name'] if len(cards)==1 else f"{len(cards)} 项视频待命",
                     reason='按你的指令继续待命；系统不下载或解析视频，其他内容继续处理。')
    elif any(c['state'] in {'queued','running'} for c in active):value['attention_state']='automatic'
    elif all(c['state']=='failed' or c.get('unavailable_reason') or c.get('routing_wait') for c in active):value['attention_state']='waiting'
    else:value['attention_state']='automatic'
    investigations=[c for c in active if c.get('repair_phase')=='investigating']
    if investigations:
        value['reason_code']='media_type_investigating'
        value['repair_phase']='investigating'
        value['current_blocker']=investigations[0].get('current_blocker','media_type_unknown')
        value['next_check_at']=min((c.get('next_check_at') or 0 for c in investigations),default=0)
    if not manual or len(manual)!=len(active):
        value['attention_label']='系统自动处理' if value['attention_state']=='automatic' else '等待系统核验'
        value['title']=cards[0]['display_name'] if len(cards)==1 else f"{len(cards)} 项媒体由系统处理"
        value['reason']='系统核对当前来源类型；非视频自动处理，视频继续待命。'



class SnapshotChanged(__import__("ledger_recovery").Deferred):
    pass


def snapshot_signatures(paths):
    result={}
    for path in paths:
        try:
            st=path.stat();result[str(path)]=(st.st_ino,st.st_size,st.st_mtime_ns)
        except FileNotFoundError:result[str(path)]=None
    return result


def snapshot_current(prepared):
    return snapshot_signatures([Path(p) for p in prepared['signatures']])==prepared['signatures']


def prepare(connection, config, mail=None):
    import operational_workbench as w
    import media_classification
    paths=[video_hold.DATABASE,Path(str(video_hold.DATABASE)+'-wal')]
    state_root=(config.get('email_sync') or {}).get('state_root')
    if state_root:
        paths+=list(Path(state_root).glob('*.sqlite*'))
    before=snapshot_signatures(paths)
    holds=video_hold.rows();saved=decisions();classifications=media_classification.records()
    if mail is None:
        try:mail=w._mail(config)
        except (OSError,ValueError,sqlite3.Error,KeyError):mail={'attachments':[]}
    attachment_map={(x['message_key'],x['attachment_id']):x for x in mail.get('attachments',[])}
    rows=[dict(r) for r in connection.execute("SELECT * FROM operational_issues WHERE kind='video_hold' OR json_extract(observation_json,'$.video_waiting')=1")]
    import media_sources
    descriptions={h['hold_id']:media_sources.describe(h,config,mail=mail,classifications=classifications) for h in holds}
    summaries={};cards={}
    for row in rows:
        ref=json.loads(row['source_ref_json'])
        hs=[h for h in holds if (row['kind']=='video_hold' and h['hold_id']==ref.get('hold_id')) or
            (row['kind']=='attachment' and h['source_id']=='dingtalk_mail' and all(json.loads(h['object_ref']).get(k)==ref.get(k) for k in ['message_key','attachment_id']))]
        obj=attachment_map.get((ref.get('message_key'),ref.get('attachment_id')))
        summaries[row['issue_id']]=video_hold.archived_reading(obj) if obj else None
        if row['kind']=='video_hold':cards[row['issue_id']]=cards_for(row,hs,config,classifications=classifications,saved=saved,descriptions=descriptions)
    return dict(holds=holds,saved=saved,cards=cards,summaries=summaries,
                revisions={r['issue_id']:r['revision'] for r in rows},signatures=before)


def reconcile(connection, config, stamp, prepared=None):
    import operational_workbench as w
    prepared = prepared if prepared is not None else prepare(connection, config)
    if not snapshot_current(prepared):
        return False
    holds=prepared['holds']; saved=prepared['saved']
    rows=[dict(r) for r in connection.execute("SELECT * FROM operational_issues WHERE kind='video_hold' OR json_extract(observation_json,'$.video_waiting')=1")]
    for row in rows:
        if prepared['revisions'].get(row['issue_id']) != row['revision']: continue
        ref=json.loads(row['source_ref_json'])
        hs=[h for h in holds if (row['kind']=='video_hold' and h['hold_id']==ref.get('hold_id')) or
            (row['kind']=='attachment' and h['source_id']=='dingtalk_mail' and all(json.loads(h['object_ref']).get(k)==ref.get(k) for k in ['message_key','attachment_id']))]
        if row['kind']=='attachment' and hs:
            for held in hs:
                child=connection.execute("SELECT issue_id FROM operational_issues WHERE kind='video_hold' AND json_extract(source_ref_json,'$.hold_id')=?",(held['hold_id'],)).fetchone()
                if child:
                    connection.execute('INSERT OR IGNORE INTO issue_alert_links SELECT ?,alert_id,alert_snapshot_json FROM issue_alert_links WHERE issue_id=?',(child['issue_id'],row['issue_id']))
            try:
                summary=prepared['summaries'].get(row['issue_id'])
            except (OSError,ValueError,sqlite3.Error):summary=None
            if summary and summary['non_video_complete']:
                w._finish(connection,row['issue_id'],'media_separated',{'reason':'已核对非视频部分回执；视频转到各自的独立待处理条目，未将附件标记为完整读取。','media_ids':[h['hold_id'] for h in hs]},stamp)
            continue
        if row['kind']=='video_hold' and row['status']=='open':
            routing={}
            route_value(routing,prepared['cards'].get(row['issue_id'],[]))
            if routing.get('attention_state') and routing['attention_state']!=row['attention_state']:
                connection.execute('UPDATE operational_issues SET attention_state=?,revision=revision+1 WHERE issue_id=?',(routing['attention_state'],row['issue_id']))
        ids=[h['hold_id'] for h in hs] or ([ref.get('hold_id')] if row['kind']=='video_hold' else [])
        ds=[saved.get(i,{}) for i in ids]
        settled=bool(ds) and all(d.get('state') in {'ignored','completed'} for d in ds)
        if settled and row['status']=='open':
            # Only video-only attachment issues may close. Other coverage errors stay open.
            if row['kind']=='attachment':
                summary=prepared['summaries'].get(row['issue_id'])
                if not summary or not summary['non_video_complete']:continue
            reason='这些媒体已逐项处理或由用户忽略；原件保留，未将忽略当作已读。'
            w._finish(connection,row['issue_id'],'media_resolved',{'reason':reason,'media_ids':ids},stamp)
        elif not settled and row['status']=='closed' and row['resolution_kind']=='media_resolved':
            connection.execute("UPDATE operational_issues SET status='open',attention_state='needs_user',resolution_kind=NULL,resolved_at=NULL,evidence_json='{}',revision=revision+1 WHERE issue_id=?",(row['issue_id'],))

    # The caller's transaction rolls back if another source changed mid-commit.
    if not snapshot_current(prepared):
        raise SnapshotChanged('media_snapshot_changed')
    return True


def execute(app,issue_id,payload):
    import operational_workbench as w
    import dingtalk_decision_center as center
    required={'action_id','action','revision','hold_id','version'}
    if set(payload)!=required or payload['action'] not in ACTIONS:raise w.WorkbenchError('action_contract','媒体处理请求字段无效。')
    try:uuid.UUID(payload['action_id'])
    except (ValueError,TypeError):raise w.WorkbenchError('action_contract','操作编号无效。')
    request_hash=video_hold.digest([issue_id,payload]);action=payload['action']
    with center.maintenance_write_guard(), w.syncv2.file_lock(w._lock_path(),blocking=True):
        c=connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM media_operations WHERE action_id=?',(payload['action_id'],)).fetchone()
            if old:
                if old['request_hash']!=request_hash:raise w.WorkbenchError('action_conflict','此操作编号已用于另一请求。',409)
                return {**json.loads(old['result_json']),'saved':True,'idempotent_replay':True}
            row=w.raw_issue(issue_id)
            public=w._public(row,w._object(app.config,row) if row['kind']=='attachment' else None)
            card=next((x for x in public.get('media_cards',[]) if x['hold_id']==payload['hold_id']),None)
            if not card or card['version']!=payload['version'] or card['revision']!=payload['revision']:
                raise w.WorkbenchError('issue_stale','媒体或处理状态已经变化，请刷新后再选择。',409)
            flag={'process_media':'can_process','ignore_media':'can_ignore','restore_media':'can_restore'}[action]
            if not card[flag]:raise w.WorkbenchError('action_unavailable',card.get('unavailable_reason') or '此媒体当前不能执行该操作。',409)
            state={'process_media':'queued','ignore_media':'ignored','restore_media':'pending'}[action]
            message={'process_media':'已排队，正在准备读取这条媒体。','ignore_media':'已忽略：从待处理列表移除，原文件保留。','restore_media':'已恢复到待处理，等待你重新选择。'}[action]
            result={'message':message}
            c.execute('''INSERT INTO media_decisions(hold_id,version,issue_id,state,revision,action_id,stage,result_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(hold_id) DO UPDATE SET state=excluded.state,revision=excluded.revision,
                action_id=excluded.action_id,stage=excluded.stage,result_json=excluded.result_json,updated_at=excluded.updated_at''',
                (card['hold_id'],card['version'],issue_id,state,card['decision_revision']+1,payload['action_id'],LABELS[state],json.dumps(result,ensure_ascii=False),video_hold.stamp()))
            c.execute("UPDATE media_decisions SET source_binding=?,decision_mode='user' WHERE hold_id=?",(card['source_binding'],card['hold_id']))
            response={'saved':True,'state':state,'result':result,'action_id':payload['action_id']}
            c.execute('INSERT INTO media_operations VALUES(?,?,?,?,?,?)',(payload['action_id'],request_hash,card['hold_id'],action,json.dumps(response,ensure_ascii=False),video_hold.stamp()))
            c.commit()
        finally:c.close()
        project_decision(card['hold_id'])
        try:w.reconcile(app.config,_lock_held=True)
        except (OSError,ValueError,sqlite3.Error):pass  # Saved per-media decision is independent of other unavailable sources.
    if action=='process_media':spawn(app.config_path)
    return response

def spawn(config_path):
    return subprocess.Popen([os.sys.executable,str(Path(__file__).with_name('media_processor.py')),'--config',str(config_path)],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)

def project_decision(ident):
    import operational_workbench as w
    d=decisions().get(ident)
    if not d:return
    with w._db() as c:
        row=c.execute('SELECT * FROM operational_issues WHERE issue_id=?',(d['issue_id'],)).fetchone()
        if not row or row['kind']!='video_hold':return
        if d['state'] in {'ignored','completed'}:
            reason='用户已忽略此版本，原件保留；未标记为已读。' if d['state']=='ignored' else '此版本媒体理解和归档已完成，结果回执保留。'
            w._finish(c,d['issue_id'],'media_resolved',{'reason':reason,'media_ids':[ident]},video_hold.stamp())
        elif row['status']=='closed' and row['resolution_kind']=='media_resolved':
            c.execute("UPDATE operational_issues SET status='open',attention_state='needs_user',resolution_kind=NULL,resolved_at=NULL,evidence_json='{}',revision=revision+1 WHERE issue_id=?",(d['issue_id'],))
        w._reconcile_groups(c,video_hold.stamp())

def update(ident, state, stage, result=None):
    with connect() as c:
        if result is None:
            c.execute('UPDATE media_decisions SET state=?,stage=?,revision=revision+1,updated_at=? WHERE hold_id=?',(state,stage,video_hold.stamp(),ident))
        else:
            c.execute('UPDATE media_decisions SET state=?,stage=?,result_json=?,revision=revision+1,updated_at=? WHERE hold_id=?',(state,stage,json.dumps(result,ensure_ascii=False),video_hold.stamp(),ident))

    if state in {'completed','ignored','pending'}:project_decision(ident)
