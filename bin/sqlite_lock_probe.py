#!/usr/bin/env python3
"""Darwin POSIX lock inspection, in a disposable process ONLY.

Opening/closing a SQLite file inside its owning process can release its POSIX
locks. Never call this code from the database process. No SQLite connection,
F_SETLK, business file writes, command arguments, or environment are inspected.
Lock offsets: https://www.sqlite.org/walformat.html#wal_locks
"""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import ctypes
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


class DarwinFlock(ctypes.Structure):
    _fields_ = [('start', ctypes.c_int64), ('length', ctypes.c_int64),
                ('pid', ctypes.c_int32), ('type', ctypes.c_int16),
                ('whence', ctypes.c_int16)]


def identity(pid):
    try:
        p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'pid=', '-o', 'ppid=',
                            '-o', 'lstart=', '-o', 'comm='], capture_output=True,
                           text=True, timeout=.15, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        fields = p.stdout.strip().split(None, 7)
        if p.returncode or len(fields) != 8 or int(fields[0]) != pid:
            return {'pid': pid, 'identity_status': 'process_exited_or_unavailable'}
        name = Path(fields[7]).name
        return {'pid': pid, 'ppid': int(fields[1]), 'process_started_at': ' '.join(fields[2:7]),
                'executable_name': name if re.fullmatch(r'[A-Za-z0-9_. -]{1,80}', name) else 'redacted',
                'identity_status': 'sampled_after_lock_query'}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'pid': pid, 'identity_status': 'unavailable'}


def probe(database, requester):
    if os.getpid() == requester:
        raise RuntimeError('lock_probe_requires_separate_process')
    out = {'method': 'darwin_fcntl_getlk', 'sampled_at_ns': time.time_ns(),
           'locks': [], 'gaps': [], 'probe_pid': os.getpid()}
    if sys.platform != 'darwin' or ctypes.sizeof(DarwinFlock) != 24:
        return dict(out, gaps=['unsupported_platform_or_abi'])
    # Query incompatible locks; a reported reader is not labelled a writer.
    targets = [('wal_write', str(database)+'-shm', 120, 1),
               ('wal_checkpoint', str(database)+'-shm', 121, 1),
               ('wal_recovery', str(database)+'-shm', 122, 1),
               ('database_reserved', str(database), 0x40000001, 1),
               ('database_pending', str(database), 0x40000000, 1),
               ('database_shared_range', str(database), 0x40000002, 510)]
    for role, path, offset, length in targets:
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            request = DarwinFlock(offset, length, 0, fcntl.F_WRLCK, os.SEEK_SET)
            found = DarwinFlock.from_buffer_copy(fcntl.fcntl(fd, fcntl.F_GETLK, bytes(request)))
            if found.type != fcntl.F_UNLCK and found.pid > 0:
                out['locks'].append({'role': role, 'pid': found.pid,
                                     'mode': 'write' if found.type == fcntl.F_WRLCK else 'read',
                                     'same_process_as_requester': found.pid == requester,
                                     'device': info.st_dev, 'inode': info.st_ino})
        except OSError as exc:
            out['gaps'].append({'role': role, 'errno': exc.errno})
        finally:
            if fd is not None:
                os.close(fd)
    out['processes'] = [identity(pid) for pid in sorted({r['pid'] for r in out['locks']})[:4]]
    out['interpretation'] = 'observed_lock_holders_at_sample_time_not_complete_causal_proof'
    return out


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(2)
    print(json.dumps(probe(Path(sys.argv[1]), int(sys.argv[2])), sort_keys=True))
