#!/usr/bin/env python3
"""Stale recovery notices must not starve open alerts; nothing is really sent."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import business_sync_state as state
import business_sync_supervisor as supervisor

NOW = datetime.fromisoformat("2020-01-02T22:00:00+08:00")


class DeliveryStarvationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="business-delivery-test-")
        self.database = Path(self.temporary.name) / "state.sqlite"
        state.initialize(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def recovery(self, category, at):
        state.open_alert("win11", category, "Win11 故障", "测试", database=self.database, at=at - timedelta(minutes=5))
        state.resolve_alert("win11", category, title="Win11已恢复", detail="测试", database=self.database, at=at)

    def rows(self):
        return {row["title"] + ":" + row["category"]: row for row in state.alerts(self.database)}

    def test_open_alert_is_served_before_old_recovery_backlog(self):
        for index in range(25):
            self.recovery(f"sync_failure_{index}", NOW - timedelta(days=10, minutes=-index))
        state.open_alert("dingtalk_mail", "sync_failure", "钉钉邮件持续失败", "测试", severity="error",
                         database=self.database, at=NOW - timedelta(days=4))
        due = state.due_alerts(self.database, at=NOW)
        self.assertEqual(len(due), 20)
        self.assertEqual(due[0]["title"], "钉钉邮件持续失败")

    def test_stale_recovery_is_retired_without_sending_and_recent_one_is_kept(self):
        self.recovery("sync_failure_old", NOW - timedelta(days=10))
        self.recovery("sync_failure_new", NOW - timedelta(hours=1))
        sent, bridge = [], []
        with mock.patch.object(supervisor, "deliver_macos", lambda alert, db=None: sent.append(("macos", alert["category"]))), \
             mock.patch.object(supervisor, "deliver_miso", lambda alert, db=None: sent.append(("miso", alert["category"]))), \
             mock.patch.object(supervisor, "recheck_macos_notifications", lambda *a, **k: None), \
             mock.patch.object(supervisor, "run_miso_bridge", lambda args: bridge.append(args) or {"ok": True, "retired_unsent": True}), \
             mock.patch.object(state, "now_dt", lambda: NOW), mock.patch.object(supervisor.state, "now_dt", lambda: NOW):
            supervisor._deliver_due_alerts({}, self.database)
        recoveries = [row for row in state.alerts(self.database) if row["category"] == "recovery"]
        old = next(r for r in recoveries if r["first_seen"].startswith("2020-01-02"))
        new = next(r for r in recoveries if r is not old)
        self.assertEqual((old["macos_status"], old["miso_status"], old["miso_error_code"]),
                         ("superseded", "superseded", "stale_recovery_notice"))
        self.assertNotIn(old["alert_id"], [a["alert_id"] for a in state.due_alerts(self.database, at=NOW)])
        self.assertTrue(sent and all(category == "recovery" for _, category in sent))
        self.assertEqual(len(sent), 2)  # the recent notice only: one macOS, one Miso
        self.assertEqual(new["macos_status"], "pending")
        self.assertEqual(bridge, [])  # no Miso event existed, so nothing to retire

    def test_unsent_miso_event_is_retired_but_uncertain_one_is_left_alone(self):
        self.recovery("sync_failure_old", NOW - timedelta(days=10))
        alert = next(r for r in state.alerts(self.database) if r["category"] == "recovery")
        state.mark_delivery(alert["alert_id"], "miso", "pending", event_id="miso_" + "a" * 64,
                            error_code="policy_evidence_missing", database=self.database)
        alert = next(r for r in state.alerts(self.database) if r["category"] == "recovery")
        with mock.patch.object(supervisor, "run_miso_bridge", lambda args: {"ok": True, "retired_unsent": False}):
            self.assertTrue(supervisor.retire_stale_recovery(alert, self.database, now=NOW))
        kept = next(r for r in state.alerts(self.database) if r["category"] == "recovery")
        self.assertEqual(kept["miso_status"], "pending")  # Miso could not prove it unsent
        self.assertEqual(kept["macos_status"], "superseded")


if __name__ == "__main__":
    unittest.main()
