"""Concurrent cold/forced dashboard reads must share one completed collection."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import concurrent.futures
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import system_dashboard_status as status
from selftest_system_dashboard import build_paths, create_fixtures, FakeRunner


class ObservedLock:
    def __init__(self, wanted):
        self.lock = threading.Lock()
        self.counter = threading.Lock()
        self.waiters = 0
        self.wanted = wanted
        self.all_arrived = threading.Event()

    def __enter__(self):
        with self.counter:
            self.waiters += 1
            if self.waiters >= self.wanted:
                self.all_arrived.set()
        self.lock.acquire()

    def __exit__(self, *args):
        self.lock.release()


def main():
    with tempfile.TemporaryDirectory() as directory:
        paths = build_paths(Path(directory))
        create_fixtures(paths)
        dashboard = status.SystemDashboard(paths, runner=FakeRunner(set()))
        lock = ObservedLock(5)
        dashboard._collect_lock = lock
        release = threading.Event()
        calls = []
        original = status.LaunchdReader.read_many

        def collect_launchd(reader, labels):
            calls.append(1)
            assert release.wait(10), 'test collector release missing'
            return original(reader, labels)

        config = {'email_sync': {'enabled': True, 'writer_registered': True, 'poll_minutes': 30}}
        with patch.object(status.LaunchdReader, 'read_many', collect_launchd):
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                jobs = [pool.submit(dashboard.collect, config, force=True) for _ in range(5)]
                arrived = lock.all_arrived.wait(10)
                release.set()
                assert arrived, 'not all concurrent force requests reached collection'
                values = [job.result(timeout=15) for job in jobs]
            assert len(calls) == 1 and all(value is values[0] for value in values), 'cold requests repeated collection'
            newer = dashboard.collect(config, force=True)
            assert len(calls) == 2 and newer is not values[0], 'later force reused old snapshot'
            assert dashboard.collect(config) is newer and len(calls) == 2, 'ordinary cache reuse changed'
            dashboard._cached_at -= 5
            assert dashboard.collect(config) is not newer and len(calls) == 3, 'expiry was extended'
        with patch.object(status.LaunchdReader, 'read_many', side_effect=RuntimeError('read failed')):
            try:
                dashboard.collect(config, force=True)
            except RuntimeError:
                pass
            else:
                raise AssertionError('forced collector failure substituted old result')
    print('{"verdict":"pass","checks":5}')


if __name__ == '__main__':
    main()
