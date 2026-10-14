#!/usr/bin/env python3
"""Behavior checks for attention, persistent alerts, and fenced submissions."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json,sqlite3,tempfile,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import dingtalk_sync_v2 as syncv2,business_sync_state as state
import claim_reconciliation as reconciliation,case_conversation as conversation
import workbench_attention as attention,alert_workbench as alerts,dingtalk_decision_center as center
from selftest_case_conversation import add_case
COUNT=0
def check(condition,label):
 global COUNT
 if not condition:raise AssertionError(label)
 COUNT+=1
def rejected(code,fn,*args,**kwargs):
 try:fn(*args,**kwargs)
 except Exception as exc:check(getattr(exc,'code',None)==code, f'{label if False else code}: {type(exc).__name__}: {exc}')
 else:raise AssertionError('expected '+code)
def main():
 with tempfile.TemporaryDirectory() as directory:
  root=Path(directory)
  syncv2.ROOT=root;syncv2.STATE_DIR=root;syncv2.DATABASE=root/'sync.sqlite';syncv2.RUNS_DIR=root/'runs';syncv2.ARCHIVE_DIR=root/'archive';syncv2.DECISION_LOCK=root/'decision.lock';syncv2.AI_LOCK=root/'ai.lock';state.DATABASE=syncv2.DATABASE;alerts.LOCK=root/'alert.lock'
  reconciliation.DATABASE=root/'case.sqlite';reconciliation.RESOLUTION_ARCHIVE=root/'archive.jsonl';reconciliation.RELATION_OUTBOX=root/'outbox';reconciliation.MAINTENANCE_STATE=root/'maintenance.json'
  center.MAINTENANCE_STATE=root/'maintenance.json';center.MAINTENANCE_LOCK=root/'maintenance.lock';center.CONTROL_START_LOCK=root/'control-start.lock';center.CONTROL_LOCK=root/'control.lock'
  syncv2.initialize();state.initialize();reconciliation.initialize()
  case_id='case-'+'9'*20;add_case(case_id)
  # A missing conversation does not hide a valid case; first submission establishes it.
  check(not conversation.conversation_snapshot(case_id)['messages'],'missing thread has a usable empty conversation')
  payload={'content':'先等外部更新。  \n','case_version':1,'context_hash':'context-9999','client_message_id':str(uuid.uuid4())}
  with mock.patch.object(conversation,'_spawn_worker',side_effect=OSError('synthetic')):
   saved=conversation.append_message(root/'unused.json',case_id,payload)
  check(saved['saved'] and saved['status']=='failed' and saved['attention_state']=='answered','worker spawn failure cannot roll back saved user intent')
  check(reconciliation.list_cases('needs_user')['count']==0,'all workload consumers read canonical attention')
  check(conversation.append_message(root/'unused.json',case_id,payload)['idempotent_replay'],'lost HTTP receipt replays safely after archive')
  with mock.patch.object(conversation,'_spawn_worker',side_effect=OSError('synthetic')):
   for attempt in range(3):
    retry_payload={'case_version':1,'context_hash':'context-9999','client_message_id':str(uuid.uuid4())}
    conversation.retry_message(root/'unused.json',case_id,saved['message_id'],retry_payload)
   check(conversation.retry_message(root/'unused.json',case_id,saved['message_id'],retry_payload)['idempotent_replay'],'the final permitted retry remains replayable after a lost receipt')
   rejected('retry_limit',conversation.retry_message,root/'unused.json',case_id,saved['message_id'],{**retry_payload,'client_message_id':str(uuid.uuid4())})
  with mock.patch.object(center,'RotatingFileHandler',side_effect=OSError('synthetic full log directory')),mock.patch.object(center,'ROOT',root):
   check(len(center.log_api_error(RuntimeError('private synthetic content'),'GET','/api/alerts/missing?private=secret'))==12,'failed error logging does not break the HTTP error response')
  conversation.sync_case_threads();check(reconciliation.work_counts()['needs_user']==0,'restart does not reopen saved reply')
  case=reconciliation.case_detail(case_id);case['evidence']=[{'claim_handle':'one','title':'已有决定','lifecycle':'open','claim_kind':'human_decision'}]
  c=syncv2.connect()
  with c:attention.record(c,case,state='waiting_external',reason='已答复',source_kind='test')
  changed={**case,'context_hash':'new-hash','evidence':[{'claim_handle':'two','title':'已有决定','lifecycle':'open','claim_kind':'human_decision'}]}
  with c:check(not attention.refresh_case(c,changed),'a repackaged source with same decision does not reopen')
  changed['evidence'].append({'claim_handle':'fact','title':'新增资料版本记录','lifecycle':'open','claim_kind':'fact'})
  with c:check(not attention.refresh_case(c,changed),'new facts do not reactivate a previously answered decision')
  changed['evidence'].append({'claim_handle':'three','title':'新增预算选择','lifecycle':'open','claim_kind':'human_decision'})
  with c:check(attention.refresh_case(c,changed),'a new explicit decision reopens with an explanation')
  c.close()
  check(attention.material_kind('船舶技术确认表 V7.3')=='technical_checklist' and attention.material_kind('船舶目录册 V7')=='catalogue','checklist versions and catalogue versions have separate identity')
  check(attention.material_version('船舶目录册V7_1放行核验')=='V7.1','indexed declared versions normalize without implying approval')
  case=reconciliation.case_detail(case_id);c=syncv2.connect()
  with c:attention.record(c,case,state='waiting_external',reason='等后续',source_kind='test')
  c.close()
  attention.refresh_after_index({'claims':[{'claim_handle':'material-v8','case_id':'case-'+'8'*20,'subject_label':case['subject'],'title':'船舶目录册 V8 校订记录','source_path':'sources/current.md','observed_at':'2020-01-02T10:00:00+08:00'}]})
  updated=attention.decorate(case)
  check(updated['attention_state']=='waiting_external' and any(item['version']=='V8' and item['source_path']=='sources/current.md' for item in updated['related_context']),'later material indexes stay linked without resetting the user disposition')
  attachments=sqlite3.connect(':memory:');attachments.row_factory=sqlite3.Row
  attachments.execute('CREATE TABLE attachments(original_name,status,risk,extract_status,last_error_code,message_key,attachment_id,sha256,actual_size,stored_name)')
  attachments.executemany('INSERT INTO attachments(original_name,status,risk,extract_status,last_error_code,message_key,attachment_id,sha256,actual_size,stored_name) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         [(name,status,risk,extraction,error,'synthetic',name,'0'*64,1,name) for name,status,risk,extraction,error in [('A checked '+str(i),'verified','normal','extracted',None) for i in range(110)]+[('Z pending '+str(i),'needs_review','archive','archive_invalid',None) for i in range(2)]])
  with mock.patch.object(alerts.email_sync,'runtime_paths',return_value={'database':syncv2.DATABASE}),mock.patch.object(alerts.email_sync,'connect_database_readonly',return_value=attachments):
   pending=alerts.pending_attachments({})
  check(len(pending)==2 and all(item['status']=='needs_review' and item['extract_status']=='archive_invalid' for item in pending),'verified attachments cannot crowd pending files out of the bounded action result')
  state.update_source('dingtalk_mail',status='persistent_failure',error_code='read_safety_unconfirmed')
  raw=state.open_alert('dingtalk_mail','sync_failure','邮件安全确认','合成凭证要求',severity='error');alert_id=raw['alert_id']
  control={'sources':[{'id':'dingtalk_mail','control':{'available':True,'running':False}}]};app=SimpleNamespace(config={},config_path=root/'unused.json')
  invalid={'server_side_pass':True,'cross_device_confirmed':False,'binding_valid':True,'tested_at':'t1'}
  valid={**invalid,'cross_device_confirmed':True,'confirmed_at':'t2'}
  with mock.patch.object(alerts,'safe_safety',return_value=invalid),mock.patch.object(center,'current_control_state',return_value=control):
   before=alerts.detail({},alert_id,control=control)
   check(any(item['id']=='confirm_mail' for item in before['actions']),'invalid cross-device receipt offers explicit confirmation')
  with mock.patch.object(alerts,'safe_safety',return_value=valid),mock.patch.object(center,'current_control_state',return_value=control):
   current=alerts.detail({},alert_id,control=control)
   check(not current['action_required'] and not any(item['id']=='confirm_mail' for item in current['actions']),'valid credential removes obsolete button')
   rejected('alert_stale',alerts.execute_action,app,alert_id,{'action_id':str(uuid.uuid4()),'action':'confirm_mail','revision':before['revision'],'still_unread':True})
   first=alerts.save_draft(alert_id,{'content':'关闭后仍在','revision':0});check(first['draft_revision']==1,'draft persisted with revision')
   check(alerts.save_draft(alert_id,{'content':'关闭后仍在','revision':0})==first,'repeated draft save is idempotent')
   rejected('draft_conflict',alerts.save_draft,alert_id,{'content':'旧窗口覆盖','revision':0})
   check(alerts.conversation(alert_id)['draft']=='关闭后仍在','stale draft cannot overwrite later draft')
   msg={'client_message_id':str(uuid.uuid4()),'content':'请帮我讨论原因','ai_profile':conversation.DEFAULT_AI_PROFILE}
   with mock.patch.object(alerts,'spawn_worker'),mock.patch.object(conversation.memory,'load_config',return_value={}):
    receipt=alerts.submit_message(root/'unused.json',alert_id,msg)
    replay=alerts.submit_message(root/'unused.json',alert_id,msg)
   check(receipt['saved'] and replay['idempotent_replay'] and len(alerts.conversation(alert_id)['messages'])==1,'message delivery replay never duplicates user content')
   c=syncv2.connect_readonly();check(c.execute('SELECT count(*) FROM alert_action_receipts').fetchone()[0]==0,'chat submission cannot execute an alert action');c.close()
   with mock.patch.object(conversation,'invoke_case_ai',return_value={'answer':'只讨论，不执行。','conversation_state':'waiting_evidence','memory_candidate':None}):
    answer=alerts.answer_message({},receipt['message_id'])
   check(answer['status']=='success' and not answer['executed'] and len(alerts.conversation(alert_id)['messages'])==2,'AI answer persists without a command or business write path')
   action={'action_id':str(uuid.uuid4()),'action':'acknowledge','revision':current['revision']}
   done=alerts.execute_action(app,alert_id,action);again=alerts.execute_action(app,alert_id,action)
   check(done['saved'] and again['idempotent_replay'],'acknowledgement receipt replays after revision changes')
   check(state.alerts()[0]['status']=='open' and state.source('dingtalk_mail')['status']=='persistent_failure','acknowledgement never claims recovery')
   fresh=alerts.detail({},alert_id,control=control);retry={'action_id':str(uuid.uuid4()),'action':'retry_source','revision':fresh['revision']}
   with mock.patch.object(center,'start_control_action',side_effect=OSError('synthetic')):
    failed=alerts.execute_action(app,alert_id,retry)
   check(failed['state']=='failed' and failed['saved'],'failed operations have durable explicit receipts')
   check(alerts.execute_action(app,alert_id,retry)['idempotent_replay'],'failure replay does not execute again')
   c=syncv2.connect_readonly();check(c.execute('SELECT count(*) FROM case_messages').fetchone()[0]==4,'alert chat is isolated from business questions');c.close()
  for db in (syncv2.DATABASE,reconciliation.DATABASE):
   c=sqlite3.connect(db);check(c.execute('PRAGMA quick_check').fetchone()[0]=='ok','database remains valid');check(not c.execute('PRAGMA foreign_key_check').fetchall(),'foreign keys remain valid');c.close()
 print(json.dumps({'verdict':'pass','checks':COUNT}));return 0
if __name__=='__main__':raise SystemExit(main())
