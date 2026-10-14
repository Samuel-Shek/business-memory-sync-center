#!/usr/bin/env python3
"""Isolated real-process flock tests; no sync, model, index or notification I/O.

Run: python3 -B selftest_controller_source_locks.py
Workers import the actual controller with inert downstream modules and use
only temporary lock files. Pipes acknowledge entry/failure without sleep races.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import importlib.util
import multiprocessing
import os
import stat
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


SINGLES = ("dingtalk_mail", "win11", "dingtalk_chat", "dingtalk_minutes", "wechat")
COMBINED = ("all", "dingtalk_all")
CONTROLLER = Path(__file__).with_name("business_sync_controller.py")


def isolated_controller():
    """Execute the real controller, never import its production dependencies."""
    dependencies = {
        name: types.ModuleType(name) for name in (
            "dingtalk_memory_automation", "dingtalk_sync_orchestrator",
            "dingtalk_sync_v2", "system_dashboard_status",
        )
    }
    dependencies["system_dashboard_status"].DashboardPaths = mock.Mock()
    dependencies["system_dashboard_status"].SystemDashboard = mock.Mock()
    spec = importlib.util.spec_from_file_location("isolated_controller", CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, dependencies):
        spec.loader.exec_module(module)
    return module


def guard_worker(root):
    """Fail closed on network, subprocess or writes outside the test directory."""
    def audit(event, args):
        if event.startswith(("socket.", "subprocess.")) or event in {"os.system", "os.posix_spawn"}:
            raise AssertionError(f"forbidden worker action: {event}")
        if event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            )
            if writing and not isinstance(path, int):
                # abspath preserves the final symlink so O_NOFOLLOW itself is tested.
                if not Path(os.path.abspath(path)).is_relative_to(root):
                    raise AssertionError("worker write outside temporary directory")
        if event in {"os.mkdir", "os.remove", "os.rmdir"}:
            if not Path(os.path.abspath(args[0])).is_relative_to(root):
                raise AssertionError("worker mutation outside temporary directory")
    sys.addaudithook(audit)


def lock_worker(connection, lock_path):
    controller = isolated_controller()
    controller.CONTROL_LOCK = Path(lock_path)
    guard_worker(controller.CONTROL_LOCK.parent)
    held = None
    try:
        connection.send(("ready",))
        while True:
            command, target = connection.recv()
            if command == "stop":
                break
            if command == "release":
                held.__exit__(None, None, None)
                held = None
                connection.send(("released",))
                continue
            if command == "body_error":
                try:
                    with controller.control_lock(target):
                        raise RuntimeError("fixture_body_error")
                except RuntimeError as exc:
                    connection.send(("body_error", str(exc)))
                continue
            assert command == "acquire" and held is None
            candidate = controller.control_lock() if target is None else controller.control_lock(target)
            try:
                handle = candidate.__enter__()
            except controller.ControllerError as exc:
                connection.send(("rejected", exc.code, exc.global_stop))
            else:
                held = candidate
                connection.send(("entered", stat.S_IMODE(os.fstat(handle.fileno()).st_mode)))
    finally:
        if held is not None:
            held.__exit__(None, None, None)
        connection.close()


class SourceLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="controller-source-locks-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.lock = self.root / "sync-center-control.lock"
        self.context = multiprocessing.get_context("spawn")

    def receive(self, connection):
        self.assertTrue(connection.poll(8), "worker blocked: lock must be nonblocking")
        return connection.recv()

    @contextmanager
    def worker(self):
        parent, child = self.context.Pipe()
        process = self.context.Process(target=lock_worker, args=(child, str(self.lock)))
        process.start()
        child.close()
        try:
            self.assertEqual(self.receive(parent), ("ready",))
            yield parent
        finally:
            if process.is_alive():
                parent.send(("stop", None))
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(3)
            parent.close()
            self.assertEqual(process.exitcode, 0, "worker crashed or failed to stop")

    def request(self, worker, command, target=None):
        worker.send((command, target))
        return self.receive(worker)

    def enter(self, worker, target):
        self.assertEqual(self.request(worker, "acquire", target), ("entered", 0o600))

    def conflict(self, worker, target):
        self.assertEqual(self.request(worker, "acquire", target), ("rejected", "already_running", True))

    def test_distinct_sources_overlap_both_directions(self):
        with self.worker() as first, self.worker() as second:
            for left in SINGLES:
                for right in SINGLES:
                    if left == right or {left, right} == {"dingtalk_chat", "dingtalk_minutes"}:
                        continue
                    with self.subTest(holder=left, entrant=right):
                        self.enter(first, left)
                        self.enter(second, right)
                        self.request(second, "release")
                        self.request(first, "release")

    def test_same_source_conflicts_and_can_retry_after_release(self):
        with self.worker() as first, self.worker() as second:
            for target in SINGLES:
                with self.subTest(target=target):
                    self.enter(first, target)
                    self.conflict(second, target)
                    self.request(first, "release")
                    self.enter(second, target)
                    self.request(second, "release")

    def test_chat_and_minutes_conflict_both_directions(self):
        with self.worker() as first, self.worker() as second:
            for left, right in (("dingtalk_chat", "dingtalk_minutes"), ("dingtalk_minutes", "dingtalk_chat")):
                with self.subTest(holder=left):
                    self.enter(first, left)
                    self.conflict(second, right)
                    self.request(first, "release")

    def test_combined_excludes_singles_both_directions(self):
        with self.worker() as first, self.worker() as second:
            for combined in COMBINED:
                for single in SINGLES:
                    for left, right in ((combined, single), (single, combined)):
                        with self.subTest(holder=left, entrant=right):
                            self.enter(first, left)
                            self.conflict(second, right)
                            self.request(first, "release")
                            self.enter(second, right)
                            self.request(second, "release")

    def test_combined_excludes_combined(self):
        with self.worker() as first, self.worker() as second:
            for left in COMBINED:
                for right in COMBINED:
                    with self.subTest(holder=left, entrant=right):
                        self.enter(first, left)
                        self.conflict(second, right)
                        self.request(first, "release")

    def test_legacy_no_argument_excludes_every_target_both_directions(self):
        with self.worker() as first, self.worker() as second:
            for target in (*SINGLES, *COMBINED, None):
                for left, right in ((None, target), (target, None)):
                    with self.subTest(holder=left, entrant=right):
                        self.enter(first, left)
                        self.conflict(second, right)
                        self.request(first, "release")

    def test_source_conflict_releases_gate_while_failed_worker_stays_alive(self):
        with self.worker() as holder, self.worker() as failed, self.worker() as exclusive:
            self.enter(holder, "dingtalk_mail")
            self.conflict(failed, "dingtalk_mail")
            self.request(holder, "release")
            # Failed process is still alive: process exit cannot hide a leaked SH gate.
            self.enter(exclusive, "all")

    def test_source_open_failure_releases_gate(self):
        source = self.root / "sync-center-control-mail.lock"
        source.mkdir()
        with self.worker() as failed, self.worker() as exclusive:
            self.assertEqual(self.request(failed, "acquire", "dingtalk_mail"),
                             ("rejected", "control_lock_unavailable", True))
            self.enter(exclusive, "all")

    def test_body_exception_releases_both_locks(self):
        with self.worker() as failed, self.worker() as next_worker:
            self.assertEqual(self.request(failed, "body_error", "dingtalk_mail"),
                             ("body_error", "fixture_body_error"))
            self.enter(next_worker, "all")
            self.request(next_worker, "release")
            self.enter(next_worker, "dingtalk_mail")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "platform has no O_NOFOLLOW")
    def test_gate_and_source_symlinks_rejected_without_touching_destination(self):
        destination = self.root / "untouched"
        destination.write_text("fixture")
        destination.chmod(0o644)
        for path in (self.lock, self.root / "sync-center-control-mail.lock"):
            with self.subTest(path=path.name):
                path.symlink_to(destination)
                with self.worker() as failed:
                    self.assertEqual(self.request(failed, "acquire", "dingtalk_mail"),
                                     ("rejected", "control_lock_unavailable", True))
                    if path != self.lock:
                        with self.worker() as exclusive:
                            self.enter(exclusive, "all")
                self.assertEqual(destination.read_text(), "fixture")
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)
                path.unlink()

    def test_gate_and_source_permissions_are_private(self):
        source = self.root / "sync-center-control-mail.lock"
        for path in (self.lock, source):
            path.touch(mode=0o666)
            path.chmod(0o666)
        with self.worker() as worker:
            self.enter(worker, "dingtalk_mail")
            for path in (self.lock, source):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_permission_failure_releases_all_opened_locks(self):
        controller = isolated_controller()
        controller.CONTROL_LOCK = self.lock
        real_fchmod = os.fchmod
        calls = 0

        def fail_source(descriptor, mode):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("fixture")
            return real_fchmod(descriptor, mode)

        with mock.patch.object(controller.os, "fchmod", side_effect=fail_source):
            with self.assertRaises(controller.ControllerError) as error:
                with controller.control_lock("dingtalk_mail"):
                    self.fail("source permission failure entered")
            self.assertEqual(error.exception.code, "control_lock_unavailable")
        with self.worker() as worker:
            self.enter(worker, "all")
            self.request(worker, "release")
            self.enter(worker, "dingtalk_mail")

    def test_main_passes_target_with_all_downstream_effects_stubbed(self):
        controller = isolated_controller()
        controller.syncv2 = mock.Mock()
        for target in (*SINGLES, *COMBINED):
            args = types.SimpleNamespace(automatic=False, target=target,
                                         config=self.root / "fixture.json", run_id="fixture")
            with self.subTest(target=target), \
                    mock.patch.object(controller, "parse_args", return_value=args), \
                    mock.patch.object(controller, "control_lock") as lock, \
                    mock.patch.object(controller, "execute", return_value={"status": "noop"}), \
                    mock.patch.object(controller, "record_manual_chat_result"), \
                    mock.patch("builtins.print"):
                self.assertEqual(controller.main(), 0)
                lock.assert_called_once_with(target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
