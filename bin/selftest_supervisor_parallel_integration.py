
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest
import tempfile
import json
import threading
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
import business_sync_supervisor as s
import supervisor_worker_pool

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=self.root/"config.json";self.config.write_text('{}')
        self.service=s.Supervisor(self.config,database=self.root/"test.sqlite",parallel_workers=True)
        self.config_data={"email_sync":{"background_collection_while_active":True}}

    def tearDown(self):self.tmp.cleanup()

    def test_maintenance_and_power_stop_dispatch(self):
        with patch.object(s,"maintenance_status",return_value={"active":True}):
            self.assertEqual(self.service.parallel_eligible({"win11"},(),{}),[])
        with patch.object(s,"maintenance_status",return_value={"active":False}), patch.object(s,"power_state",return_value={"known":True,"on_ac":False}):
            self.assertEqual(self.service.parallel_eligible({"win11"},(),{}),[])

    def test_idle_and_shared_dingtalk_group_are_respected(self):
        with patch.object(s,"maintenance_status",return_value={"active":False}), patch.object(s,"power_state",return_value={"known":True,"on_ac":True}), patch.object(s,"idle_state",return_value={"ready":True}):
            self.assertEqual(self.service.parallel_eligible({"dingtalk_chat","dingtalk_minutes","win11"},(),{}),["dingtalk_chat","win11"])
            self.assertEqual(self.service.parallel_eligible({"dingtalk_chat","dingtalk_minutes","win11"},("dingtalk_chat",),{}),["win11"])
        with patch.object(s,"maintenance_status",return_value={"active":False}), patch.object(s,"power_state",return_value={"known":True,"on_ac":True}), patch.object(s,"idle_state",return_value={"ready":False}), patch.object(s.state,"source",return_value={}),patch.object(s.state,"update_source"):
            self.assertEqual(self.service.parallel_eligible({"dingtalk_mail","win11"},(),self.config_data),["dingtalk_mail"])

    def test_controller_results_are_recorded_on_owner_while_mail_is_slow(self):
        release=threading.Event();owner=threading.get_ident();seen=[]
        def worker(source):
            if source=="dingtalk_mail":self.assertTrue(release.wait(2))
            else:release.set()
            return {"status":"noop"}
        def record(source,receipt,when):
            self.assertEqual(threading.get_ident(),owner);seen.append(source)
            return {"status":"healthy","last_run_id":source}
        with patch.object(self.service,"parallel_eligible",side_effect=lambda candidates,active,config:[x for x in ("dingtalk_mail","win11") if x in candidates and x not in active]),patch.object(self.service,"run_worker",side_effect=worker),patch.object(self.service,"record_outcome",side_effect=record),patch.object(self.service,"pulse_owner"),patch.object(s.state,"update_source"),patch.object(s.state,"sources",return_value=[]),patch.object(s.state,"update_runtime"):
            result=self.service.run_parallel_sources({"dingtalk_mail","win11"},self.config_data)
        self.assertEqual(set(seen),{"dingtalk_mail","win11"})
        self.assertEqual(len(result),2)

    def test_newly_due_minutes_continue_after_chat_defers_during_mail(self):
        now=datetime.fromisoformat("2020-01-02T10:00:00+08:00")
        self.service.clock=lambda:now
        rows=[{"source_id":source,"next_due":now.isoformat()} for source in ("dingtalk_chat","dingtalk_minutes","dingtalk_mail")]
        def record(source,receipt,at):
            for row in rows:
                if row["source_id"]==source:row["next_due"]="2020-01-02T10:01:00+08:00"
            return {"status":"deferred"}
        def coordinated(initial,**hooks):
            self.assertEqual(initial,["dingtalk_mail"])
            hooks["prepare"]("dingtalk_mail")
            self.assertEqual(hooks["eligible"]({"dingtalk_mail"}),["dingtalk_chat"])
            hooks["prepare"]("dingtalk_chat")
            hooks["record"]("dingtalk_chat",{"status":"deferred"})
            self.assertEqual(hooks["continuations"]({"dingtalk_mail"}),["dingtalk_minutes"])
            return []
        with patch.object(s,"maintenance_status",return_value={"active":False}),patch.object(s,"power_state",return_value={"known":True,"on_ac":True}),patch.object(s,"idle_state",return_value={"ready":True}),patch.object(s,"chat_checkpoint_progress",return_value={"status":"available"}),patch.object(s.state,"sources",side_effect=lambda db:rows),patch.object(s.state,"update_source"),patch.object(s.state,"update_runtime"),patch.object(self.service,"record_outcome",side_effect=record),patch.object(s.supervisor_worker_pool,"run",side_effect=coordinated):
            self.service.run_parallel_sources({"dingtalk_mail"},{})

    def test_notification_delivery_does_not_renew_supervisor_heartbeat(self):
        alert={"alert_id":"a","source_id":"system","category":"test","status":"open","macos_status":"delivered","miso_status":"delivered"}
        with patch.object(s.ledger_recovery,"flush"),patch.object(s,"recheck_macos_notifications"),patch.object(s.state,"due_alerts",return_value=[alert]),patch.object(s,"_pending_delivery_alerts",return_value=set()),patch.object(s.state,"claim_delivery",return_value=alert),patch.object(s.state,"update_runtime") as update:
            self.assertEqual(s._deliver_due_alerts({},self.root/"test.sqlite"),1)
            update.assert_not_called()

if __name__=="__main__":unittest.main()
