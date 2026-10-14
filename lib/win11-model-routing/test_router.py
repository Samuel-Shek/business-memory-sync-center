
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest,tempfile,sys,json,copy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
import win11_model_router as r
class RouterTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.old=r.HOME;r.HOME=self.root/'home';self.policy=r.load_policy(Path(__file__).parent/'routing.json');self.calls=[]
 def tearDown(self):r.HOME=self.old;self.tmp.cleanup()
 def call(self,transport,**kw):return r.complete('fixture',purpose='unit',job=self.root/'job',validate=json.loads,policy=self.policy,transport=transport,**kw)
 def fail(self,p,m,t):self.calls.append(p);raise r.Failure('quota')
 def test_order_retry(self):
  with self.assertRaises(r.Exhausted) as e:self.call(self.fail)
  self.assertEqual(self.calls,['mimo','kimi','mimo']);self.assertEqual(e.exception.receipt['outcome'],'chain_exhausted')
 def test_first_success_and_idempotence(self):
  def ok(p,m,t):self.calls.append(p);return '{}',{'actual_model':m}
  self.call(ok);self.call(ok);self.assertEqual(self.calls,['mimo'])
 def test_invalid_output_falls_back(self):
  def invoke(p,m,t):self.calls.append(p);return ('bad' if p=='mimo' else '{}'),{'actual_model':m}
  _,_,receipt=self.call(invoke);self.assertEqual(self.calls,['mimo','kimi']);self.assertEqual(receipt['attempts'][0]['category'],'invalid_output')
 def test_image_model(self):
  img=self.root/'test.png';img.write_bytes(b'fixture')
  def invoke(p,m,t):self.assertEqual(m,'mimo-v2.6-pro');return '{}',{'actual_model':m}
  self.call(invoke,images=[img])
 def test_cooldown_retry_primary(self):
  for p in ('mimo','mimo','kimi','claude','gpt'):r.atomic(r.HOME/'health'/('summary-'+p+'.json'),{'cooldown_until':r.time.time()+1000})
  with self.assertRaises(r.Exhausted):self.call(self.fail)
  self.assertEqual(self.calls,['mimo'])
 def test_budget_is_not_all_failed(self):
  with self.assertRaises(r.Exhausted) as e:self.call(self.fail,timeout=-1)
  self.assertEqual(e.exception.receipt['outcome'],'budget_exhausted');self.assertEqual(self.calls,[])
 def test_uncertain_never_replay(self):
  def bad(p,m,t):self.calls.append(p);raise r.Uncertain('unknown')
  with self.assertRaises(r.Uncertain):self.call(bad)
  with self.assertRaises(r.Uncertain):self.call(bad)
  self.assertEqual(self.calls,['mimo'])
 def test_source_changed(self):
  self.call(lambda p,m,t:('{}',{}))
  with self.assertRaises(r.Uncertain):r.complete('changed',purpose='unit',job=self.root/'job',validate=json.loads,policy=self.policy,transport=self.fail)
 def test_hash_tampering(self):
  self.call(lambda p,m,t:('{}',{}));(self.root/'job/result.txt').write_text('{"bad":true}')
  with self.assertRaises(r.Uncertain):self.call(self.fail)
 def test_all_failure_categories(self):
  for category in ('quota','auth','timeout','network','server','empty','bad_request'):
   with self.subTest(category=category):
    def invoke(p,m,t):
     if p=='mimo':raise r.Failure(category)
     return '{}',{}
    r.complete('fixture',purpose=category,job=self.root/category,validate=json.loads,policy=self.policy,transport=invoke,use_health=False)
 def test_environment(self):
  with patch.dict(r.os.environ,{'OPENAI_BASE_URL':'bad','ANTHROPIC_BASE_URL':'bad','MIMO_API_KEY':'secret','KIMI_API_KEY':'secret','DEEPSEEK_API_KEY':'secret'}):
   env=r.environment();self.assertFalse(any(k in env for k in ('OPENAI_BASE_URL','ANTHROPIC_BASE_URL','MIMO_API_KEY','KIMI_API_KEY','DEEPSEEK_API_KEY')))
 def test_policy_integrity(self):
  p=copy.deepcopy(self.policy);p['payload']['routing']['text_chain'].reverse();f=self.root/'p.json';r.atomic(f,p)
  with self.assertRaises(ValueError):r.load_policy(f)
 def test_no_agent_retry_before_checkpoint(self):
  def checkpoint():raise r.Uncertain('checkpoint_unavailable')
  with self.assertRaises(r.Uncertain):self.call(self.fail,agent=True,checkpoint=checkpoint)
  self.assertEqual(self.calls,[])
 def test_mimo_agent_uses_existing_anthropic_client(self):
  result=json.dumps({'result':'{}','modelUsage':{'mimo-v2.5-pro':{}},'is_error':False})
  policy=copy.deepcopy(self.policy);policy['payload']['routing']['enabled_providers'].append('mimo')
  with patch.object(r,'run_process',return_value=result) as call,patch.object(r,'key',return_value='test-only'):
   _,evidence=r.cli_call('mimo','mimo-v2.5-pro','fixture',(),policy['payload'],10,self.root,self.root,True)
   args=call.call_args.args
   self.assertEqual(args[2]['ANTHROPIC_BASE_URL'],'https://token-plan-cn.xiaomimimo.com/anthropic')
   self.assertIn('--tools',args[0]);self.assertEqual(evidence['actual_model'],'mimo-v2.5-pro')
 def test_claude_auxiliary_usage_keeps_primary_identity(self):
  self.policy['payload']['routing']['enabled_providers'].append('claude')
  result=json.dumps({'result':'{}','modelUsage':{'claude-haiku-4-5-20251001':{},'claude-opus-5':{}},'is_error':False})
  with patch.object(r,'run_process',return_value=result):
   _,evidence=r.cli_call('claude','claude-opus-5','fixture',(),self.policy['payload'],10,self.root,self.root)
   self.assertEqual(evidence['actual_model'],'claude-opus-5')
 def test_agent_workflow_is_not_cut_at_one_request_timeout(self):
  def invoke(p,m,cap):
   self.assertGreater(cap,880);self.assertLessEqual(cap,900);return '{}',{}
  self.call(invoke,agent=True,timeout=900)
 def test_single_summary_keeps_request_cap(self):
  def invoke(p,m,cap):self.assertEqual(cap,420);return '{}',{}
  self.call(invoke,timeout=900)
 def test_claude_sets_request_deadline_and_effort_in_child_only(self):
  self.policy['payload']['routing']['enabled_providers'].append('claude')
  result=json.dumps({'result':'{}','modelUsage':{'claude-opus-5':{}},'is_error':False})
  with patch.object(r,'run_process',return_value=result) as call:
   r.cli_call('claude','claude-opus-5','fixture',(),self.policy['payload'],900,self.root,self.root,True)
   args=call.call_args.args;self.assertEqual(args[2]['API_TIMEOUT_MS'],'420000');self.assertEqual(args[2]['CLAUDE_CODE_EFFORT_LEVEL'],'high');self.assertEqual(args[4],900)
 def test_multiple_short_requests_can_outlive_one_request_budget(self):
  self.policy['payload']['routing']['per_attempt_timeout_seconds']=1
  self.policy['sha256']=r.digest(r.canonical(self.policy['payload']).encode())
  def invoke(provider,model,cap):
   script="import time; [time.sleep(0.4) for _ in range(5)]; print('{}')"
   text=r.run_process([sys.executable,'-c',script],'',r.environment(),self.root,cap)
   return text,{'actual_model':model}
  start=r.time.monotonic();self.call(invoke,agent=True,timeout=8,use_health=False)
  self.assertGreaterEqual(r.time.monotonic()-start,2)
if __name__=='__main__':unittest.main()
