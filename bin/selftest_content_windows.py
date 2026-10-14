
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import asyncio,copy,json,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from mail_attachment_reader import Reader
from content_identity import digest,key
from win11_mail_attachments import validate_result,AttachmentError,sender_is_self

class WindowsCases(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'jobs').mkdir()
    def tearDown(self):self.temp.cleanup()
    def runjob(self,message='message1',data=b'Hello',mode='exact_reuse',eligible=True):
        jobid=uuid.uuid4().hex;job=self.root/'jobs'/jobid;job.mkdir();p=job/'original.txt';p.write_bytes(data)
        ctx={'authorized':True,'domain':'test-owner','purpose':'business-memory','permission_epoch':'1'}
        source={'system':'email','mailbox':'fixture_df067515@example.invalid','message_id':message,'attachment_id':'1'}
        request={'mode':mode,'eligible':eligible,'context':ctx,'source':source,'event_id':message,'policy_sha256':'a'*64,'scope':'full machine text extraction and available OCR; not business verification','redaction':'none','exclusions':'self-v1'}
        d={'job_id':jobid,'identity_sha256':key(source),'worker_sha256':'b'*64,'source_sha256':digest(data),'source_bytes':len(data),'remote_path':str(p),'content_cache_request':request}
        (job/'download.json').write_text(json.dumps(d));return asyncio.run(Reader(job).run())
    def transport(self,r):
        r=copy.deepcopy(r);r['host']='EXAMPLE_USER';r['remote_path']='C:/Users/example/AppData/Local/BusinessMemory/mail-attachments/jobs/'+r['job_id']+'/original.txt';return r
    def validate(self,r):return validate_result(r,job_id=r['job_id'],identity=r['identity_sha256'],worker_hash=r['worker_sha256'],declared_size=r['source_bytes'],expected_host='EXAMPLE_USER')
    def test_fresh_task_receipt_cache_product_only(self):
        a=self.runjob();b=self.runjob('message2');self.assertFalse(a['content_cache']['hit']);self.assertTrue(b['content_cache']['hit']);self.assertEqual(a['text'],b['text']);self.assertNotEqual(a['job_id'],b['job_id']);self.assertNotEqual(a['identity_sha256'],b['identity_sha256']);self.validate(self.transport(b));self.assertFalse(b['original_received_on_m2'])
    def test_reject_old_job_receipt(self):
        a=self.transport(self.runjob());b=self.runjob('message2')
        with self.assertRaises(AttachmentError):validate_result(a,job_id=b['job_id'],identity=b['identity_sha256'],worker_hash=b['worker_sha256'],declared_size=b['source_bytes'],expected_host='EXAMPLE_USER')
    def test_raw_and_view_independent_validation(self):
        a=self.transport(self.runjob());a['local_view_sha256']='c'*64
        with self.assertRaises(AttachmentError):self.validate(a)
    def test_truncation_never_complete(self):
        a=self.transport(self.runjob(data=b'a'*35000));self.assertGreater(len(a['text']),30000);a['coverage']['content_truncated']=True
        with self.assertRaises(AttachmentError):self.validate(a)
    def test_self_sender_policy_unchanged(self):self.assertTrue(sender_is_self({'from':'fixture_df067515@example.invalid'},{'self_sender_addresses':['fixture_df067515@example.invalid']}))
    def test_ineligible_and_observation_no_reuse(self):
        self.runjob();self.assertFalse(self.runjob('message2',eligible=False)['content_cache']['hit']);self.assertFalse(self.runjob('message3',mode='observe')['content_cache']['hit'])
    def test_registry_failure_falls_back(self):
        with patch('mail_attachment_reader.Registry',side_effect=OSError('disk')):r=self.runjob()
        self.assertEqual(r['extract_status'],'extracted');self.assertEqual(r['content_cache']['reason'],'index_unavailable')
    def test_permission_failure_no_fallback(self):
        from content_identity import AccessDenied
        with patch('mail_attachment_reader.Registry.context',side_effect=AccessDenied('revoked')):
            with self.assertRaises(AccessDenied):self.runjob()
    def test_registry_access_denied_does_not_fall_back(self):
        from content_identity import AccessDenied
        for method in ['register','lookup','commit','receipt','acknowledge']:
            with self.subTest(method=method):
                with patch('mail_attachment_reader.Registry.'+method,side_effect=AccessDenied('revoked')):
                    with self.assertRaises(AccessDenied):self.runjob(message=method)
    def test_mac_access_denied_not_index_fault(self):
        import win11_mail_attachments as w
        from content_identity import AccessDenied
        with patch.object(w,'policy',return_value={}),patch.object(w.content_registry,'mail_begin',side_effect=AccessDenied('revoked')):
            with self.assertRaises(AttachmentError) as got:w.fetch({}, {}, {})
        self.assertEqual(got.exception.code,'content_source_access_denied')
    def test_mac_index_failure_keeps_security_context_disables_reuse(self):
        import content_registry as b
        pol={'version':'cross-source-v1','mode':'exact_reuse','owner_authorized':True,'domain':'owner','purpose':'business','permission_epoch':'1','exact_formats':['.png']}
        row={'mailbox':'fixture_df067515@example.invalid','message_id':'m','message_key':'k'};item={'id':'a','name':'image.png'}
        with patch.object(b,'policy',return_value=pol),patch.object(b,'registry',side_effect=OSError('disk')):h=b.mail_begin({},row,item)
        self.assertIsNone(h['event']);self.assertFalse(b.mail_request(h,'.png')['eligible']);self.assertTrue(h['context']['authorized'])
    def test_mac_policy_read_failure_is_not_index_fallback(self):
        import win11_mail_attachments as w
        with patch.object(w,'policy',return_value={}),patch.object(w.content_registry,'policy',side_effect=PermissionError('denied')):
            with self.assertRaises(AttachmentError) as got:w.fetch({}, {}, {})
        self.assertEqual(got.exception.code,'content_policy_unavailable')
    def test_windows_only_dispatch_original(self):
        s=Path(__file__).with_name('mail_attachment_dispatch.ps1').read_text();self.assertIn('$output=[IO.File]::Open($target',s);self.assertNotIn('scp',s.lower())
        transport=Path(__file__).with_name('win11_mail_attachments.py').read_text();self.assertIn("'--session-only'",transport);self.assertIn('content_registry.mail_finish',transport)
    def test_cache_contains_no_task_receipt(self):
        self.runjob();import sqlite3
        with sqlite3.connect(self.root/'content-cache/registry.sqlite') as d:p=json.loads(d.execute('SELECT product FROM artifacts').fetchone()[0])
        self.assertEqual(set(p),{'text','text_sha256','text_bytes','coverage'});self.assertNotIn('job_id',json.dumps(p));self.assertNotIn('mailbox',json.dumps(p))
if __name__=='__main__':unittest.main(verbosity=2)
