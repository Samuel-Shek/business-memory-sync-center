#!/usr/bin/env python3
"""Isolated end-to-end mail phase contracts. Never calls external services."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import io,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest import mock
from PIL import Image
import email_sync as s
import email_phases as p
import model_router
from selftest_email_sync import temporary_config

class Phases(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=temporary_config(self.root);self.config['email_sync']['phased_processing']=True
        self.policy={'self_sender_addresses':['fixture_4f3049e2@example.invalid'],'policy_id':'win11-direct-except-self-v1'}
        self.policy_patch=mock.patch.object(s.win11_attachments,'policy',return_value=self.policy);self.policy_patch.start()
        self.net=mock.patch.object(s,'run_dws',side_effect=AssertionError('unmocked network'));self.net.start()
        self.notify=mock.patch.object(s,'send_feishu_direct',side_effect=AssertionError('real notification forbidden'));self.notify.start()
        model_router.isolate_for_tests(self.root/'model-router')
        self.reconciliation=mock.patch.object(s.reconciliation,'open_claim_context',return_value={});self.reconciliation.start()
        s.initialize(self.config);self.seed('mail')
    def tearDown(self):
        self.reconciliation.stop();self.notify.stop();self.net.stop();self.policy_patch.stop();self.tmp.cleanup()
    def seed(self,key):
        with s.connect_database(self.config) as c:
            c.execute("INSERT OR IGNORE INTO batches(batch_id,mode,discovered_count,created_at) VALUES('b','incremental',1,?)",(s.now_iso(),))
            c.execute("INSERT INTO messages(message_key,mailbox,message_id,folder_id,direction,subject_hash,first_batch_id,discovered_at,updated_at) VALUES(?,?,?,'2','incoming','hash','b',?,?)",(key,'fixture_4f3049e2@example.invalid',key,s.now_iso(),s.now_iso()))
            c.execute("INSERT INTO batch_messages(batch_id,message_key) VALUES('b',?)",(key,))
    def row(self,key='mail'):
        with s.connect_database_readonly(self.config) as c:return c.execute('SELECT * FROM messages WHERE message_key=?',(key,)).fetchone()
    def image(self,fmt='PNG'):
        stream=io.BytesIO();Image.new('RGB',(32,24),'white').save(stream,format=fmt);return stream.getvalue()
    def archive(self,items,key='mail',sender='fixture_764fc547@example.invalid'):
        message={'id':key,'markdownBody':'正文先入库','subject':'test','from':sender,'bodyHasMore':False,'bodyTotalLength':len('正文先入库')}
        with mock.patch.object(s,'run_dws',side_effect=[{'message':message},{'attachments':items}]),mock.patch.object(s,'manual_archive_hashes',return_value=set()),mock.patch.object(s.win11_attachments,'fetch',side_effect=AssertionError('archive must not wait Windows')):
            result=s.archive_one(self.config,self.row(key));s.mark_archive_success(self.config,self.row(key),result)
        p.reconcile(self.config);return self.row(key)
    def complete_image(self,key='mail',fmt='PNG'):
        job=p.claim(self.config,'image');data=self.image(fmt)
        def download(config,args,**kwargs):
            output=Path(args[args.index('--output')+1])/args[args.index('--name')+1];output.write_bytes(data)
        v={'text':'HELLO','description':'white image with fixture text','complete':True}
        rr=model_router.RouteResult(text=json.dumps(v),provider='fixture',model='fixture',seconds=0,value=v)
        with mock.patch.object(s,'run_dws',side_effect=download),mock.patch.object(model_router,'complete',return_value=rr):
            proof,result=p.run_job(self.config,job)
        p.finish(self.config,job,proof,result);return proof,result
    def test_gif_all_frames_verified_and_tamper_rejected(self):
        stream=io.BytesIO();Image.new('RGB',(20,20),'red').save(stream,format='GIF',save_all=True,append_images=[Image.new('RGB',(20,20),'blue')],duration=100,loop=0)
        data=stream.getvalue();self.archive([{'id':'gif','name':'frames.gif','size':len(data)}]);job=p.claim(self.config,'image')
        def download(config,args,**kwargs):
            (Path(args[args.index('--output')+1])/args[args.index('--name')+1]).write_bytes(data)
        v={'text':'','description':'two colored frames','complete':True}
        rr=model_router.RouteResult(text=json.dumps(v),provider='fixture',model='fixture',seconds=0,value=v)
        with mock.patch.object(s,'run_dws',side_effect=download),mock.patch.object(model_router,'complete',return_value=rr) as call:
            proof,result=p.run_job(self.config,job)
            self.assertEqual(len(call.call_args.kwargs['images']),2)
        p.finish(self.config,job,proof,result)
        with s.connect_database_readonly(self.config) as db:ready=dict(db.execute('SELECT * FROM attachment_jobs WHERE job_key=?',(job['job_key'],)).fetchone())
        self.assertTrue(p.verified_result(self.config,ready)['frame_coverage']['complete'])
        target=Path(ready['result_path']);raw=json.loads(target.read_text());raw['frame_coverage']['frames'].pop();target.write_text(json.dumps(raw));ready['result_sha256']=s.sha256_file(target)
        with self.assertRaises(s.EmailSyncError):p.verified_result(self.config,ready)

    def test_model_failure_keeps_provider_reason_and_retry_budget(self):
        data=self.image();self.archive([{'id':'photo','name':'photo.png','size':len(data)}]);job=p.claim(self.config,'image')
        failure=model_router.RouterExhausted('quota',[{'provider':'deepseek','category':'quota','cycle':1},{'provider':'kimi','category':'invalid_output','rejection_code':'ValueError','cycle':1}])
        p.finish(self.config,job,error=failure)
        with s.connect_database_readonly(self.config) as c:
            row=dict(c.execute('SELECT * FROM attachment_jobs WHERE job_key=?',(job['job_key'],)).fetchone())
            recovery=dict(c.execute('SELECT * FROM mail_recovery_tasks WHERE object_key=?',(job['job_key'],)).fetchone())
        self.assertEqual(row['state'],'retry');self.assertEqual(row['last_error_code'],'model_quota_unavailable');self.assertEqual(recovery['attempt_count'],1)
        self.assertEqual(row['attempts'],1)

    def fixture_release(self):
        import shutil,mail_auto_recovery as recovery
        bridge=self.root/'bridge';(bridge/'state/sync-autonomy').mkdir(parents=True);(bridge/'bin').mkdir()
        names=('email_sync.py','email_phases.py','sync_repair_rules.py','image_frame_coverage.py','mail_auto_recovery.py','mail_attachment_reader.py','mail_attachment_worker.ps1')
        for name in names:shutil.copy2(Path(s.__file__).parent/name,bridge/'bin'/name)
        log=bridge/'test.log';log.write_text('synthetic tests')
        release={'policy':'sync-center-owner-approved-20260929-v1','tests_passed':True,'release_id':'fixture','code_binding':{n:s.sha256_file(bridge/'bin'/n) for n in names},'tests':[{'path':str(log),'sha256':s.sha256_file(log)}]}
        (bridge/'state/sync-autonomy/release.json').write_text(json.dumps(release));return bridge
    def test_verified_gif_prompt_fix_grants_once_without_reset(self):
        import mail_auto_recovery as recovery
        data=self.image('GIF');self.archive([{'id':'gif','name':'icon.gif','size':len(data)}]);job=p.claim(self.config,'image')
        directory=p.root(self.config)/job['job_key']/job['lease_token'];directory.mkdir(parents=True)
        (directory/'original.image').write_bytes(data)
        item=json.loads(job['item_json']);identity=p.image_source_identity(self.row(),item)
        (directory/'download.json').write_text(json.dumps({'source_identity_sha256':identity,'source_bytes':len(data),'source_sha256':s.sha256_bytes(data) if hasattr(s,'sha256_bytes') else __import__('hashlib').sha256(data).hexdigest()}))
        p.finish(self.config,job,error='RouterExhausted');bridge=self.fixture_release()
        with mock.patch.object(s,'BRIDGE_ROOT',bridge):
            self.assertEqual(recovery.release_repairs(self.config),1);self.assertEqual(recovery.release_repairs(self.config),0)
        with s.connect_database_readonly(self.config) as c:
            row=dict(c.execute('SELECT * FROM attachment_jobs WHERE job_key=?',(job['job_key'],)).fetchone())
            task=dict(c.execute('SELECT * FROM mail_recovery_tasks WHERE object_key=?',(job['job_key'],)).fetchone())
        self.assertEqual(row['state'],'retry');self.assertEqual(row['attempts'],1);self.assertEqual(task['attempt_count'],1)
    def test_legacy_incomplete_job_enters_recovery_before_grant(self):
        import mail_auto_recovery as recovery
        self.archive([{'id':'sheet','name':'book.xlsx','size':123}]);job=p.claim(self.config,'win11')
        directory=p.root(self.config)/job['job_key']/job['lease_token'];directory.mkdir(parents=True)
        proof=directory/'result.json';proof.write_text('{}')
        (directory/'win11.json').write_text(json.dumps({'coverage':{'nodes':[{'status':'partial'},{'status':'deferred_media','name':'oleObject.bin'}]}}))
        with s.connect_database(self.config) as c:c.execute("UPDATE attachment_jobs SET state='needs_review',last_error_code='incomplete_content',result_path=? WHERE job_key=?",(str(proof),job['job_key']))
        self.config['email_sync'].setdefault('attachment_processing',{})['worker_script_sha256']='new';bridge=self.fixture_release()
        with mock.patch.object(s,'BRIDGE_ROOT',bridge),mock.patch.object(p,'verified_result',return_value={'worker_sha256':'old'}):
            self.assertEqual(recovery.release_repairs(self.config),1);self.assertEqual(recovery.release_repairs(self.config),0)
        with s.connect_database_readonly(self.config) as c:
            row=dict(c.execute('SELECT * FROM attachment_jobs WHERE job_key=?',(job['job_key'],)).fetchone())
            task=dict(c.execute('SELECT * FROM mail_recovery_tasks WHERE object_key=?',(job['job_key'],)).fetchone())
        self.assertEqual(row['state'],'retry');self.assertEqual(row['attempts'],1);self.assertEqual(task['attempt_count'],0)

    def test_historical_image_does_not_downgrade_other_attachment(self):
        data=self.image();row=self.archive([{'id':'photo','name':'photo.png','size':len(data)},{'id':'doc','name':'doc.pdf','size':20}])
        manifest_path=Path(row['archive_path'])/'manifest.json';manifest=json.loads(manifest_path.read_text());manifest['schema_version']=2
        manifest['attachments'][1].update(extract_status='extracted',risk='ordinary',sha256='f'*64)
        manifest_path.write_text(json.dumps(manifest))
        with s.connect_database(self.config) as c:
            c.execute("DELETE FROM attachment_jobs WHERE kind='win11'")
            c.execute("UPDATE attachments SET status='verified',extract_status='extracted',sha256=? WHERE attachment_id=?",('f'*64,s.sha256_text('doc')[:16]))
        self.complete_image();p.reconcile(self.config)
        with s.connect_database_readonly(self.config) as c:
            self.assertEqual(('verified','extracted'),tuple(c.execute("SELECT status,extract_status FROM attachments WHERE attachment_id=?",(s.sha256_text('doc')[:16],)).fetchone()))
    def test_body_count_survives_late_supplement(self):
        row=self.archive([])
        with s.connect_database(self.config) as c:c.execute("UPDATE messages SET memory_state='pending',body_memory_applied_at=?",(s.now_iso(),))
        with s.connect_database_readonly(self.config) as c:self.assertEqual(1,p.counts(c)['body_applied'])
    def test_body_survives_offline_large_attachment(self):
        row=self.archive([{'id':'large','name':'large.pdf','size':20*1024*1024}])
        self.assertEqual('archived',row['archive_state']);self.assertTrue(s.verify_archive(Path(row['archive_path']),'mail')[0])
        self.assertEqual('pending',p.job_rows(self.config,'mail')[0]['state'])
    def test_self_sender_still_excluded(self):
        row=self.archive([{'id':'photo','name':'photo.png','size':1}],sender='fixture_4f3049e2@example.invalid')
        self.assertEqual([],p.job_rows(self.config,'mail'));self.assertTrue(s.verify_archive(Path(row['archive_path']),'mail')[0])
    def test_inline_image_and_late_result_changes_semantic_key(self):
        data=self.image();row=self.archive([{'id':'photo','name':'photo.png','size':len(data),'isInline':True}])
        before=s.semantic_key_for_rows('group',[row]);proof,r=self.complete_image()
        with self.assertRaises(s.EmailSyncError):p.assert_current(self.config,[row])
        self.assertEqual(1,p.reconcile(self.config));after=self.row()
        self.assertNotEqual(before,s.semantic_key_for_rows('group',[after]));self.assertEqual(0,p.reconcile(self.config))
        with mock.patch.object(s.memory,'model_memory_context',return_value=[]):doc,h=s.build_semantic_document(self.config,[after],'b')
        self.assertIn('HELLO',doc['messages'][0]['attachments'][0]['extracted_text'])
        self.assertTrue(doc['messages'][0]['attachments'][0]['processing_complete'])
        with s.connect_database_readonly(self.config) as c:self.assertEqual(1,p.counts(c)['awaiting_supplement'])
    def test_avif_decodes_actual_bytes(self):
        data=self.image('AVIF');self.archive([{'id':'avif','name':'image.avif','size':len(data)}])
        proof,result=self.complete_image(fmt='AVIF');self.assertEqual('AVIF',result['actual_format']);self.assertTrue(p.verified_result(self.config,proof)['complete'])
    def test_corrupt_image_does_not_mark_complete(self):
        self.archive([{'id':'bad','name':'image.png','size':4}]);job=p.claim(self.config,'image')
        def download(config,args,**kw):(Path(args[args.index('--output')+1])/args[args.index('--name')+1]).write_bytes(b'nope')
        with mock.patch.object(s,'run_dws',side_effect=download):
            with self.assertRaises(s.EmailSyncError) as cm:p.run_job(self.config,job)
        p.finish(self.config,job,error=cm.exception.code);self.assertEqual('needs_review',p.job_rows(self.config,'mail')[0]['state'])
        self.assertEqual('archived',self.row()['archive_state'])
    def test_attachment_power_gate_is_per_file(self):
        self.archive([{'id':'large','name':'large.pdf','size':20*1024*1024}]);job=p.claim(self.config,'win11')
        with mock.patch.object(s.win11_attachments,'example_bd50c44d55d3',return_value=False),mock.patch.object(s.win11_attachments,'fetch') as fetch:
            with self.assertRaises(s.EmailSyncError) as cm:p.run_job(self.config,job)
        self.assertEqual('waiting_win11_power',cm.exception.code);fetch.assert_not_called()
    def test_small_attachment_ignores_windows_power(self):
        self.archive([{'id':'small','name':'small.pdf','size':123}]);job=p.claim(self.config,'win11')
        with mock.patch.object(s.win11_attachments,'example_bd50c44d55d3',side_effect=AssertionError('small attachment power queried')),mock.patch.object(s.win11_attachments,'fetch',side_effect=s.EmailSyncError('fixture_reached','fixture')):
            with self.assertRaises(s.EmailSyncError) as cm:p.run_job(self.config,job)
        self.assertEqual('fixture_reached',cm.exception.code)
    def test_unknown_size_waits_for_power(self):
        self.archive([{'id':'unknown','name':'unknown.pdf'}]);job=p.claim(self.config,'win11')
        with mock.patch.object(s.win11_attachments,'example_bd50c44d55d3',return_value=False):
            with self.assertRaises(s.EmailSyncError) as cm:p.run_job(self.config,job)
        self.assertEqual('waiting_win11_power',cm.exception.code)
    def test_oversize_is_visible_without_download(self):
        self.archive([{'id':'huge','name':'huge.zip','size':p.LIMIT+1}]);self.assertIsNone(p.claim(self.config,'win11'))
        self.assertEqual('attachment_size_limit',p.job_rows(self.config,'mail')[0]['last_error_code'])
    def test_video_is_held(self):
        self.archive([{'id':'video','name':'v.mp4','size':200}]);j=p.job_rows(self.config,'mail')[0]
        self.assertEqual('hold',j['kind']);self.assertEqual('waiting_instruction',j['last_error_code'])
    def test_result_tamper_rejected(self):
        self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}]);proof,r=self.complete_image()
        (Path(proof['result_path']).parent/'text.txt').write_text('tampered')
        with self.assertRaises(s.EmailSyncError):p.verified_result(self.config,proof)
    def test_duplicate_registration_is_noop(self):
        row=self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}]);p.enqueue_archived(self.config,row);p.enqueue_archived(self.config,row)
        self.assertEqual(1,len(p.job_rows(self.config,'mail')))
    def test_stale_worker_cannot_publish(self):
        self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}]);job=p.claim(self.config,'image')
        with s.connect_database(self.config) as c:c.execute("UPDATE attachment_jobs SET lease_token='new-owner'")
        p.finish(self.config,job,error='old-worker');self.assertEqual('running',p.job_rows(self.config,'mail')[0]['state'])
    def test_migration_idempotent(self):
        s.initialize(self.config);s.initialize(self.config)
        with s.connect_database_readonly(self.config) as c:self.assertEqual('ok',c.execute('PRAGMA integrity_check').fetchone()[0])
    def test_ignore_preserves_results_excludes_input_and_only_stops_selected_mail(self):
        import uuid
        self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}])
        proof,result=self.complete_image();before=p.fingerprint(self.config,self.row())
        self.seed('second');self.archive([{'id':'other','name':'other.png','size':len(self.image())}],key='second')
        ids=[j['attachment_id_hash'] for j in p.job_rows(self.config,'mail')]
        action=str(uuid.uuid4());receipt=p.ignore_attachments(self.config,'mail',ids,action_id=action,note='ignore this mail')
        self.assertEqual(receipt,p.ignore_attachments(self.config,'mail',ids,action_id=action,note='ignore this mail'))
        self.assertTrue(Path(proof['result_path']).is_file())
        self.assertNotEqual(before,p.fingerprint(self.config,self.row()))
        with mock.patch.object(p,'verified_result',side_effect=AssertionError('ignored content read')),mock.patch.object(s.win11_attachments,'full_read_receipt',side_effect=AssertionError('ignored fallback read')),mock.patch.object(s.memory,'model_memory_context',return_value=[]):
            document,_=s.build_semantic_document(self.config,[self.row()],'test')
        attachment=document['messages'][0]['attachments'][0]
        self.assertEqual('',attachment['extracted_text']);self.assertEqual('ignored_by_user',attachment['extract_status'])
        self.assertFalse(attachment['full_read_confirmed']);self.assertFalse(attachment['processing_complete'])
        self.assertEqual('second',p.claim(self.config,'image')['message_key'])
        p.reconcile(self.config)
        self.assertEqual('ignored',p.job_rows(self.config,'mail')[0]['state'])
        self.assertEqual(1,p.status_counts(self.config)['ignored'])
        self.assertEqual(1,p.status_counts(self.config)['pending'])
    def test_ignore_refuses_busy_or_changed_inventory(self):
        import uuid
        self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}])
        ids=[j['attachment_id_hash'] for j in p.job_rows(self.config,'mail')]
        with self.assertRaises(s.EmailSyncError):p.ignore_attachments(self.config,'mail',['wrong'],action_id=str(uuid.uuid4()),note='ignore')
        p.claim(self.config,'image')
        with self.assertRaises(s.EmailSyncError):p.ignore_attachments(self.config,'mail',ids,action_id=str(uuid.uuid4()),note='ignore')
        self.assertEqual('running',p.job_rows(self.config,'mail')[0]['state'])
    def test_partial_report_not_complete(self):
        self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}])
        with s.connect_database(self.config) as c:c.execute("UPDATE messages SET memory_state='applied'")
        stats=s.batch_stats(self.config,'b');self.assertFalse(stats['processing_complete']);self.assertIn('图片待处理 1',s.compose_report(stats))
    def test_png_reuse_retains_separate_mail_identity(self):
        data=self.image();self.archive([{'id':'photo','name':'photo.png','size':len(data)}]);self.complete_image()
        self.seed('second');self.archive([{'id':'same','name':'same.png','size':len(data)}],key='second')
        job=p.claim(self.config,'image')
        def download(config,args,**kw):(Path(args[args.index('--output')+1])/args[args.index('--name')+1]).write_bytes(data)
        with mock.patch.object(s,'run_dws',side_effect=download),mock.patch.object(model_router,'complete',side_effect=AssertionError('duplicate PNG model call')):
            proof,result=p.run_job(self.config,job)
        self.assertTrue(result['exact_image_reuse']);self.assertEqual('second',result['message_key'])
    def test_resume_completed_files_without_repeat_network(self):
        self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}]);proof,result=self.complete_image()
        with s.connect_database(self.config) as c:c.execute("UPDATE attachment_jobs SET state='retry',result_path=NULL,result_sha256=NULL")
        new=p.claim(self.config,'image');recovered,r=p.run_job(self.config,new)
        self.assertEqual(proof['result_sha256'],recovered['result_sha256'])
    def test_awaiting_supplement_only_completes_after_semantic_application(self):
        self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}]);self.complete_image();p.reconcile(self.config)
        row=self.row()
        with mock.patch.object(s.memory,'model_memory_context',return_value=[]):
            s.prepare_semantic_group(self.config,'b','group',[row])
            with s.connect_database_readonly(self.config) as c:group=c.execute('SELECT * FROM semantic_groups').fetchone()
            with mock.patch.object(s,'invoke_email_model',return_value={}),mock.patch.object(s,'validate_email_proposal',return_value={'no_business_change':True,'project_updates':[]}):
                s._process_semantic_group(self.config,group,semantic_id=group['semantic_id'],handoff_receipt={})
        self.assertEqual('complete',p.job_rows(self.config,'mail')[0]['state']);self.assertEqual('applied',self.row()['memory_state'])
        self.assertTrue(s.batch_stats(self.config,'b')['processing_complete'])
    def test_external_image_reference_remains_a_gap(self):
        self.assertEqual(1,p.image_reference_gaps('![x](https://example.invalid/image.png)',[]))
        self.assertEqual(0,p.image_reference_gaps('![x](cid:part)',[{'contentId':'<part>'}]))
        self.assertEqual(1,p.image_reference_gaps('![x](cid:missing)',[]))
    def test_unknown_owner_cannot_finish_someone_elses_job(self):
        self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}]);proof,result=self.complete_image()
        altered={**proof,'message_key':'nonexistent'}
        with self.assertRaises(s.EmailSyncError):p.verified_result(self.config,altered)
    def test_real_windows_receipt_contract(self):
        text='fixture text';source='a'*64;size=123;rid='job';identity='b'*64;worker='c'*64
        remote={'schema_version':2,'downloaded':True,'job_id':rid,'identity_sha256':identity,'worker_sha256':worker,'host':'EXAMPLE_USER',
            'original_received_on_m2':False,'source_sha256':source,'source_bytes':size,'foreground_input_used':False,'clipboard_used':False,
            'remote_path':'C:/Users/example/AppData/Local/BusinessMemory/mail-attachments/jobs/job/original.pdf',
            'reader_sha256':'d'*64,'local_view_sha256':source,'text':text,'text_sha256':s.sha256_text(text),'text_chars':len(text),'text_bytes':len(text),
            'extract_status':'extracted','risk':'ordinary','coverage':{'complete':True,'content_truncated':False,'nodes':[{'status':'complete','sha256':source,'bytes':size}],
                'text_chunks':[{'index':0,'chars':len(text),'sha256':s.sha256_text(text)}],'text_chunk_size':24000}}
        self.archive([{'id':'doc','name':'doc.pdf','size':size}]);job=p.claim(self.config,'win11')
        with mock.patch.object(s.win11_attachments,'fetch',return_value=remote):proof,result=p.run_job(self.config,job)
        self.assertTrue(p.verified_result(self.config,proof)['complete']);self.assertFalse(result['originals_on_m2'])

    def test_verified_partial_video_projects_as_waiting(self):
        import operational_workbench as work
        self.archive([{'id':'doc','name':'doc.xlsx','size':12}]);job=p.claim(self.config,'win11');directory=p.root(self.config)/job['job_key']/job['lease_token'];directory.mkdir(parents=True)
        (directory/'result.json').write_text('{}');(directory/'win11.json').write_text(json.dumps({'coverage':{'nodes':[{'name':'book.xlsx','status':'incomplete'},{'name':'book.xlsx / movie.mp4','status':'deferred_video'},{'name':'book.xlsx / sheet1','status':'complete'}]}}))
        with s.connect_database(self.config) as c:c.execute("UPDATE attachment_jobs SET state='needs_review',last_error_code='incomplete_content',result_path=? WHERE job_key=?",(str(directory/'result.json'),job['job_key']))
        result={'kind':'win11','complete':False,'text':'read sheets','created_at':s.now_iso()}
        with mock.patch.object(p,'verified_result',return_value=result),mock.patch.object(work,'_attachment_reading',return_value={'status':'partial_static'}):
            rows=work._mail(self.config)['attachments']
        self.assertTrue(rows[0]['reading']['video_waiting']);self.assertTrue(rows[0]['reading']['receipt_verified']);self.assertEqual(rows[0]['reading']['status'],'waiting_instruction')

    def test_feature_off_preserves_results(self):
        self.archive([{'id':'photo','name':'photo.png','size':len(self.image())}]);proof,r=self.complete_image();p.reconcile(self.config)
        self.config['email_sync']['phased_processing']=False
        self.assertEqual(0,p.start_workers(self.config));self.assertTrue(p.verify_all(self.config)['verified'])

if __name__=='__main__':unittest.main()
