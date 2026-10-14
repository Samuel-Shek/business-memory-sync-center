#!/usr/bin/env python3
"""Offline-only recurrence, crash, cache coverage and accounting acceptance tests."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import copy
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import model_router as router
import model_call_ledger as ledger
import attachment_analysis as aa
import email_batched_input as batching

CONFIG={'model_routing':{'persistent_guard':True,'source_analysis_cache':True}}

def lock_probe(root,prompt,queue):
    try:
        with ledger.Admission(root,prompt,[],'test'):queue.put('bad')
    except ledger.Held as e:queue.put(e.category)

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        router.isolate_for_tests(self.root)
        self.calls=[]
        self.patch=patch.object(router,'_call_http',self.reply);self.patch.start()
        self.answer=router.ProviderReply('{"ok":true}','req-test',{'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':40,'prompt_cache_miss_tokens':60})
        self.cfg=copy.deepcopy(CONFIG)
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def reply(self,provider,*a,**k):
        self.calls.append(provider)
        if isinstance(self.answer,Exception):raise self.answer
        return self.answer
    def call(self,prompt='p',**kw):
        return router.complete(self.cfg,prompt=prompt,purpose='test',validate=kw.pop('validate',json.loads),**kw)
    def data(self):return ledger.report(self.root/'model-calls')
    def test_success_duplicate_usage_and_exports(self):
        x=self.call();y=self.call()
        self.assertEqual(self.calls,['deepseek']);self.assertTrue(y.cached);self.assertFalse(x.cached)
        r=self.data();self.assertEqual(len(r['calls']),1);self.assertEqual(r['operations'][0]['cache_hits'],1)
        self.assertEqual(json.loads(r['calls'][0]['usage_json'])['input_tokens'],100)
        self.assertIsNone(r['official_bill']);self.assertIn('input_tokens',ledger.export_csv(r))
        self.assertNotIn('"p"',json.dumps(r));self.assertEqual(r['by_hour_purpose'][0]['requests'],1)
    def test_full_failure_only_three_across_restarts_and_model_change(self):
        self.answer=router.ProviderReply('not-json','bad',{'prompt_tokens':17})
        for _ in range(4):
            with self.assertRaises(router.RouterExhausted):self.call()
            router.isolate_for_tests(self.root)
            self.cfg['model_routing']['text_chain']=['kimi:k3','deepseek:deepseek-flash']
        self.assertEqual(self.calls,['deepseek','kimi','deepseek'])
        self.assertEqual(len(self.data()['calls']),3)
        self.assertTrue(all(json.loads(r['usage_json'])['input_tokens']==17 for r in self.data()['calls']))
    def test_validator_repair_reuses_invalid_reply(self):
        self.answer=router.ProviderReply('1')
        def reject(x):raise ValueError('old checker')
        with self.assertRaises(router.RouterExhausted):self.call(validate=reject)
        count=len(self.calls);r=self.call(validate=int)
        self.assertEqual(r.value,1);self.assertTrue(r.cached);self.assertEqual(len(self.calls),count)
    def test_uncertain_never_retries_other_task_continues(self):
        self.answer=router._AttemptFailed('network','synthetic dropped response')
        for _ in range(3):
            with self.assertRaises(router.RouterExhausted) as e:self.call()
            self.assertEqual(e.exception.category,'request_uncertain')
        self.assertEqual(len(self.calls),1)
        self.answer=router.ProviderReply('{}');self.call('new data')
        self.assertEqual(len(self.calls),2)
    def test_crash_without_response_blocks(self):
        with ledger.Admission(self.root/'model-calls','p',[],'test',{'scope':'test'}) as g:g.begin(0,'deepseek','deepseek-flash')
        with self.assertRaises(router.RouterExhausted) as e:self.call()
        self.assertEqual(e.exception.category,'request_uncertain');self.assertEqual(self.calls,[])
    def test_crash_after_response_before_index_reconciles(self):
        with ledger.Admission(self.root/'model-calls','p',[],'test',{'scope':'test'}) as g:
            aid=g.begin(0,'deepseek','deepseek-flash')
            with patch.object(g,'_index_response',side_effect=RuntimeError('crash')):
                with self.assertRaises(RuntimeError):g.received(aid,self.answer)
        r=self.call();self.assertTrue(r.cached);self.assertEqual(self.calls,[])
        self.assertEqual(self.data()['events'][0]['kind'],'response_reconciled')
    def test_concurrent_same_operation_one_executor(self):
        root=self.root/'independent'
        ctx=multiprocessing.get_context('spawn');q=ctx.Queue()
        with ledger.Admission(root,'p',[],'test'):
            p=ctx.Process(target=lock_probe,args=(root,'p',q));p.start();p.join(10)
            self.assertEqual(p.exitcode,0);self.assertEqual(q.get(timeout=2),'duplicate_inflight')
    def test_corrupt_response_blocks_without_spending(self):
        self.call();p=next((self.root/'model-calls'/'responses').glob('*.json'));p.write_text('{}')
        with self.assertRaises(router.RouterExhausted) as e:self.call()
        self.assertEqual(e.exception.category,'guard_cache_corrupt');self.assertEqual(len(self.calls),1)
    def test_source_contract_conflict_holds(self):
        c={'scope':'source-one','source_version':'version1'};self.call(request_context=c)
        with self.assertRaises(router.RouterExhausted) as e:self.call('different',request_context=c)
        self.assertEqual(e.exception.category,'guard_source_conflict')
        self.call('different',request_context={**c,'source_version':'version2'});self.assertEqual(len(self.calls),2)
    def test_runtime_ids_do_not_reset_attempts_and_restore_schema(self):
        self.answer=router.ProviderReply('{"run_id":"__stable_run_id__"}')
        a='run-20260929-aaaaaaaa';b='run-20260929-bbbbbbbb'
        r=self.call('run '+a,request_context={'scope':'import','volatile_values':{'run_id':a}})
        r=self.call('run '+b,request_context={'scope':'import','volatile_values':{'run_id':b}})
        self.assertEqual(r.value['run_id'],b);self.assertIn(b,r.text);self.assertTrue(r.cached)
        self.assertEqual(len(self.calls),1)
    def test_local_validator_bug_stops_without_fallback_then_rechecks(self):
        def broken(text): raise TypeError('synthetic local bug')
        with self.assertRaises(router.RouterExhausted) as e:self.call(validate=broken)
        self.assertEqual(e.exception.category,'validation_error');self.assertEqual(self.calls,['deepseek'])
        r=self.call();self.assertTrue(r.cached);self.assertEqual(len(self.calls),1)

    def test_dingtalk_batch_generation_time_does_not_rearm(self):
        import dingtalk_memory_automation as memory
        for i in range(4):
            ident=f'batch-20260929-{i:08d}'
            doc={'batch_id':ident,'generated_at':f'2020-01-02T{i+10:02}:00:00+08:00','messages':[{'time':'2020-01-02T12:00:00+08:00','text':'unchanged'}]}
            model_doc=memory.model_batch_document(self.cfg,doc)
            self.assertNotIn('generated_at',model_doc)
            self.assertIn('generated_at',doc)
            self.call(json.dumps(model_doc),request_context={'scope':'dingtalk','volatile_values':{'batch_id':ident}})
        self.assertEqual(len(self.calls),1)

    def test_unknown_usage_kimi_subscription_and_cooldown(self):
        self.answer=router.ProviderReply('{}')
        router._mark('deepseek',ok=False,category='quota',detail='synthetic',cooldown=300)
        r=self.call();self.assertEqual(r.provider,'kimi');self.assertEqual(r.usage['status'],'unknown')
        row=self.data()['calls'][0];self.assertIsNone(row['cost_cny_estimate']);self.assertEqual(row['billing_mode'],'subscription_quota')
        self.assertEqual(self.data()['events'][0]['kind'],'cooling_down')
    def test_tariff_time_unknown_and_subscription(self):
        u={'cache_hit_tokens':1000000,'cache_miss_tokens':1000000,'output_tokens':1000000}
        t={'verified_days':{'2020-01-02':'workday','2020-01-02':'offday'}}
        self.assertAlmostEqual(ledger.estimate('deepseek','deepseek-flash',u,'2020-01-02T10:00:00+08:00',t)[0],10.04)
        self.assertAlmostEqual(ledger.estimate('deepseek','deepseek-flash',u,'2020-01-02T10:00:00+08:00',t)[0],5.02)
        self.assertIsNone(ledger.estimate('kimi','k3',u,'2020-01-02T10:00:00+08:00',t)[0])
        self.assertIsNone(ledger.estimate('deepseek','deepseek-flash',u,'2027-01-01T10:00:00+08:00',t)[0])
    def test_context_source_ownership_separated(self):
        self.call(request_context={'scope':'source1'});self.call(request_context={'scope':'source2'})
        self.assertEqual(len(self.calls),2)

class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name);self.calls=[]
    def tearDown(self):self.t.cleanup()
    def invoke(self,binding,chunk):
        self.calls.append(binding)
        return {'observations':[{'quote':chunk[:20],'observation':'Synthetic source observation'}],'uncertainties':['Only extracted text; no original media claim.']}
    def doc(self,lengths=(30000,)):
        return {'batch_id':'test-batch-20260929','current_memory':{'version':1},'messages':[{'source_token':'S001','attachments':[
            {'name':f'file-{i}','extracted_text':chr(65+i)*n,'full_read_confirmed':False} for i,n in enumerate(lengths)]}]}
    def prepare(self,d,scopes=['owner-message-1']):
        return aa.prepare(d,self.root/'run',self.root/'cache',scopes,self.invoke)
    def test_historical_four_rounds_42_to_12(self):
        base=self.doc((2663489,2666065));original=copy.deepcopy(base);old_calls=[]
        def old_invoke(ident,doc):old_calls.append(ident);return {'synthetic_proposal':True}
        def read(path,default):return json.loads(path.read_text()) if path.exists() else default
        for n in range(4):
            d=copy.deepcopy(base);d['current_memory']['version']=n;d['batch_id']=f'round-{n}'
            if n==0:d['messages'][0]['attachments']=d['messages'][0]['attachments'][:1]
            batching.prepare(d,self.root/'old-runs'/str(n),old_invoke,lambda raw,doc:raw,ledger.atomic,read,ledger.digest)
            result=self.prepare(d)
            self.assertEqual(result['current_memory']['version'],n)
            for a in result['messages'][0]['attachments']:
                self.assertFalse(a['full_read_confirmed'])
                self.assertFalse(json.loads(a['extracted_text'])['source_extraction_complete'])
        self.assertEqual(len(old_calls),42);self.assertEqual(len(self.calls),12);self.assertEqual(base,original)
        coverage=json.loads((self.root/'run'/'source-analysis-coverage.json').read_text())
        self.assertTrue(all(c['reused'] for f in coverage['files'] for c in f['chunks']))
        self.assertEqual(sum(f['chars'] for f in coverage['files']),5329554)
    def test_mutation_addition_owner_and_contract(self):
        d=self.doc();self.prepare(d);self.prepare(d);self.assertEqual(len(self.calls),1)
        d['messages'][0]['attachments'][0]['extracted_text']='Z'*30000;self.prepare(d);self.assertEqual(len(self.calls),2)
        d['messages'][0]['attachments'].append({'name':'added','extracted_text':'C'*30000});self.prepare(d);self.assertEqual(len(self.calls),3)
        self.prepare(d,['different-owner']);self.assertEqual(len(self.calls),5)
        with patch.object(aa,'CONTRACT','new-contract'):self.prepare(d)
        self.assertEqual(len(self.calls),7)
    def test_corrupt_and_unparsed_fail_closed(self):
        d=self.doc();self.prepare(d);p=next((self.root/'cache').glob('*/*.json'));p.write_text('{}')
        with self.assertRaises(batching.BatchInputError) as e:self.prepare(d)
        self.assertEqual(e.exception.code,'attachment_analysis_cache_corrupt');self.assertEqual(len(self.calls),1)
        with self.assertRaises(batching.BatchInputError):aa.validate({'observations':[{'quote':'not here','observation':'no'}],'uncertainties':[]},'source')
    def test_raw_source_change_and_size_boundary(self):
        d=self.doc();d['messages'][0]['attachments'][0]['source_text_sha256']='raw1';self.prepare(d)
        d['messages'][0]['attachments'][0]['source_text_sha256']='raw2';self.prepare(d);self.assertEqual(len(self.calls),2)
        with self.assertRaises(batching.BatchInputError) as e:self.prepare(self.doc((10000001,)))
        self.assertEqual(e.exception.code,'attachment_processing_approval_required')
    def test_all_fragments_validate_before_merge(self):
        d=self.doc((600000,))
        def bad(b,c):
            if b['start']:return {'observations':[], 'uncertainties':[]}
            return self.invoke(b,c)
        with self.assertRaises(batching.BatchInputError):aa.prepare(d,self.root/'run',self.root/'cache',['owner'],bad)
        self.assertEqual(json.loads((self.root/'run'/'source-analysis-coverage.json').read_text())['status'],'running')
        self.assertEqual(len(d['messages'][0]['attachments'][0]['extracted_text']),600000)

class CommitTests(unittest.TestCase):
    def test_new_metadata_preserves_existing_queued_source_hash(self):
        import email_sync as mail
        d={'batch_id':'batch','mailbox_scope':'test','source_contract':'test','messages':[{'source_token':'S001','attachments':[{'extracted_text':'x'}]}]}
        old=mail.semantic_input_sha(d)
        d['messages'][0]['attachments'][0]['source_text_sha256']='new evidence'
        self.assertEqual(old,mail.semantic_input_sha(d))
    def test_submission_rechecks_source_and_memory_under_lock(self):
        import email_sync as mail
        from unittest.mock import MagicMock
        c={'model_routing':{'source_analysis_cache':True}}
        proposal={'no_business_change':True}
        original={'source':'v1','memory':'v1'}
        for changed in ({'source':'v2','memory':'v1'},{'source':'v1','memory':'v2'}):
            with patch.object(mail.email_phases,'assert_current'), patch.object(mail.memory,'acquire_lock',return_value=MagicMock()), patch.object(mail,'maintenance_active',return_value=False), patch.object(mail,'semantic_rows',return_value=[]), patch.object(mail,'build_semantic_document',return_value=(changed,'unused')):
                with self.assertRaises(mail.EmailSyncError) as e:
                    mail.apply_email_proposal(c,'semantic','group','source',[],proposal,expected_context_sha=mail.decision_context_sha(original))
                self.assertEqual(e.exception.code,'model_context_changed')

class MediaMaintenanceTests(unittest.TestCase):
    def test_maintenance_pauses_worker_without_consuming_queue(self):
        import media_processor as worker
        import dingtalk_decision_center as center
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'config.json';config.write_text('{}')
            queued={'state':'queued','updated_at':'2020-01-02'}
            with patch.object(sys,'argv',['media_processor','--config',str(config)]), patch.object(worker,'RUNS',root/'runs'), patch.object(worker.actions,'decisions',return_value={'test':queued}), patch.object(worker.actions,'update') as update, patch.object(center,'maintenance_write_guard',side_effect=center.DecisionError('maintenance_active','maintenance',423)):
                worker.main();update.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
