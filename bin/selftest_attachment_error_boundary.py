
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest,tempfile
from pathlib import Path
from unittest.mock import patch
import email_sync as mail
import attachment_analysis as aa
import model_router as router

class Boundary(unittest.TestCase):
 def test_attachment_exhaustion_becomes_current_structured_mail_error(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);prompt=root/'p';prompt.write_text('test');schema=root/'s';schema.write_text('{}')
   config={'email_sync':{},'model_routing':{'source_analysis_cache':True}}
   failure=router.RouterExhausted('invalid_output',[{'provider':'mimo','category':'invalid_output'}])
   with patch.object(mail,'EMAIL_PROMPT',prompt),patch.object(mail,'EMAIL_SCHEMA',schema),patch.object(mail,'runtime_paths',return_value={'runs':root}),patch.object(mail,'semantic_rows',return_value=[]),patch.object(aa,'prepare_email',side_effect=failure):
    with self.assertRaises(mail.EmailSyncError) as ctx:mail.invoke_email_model(config,'test',{})
    self.assertEqual(ctx.exception.code,'model_output_invalid')
 def test_exact_quote_guards_remain_with_long_valid_quotes(self):
  good={'observations':[{'quote':'exact source','observation':'supported'}],'uncertainties':[]}
  self.assertEqual(aa.validate(good,'exact source'),good)
  with self.assertRaises(Exception):aa.validate(good,'different source')
  long={'observations':[{'quote':'x'*2001,'observation':'o'}],'uncertainties':[]}
  self.assertEqual(aa.validate(long,'x'*2001),long)
  with self.assertRaises(Exception):aa.validate(long,'x'*2000+'y')
class VideoScope(unittest.TestCase):
 def test_only_verified_nonvideo_attachment_can_be_ignored(self):
  import operational_workbench as work
  import json
  row={'kind':'attachment','status':'open','observation_json':json.dumps({'reading':{'non_video_complete':True,'receipt_verified':True}})}
  current={'video_waiting':True,'fingerprint_current':True,'object_available':True}
  self.assertIn('dispose',[x['id'] for x in work._actions(row,current,None,False)])
  current['fingerprint_current']=False
  self.assertNotIn('dispose',[x['id'] for x in work._actions(row,current,None,False)])
  current['fingerprint_current']=True;row['observation_json']='{}'
  self.assertNotIn('dispose',[x['id'] for x in work._actions(row,current,None,False)])

class PromptIdentity(unittest.TestCase):
 def test_existing_operation_keeps_original_prompt_and_budget(self):
  import json
  import model_call_ledger as ledger
  import dingtalk_sync_v2 as sync
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);router.isolate_for_tests(root)
   config={'email_sync':{'mailbox':'test','model_timeout_seconds':1},'model_routing':{'persistent_guard':True}}
   doc={'messages':[{'attachments':[{'name':'x.txt','extracted_text':'A'*30000}]}]}
   rows=[{'message_key':'synthetic'}];calls=[]
   def fake(provider,model,prompt,*args,**kwargs):
    calls.append(prompt);return router.ProviderReply('{"observations":[{"source_span":["E00001","E00001"],"observation":"test"}],"uncertainties":[]}')
   with patch.object(router,'_call_http',side_effect=fake),patch.object(sync,'AI_LOCK',root/'ai.lock'):
    aa.prepare_email(config,'one',doc,root/'run',rows)
    # Remove only isolated derived analysis cache, keeping the durable paid call ledger.
    import shutil
    shutil.rmtree(root/'attachment-analysis-v1')
    aa.prepare_email(config,'two',doc,root/'run2',rows)
   self.assertEqual(len(calls),1)
   self.assertIn('source_segments',calls[0])

