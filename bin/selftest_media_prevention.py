"""Offline regressions: synthetic sources only; never touch production queues."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import copy, json, tempfile, unittest, sys, subprocess
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import media_classification as c, media_image_intake as intake, media_processor as worker
import media_sources as sources, media_actions as actions, video_hold

class Recovery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.patches=[patch.object(video_hold,'ROOT',self.root),patch.object(video_hold,'DATABASE',self.root/'holds.sqlite'),
    patch.object(worker,'RUNS',self.root/'runs'),patch.object(actions,'decisions',return_value={})]
  for p in self.patches:p.start()
  self.h={'hold_id':'a'*24,'version':'v1','kind':'unknown_media','source_id':'dingtalk_chat','object_ref':json.dumps({'message_id':'m','resource_index':0})}
  self.info={'mode':'chat','ready':True,'resource':{'type':'mediaId','resourceId':'@r'},'args':{'open-conversation-id':'c','message-id':'m','resource-id':'@r','type':'mediaId'}}
  self.message={'openMessageId':'m','openConversationId':'c','content':'[图片消息](mediaId=@r)'}
  self.cfg={'email_sync':{'phase_python':sys.executable},'wechat_chat':{},'dingtalk_automation':{'model':'fixture'}}
  self.patches.extend([patch.object(video_hold,'rows',return_value=[self.h]),patch.object(video_hold,'by_id',side_effect=lambda ident:self.h if ident==self.h['hold_id'] else None),patch.object(sources,'resolve',return_value=self.info)])
  for p in self.patches[4:]:p.start()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.tmp.cleanup()
 def fetch(self,ids):return {'success':True,'result':{'messages':[self.message]}}
 def fake_acquire(self,h,cfg,dest):
  dest.mkdir(parents=True,exist_ok=True);p=dest/'original.media';Image.new('RGB',(3,3),'red').save(p,format='PNG')
  return p,{'hold_id':h['hold_id'],'version':h['version'],'source_sha256':worker.sha(p),'source_bytes':p.stat().st_size,'source_id':'dingtalk_chat','member':None}
 def current(self):return c.records()[self.h['hold_id']]
 def due(self):
  with c.connect() as db:db.execute('update media_classifications set next_attempt=0')
 def test_exact_reference_requires_all_bindings(self):
  self.assertTrue(intake.exact_image_reference(self.h,self.info,self.message))
  for k,v in [('openMessageId','other'),('openConversationId','other'),('content','图片 (mediaId=@r)'),('content','[图片消息](mediaId=@r-other)'),('content','[视频消息](mediaId=@r)')]:
   msg={**self.message,k:v};self.assertFalse(intake.exact_image_reference(self.h,self.info,msg))
 def test_missing_type_acquires_and_identifies_real_png(self):
  with patch.object(sources,'acquire',side_effect=self.fake_acquire) as acquire:c.scan(self.cfg,self.fetch)
  self.assertEqual(acquire.call_count,1);self.assertEqual(self.current()['kind'],'image');self.assertEqual(self.current()['attempts'],1)
  ev=json.loads(self.current()['evidence_json']);self.assertEqual(ev['format'],'PNG');self.assertEqual(ev['frames'],1)
 def test_fresh_identified_does_not_download_again(self):
  c.record(self.h,'image',{'proof':'fixture'})
  with patch.object(sources,'acquire') as acquire:c.scan(self.cfg,self.fetch)
  acquire.assert_not_called()
 def test_metadata_only_never_acquires_known_video(self):
  self.message['resources']=[{'resourceId':'@r','resourceIdType':'mediaId','resourceType':'video'}]
  with patch.object(sources,'acquire') as acquire:c.scan(self.cfg,self.fetch)
  acquire.assert_not_called();self.assertEqual(self.current()['kind'],'video')
 def test_same_empty_response_enters_daily_investigation(self):
  self.message['content']='unknown';c.scan(self.cfg,self.fetch);self.due();c.scan(self.cfg,self.fetch)
  row=self.current();ev=json.loads(row['evidence_json']);self.assertEqual(ev['no_progress_count'],2);self.assertEqual(ev['repair_phase'],'investigating');self.assertGreater(row['next_attempt']-c.time.time(),86390)
 def test_source_wait_preserves_attempts(self):
  c.record(self.h,'unknown_media',{},state='retry');self.due()
  with patch.object(sources,'resolve',side_effect=RuntimeError('offline')):c.scan(self.cfg,self.fetch)
  self.assertEqual(self.current()['attempts'],1)
 def test_source_version_change_wakes_before_daily_due(self):
  self.message['content']='unknown';c.scan(self.cfg,self.fetch);self.due();c.scan(self.cfg,self.fetch)
  self.h['version']='v2'
  with patch.object(intake,'acquire_image') as acquire:c.scan(self.cfg,self.fetch)
  self.assertEqual(self.current()['version'],'v2');self.assertEqual(json.loads(self.current()['evidence_json'])['no_progress_count'],1)
 def test_retry_budget_retained_and_not_regranted(self):
  with patch.object(intake,'acquire_image',side_effect=intake.IntakeError('image_decode_failed')) as acquire:
   for _ in range(5):self.due();c.scan(self.cfg,self.fetch)
  self.assertEqual(acquire.call_count,3);ev=json.loads(self.current()['evidence_json']);self.assertEqual(ev['image_attempts'],3);self.assertEqual(ev['reason_code'],'image_retry_exhausted');self.assertEqual(self.current()['attempts'],3)
 def test_attempt_is_durable_before_acquisition(self):
  def check(*a):
   ev=json.loads(self.current()['evidence_json']);self.assertEqual(ev['image_attempts'],1);self.assertEqual(self.current()['attempts'],1)
   raise intake.IntakeError('image_decode_failed')
  with patch.object(intake,'acquire_image',side_effect=check):c.scan(self.cfg,self.fetch)
 def test_source_changes_after_download_never_identified(self):
  def changed(*args):
   result=self.fake_acquire(*args);self.h['version']='v2';return result
  frozen=copy.deepcopy(self.h)
  with patch.object(sources,'acquire',side_effect=changed):
   with self.assertRaises(intake.IntakeError):intake.acquire_image(frozen,self.info,self.cfg)
 def test_valid_cache_reused_without_download(self):
  with patch.object(sources,'acquire',side_effect=self.fake_acquire) as acquire:
   intake.acquire_image(self.h,self.info,self.cfg);intake.acquire_image(self.h,self.info,self.cfg)
  self.assertEqual(acquire.call_count,1)
 def test_partial_cache_preserved_without_redownload(self):
  work=worker.RUNS/self.h['hold_id']/video_hold.digest('v1')[:16];(work/'source').mkdir(parents=True);(work/'source/original.media').write_bytes(b'partial')
  with patch.object(sources,'acquire') as acquire:
   with self.assertRaisesRegex(intake.IntakeError,'media_cache_incomplete'):intake.acquire_image(self.h,self.info,self.cfg)
  acquire.assert_not_called();self.assertEqual((work/'source/original.media').read_bytes(),b'partial')
 def test_unknown_download_outcome_no_blind_retry(self):
  with patch.object(sources,'acquire',side_effect=TimeoutError('interrupted')) as acquire:
   with self.assertRaises(TimeoutError):intake.acquire_image(self.h,self.info,self.cfg)
   with self.assertRaisesRegex(intake.IntakeError,'media_acquisition_uncertain'):intake.acquire_image(self.h,self.info,self.cfg)
  self.assertEqual(acquire.call_count,1)
 def test_corrupt_cached_original_is_not_overwritten(self):
  with patch.object(sources,'acquire',side_effect=self.fake_acquire):intake.acquire_image(self.h,self.info,self.cfg)
  p=worker.RUNS/self.h['hold_id']/video_hold.digest('v1')[:16]/'source/original.media';p.write_bytes(b'bad')
  with patch.object(sources,'acquire') as acquire:
   with self.assertRaisesRegex(intake.IntakeError,'media_cache_mismatch'):intake.acquire_image(self.h,self.info,self.cfg)
  acquire.assert_not_called()
 def test_batch_limited_to_one_acquisition_without_starving_waiters(self):
  h2={**self.h,'hold_id':'b'*24}
  with patch.object(video_hold,'rows',return_value=[self.h,h2]),patch.object(video_hold,'by_id',side_effect=lambda ident:self.h if ident==self.h['hold_id'] else h2),patch.object(intake,'acquire_image',return_value=('image',{'proof':'test'})) as acquire:
   c.scan(self.cfg,self.fetch);self.assertEqual(acquire.call_count,1)
   second=c.records()[h2['hold_id']];self.assertEqual(second['attempts'],0);self.assertLess(second['next_attempt']-c.time.time(),61)
   self.due();c.scan(self.cfg,self.fetch);self.assertEqual(acquire.call_count,2)
 def test_one_bad_item_cannot_erase_success(self):
  h2={**self.h,'hold_id':'b'*24}
  self.message['resources']=[{'resourceId':'@r','resourceIdType':'mediaId','resourceType':'image'}]
  with patch.object(video_hold,'rows',return_value=[self.h,h2]),patch.object(intake,'require_current',side_effect=[None,RuntimeError('one bad')]):c.scan(self.cfg,self.fetch)
  self.assertEqual(c.records()[self.h['hold_id']]['state'],'identified');self.assertEqual(c.records()[h2['hold_id']]['state'],'retry')
 def test_mislabeled_video_stops_before_content_analysis(self):
  with patch.object(sources,'acquire',side_effect=self.fake_acquire),patch.object(intake,'decoded_image',side_effect=intake.IntakeError('image_format_unrecognized')),patch.object(worker,'automatic_kind',return_value='video'),patch.object(worker,'analyze') as analyze:
   kind,_=intake.acquire_image(self.h,self.info,self.cfg)
  self.assertEqual(kind,'video');analyze.assert_not_called()
 def test_fake_png_cannot_pass_decoder(self):
  p=self.root/'fake.png';p.write_bytes(b'\x89PNG\r\n\x1a\nnot image')
  with self.assertRaises(intake.IntakeError):intake.decoded_image(self.cfg,p,self.root/'proof')
 def gif(self,frames=3):
  p=self.root/'test.gif';images=[Image.new('RGB',(3,3),(i%255,20,40)) for i in range(frames)];images[0].save(p,save_all=True,append_images=images[1:],duration=30,loop=0);return p
 def test_gif_all_frames_are_decoded_and_read(self):
  p=self.gif();work=self.root/'imagework';proof=intake.decoded_image(self.cfg,p,work/'image-proof');seen=[]
  def model(cfg,d,payload,images):
   seen.extend(images);return {'summary':'fixture','business_value':False,'business_reason':'fixture','key_points':[],'uncertainties':[],'needs_closer_review':[]}
  result=worker.analyze_image(self.cfg,p,work,{'source_sha256':worker.sha(p)},proof,model=model)
  self.assertEqual(proof['frame_coverage']['frame_count'],3);self.assertEqual(len(seen),3);self.assertEqual(result['frame_coverage'],proof['frame_coverage'])
 def test_exact_32_frame_boundary(self):
  p=self.gif(32);proof=intake.decoded_image(self.cfg,p,self.root/'proof');self.assertEqual(proof['frame_coverage']['frame_count'],32)
 def test_missing_gif_terminator_rejected(self):
  p=self.gif();p.write_bytes(p.read_bytes()[:-1])
  with self.assertRaisesRegex(intake.IntakeError,'image_decode_failed'):intake.decoded_image(self.cfg,p,self.root/'proof')
 def test_frame_tampering_during_model_call_rejected(self):
  p=self.gif();work=self.root/'work';proof=intake.decoded_image(self.cfg,p,work/'image-proof')
  def tamper(cfg,d,payload,images):images[0].write_bytes(b'corrupted');return {}
  with self.assertRaisesRegex(RuntimeError,'画面已变化'):worker.analyze_image(self.cfg,p,work,{'source_sha256':worker.sha(p)},proof,model=tamper)
 def test_cumulative_pixel_budget_checked_per_page(self):
  import image_frame_coverage as frames
  p=self.root/'unequal.tiff';Image.new('RGB',(1,1)).save(p,save_all=True,append_images=[Image.new('RGB',(4,4))]);directory=self.root/'proof';directory.mkdir()
  with patch.object(frames,'MAX_TOTAL_PIXELS',10):
   with self.assertRaisesRegex(ValueError,'image_frame_budget'):frames.decode(p,directory)
 def test_multiframe_over_limit_not_partial_success(self):
  p=self.gif(33)
  with self.assertRaisesRegex(intake.IntakeError,'image_frame_budget'):intake.decoded_image(self.cfg,p,self.root/'proof')
 def test_full_decode_detects_corrupt_later_frame(self):
  p=self.gif();p.write_bytes(p.read_bytes()[:len(p.read_bytes())//2])
  with self.assertRaises(intake.IntakeError):intake.decoded_image(self.cfg,p,self.root/'proof')
 def test_error_history_is_kept(self):
  self.message['content']='unknown';c.scan(self.cfg,self.fetch);self.due();c.scan(self.cfg,self.fetch)
  receipts=list((self.root/'receipts/media-classification'/self.h['hold_id']).glob('*.json'));self.assertEqual(len(receipts),2)
  self.assertTrue(any(json.loads(p.read_text())['previous'].get('attempts')==1 for p in receipts))
 def test_investigation_not_user_choice(self):
  v={};actions.route_value(v,[{'state':'pending','needs_confirmation':False,'repair_phase':'investigating','display_name':'fixture','current_blocker':'image_decode_failed'}]);self.assertEqual(v['attention_state'],'automatic');self.assertEqual(v['reason_code'],'media_type_investigating')
 def test_download_denial_is_classified_without_leaking_output(self):
  info={**self.info,'args':{'type':'mediaId','resource-id':'@r'}}
  r=subprocess.CompletedProcess([],1,b'{"status":403,"url":"secret-signed-url"}',b'')
  with patch.object(sources,'resolve',return_value=info),patch.object(sources.subprocess,'run',return_value=r):
   with self.assertRaises(sources.SourceError) as error:sources.acquire(self.h,self.cfg,self.root/'download')
  self.assertEqual(error.exception.code,'media_download_denied');self.assertNotIn('secret',str(error.exception))

class NativeOwnership(unittest.TestCase):
 def test_old_process_cannot_claim_or_publish(self):
  import media_release
  with patch.object(media_release,'binding',return_value={}):
   with self.assertRaises(media_release.ReleaseChanged):media_release.check()
 def test_native_scheduler_skips_isolated_database_and_maintenance(self):
  import business_sync_supervisor as supervisor
  s=supervisor.Supervisor(supervisor.CONFIG_PATH,database=Path('/isolated'))
  with patch.object(c,'spawn') as spawn:s.schedule_media_recovery()
  spawn.assert_not_called()
  s.database=None
  with patch.object(supervisor,'maintenance_active',return_value=True),patch.object(c,'spawn') as spawn:s.schedule_media_recovery()
  spawn.assert_not_called()
 def test_native_scheduler_runs_without_ui(self):
  import business_sync_supervisor as supervisor
  s=supervisor.Supervisor(supervisor.CONFIG_PATH)
  with patch.object(supervisor,'maintenance_active',return_value=False),patch.object(c,'spawn') as spawn:s.schedule_media_recovery()
  spawn.assert_called_once_with(supervisor.CONFIG_PATH)

if __name__=='__main__':unittest.main()
