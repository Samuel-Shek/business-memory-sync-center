"""Private, crash-safe model admission and usage receipts. No daily spending cap."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import csv
import fcntl
import hashlib
import io
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

VERSION = 1
PRICE_VERSION = 'deepseek-cny-20260928-export-v1'
TZ = timezone(timedelta(hours=8))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode()).hexdigest()


def now():
    return datetime.now(TZ).isoformat()


class Held(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise Held('guard_state_invalid')
    path.chmod(0o700)


def atomic(path, value):
    private_dir(path.parent)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(canonical(value)); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def connect(root):
    private_dir(root)
    path = root / 'usage.sqlite'
    if path.is_symlink():
        raise Held('guard_state_invalid')
    c = sqlite3.connect(path, timeout=15)
    path.chmod(0o600)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=FULL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS operations(
      operation_id TEXT PRIMARY KEY, project TEXT NOT NULL, purpose TEXT NOT NULL,
      source_version TEXT NOT NULL, request_sha TEXT NOT NULL, created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL, status TEXT NOT NULL, cache_hits INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS attempts(
      attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, slot INTEGER NOT NULL,
      provider TEXT NOT NULL, model TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
      status TEXT NOT NULL, response_id TEXT, response_sha TEXT, usage_json TEXT,
      input_chars INTEGER NOT NULL, image_count INTEGER NOT NULL,
      cost_cny_estimate REAL, price_version TEXT, billing_mode TEXT NOT NULL,
      UNIQUE(operation_id,slot));
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, at TEXT NOT NULL,
      kind TEXT NOT NULL, provider TEXT);
    ''')
    return c


def usage_fields(raw):
    if not isinstance(raw, dict):
        return {'status': 'unknown'}
    def number(*values):
        return next((v for v in values if isinstance(v, int) and not isinstance(v, bool) and v >= 0), None)
    details = raw.get('prompt_tokens_details') or {}
    details = details if isinstance(details, dict) else {}
    output_details = raw.get('completion_tokens_details') or {}
    output_details = output_details if isinstance(output_details, dict) else {}
    inp = number(raw.get('prompt_tokens'), raw.get('input_tokens'))
    hit = number(raw.get('prompt_cache_hit_tokens'), details.get('cached_tokens'))
    miss = number(raw.get('prompt_cache_miss_tokens'))
    if miss is None and inp is not None and hit is not None and hit <= inp:
        miss = inp - hit
    return {'status': 'reported' if inp is not None or number(raw.get('completion_tokens'), raw.get('output_tokens')) is not None else 'unknown', 'input_tokens': inp,
            'output_tokens': number(raw.get('completion_tokens'), raw.get('output_tokens')),
            'cache_hit_tokens': hit, 'cache_miss_tokens': miss,
            'reasoning_tokens': number(output_details.get('reasoning_tokens'))}


def estimate(provider, model, usage, stamp, tariff):
    # Only explicitly verified dates are priced. Unknown holiday schedules are not guessed.
    if provider != 'deepseek' or model != 'deepseek-flash':
        return None, None
    dt = datetime.fromisoformat(stamp).astimezone(TZ)
    day = dt.date().isoformat()
    mode = tariff.get('verified_days', {}).get(day)
    if mode not in ('workday', 'offday'):
        return None, None
    keys = ('cache_hit_tokens', 'cache_miss_tokens', 'output_tokens')
    if any(usage.get(k) is None for k in keys):
        return None, None
    busy = mode == 'workday' and (9 <= dt.hour < 12 or 14 <= dt.hour < 18)
    rates = tariff.get('peak_rates', (0.04, 2.0, 8.0)) if busy else tariff.get('offpeak_rates', (0.02, 1.0, 4.0))
    return sum(usage[k] * rate / 1_000_000 for k, rate in zip(keys, rates)), tariff.get('version', PRICE_VERSION)


class Admission:
    def __init__(self, root, prompt, images, purpose, context=None, tariff=None):
        self.root = Path(root)
        self.prompt = prompt
        self.image_count = len(images)
        context = context or {}
        # Labels are bounded machine identifiers; raw customer names never enter accounting.
        self.project = str(context.get('project', 'business-memory-bridge'))
        self.purpose = purpose
        if any(not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', x) for x in (self.project, purpose)):
            raise Held('guard_context_invalid')
        image_hashes = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in images]
        self.request_sha = digest([prompt, image_hashes])
        self.source_version = digest(context.get('source_version', self.request_sha))
        self.key = digest([VERSION, self.project, purpose, context.get('scope', ''),
                           self.source_version, context.get('contract', '')])
        self.tariff = tariff or {}
        self.fd = None
        self.db = None

    def __enter__(self):
        private_dir(self.root / 'locks')
        self.fd = os.open(self.root / 'locks' / (self.key + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd); self.fd = None
            raise Held('duplicate_inflight') from None
        try:
            self.db = connect(self.root)
            old = self.db.execute('SELECT * FROM operations WHERE operation_id=?', (self.key,)).fetchone()
            if old and old['request_sha'] != self.request_sha:
                raise Held('guard_source_conflict')
            self.db.execute('INSERT OR IGNORE INTO operations VALUES(?,?,?,?,?,?,?,?,0)',
                            (self.key, self.project, self.purpose, self.source_version,
                             self.request_sha, now(), now(), 'pending'))
            self.db.commit()
            for row in self.rows():
                if row['status'] not in ('started', 'uncertain'):
                    continue
                # A response may have been fsynced before SQLite was updated.
                path = self.root / 'responses' / (row['attempt_id'] + '.json')
                if path.exists():
                    try:
                        if path.is_symlink(): raise ValueError('symlink')
                        obj = json.loads(path.read_text())
                        if (obj.get('operation_id') != self.key or obj.get('request_sha') != self.request_sha
                            or obj.get('attempt_id') != row['attempt_id'] or not isinstance(obj.get('text'), str)):
                            raise ValueError('binding')
                        self._index_response(row['attempt_id'], obj)
                        self.event('response_reconciled', row['provider'])
                    except (ValueError, TypeError, OSError):
                        self.status('cache_corrupt')
                        raise Held('guard_cache_corrupt') from None
                else:
                    self.settle(row['attempt_id'], 'uncertain')
                    self.status('uncertain')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.db is not None:
            self.db.close()
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)

    def status(self, state):
        self.db.execute('UPDATE operations SET status=?,updated_at=? WHERE operation_id=?', (state, now(), self.key))
        self.db.commit()

    def rows(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM attempts WHERE operation_id=? ORDER BY slot', (self.key,))]

    def event(self, kind, provider=None):
        self.db.execute('INSERT INTO events(operation_id,at,kind,provider) VALUES(?,?,?,?)', (self.key, now(), kind, provider))
        if kind == 'cache_reused':
            self.db.execute('UPDATE operations SET cache_hits=cache_hits+1 WHERE operation_id=?', (self.key,))
        self.db.commit()

    def begin(self, slot, provider, model):
        aid = uuid.uuid4().hex
        self.db.execute('INSERT INTO attempts(attempt_id,operation_id,slot,provider,model,started_at,status,input_chars,image_count,billing_mode) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (aid, self.key, slot, provider, model, now(), 'started', len(self.prompt), self.image_count,
                         'subscription_quota' if provider in ('kimi', 'mimo') else 'metered' if provider == 'deepseek' else 'unknown'))
        self.status('running')
        return aid

    def plan(self, order):
        path = self.root / 'plans' / (self.key + '.json')
        if not path.exists():
            atomic(path, {'operation_id':self.key, 'order':order, 'tariff':self.tariff})
            return order
        try:
            if path.is_symlink(): raise ValueError('symlink')
            obj = json.loads(path.read_text())
            if obj['operation_id'] != self.key or not obj['order']: raise ValueError('binding')
            self.tariff = obj.get('tariff', {})
            return [tuple(pair) for pair in obj['order']]
        except (OSError, ValueError, KeyError, TypeError):
            raise Held('guard_state_invalid') from None

    def received(self, aid, text):
        # Text is in a private response vault, not in usage/statistical tables.
        payload = {'operation_id': self.key, 'request_sha': self.request_sha, 'attempt_id': aid,
                   'received_at': now(), 'text': str(text), 'response_id': getattr(text, 'response_id', None),
                   'usage': usage_fields(getattr(text, 'usage', None)),
                   'finish_error': getattr(text, 'finish_error', None)}
        atomic(self.root / 'responses' / (aid + '.json'), payload)
        self._index_response(aid, payload)

    def _index_response(self, aid, payload):
        stamp = payload['received_at']
        row = self.db.execute('SELECT provider,model,started_at FROM attempts WHERE attempt_id=?', (aid,)).fetchone()
        cost, version = estimate(row['provider'], row['model'], payload['usage'], row['started_at'], self.tariff)
        self.db.execute('UPDATE attempts SET ended_at=?,status=?,response_id=?,response_sha=?,usage_json=?,cost_cny_estimate=?,price_version=? WHERE attempt_id=?',
                        (stamp, 'received', payload['response_id'], digest(payload), canonical(payload['usage']), cost, version, aid))
        self.db.commit()

    def response(self, row):
        try:
            p = self.root / 'responses' / (row['attempt_id'] + '.json')
            if p.is_symlink():
                raise ValueError('symlink')
            obj = json.loads(p.read_text())
            if (digest(obj) != row['response_sha'] or obj.get('operation_id') != self.key
                or obj.get('request_sha') != self.request_sha or obj.get('attempt_id') != row['attempt_id']):
                raise ValueError('hash')
            return obj
        except (OSError, ValueError, TypeError):
            self.status('cache_corrupt')
            raise Held('guard_cache_corrupt') from None

    def settle(self, aid, status):
        self.db.execute('UPDATE attempts SET status=?,ended_at=COALESCE(ended_at,?) WHERE attempt_id=?', (status, now(), aid))
        self.db.commit()


def report(root, date=None, purpose=None, operations_only=False):
    path = Path(root) / 'usage.sqlite'
    if not path.is_file():
        return {'schema_version': VERSION, 'calls': [], 'operations': [], 'unknown_cost_is_zero': False}
    # Match the existing sync ledger's WAL-safe, SQL-read-only connection.
    # mode=rw cannot create a missing DB; query_only is enabled before any query.
    with sqlite3.connect(path.absolute().as_uri()+'?mode=rw', uri=True) as c:
        c.execute('PRAGMA query_only=ON')
        c.row_factory = sqlite3.Row
        q = 'SELECT a.*,o.project,o.purpose FROM attempts a JOIN operations o USING(operation_id) WHERE 1=1'
        params = []
        if date:
            q += ' AND substr(a.started_at,1,10)=?'; params.append(date)
        if purpose:
            q += ' AND o.purpose=?'; params.append(purpose)
        rows = [] if operations_only else [dict(r) for r in c.execute(q + ' ORDER BY a.started_at', params)]
        ops = [dict(r) for r in c.execute('SELECT operation_id,project,purpose,status,updated_at,cache_hits FROM operations ORDER BY updated_at DESC')]
        events = [] if operations_only else [dict(r) for r in c.execute('SELECT e.*,o.purpose,o.project FROM events e JOIN operations o USING(operation_id) WHERE (? IS NULL OR substr(e.at,1,10)=?) AND (? IS NULL OR o.purpose=?) ORDER BY e.at', (date,date,purpose,purpose))]
    groups = {}
    for row in rows:
        key = (row['started_at'][:13], row['purpose'], row['provider'])
        g = groups.setdefault(key, {'hour':key[0], 'purpose':key[1], 'provider':key[2], 'requests':0,
            'input_tokens':0, 'output_tokens':0, 'usage_unknown':0, 'estimated_cny':0, 'cost_unknown':0})
        u = json.loads(row.get('usage_json') or '{}'); g['requests'] += 1
        for field in ('input_tokens','output_tokens'): g[field] += u.get(field) or 0
        g['usage_unknown'] += u.get('input_tokens') is None or u.get('output_tokens') is None
        g['cost_unknown'] += row['cost_cny_estimate'] is None
        g['estimated_cny'] += row['cost_cny_estimate'] or 0
    return {'schema_version': VERSION, 'calls': rows, 'operations': ops, 'events': events,
            'by_hour_purpose': list(groups.values()), 'official_bill': None, 'unknown_cost_is_zero': False}



def export_csv(data):
    out = io.StringIO()
    fields = ['started_at','ended_at','project','purpose','provider','model','status','billing_mode',
              'input_tokens','output_tokens','cache_hit_tokens','cache_miss_tokens','cost_cny_estimate','price_version','attempt_id']
    w = csv.DictWriter(out, fields); w.writeheader()
    for row in data['calls']:
        d = {**row, **json.loads(row.get('usage_json') or '{}')}
        d['status'] = row['status']
        w.writerow({k: d.get(k) for k in fields})
    return out.getvalue()
