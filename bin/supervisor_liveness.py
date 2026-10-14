#!/usr/bin/env python3
"""Content-free supervisor OWNER receipts; never proof of business completion.

Public API:
  pulse(root, *, stage, workers=None, source=None, cycle_started=False,
        cycle_finished=False) -> receipt (raises on unsafe/unknown evidence).
  read(root) -> receipt or {'read_status': 'missing'|'invalid'|'unavailable'}.
  clock_sample() -> {boot_id, active_ns, continuous_ns, wall_ns, clock_kind}.
  evaluate(snapshot, service, current) -> owner health plus separate worker state.

Only the supervisor main thread calls pulse, under its existing singleton lock.
Pass a trusted absolute root with OS aliases already resolved (e.g. /private/var
for macOS temporary directories). This module never follows directory symlinks.
workers: up to 16 {source_id, started_at, progress_at, progress[, pid, stage,
run_id, validated_chunks, covered_chars, observed_files, fragment_stage_complete]}.
Dates are aware ISO strings/datetimes; progress is a machine token, counter or
None. No paths, arbitrary text, payloads, model inputs or completion inference.
None preserves workers; [] clears them. cycle_* describe scheduling only.
service running: loaded=True, state='running', pid and process_identity (owner
or its native launcher parent; obtain with process_identity(pid)). Between
run-once invocations, state='not running', last_exit_code=0 requires a fresh
cycle_finished receipt. current may include recovery_after, a clock
sample from a previous fault observation. Unknown evidence never permits close.

Darwin uses mach_absolute_time (awake) and mach_continuous_time (includes sleep),
converted with mach_timebase_info. Neither has a per-interpreter epoch. Kernel
boot-session UUID and process birth time prevent reboot/PID-reuse confusion.
No generic monotonic fallback: unsupported clocks fail closed.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import ctypes
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

RECEIPT = 'supervisor-liveness.json'
OBSERVATION = 'supervisor-liveness-observation.json'  # sentinel-owned, not a pulse
HEARTBEAT_GRACE_SECONDS = 420
WORKER_PROGRESS_GRACE_SECONDS = 420
SLEEP_TOLERANCE_NS = 2_000_000_000
MAX_BYTES = 16384
SOURCES = frozenset({'dingtalk_chat', 'dingtalk_minutes', 'dingtalk_mail', 'win11',
                     'memory_core', 'system'})
_TOKEN = re.compile(r'[A-Za-z0-9_:.=-]{1,80}\Z')


def _token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError('machine_token_required')
    return value


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _clock_valid(value):
    return (isinstance(value, dict) and value.get('clock_kind') == 'darwin_mach_v1'
            and isinstance(value.get('boot_id'), str)
            and re.fullmatch(r'[0-9a-fA-F-]{36}', value['boot_id']) is not None
            and all(_integer(value.get(k), 1) for k in ('active_ns', 'continuous_ns', 'wall_ns'))
            and value['continuous_ns'] >= value['active_ns'])


def clock_sample() -> dict:
    """Read only OS clock evidence; failures return unknown, never wall fallback."""
    if sys.platform != 'darwin':
        return {'error_code': 'clock_unsupported'}
    try:
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        class Timebase(ctypes.Structure):
            _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
        base = Timebase()
        lib.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
        if lib.mach_timebase_info(ctypes.byref(base)) != 0 or not base.denom:
            raise OSError('timebase_unavailable')
        lib.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        boot = ctypes.create_string_buffer(128)
        size = ctypes.c_size_t(len(boot))
        if lib.sysctlbyname(b'kern.bootsessionuuid', boot, ctypes.byref(size), None, 0):
            raise OSError('boot_unavailable')
        for name in ('mach_absolute_time', 'mach_continuous_time'):
            getattr(lib, name).restype = ctypes.c_uint64
            getattr(lib, name).argtypes = []
        # Bound sampling jitter/suspend during acquisition; never mix epochs.
        for _ in range(3):
            a = lib.mach_absolute_time() * base.numer // base.denom
            c = lib.mach_continuous_time() * base.numer // base.denom
            w = time.time_ns()
            a2 = lib.mach_absolute_time() * base.numer // base.denom
            c2 = lib.mach_continuous_time() * base.numer // base.denom
            if max(a2-a, c2-c) < 50_000_000:
                result = dict(clock_kind='darwin_mach_v1', boot_id=str(uuid.UUID(boot.value.decode())),
                              active_ns=a, continuous_ns=c, wall_ns=w)
                if _clock_valid(result):
                    return result
        raise OSError('clock_sample_unstable')
    except (OSError, AttributeError, ValueError):
        return {'error_code': 'clock_unavailable'}


def process_identity(pid: int) -> str | None:
    """Kernel PID birth identity only; does not read argv, paths or process memory.

    ABI: Apple's xnu/bsd/sys/proc_info.h, PROC_PIDTBSDINFO (3).
    """
    if sys.platform != 'darwin' or not _integer(pid, 1):
        return None
    try:
        class BSDInfo(ctypes.Structure):
            _fields_ = [('head', ctypes.c_uint32 * 12), ('comm', ctypes.c_char * 16),
                        ('name', ctypes.c_char * 32), ('tail', ctypes.c_uint32 * 6),
                        ('seconds', ctypes.c_uint64), ('microseconds', ctypes.c_uint64)]
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
        data = BSDInfo()
        if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(data), ctypes.sizeof(data)) != ctypes.sizeof(data):
            return None
        if data.head[3] != pid or data.head[1] == 5 or not data.seconds:  # SZOMB
            return None
        return f'{pid}:{data.seconds}:{data.microseconds}'
    except (OSError, AttributeError):
        return None


@contextmanager
def _directory(root, *, create=False):
    """Pin every directory component; refuse symlinks, including a swapped state/."""
    path = Path(root).absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for component in (*path.parts[1:], 'state'):
            if component in {'.', '..'}:
                raise ValueError('unsafe_path')
            if component == 'state' and create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('unsafe_state_directory')
        yield descriptor
    finally:
        os.close(descriptor)


def _file_safe(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and not info.st_mode & 0o077 and info.st_size <= MAX_BYTES)


def _read_json(root, name):
    try:
        with _directory(root) as directory:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, 'rb') as handle:
                before = os.fstat(handle.fileno())
                if not _file_safe(before):
                    raise ValueError('unsafe_receipt')
                raw = handle.read(MAX_BYTES + 1)
                after = os.fstat(handle.fileno())
                named = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (len(raw) > MAX_BYTES or
                    (before.st_ino, before.st_mtime_ns, before.st_size) !=
                    (after.st_ino, after.st_mtime_ns, after.st_size) or
                    (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)):
                    raise ValueError('receipt_changed')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('receipt_invalid')
        value['_file_mtime_ns'] = after.st_mtime_ns
        value['read_status'] = 'ok'
        return value
    except FileNotFoundError:
        return {'read_status': 'missing'}
    except (ValueError, UnicodeError):
        return {'read_status': 'invalid'}
    except OSError:
        return {'read_status': 'unavailable'}


def _write_json(root, name, value):
    raw = (json.dumps(value, sort_keys=True, allow_nan=False) + '\n').encode()
    if len(raw) > MAX_BYTES:
        raise ValueError('receipt_too_large')
    with _directory(root, create=True) as directory:
        try:
            if not _file_safe(os.stat(name, dir_fd=directory, follow_symlinks=False)):
                raise ValueError('unsafe_existing_receipt')
        except FileNotFoundError:
            pass
        temporary = '.liveness-' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def read(root) -> dict:
    """Read the fixed owner receipt only; never create files or refresh heartbeat."""
    return _read_json(root, RECEIPT)


def _date(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('aware_timestamp_required')
    return parsed.isoformat()


def _workers(values, previous, clock):
    if not isinstance(values, list) or len(values) > 16:
        raise ValueError('workers_invalid')
    result, seen = [], set()
    old = {w['source_id']: w for w in previous}
    for value in values:
        if not isinstance(value, dict) or set(value) - {
                'source_id', 'started_at', 'progress_at', 'progress', 'pid', 'stage',
                'run_id', 'validated_chunks', 'covered_chars', 'observed_files', 'fragment_stage_complete'}:
            raise ValueError('worker_fields_invalid')
        source = value.get('source_id')
        if source not in SOURCES or source in seen:
            raise ValueError('worker_source_invalid')
        seen.add(source)
        progress = value.get('progress')
        if progress is not None and not _integer(progress):
            _token(progress)
        entry = dict(source_id=source, started_at=_date(value.get('started_at')),
                     progress_at=_date(value.get('progress_at')), progress=progress)
        for key in ('stage', 'run_id'):
            if value.get(key) is not None:
                entry[key] = _token(value[key])
        for key in ('validated_chunks', 'covered_chars', 'observed_files'):
            if value.get(key) is not None:
                if not _integer(value[key]) or value[key] > 2**63-1:
                    raise ValueError('worker_counter_invalid')
                entry[key] = value[key]
        if value.get('fragment_stage_complete') is not None:
            if type(value['fragment_stage_complete']) is not bool:
                raise ValueError('worker_fragment_flag_invalid')
            entry['fragment_stage_complete'] = value['fragment_stage_complete']
        if value.get('pid') is not None:
            if not _integer(value['pid'], 1):
                raise ValueError('worker_pid_invalid')
            entry['pid'] = value['pid']
        prior = old.get(source, {})
        # Only observed changes count. Initial or unknown progress is not proof.
        known = progress not in (None, 'unknown', 'pending', 'running', 'waiting',
                                 'unavailable', 'network_wait', 'network_waiting', 'keepalive')
        same_run = (prior and entry['started_at'] is not None
                    and prior.get('started_at') == entry['started_at']
                    and prior.get('run_id') == entry.get('run_id'))
        evidence_keys = ('progress_at', 'progress', 'stage', 'validated_chunks', 'covered_chars',
                         'observed_files', 'fragment_stage_complete')
        changed = any(prior.get(k) != entry.get(k) for k in evidence_keys)
        # Merely changing the annotation from unknown/unavailable to observed
        # does not establish new source progress without changed source metadata.
        if progress == 'observed':
            changed = (entry['progress_at'] is not None and any(
                prior.get(k) != entry.get(k) for k in evidence_keys if k != 'progress'))
        entry['observed_progress_active_ns'] = (
            clock['active_ns'] if known and same_run and changed
            else prior.get('observed_progress_active_ns') if known and same_run else None)
        result.append(entry)
    return result


def pulse(root, *, stage, workers=None, source=None,
          cycle_started=False, cycle_finished=False, defer_reason=None, next_retry_at=None) -> dict:
    """Supervisor main-thread only. No DB access, notification or sync invocation."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('owner_main_thread_required')
    _token(stage)
    if source is not None and source not in SOURCES:
        raise ValueError('source_invalid')
    if type(cycle_started) is not bool or type(cycle_finished) is not bool:
        raise ValueError('cycle_flag_invalid')
    clock = clock_sample()
    identity = process_identity(os.getpid())
    parent_identity = process_identity(os.getppid())
    if not _clock_valid(clock) or identity is None or parent_identity is None:
        raise RuntimeError('owner_evidence_unavailable')
    if stage == 'cycle_deferred':
        if defer_reason != 'ledger_retry_wait' or next_retry_at is None:
            raise ValueError('deferred_evidence_required')
        next_retry_at = _date(next_retry_at)
        if datetime.fromisoformat(next_retry_at).timestamp() <= clock['wall_ns']/1e9:
            raise ValueError('deferred_retry_not_future')
    elif defer_reason is not None or next_retry_at is not None:
        raise ValueError('deferred_fields_unexpected')
    previous = read(root)
    if previous.get('read_status') not in {'ok', 'missing'}:
        raise ValueError('owner_receipt_unreadable')
    if previous.get('read_status') == 'ok' and not _receipt_valid(previous):
        raise ValueError('owner_receipt_invalid')
    same_owner = (previous.get('process_identity') == identity and
                  previous.get('heartbeat', {}).get('boot_id') == clock['boot_id'])
    if previous.get('read_status') == 'ok' and not same_owner:
        # A child/second live process cannot steal an existing owner's receipt.
        if (previous['heartbeat']['boot_id'] == clock['boot_id'] and
                process_identity(previous['pid']) == previous['process_identity']):
            raise RuntimeError('another_owner_alive')
    old = previous if same_owner else {}
    result = dict(schema_version=2, pid=os.getpid(), ppid=os.getppid(),
                  process_identity=identity, parent_identity=parent_identity,
                  sequence=old.get('sequence', 0) + 1,
                  heartbeat=clock, stage=stage, current_source=source,
                  cycle_started=clock if cycle_started or stage == 'cycle_started' else old.get('cycle_started'),
                  cycle_finished=clock if cycle_finished or stage == 'cycle_finished' else old.get('cycle_finished'),
                  workers=old.get('workers', []) if workers is None else _workers(workers, old.get('workers', []), clock))
    result.update(cycle_ended=clock if stage in {'cycle_finished', 'cycle_deferred', 'cycle_failed'} else None,
                  cycle_outcome={'cycle_finished':'completed','cycle_deferred':'deferred','cycle_failed':'failed'}.get(stage),
                  defer_reason=defer_reason, next_retry_at=next_retry_at)
    if stage == 'cycle_deferred' and (result['workers'] or result['cycle_started'] is None):
        raise ValueError('deferred_workers_or_start_invalid')
    _write_json(root, RECEIPT, result)
    return result


