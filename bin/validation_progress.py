"""Best-effort content-free validation timing, outside business snapshots."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_LOCAL = example-host.invalid()
LIMIT = 4 * 1024 * 1024

class Progress:
    def __init__(self, root, component, run_id=None, clock=time.monotonic):
        self.root = Path(root)
        self.component = component
        self.run_id = run_id or str(uuid.uuid4())
        self.clock = clock
        self.started = clock()
        self.frames = []
        self.counts = {}
        self.guard = threading.RLock()
        self.stop = threading.Event()
        self.dropped = 0
        self.slow = False
        self.code_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.thread = None
        self.io_guard = threading.Lock()
        self.lock_fd = None
        self.journal_fd = None

    def emit(self, event, **fields):
        with self.guard:
            now = self.clock()
            payload = dict(schema_version=1, recorded_at=datetime.now().astimezone().isoformat(timespec='seconds'),
                           component=self.component, run_id=self.run_id, pid=os.getpid(), ppid=os.getppid(),
                           code_sha256=self.code_sha256, event=event,
                           phase=self.frames[-1][0] if self.frames else 'idle',
                           phase_seconds=round(now-self.frames[-1][1], 6) if self.frames else 0,
                           elapsed_seconds=round(now-self.started, 6), counts=dict(self.counts),
                           slow=self.slow, diagnostic_dropped=self.dropped, **fields)
        if not self.io_guard.acquire(False):
            self.dropped += 1
            return
        held = False
        try:
            state = self.root / 'state'
            if self.lock_fd is None:
                state.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.lock_fd = os.open(state / 'validation-progress.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
            path = state / 'validation-progress.jsonl'
            try: info = path.stat()
            except FileNotFoundError: info = None
            if info and info.st_size >= LIMIT:
                for i in range(2, 0, -1):
                    old = Path(str(path)+'.'+str(i))
                    if old.exists(): os.replace(old, str(path)+'.'+str(i+1))
                os.replace(path, str(path)+'.1')
                info = None
            if self.journal_fd is not None:
                before = os.fstat(self.journal_fd)
                if info is None or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
                    os.close(self.journal_fd)
                    self.journal_fd = None
            if self.journal_fd is None:
                self.journal_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            data = (json.dumps(payload, sort_keys=True)+'\n').encode()
            if os.write(self.journal_fd, data) != len(data): raise OSError('short diagnostic write')
        except (OSError, ValueError):
            self.dropped += 1
            if self.dropped == 1:
                try: os.write(2, b'validation_progress_unavailable\n')
                except OSError: pass
        finally:
            if held:
                try: fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                except OSError: self.dropped += 1
            self.io_guard.release()

    def __enter__(self):
        self.previous = getattr(_LOCAL, 'progress', None)
        _LOCAL.progress = self
        self.emit('started')
        def heartbeat():
            while not self.stop.wait(5): self.emit('progress')
        self.thread = threading.Thread(target=heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, typ, value, tb):
        self.stop.set()
        self.thread.join(timeout=1)
        self.emit('finished', outcome='error' if typ else 'completed')
        for fd in (self.journal_fd, self.lock_fd):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
        self.journal_fd = self.lock_fd = None
        _LOCAL.progress = self.previous

    @contextmanager
    def stage(self, name):
        with self.guard: self.frames.append((name, self.clock()))
        self.emit('stage_started')
        try:
            yield
        finally:
            self.emit('stage_finished')
            with self.guard: self.frames.pop()


def current():
    return getattr(_LOCAL, 'progress', None)

@contextmanager
def stage(name):
    p = current()
    if p:
        with p.stage(name): yield
    else: yield

def count(name, amount=1):
    p = current()
    if p:
        with p.guard: p.counts[name] = p.counts.get(name, 0) + amount
