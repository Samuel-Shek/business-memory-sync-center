"""Offline image failure and source reuse regression contracts."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock
import email_sync as s
import email_phases as p
import mail_auto_recovery as recovery
from selftest_email_phases import Phases

class DwsEvidence(unittest.TestCase):
    def test_numeric_exit_keeps_safe_reason_and_provider_retry_policy(self):
        payload={'error':{'code':5,'message':'connection reset https://private.invalid/?token=secret',
                          'retryable':True,'retry_after_seconds':7200}}
        proc=subprocess.CompletedProcess([],5,'',json.dumps(payload))
        with mock.patch.object(s,'dws_binary',return_value=Path('/fixture/dws')),mock.patch.object(s.subprocess,'run',return_value=proc):
            with self.assertRaises(s.EmailSyncError) as raised:s.run_dws({'email_sync':{'dws_timeout_seconds':1}},['mail','attachment','download'])
        detail=raised.exception.diagnostic
        self.assertEqual('connection_failure',detail['source_code'])
        self.assertTrue(detail['retryable']);self.assertEqual(7200,detail['retry_after_seconds'])
        self.assertNotIn('secret',json.dumps(detail));self.assertNotIn('private.invalid',json.dumps(detail))

class ImageRecovery(Phases):
    def test_phase_reading_and_legacy_projection_include_independent_mail_status(self):
        import operational_workbench as w
        import media_actions
        data=self.image();self.archive([{'id':'photo','name':'photo.png','size':len(data)}])
        self.complete_image();p.reconcile(self.config,message_key='mail')
        item=w._mail(self.config)['attachments'][0]
        self.assertEqual('邮件已归档；记忆处理未完成',item['reading']['mail_status'])
        # Old projection snapshots lacked this key; they must still render.
        item['reading'].pop('mail_status')
        row={'issue_id':'fixture','kind':'attachment','source_id':'dingtalk_mail','title':'test',
             'fingerprint':w.attachment_fingerprint(item),'status':'closed','attention_state':'closed',
             'resolution_kind':'recovered','first_seen':'1','last_seen':'2','resolved_at':'2','revision':1,
             'observation_json':json.dumps({'error_code':'5'}),'source_ref_json':'{}','evidence_json':'{}'}
        with mock.patch.object(media_actions,'decorate',side_effect=lambda value,*args:value):
            value=w._public(row,item,{'holds':[],'classifications':[],'decisions':[]})
        self.assertIn('记忆处理未完成',value['impact']);self.assertIn('附件已完成读取',value['impact'])

    def test_scoped_reconcile_does_not_touch_another_mail(self):
        self.archive([]);self.seed('other');self.archive([],key='other')
        with s.connect_database(self.config) as c:c.execute('UPDATE messages SET phase_fingerprint=NULL')
        self.assertEqual(1,p.reconcile(self.config,message_key='mail'))
        self.assertIsNotNone(self.row()['phase_fingerprint'])
        self.assertIsNone(self.row('other')['phase_fingerprint'])

    def test_download_failure_persists_reason_and_respects_cooldown(self):
        self.archive([{'id':'photo','name':'photo.png','size':100}]);job=p.claim(self.config,'image')
        error=s.EmailSyncError('5','provider error');error.diagnostic={'retryable':True,'retry_after_seconds':7200,'source_code':'connection_failure'}
        with mock.patch.object(s,'run_dws',side_effect=error):
            with self.assertRaises(s.EmailSyncError) as raised:p.run_job(self.config,job)
        p.finish(self.config,job,error=raised.exception)
        after=p.job_rows(self.config,'mail')[0]
        self.assertEqual('retry',after['state']);self.assertEqual('image_download_retryable',after['last_error_code'])
        self.assertGreaterEqual(recovery.parse(after['next_retry_at']),recovery.now()+timedelta(seconds=7195))
        detail=json.loads((p.root(self.config)/job['job_key']/job['lease_token']/'failure.json').read_text())
        self.assertEqual('connection_failure',detail['source_code'])
        again=recovery.observe(self.config,job['job_key'],'attachment',recovery.digest(job['item_json']),after['last_error_code'],encountered=True)
        self.assertEqual(after['next_retry_at'],again['next_due']);self.assertEqual(1,again['attempt_count'])

    def test_unknown_and_denied_never_become_automatic_retry(self):
        for flag,expected in [(None,'image_download_unclassified'),(False,'image_download_blocked')]:
            with self.subTest(flag=flag):
                directory=self.root/str(flag)/'attempt';directory.mkdir(parents=True)
                error=s.EmailSyncError('5','timeout');error.diagnostic={'retryable':flag,'source_code':'timeout'}
                with mock.patch.object(s,'run_dws',side_effect=error):
                    with self.assertRaises(s.EmailSyncError) as raised:p.prepare_image_source(self.config,{'mailbox':'x','message_id':'m'},{'id':'a','name':'x.png','size':10},directory)
                self.assertEqual(expected,raised.exception.code);self.assertEqual('maintenance',recovery.category(expected))

    def test_verified_original_is_reused_after_interrupted_understanding(self):
        data=self.image();row=self.archive([{'id':'photo','name':'photo.png','size':len(data)}]);job=p.claim(self.config,'image');item=json.loads(job['item_json'])
        before=p.root(self.config)/job['job_key']/'before';before.mkdir(parents=True)
        (before/'original.image').write_bytes(data)
        (before/'download.json').write_text(p.canonical({'source_identity_sha256':p.image_source_identity(row,item),'source_sha256':p.digest(data),'source_bytes':len(data)}))
        after=before.parent/'after';after.mkdir()
        with mock.patch.object(s,'run_dws',side_effect=AssertionError('must reuse verified bytes')):
            self.assertEqual(data,p.prepare_image_source(self.config,row,item,after).read_bytes())
        # Same name/size without matching bytes must never be reused.
        (before/'original.image').write_bytes(b'x'*len(data));(after/'original.image').write_bytes(b'x'*len(data))
        last=before.parent/'last';last.mkdir()
        with mock.patch.object(s,'run_dws',side_effect=s.EmailSyncError('fixture_download','rejected cache')) as fetch:
            with self.assertRaises(s.EmailSyncError):p.prepare_image_source(self.config,row,item,last)
        fetch.assert_called_once()

if __name__=='__main__':unittest.main()
