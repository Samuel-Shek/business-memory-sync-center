"""Persistent pre-ready host budget; process restarts do not reset 0/2/1."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


class BudgetBlocked(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


class ListenerBudget:
    def __init__(self, path: Path, *, clock=None):
        self.path = path
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def read(self):
        if not self.path.exists():
            return {'schema_version': 1, 'attempt_count': 0, 'max_attempts': 2, 'state': 'new'}
        try:
            row = json.loads(self.path.read_text())
            if row.get('schema_version') != 1 or not isinstance(row['attempt_count'], int):
                raise ValueError()
            return row
        except (OSError, ValueError, KeyError):
            raise BudgetBlocked('event_budget_invalid')

    def save(self, row):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(row, sort_keys=True))
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def reserve(self):
        row = self.read()
        now = self.clock()
        if row['state'] == 'stopped':
            row = {'schema_version': 1, 'attempt_count': 0, 'max_attempts': 2, 'state': 'new'}
        due = parse_time(row.get('next_retry_at'))
        if row.get('guard') and (due is None or now < due):
            raise BudgetBlocked('event_subscription_' + row['guard'])
        if due and now < due:
            raise BudgetBlocked('event_subscription_cooldown')
        if row['attempt_count'] >= row['max_attempts']:
            raise BudgetBlocked('event_retry_budget_exhausted')
        row.update(state='starting', attempt_count=row['attempt_count'] + 1,
                   attempt_at=now.isoformat(), pid=os.getpid(), guard=None)
        self.save(row)
        return row

    def failed(self, retryable, *, retry_after=0, next_retry_at=None, code='event_not_ready', guard=None):
        row = self.read()
        maximum = 3 if retryable is True else 1 if retryable is False else 2
        row['max_attempts'] = maximum if row['attempt_count'] == 1 else min(row['max_attempts'], maximum)
        due = self.clock() + timedelta(seconds=max(1, retry_after))
        explicit = parse_time(next_retry_at)
        if explicit and explicit > due:
            due = explicit
        row.update(state='failed', last_error_code=code, last_failure_at=self.clock().isoformat(),
                   next_retry_at=due.isoformat() if not guard or explicit or retry_after else None, guard=guard)
        self.save(row)
        return row

    def ready(self):
        row = self.read()
        row.update(state='ready', ready_at=self.clock().isoformat(), guard=None, next_retry_at=None)
        self.save(row)

    def stopped(self):
        row = self.read()
        if row['state'] == 'ready':
            row.update(state='stopped', stopped_at=self.clock().isoformat())
            self.save(row)
