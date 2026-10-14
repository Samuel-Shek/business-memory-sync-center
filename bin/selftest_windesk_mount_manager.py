
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import windesk_mount_manager as m


class FakeSystem:
    def __init__(self, mount=m.ZT, probe=0, unmount=0, lsof=(1, ''), helper=0):
        self.mount, self.probe, self.unmount = mount, probe, unmount
        self.lsof, self.helper, self.calls = lsof, helper, []

    def run(self, argv, timeout, capture=False):
        self.calls.append(argv)
        if argv[0] == '/sbin/mount':
            text = '' if self.mount is None else f'//Example User@{self.mount}/DeskShare on {m.MOUNT} (smbfs, nodev)'
            return 0, text
        if '--probe' in argv:
            return self.probe, ''
        if argv[0] == '/sbin/umount':
            if self.unmount == 0 or '-f' in argv:
                self.mount = None
                return 0, ''
            return self.unmount, ''
        if argv[0] == '/usr/sbin/lsof':
            return self.lsof
        if argv[0] in (str(m.HELPER), str(m.LAN_HELPER)):
            if not self.helper:
                self.mount = m.LAN if argv[1] == 'mount-lan' else m.ZT
                self.probe = 0
            return self.helper, ''
        if argv[0] == '/bin/ps':
            return 1, ''
        raise AssertionError(argv)


class MountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        helper = self.root/'helper'; helper.write_text(''); helper.chmod(0o700)
        self.patches = [patch.object(m, 'MOUNT', self.root/'mount'),
                        patch.object(m, 'MAINTENANCE', self.root/'maintenance.json'),
                        patch.object(m, 'WATCHER_HEALTH', self.root/'watcher.json'),
                        patch.object(m, 'HELPER', helper),
                        patch.object(m, 'LAN_HELPER', helper),
                        patch.object(m, 'emit', lambda *a, **kw: None)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def test_healthy_mount_does_not_remount(self):
        system = FakeSystem(); state = {}
        m.maintain(state, {}, system.run, 1000)
        self.assertEqual(state['status'], 'healthy')
        self.assertFalse(any(a[0]=='/sbin/umount' for a in system.calls))

    def test_first_timeout_waits_second_recovers(self):
        system = FakeSystem(probe=124); state = {}
        m.maintain(state, {}, system.run, 1000)
        self.assertEqual(state['probe_failures'], 1)
        self.assertFalse(any(a[0]=='/sbin/umount' for a in system.calls))
        m.maintain(state, {}, system.run, 1030)
        self.assertEqual(state['status'], 'healthy')
        self.assertEqual(state['recoveries'], 1)
        self.assertEqual(json.loads(m.MAINTENANCE.read_text())['expires_at'], 0)

    def test_busy_mount_with_other_user_is_never_forced(self):
        system = FakeSystem(probe=124, unmount=1, lsof=(0, 'p123\ncMicrosoft Word\nf5\naw\n'))
        state = {'probe_failures': 1}
        m.maintain(state, {}, system.run, 1000)
        self.assertEqual(state['status'], 'recovery_deferred')
        self.assertFalse(any('-f' in a for a in system.calls))
        count = len(system.calls)
        m.maintain(state, {}, system.run, 1010)
        self.assertEqual(state['status'], 'backoff')
        self.assertEqual(len(system.calls)-count, 1)

    def test_inconclusive_open_file_check_is_never_forced(self):
        for evidence in [(124, ''), (1, 'permission denied')]:
            system = FakeSystem(probe=124, unmount=1, lsof=evidence)
            state = {'probe_failures': 1}
            m.maintain(state, {}, system.run, 1000)
            self.assertFalse(any('-f' in a for a in system.calls))

    def test_force_requires_two_failures_and_no_other_handles(self):
        system = FakeSystem(probe=124, unmount=1)
        state = {'probe_failures': 1}
        m.maintain(state, {}, system.run, 1000)
        self.assertTrue(any('-f' in a for a in system.calls))
        self.assertEqual(state['status'], 'healthy')

    def test_wrong_mount_identity_never_touched(self):
        system = FakeSystem(mount='1.2.3.4'); state = {}
        m.maintain(state, {}, system.run, 1000)
        self.assertEqual(state['status'], 'blocked_wrong_mount_identity')
        self.assertEqual(len(system.calls), 1)

    def test_missing_mount_recovers(self):
        system = FakeSystem(mount=None); state = {}
        m.maintain(state, {}, system.run, 1000)
        self.assertEqual(state['status'], 'healthy')

    def test_locked_keychain_stays_in_backoff(self):
        system = FakeSystem(mount=None, helper=20); state = {}
        m.maintain(state, {}, system.run, 1000)
        self.assertEqual(state['helper_exit'], 20)
        self.assertEqual(state['retry_after'], 1030)
        self.assertEqual(sum(a[0]==str(m.HELPER) for a in system.calls), 1)

    def test_lan_requires_enablement_and_matching_identity(self):
        cfg = {'lan_enabled': True, 'expected_smb_guid': 'known-server'}
        self.assertEqual(m.select_server({}, lambda _: 'known-server'), m.ZT)
        self.assertEqual(m.select_server(cfg, lambda _: None), m.ZT)
        self.assertEqual(m.select_server(cfg, lambda _: 'other-server'), m.ZT)
        self.assertEqual(m.select_server(cfg, lambda _: 'known-server'), m.LAN)

    def test_lan_switch_waits_for_three_observations(self):
        system = FakeSystem(); state = {}
        cfg = {'lan_enabled': True, 'expected_smb_guid': 'known-server'}
        for now in (1000, 1030):
            m.maintain(state, cfg, system.run, now, lambda _: 'known-server')
            self.assertEqual(system.mount, m.ZT)
        m.maintain(state, cfg, system.run, 1060, lambda _: 'known-server')
        self.assertEqual(system.mount, m.LAN)

    def test_healthy_busy_mount_is_not_forced_for_speed(self):
        system = FakeSystem(unmount=1); state = {'preferred_samples': 2}
        cfg = {'lan_enabled': True, 'expected_smb_guid': 'known-server'}
        m.maintain(state, cfg, system.run, 1000, lambda _: 'known-server')
        self.assertFalse(any('-f' in a for a in system.calls))

    def test_child_timeout_returns_promptly_and_kills_child(self):
        state = {}; start = time.monotonic()
        code, _ = m.Runner(state).run([sys.executable, '-c', 'import time; time.sleep(10)'], 0.05)
        self.assertEqual(code, 124)
        self.assertLess(time.monotonic()-start, 2)
        self.assertNotIn('lingering_child', state)

    def test_performance_switch_failure_preserves_backoff(self):
        system = FakeSystem(unmount=1); state = {'preferred_samples': 2}
        cfg = {'lan_enabled': True, 'expected_smb_guid': 'known-server'}
        m.maintain(state, cfg, system.run, 1000, lambda _: 'known-server')
        self.assertEqual(state['retry_after'], 1030)
        m.maintain(state, cfg, system.run, 1031, lambda _: 'known-server')
        self.assertEqual(state['retry_after'], 1091)

    def test_local_mountpoint_symlink_is_rejected(self):
        target = self.root/'elsewhere'; target.mkdir()
        m.MOUNT.symlink_to(target)
        system = FakeSystem(mount=None); state = {}
        m.maintain(state, {}, system.run, 1000)
        self.assertEqual(state['reason'], 'unsafe_local_mountpoint')
        self.assertFalse(any(a[0]==str(m.HELPER) for a in system.calls))

    def test_source_access_denied_does_not_remount(self):
        system = FakeSystem(probe=65); state = {'probe_failures': 2}
        m.maintain(state, {}, system.run, 1000)
        self.assertEqual(state['status'], 'source_access_unavailable')
        self.assertFalse(any(a[0]=='/sbin/umount' for a in system.calls))

    def test_cached_probe_cannot_mask_stalled_observer(self):
        m.WATCHER_HEALTH.write_text(json.dumps({'status':'offline',
            'last_error':{'code':'scan_timeout','at':'1970-01-01T00:15:00+00:00'}, 'consecutive_scan_timeouts':2}))
        system = FakeSystem(); state = {'probe_failures': 1}
        m.maintain(state, {}, system.run, 1000)
        self.assertTrue(any(a[0]=='/sbin/umount' for a in system.calls))
        self.assertEqual(state['status'], 'healthy')
        unmounts = sum(a[0]=='/sbin/umount' for a in system.calls)
        m.maintain(state, {}, system.run, 1030)
        self.assertEqual(sum(a[0]=='/sbin/umount' for a in system.calls), unmounts)


if __name__ == '__main__':
    unittest.main()
