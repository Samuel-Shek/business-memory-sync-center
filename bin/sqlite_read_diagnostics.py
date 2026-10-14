#!/usr/bin/env python3
"""Content-free SQLite read diagnostics and strictly bounded transient retries."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any, Callable


def storage_evidence(path: Path) -> dict[str, Any]:
    result = {}
    for role, candidate in (("database", path), ("wal", Path(str(path) + "-wal")),
                            ("shm", Path(str(path) + "-shm")), ("parent", path.parent)):
        try:
            value = candidate.lstat()
            result[role] = {"exists": True, "regular": stat.S_ISREG(value.st_mode),
                            "symlink": stat.S_ISLNK(value.st_mode), "mode": oct(stat.S_IMODE(value.st_mode)),
                            "owner_matches": value.st_uid == os.getuid(), "size": value.st_size,
                            "readable": os.access(candidate, os.R_OK), "writable": os.access(candidate, os.W_OK)}
        except OSError as exc:
            result[role] = {"exists": False, "errno": exc.errno}
    return result


def error_evidence(exc: BaseException, path: Path, phase: str, attempt: int = 1) -> dict[str, Any]:
    code = getattr(exc, "sqlite_errorcode", None)
    name = getattr(exc, "sqlite_errorname", None)
    # Python 3.9 does not expose SQLite result attributes. Categorize only
    # exact known messages; never retain arbitrary exception text.
    message = str(exc).lower()
    fallback = {"database is locked": (5, "SQLITE_BUSY"), "database is busy": (5, "SQLITE_BUSY"),
                "unable to open database file": (14, "SQLITE_CANTOPEN"),
                "database disk image is malformed": (11, "SQLITE_CORRUPT"),
                "attempt to write a readonly database": (8, "SQLITE_READONLY")}.get(message)
    inferred = code is None and fallback is not None
    if inferred:
        code, name = fallback
    return {"database_role": "sync_alert_ledger", "phase": phase, "attempt": attempt,
            "sqlite_code": code if type(code) is int else None,
            "sqlite_name": name if isinstance(name, str) and name.startswith("SQLITE_") else None,
            "code_inferred_from_exact_message": inferred, "sqlite_version": sqlite3.sqlite_version,
            "storage": storage_evidence(path)}


def read_rows(path: Path, sql: str, parameters: tuple, *, connector: Callable,
              sleeper: Callable[[float], None] = time.sleep) -> list:
    for attempt in range(1, 4):
        connection = None
        phase = "connect"
        try:
            connection = connector(path, readonly=True)
            phase = "execute"
            cursor = connection.execute(sql, parameters)
            phase = "fetch"
            return cursor.fetchall()
        except sqlite3.Error as exc:
            evidence = error_evidence(exc, path, phase, attempt)
            exc.safe_sqlite_evidence = evidence
            code = evidence["sqlite_code"]
            # CANTOPEN, corruption, authorization and persistent/unknown
            # errors never get a fallback or an empty-success result.
            transient = type(code) is int and code & 0xff == 5
            if not transient or attempt == 3:
                raise
        finally:
            if connection is not None:
                connection.close()
        sleeper(0.05 * attempt)
    raise AssertionError("bounded_read_unreachable")