def _receipt_valid(snapshot):
    if not isinstance(snapshot, dict):
        return False
    try:
        if (type(snapshot.get('schema_version')) is not int or snapshot['schema_version'] not in {1, 2}
                or not _integer(snapshot.get('pid'), 1) or not _integer(snapshot.get('ppid'), 1)
                or not _integer(snapshot.get('sequence'), 1)
                or not isinstance(snapshot.get('process_identity'), str)
                or not re.fullmatch(r'\d+:\d+:\d+', snapshot['process_identity'])
                or not snapshot['process_identity'].startswith(str(snapshot['pid']) + ':')
                or not isinstance(snapshot.get('parent_identity'), str)
                or not re.fullmatch(r'\d+:\d+:\d+', snapshot['parent_identity'])
                or not snapshot['parent_identity'].startswith(str(snapshot['ppid']) + ':')
                or not _clock_valid(snapshot.get('heartbeat'))):
            return False
        _token(snapshot['stage'])
        if snapshot['schema_version'] == 2:
            ended = snapshot.get('cycle_ended')
            expected = {'cycle_finished':'completed','cycle_deferred':'deferred','cycle_failed':'failed'}.get(snapshot['stage'])
            if snapshot.get('cycle_outcome') != expected:
                return False
            if expected and (not _clock_valid(ended) or ended != snapshot['heartbeat']):
                return False
            if not expected and ended is not None:
                return False
            if expected in {'completed','deferred'} and (snapshot.get('cycle_started') is None or snapshot['workers']):
                return False
            if expected == 'deferred':
                if snapshot.get('defer_reason') != 'ledger_retry_wait' or _date(snapshot.get('next_retry_at')) is None:
                    return False
                if datetime.fromisoformat(snapshot['next_retry_at']).timestamp() <= ended['wall_ns']/1e9:
                    return False
            elif snapshot.get('defer_reason') is not None or snapshot.get('next_retry_at') is not None:
                return False
        elif snapshot['stage'] == 'cycle_deferred':
            return False
        if snapshot.get('current_source') is not None and snapshot['current_source'] not in SOURCES:
            return False
        for key in ('cycle_started', 'cycle_finished'):
            c = snapshot.get(key)
            if c is not None and (not _clock_valid(c) or c['boot_id'] != snapshot['heartbeat']['boot_id']
                                  or c['active_ns'] > snapshot['heartbeat']['active_ns']):
                return False
        workers = snapshot['workers']
        if not isinstance(workers, list) or len(workers) > 16:
            return False
        seen = set()
        for worker in workers:
            if not isinstance(worker, dict) or worker.get('source_id') not in SOURCES or worker['source_id'] in seen:
                return False
            seen.add(worker['source_id'])
            fields = {'source_id', 'started_at', 'progress_at', 'progress', 'pid', 'stage',
                      'run_id', 'validated_chunks', 'covered_chars', 'observed_files',
                      'fragment_stage_complete', 'observed_progress_active_ns'}
            if set(worker) - fields:
                return False
            for key in ('stage', 'run_id'):
                if worker.get(key) is not None:
                    _token(worker[key])
            for key in ('validated_chunks', 'covered_chars', 'observed_files'):
                if worker.get(key) is not None and (not _integer(worker[key]) or worker[key] > 2**63-1):
                    return False
            if worker.get('pid') is not None and not _integer(worker['pid'], 1):
                return False
            if worker.get('fragment_stage_complete') is not None and type(worker['fragment_stage_complete']) is not bool:
                return False
            _date(worker.get('started_at'))
            _date(worker.get('progress_at'))
            p = worker.get('progress')
            if p is not None and not _integer(p):
                _token(p)
            a = worker.get('observed_progress_active_ns')
            if a is not None and (not _integer(a, 1) or a > snapshot['heartbeat']['active_ns']):
                return False
        return True
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


