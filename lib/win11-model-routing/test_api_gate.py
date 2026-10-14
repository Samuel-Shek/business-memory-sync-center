
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest,tempfile,json,copy
from pathlib import Path
from unittest.mock import patch
import win11_model_router as r
import mail_api_transport as m
from test_transport import Host
class GateTests(unittest.TestCase):
 def invoke(self,post):
  cfg=r.load_policy()['payload'];host=Host();self.host=host
  return m.agent_call('mimo','mimo-v2.6-pro','fixture',cfg,10,Path('.'),Path('.'),mcp_config='fixture',post=post,host_factory=lambda *a:host)
 def test_malformed_arguments_and_ids_are_rejected_before_dispatch(self):
  variants=[{'id':'one','input':{'{}':''}},{'input':{}},{'id':'','input':{}}]
  for variant in variants:
   with self.subTest(variant=variant),self.assertRaises(r.Failure) as e:
    self.invoke(lambda *a:{'model':'mimo-v2.6-pro','stop_reason':'tool_use','content':[dict(type='tool_use',name='mcp__mail__read_chunk',**variant)]})
   self.assertEqual(e.exception.category,'invalid_output');self.assertEqual(self.host.calls,[])
 def test_tls_is_terminal_at_every_chain_position_and_restart(self):
  for position in range(3):
   with tempfile.TemporaryDirectory() as d:
    calls=[]
    def request(p,m,t):
     calls.append(p)
     if len(calls)==position+1:raise r.failure_from_text('UNKNOWN_CERTIFICATE_VERIFICATION_ERROR private')
     raise r.Failure('auth')
    for _ in range(2):
     with self.assertRaises(r.Exhausted) as exc:r.complete('fixture',purpose='gate',job=Path(d),validate=json.loads,transport=request,use_health=False)
     self.assertEqual(exc.exception.receipt['outcome'],'client_security_failed')
    self.assertEqual(calls,['mimo','kimi','mimo'][:position+1])
 def test_tool_uncertain_blocks_model_fallback_and_restart(self):
  with tempfile.TemporaryDirectory() as d:
   calls=[]
   def request(p,m,t):calls.append(p);raise r.Uncertain('mail_tool_outcome_unknown_no_replay')
   for _ in range(2):
    with self.assertRaises(r.Uncertain):r.complete('fixture',purpose='gate',job=Path(d),validate=json.loads,transport=request,use_health=False)
   self.assertEqual(calls,['mimo'])
 def test_previous_deepseek_policy_remains_exact_and_accepted(self):
  import routing_receipt
  policy=r.load_policy();sha='be0a4a8413ad3efc1f9b410752baa426ffef5c2fe4aeec32761571a831338eaa'
  selected=routing_receipt.policy_for_receipt({'policy_sha256':sha,'routing_version':'20260921-deepseek-v1'},policy)
  self.assertEqual(selected['payload']['routing']['text_chain'],['deepseek:deepseek-flash','kimi:k3'])
  self.assertEqual(r.digest(r.canonical(selected['payload']).encode()),sha)
 def test_summary_certificate_is_classified_without_error_body(self):
  import ssl,urllib.error
  with patch.object(r,'key',return_value='fixture'),patch.object(r.urllib.request,'build_opener') as opener:
   opener.return_value.open.side_effect=urllib.error.URLError(ssl.SSLCertVerificationError('private'))
   with self.assertRaises(r.Failure) as e:r.http_call('mimo','mimo-v2.6-pro','fixture',(),r.load_policy()['payload'],1)
  self.assertEqual(e.exception.category,'tls_certificate');self.assertNotIn('private',str(e.exception.diagnostic))
 def test_api_worker_enforces_hard_deadline(self):
  import sys,time
  launch=m.subprocess.Popen
  def sleeper(*args,**kw):return launch([sys.executable,'-c','import time;time.sleep(5)'],**kw)
  start=time.monotonic()
  with patch.object(m.subprocess,'Popen',side_effect=sleeper),self.assertRaises(r.Failure) as e:m.bounded_request('mimo',{'fixture':True},.1)
  self.assertEqual(e.exception.category,'timeout');self.assertLess(time.monotonic()-start,2)
 def test_auth_and_timeout_keep_original_fallback_order(self):
  with tempfile.TemporaryDirectory() as d:
   calls=[]
   def request(p,m,t):calls.append(p);raise r.Failure('auth' if len(calls)==1 else 'timeout')
   with self.assertRaises(r.Exhausted) as exc:r.complete('fixture',purpose='gate',job=Path(d),validate=json.loads,transport=request,use_health=False)
   self.assertEqual(calls,['mimo','kimi','mimo']);self.assertEqual(exc.exception.receipt['outcome'],'chain_exhausted')
if __name__=='__main__':unittest.main()