class SourceReferences(unittest.TestCase):
 def test_reference_cache_rechecks_offsets_even_when_same_quote_exists(self):
  import copy
  import model_call_ledger as ledger
  text='x'*800;binding={'source':'synthetic'};parts=aa.reference_segments(text)
  result,spans=aa.resolve_references({'observations':[{'source_span':['E00001','E00001'],'observation':'x'}],'uncertainties':[]},text,parts)
  cached={'result':result,'reference_receipt':{'reference_version':aa.REFERENCE_VERSION,'binding':binding,'result_sha256':ledger.digest(result),'spans':spans}}
  aa.validate_reference_cache(cached,binding,text)
  changed=copy.deepcopy(cached);changed['reference_receipt']['spans'][0]['start']=400
  with self.assertRaises(ValueError):aa.validate_reference_cache(changed,binding,text)
 def test_every_character_including_unicode_and_newlines_is_preserved(self):
  text=('中😀\t1.2\r\n否定：未批准。\n'*2000)
  parts=aa.reference_segments(text)
  self.assertEqual(''.join(p['text'] for p in parts),text)
  value={'observations':[{'source_span':[parts[0]['id'],parts[1]['id']],'observation':'来源仅有未批准表述'}],'uncertainties':[]}
  result,receipt=aa.resolve_references(value,text,parts)
  self.assertEqual(result['observations'][0]['quote'],text[:parts[1]['end']])
  self.assertEqual(receipt[0]['start'],0)
 def test_unknown_and_reversed_spans_are_rejected(self):
  text='x'*3000;parts=aa.reference_segments(text)
  for span in (['E99999','E99999'],['E00002','E00001'],[True,2],[0,1],[1.0,2],['E00001']):
   with self.subTest(span=span),self.assertRaises(Exception):
    aa.resolve_references({'observations':[{'source_span':span,'observation':'x'}],'uncertainties':[]},text,parts)
 def test_integer_ordinals_are_lossless_source_id_aliases(self):
  text='a'*1000;parts=aa.reference_segments(text)
  def value(span):return {'observations':[{'source_span':span,'observation':'x'}],'uncertainties':[]}
  numeric,receipt=aa.resolve_references(value([1,2]),text,parts)
  labelled,_=aa.resolve_references(value(['E00001','E00002']),text,parts)
  self.assertEqual(numeric,labelled)
  self.assertEqual(receipt[0]['source_span'],['E00001','E00002'])
 def test_raw_quote_cannot_bypass_reference_selection(self):
  text='original'
  with self.assertRaises(Exception):
   aa.resolve_references({'observations':[{'quote':'invented','observation':'x'}],'uncertainties':[]},text,aa.reference_segments(text))
 def test_failed_legacy_operation_keeps_exact_request_and_three_attempt_budget(self):
  import json
  import model_call_ledger as ledger
  import dingtalk_sync_v2 as sync
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);router.isolate_for_tests(root)
   config={'email_sync':{'mailbox':'test','model_timeout_seconds':10},'model_routing':{'persistent_guard':True}}
   chunk='A'*30000
   scope=ledger.digest([ledger.digest(['test','synthetic']),'x.txt',None])
   binding={'scope':scope,'text_sha256':ledger.digest(chunk),'start':0,'end':len(chunk),
    'chunk_sha256':ledger.digest(chunk),'original_text_sha256':ledger.digest(chunk),'contract':aa.CONTRACT}
   context={'scope':scope,'source_version':ledger.digest(binding),'contract':aa.CONTRACT}
   prompt=aa.PROMPT+'\n'+ledger.canonical({'range':[0,len(chunk)],'text':chunk})
   with ledger.Admission(root/'model-calls',prompt,[],'email_attachment_analysis',context) as guard:
    for slot,provider in enumerate(('deepseek','kimi','deepseek')):
     aid=guard.begin(slot,provider,'synthetic');guard.received(aid,router.ProviderReply('{"observations":[{"quote":"fabricated","observation":"x"}],"uncertainties":[]}'));guard.settle(aid,'invalid_output')
    guard.status('exhausted')
   doc={'messages':[{'attachments':[{'name':'x.txt','extracted_text':chunk}]}]}
   with patch.object(router,'_call_http',side_effect=AssertionError('must not spend again')),patch.object(sync,'AI_LOCK',root/'ai.lock'):
    for _ in range(2):
     with self.assertRaises(router.RouterExhausted):aa.prepare_email(config,'new-batch-id',doc,root/'run',[{'message_key':'synthetic'}])
   report=ledger.report(root/'model-calls');self.assertEqual(len(report['calls']),3)
   self.assertEqual(len(report['operations']),1)
if __name__=='__main__':unittest.main()
