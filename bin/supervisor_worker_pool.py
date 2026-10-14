"""Keep scheduler ownership on the main thread while bounded source workers wait.

Workers only execute subprocesses. State writes and decisions stay on the owner
thread. A source cannot be submitted again until its previous result is recorded.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time
import os
import signal
import subprocess


def run(initial, *, execute, record, eligible, pulse, interval=15.0,
        rescan_interval=60.0, clock=time.monotonic, prepare=lambda source: None,
        continuations=lambda active: ()):
    active = {}
    outcomes = []
    errors = []
    next_scan = clock() + rescan_interval
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="sync-source") as pool:
        def submit(sources):
            for source in sources:
                if source not in active:
                    try:
                        prepare(source)
                        active[source] = pool.submit(execute, source)
                    except Exception as exc:
                        errors.append(exc)
                        break
        submit(initial)
        while active:
            try:
                pulse(tuple(active))
            except Exception as exc:
                if not errors:
                    errors.append(exc)
            done, _ = wait(tuple(active.values()), timeout=interval, return_when=FIRST_COMPLETED)
            for source, future in list(active.items()):
                if future not in done:
                    continue
                try:
                    outcomes.append(record(source, future.result()))
                except Exception as exc:
                    # Drain already running workers, but never start a replacement
                    # when recording a completed result failed or is ambiguous.
                    errors.append(exc)
                del active[source]
            if done and not errors:
                try:
                    submit(continuations(frozenset(active)))
                except Exception as exc:
                    errors.append(exc)
            if active and not errors and clock() >= next_scan:
                try:
                    submit(eligible(frozenset(active)))
                except Exception as exc:
                    errors.append(exc)
                next_scan = clock() + rescan_interval
    if errors:
        raise errors[0]
    return outcomes


def command(arguments, timeout):
    """The existing six-hour worker bound owns a process group, not just caffeinate."""
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A separately managed descendant may retain a pipe. It is not
            # ours to kill; close local pipes so it cannot hold this worker.
            process.stdout.close()
            process.stderr.close()
            process.wait(timeout=5)
        raise
    return subprocess.CompletedProcess(arguments, process.returncode, out, err)
