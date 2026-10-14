"""Synthetic concurrency/crash tests: no network, accounts or production writes."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import sqlite3
import uuid
from pathlib import Path
from unittest import TestCase,main
from unittest.mock import patch
import dingtalk_memory_automation as m
import dingtalk_commit_guard as g
import dingtalk_prepared_recovery as r
import dingtalk_batch_contract as c
import business_page_integrity as i
from selftest_remaining_sync_repair import ChatFixture,Crash
from selftest_dingtalk_memory_automation import insert_message,make_note
from selftest_business_page_integrity import claim_block

class GuardTests(ChatFixture):
    def fixture(self):
        insert_message(self.config,'msg','conv','合成消息','2020-01-02 23:00:00')
        batch=m.prepare_batch(self.config);old=batch['batch_id'];d=self.paths['runs']/old
        proposal={'batch_id':old,'source_summary':None,'no_business_change':False,'claim_relations':[],
          'candidates':[{'action':'annotate_page','category':'internal_material','memory_kind':'status','dedupe_key':'fixture','title':'合成记录','summary':'合成说明','evidence_boundary':'仅测试','source_tokens':['S001'],'existing_memory_id':None,'conflict_reason':None}]}
        proposal=m.validate_proposal(self.config,old,proposal);c.save(d/'proposal.json',proposal)
        with m.connect_database(self.config) as db:db.execute("update semantic_batches set proposal_sha256=? where batch_id=?",(m.sha256_path(d/'proposal.json'),old))
        target=self.paths['memory_root'].parent/'公司资料/test.md';target.parent.mkdir(exist_ok=True)
        before=i.ensure_claim_base('Original prose.\n').rstrip()+'\n\n'+claim_block('BUSINESS-20260903-002')+'\n';target.write_text(before)
        planned=before+'\n'+claim_block('BUSINESS-20260903-003')+'\n'
        original=c.save
        def interrupt(path,value):
            original(path,value)
            if path.name=='commit-receipt.json' and value.get('phase')=='prepared':raise Crash()
        with patch.object(m,'build_memory_changes',return_value=({target:planned},['BUSINESS-20260903-003'],[])),patch.object(c,'save',side_effect=interrupt):
            with self.assertRaises(Crash):m.apply_proposal(self.config,batch,proposal)
        return batch,proposal,target,before,d
    def occupy(self):
        f=self.paths['memory_root']/'04_对话来源摘要/media.md';f.write_text(make_note('BUSINESS-20260903-003','合成媒体'));return f
    def test_id_collision_before_write_and_one_child_without_model(self):
        batch,p,target,before,d=self.fixture();self.occupy();old=batch['batch_id']
        frozen={name:(d/name).read_bytes() for name in ['commit-receipt.json','planned-writes.json','input-manifest.json','proposal.json','prewrite-evidence.json']}
        with patch.object(m,'apply_changes',wraps=m.apply_changes) as apply:
            with self.assertRaises(m.AutomationError) as exc:m.apply_proposal(self.config,batch,p)
            self.assertEqual(exc.exception.code,'proposal_commit_id_conflict');self.assertEqual(apply.call_count,0)
        new=before+'\n'+claim_block('BUSINESS-20260903-004')+'\n'
        with patch.object(m,'build_memory_changes',return_value=({target:new},['BUSINESS-20260903-004'],[])),patch.object(m,'invoke_codex',side_effect=AssertionError('model not allowed')):
            result=m.apply_proposal_with_recovery(self.config,batch,p)
        self.assertEqual(result['writes'],1);self.assertNotEqual(batch['batch_id'],old);self.assertEqual(self.queue_states(),['applied'])
        for name,data in frozen.items():self.assertEqual((d/name).read_bytes(),data)
        self.assertEqual(target.read_text(),new)
        self.assertNotIn('prepared', [v['status'] for v in r.prepare_pending(self.config)['batches'].values()])
        child_p=json.loads((self.paths['runs']/batch['batch_id']/'proposal.json').read_text())
        with patch.object(m,'apply_changes',side_effect=AssertionError('no duplicate writes')):
            m.apply_proposal(self.config,batch,child_p)
        import handoff_context
        detail={'source_id':'dingtalk_chat','source_label':'钉钉聊天','kind':'group','members':[],
                'status':'closed','first_seen':'2020-01-02T00:00:00+08:00'}
        evidence=handoff_context.operational(self.config,detail,self.root)['chat_recovery']
        self.assertTrue(evidence['candidates'][0]['recovery_verified'])
        self.assertNotIn('Original prose',json.dumps(evidence))
        self.assertNotIn(str(self.root),json.dumps(evidence))
    def test_existing_id_update_not_collision(self):
        batch,p,target,before,d=self.fixture()
        self.assertEqual(g.id_conflicts(self.config,{str(target):before.replace('Original','Updated')},{str(target):before}),[])
    def test_duplicate_identifier_inside_one_plan_blocks(self):
        batch,p,target,before,d=self.fixture()
        planned=before+'\n'+claim_block('BUSINESS-20260903-004')*2
        self.assertEqual(g.id_conflicts(self.config,{str(target):planned},{str(target):before}),['BUSINESS-20260903-004'])
    def test_readonly_recovery_diagnostics_reject_forged_completion(self):
        self.test_id_collision_before_write_and_one_child_without_model()
        with m.connect_database(self.config) as db:
            child=db.execute("select batch_id from semantic_batches where status='applied'").fetchone()[0]
        path=self.paths['runs']/child/'commit-receipt.json'
        value=json.loads(path.read_text());value['verification']={};c.save(path,value)
        self.assertFalse(g.recovery_evidence(self.config)['candidates'][0]['recovery_verified'])

    def test_pure_additive_prewrite_reconstruction(self):
        batch,p,target,before,d=self.fixture();target.write_text(before+'\n'+claim_block('BUSINESS-20260903-005')+'\n');m.release_batch_failure(self.config,batch['batch_id'],'proposal_commit_content_conflict')
        proof=r.inspect(self.config,batch['batch_id']);self.assertEqual(proof['changed_targets'],1)
    def test_prose_change_stops_and_records_reason(self):
        batch,p,target,before,d=self.fixture();target.write_text(i.prepare_base_update(before,before.replace('Original','Changed'),allow_base_change=True));self.occupy()
        with self.assertRaises(m.AutomationError):m.apply_proposal_with_recovery(self.config,batch,p)
        self.assertEqual(json.loads((d/'recovery-assessment.json').read_text())['recovery_reason'],'prepared_recovery_not_additive')
        self.assertEqual(self.queue_states(),['failed'])
    def test_missing_prewrite_proof_never_implies_no_writes(self):
        batch,p,target,before,d=self.fixture();self.occupy();(d/'prewrite-evidence.json').unlink()
        with self.assertRaises(m.AutomationError):m.apply_proposal_with_recovery(self.config,batch,p)
        self.assertEqual(json.loads((d/'recovery-assessment.json').read_text())['recovery_reason'],'prepared_recovery_prewrite_unverified')
    def test_corrupt_preimage_blocks(self):
        batch,p,target,before,d=self.fixture();self.occupy();next((d/'prewrite-backup').iterdir()).write_text('corrupt')
        with self.assertRaises(m.AutomationError):m.apply_proposal_with_recovery(self.config,batch,p)
        self.assertEqual(self.queue_states(),['failed'])
    def test_child_failure_keeps_child_owner_and_no_grandchild(self):
        batch,p,target,before,d=self.fixture();self.occupy();old=batch['batch_id']
        with patch.object(m,'build_memory_changes',side_effect=m.AutomationError('proposal_commit_content_conflict','synthetic')):
            with self.assertRaises(m.AutomationError):m.apply_proposal_with_recovery(self.config,batch,p)
        self.assertNotEqual(batch['batch_id'],old);m.release_batch_failure(self.config,batch['batch_id'],'proposal_commit_content_conflict')
        self.assertNotIn('prepared', [v['status'] for v in r.prepare_pending(self.config)['batches'].values()])
        with m.connect_database(self.config) as db:self.assertEqual(db.execute('select count(*) from semantic_batches').fetchone()[0],2)
    def test_orphan_changed_proposal_blocks(self):
        batch,p,target,before,d=self.fixture();self.occupy();old=batch['batch_id'];m.release_batch_failure(self.config,old,'proposal_commit_id_conflict')
        child=str(uuid.uuid5(uuid.NAMESPACE_URL,'business-prepared-recovery:'+old));dest=self.paths['runs']/child;dest.mkdir();(dest/'proposal.json').write_text('{}')
        with self.assertRaises(r.RecoveryBlocked):r.prepare(self.config,old)
        self.assertEqual((dest/'proposal.json').read_text(),'{}')

    def test_clean_orphan_resumes_without_another_child(self):
        batch,p,target,before,d=self.fixture();self.occupy();old=batch['batch_id']
        m.release_batch_failure(self.config,old,'proposal_commit_id_conflict')
        real=m.atomic_write
        def fail(path,text,*args,**kwargs):
            if path.name=='proposal.json':raise OSError('synthetic crash')
            return real(path,text,*args,**kwargs)
        with patch.object(m,'atomic_write',side_effect=fail):
            with self.assertRaises(OSError):r.prepare(self.config,old)
        with m.connect_database(self.config) as db:
            self.assertEqual(db.execute('select count(*) from semantic_batches').fetchone()[0],1)
        result=r.prepare(self.config,old)
        self.assertEqual(result['status'],'prepared')
        m.release_batch_failure(self.config,old,'late_parent_callback')
        with m.connect_database(self.config) as db:
            row=db.execute('select batch_id,state from semantic_queue').fetchone()
            self.assertEqual((row['batch_id'],row['state']),(result['child_batch_id'],'claimed'))

    def test_started_without_journal_is_never_unwritten(self):
        batch,p,target,before,d=self.fixture();self.occupy();g.mark_started(d)
        with self.assertRaises(m.AutomationError):m.apply_proposal_with_recovery(self.config,batch,p)
        self.assertEqual(json.loads((d/'recovery-assessment.json').read_text())['recovery_reason'],'prepared_recovery_prewrite_unverified')

    def test_source_drift_cannot_enter_fresh_model_queue(self):
        batch,p,target,before,d=self.fixture();self.occupy()
        m.release_batch_failure(self.config,batch['batch_id'],'proposal_commit_id_conflict')
        with m.connect_database(self.config) as db:
            db.execute("update messages set content_sha256='changed'")
        self.assertIsNone(m.claim_reusable_proposal(self.config))
        with m.connect_database(self.config) as db:
            selected,_=m.select_batch_rows(db,100,30000)
            self.assertEqual(selected,[])

    def test_failure_receipt_error_does_not_prevent_rollback(self):
        batch,p,target,before,d=self.fixture()
        self.validation.side_effect=m.AutomationError('validation_failed','synthetic')
        original=m.restore_from_journal
        with patch.object(g,'record_failure',return_value=False),patch.object(m,'restore_from_journal',wraps=original) as rollback:
            with self.assertRaises(m.AutomationError):m.apply_proposal(self.config,batch,p)
            self.assertEqual(rollback.call_count,1)
        self.assertEqual(target.read_text(),before)

    def test_late_parent_cannot_write_after_transfer(self):
        batch,p,target,before,d=self.fixture();self.occupy();old=batch['batch_id']
        m.release_batch_failure(self.config,old,'proposal_commit_id_conflict');r.prepare(self.config,old)
        with patch.object(m,'apply_changes',side_effect=AssertionError('stale parent wrote')):
            with self.assertRaises(m.AutomationError) as ex:m.apply_proposal(self.config,batch,p)
        self.assertEqual(ex.exception.code,'prepared_recovery_original_held')
        self.assertEqual(target.read_text(),before)

    def test_child_target_drift_blocks_before_building_another_plan(self):
        batch,p,target,before,d=self.fixture();self.occupy();old=batch['batch_id']
        m.release_batch_failure(self.config,old,'proposal_commit_id_conflict');result=r.prepare(self.config,old)
        child,cp=m.claim_reusable_proposal(self.config,recover=False,only_batch=result['child_batch_id'])
        target.write_text(before+'\n'+claim_block('BUSINESS-20260903-006')+'\n')
        with patch.object(m,'build_memory_changes',side_effect=AssertionError('must not refreeze')):
            with self.assertRaises(m.AutomationError) as ex:m.apply_proposal(self.config,child,cp)
        self.assertEqual(ex.exception.code,'proposal_commit_content_conflict')

class CauseTests(TestCase):
    def test_handoff_failure_fields_survive_privacy_depth_limit(self):
        import handoff_context,codex_handoff
        detail={'source_id':'dingtalk_chat','source_label':'钉钉聊天','kind':'group','members':[], 'status':'open'}
        projection={'candidates':[{'batch_id':'synthetic', 'failures':[{'error_code':'validation_failed','stage':'original_failure'},{'original_error_code':'validation_failed','rollback_error_code':'validation_context_unavailable','stage':'rollback_failure'}]}]}
        with patch.object(g,'recovery_evidence',return_value=projection):
            clean=codex_handoff.clean({'problem':handoff_context.operational({},detail,Path('/synthetic'))})
        candidate=clean['problem']['chat_recovery']['candidates'][0]
        self.assertEqual(candidate['first_failure']['error_code'],'validation_failed')
        self.assertEqual(candidate['latest_failure']['rollback_error_code'],'validation_context_unavailable')
        self.assertIn('failures',projection['candidates'][0])

    def test_exception_categories_and_no_private_text(self):
        import tempfile,validation_evidence as v
        with tempfile.TemporaryDirectory() as tmp:
            config={'memory_root':tmp};path=Path(tmp)/'config.json';path.write_text(json.dumps(config))
            for error,code,category in [(v.InputsChanged('private path'),'validation_inputs_changed','inputs_changed'),(PermissionError('private path'),'validation_permission_denied','permission_denied'),(sqlite3.OperationalError('private sql'),'validation_context_unavailable','database_read_failed'),(OSError('private file'),'validation_context_unavailable','source_context_unavailable')]:
                with patch.object(m,'CONFIG_PATH',path),patch.object(v,'context',side_effect=error),patch.object(m,'record_validation_failure') as receipt:
                    with self.assertRaises(m.AutomationError) as ex:m.assert_validation_access(config)
                    self.assertEqual(ex.exception.code,code);self.assertEqual(ex.exception.notification_detail['cause_category'],category)
                    self.assertNotIn('private',json.dumps(receipt.call_args.args))
if __name__=='__main__':main()
