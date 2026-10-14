"""Fail-closed, schema-2 mail rollback; no production imports or journal writes.

Contract for BOTH exception rollback and interrupted-journal recovery:
* The caller holds the SAME exclusive memory-writer lock from preparation through
  rollback, journal persistence, index rebuild and validation. ``lock_held`` is an
  assertion, not lock acquisition. All writers must cooperate; hashes alone are
  not an atomic compare-and-swap against an uncooperative concurrent process.
* Backups are made by the caller before make_entry(); entries and intended AFTER
  bytes must be durably journaled BEFORE those bytes are written. None means an
  absent file, distinct from SHA256(empty bytes). Do not hash a file after a crash
  and claim that observed content belongs to this transaction.
* Pass backup_root from the trusted run directory, never from journal content.
  safe_target is a trusted predicate, e.g. memory.safe_target plus exact indexes.
  derived_targets is a trusted, exact index allowlist, NOT inferred from entries.
* Index entries have derived=True and NO backup. They still require known
  before/after fingerprints for the full-batch conflict check. Stage/render index
  bytes and journal their after hash before publishing them. If the current index
  API cannot do that, fail closed on an unknown index hash; never invent ownership.
* On conflict, nothing (including journal, backups, indexes) is written here.
  Return value contains sanitized conflict codes/paths/hashes for the entrypoint
  to record. No callback is invoked to record or rebuild during preflight.
* On restored, persist state=restored_pending_validation, then REBUILD the returned
  derived paths from current source files and validate, then persist a terminal
  state. Never copy an index backup. A restart already in pending-validation state
  should resume rebuild/validation at the entrypoint, not run source rollback.
  Do not mark complete on rebuild/validation failure.
* Each restore is atomic, not the whole batch. An I/O failure can leave a partial
  rollback; RecoveryWriteError lists completed paths. Keep the journal recoverable:
  a retry accepts already-restored before hashes. Terminal journals are no-ops.

The helper only reads the files explicitly named by the caller/journal. It has
no CLI, logging, scanning, refresh, database, network, or import-time side effects.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

PathCheck = Callable[[Path], bool]
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL = {"committed", "rolled_back", "rolled_back_after_recovery"}
_ACTIVE = {"prepared", "applying", "restored_pending_validation"}


class RecoveryConflict(ValueError):
    """Preparation failure; .conflict contains metadata, never file content."""

    def __init__(self, code: str, **details: Any):
        self.conflict = {"code": code, **details}
        super().__init__(code)


class RecoveryWriteError(OSError):
    """Actual I/O failure AFTER preflight; do not misreport as zero-write conflict."""

    def __init__(self, target: Path, completed: list[str]):
        self.target = str(target)
        self.completed = tuple(completed)
        super().__init__(f"rollback_io_failed: {target}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path(value: Any) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise RecoveryConflict("unsafe_path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise RecoveryConflict("unsafe_path", path=str(path))
    # Check BEFORE resolve: resolve would hide symlinks, including parent links.
    for part in (*reversed(path.parents), path):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or (part != path and not stat.S_ISDIR(mode)):
            raise RecoveryConflict("unsafe_path", path=str(path))
    return path


def _read(path: Path) -> tuple[bytes | None, int]:
    _path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, 0o644
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RecoveryConflict("not_private_regular_file", path=str(path))
        return stream.read(), stat.S_IMODE(info.st_mode)


def _fingerprint(data: bytes | None) -> str | None:
    return None if data is None else sha256_bytes(data)


def _hash(value: Any) -> bool:
    return value is None or (isinstance(value, str) and _HASH.fullmatch(value) is not None)


def _root(value: Path) -> Path:
    root = _path(value)
    if not root.is_dir():
        raise RecoveryConflict("unsafe_backup_root", path=str(root))
    return root


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _target(value: Any, safe_target: PathCheck, backup_root: Path) -> Path:
    target = _path(value)
    if _inside(target, backup_root) or not safe_target(target):
        raise RecoveryConflict("unsafe_target", path=str(target))
    return target


def make_entry(
    target: Path, after: bytes | None, *, backup: Path | None,
    backup_root: Path, safe_target: PathCheck, derived: bool = False,
) -> dict[str, Any]:
    """Read-only constructor; caller already made the backup while holding lock.

    after is the EXACT bytes to publish (UTF-8 encode text at the caller). A
    derived entry must use backup=None. Existing ordinary files require a backup
    whose bytes match the current target. Does not modify the target or backup.
    """
    if after is not None and not isinstance(after, bytes):
        raise RecoveryConflict("after_must_be_bytes")
    if type(derived) is not bool:
        raise RecoveryConflict("invalid_derived")
    root = _root(backup_root)
    target = _target(target, safe_target, root)
    before, mode = _read(target)
    entry = {
        "target": str(target), "backup": str(backup) if backup is not None else None,
        "existed": before is not None, "mode": mode, "derived": derived,
        "before_sha256": _fingerprint(before), "after_sha256": _fingerprint(after),
        "backup_sha256": _fingerprint(before) if not derived else None,
    }
    _check_entry(entry, root, safe_target, {target} if derived else set())
    return entry


@dataclass(frozen=True)
class _Restore:
    target: Path
    before: str | None
    current: str | None
    backup_bytes: bytes | None
    mode: int
    derived: bool
    backup: Path | None


def _check_entry(
    entry: Any, root: Path, safe_target: PathCheck, derived_targets: set[Path],
) -> _Restore:
    required = {"target", "backup", "existed", "mode", "derived",
                "before_sha256", "after_sha256", "backup_sha256"}
    if not isinstance(entry, dict) or not required.issubset(entry):
        raise RecoveryConflict("missing_schema2_fields")
    target = _target(entry["target"], safe_target, root)
    before, after = entry["before_sha256"], entry["after_sha256"]
    if not all(_hash(entry[key]) for key in ("before_sha256", "after_sha256", "backup_sha256")):
        raise RecoveryConflict("invalid_hash", path=str(target))
    if type(entry["existed"]) is not bool or entry["existed"] != (before is not None):
        raise RecoveryConflict("invalid_existed", path=str(target))
    if type(entry["derived"]) is not bool or entry["derived"] != (target in derived_targets):
        raise RecoveryConflict("derived_allowlist_mismatch", path=str(target))
    mode = entry["mode"]
    if type(mode) is not int or not 0 <= mode <= 0o777:
        raise RecoveryConflict("invalid_mode", path=str(target))
    backup, saved = None, None
    if entry["derived"] or before is None:
        if entry["backup"] is not None or entry["backup_sha256"] is not None:
            raise RecoveryConflict("unexpected_backup", path=str(target))
    else:
        backup = _path(entry["backup"])
        if backup == root or not _inside(backup, root):
            raise RecoveryConflict("unsafe_backup", path=str(backup))
        saved, _ = _read(backup)
        if entry["backup_sha256"] != before or _fingerprint(saved) != before:
            raise RecoveryConflict("backup_hash_mismatch", path=str(backup))
    current, _ = _read(target)
    fingerprint = _fingerprint(current)
    if fingerprint not in (before, after):
        raise RecoveryConflict("current_hash_conflict", path=str(target),
                               current_sha256=fingerprint, before_sha256=before,
                               after_sha256=after)
    return _Restore(target, before, fingerprint, saved, mode, entry["derived"], backup)


def _atomic_restore(target: Path, data: bytes, mode: int) -> None:
    # Never chmod/mkdir a source parent directory. No text decode/newline rewrite.
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.rollback-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_parent(target: Path) -> None:
    """Make the replace/unlink durable before the caller persists next_state."""
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rollback_transaction(
    journal: dict[str, Any], *, backup_root: Path, safe_target: PathCheck,
    derived_targets: Iterable[Path] = (), lock_held: bool = False,
) -> dict[str, Any]:
    """Shared exception/restart helper. Preflight EVERY entry before ANY write.

    Returns status=conflict/restored/noop; conflicts are returned, never persisted.
    Does not mutate journal. Restored returns next_state and rebuild_required.
    Partial I/O errors raise RecoveryWriteError; retry the same durable journal.
    """
    def conflict(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"status": "conflict", "changed": [], "rebuild_required": [],
                "conflicts": items}

    if lock_held is not True:
        return conflict([{"code": "exclusive_lock_required"}])
    if not isinstance(journal, dict) or type(journal.get("schema_version")) is not int or journal.get("schema_version") != 2:
        return conflict([{"code": "legacy_or_unsupported_schema"}])
    state = journal.get("state")
    if not isinstance(state, str) or state not in _ACTIVE | _TERMINAL:
        return conflict([{"code": "invalid_state"}])
    if state in _TERMINAL:
        return {"status": "noop", "changed": [], "rebuild_required": [], "conflicts": []}
    entries = journal.get("paths")
    if not isinstance(entries, list) or not entries:
        return conflict([{"code": "invalid_entries"}])
    try:
        root = _root(backup_root)
        indexes = {_target(p, safe_target, root) for p in derived_targets}
    except (RecoveryConflict, OSError, ValueError, TypeError) as exc:
        detail = exc.conflict if isinstance(exc, RecoveryConflict) else {"code": "path_policy_failed"}
        return conflict([detail])
    plans, errors = [], []
    targets: set[Path] = set()
    backups: set[Path] = set()
    identities: set[tuple[int, int]] = set()
    # Continue collecting conflicts, but perform no writes, not even temp files.
    for index, entry in enumerate(entries):
        try:
            plan = _check_entry(entry, root, safe_target, indexes)
            if plan.target in targets or (plan.backup is not None and plan.backup in backups):
                raise RecoveryConflict("duplicate_path", path=str(plan.target))
            # Also reject differently-cased aliases on case-insensitive volumes.
            for path in (plan.target, plan.backup):
                if path is not None and path.exists():
                    info = path.stat()
                    identity = (info.st_dev, info.st_ino)
                    if identity in identities:
                        raise RecoveryConflict("duplicate_path", path=str(path))
                    identities.add(identity)
            targets.add(plan.target)
            if plan.backup is not None:
                backups.add(plan.backup)
            if state == "restored_pending_validation" and not plan.derived and plan.current != plan.before:
                raise RecoveryConflict("pending_validation_source_changed", path=str(plan.target))
            plans.append(plan)
        except (RecoveryConflict, OSError, ValueError, TypeError) as exc:
            detail = exc.conflict if isinstance(exc, RecoveryConflict) else {"code": "entry_unreadable_or_invalid"}
            errors.append({"entry_index": index, **detail})
    if errors:
        return conflict(errors)
    changed: list[str] = []
    for plan in plans:
        if plan.derived or plan.current == plan.before:
            continue
        try:
            if plan.before is None:
                plan.target.unlink()
            else:
                assert plan.backup_bytes is not None
                _atomic_restore(plan.target, plan.backup_bytes, plan.mode)
            changed.append(str(plan.target))
            _sync_parent(plan.target)
        except OSError as exc:
            raise RecoveryWriteError(plan.target, changed) from exc
    return {"status": "restored", "changed": changed, "conflicts": [],
            "next_state": "restored_pending_validation",
            "rebuild_required": [str(plan.target) for plan in plans if plan.derived]}
