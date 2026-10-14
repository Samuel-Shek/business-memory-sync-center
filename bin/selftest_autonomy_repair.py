"""Regression tests against real entrypoints with synthetic sources only."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext
import dingtalk_minutes_memory as minutes
import email_sync as mail
import model_router as router
import dingtalk_sync_orchestrator as orchestration
import business_sync_controller as controller
from selftest_email_sync import temporary_config

class RepairTests(unittest.TestCase):
 def test_missing_sources_do_not_abort_complete_recordings(self):
  entries=[('missing',{'start_time':1790000000000,'missing_artifacts':['transcript']}),
           ('ready',{'start_time':1790000000001,'missing_artifacts':[], 'media_sha256':'ok','media_storage':'local'})]
  with patch.object(minutes,'load_sync_entries',return_value=entries), patch.object(minutes.syncv2,'file_lock',return_value=nullcontext()), \
       patch.object(minutes,'transcript_payload',return_value=(Path('/fixture'),'text')),patch.object(minutes.syncv2,'sha256_file',return_value='x'), \
       patch.object(minutes,'upsert_discovered_recording',return_value=({},True,False)) as upsert, \
       patch.object(minutes,'resolve_visible_incidents',return_value=0),patch.object(minutes.syncv2,'set_component_health'):
   result=minutes.run_source_sync({'dingtalk_minutes_memory':{'cutover_at':'2020-01-02T00:00:00+08:00'}})
  self.assertEqual(result['status'],'waiting_source');self.assertEqual(result['discovered'],1)
  self.assertEqual(result['waiting_count'],1);self.assertEqual(result['failures'],[]);self.assertEqual(upsert.call_count,1)
  self.assertNotIn('missing',json.dumps(result['waiting_items'][0]['task_ref_sha256']))
 def test_source_index_lookup_preserves_related_business_record(self):
  with tempfile.TemporaryDirectory() as td:
   cfg=temporary_config(Path(td)); records=[{'source_ref':'email-conversation:x','memory_kind':'status','source_system':'email'},
    {'source_ref':'email-conversation:x','memory_kind':'source_index','source_system':'email','raw_frontmatter':'a','path':'/fixture/source.md','memory_id':'id'}]
   proposal={'source_summary':{'summary':'synthetic','evidence_boundary':'synthetic'}}
   with patch.object(mail.memory,'rebuild_existing_note',return_value='new'):
    result=mail.source_note_change(cfg,records,set(),'email-conversation:x','x',proposal,'2020-01-02')
    self.assertEqual(result[0],Path('/fixture/source.md'))
    with self.assertRaises(mail.EmailSyncError):mail.source_note_change(cfg,records+[records[1]],set(),'email-conversation:x','x',proposal,'2020-01-02')
 def test_non_codex_quota_not_attributed_to_codex(self):
  failure=router.RouterExhausted('quota',[{'provider':'deepseek','category':'quota','cycle':1}])
  self.assertEqual(router.legacy_error_code(failure),'model_quota_unavailable')
  self.assertNotIn('detail',router.failure_projection(failure)['providers'][0])
 def test_source_drift_not_masked_by_final_quota(self):
  failure=router.RouterExhausted('quota',[{'provider':'kimi','category':'invalid_output','rejection_code':'proposal_source_drift'},{'provider':'deepseek','category':'quota'}])
  self.assertEqual(router.legacy_error_code(failure),'model_source_changed')
 def test_structured_partial_error_preserved(self):
  from subprocess import CompletedProcess
  p=CompletedProcess([],2,json.dumps({'status':'partial','failures':['source_incomplete']}),'')
  with patch.object(orchestration.subprocess,'run',return_value=p):
   with self.assertRaises(orchestration.OrchestratorError) as got:orchestration.run_json([],timeout=1,environment={},component='recording_source')
  self.assertEqual(got.exception.code,'recording_source_source_incomplete')
 def test_complete_queue_continues_with_source_wait(self):
  result={'recordings_new':0,'recordings_updated':0,'recording_media_pending':0,'memory_writes':0,'model_calls':0}
  with patch.object(controller,'set_step'),patch.object(controller,'require_automatic_power'),patch.object(controller.syncv2,'set_component_health'), \
    patch.object(controller.dingtalk,'recording_queue',return_value=[]), patch.object(controller.dingtalk,'run_json',side_effect=[{'status':'success','selected':0},{'status':'waiting_source','waiting_count':1,'waiting_items':[{'reason':'source_incomplete'}]}]):
   self.assertTrue(controller.run_minutes_part(Path('/fixture'), 'run', {}, result))
  self.assertEqual(result['recording_source_waiting_count'],1)
 def test_minutes_shared_slot_wait_exits_after_one_attempt(self):
  for reason in ('chat_ai_priority','future_wait_reason'):
   result={'recordings_new':0,'recordings_updated':0,'recording_media_pending':0,'memory_writes':0,'model_calls':0}
   receipts=[{'status':'success','selected':0},{'status':'success'},
             {'status':'deferred','reason':reason,'model_calls':0}]
   with patch.object(controller,'set_step') as step,patch.object(controller,'require_automatic_power'),patch.object(controller.syncv2,'set_component_health'), \
     patch.object(controller.dingtalk,'recording_queue',return_value=['fixture']),patch.object(controller.dingtalk,'recording_status',return_value='chunking'), \
     patch.object(controller.dingtalk,'run_json',side_effect=receipts) as run:
    with self.assertRaises(controller.ControllerError) as got:controller.run_minutes_part(Path('/fixture'),'run',{},result)
   expected='chat_ai_priority' if reason=='chat_ai_priority' else 'recording_semantic_deferred'
   self.assertEqual(got.exception.code,expected)
   self.assertIn(expected,controller.DEFERRED_ERROR_CODES)
   self.assertEqual(run.call_count,3) # scan + source + one semantic call
   self.assertEqual(result['model_calls'],0)
   self.assertEqual(step.call_args.args[2],'deferred')
 def test_wait_step_is_not_marked_failed(self):
  with patch.object(controller.syncv2,'list_action_steps',return_value=[{'status':'running','key':'recording_memory'}]),patch.object(controller,'set_step') as step:
   controller.mark_running_step_failed('fixture','chat_ai_priority')
  self.assertEqual(step.call_args.args[2],'deferred')
 def test_known_fix_grant_preserves_attempts_and_cannot_repeat(self):
  import mail_auto_recovery as recovery
  import shutil
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);cfg=temporary_config(root);mail.initialize(cfg)
   task=recovery.observe(cfg,'semantic-fixture','semantic','input-v1','source_note_collision',attempted=True)
   self.assertEqual(task['attempt_count'],1)
   (root/'state/sync-autonomy').mkdir(parents=True);(root/'bin').mkdir(exist_ok=True)
   names=('email_sync.py','email_phases.py','sync_repair_rules.py','image_frame_coverage.py','mail_auto_recovery.py','mail_attachment_reader.py','mail_attachment_worker.ps1')
   for name in names:shutil.copy2(Path(mail.__file__).parent/name,root/'bin'/name)
   log=root/'test.log';log.write_text('passed synthetic fixture')
   release={'policy':'sync-center-owner-approved-20260929-v1','tests_passed':True,'release_id':'fixture-v1','code_binding':{n:mail.sha256_file(root/'bin'/n) for n in names},'tests':[{'path':str(log),'sha256':mail.sha256_file(log)}]}
   (root/'state/sync-autonomy/release.json').write_text(json.dumps(release))
   with patch.object(mail,'BRIDGE_ROOT',root):
    self.assertEqual(recovery.release_repairs(cfg),1)
    same=recovery.observe(cfg,'semantic-fixture','semantic','input-v1','source_note_collision')
    self.assertEqual(same['state'],'pending');self.assertEqual(same['attempt_count'],1)
    recovery.observe(cfg,'semantic-fixture','semantic','input-v1','source_note_collision',attempted=True)
    self.assertEqual(recovery.release_repairs(cfg),0)
    same=recovery.observe(cfg,'semantic-fixture','semantic','input-v1','source_note_collision')
    self.assertEqual(same['attempt_count'],2);self.assertEqual(same['state'],'needs_maintenance')

if __name__=='__main__':unittest.main()
