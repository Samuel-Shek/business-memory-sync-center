
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import copy,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import win11_model_router as r
from routing_receipt import validate_route
import mail_route_runner
class ReceiptTests(unittest.TestCase):
 def setUp(self):
  self.policy=r.load_policy();self.text='Test fixture: delivery quantity is seven.';self.model='mimo-v2.6-pro'
  self.summary=dict(model=self.model,text_sha256=r.digest(self.text.encode()),items=[dict(summary='材料称数量为七。',quote=self.text,start=0,end=len(self.text))])
  self.route=dict(schema_version=2,policy_sha256=self.policy['sha256'],routing_version=self.policy['version'],provider='mimo',model=self.model,outcome='ok',attempts=[dict(provider='mimo',model=self.model,category='ok',cycle=1,evidence={'actual_model':self.model,'transport':'http','tool_calls':0})])
  self.audit=dict(schema_version=2,text_sha256=self.summary['text_sha256'],summary_sha256=r.digest(r.canonical(self.summary).encode()),model=self.model,tool_calls=0,route_receipt=self.route)
 def test_valid(self):self.assertEqual(validate_route(self.audit,self.summary,self.text,self.policy),self.model)
 def test_mutations_rejected(self):
  for field,val in [('model','gpt-6-astra'),('policy_sha256','a'*64),('outcome','started'),('routing_version','old')]:
   with self.subTest(field=field):
    a=copy.deepcopy(self.audit);a['route_receipt'][field]=val
    with self.assertRaises(ValueError):validate_route(a,self.summary,self.text,self.policy)
 def test_source_changed(self):
  with self.assertRaises(ValueError):validate_route(self.audit,self.summary,self.text+'x',self.policy)
 def test_false_tool_free_claim(self):
  self.route['attempts'][0]['evidence']['tool_calls']=1
  with self.assertRaises(ValueError):validate_route(self.audit,self.summary,self.text,self.policy)
 def test_reordered_attempts(self):
  self.route['attempts'][0]['provider']='kimi'
  with self.assertRaises(ValueError):validate_route(self.audit,self.summary,self.text,self.policy)
 def test_schema_counts(self):
  schema=json.loads((Path(__file__).parent/'mail_sync_result.schema.json').read_text())
  obj={k:(0 if v['type']=='integer' else '') for k,v in schema['properties'].items()};obj['status']='success'
  self.assertEqual(mail_route_runner.validate(json.dumps(obj),schema),obj)
  obj['business_processed']=True
  with self.assertRaises(ValueError):mail_route_runner.validate(json.dumps(obj),schema)
 def test_success_error_conflict(self):
  schema=json.loads((Path(__file__).parent/'mail_sync_result.schema.json').read_text());obj={k:(0 if v['type']=='integer' else '') for k,v in schema['properties'].items()};obj.update(status='success',error_summary='Failed')
  with self.assertRaises(ValueError):mail_route_runner.validate(json.dumps(obj),schema)
if __name__=='__main__':unittest.main()
