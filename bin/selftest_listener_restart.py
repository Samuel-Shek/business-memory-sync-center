"""Offline restart contracts. No subscription, message or production state writes."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import dingtalk_batch_contract as contract
import dingtalk_memory_automation as memory
import listener_manual_acceptance as manual
import maintenance_mode as maintenance
from listener_retry_budget import BudgetBlocked


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = {'memory_root': str(root/'vault/00_业务记忆库'), 'external_root': str(root/'external'),
                       'dingtalk_chat': {'dws_bin': str(root/'dws')},
                       'dingtalk_automation': {'scope': 'fixture', 'event_runtime_root': str(root/'runtime')}}
        (root/'dws').write_text('not an executable provider')
        (root/'dws').chmod(0o700)
        self.root = manual.directory(self.config)
        self.now = datetime.now(timezone.utc)
        self.action_id = str(uuid.uuid4())
        self.legacy = manual.legacy_path(self.config, 'all-direct')
        contract.save(self.legacy, {'schema_version': 1, 'attempt_count': 2, 'max_attempts': 2, 'state': 'failed'})
        self.original = self.legacy.read_bytes()
        self.proof_path = self.root/'listener-cause-correction.json'
        contract.save(self.proof_path, {'schema_version': 1, 'cause_identified': True, 'corrected': True,
                      'verification': 'read_only_preflight', 'before_error_code': 'fixture_cause',
                      'binding': manual.binding(self.config), 'corrected_at': manual.now(),
                      'diagnosis_sha256': 'a'*64, 'correction_sha256': 'b'*64})
        receipt = manual.approve(self.config, self.action_id)
        receipt = manual.take(self.config)
        for kind in ('all-direct', 'all-group'):
            budget = manual.ManualBudget(self.config, kind, receipt)
            budget.reserve()
            budget.ready()
            budget.stopped()
        manual.finish(self.config, receipt, 'accepted')
        self.receipt_path = self.root/'listener-manual-acceptance.json'
        self.accepted_bytes = self.receipt_path.read_bytes()

    def budget(self, kind='all-direct'):
        item = manual.operational_budgets(self.config)[kind]
        item.clock = lambda: self.now
        return item

    def assert_preserved(self):
        self.assertEqual(self.legacy.read_bytes(), self.original)
        self.assertEqual(self.receipt_path.read_bytes(), self.accepted_bytes)

    def test_keychain_timeout_keeps_safe_cause_without_relaxing_budget(self):
        for stage in ('load access token', 'load OAuth identity metadata'):
            line = ('● Error: event consume --as user: ' + stage + ': load from keychain: '
                    'read DEK from macOS Keychain: context deadline exceeded')
            result = memory.safe_event_failure([line])
            self.assertEqual(result['error_codes'], ['event_oauth_keychain_read_timeout'])
            self.assertIsNone(result['retryable'])
        unrelated = memory.safe_event_failure(['token=PRIVATE_VALUE context deadline exceeded'])
        self.assertEqual(unrelated['error_codes'], [])
        self.assertNotIn('PRIVATE_VALUE', json.dumps(unrelated))
        terminal = memory.safe_event_failure(['{"error":{"reason":"terminal_hold","retryable":false}}'])
        self.assertFalse(terminal['retryable'])
        self.assert_preserved()

    def test_accepted_listener_restarts_and_preserves_old_evidence(self):
        for _ in range(3):
            b = self.budget()
            self.assertEqual(b.reserve()['attempt_count'], 1)
            b.ready()
            b.stopped()
        self.assert_preserved()

    def test_unaccepted_one_shot_is_not_reused(self):
        row = json.loads(self.accepted_bytes)
        for state in ['failed', 'starting', 'ready_acceptance_pending', 'approved']:
            contract.save(self.receipt_path, {**row, 'state': state})
            self.assertIsNone(manual.operational_budgets(self.config))
        with self.assertRaises(BudgetBlocked):
            manual.approve(self.config, str(uuid.uuid4()))

    def test_failure_budget_survives_restarts_and_failed_stops(self):
        for _ in range(2):
            b = self.budget()
            b.reserve()
            b.failed(None)
            b.stopped()
            self.now += timedelta(seconds=2)
        with self.assertRaises(BudgetBlocked):
            self.budget().reserve()
        self.assert_preserved()

    def test_crash_after_reserve_does_not_refill(self):
        self.budget().reserve()
        self.budget().reserve()
        with self.assertRaises(BudgetBlocked):
            self.budget().reserve()

    def test_later_provider_hold_cannot_be_bypassed(self):
        b = self.budget()
        b.reserve()
        b.failed(True, guard='terminal_hold')
        b.stopped()
        with self.assertRaises(BudgetBlocked):
            self.budget().reserve()

    def test_original_budget_drift_fails_without_new_attempt(self):
        contract.save(self.legacy, {'schema_version': 1, 'attempt_count': 2, 'max_attempts': 2,
                                  'state': 'failed', 'guard': 'terminal_hold'})
        with self.assertRaises(BudgetBlocked):
            self.budget()
        self.assertFalse(list(self.root.glob('listener-operational-*.json')))

    def test_missing_or_failed_scope_is_not_completed_acceptance(self):
        path = self.root/('listener-manual-'+self.action_id+'-all-group.json')
        original = path.read_bytes()
        for data in [{'schema_version': 1}, {**json.loads(original), 'state': 'failed'},
                     {**json.loads(original), 'last_error_code': 'event_failure'}]:
            contract.save(path, data)
            with self.assertRaises(BudgetBlocked):
                self.budget()

    def test_binary_or_cause_proof_drift_blocks_restart(self):
        self.proof_path.write_text('{}')
        with self.assertRaises(BudgetBlocked):
            self.budget()

    def test_runtime_change_cannot_rebind_existing_budget(self):
        self.budget().reserve()
        self.config['dingtalk_automation']['event_runtime_root'] += '-changed'
        with self.assertRaises(BudgetBlocked):
            self.budget().reserve()

    def test_ready_owner_must_be_dead_before_new_episode(self):
        b = self.budget()
        b.reserve()
        b.ready()
        with self.assertRaises(BudgetBlocked):
            b.reserve()
        self.assertEqual(b.read()['attempt_count'], 1)
        with patch.object(manual.OperationalBudget, 'owner_alive', return_value=False):
            self.assertEqual(b.reserve()['attempt_count'], 1)
        self.assertTrue(list((self.root/'listener-budget-history').glob('*.json')))
        self.assert_preserved()

    def legacy_failed_restart(self):
        b = self.budget()
        b.reserve()
        b.ready()
        prior = b.read()
        self.now += timedelta(seconds=10)
        # Reproduce the old implementation carrying successful count forward.
        row = {**prior, 'state': 'starting', 'attempt_count': prior['attempt_count'] + 1,
               'attempt_at': self.now.isoformat()}
        b.save(row)
        b.failed(None)
        self.now += timedelta(seconds=2)
        return b, prior

    def test_legacy_correction_preserves_failure_and_allows_only_remaining_attempt(self):
        b, prior = self.legacy_failed_restart()
        before = b.read()
        with patch.object(manual.OperationalBudget, 'owner_alive', return_value=False):
            corrected = b.reconcile_pre_ready_attempts(prior, 'a'*64)
            self.assertEqual(corrected['attempt_count'], 1)
            self.assertEqual(corrected['max_attempts'], before['max_attempts'])
            self.assertEqual(corrected['last_failure_at'], before['last_failure_at'])
            self.assertEqual(corrected['last_error_code'], before['last_error_code'])
            self.assertEqual(b.reconcile_pre_ready_attempts(prior, 'a'*64), corrected)
            self.assertEqual(b.reserve()['attempt_count'], 2)
            b.failed(None)
            self.now += timedelta(seconds=2)
            with self.assertRaises(BudgetBlocked): b.reserve()
        history = list((self.root/'listener-budget-history').glob('*.json'))
        self.assertEqual(json.loads(history[0].read_text())['budget'], before)
        self.assert_preserved()

    def test_correction_rejects_guards_changed_evidence_and_unknown_history(self):
        b, prior = self.legacy_failed_restart()
        original = b.read()
        invalid = [{**original, 'guard': 'terminal_hold'},
                   {**original, 'max_attempts': 1},
                   {**original, 'attempt_count': 3},
                   {**original, 'ready_at': None}]
        with patch.object(manual.OperationalBudget, 'owner_alive', return_value=False):
            for row in invalid:
                b.save(row)
                with self.assertRaises(BudgetBlocked): b.reconcile_pre_ready_attempts(prior, 'a'*64)
                self.assertEqual(b.read(), row)
            b.save(original)
            with self.assertRaises(BudgetBlocked):
                b.reconcile_pre_ready_attempts({**prior, 'accepted_anchor': {}}, 'a'*64)
        self.assert_preserved()

    def test_budget_exhaustion_parks_without_hiding_failure(self):
        updates = []
        class Consumer:
            def __init__(self, config, kind, lines, stop):
                self.kind, self.process, self.budget = kind, None, None
            def start_with_budget(self):
                raise memory.AutomationError('event_retry_budget_exhausted', 'fixture')
            def shutdown(self): pass
        with patch.object(memory, 'EventConsumer', Consumer), \
             patch.object(memory, 'acquire_lock', return_value=io.StringIO()), \
             patch.object(memory, 'status_update', side_effect=lambda config, **x: updates.append(x)), \
             patch.object(memory, 'append_log'), patch.object(memory, 'raise_alert') as alert:
            self.assertEqual(memory.run_listener(self.config), 0)
        alert.assert_called_once()
        self.assertTrue(any(x.get('listener_restart_policy') == 'manual_required' for x in updates))
        self.assertEqual(updates[-1]['listener_state'], 'failed')
        self.assert_preserved()

    def test_admission_respects_completed_ready_episode_without_mutating(self):
        b, prior = self.legacy_failed_restart()
        b.ready()
        before = b.path.read_bytes()
        consumer = type('Consumer', (), {'budget': b})()
        with patch.object(manual.OperationalBudget, 'owner_alive', return_value=False):
            self.assertIsNone(memory.listener_admission([consumer]))
        self.assertEqual(b.path.read_bytes(), before)
        with patch.object(manual.OperationalBudget, 'owner_alive', return_value=True):
            self.assertEqual(memory.listener_admission([consumer]), 'event_listener_owner_alive')
        b.failed(None)
        b.save({**b.read(), 'next_retry_at': (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
        self.assertEqual(memory.listener_admission([consumer]), 'event_retry_budget_exhausted')
        self.assert_preserved()

    def test_correction_preserves_cooldown_and_requires_known_dead_owner(self):
        b, prior = self.legacy_failed_restart()
        original = b.read()
        b.save({**original, 'next_retry_at': (self.now+timedelta(hours=1)).isoformat()})
        with patch.object(manual.OperationalBudget, 'owner_alive', return_value=False):
            with self.assertRaises(BudgetBlocked): b.reconcile_pre_ready_attempts(prior, 'a'*64)
        b.save(original)
        with patch.object(manual.OperationalBudget, 'owner_alive', return_value=True):
            with self.assertRaises(BudgetBlocked): b.reconcile_pre_ready_attempts(prior, 'a'*64)
        self.assertEqual(b.read(), original)
        self.assert_preserved()

    def test_maintenance_stops_before_touching_unaccepted_listener(self):
        contract.save(self.root/'automation_status.json', {'listener_state': 'ready'})
        row = json.loads(self.accepted_bytes)
        contract.save(self.receipt_path, {**row, 'state': 'ready_acceptance_pending'})
        with self.assertRaises(BudgetBlocked):
            manual.maintenance_restart_preflight(self.config)
        contract.save(self.receipt_path, row)
        manual.maintenance_restart_preflight(self.config)
        self.assertFalse(list(self.root.glob('listener-operational-*.json')))

    def test_listener_uses_operational_budget_and_graceful_stop(self):
        handlers, selected = {}, []
        class Consumer:
            def __init__(self, config, kind, lines, stop):
                self.kind, self.budget, self.process = kind, None, None
                self.subscription_ref = 'fixture'
            def start_with_budget(self):
                selected.append(self.budget)
                self.budget.reserve()
                self.budget.ready()
                if self.kind == 'all-group':
                    handlers[memory.signal.SIGTERM](memory.signal.SIGTERM, None)
            def shutdown(self):
                pass
        def signal(sig, handler):
            handlers[sig] = handler
            return None
        with patch.object(memory, 'EventConsumer', Consumer), \
             patch.object(memory, 'acquire_lock', return_value=io.StringIO()), \
             patch.object(memory, 'status_update'), patch.object(memory, 'append_log'), \
             patch.object(memory.signal, 'signal', side_effect=signal), \
             patch.object(memory.threading, 'Thread', return_value=Mock()):
            self.assertEqual(memory.run_listener(self.config), 0)
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(isinstance(b, manual.OperationalBudget) and b.read()['state']=='stopped' for b in selected))
        self.assert_preserved()

    def test_maintenance_rejects_before_changing_service_state(self):
        with patch.object(maintenance, 'read_state', return_value=None), \
             patch.object(maintenance, 'unregistered_candidates', return_value=[]), \
             patch.object(maintenance, 'unregistered_writer_processes', return_value=[]), \
             patch.object(manual, 'maintenance_restart_preflight', side_effect=BudgetBlocked('pending_acceptance')), \
             patch.object(maintenance, 'atomic_json') as save, patch.object(maintenance, 'bootout') as stop:
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.enter(self.config, 'fixture')
            save.assert_not_called()
            stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
