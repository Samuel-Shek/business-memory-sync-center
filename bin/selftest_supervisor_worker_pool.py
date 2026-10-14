"""Isolated scheduler acceptance: no production workers, databases or network."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import threading
import time
import unittest
from unittest.mock import patch
import supervisor_worker_pool as pool


class PoolTests(unittest.TestCase):
    def test_long_mail_does_not_block_next_source_and_owner_keeps_pulsing(self):
        release = threading.Event()
        main = threading.get_ident()
        pulses = []
        records = []
        def execute(source):
            self.assertNotEqual(threading.get_ident(), main)
            if source == "dingtalk_mail":
                self.assertTrue(release.wait(2))
            else:
                release.set()
            return {"status": "noop"}
        def owner(active):
            self.assertEqual(threading.get_ident(), main)
            pulses.append(tuple(active))
        def record(source, receipt):
            self.assertEqual(threading.get_ident(), main)
            records.append(source)
            return source
        result = pool.run(["dingtalk_mail"], execute=execute, record=record,
                          eligible=lambda active: ["win11"], pulse=owner,
                          interval=.005, rescan_interval=.03)
        self.assertEqual(set(result), {"dingtalk_mail", "win11"})
        self.assertGreaterEqual(len(pulses), 3)
        self.assertEqual(len(records), 2)

    def test_duplicate_source_is_never_submitted_while_inflight(self):
        counts = {}
        def execute(source):
            counts[source] = counts.get(source, 0) + 1
            time.sleep(.05)
            return {}
        pool.run(["mail", "mail"], execute=execute, record=lambda s,r:s,
                 eligible=lambda active:["mail"], pulse=lambda a:None,
                 interval=.003, rescan_interval=.005)
        self.assertEqual(counts, {"mail": 1})

    def test_ambiguous_result_stops_new_work_but_drains_existing_worker(self):
        ended = threading.Event()
        launches = []
        def execute(source):
            launches.append(source)
            if source == "slow":
                time.sleep(.04)
                ended.set()
            return {}
        def record(source, receipt):
            if source == "fast":
                raise RuntimeError("commit_unknown")
            return source
        with self.assertRaisesRegex(RuntimeError, "commit_unknown"):
            pool.run(["fast", "slow"], execute=execute, record=record,
                     eligible=lambda active:["unexpected"], pulse=lambda a:None,
                     interval=.003, rescan_interval=.005)
        self.assertTrue(ended.is_set())
        self.assertNotIn("unexpected", launches)

    def test_initial_prepare_failure_drains_first_worker_with_pulses(self):
        recorded=[];pulses=[]
        def prepare(source):
            if source=="bad":raise OSError("ledger_busy")
        def execute(source):time.sleep(.04);return {}
        with self.assertRaisesRegex(OSError,"ledger_busy"):
            pool.run(["good","bad"],execute=execute,record=lambda source,r:recorded.append(source),
                eligible=lambda active:[],pulse=lambda active:pulses.append(active),
                prepare=prepare,interval=.005)
        self.assertEqual(recorded,["good"])
        self.assertGreater(len(pulses),2)

    def test_shared_group_pending_source_runs_after_last_worker_finishes(self):
        pending={"minutes"};seen=[]
        def more(active):return list(pending) if not active else []
        def prepare(source):pending.discard(source)
        result=pool.run(["chat"],execute=lambda source:{},record=lambda source,r:seen.append(source),
            eligible=lambda active:[],pulse=lambda active:None,prepare=prepare,continuations=more)
        self.assertEqual(seen,["chat","minutes"])

    def test_pulse_failure_does_not_abandon_or_duplicate_worker(self):
        ended = threading.Event()
        def execute(source):
            time.sleep(.03)
            ended.set()
            return {}
        def pulse(active):
            raise OSError("receipt_unwritable")
        with self.assertRaises(OSError):
            pool.run(["one"], execute=execute, record=lambda s,r:s,
                     eligible=lambda a:["two"], pulse=pulse,
                     interval=.005,rescan_interval=.01)
        self.assertTrue(ended.is_set())


if __name__ == "__main__":
    unittest.main()
