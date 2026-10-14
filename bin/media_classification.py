"""Bounded media routing. Exact image hints permit original verification only."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import argparse,fcntl,json,sqlite3,subprocess,sys,time,uuid,re
from datetime import datetime
from pathlib import Path
import video_hold,media_sources,media_release
POLICY='nonvideo-auto-v1'

def connect():
 c=video_hold._connect()
 c.execute('''CREATE TABLE IF NOT EXISTS media_classifications(hold_id TEXT PRIMARY KEY,version TEXT NOT NULL,
 kind TEXT NOT NULL,state TEXT NOT NULL,evidence_json TEXT NOT NULL DEFAULT '{}',attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)''');c.commit();return c

def records():
 if not video_hold.DATABASE.exists():return {}
 with sqlite3.connect(video_hold.DATABASE.as_uri()+'?mode=ro',uri=True) as c:
  c.row_factory=sqlite3.Row
  if not c.execute("SELECT 1 FROM sqlite_master WHERE name='media_classifications'").fetchone():return {}
  return {r['hold_id']:dict(r) for r in c.execute('SELECT * FROM media_classifications')}

def effective(h,rows=None):
 r=(rows if rows is not None else records()).get(h['hold_id'],{})
 return r.get('kind') if r.get('version')==h.get('version') and r.get('state')=='identified' else h.get('kind','unknown_media')

def record(h,kind,evidence,*,state='identified',attempted=True,retry_after=None):
 import media_processor
 old=records().get(h['hold_id'],{})
 attempts=old.get('attempts',0)+int(attempted)
 retry=0 if state=='identified' else time.time()+(retry_after if retry_after is not None else min(21600,300*2**min(max(attempts-1,0),6)))
 # Keep every transition, including the previous evidence on the first upgraded call.
 directory=video_hold.ROOT/'receipts/media-classification'/h['hold_id']
 receipt={'hold_id':h['hold_id'],'version':h['version'],'kind':kind,'state':state,'attempts':attempts,
          'next_attempt':retry,'evidence':evidence,'previous':old,'at':video_hold.stamp()}
 media_processor.save(directory/(str(uuid.uuid4())+'.json'),receipt)
 with connect() as c:
  c.execute("""INSERT INTO media_classifications VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(hold_id) DO UPDATE SET
   version=excluded.version,kind=excluded.kind,state=excluded.state,evidence_json=excluded.evidence_json,
   attempts=excluded.attempts,next_attempt=excluded.next_attempt,updated_at=excluded.updated_at""",
   (h['hold_id'],h['version'],kind,state,json.dumps(evidence,ensure_ascii=False),attempts,retry,video_hold.stamp()))

ROUTING_POLICY='image-fallback-20260929-v1'

def waiting(h, prior, code, *, fingerprint=None, attempted=False, image_attempt=False):
 old=prior.get(h['hold_id'],{})
 evidence=json.loads(old.get('evidence_json','{}')) if old.get('version')==h['version'] else {}
 unchanged=bool(fingerprint and fingerprint==evidence.get('response_fingerprint'))
 if code=='image_candidate_wait':fingerprint=None;unchanged=False
 count=evidence.get('no_progress_count',0)+1 if unchanged else 1 if fingerprint else evidence.get('no_progress_count',0)
 image_attempts=evidence.get('image_attempts',0)+int(image_attempt)
 investigation=((count>=2 and code!='image_candidate_wait') or code in {'image_frame_budget','image_decode_failed','image_format_unrecognized','media_source_changed',
   'media_cache_incomplete','media_cache_mismatch','media_cache_path','media_acquisition_uncertain','image_decoder_unavailable','image_retry_exhausted','media_download_denied'})
 reasons={'metadata_empty':'接口没有返回明确类型，系统继续查证。',
  'source_unavailable':'原始来源暂不可用，等待来源恢复。','source_changed':'来源版本变化，等待当前版本登记。',
  'image_candidate_wait':'已找到精确图片引用，等待原件核验。',
  'image_retry_exhausted':'原件核验已达到有限尝试上限，系统调查原因；历史与原件保留。',
  'media_download_denied':'来源拒绝下载，等待权限或来源条件变化。'}
 evidence.update(policy=ROUTING_POLICY,reason_code=code,reason=reasons.get(code,'原件核验未完成，系统保留证据继续调查。'),
   repair_phase='investigating' if investigation else 'waiting_source' if code=='source_unavailable' else 'verifying_original' if code=='image_candidate_wait' else 'scheduled',
   no_progress_count=count,image_attempts=image_attempts)
 if fingerprint:evidence['response_fingerprint']=fingerprint
 delay=86400 if investigation or code=='source_unavailable' else 60 if code=='image_candidate_wait' else min(21600,300*2**min(image_attempts,6))
 record(h,'unknown_media',evidence,state='retry',attempted=attempted,retry_after=delay)

def match_resource(h,info,message):
 ref=json.loads(h['object_ref']);args=info['args']
 if message.get('openMessageId')!=ref['message_id'] or message.get('openConversationId')!=args.get('open-conversation-id'):return None
 candidates=[r for r in message.get('resources',[]) if r.get('resourceId')==info['resource'].get('resourceId') and r.get('resourceIdType')==info['resource'].get('type')]
 method='exact_resource_id'
 if not candidates:
  resources=message.get('resources',[])
  # Match the complete media link and sole typed resource; never guess by ordinal.
  link='(mediaId='+str(info['resource'].get('resourceId',''))+')'
  if len(resources)==1 and info['resource'].get('type')=='mediaId' and resources[0].get('resourceIdType')=='mediaId' and link in str(message.get('content','')):
   candidates=resources;method='unique_typed_resource_in_same_message'
 if len(candidates)!=1:return None
 r=candidates[0];kind=str(r.get('resourceType','')).lower()
 mapped={'video':'video','image':'image','picture':'image','audio':'audio','voice':'audio','emoji':'image'}.get(kind)
 if not mapped:return None
 return mapped,{'method':'dingtalk_resource_metadata','binding_method':method,'resource_type':kind,'resource_sha256':video_hold.digest(r['resourceId']),
  'original_resource_sha256':video_hold.digest(info['resource']),'download_resource_id':r['resourceId'],'download_resource_type':r['resourceIdType']}

def scan(config,fetch=None):
 import media_actions as actions,media_image_intake as intake
 saved=actions.decisions();prior=records();pending=[];visited=0
 def eligible_order(h):
  prev=prior.get(h['hold_id'],{})
  try:first=datetime.fromisoformat(h.get('first_seen','')).timestamp()
  except (ValueError,TypeError):first=0
  return (prev.get('next_attempt') or first,prev.get('updated_at') or h.get('first_seen',''),h['hold_id'])
 holds=sorted(video_hold.rows(),key=eligible_order)
 for h in holds:
  decision=saved.get(h['hold_id'],{})
  if h['kind']!='unknown_media' or (decision.get('version')==h['version'] and decision.get('state') in {'ignored','completed','queued','running'}):continue
  prev=prior.get(h['hold_id'],{});ev=json.loads(prev.get('evidence_json','{}'))
  if prev.get('version')==h['version'] and (prev.get('state')=='identified' or (ev.get('policy')==ROUTING_POLICY and prev.get('next_attempt',0)>time.time())):continue
  if visited>=50:break
  visited+=1
  try:
   info=media_sources.resolve(h,config)
   if info['mode']!='chat' or not info.get('ready'):raise ValueError('source_unavailable')
   pending.append((h,info))
  except Exception:waiting(h,prior,'source_unavailable')
 if not pending:return records()
 ids=list(dict.fromkeys(json.loads(h['object_ref'])['message_id'] for h,_ in pending))
 try:
  if fetch:payload=fetch(ids)
  else:
   r=subprocess.run([str(config.get('dingtalk_chat',{}).get('dws_bin') or _archive_path('/Users/example/.local/bin/dws')),'chat','message','list-by-ids','--msg-ids',','.join(ids),'--format','json'],capture_output=True,text=True,timeout=60)
   if r.returncode:raise ValueError('metadata_query_failed')
   payload=json.loads(r.stdout)
  if payload.get('success') is not True:raise ValueError('metadata_query_failed')
  msgs=payload.get('result',{}).get('messages',[])
 except Exception:
  for h,_ in pending:waiting(h,prior,'metadata_query_failed',attempted=True)
  return records()
 acquired=False
 for h,info in pending:
  try:
   matches=[m for m in msgs if m.get('openMessageId')==json.loads(h['object_ref'])['message_id'] and m.get('openConversationId')==info['args'].get('open-conversation-id')]
   message=matches[0] if len(matches)==1 else {}
   intake.require_current(h,info,config)
   result=match_resource(h,info,message)
   if result:record(h,*result);continue
   # Store only a hash of the relevant response, never private message text.
   hint=intake.exact_image_reference(h,info,message)
   fingerprint=video_hold.digest([h['version'],info['resource'],message.get('resources',[]),hint,len(matches)])
   if not hint:
    waiting(h,prior,'metadata_empty',fingerprint=fingerprint,attempted=True);continue
   prev=prior.get(h['hold_id'],{})
   ev=json.loads(prev.get('evidence_json','{}')) if prev.get('version')==h['version'] else {}
   if ev.get('image_attempts',0)>=3:
    waiting(h,prior,'image_retry_exhausted',fingerprint=fingerprint);continue
   if acquired:
    waiting(h,prior,'image_candidate_wait',fingerprint=fingerprint);continue
   acquired=True
   waiting(h,prior,'image_verifying',fingerprint=fingerprint,attempted=True,image_attempt=True)
   try:
    result=intake.acquire_image(h,info,config)
    intake.require_current(h,info,config)
    record(h,*result,attempted=False)
   except Exception as exc:
    code=getattr(exc,'code','image_verification_failed')
    waiting(h,records(),code,fingerprint=fingerprint)
  except Exception:
   # One object's error cannot overwrite another object's successful evidence.
   waiting(h,prior,'source_changed')
 return records()

def queue_nonvideo(config):
 import media_actions as actions,operational_workbench as w
 rows=records();queued=0
 with w.syncv2.file_lock(w._lock_path(),blocking=True):
  for h in video_hold.rows():
   if effective(h,rows) not in {'image','audio','document'}:continue
   meta=media_sources.describe(h,config)
   if not meta['available']:continue
   with w._db(readonly=True) as db:
    issue=db.execute("SELECT issue_id,status FROM operational_issues WHERE kind='video_hold' AND json_extract(source_ref_json,'$.hold_id')=?",(h['hold_id'],)).fetchone()
   if not issue or issue['status']!='open':continue
   with actions.connect() as c:
    c.execute('BEGIN IMMEDIATE')
    old=c.execute('SELECT * FROM media_decisions WHERE hold_id=?',(h['hold_id'],)).fetchone()
    if old:
     if old['version']!=h['version']:continue
     # Bounded retries belong only to automatic non-video work. User decisions are immutable here.
     due=old['decision_mode']=='automatic' and old['state']=='failed' and old['automatic_retries']<3 and time.time()-datetime.fromisoformat(old['updated_at']).timestamp()>=600*2**old['automatic_retries']
     if not due:continue
     c.execute("UPDATE media_decisions SET state='queued',stage='等待自动重试',automatic_retries=automatic_retries+1,revision=revision+1,updated_at=?,source_binding=? WHERE hold_id=?",(video_hold.stamp(),meta['source_binding'],h['hold_id']))
     c.commit();queued+=1;continue
    result={'message':'已识别为非视频，正在自动处理。','authorization':POLICY}
    c.execute('''INSERT INTO media_decisions(hold_id,version,issue_id,state,revision,action_id,stage,result_json,updated_at,source_binding,decision_mode)
     VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(h['hold_id'],h['version'],issue['issue_id'],'queued',1,str(uuid.uuid4()),'等待自动处理',json.dumps(result,ensure_ascii=False),video_hold.stamp(),meta['source_binding'],'automatic'))
    c.commit();queued+=1
 return queued

def worker_idle():
 path=video_hold.ROOT/'receipts/media-processing/worker.lock'
 if not path.exists():return True
 with path.open('a+') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return False
  return True

def spawn(config_path):
 return subprocess.Popen([sys.executable,str(Path(__file__)), '--config',str(config_path)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)

def start_loop(config_path):
 import threading
 stop=threading.Event()
 def run():
  while not stop.is_set():
   try:spawn(config_path)
   except OSError:pass
   stop.wait(60)
 thread=threading.Thread(target=run,name='media-type-routing',daemon=True);thread.start();return stop

def cycle_receipt(phase,code=None):
 import media_processor
 path=video_hold.ROOT/'state/media-recovery-status.json'
 try:previous=json.loads(path.read_text())
 except (OSError,ValueError):previous={}
 same=previous.get('runtime_code_binding')==media_release.LOADED
 started=previous.get('first_cycle_at') if same else None
 count=previous.get('natural_cycles',0) if same else 0
 media_processor.save(path,{'policy':ROUTING_POLICY,'phase':phase,'reason_code':code,
   'runtime_code_binding':media_release.LOADED,'natural_cycles':count+int(phase=='checked'),
   'first_cycle_at':started or video_hold.stamp(),'updated_at':video_hold.stamp()})

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);args=parser.parse_args()
 lockpath=video_hold.DATABASE.parent/'media-classification.lock';lockpath.parent.mkdir(parents=True,exist_ok=True)
 with lockpath.open('a+') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return
  import dingtalk_decision_center as center,operational_workbench as w,media_actions as actions
  config=json.loads(args.config.read_text())
  try:
   with center.maintenance_write_guard():
    media_release.check()
    w.reconcile(config);queue_nonvideo(config)
    if worker_idle() and any(x['state'] in {'queued','running'} for x in actions.decisions().values()):actions.spawn(args.config)
    scan(config);w.reconcile(config);queue_nonvideo(config);w.reconcile(config)
    if worker_idle() and any(x['state'] in {'queued','running'} for x in actions.decisions().values()):actions.spawn(args.config)
    cycle_receipt('checked')
  except (center.DecisionError,media_release.ReleaseChanged):pass
  except Exception as exc:
   cycle_receipt('investigating',getattr(exc,'code',type(exc).__name__))
   raise
if __name__=='__main__':main()
