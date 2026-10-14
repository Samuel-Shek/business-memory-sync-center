"""Read-only episode display and handoff regression tests."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import chat_read_recovery as view
import handoff_context

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.config={'external_root':str(self.root)}
        self.live={'listener_ready_at':'2020-01-02T21:34:43+08:00',
          'subscription_checked_at':'2020-01-02T22:02:42+08:00',
          'incoming_by_kind':{'direct':False,'group':False},'live_event_receipts':{},
          'live_acceptance':'live_acceptance_pending','subscriptions_valid':False,
          'subscription_verification':'pending','server_subscription_listing':'empty',
          'live_acceptance_at':'2020-01-02T18:50:00+08:00'}
    def seen(self,kind):
        self.live['incoming_by_kind'][kind]=True
        self.live['live_event_receipts'][kind]={'received_at':'2020-01-02T21:40:00+08:00','event_ref':'must-not-leak'}
    def result(self):return view.listener_acceptance_view(self.config,self.live)
    def test_no_messages_and_old_success_are_pending(self):
        r=self.result();self.assertEqual(r['status'],'pending');self.assertIsNone(r['accepted_at']);self.assertEqual(r['historical_accepted_at'],self.live['live_acceptance_at'])
        self.assertEqual(r['event_counts_status'],'unavailable');self.assertIsNone(r['scopes']['direct']['received_events'])
    def test_single_scope_cannot_pass(self):
        for kind in ['direct','group']:
            with self.subTest(kind=kind):
                self.live['incoming_by_kind']={'direct':False,'group':False};self.live['live_event_receipts']={};self.seen(kind)
                r=self.result();self.assertTrue(r['scopes'][kind]['accepted']);self.assertEqual(r['status'],'pending')
    def test_receipt_before_ready_is_rejected(self):
        self.seen('group');self.live['live_event_receipts']['group']['received_at']='2020-01-02T21:31:00+08:00'
        self.assertFalse(self.result()['scopes']['group']['accepted'])
    def test_offsets_use_time_not_string_order(self):
        self.seen('group');self.live['live_event_receipts']['group']['received_at']='2020-01-02T13:40:00Z'
        self.assertTrue(self.result()['scopes']['group']['accepted'])
    def test_old_check_does_not_bind_to_new_episode(self):
        self.seen('group');self.live['subscription_checked_at']='2020-01-02T20:00:00+08:00'
        r=self.result();self.assertFalse(r['evidence_current_episode']);self.assertFalse(r['scopes']['group']['accepted'])
    def test_both_messages_still_require_subscription_and_completion(self):
        self.seen('direct');self.seen('group');self.assertEqual(self.result()['status'],'pending')
        self.live.update(subscriptions_valid=True,subscription_verification='live_delivery',live_acceptance='passed',live_acceptance_at='2020-01-02T22:02:42+08:00')
        r=self.result();self.assertEqual(r['status'],'passed');self.assertNotIn('must-not-leak',json.dumps(r))
    def test_counts_explain_old_events_without_accepting(self):
        p=self.root/'dingtalk-chat/state/chat.sqlite';p.parent.mkdir(parents=True)
        with sqlite3.connect(p) as c:
            c.executescript('CREATE TABLE event_inbox(event_kind TEXT,message_id TEXT,received_at TEXT);CREATE TABLE messages(message_id TEXT,create_time TEXT);')
            c.execute("INSERT INTO messages VALUES('m','2020-01-02 21:31:30')")
            c.executemany('INSERT INTO event_inbox VALUES(?,?,?)',[('group','m','2020-01-02T21:35:11+08:00'),('group','m','2020-01-02T21:35:12+08:00')])
        before=p.read_bytes();r=self.result();self.assertEqual(r['scopes']['group']['received_events'],2);self.assertEqual(r['scopes']['group']['older_message_events'],2);self.assertFalse(r['scopes']['group']['accepted']);self.assertEqual(p.read_bytes(),before)
    def test_handoff_carries_safe_episode_evidence(self):
        detail={'kind':'source','observation':{'listener_acceptance':self.result()},'attention_state':'waiting'}
        out=handoff_context.operational(self.config,detail,self.root)
        self.assertEqual(out['context_version'],2)
        import codex_handoff
        safe=codex_handoff.clean({'problem':out})
        receipt=safe['problem']['members'][0]['observation']['listener_acceptance']
        self.assertEqual(receipt['direct_accepted'],False)
        self.assertEqual(receipt['group_accepted'],False)
        self.assertEqual(receipt['historical_accepted_at'],self.live['live_acceptance_at'])
        self.assertNotIn('[内容已省略]',json.dumps(safe,ensure_ascii=False))
        self.assertNotIn('scopes',receipt)

if __name__=='__main__':unittest.main()
