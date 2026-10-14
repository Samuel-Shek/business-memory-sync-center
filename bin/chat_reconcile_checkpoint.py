"""Durable, additive checkpoints for a fixed chat read window.

The collector owns writes under its existing lock. The supervisor reads the
persisted schedule; no other layer creates a second retry budget. Message bodies
and upstream error text never enter these tables.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import fcntl
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

TABLE = 'chat_reconcile_windows'
HARD_ERRORS = frozenset({'chat_read_auth_required', 'chat_read_blocked', 'chat_checkpoint_invalid'})


def stamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def parse(value):
    d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('checkpoint timestamps require a timezone')
    return d


def ensure_schema(c):
    # Additive extension: old collectors and listeners continue to read schema v3.
    c.executescript('''
    CREATE TABLE IF NOT EXISTS chat_reconcile_windows (
      window_id TEXT PRIMARY KEY, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
      status TEXT NOT NULL, list_complete INTEGER NOT NULL DEFAULT 0,
      attempt_count INTEGER NOT NULL DEFAULT 0, retry_round INTEGER NOT NULL DEFAULT 0,
      probe_count INTEGER NOT NULL DEFAULT 0, next_due TEXT, error_code TEXT,
      last_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS one_active_chat_window
      ON chat_reconcile_windows((1)) WHERE status <> 'complete';
    CREATE TABLE IF NOT EXISTS chat_reconcile_targets (
      window_id TEXT NOT NULL REFERENCES chat_reconcile_windows(window_id),
      conversation_id TEXT NOT NULL, conversation_name TEXT NOT NULL,
      historical_backfill INTEGER NOT NULL DEFAULT 0, ordinal INTEGER NOT NULL,
      range_start TEXT NOT NULL, range_end TEXT NOT NULL,
      complete INTEGER NOT NULL DEFAULT 0, attempt_count INTEGER NOT NULL DEFAULT 0,
      last_attempt_at TEXT, last_run_id TEXT, error_code TEXT, evidence_json TEXT,
      PRIMARY KEY(window_id,conversation_id));
    CREATE TABLE IF NOT EXISTS chat_reconcile_ranges (
      window_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
      range_start TEXT NOT NULL, range_end TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      PRIMARY KEY(window_id,conversation_id,range_start,range_end),
      FOREIGN KEY(window_id,conversation_id) REFERENCES chat_reconcile_targets(window_id,conversation_id));
    CREATE TABLE IF NOT EXISTS chat_reconcile_attempts (
      run_id TEXT PRIMARY KEY, window_id TEXT NOT NULL REFERENCES chat_reconcile_windows(window_id),
      started_at TEXT NOT NULL, completed_at TEXT, probe INTEGER NOT NULL,
      status TEXT NOT NULL, evidence_json TEXT);
    ''')


def active(c):
    r = c.execute("SELECT * FROM chat_reconcile_windows WHERE status<>'complete'").fetchone()
    return dict(r) if r else None


def get(c, window_id):
    return dict(c.execute('SELECT * FROM chat_reconcile_windows WHERE window_id=?', (window_id,)).fetchone())


def new_window(c, start, end, now):
    if parse(start) >= parse(end):
        raise ValueError('invalid checkpoint window')
    identifier = str(uuid.uuid4())
    c.execute("INSERT INTO chat_reconcile_windows(window_id,window_start,window_end,status,created_at,updated_at) VALUES(?,?,?,'pending',?,?)",
              (identifier, start, end, now, now))
    c.commit()
    return get(c, identifier)


def progress(c, identifier):
    w = get(c, identifier)
    if (w['status'] not in {'pending','retry_wait','probing','manual_required','complete'}
            or parse(w['window_start']) >= parse(w['window_end'])
            or any(not isinstance(w[k],int) or w[k]<0 for k in ('attempt_count','retry_round','probe_count'))):
        raise ValueError('invalid checkpoint metadata')
    if w.get('next_due'):
        parse(w['next_due'])
    n, done = c.execute('SELECT COUNT(*),COALESCE(SUM(complete),0) FROM chat_reconcile_targets WHERE window_id=?', (identifier,)).fetchone()
    return {**w, 'list_complete': bool(w['list_complete']), 'conversations_discovered': n,
            'conversations_complete': done, 'conversations_incomplete': n-done}


def read_progress(config):
    if not config.get('external_root'):
        return {}
    path = Path(config['external_root']) / 'dingtalk-chat/state/chat.sqlite'
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1) as c:
            c.row_factory = sqlite3.Row
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
                return {}
            w = c.execute("SELECT window_id FROM chat_reconcile_windows ORDER BY (status<>'complete') DESC,created_at DESC,rowid DESC LIMIT 1").fetchone()
            return progress(c, w[0]) if w else {}
    except (sqlite3.OperationalError, OSError):
        return {'status': 'unavailable', 'error_code': 'chat_checkpoint_busy', 'next_due': None}
    except (sqlite3.DatabaseError, ValueError, TypeError):
        return {'status': 'manual_required', 'error_code': 'chat_checkpoint_invalid', 'next_due': None}


def due(w, now):
    return w['status'] != 'manual_required' and (not w.get('next_due') or parse(w['next_due']) <= parse(now))


def delay(identifier, attempt):
    base = (60, 300, 900)[attempt-1] if 1 <= attempt <= 3 else 21600
    # Stable jitter survives process restarts and never shortens the base delay.
    fraction = int(hashlib.sha256(f'{identifier}:{attempt}'.encode()).hexdigest()[:8], 16) / 0xffffffff
    return base + int(base * .1 * fraction)


def acquire_database_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.with_suffix('.read.lock').open('a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ValueError('checkpoint_database_busy')
    return handle


def require_owner(c, identifier, run_id):
    w = get(c, identifier)
    if w['last_run_id'] != run_id:
        raise ValueError('checkpoint_owner_changed')
    return w


def begin(c, w, run_id, now):
    c.execute('BEGIN IMMEDIATE')
    try:
        fresh = get(c, w['window_id'])
        if (fresh['attempt_count'], fresh['last_run_id']) != (w['attempt_count'], w['last_run_id']) or not due(fresh, now):
            raise ValueError('checkpoint_claim_conflict')
        w = fresh
        probe = w['status'] == 'probing'
        count = w['attempt_count'] + 1
        failure_round = w['retry_round'] + 1
        reserved_due = (parse(now)+timedelta(seconds=delay(w['window_id'], failure_round))).isoformat(timespec='seconds')
        # Reserve before I/O. Database-bound flock prevents takeover during a long read.
        c.execute("UPDATE chat_reconcile_attempts SET status='interrupted',completed_at=? WHERE window_id=? AND status='running'", (now,w['window_id']))
        c.execute("UPDATE chat_reconcile_windows SET status=?,attempt_count=?,retry_round=retry_round+?,probe_count=probe_count+?,next_due=?,last_run_id=?,updated_at=? WHERE window_id=?",
                  ('probing' if failure_round>=4 else 'retry_wait', count, 1, int(probe), reserved_due,run_id,now,w['window_id']))
        c.execute("INSERT INTO chat_reconcile_attempts VALUES(?,?,?,?,?,'running',NULL)", (run_id,w['window_id'],now,None,int(probe)))
        c.commit()
        return probe
    except Exception:
        c.rollback()
        raise


def never_completed(c, ids):
    # Listed or not, a conversation without a full-history receipt was only ever read by window.
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone():
        return set()
    complete = {r[0] for r in c.execute('SELECT conversation_id FROM conversations WHERE last_complete_scan_at IS NOT NULL')}
    return {cid for cid in ids if cid not in complete}


def set_members(c, identifier, conversations, now, run_id, historical_start):
    # This is called only after the entire list passes its completeness contract.
    members = {}
    for item in conversations:
        if not isinstance(item, dict) or not isinstance(item.get('openConversationId'), str) or not item['openConversationId'].strip():
            raise ValueError('invalid conversation list member')
        cid = item['openConversationId'].strip()
        members[cid] = (str(item.get('conversationName') or ''), int(item.get('discoveredFrom') == 'local_event_history'))
    with c:
        c.execute('BEGIN IMMEDIATE')
        w = require_owner(c,identifier,run_id)
        incomplete = never_completed(c, members)
        members = {cid:(name,int(backfill or cid in incomplete)) for cid,(name,backfill) in members.items()}
        for ordinal, (cid,(name,backfill)) in enumerate(members.items()):
            c.execute('INSERT INTO chat_reconcile_targets(window_id,conversation_id,conversation_name,historical_backfill,ordinal,range_start,range_end) VALUES(?,?,?,?,?,?,?)',
                      (identifier,cid,name,backfill,ordinal,historical_start if backfill else w['window_start'],w['window_end']))
            c.execute('INSERT INTO chat_reconcile_ranges(window_id,conversation_id,range_start,range_end) VALUES(?,?,?,?)',
                      (identifier,cid,historical_start if backfill else w['window_start'],w['window_end']))
        c.execute('UPDATE chat_reconcile_windows SET list_complete=1,updated_at=? WHERE window_id=?',(now,identifier))


def targets(c, identifier, probe=False):
    rows = c.execute('SELECT * FROM chat_reconcile_targets WHERE window_id=? AND complete=0 ORDER BY COALESCE(last_attempt_at,\'\'),ordinal', (identifier,)).fetchall()
    if probe:
        rows = rows[:1]
    result = []
    for r in rows:
        segment = c.execute("SELECT range_start,range_end FROM chat_reconcile_ranges WHERE window_id=? AND conversation_id=? AND status='pending' ORDER BY range_start LIMIT 1", (identifier,r['conversation_id'])).fetchone()
        if segment is None:
            raise ValueError('checkpoint_pending_range_missing')
        result.append({'openConversationId':r['conversation_id'],'conversationName':r['conversation_name'],
                       'discoveredFrom':'local_event_history' if r['historical_backfill'] else 'checkpoint',
                       'historicalBackfill':bool(r['historical_backfill']),
                       'range_start':segment['range_start'],'range_end':segment['range_end']})
    return result


def safe_evidence(e):
    allowed = {'error_code','retryable','retry_after_seconds','failure_sha256','elapsed_ms','phase','source_code','http_status','pages_fetched','failed_count','attempt'}
    return {k:v for k,v in e.items() if k in allowed and (v is None or isinstance(v,(str,int,float,bool)))}


def record_target(c, identifier, cid, run_id, now, complete, evidence, range_start=None, range_end=None):
    # Caller commits this together with the actual message upserts and scan receipt.
    w = require_owner(c,identifier,run_id)
    if w['status'] == 'complete':
        raise ValueError('checkpoint_already_complete')
    e = safe_evidence(evidence or {})
    segment = c.execute("SELECT * FROM chat_reconcile_ranges WHERE window_id=? AND conversation_id=? AND status='pending' ORDER BY range_start LIMIT 1",(identifier,cid)).fetchone()
    if segment is None or (range_start and (range_start,range_end) != (segment['range_start'],segment['range_end'])):
        raise ValueError('checkpoint_segment_changed')
    split = False
    if not complete and e.get('source_code') == 'pagination_limit':
        start,end = parse(segment['range_start']),parse(segment['range_end'])
        seconds = int((end-start).total_seconds())
        if seconds > 1:
            mid = (start+timedelta(seconds=seconds//2)).isoformat(timespec='seconds')
            c.execute("UPDATE chat_reconcile_ranges SET status='split' WHERE window_id=? AND conversation_id=? AND range_start=? AND range_end=?",(identifier,cid,segment['range_start'],segment['range_end']))
            for lower,upper in ((segment['range_start'],mid),(mid,segment['range_end'])):
                c.execute('INSERT INTO chat_reconcile_ranges(window_id,conversation_id,range_start,range_end) VALUES(?,?,?,?)',(identifier,cid,lower,upper))
            split = True
        else:
            e.update(error_code='chat_read_blocked',retryable=False,source_code='pagination_limit_same_second')
    elif complete:
        c.execute("UPDATE chat_reconcile_ranges SET status='complete' WHERE window_id=? AND conversation_id=? AND range_start=? AND range_end=?",(identifier,cid,segment['range_start'],segment['range_end']))
    complete = c.execute("SELECT COUNT(*) FROM chat_reconcile_ranges WHERE window_id=? AND conversation_id=? AND status='pending'",(identifier,cid)).fetchone()[0] == 0
    if not complete:
        error = e.get('error_code','chat_read_unclassified')
        wait = int(e.get('retry_after_seconds') or 0)
        deadline = (parse(now)+timedelta(seconds=wait)).isoformat(timespec='seconds')
        if error in HARD_ERRORS:
            c.execute("UPDATE chat_reconcile_windows SET status='manual_required',error_code=?,next_due=NULL WHERE window_id=?",(error,identifier))
        elif wait and (not w.get('next_due') or parse(deadline)>parse(w['next_due'])):
            c.execute('UPDATE chat_reconcile_windows SET next_due=?,error_code=? WHERE window_id=?',(deadline,error,identifier))
    c.execute('UPDATE chat_reconcile_targets SET complete=?,attempt_count=attempt_count+1,last_attempt_at=?,last_run_id=?,error_code=?,evidence_json=? WHERE window_id=? AND conversation_id=?',
              (int(complete),now,run_id,None if complete or split else e.get('error_code','chat_read_unclassified'),json.dumps(e,sort_keys=True),identifier,cid))
    return {'target_complete':complete,'partitioned':split,'evidence':e}


def finish(c, identifier, run_id, now, failures, probe=False, fatal=None, commit=True):
    if not c.in_transaction:
        c.execute('BEGIN IMMEDIATE')
    require_owner(c,identifier,run_id)
    w = progress(c, identifier)
    if w['status'] == 'complete':
        if commit:
            c.commit()
        return w
    done = w['list_complete'] and w['conversations_incomplete'] == 0 and not fatal
    codes = [str(e.get('error_code') or 'chat_read_unclassified') for e in failures]
    if fatal:
        codes.insert(0, fatal)
    hard = w.get('error_code') if w['status']=='manual_required' else next((code for code in codes if code in HARD_ERRORS), None)
    wait = max([int(e.get('retry_after_seconds') or 0) for e in failures] or [0])
    if done and not hard:
        status, error, next_due = 'complete', None, None
    elif hard:
        status, error, next_due = 'manual_required', hard, None
    elif not failures and not fatal:
        # A healthy probe permits a scheduled continuation, not a success claim.
        status, error = 'pending', 'chat_reconcile_incomplete'
        next_due = (parse(now)+timedelta(seconds=max(60,wait))).isoformat(timespec='seconds')
    else:
        status = 'probing' if w['retry_round']>=4 else 'retry_wait'
        error = codes[0] if codes else 'chat_reconcile_incomplete'
        next_due = (parse(now)+timedelta(seconds=max(wait,delay(identifier,w['retry_round'])))).isoformat(timespec='seconds')
    if next_due and w.get('next_due') and status not in {'pending'}:
        next_due = max(parse(next_due),parse(w['next_due'])).isoformat(timespec='seconds')
    if status in {'pending','complete'} and not failures and not fatal:
        c.execute('UPDATE chat_reconcile_windows SET retry_round=MAX(0,retry_round-1) WHERE window_id=?',(identifier,))
    c.execute('UPDATE chat_reconcile_windows SET status=?,error_code=?,next_due=?,updated_at=? WHERE window_id=?',(status,error,next_due,now,identifier))
    c.execute('UPDATE chat_reconcile_attempts SET completed_at=?,status=?,evidence_json=? WHERE run_id=?',
              (now,'success' if status=='complete' else status,json.dumps([safe_evidence(e) for e in failures],sort_keys=True),run_id))
    if commit:
        c.commit()
    return progress(c, identifier)


def recovery_receipt(w, waiting=False):
    return {'error_code': ('chat_read_cooldown' if waiting and w['status']!='manual_required' else w.get('error_code')) or 'chat_reconcile_incomplete',
            'next_retry_at': w.get('next_due'), 'retry_after_seconds': max(0,int((parse(w['next_due'])-parse(stamp())).total_seconds())) if w.get('next_due') else 0,
            'checkpoint_status':w['status'], 'attempt':w['attempt_count']}
