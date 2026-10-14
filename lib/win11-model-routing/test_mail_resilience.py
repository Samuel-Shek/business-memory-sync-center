
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import copy,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import mail_route_runner as m
import win11_model_router as r

class ControllerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.work=self.root/'project';(self.work/'work').mkdir(parents=True)
  for n in ('mail_mirror.py','mail_retry_policy.py','mail_attachment_refs.py','mail_cloud_attachments.py'):(self.work/'work'/n).write_text('fixture')
  self.job=self.root/'job';self.job.mkdir()
  self.q={'pending_count':1,'blocked_count':1,'waiting_count':0,'actionable_count':0,'actionable_paths':[], 'hard_error_count':0,'deferred_items':[{'reasons':['CLOUD_ATTACHMENT_TYPE_BLOCKED']}]}
  self.counts={'business_processed':0,'prefilter_ignored':0,'categories':{}}
  self.pre={'steps':{'sync':{'result':{'report_path':'synthetic','run_id':'run'}},'prefilter-nonbusiness':{'result':{'checked':1}}}}
 def tearDown(self):self.tmp.cleanup()
 def test_preflight_once_and_crash_fail_closed(self):
  calls=[]
  def command(w,name):calls.append(name);return {'report_path':'fixture'}
  verifier=lambda v,r:v
  m.prepare(self.job,self.work,'run',command,verifier);m.prepare(self.job,self.work,'run',command,verifier)
  self.assertEqual(['sync','retry-attachment-extraction','prefilter-nonbusiness'],calls)
  s=json.loads((self.job/'preflight.json').read_text());s['steps']['sync']['status']='started';r.atomic(self.job/'preflight.json',s)
  with self.assertRaises(r.Uncertain):m.prepare(self.job,self.work,'run',command,verifier)
  self.assertEqual(3,len(calls))
 def test_input_change_blocks_preflight_replay(self):
  m.prepare(self.job,self.work,'run',lambda *a:{},lambda v,r:v)
  (self.work/'work/mail_mirror.py').write_text('changed')
  with self.assertRaises(r.Uncertain):m.prepare(self.job,self.work,'run',lambda *a:self.fail('replay'),lambda v,r:v)
 def test_no_actionable_skips_model_but_not_collection(self):
  run='557a953a-7431-5891-bc7e-faa8543463b4'
  schema=Path(__file__).with_name('mail_sync_result.schema.json');prompt=self.root/'prompt';prompt.write_text('fixture')
  args=SimpleNamespace(run_id=run,workdir=str(self.work),schema=str(schema),prompt=str(prompt),result=str(self.root/'result.json'))
  calls=[]
  def command(w,c):calls.append(c);return self.counts if c=='run-receipts' else self.q
  with patch.dict(os.environ,{'BUSINESS_MAIL_SYNC_RUN_ID':run}),patch.object(r,'HOME',self.root/'router'),patch.object(m,'subprocess') as sub,patch.object(m,'prepare',return_value=self.pre) as prep,patch.object(r,'complete',side_effect=AssertionError('must not invoke')):
   sub.run.return_value.returncode=0;sub.run.return_value.stdout=b'{"items":0}'
   self.assertEqual(0,m.execute(args,command));prep.assert_called_once()
  result=json.loads(Path(args.result).read_text());self.assertEqual('attention',result['status']);self.assertFalse(result['model_invoked']);self.assertEqual(1,result['pending_remaining'])
 def test_aggregate_prefers_real_errors_and_real_counts(self):
  fake={'status':'success','business_processed':999}
  self.assertEqual(0,m.assemble(self.pre,self.q,self.counts,fake)['business_processed'])
  self.assertEqual('attention',m.assemble(self.pre,self.q,self.counts,fake)['status'])
  self.q['hard_error_count']=1;self.assertEqual('error',m.assemble(self.pre,self.q,self.counts,fake)['status'])
  self.q['hard_error_count']=0;self.assertEqual('error',m.assemble(self.pre,self.q,self.counts,fake,'model fault')['status'])
 def test_ocr_or_other_pending_never_all_complete(self):
  self.q.update(blocked_count=0,actionable_count=1)
  self.assertEqual('attention',m.assemble(self.pre,self.q,self.counts,{'status':'success'})['status'])
 def test_invalid_output_reason_does_not_echo_unknown_fields(self):
  schema=json.loads(Path(__file__).with_name('mail_sync_result.schema.json').read_text())
  with self.assertRaises(r.OutputInvalid) as c:m.validate('{"synthetic-secret":"private"}',schema)
  self.assertEqual({'reason':'fields_invalid'},c.exception.diagnostic)
 def test_validation_diagnostic_has_no_raw_content(self):
  # Use a local policy fixture and fake transport: no provider/network/production credentials.
  policy=r.load_policy();root=self.root/'route'
  def invalid(text):raise r.OutputInvalid('count_invalid','pending_remaining')
  with patch.object(r,'HOME',self.root/'isolated'):
   with self.assertRaises(r.Exhausted) as c:r.complete('fixture',purpose='unit',job=root,validate=invalid,policy=policy,transport=lambda *a:('synthetic-secret-DO-NOT-LOG',{}),use_health=False)
  raw=json.dumps(c.exception.receipt);self.assertNotIn('synthetic-secret',raw)
  self.assertEqual('count_invalid',c.exception.receipt['attempts'][0]['diagnostic']['reason'])
 def test_cli_envelope_and_identity_are_separate(self):
  policy=r.load_policy()['payload']
  with patch.object(r,'run_process',return_value='not json secret'),patch.object(r,'key',return_value='fixture'):
   with self.assertRaises(r.Failure) as c:r.cli_call('mimo','mimo-v2.6-pro','fixture',(),policy,10,self.root,self.root,True)
  self.assertEqual('example_24b073193a85',c.exception.diagnostic['stage']);self.assertNotIn('secret',json.dumps(c.exception.diagnostic))
  with patch.object(r,'run_process',return_value=json.dumps({'result':'{}','modelUsage':{'unrelated-model':{}}})),patch.object(r,'key',return_value='fixture'):
   with self.assertRaises(r.Failure) as c:r.cli_call('mimo','mimo-v2.6-pro','fixture',(),policy,10,self.root,self.root,True)
  self.assertEqual('model_identity',c.exception.diagnostic['stage'])

 def test_native_schema_uses_structured_output_without_extra_retry_budget(self):
  policy=r.load_policy()['payload'];schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
  output=json.dumps({'result':'unstructured narrative','structured_output':{'ok':True},'modelUsage':{'mimo-v2.6-pro':{}}})
  with patch.object(r,'run_process',return_value=output) as call,patch.object(r,'key',return_value='fixture'):
   text,evidence=r.cli_call('mimo','mimo-v2.6-pro','fixture',(),policy,10,self.root,self.root,True,output_schema=schema)
  self.assertEqual({'ok':True},json.loads(text));self.assertIn('--json-schema',call.call_args.args[0]);self.assertEqual('0',call.call_args.args[2]['MAX_STRUCTURED_OUTPUT_RETRIES'])
  output=json.dumps({'result':'{}','modelUsage':{'mimo-v2.6-pro':{}}})
  with patch.object(r,'run_process',return_value=output),patch.object(r,'key',return_value='fixture'):
   with self.assertRaises(r.Failure) as c:r.cli_call('mimo','mimo-v2.6-pro','fixture',(),policy,10,self.root,self.root,True,output_schema=schema)
  self.assertEqual('structured_output_missing',c.exception.diagnostic['reason'])

class RestrictedToolTests(unittest.TestCase):
 def test_mcp_mail_uses_only_closed_host_transport(self):
  import mail_api_transport as transport
  cfg=r.load_policy()['payload']
  with patch.object(transport,'agent_call',return_value=('{}',{})) as api,patch.object(r,'run_process') as native:
   r.cli_call('mimo','mimo-v2.6-pro','fixture',(),cfg,10,Path('.'),Path('.'),agent=True,mcp_config='fixed-mcp.json')
  native.assert_not_called();self.assertEqual(api.call_args.kwargs['mcp_config'],'fixed-mcp.json')

if __name__=='__main__':unittest.main()
