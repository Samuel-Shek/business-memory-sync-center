"""Isolated parser, held-video and bounded reanalysis tests; no provider calls."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import asyncio
import base64
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch, AsyncMock
import zipfile

import attachment_analysis as aa
import email_sync as mail
import mail_attachment_reader as reader
import mail_auto_recovery as recovery
import model_call_ledger as ledger
import model_router as router
import video_hold
import win11_mail_attachments as transport


def package(extra=None, *, empty=False, broken=False):
    files={'[Content_Types].xml':'<Types/>',
           'xl/workbook.xml':'<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" r:id="rId1"/></sheets></workbook>',
           'xl/_rels/workbook.xml.rels':'<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
           'xl/worksheets/sheet1.xml':'<worksheet><sheetData>'+('' if empty else '<row><c r="A1" t="s"><v>0</v></c></row>')+'</sheetData></worksheet>',
           'xl/sharedStrings.xml':'<sst><si><t>未批准</t></si></sst>'}
    if broken:del files['xl/worksheets/sheet1.xml']
    files.update(extra or {})
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:
        for name,data in files.items():z.writestr(name,data)
    return output.getvalue()


class OfficeFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.reader=reader.Reader(self.tmp.name)

    def test_mislabeled_ooxml_uses_real_format_and_shared_strings(self):
        node=asyncio.run(self.reader.read(package(),'wrong.xls'))
        self.assertEqual(node['status'],'complete')
        self.assertEqual(node['detected_format'],'.xlsx')
        self.assertEqual(node['sheets_read'],1)
        self.assertIn('A1=未批准','\n'.join(self.reader.parts))

    def test_cfb_has_priority_over_appended_zip_drawing(self):
        data=bytes.fromhex('d0cf11e0a1b11ae1')+package()
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        obj=types.SimpleNamespace(listdir=lambda:[['Workbook']],exists=lambda s:s=='Workbook')
        context=types.SimpleNamespace(__enter__=lambda s:obj)
        class Ole:
            def __enter__(self):return obj
            def __exit__(self,*args):pass
        with patch.dict('sys.modules',{'olefile':types.SimpleNamespace(OleFileIO=lambda *_:Ole())}),patch.object(self.reader,'office_com',new_callable=AsyncMock) as com:
            node=asyncio.run(self.reader.read(data,'original.xls'))
            self.assertEqual(node['container_signature'],'cfb')
            self.assertEqual(com.call_args.args[2],'.xls')

    def test_unrecognized_zip_office_cannot_claim_zero_text_success(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:z.writestr('drs/downrev.xml','<drawing/>')
        node=asyncio.run(self.reader.read(b.getvalue(),'original.xls'))
        self.assertEqual(node['status'],'failed')
        self.assertEqual(node['error_code'],'office_container_not_recognized')

    def test_real_empty_workbook_has_structural_proof(self):
        node=asyncio.run(self.reader.read(package(empty=True),'empty.xlsx'))
        self.assertEqual(node['status'],'complete')
        self.assertTrue(node['verified_empty_workbook'])
        self.assertEqual(node['sheets_expected'],node['sheets_read'])
        self.assertEqual(node['nonempty_cells'],0)

    def test_missing_declared_sheet_is_not_empty(self):
        node=asyncio.run(self.reader.read(package(empty=True,broken=True),'broken.xlsx'))
        self.assertEqual(node['status'],'failed')

    def test_held_video_remains_unread_and_nonvideo_coverage_is_separate(self):
        node=asyncio.run(self.reader.read(package({'xl/media/clip.mp4':b'held payload'}),'video.xlsx'))
        self.assertEqual(node['status'],'incomplete')
        held=[n for n in self.reader.nodes if n['status']=='deferred_video']
        self.assertEqual(len(held),1);self.assertFalse(held[0]['content_read'])
        self.assertTrue(video_hold.coverage_summary({'coverage':{'nodes':self.reader.nodes}})['non_video_complete'])

    def test_payload_and_worker_hash_bindings_match_installed_reader(self):
        worker=Path(reader.__file__).with_name('mail_attachment_worker.ps1').read_text()
        payload=re.search(r"WriteAllBytes\(\$source,\[Convert\]::FromBase64String\('([^']+)'",worker)[1]
        source=Path(reader.__file__).read_bytes()
        self.assertEqual(base64.b64decode(payload),source)
        self.assertEqual(worker.count(reader.sha(source)),2)
        self.assertNotIn('KET.Application',source.decode())


class OutputBudgetTests(unittest.TestCase):
    def test_worst_allowed_result_fits_after_restoring_quotes(self):
        text='x'*500000;segments=aa.reference_segments(text)
        value={'observations':[{'source_span':[i,i],'observation':'中'*300} for i in range(1,13)],
               'uncertainties':['中'*300 for _ in range(8)]}
        result,_=aa.resolve_references(value,text,segments,budgeted=True)
        self.assertLess(len(ledger.canonical(result)),20000)

    def test_historical_output_guidance_does_not_invalidate_evidence(self):
        segments=aa.reference_segments('x'*3000)
        values=[{'observations':[{'source_span':[1,1],'observation':'x'}]*13,'uncertainties':[]},
                {'observations':[{'source_span':[1,2],'observation':'x'}],'uncertainties':[]},
                {'observations':[{'source_span':[1,1],'observation':'x'*301}],'uncertainties':[]}]
        for value in values:
            before=json.dumps(value)
            result,_=aa.resolve_references(value,'x'*3000,segments,budgeted=True)
            self.assertEqual(len(result['observations']),len(value['observations']))
            self.assertEqual(json.dumps(value),before)

    def test_alert_distinguishes_overflow_and_service_error_without_html(self):
        error=router.RouterExhausted('invalid_output',[
            {'provider':'mimo','model':'synthetic','category':'invalid_output','rejection_code':'attachment_analysis_too_large'},
            {'provider':'kimi','model':'synthetic','category':'server','detail':'<html>504 Gateway</html>'}])
        text=router.exhausted_report('email_attachment_analysis',error)
        self.assertIn('超过长度上限',text);self.assertIn('服务端报错',text)
        self.assertNotIn('<html>',text);self.assertNotIn('下一轮按原节奏重试',text)


class ReanalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);router.isolate_for_tests(self.root)
        self.config={'external_root':str(self.root),'memory_root':str(self.root),
            'email_sync':{'mailbox':'test','state_root':str(self.root),'model_timeout_seconds':1,
                'read_safety_receipt':str(self.root/'safety'),'target_map_path':str(self.root/'map'),'feishu_profile_root':str(self.root)},
            'model_routing':{'persistent_guard':True}}
        self.db=self.root/'state/email-sync.sqlite';self.db.parent.mkdir()
        with self.connection() as c:
            c.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE semantic_groups(semantic_id TEXT PRIMARY KEY,status TEXT);INSERT INTO semantic_groups VALUES("task","failed");')
        self.patches=[patch.object(mail,'connect_database',side_effect=lambda *_:self.connection()),patch.object(mail,'connect_database_readonly',side_effect=lambda *_:self.connection()),patch.object(mail,'semantic_rows',side_effect=lambda *_:self.rows)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.chunk='A'*30000;self.rows=[{'message_key':'synthetic'}]
        scope=ledger.digest([ledger.digest(['test','synthetic']),'x.txt',None])
        self.binding={'scope':scope,'text_sha256':ledger.digest(self.chunk),'start':0,'end':len(self.chunk),'chunk_sha256':ledger.digest(self.chunk),'original_text_sha256':ledger.digest(self.chunk),'contract':aa.CONTRACT}
        self.context={'scope':scope,'source_version':ledger.digest(self.binding),'contract':aa.CONTRACT}
        prompt=aa.PROMPT+'\n'+ledger.canonical({'range':[0,len(self.chunk)],'text':self.chunk})
        self.operation=ledger.Admission(self.root/'model-calls',prompt,[],'email_attachment_analysis',self.context)
        with self.operation as guard:
            for slot,provider in enumerate(('mimo','kimi','mimo')):
                aid=guard.begin(slot,provider,'synthetic');guard.received(aid,router.ProviderReply('{}'));guard.settle(aid,'invalid_output')
            guard.status('exhausted')
        self.doc={'messages':[{'attachments':[{'name':'x.txt','extracted_text':self.chunk}]}]}
        self.test=self.root/'passed-test.txt';self.test.write_text('isolated tests passed')
        self.receipts=[{'path':str(self.test),'sha256':mail.sha256_file(self.test),'passed':True}]

    def connection(self):
        c=sqlite3.connect(self.db);c.row_factory=sqlite3.Row;return c

    def grant(self):
        return recovery.authorize_attachment_reanalysis(self.config,'task',self.doc,self.rows,
            authorization='owner explicitly requested this bounded repair',fix_version='synthetic-v1',test_receipts=self.receipts)

    def test_empty_or_unrelated_explicit_scope_never_grants_an_operation(self):
        for scope in ([], ['unrelated']):
            result=recovery.authorize_attachment_reanalysis(self.config,'task',self.doc,self.rows,
                authorization='owner explicitly requested this bounded repair',fix_version='synthetic-v1',
                test_receipts=self.receipts,only_previous_operations=scope)
            self.assertEqual(result,[])
        with self.connection() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM meta WHERE key LIKE 'attachment-reanalysis:%'").fetchone()[0],0)

    def test_explicit_existing_scope_preserves_the_grant_and_paid_history(self):
        first=self.grant()
        second=recovery.authorize_attachment_reanalysis(self.config,'task',self.doc,self.rows,
            authorization='owner explicitly requested this bounded repair',fix_version='synthetic-v1',
            test_receipts=self.receipts,only_previous_operations=[self.operation.key])
        self.assertEqual(first,second)
        with sqlite3.connect(self.root/'model-calls/usage.sqlite') as c:
            self.assertEqual(c.execute('SELECT count(*) FROM attempts').fetchone()[0],3)
            self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],1)

    def test_grant_is_idempotent_and_preserves_old_three_calls(self):
        first=self.grant();second=self.grant();self.assertEqual(first,second)
        self.assertEqual(len(first),1)
        context=recovery.attachment_repair_context(self.config,self.binding,self.operation.key)
        self.assertNotEqual(context['source_version'],self.context['source_version'])
        with sqlite3.connect(self.root/'model-calls/usage.sqlite') as c:
            self.assertEqual(c.execute('SELECT count(*) FROM attempts').fetchone()[0],3)
            self.assertEqual(c.execute('SELECT status FROM operations').fetchone()[0],'exhausted')

    def test_different_authorization_cannot_silently_grant_again(self):
        self.grant()
        with self.assertRaises(ValueError):recovery.authorize_attachment_reanalysis(self.config,'task',self.doc,self.rows,authorization='different',fix_version='synthetic-v2',test_receipts=self.receipts)

    def test_binding_and_test_drift_are_rejected_before_paid_call(self):
        self.grant()
        with self.assertRaises(mail.EmailSyncError):recovery.attachment_repair_context(self.config,{**self.binding,'end':20},self.operation.key)
        self.test.write_text('changed')
        with self.assertRaises(mail.EmailSyncError):recovery.attachment_repair_context(self.config,self.binding,self.operation.key)

    def test_restart_reuses_repaired_response_and_receipt(self):
        self.grant();calls=[]
        def fake(*args,**kwargs):
            calls.append(1);return router.ProviderReply('{"observations":[{"source_span":[1,1],"observation":"supported"}],"uncertainties":[]}')
        import dingtalk_sync_v2 as sync
        with patch.object(router,'_call_http',side_effect=fake),patch.object(sync,'AI_LOCK',self.root/'ai.lock'):
            aa.prepare_email(self.config,'task',self.doc,self.root/'run',self.rows)
            # Remove isolated derived cache only, then restart with a different run id.
            import shutil;shutil.rmtree(self.root/'attachment-analysis-v1')
            aa.prepare_email(self.config,'new-id',self.doc,self.root/'run2',self.rows)
        self.assertEqual(len(calls),1)
        with sqlite3.connect(self.root/'model-calls/usage.sqlite') as c:
            self.assertEqual(c.execute('SELECT count(*) FROM attempts').fetchone()[0],4)
        proof=json.loads(next((self.root/'run/reference-receipts').glob('*.json')).read_text())
        self.assertIn('repair_generation',proof)

    def test_applied_task_cannot_be_rearmed(self):
        with self.connection() as c:c.execute('UPDATE semantic_groups SET status="applied"')
        with self.assertRaises(ValueError):self.grant()

    def test_changed_generation_cannot_create_another_budget(self):
        self.grant()
        with self.connection() as c:
            row=c.execute('SELECT key,value FROM meta WHERE key LIKE "attachment-reanalysis:%"').fetchone()
            value=json.loads(row['value']);value['generation']='arbitrary-new-generation'
            c.execute('UPDATE meta SET value=? WHERE key=?',(json.dumps(value),row['key']))
        with self.assertRaises(mail.EmailSyncError):recovery.attachment_repair_context(self.config,self.binding,self.operation.key)

    def test_unrelated_mail_members_cannot_be_granted(self):
        with patch.object(mail,'semantic_rows',return_value=[{'message_key':'another-mail'}]):
            with self.assertRaises(ValueError):self.grant()


if __name__=='__main__':unittest.main(verbosity=2)
