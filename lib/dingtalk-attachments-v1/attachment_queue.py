"""Durable NEW-resource discovery. No scheduler, network, model or memory writes."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import os
from pathlib import Path
import sqlite3
import time
from handoff_core import canonical, digest, Rejected


class Queue:
    def __init__(self, root):
        root = Path(root)
        if root.is_symlink(): raise Rejected('unsafe_root')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        path = root / 'requests.sqlite'
        if path.is_symlink(): raise Rejected('unsafe_root')
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS seen(message TEXT PRIMARY KEY,revision TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS requests(
            key TEXT PRIMARY KEY,request TEXT NOT NULL,stage TEXT NOT NULL DEFAULT 'discovered',
            status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
            due REAL NOT NULL DEFAULT 0,error TEXT,task_key TEXT,notified_error TEXT,
            created REAL NOT NULL,updated REAL NOT NULL);
        ''')

    def close(self): self.db.close()

    def initialize(self, rows):
        if self.db.execute("SELECT 1 FROM meta WHERE key='baseline'").fetchone():
            return False
        # Record hashes only. Do NOT enqueue/download historical references.
        with self.db:
            for row in rows:
                self.db.execute('INSERT INTO seen VALUES(?,?)',
                    (row['message_id'], row['resource_refs_sha256']))
            self.db.execute("INSERT INTO meta VALUES('baseline',?)", (str(time.time()),))
        return True

    def enqueue(self, request):
        key = digest(canonical(request).encode())
        now = time.time()
        with self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO requests(key,request,created,updated) VALUES(?,?,?,?)',
                (key, canonical(request), now, now))
        return key, bool(cur.rowcount)

    def discover(self, rows, organization, profile):
        if not self.db.execute("SELECT 1 FROM meta WHERE key='baseline'").fetchone():
            raise Rejected('baseline_required')
        added = 0
        with self.db:
            for row in rows:
                old = self.db.execute('SELECT revision FROM seen WHERE message=?', (row['message_id'],)).fetchone()
                if old and old[0] == row['resource_refs_sha256']: continue
                refs = json.loads(row['resource_refs_json'])
                if not isinstance(refs, list): raise Rejected('resources_unknown')
                for ref in refs:
                    if not isinstance(ref, dict): raise Rejected('resources_unknown')
                    # Unknown type stays an explicit item; no invented ID.
                    request = dict(organization=organization, profile=profile,
                        message=row['message_id'], conversation=row['conversation_id'],
                        resource=ref, reference_revision=digest(canonical(ref).encode()))
                    key = digest(canonical(request).encode())
                    cur = self.db.execute('INSERT OR IGNORE INTO requests(key,request,created,updated) VALUES(?,?,?,?)',
                        (key, canonical(request), time.time(), time.time()))
                    added += bool(cur.rowcount)
                self.db.execute('INSERT OR REPLACE INTO seen VALUES(?,?)',
                    (row['message_id'], row['resource_refs_sha256']))
        return added

    def update(self, key, *, stage=None, status=None, error=None, task_key=None, delay=0):
        with self.db:
            self.db.execute('''UPDATE requests SET stage=COALESCE(?,stage),status=COALESCE(?,status),
              error=?,task_key=COALESCE(?,task_key),due=?,updated=? WHERE key=?''',
              (stage,status,error,task_key,time.time()+delay,time.time(),key))

    def pending(self, limit=2):
        return [dict(r) for r in self.db.execute('''SELECT * FROM requests
          WHERE status IN ('pending','retry','remote_uncertain') AND due<=?
          ORDER BY updated,key LIMIT ?''',(time.time(),limit))]

    def counts(self):
        return [dict(r) for r in self.db.execute('SELECT stage,status,count(*) AS count FROM requests GROUP BY stage,status')]

    def attempted(self,key):
        with self.db:
            self.db.execute('UPDATE requests SET attempts=attempts+1 WHERE key=?',(key,))


def message_rows(path):
    db = sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True)
    db.row_factory = sqlite3.Row
    try:
        # Local metadata, not historical chat API scanning.
        return [dict(r) for r in db.execute('''SELECT message_id,conversation_id,
          resource_refs_json,resource_refs_sha256 FROM messages WHERE current_visible=1''')]
    finally: db.close()