def evaluate(snapshot, service, current) -> dict:
    """Pure function. healthy means owner liveness ONLY, not source/business health."""
    result = dict(status='unknown', error_code=None, age_seconds=None, can_resolve=False,
                  owner_active=False, health_scope='owner_liveness', business_status='not_evaluated',
                  workers=[], sleep_seconds=None)
    def done(status, error):
        return dict(result, status=status, error_code=error)
    if not isinstance(service, dict) or not isinstance(snapshot, dict):
        return done('unknown', 'liveness_input_invalid')
    if service.get('loaded') is False:
        return done('failed', 'supervisor_unloaded')
    if snapshot.get('read_status') == 'missing':
        return done('migration_pending', 'supervisor_owner_receipt_missing')
    if snapshot.get('read_status', 'ok') != 'ok' or not _receipt_valid(snapshot):
        return done('unknown', 'supervisor_owner_receipt_invalid')
    if not _clock_valid(current):
        return done('unknown', 'supervisor_clock_unavailable')
    heartbeat = snapshot['heartbeat']
    if heartbeat['boot_id'] != current['boot_id']:
        return done('stale', 'supervisor_receipt_previous_boot')
    age = current['active_ns'] - heartbeat['active_ns']
    elapsed = current['continuous_ns'] - heartbeat['continuous_ns']
    wall = current['wall_ns'] - heartbeat['wall_ns']
    if min(age, elapsed) < 0 or elapsed-age < -SLEEP_TOLERANCE_NS:
        return done('unknown', 'supervisor_clock_inconsistent')
    result.update(age_seconds=age/1e9, sleep_seconds=max(0, (elapsed-age)/1e9),
                  wall_clock_shift_seconds=(wall-elapsed)/1e9)
    mtime = snapshot.get('_file_mtime_ns')
    if mtime is not None and (not _integer(mtime, 1) or abs(mtime-heartbeat['wall_ns']) > 5_000_000_000):
        return done('unknown', 'supervisor_receipt_file_time_mismatch')
    if service.get('loaded') is not True:
        return done('unknown', 'supervisor_service_unknown')
    between_cycles = service.get('state') in {'not running', 'exited', 'stopped'}
    if between_cycles:
        if type(service.get('last_exit_code')) is not int:
            return done('unknown', 'supervisor_exit_unknown')
        if service['last_exit_code'] != 0:
            return done('failed', 'supervisor_exit_nonzero')
        terminal = (snapshot['stage'] == 'cycle_finished' and snapshot.get('cycle_finished') == heartbeat) or (
            snapshot.get('schema_version') == 2 and snapshot['stage'] == 'cycle_deferred' and snapshot.get('cycle_ended') == heartbeat)
        if (not terminal or snapshot.get('cycle_started') is None or snapshot['workers']):
            return done('failed', 'supervisor_finished_receipt_required')
        if service.get('pid') is not None:
            expected = (snapshot['process_identity'] if service['pid'] == snapshot['pid'] else
                        snapshot['parent_identity'] if service['pid'] == snapshot['ppid'] else None)
            if expected is None or (service.get('process_identity') is not None and
                                    service['process_identity'] != expected):
                return done('stale', 'supervisor_owner_mismatch')
    else:
        if service.get('state') != 'running' or not _integer(service.get('pid'), 1):
            return done('unknown', 'supervisor_service_unknown')
        expected = (snapshot['process_identity'] if service['pid'] == snapshot['pid'] else
                    snapshot['parent_identity'] if service['pid'] == snapshot['ppid'] else None)
        if expected is None:
            return done('stale', 'supervisor_owner_mismatch')
        if not service.get('process_identity'):
            return done('unknown', 'supervisor_owner_identity_unknown')
        if service['process_identity'] != expected:
            return done('stale', 'supervisor_owner_mismatch')
    if snapshot['stage'] in {'failed', 'cycle_failed'}:
        return done('failed', 'supervisor_owner_failed')
    if age > HEARTBEAT_GRACE_SECONDS * 1e9:
        return done('stale', 'supervisor_owner_heartbeat_stale')
    result['owner_active'] = not between_cycles
    # Sleep explains elapsed wall time but never cures a fault by itself.
    if elapsed-age > SLEEP_TOLERANCE_NS:
        return done('wake_pending', 'supervisor_post_wake_pulse_required')
    if abs(wall-elapsed) > 5_000_000_000:
        return done('unknown', 'supervisor_wall_clock_changed')
    fence = current.get('recovery_after')
    if fence is not None:
        if not _clock_valid(fence):
            return done('unknown', 'supervisor_recovery_evidence_invalid')
        if fence['boot_id'] == heartbeat['boot_id'] and heartbeat['active_ns'] <= fence['active_ns']:
            return done('recovery_pending', 'supervisor_new_owner_pulse_required')
    for worker in snapshot['workers']:
        observed = worker.get('observed_progress_active_ns')
        progress_age = (current['active_ns']-observed)/1e9 if observed is not None else None
        status = 'unknown' if observed is None else 'progressing' if progress_age <= WORKER_PROGRESS_GRACE_SECONDS else 'stale'
        result['workers'].append(dict(source_id=worker['source_id'], status=status,
                                      progress_age_seconds=progress_age))
    if snapshot['stage'] == 'cycle_deferred':
        if datetime.fromisoformat(snapshot['next_retry_at']).timestamp() + HEARTBEAT_GRACE_SECONDS < current['wall_ns']/1e9:
            return done('stale', 'supervisor_deferred_retry_overdue')
        result.update(cycle_outcome='deferred', defer_reason=snapshot['defer_reason'], next_retry_at=snapshot['next_retry_at'])
    result.update(can_resolve=True, activity='between_cycles' if between_cycles else
                  'workers_present' if result['workers'] else 'owner_idle')
    return done('healthy', None)
