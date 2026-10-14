
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import ast,sqlite3,tempfile,unittest,json
from pathlib import Path
from datetime import datetime
from unittest.mock import patch,Mock
import index_runtime_recovery as r
import automatic_memory_validation as v
import business_sync_supervisor as s
import validation_retry,operational_workbench

class Boundaries(unittest.TestCase):
 def test_known_fault_contained_but_unknown_still_raises(self):
  with tempfile.TemporaryDirectory() as tmp:
   service=s.Supervisor(Path(tmp)/'config.json',database=Path(tmp)/'sync.sqlite')
   with patch.object(service,'schedule_full_validation',return_value=None),patch.object(service,'_reconcile_attention'),patch.object(service,'reconcile_chat_recovery'),patch.object(service,'schedule_local_repair') as tail,patch.object(operational_workbench,'reconcile'),patch.object(operational_workbench,'recover_actions'),patch.object(r,'retain_expected_failure') as retain,patch.object(validation_retry,'reconcile',side_effect=v.ValidationFault('memory_index_update_failed')):
    service.reconcile_attention({},datetime.now().astimezone());tail.assert_called_once();retain.assert_called_once()
   with patch.object(service,'schedule_full_validation',return_value=None),patch.object(service,'_reconcile_attention'),patch.object(service,'reconcile_chat_recovery'),patch.object(validation_retry,'reconcile',side_effect=v.ValidationFault('unknown_new_fault')):
    with self.assertRaises(v.ValidationFault):service.reconcile_attention({},datetime.now().astimezone())
 def test_receiver_failure_retained_after_success(self):
  tree=ast.parse((Path(__file__).parent/'receiver.py').read_text())
  nodes=[x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name in {'refresh_dirty_indexes','set_maintenance_value'}]
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);now=lambda:datetime.now().astimezone().isoformat();refresher=Mock(side_effect=RuntimeError('private-body'))
   ns={'ROOT':root,'now_iso':now,'run_index_refresher':refresher,'append_log':Mock(),'write_health':Mock(),'restore_index_refresh_health':Mock(),'datetime':datetime,'time':__import__('time'),'INDEX_REFRESH_RETRY_COOLDOWN_SECONDS':60}
   exec(compile(ast.Module(body=nodes,type_ignores=[]),'receiver-selected-functions','exec'),ns)
   c=sqlite3.connect(root/'fixture.sqlite');c.execute('create table maintenance_state(key text primary key,value text,updated_at text)')
   c.executemany('insert into maintenance_state values(?,?,?)',[('index_dirty_generation','2',now()),('index_applied_generation','1',now()),('index_last_error','',now())]);c.commit()
   self.assertFalse(ns['refresh_dirty_indexes'](c,force=True));ps=list((root/'state/index-failures').glob('*.json'));self.assertEqual(len(ps),1);self.assertNotIn('private-body',ps[0].read_text())
   first=ps[0].read_bytes();self.assertTrue(c.execute("select value from maintenance_state where key='index_last_failure_event'").fetchone()[0]);refresher.side_effect=None
   self.assertTrue(ns['refresh_dirty_indexes'](c,force=True));self.assertEqual(ps[0].read_bytes(),first);self.assertEqual(c.execute("select value from maintenance_state where key='index_last_error'").fetchone()[0],'');c.close()
 def test_receiver_status_only_failure_durable(self):
  tree=ast.parse((Path(__file__).parent/'receiver.py').read_text());nodes=[x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name in {'refresh_dirty_indexes','set_maintenance_value'}]
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);now=lambda:datetime.now().astimezone().isoformat()
   ns={'ROOT':root,'now_iso':now,'run_index_refresher':Mock(side_effect=RuntimeError('secret')),'append_log':Mock(),'write_health':Mock(),'restore_index_refresh_health':Mock(),'datetime':datetime,'time':__import__('time'),'INDEX_REFRESH_RETRY_COOLDOWN_SECONDS':60}
   exec(compile(ast.Module(body=nodes,type_ignores=[]),'receiver-selected-functions','exec'),ns)
   c=sqlite3.connect(root/'fixture.sqlite');c.execute('create table maintenance_state(key text primary key,value text,updated_at text)');c.executemany('insert into maintenance_state values(?,?,?)',[('index_dirty_generation','2',now()),('index_applied_generation','2',now()),('index_last_error','error',now())]);c.commit()
   self.assertFalse(ns['refresh_dirty_indexes'](c,force=True));d=json.loads(next((root/'state/index-failures').glob('*.json')).read_text());self.assertEqual(d['phase'],'index_status_repair');ns['restore_index_refresh_health'].assert_not_called();c.close()
 def test_only_successful_index_refresh_restores_its_health(self):
  tree=ast.parse((Path(__file__).parent/'receiver.py').read_text());nodes=[x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name in {'refresh_dirty_indexes','set_maintenance_value','restore_index_refresh_health'}]
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);health=root/'health.json';now=lambda:datetime.now().astimezone().isoformat();write=Mock()
   ns={'ROOT':root,'HEALTH_PATH':health,'json':json,'now_iso':now,'run_index_refresher':Mock(),'append_log':Mock(),'write_health':write,'datetime':datetime,'time':__import__('time'),'INDEX_REFRESH_RETRY_COOLDOWN_SECONDS':60}
   exec(compile(ast.Module(body=nodes,type_ignores=[]),'receiver-selected-functions','exec'),ns)
   c=sqlite3.connect(root/'fixture.sqlite');c.execute('create table maintenance_state(key text primary key,value text,updated_at text)');c.executemany('insert into maintenance_state values(?,?,?)',[('index_dirty_generation','2',now()),('index_applied_generation','2',now()),('index_last_error','error',now())]);c.commit()
   health.write_text(json.dumps({'status':'degraded','detail':'memory_index_status_commit_failed'}))
   self.assertTrue(ns['refresh_dirty_indexes'](c,force=True));write.assert_called_once_with('running','memory index refresh recovered',applied_generation=2)
   for status,detail in [('degraded','items_quarantined'),('failed','receiver failed to start'),('degraded','watch_inbox_trust_failed'),('running','receiver started')]:
    write.reset_mock();health.write_text(json.dumps({'status':status,'detail':detail}));ns['restore_index_refresh_health'](2);write.assert_not_called()
   write.reset_mock();health.write_text('{');ns['restore_index_refresh_health'](2);write.assert_not_called();c.close()
if __name__=='__main__':unittest.main()
