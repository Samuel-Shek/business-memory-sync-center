"""Offline recovery contracts; no live mail, models, or notifications."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock
import mail_auto_recovery as r
import email_sync as s
from selftest_email_sync import temporary_config
import selftest_email_sync as fixtures

class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.config=temporary_config(Path(self.tmp.name))
        fixtures.EmailSyncTests.seed_archive_message(self,self.config)
    def tearDown(self):self.tmp.cleanup()
    def row(self):
        with s.connect_database_readonly(self.config) as c:return c.execute('SELECT * FROM messages').fetchone()
    def test_categories_never_retry_denied_or_corrupt(self):
        for code in ['content_source_access_denied','content_source_policy_changed','read_safety_unconfirmed']:
            self.assertEqual('manual',r.category(code))
        self.assertEqual('maintenance',r.category('win11_text_hash_mismatch'))
        self.assertEqual('transient',r.category('content_source_probe_timeout'))
        self.assertEqual('probe_required',r.category('content_source_access_changed'))
    def test_probe_is_one_grant_not_success_and_count_survives_restart(self):
        t=r.observe(self.config,'mail','archive','version','content_source_access_changed',attempted=True)
        later=r.parse(t['next_due'])+timedelta(seconds=1)
        with mock.patch.object(r,'probe_source',return_value={'source_identity_verified':True}) as probe:
            self.assertTrue(r.grant_probe(self.config,t,self.row(),at=later))
            new=r.observe(self.config,'mail','archive','version','content_source_access_changed')
            self.assertFalse(r.grant_probe(self.config,new,self.row(),at=later));self.assertEqual(1,probe.call_count)
        self.assertEqual(1,new['attempt_count']);self.assertEqual(1,new['probe_count']);self.assertEqual('repairing',new['state'])
    def test_probe_denied_does_not_grant_or_reset(self):
        t=r.observe(self.config,'mail','archive','v','content_source_access_changed',attempted=True)
        with mock.patch.object(r,'probe_source',side_effect=s.EmailSyncError('source_access_denied','fixture')):
            self.assertFalse(r.grant_probe(self.config,t,self.row(),at=r.parse(t['next_due'])+timedelta(seconds=1)))
        self.assertEqual(1,r.summary(self.config)['states']['needs_user'])
    def test_budget_transitions_to_six_hour_probe(self):
        for _ in range(5):t=r.observe(self.config,'mail','archive','v','network_unavailable',attempted=True)
        self.assertEqual('waiting_probe',t['state']);self.assertEqual(5,t['attempt_count'])
        self.assertGreaterEqual((r.parse(t['next_due'])-r.now()).total_seconds(),21598)
        self.assertEqual(5,r.observe(self.config,'mail','archive','v','network_unavailable')['attempt_count'])
    def test_success_keeps_history_and_is_not_double_counted(self):
        r.observe(self.config,'mail','archive','v','network_unavailable',attempted=True)
        for _ in range(2):r.recovered(self.config,'mail','archive',{'archive_verified':True})
        self.assertEqual(2,r.summary(self.config)['actual_attempts'])
        self.assertEqual(1,r.summary(self.config)['states']['recovered'])
    def test_generation_does_not_reawaken_on_failure_reread(self):
        r.observe_generation(self.config)
        self.assertFalse(r.scheduling_hint(self.config)['changed'])
        with s.connect_database(self.config) as c:c.execute("UPDATE messages SET last_error_code='fixture',updated_at=?",(s.now_iso(),))
        self.assertFalse(r.scheduling_hint(self.config)['changed'])
        with s.connect_database(self.config) as c:c.execute("UPDATE messages SET body_sha256='new-body'")
        self.assertTrue(r.scheduling_hint(self.config)['changed'])
    def test_disable_changes_nothing(self):
        self.config['email_sync']['auto_recovery_enabled']=False
        self.assertIsNone(r.observe(self.config,'mail','archive','v','timeout',attempted=True))
        self.assertIsNone(r.scheduling_hint(self.config));self.assertEqual(set(),r.archive_grants(self.config,[self.row()]))
    def test_legacy_frozen_is_probed_not_silently_unfrozen(self):
        s.mark_archive_failure(self.config,self.row(),'content_source_access_changed','runtime')
        row=self.row();before=row['archive_failure_attempts']
        self.assertEqual(set(),r.archive_grants(self.config,[row]))
        t=r.observe(self.config,'mail','archive',s.archive_source_fingerprint(row),'content_source_access_changed')
        with mock.patch.object(r,'probe_source',return_value={'source_identity_verified':True}):
            self.assertEqual({'mail'},r.archive_grants(self.config,[row],at=r.parse(t['next_due'])+timedelta(seconds=1)))
        self.assertEqual(before,self.row()['archive_failure_attempts']);self.assertEqual('failed',self.row()['archive_state'])
    def test_probe_identity_mismatch(self):
        with mock.patch.object(s,'run_dws',return_value={'message':{'id':'other'}}):
            with self.assertRaises(s.EmailSyncError):r.probe_source(self.config,self.row())

    def test_error_changes_share_budget_and_success_counts_once(self):
        a=r.observe(self.config,'mail','archive','v','network_timeout',attempted=True)
        b=r.observe(self.config,'mail','archive','v','http_503',attempted=True)
        self.assertEqual(a['task_key'],b['task_key']);self.assertEqual(2,b['attempt_count'])
        r.recovered(self.config,'mail','archive',{'archive_verified':True})
        self.assertEqual(3,r.summary(self.config)['actual_attempts'])
    def test_waiting_advances_due_without_consuming_attempt(self):
        first=r.now();a=r.observe(self.config,'job','attachment','v','waiting_win11_power',at=first)
        b=r.observe(self.config,'job','attachment','v','waiting_win11_power',encountered=True,at=first+timedelta(hours=1))
        self.assertGreater(r.parse(b['next_due']),first+timedelta(hours=1))
        self.assertEqual(0,b['attempt_count']);self.assertEqual(0,r.summary(self.config)['actual_attempts'])
    def test_waiting_read_does_not_postpone_due(self):
        self.seed_group('waiting',status='failed',code='memory_lock_busy')
        a=r.observe(self.config,'waiting','semantic','input','memory_lock_busy',attempted=True,at=r.now()-timedelta(hours=1))
        b=r.observe(self.config,'waiting','semantic','input','memory_lock_busy')
        self.assertEqual(a['next_due'],b['next_due'])
        with mock.patch.object(s,'process_semantic_group',return_value={'writes':0}) as process:
            result=s.process_pending_semantics(self.config)
        self.assertEqual(1,result['attempted']);self.assertEqual(1,process.call_count)
    def test_blocked_semantics_and_scope_do_not_leave_overdue_hint(self):
        self.seed_group('blocked',status='failed',code='model_timeout')
        r.observe_generation(self.config)
        r.observe(self.config,'blocked','semantic','input','model_timeout',attempted=True,at=r.now()-timedelta(hours=1))
        result={'blocked_semantic_ids':['blocked'],'blocked_scopes':[{'group_key':'g','error_code':'semantic_orphan_sources_unavailable'}]}
        for _ in range(2):
            r.record_semantic_blocks(self.config,result);r.reconcile_ledger(self.config)
            self.assertIsNone(r.scheduling_hint(self.config)['next_due'])
        self.assertEqual(1,r.summary(self.config)['states']['blocked_input'])
        self.assertEqual(1,r.summary(self.config)['states']['needs_maintenance'])

    def seed_group(self,sid,status='pending',code=None,order='1'):
        with s.connect_database(self.config) as c:
            c.execute("INSERT INTO semantic_groups(semantic_id,semantic_key,group_key,source_ref,batch_id,input_sha256,status,last_error_code,created_at) VALUES(?,?,?,'ref','batch','input',?,?,?)",(sid,sid,sid,status,code,order))
    def test_excluded_old_group_does_not_starve_new_mail(self):
        self.seed_group('old',order='1');self.seed_group('new',order='2')
        with mock.patch.object(s,'process_semantic_group',return_value={'writes':0}) as process:
            result=s.process_pending_semantics(self.config,max_calls=1,exclude_ids=['old'])
        self.assertEqual('new',process.call_args.args[1]['semantic_id']);self.assertEqual(1,result['attempted'])
    def test_semantic_cooldown_prevents_model_call(self):
        self.seed_group('cooling',status='failed',code='model_timeout')
        r.observe(self.config,'cooling','semantic','input','model_timeout',attempted=True)
        with mock.patch.object(s,'process_semantic_group',side_effect=AssertionError('cooldown bypassed')):
            result=s.process_pending_semantics(self.config)
        self.assertEqual(0,result['attempted']);self.assertEqual(1,result['failed'])
    def test_review_job_retires_old_transient_due(self):
        with s.connect_database(self.config) as c:
            c.execute("INSERT INTO attachment_jobs(job_key,message_key,attachment_id_hash,item_json,kind,state,last_error_code,created_at,updated_at) VALUES('j','mail','h','{}','image','needs_review','incomplete','1','1')")
        r.observe_generation(self.config)
        r.observe(self.config,'j','attachment','v','model_timeout',attempted=True,at=r.now()-timedelta(hours=1))
        for _ in range(2):
            r.reconcile_ledger(self.config)
            self.assertIsNone(r.scheduling_hint(self.config)['next_due'])
        self.assertEqual(1,r.summary(self.config)['states']['needs_maintenance'])
    def test_archive_exhaustion_cannot_bypass_failed_probe(self):
        s.mark_archive_failure(self.config,self.row(),'dws_timeout','same')
        with s.connect_database(self.config) as c:
            c.execute("UPDATE messages SET archive_retry_exhausted_at='2000-01-01T00:00:00+00:00',archive_next_retry_at='2000-01-01T00:00:00+00:00'")
        r.observe(self.config,'mail','archive',s.archive_source_fingerprint(self.row()),'dws_timeout',attempted=True,at=r.now()-timedelta(hours=7))
        with mock.patch.object(r,'probe_source',side_effect=s.EmailSyncError('dws_timeout','fixture')),mock.patch.object(s,'archive_one') as archive:
            self.assertEqual(0,s.archive_pending(self.config)['attempted'])
            archive.assert_not_called()
    def test_enabled_archive_uses_shared_budget_then_probe(self):
        with mock.patch.object(s,'archive_one',side_effect=s.EmailSyncError('dws_timeout','fixture')) as archive:
            for i in range(5):
                if i:
                    with s.connect_database(self.config) as c:c.execute("UPDATE mail_recovery_tasks SET next_due='2000-01-01T00:00:00+00:00'")
                self.assertEqual(1,s.archive_pending(self.config)['attempted'])
            self.assertEqual(5,archive.call_count)
        self.assertEqual(1,r.summary(self.config)['states']['waiting_probe'])

    def test_archive_probe_round_is_bounded(self):
        rows=[]
        for i in range(8):
            row=dict(self.row());row.update(message_key='m'+str(i),archive_state='failed',last_error_code='content_source_access_changed',archive_retry_exhausted_at=s.now_iso(),archive_failure_class='deterministic')
            rows.append(row)
            r.observe(self.config,row['message_key'],'archive',s.archive_source_fingerprint(row),'content_source_access_changed',at=r.now()-timedelta(days=1))
        with mock.patch.object(r,'probe_source',return_value={'source_identity_verified':True}) as probe:
            self.assertEqual(3,len(r.archive_grants(self.config,rows)));self.assertEqual(3,probe.call_count)

if __name__=='__main__':unittest.main()
