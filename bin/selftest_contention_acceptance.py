"""Prove that deployment alone, old successes, and maintenance cannot close faults."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import automatic_memory_validation as validation
import validation_evidence
import supervisor_incidents as incidents
import business_sync_state as state


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        self.db = self.root / "state/sync.sqlite"
        state.initialize(self.db)
        self.binding = {"fixture.py": "a"*64}
        self.native = self.root / "bin/BUSINESS Sync Supervisor.app/Contents/MacOS/business-sync-supervisor-launcher"
        self.native.parent.mkdir(parents=True)
        self.native.write_bytes(b"synthetic executable")
        self.native_hash = hashlib.sha256(self.native.read_bytes()).hexdigest()
        self.test_log = self.root / "tests.log"
        self.test_log.write_text("synthetic passed test evidence")
        self.targets = []
        for minute in (1, 2, 3):
            self.targets.append(incidents.retain(self.root, {
                "recorded_at": f"2020-01-02T10:0{minute}:00+08:00",
                "reason": "sqlite_database_busy", "error_type": "OperationalError", "frames": []}))
        incidents.register_pending(self.root, self.db)
        self.targets = [json.loads((incidents.inbox(self.root) / (i["incident_id"] + ".json")).read_text()) for i in self.targets]
        self.proof = {"schema_version": 1, "reason": "bounded_ledger_contention_corrected",
                      "isolated_reproduction": True, "deployed_at": "2020-01-02T10:10:00+08:00",
                      "code_binding": self.binding, "native_binary_sha256": self.native_hash,
                      "incident_ids": [i["incident_id"] for i in self.targets], "alert_id": self.targets[0]["alert_id"],
                      "test_evidence": [{"path": str(self.test_log), "checks": 1,
                                         "sha256": hashlib.sha256(self.test_log.read_bytes()).hexdigest()}]}
        self.save("supervisor-contention-correction-proof.json", self.proof)
        self.save("maintenance-mode.json", {"status": "inactive"})
        self.save("sync-supervisor-last-error.json", self.targets[-1]["evidence"])
        self.rows = [{"pid": n, "checked_at": f"2020-01-02T10:1{n}:00+08:00",
                      "attention_reconciliation": "passed", "execution_owner": "launchd_native_supervisor",
                      "code_binding": self.binding, "native_binary_sha256": self.native_hash} for n in (1, 2)]
        self.save("supervisor-attention-receipts.json", {"receipts": self.rows})
        self.save("last-memory-validation-success.json", {"recorded_at": "2020-01-02T10:11:00+08:00"})
        for target, name, value in [(incidents, "code_binding", self.binding),
                                     (validation, "index_state", {"dirty": 1, "applied": 1}),
                                     (validation_evidence, "receipt_is_current", True)]:
            p = patch.object(target, name, return_value=value)
            p.start(); self.addCleanup(p.stop)

    def save(self, name, value):
        (self.root / "state" / name).write_text(json.dumps(value))

    def assert_pending(self):
        self.assertEqual(incidents.verify_correction(self.root, self.db), 0)
        self.assertEqual(incidents.unresolved(self.root), 3)
        self.assertEqual(state.alerts(self.db)[0]["status"], "open")

    def test_complete_evidence_closes_only_bound_failure_preserving_originals(self):
        original = [i["evidence"] for i in self.targets]
        state.open_alert("system", "unrelated", "fixture", "fixture", database=self.db)
        self.assertEqual(incidents.verify_correction(self.root, self.db), 3)
        self.assertEqual(incidents.unresolved(self.root), 0)
        current = [json.loads((incidents.inbox(self.root)/(i["incident_id"]+".json")).read_text()) for i in self.targets]
        self.assertEqual(original, [i["evidence"] for i in current])
        self.assertTrue(all(i["historical_lock_owner"] == "unknown" for i in current))
        self.assertTrue(any(a["category"] == "unrelated" and a["status"] == "open" for a in state.alerts(self.db)))
        self.assertEqual(incidents.verify_correction(self.root, self.db), 0)

    def test_maintenance_never_runs_full_validation_or_closes(self):
        self.save("maintenance-mode.json", {"status": "active"})
        with patch.object(validation_evidence, "receipt_is_current", side_effect=AssertionError("must remain paused")):
            self.assert_pending()

    def test_new_error_after_deploy_blocks(self):
        self.save("sync-supervisor-last-error.json", {"recorded_at": "2020-01-02T10:13:00+08:00"})
        self.assert_pending()

    def test_same_process_or_short_gap_cannot_count_as_two_cycles(self):
        self.rows[1]["pid"] = 1
        self.save("supervisor-attention-receipts.json", {"receipts": self.rows})
        self.assert_pending()
        self.rows[1]["pid"] = 2
        self.rows[1]["checked_at"] = "2020-01-02T10:11:20+08:00"
        self.save("supervisor-attention-receipts.json", {"receipts": self.rows})
        self.assert_pending()

    def test_code_or_test_evidence_changes_block(self):
        with patch.object(incidents, "code_binding", return_value={"fixture.py": "b"*64}):
            self.assert_pending()
        self.test_log.write_text("changed")
        self.assert_pending()

    def test_stale_or_predeployment_full_validation_cannot_close(self):
        with patch.object(validation_evidence, "receipt_is_current", return_value=False):
            self.assert_pending()
        self.save("last-memory-validation-success.json", {"recorded_at": "2020-01-02T10:09:00+08:00"})
        self.assert_pending()

    def test_unrelated_unresolved_incident_blocks_aggregate_closure(self):
        incidents.retain(self.root, {"recorded_at": "2020-01-02T10:04:00+08:00", "reason": "unknown"})
        self.assertEqual(incidents.verify_correction(self.root, self.db), 0)
        self.assertEqual(incidents.unresolved(self.root), 4)

    def test_newer_or_unparseable_validation_failure_supersedes_matching_success(self):
        for at in ["2020-01-02T10:12:00+08:00", "2020-01-02T10:11:00+08:00", "invalid"]:
            self.save("last-memory-validation-failure.json", {"recorded_at": at, "verdict": "fail"})
            self.assert_pending()
        self.save("last-memory-validation-failure.json", {"recorded_at": "2020-01-02T10:10:00+08:00", "verdict": "fail"})
        self.assertEqual(incidents.verify_correction(self.root, self.db), 3)

    def test_active_source_writer_leaves_incidents_untouched(self):
        with validation.try_lock(self.root / "state/sync-center-control.lock"):
            self.assert_pending()


if __name__ == "__main__": unittest.main()
