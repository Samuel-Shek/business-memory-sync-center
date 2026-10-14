"""Bound a single attached validator process group; never replay the command."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import os
import signal
import subprocess
import time
import threading
from contextlib import contextmanager
import validation_progress as progress


class CleanupError(OSError):
    pass

def settings(config):
    obj = config.get('automatic_memory_validation', {})
    if not isinstance(obj, dict): raise ValueError('automatic_validation_config_invalid')
    timeout = obj.get('full_validation_timeout_seconds', 300)
    slow = obj.get('slow_after_seconds', 120)
    if type(timeout) is not int or type(slow) is not int or not 1 <= slow < timeout <= 600:
        raise ValueError('automatic_validation_config_invalid')
    return timeout, slow


def group_exists(pid):
    try: os.killpg(pid, 0); return True
    except ProcessLookupError: return False
    except PermissionError: return True  # Unknown/exiting group: never treat denial as exit.


def cleanup(child, grouped):
    # Ruby establishes its own process group before requiring code or spawning.
    # If startup has not reached that instruction, SIGKILL its PID first: this
    # prevents racing a late group creation and subsequent descendant spawn.
    if not grouped:
        try: child.kill()
        except ProcessLookupError: pass
        child.wait()
        grouped = group_exists(child.pid)
    if grouped:
        try: os.killpg(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError): pass
        try: child.wait(timeout=2)
        except subprocess.TimeoutExpired: pass
        # macOS can briefly retain an exiting group after its leader is reaped.
        # Give all readers the TERM grace period before escalating the group.
        grace = time.monotonic()+2
        while group_exists(child.pid) and time.monotonic() < grace:
            time.sleep(.02)
        if group_exists(child.pid):
            try: os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError): pass
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise CleanupError("validation_process_cleanup_unproven") from exc
        deadline = time.monotonic()+5
        while group_exists(child.pid) and time.monotonic() < deadline:
            time.sleep(.02)
        if group_exists(child.pid): raise CleanupError('validation_process_cleanup_unproven')
    p = progress.current()
    if p: p.emit('process_cleanup', child_pid=child.pid, group_exited=True)


@contextmanager
def termination_signals():
    previous = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(number, frame):
            raise SystemExit(128 + number)
        for number in (signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, interrupted)
    try:
        yield
    finally:
        for number, handler in previous.items(): signal.signal(number, handler)


def run(args, *, timeout, slow_after, env, pass_fds, clock=time.monotonic, popen=subprocess.Popen, on_slow=None):
    with termination_signals():
        return _run(args, timeout=timeout, slow_after=slow_after, env=env, pass_fds=pass_fds, clock=clock, popen=popen, on_slow=on_slow)


def _run(args, *, timeout, slow_after, env, pass_fds, clock, popen, on_slow):
    start = clock()
    p = progress.current()
    warned = False
    child = popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, env=env, pass_fds=pass_fds)
    grouped = False
    try:
        while True:
            try: grouped = os.getpgid(child.pid) == child.pid
            except ProcessLookupError: pass
            elapsed = clock()-start
            if elapsed >= slow_after and not warned:
                warned = True
                if on_slow is not None: on_slow()
                if p:
                    p.slow = True
                    p.emit('slow_validation', child_pid=child.pid, validator_seconds=elapsed)
            if elapsed >= timeout:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                stdout, stderr = child.communicate(timeout=min(1, timeout-elapsed))
                break
            except subprocess.TimeoutExpired:
                continue
        if group_exists(child.pid):
            # A successful leader must not leave readers or inherited locks.
            cleanup(child, True)
            raise CleanupError('validation_descendants_after_exit')
        if p: p.emit('validator_exited', child_pid=child.pid, validator_seconds=clock()-start, returncode=child.returncode)
        return subprocess.CompletedProcess(args, child.returncode, stdout, stderr)
    except BaseException:
        cleanup(child, grouped)
        raise
    finally:
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream: stream.close()
