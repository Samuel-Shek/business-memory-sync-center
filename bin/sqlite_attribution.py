"""Bounded, content-free ledger activity; never a business lock or retry.

Statement observations are not lock ownership. Only the isolated OS probe can
identify observed POSIX holders. Full attribution remains explicitly incomplete
when evidence is absent. The journal is 4 x 8 MiB; incidents retain snapshots.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import fcntl
import hashlib
import json
import marshal
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

MAX_BYTES = 8 * 1024 * 1024
GENERATIONS = 4
ROOT = Path(__file__).resolve().parent
PROCESS_TOKEN = uuid.uuid4().hex
_hashes = {}
_warned = False
_dropped = 0


def warning():
    global _warned, _dropped
    _dropped += 1
    if not _warned:
        _warned = True
        try:
            os.write(2, b'sqlite_attribution_unavailable: diagnostic evidence may be incomplete\n')
        except OSError:
            pass


def origin():
    frames = []
    frame = sys._getframe(1)
    while frame and len(frames) < 6:
        p = Path(frame.f_code.co_filename)
        if p.parent == ROOT and p.name != 'sqlite_attribution.py':
            code = frame.f_code
            if code not in _hashes:
                if len(_hashes) > 2048:
                    _hashes.clear()
                _hashes[code] = hashlib.sha256(marshal.dumps(code)).hexdigest()
            frames.append({'file': p.name, 'function': code.co_name, 'line': frame.f_lineno,
                           'loaded_code_sha256': _hashes[code]})
        frame = frame.f_back
    return {'pid': os.getpid(), 'ppid': os.getppid(), 'thread_id': threading.get_ident(),
            'process_token': PROCESS_TOKEN, 'frames': frames, 'diagnostic_losses_in_process': _dropped}


def journal_path(database):
    key = hashlib.sha256(str(Path(database).resolve()).encode()).hexdigest()[:16]
    return Path(database).parent / ('sqlite-attribution-' + key + '.jsonl')


_journal_handles = {}
_journal_guard = threading.Lock()
_journal_pid = os.getpid()


def _reset_after_fork():
    global _journal_guard, _journal_pid, PROCESS_TOKEN
    for handle in _journal_handles.values():
        for fd in handle:
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
    _journal_handles.clear()
    _journal_guard = threading.Lock()
    _journal_pid = os.getpid()
    PROCESS_TOKEN = uuid.uuid4().hex


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def record(database, event):
    """Cached diagnostic FDs, nonblocking locks, bounded disk/memory use.

    No database or shm descriptor is opened here. After rotating, processes
    reopen only their diagnostic descriptor, never SQLite's descriptors.
    """
    if _journal_pid != os.getpid():
        _reset_after_fork()
    if not _journal_guard.acquire(False):
        warning()
        return False
    held = False
    handles = None
    try:
        path = journal_path(database)
        handles = _journal_handles.get(path)
        if handles is None:
            if len(_journal_handles) >= 8:
                old = _journal_handles.pop(next(iter(_journal_handles)))
                for fd in old:
                    if fd is not None: os.close(fd)
            lock_fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handles = [lock_fd, None]
            _journal_handles[path] = handles
        fcntl.flock(handles[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
        try: info = path.stat()
        except FileNotFoundError: info = None
        if info is not None and info.st_size >= MAX_BYTES:
            for n in range(GENERATIONS-1, 0, -1):
                old = path if n == 1 else Path(str(path)+'.'+str(n-1))
                if old.exists(): os.replace(old, str(path)+'.'+str(n))
            info = None
        if handles[1] is not None:
            previous = os.fstat(handles[1])
            if info is None or (previous.st_dev, previous.st_ino) != (info.st_dev, info.st_ino):
                os.close(handles[1]); handles[1] = None
        if handles[1] is None:
            handles[1] = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        data = json.dumps(dict(event, recorded_at_ns=time.time_ns(), schema_version=1),
                          sort_keys=True).encode() + b'\n'
        if len(data) > 16000 or os.write(handles[1], data) != len(data):
            warning()
            return False
        return True
    except (OSError, ValueError, TypeError):
        warning()
        return False
    finally:
        if held:
            try: fcntl.flock(handles[0], fcntl.LOCK_UN)
            except OSError: warning()
        _journal_guard.release()


def attach(connection, database):
    """Passive trace, no SQL, authorizer, transaction changes or parameters logged."""
    connection_id = uuid.uuid4().hex
    last = [None]
    def trace(statement):
        try:
            match = re.match(r'\s*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|PRAGMA)\b', statement, re.I)
            if not match:
                return
            command = match[1].upper()
            if command == "PRAGMA" and not re.match(r"\s*PRAGMA\s+journal_mode\b", statement, re.I):
                return
            who = origin()
            signature = (command, tuple((f['file'], f['function'], f['line']) for f in who['frames']))
            if signature == last[0]:
                return
            last[0] = signature
            record(database, dict(who, event='statement_observed', connection_id=connection_id,
                                  statement_kind=command,
                                  ownership='not_asserted'))
        except Exception:
            warning()
    connection.set_trace_callback(trace)
    return connection


def recent(database, since_ns, limit=24):
    rows, gaps = [], []
    path = journal_path(database)
    for candidate in (Path(str(path)+'.1'), path):
        try:
            with candidate.open('rb') as handle:
                size = os.fstat(handle.fileno()).st_size
                handle.seek(max(0, size - 256*1024))
                if size > 256*1024:
                    handle.readline()
                    gaps.append('bounded_journal_tail')
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        gaps.append('incomplete_journal_line')
                        continue
                    if isinstance(row, dict) and type(row.get('recorded_at_ns')) is int:
                        if row['recorded_at_ns'] >= since_ns: rows.append(row)
                    else:
                        gaps.append('invalid_journal_schema')
        except FileNotFoundError:
            continue
        except OSError:
            gaps.append('journal_unreadable')
    if len(rows) > limit:
        gaps.append('event_limit')
    return {'events': rows[-limit:], 'gaps': sorted(set(gaps)),
            'interpretation': 'context_only_statement_start_does_not_prove_lock_ownership'}


def snapshot(database, *, since_ns=None, timeout=.6):
    value = {'requester': origin(), 'status': 'incomplete', 'lock_owner': 'unknown',
             'recent_activity': recent(database, since_ns or time.time_ns()-60_000_000_000)}
    if timeout < .05:
        return dict(value, gap='diagnostic_time_budget_exhausted')
    try:
        # Critical: the child, not this SQLite process, opens the db/shm files.
        result = subprocess.run([sys.executable, str(ROOT/'sqlite_lock_probe.py'), str(database), str(os.getpid())],
                                capture_output=True, text=True, timeout=min(.6, timeout),
                                close_fds=True, stdin=subprocess.DEVNULL)
        if result.returncode:
            return dict(value, gap='lock_probe_failed')
        probe = json.loads(result.stdout)
        value['os_probe'] = probe
        writers = [r for r in probe.get('locks', []) if r['mode'] == 'write' and
                   r['role'] in ('wal_write', 'database_reserved', 'database_pending')]
        if writers:
            value.update(status='lock_holder_observed', lock_owner=writers[0]['pid'])
        else:
            value['gap'] = 'no_conflicting_writer_observed_at_sample_time'
        return value
    except Exception:
        return dict(value, gap='lock_probe_unavailable_or_timed_out')
