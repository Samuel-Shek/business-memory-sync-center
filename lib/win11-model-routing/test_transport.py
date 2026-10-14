
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest,sys,copy,tempfile,ssl,urllib.error,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
import win11_model_router as r
import mail_api_transport as m
class Host:
 def __init__(self,*args):self.calls=[];self.closed=False
 def rpc(self,method,params):
  if method=='initialize':return {}
  if method=='tools/list':return {'tools':[{'name':n,'description':'fixture','inputSchema':{'type':'object','additionalProperties':False}} for n in m.TOOLS]}
  self.calls.append(params);return {'content':[{'type':'text','text':'{"items":[]}'}]}
 def close(self):self.closed=True
class Tests(unittest.TestCase):
 def setUp(self):self.cfg=copy.deepcopy(r.load_policy()['payload']);self.host=Host()
 def invoke(self,post):return m.agent_call('mimo','mimo-v2.6-pro','fixture',self.cfg,10,Path('.'),Path('.'),mcp_config='fixture',post=post,host_factory=lambda *a:self.host)
 def test_closed_tool_loop(self):
  seen=[]
  def post(p,b,t):
   seen.append(b)
   if len(seen)==1:return {'model':'mimo-v2.6-pro','stop_reason':'tool_use','content':[{'type':'tool_use','id':'fixture-1','name':'mcp__mail__read_chunk','input':{}}]}
   return {'model':'mimo-v2.6-pro','stop_reason':'end_turn','content':[{'type':'text','text':'{}'}]}
  text,e=self.invoke(post);self.assertEqual(text,'{}');self.assertEqual(e['tool_calls'],1);self.assertTrue(self.host.closed);self.assertEqual(seen[1]['messages'][-1]['content'][0]['tool_use_id'],'fixture-1')
 def test_unknown_batch_tool_never_dispatches(self):
  def post(*a):return {'model':'mimo-v2.6-pro','stop_reason':'tool_use','content':[{'type':'tool_use','id':'1','name':'mcp__mail__read_chunk','input':{}},{'type':'tool_use','id':'2','name':'Bash','input':{}}]}
  with self.assertRaises(r.Failure):self.invoke(post)
  self.assertEqual(self.host.calls,[]);self.assertTrue(self.host.closed)
 def test_wrong_model_never_dispatches(self):
  with self.assertRaises(r.Failure):self.invoke(lambda *a:{'model':'unrelated','content':[]})
  self.assertEqual(self.host.calls,[])
 def test_unknown_tool_outcome_is_terminal(self):
  original=self.host.rpc
  def rpc(method,p):
   if method=='tools/call':raise r.Uncertain('mail_tool_outcome_unknown_no_replay')
   return original(method,p)
  self.host.rpc=rpc
  def post(*a):return {'model':'mimo-v2.6-pro','stop_reason':'tool_use','content':[{'type':'tool_use','id':'1','name':'mcp__mail__save_draft','input':{}}]}
  with self.assertRaises(r.Uncertain):self.invoke(post)
  self.assertTrue(self.host.closed)
 def test_tls_auth_timeout_redact_and_verify(self):
  errors=[(urllib.error.URLError(ssl.SSLCertVerificationError('private')),'tls_certificate'),(urllib.error.HTTPError('https://example.test',401,'private',{},None),'auth'),(TimeoutError('private'),'timeout')]
  for error,category in errors:
   with self.subTest(category=category),patch.object(r,'key',return_value='synthetic-key'),patch.object(m.urllib.request,'build_opener') as opener:
    opener.return_value.open.side_effect=error
    with self.assertRaises(r.Failure) as e:m.request('mimo',{'model':'fixture'},1)
    self.assertEqual(e.exception.category,category);self.assertNotIn('private',str(e.exception.diagnostic));handler=opener.call_args.args[1];self.assertTrue(handler._context.check_hostname);self.assertEqual(handler._context.verify_mode,ssl.CERT_REQUIRED)
if __name__=='__main__':unittest.main()
