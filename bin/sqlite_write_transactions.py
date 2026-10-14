"""Bounded retries for local ledger transactions, never for business operations.

Callbacks must use the supplied connection only, return materialized values, and
have no effects outside that transaction. In particular they cannot commit,
launch work, notify, or call a provider. A successful commit is never replayed.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import os
import sqlite3
import time
import uuid
import sqlite_attribution as attribution
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sqlite_read_diagnostics import error_evidence

MAX_ATTEMPTS = 3
WAIT_BUDGET_SECONDS = 15.0
ATTEMPT_WAIT_SECONDS = 5.0


def _record(path: Path, value: dict[str, Any]) -> None:
    """Diagnostics must never turn a committed write into a retryable failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
    except OSError:
        pass


def write_transaction(path: Path, operation: Callable[[sqlite3.Connection], Any], *,
                      connector: Callable, phase: str, diagnostic_path: Path | None = None,
                      sleeper: Callable = time.sleep, clock: Callable = time.monotonic,
                      budget: float = WAIT_BUDGET_SECONDS,
                      attempt_wait: float = ATTEMPT_WAIT_SECONDS) -> Any:
    started = clock()
    started_ns = time.time_ns()
    transaction_id = uuid.uuid4().hex
    context = dict(attribution.origin(), transaction_id=transaction_id, operation=phase)
    deadline = started + min(WAIT_BUDGET_SECONDS, max(0.001, budget))
    destination = diagnostic_path or path.parent / "sqlite-write-contention.jsonl"
    last_evidence = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        connection = None
        committed = False
        stage = "connect"
        transaction_outcome = "failed_before_commit"
        attribution.record(path, dict(context, event="attempt_started", attempt=attempt))
        try:
            remaining = max(0, deadline - clock())
            connection = connector(path, timeout=min(attempt_wait, remaining))
            connection.execute("PRAGMA busy_timeout=%d" % max(0, int(min(attempt_wait, deadline - clock()) * 1000)))
            stage = "begin"
            connection.execute("BEGIN IMMEDIATE")
            attribution.record(path, dict(context, event="write_lock_acquired", attempt=attempt))
            stage = "execute"
            result = operation(connection)
            # The callback must not commit. Otherwise an uncertain failure later
            # could replay increments, events, or receipts that already exist.
            if not connection.in_transaction:
                raise RuntimeError("ledger_callback_ended_transaction")
            connection.execute("PRAGMA busy_timeout=%d" % max(0, int(min(attempt_wait, deadline - clock()) * 1000)))
            stage = "commit"
            connection.commit()
            committed = True
            transaction_outcome = "committed"
        except sqlite3.Error as exc:
            evidence = error_evidence(exc, path, stage, attempt)
            evidence.update(operation=phase, elapsed_seconds=round(clock() - started, 4),
                            retry_count=attempt - 1, pid=os.getpid(), lock_owner="unknown",
                            recorded_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            code = evidence["sqlite_code"]
            evidence["transaction_id"] = transaction_id
            evidence["attribution"] = (attribution.snapshot(path, since_ns=started_ns-60_000_000_000,
                                                           timeout=max(0, deadline-clock()))
                if type(code) is int and code & 0xff == 5 else
                {"requester": attribution.origin(), "lock_owner": "not_applicable", "status": "non_contention_error"})
            evidence["lock_owner"] = evidence["attribution"]["lock_owner"]
            exc.safe_sqlite_evidence = evidence
            code = evidence["sqlite_code"]
            # A commit that ended the transaction and then reported an error is
            # ambiguous. Never risk applying it a second time.
            uncertain = stage == "commit" and connection is not None and not connection.in_transaction
            if uncertain:
                transaction_outcome = "commit_outcome_ambiguous_no_replay"
            retry = (not committed and not uncertain and type(code) is int and code & 0xff == 5
                     and attempt < MAX_ATTEMPTS and clock() < deadline)
            evidence["commit_outcome_ambiguous"] = uncertain
            evidence["outcome"] = "commit_outcome_ambiguous_no_replay" if uncertain else "retrying" if retry else "failed"
            _record(destination, evidence)
            last_evidence = evidence
            if not retry:
                raise
        else:
            if last_evidence is not None:
                _record(destination, {**last_evidence, "attempt": attempt, "retry_count": attempt - 1,
                                      "elapsed_seconds": round(clock() - started, 4), "outcome": "recovered"})
            elif clock() - started >= .05:
                _record(destination, dict(context, outcome="completed_after_wait",
                        elapsed_seconds=round(clock()-started, 4), attempt=attempt,
                        recent_activity=attribution.recent(path, started_ns-60_000_000_000)))
            return result
        finally:
            if connection is not None:
                try:
                    if not committed and connection.in_transaction:
                        connection.rollback()
                        if transaction_outcome != "commit_outcome_ambiguous_no_replay":
                            transaction_outcome = "rolled_back"
                finally:
                    connection.close()
            attribution.record(path, dict(context, event="attempt_finished", attempt=attempt,
                                           outcome=transaction_outcome, elapsed_seconds=round(clock()-started, 4)))
        sleeper(min(0.1 * attempt, max(0, deadline - clock())))
    raise AssertionError("bounded_write_unreachable")
