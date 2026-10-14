#!/usr/bin/env python3
"""Read-only SMB folder observer that queues trusted local schema-v2 events.

The watcher never writes the bridge database, Obsidian, or the SMB source.  It
only reads source metadata/content, reads SQLite in query-only mode, and
atomically creates owner-only event bundles for receiver.py.
"""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import json
import multiprocessing
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

os.umask(0o077)

ROOT = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync'))
CONFIG_PATH = ROOT / "config.json"
WATCH_INBOX = ROOT / "watch_inbox"
DB_PATH = ROOT / "state" / "bridge.sqlite"
STATE_ROOT = ROOT / "state" / "folder_watcher"
LOG_PATH = ROOT / "logs" / "folder_watcher.jsonl"
HEALTH_PATH = ROOT / "state" / "folder_watcher_health.json"
LOCK_PATH = ROOT / "state" / "folder_watcher.lock"
TRIGGER_PATH = ROOT / "state" / "force_scan.trigger"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
SANDBOX_FLAG = "BUSINESS_WATCHER_SOURCE_WRITE_SANDBOX"

WATCH_EVENT_NAMESPACE = uuid.UUID('ea6e317b-1a39-5ddc-bb6c-8282a7f53461')
WATCH_ENTRY_NAMESPACE = uuid.UUID('60d4044f-aeaf-569d-93d2-436fa15fe9c5')
SAFE_SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TEMP_SUFFIXES = {".tmp", ".partial", ".part", ".crdownload", ".swp", ".swx", ".download"}
TEMP_NAMES = {".ds_store", "desktop.ini", "thumbs.db"}
CACHE_DIRECTORIES = {
    "$recycle.bin", "system volume information", ".spotlight-v100", ".trashes",
    ".temporaryitems", ".fseventsd", "__macosx",
}
RUNNING = True
LOCK_FD = None
HEALTH_STATE = {
    "process_started_at": None,
    "process_starts_total": 0,
    "restart_count": 0,
    "fatal_errors_total": 0,
    "consecutive_scan_timeouts": 0,
    "last_error": None,
    "last_successful_quick_scan_at": None,
    "last_successful_full_scan_at": None,
}


class WatcherError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def mtime_iso(mtime_ns):
    seconds, nanoseconds = divmod(int(mtime_ns), 1_000_000_000)
    stamp = datetime.fromtimestamp(seconds, timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanoseconds // 1_000:06d}Z"


def append_log(event, **fields):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": now_iso(), "event": event, **fields}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def existing_fatal_count():
    if not LOG_PATH.is_file():
        return 0
    count = 0
    try:
        with LOG_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    if json.loads(line).get("event") == "folder_watcher_fatal":
                        count += 1
                except (json.JSONDecodeError, AttributeError):
                    continue
    except OSError:
        return 0
    return count


def initialize_health_state(process_started_at=None):
    previous = {}
    if HEALTH_PATH.is_file():
        try:
            previous = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    HEALTH_STATE.update(
        {
            "process_started_at": process_started_at or now_iso(),
            "process_starts_total": int(previous.get("process_starts_total", 0) or 0) + 1,
            "restart_count": int(previous.get("process_starts_total", 0) or 0),
            "fatal_errors_total": max(
                int(previous.get("fatal_errors_total", 0) or 0), existing_fatal_count()
            ),
            "consecutive_scan_timeouts": 0,
            "last_error": previous.get("last_error"),
            "last_successful_quick_scan_at": previous.get("last_successful_quick_scan_at"),
            "last_successful_full_scan_at": previous.get("last_successful_full_scan_at"),
        }
    )


def note_health_failure(code):
    HEALTH_STATE["last_error"] = {"code": code, "at": now_iso()}
    if code == "scan_timeout":
        HEALTH_STATE["consecutive_scan_timeouts"] = int(
            HEALTH_STATE.get("consecutive_scan_timeouts", 0)
        ) + 1
    else:
        HEALTH_STATE["consecutive_scan_timeouts"] = 0


def note_health_success(full_scan):
    stamp = now_iso()
    HEALTH_STATE["last_successful_quick_scan_at"] = stamp
    if full_scan:
        HEALTH_STATE["last_successful_full_scan_at"] = stamp
    HEALTH_STATE["consecutive_scan_timeouts"] = 0


def write_health(status, detail, **extra):
    atomic_json(
        HEALTH_PATH,
        {
            "schema_version": 2,
            "status": status,
            "detail": detail,
            "updated_at": now_iso(),
            "source_write_sandbox": os.environ.get(SANDBOX_FLAG) == "1",
            **HEALTH_STATE,
            **extra,
        },
    )


def bounded_sleep_seconds(deadline, current=None):
    """Return at most one second and never a negative sleep interval."""
    now = time.monotonic() if current is None else current
    return max(0.0, min(1.0, deadline - now))


def read_config(path=CONFIG_PATH):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") not in {1, 2}:
        raise WatcherError("config_schema", "unsupported config schema")
    sources = data.get("watch_sources", [])
    if not isinstance(sources, list):
        raise WatcherError("config_sources", "watch_sources must be a list")
    data["watch_sources"] = [validate_source_config(source) for source in sources]
    return data


def validate_source_config(source):
    required = {"source_id", "root", "mount_root", "expected_fs", "expected_share", "expected_server"}
    if not isinstance(source, dict) or not required.issubset(source):
        raise WatcherError("source_config", "watch source configuration is incomplete")
    result = dict(source)
    source_id = result["source_id"]
    if not isinstance(source_id, str) or not SAFE_SOURCE_RE.fullmatch(source_id):
        raise WatcherError("source_id", "watch source id is invalid")
    for field in ("root", "mount_root", "expected_fs", "expected_share", "expected_server"):
        if not isinstance(result[field], str) or not result[field]:
            raise WatcherError("source_config", f"{field} is invalid")
    root = os.path.normpath(result["root"])
    mount_root = os.path.normpath(result["mount_root"])
    if not os.path.isabs(root) or not os.path.isabs(mount_root):
        raise WatcherError("source_config", "watch paths must be absolute")
    try:
        common = os.path.commonpath([root, mount_root])
    except ValueError:
        common = ""
    if common != mount_root or root == mount_root:
        raise WatcherError("source_config", "source root must be below mount root")
    excluded = result.get("excluded_relative_paths", [])
    if not isinstance(excluded, list) or any(not isinstance(value, str) for value in excluded):
        raise WatcherError("source_config", "excluded_relative_paths must be a list of relative paths")
    result["excluded_relative_paths"] = sorted({canonical_relative(value) for value in excluded})
    result["root"] = root
    result["mount_root"] = mount_root
    result["poll_seconds"] = positive_number(result.get("poll_seconds", 15), "poll_seconds")
    result["full_reconcile_seconds"] = positive_number(result.get("full_reconcile_seconds", 60), "full_reconcile_seconds")
    result["stable_samples"] = positive_int(result.get("stable_samples", 3), "stable_samples")
    result["stable_span_seconds"] = positive_number(result.get("stable_span_seconds", 60), "stable_span_seconds")
    result["scan_timeout_seconds"] = positive_number(result.get("scan_timeout_seconds", 45), "scan_timeout_seconds")
    result["delete_confirm_scans"] = positive_int(result.get("delete_confirm_scans", 2), "delete_confirm_scans")
    backoff = result.get("disconnect_backoff_seconds", [15, 30, 60])
    if not isinstance(backoff, list) or not backoff:
        raise WatcherError("source_config", "disconnect backoff must be a non-empty list")
    result["disconnect_backoff_seconds"] = [positive_number(value, "disconnect_backoff_seconds") for value in backoff]
    servers = result.get("allowed_servers", [result["expected_server"]])
    if not isinstance(servers, list) or not servers or any(not isinstance(v, str) or not v for v in servers):
        raise WatcherError("source_config", "allowed_servers must be a nonempty server list")
    if result["expected_server"] not in servers:
        raise WatcherError("source_config", "expected_server must be allowed")
    result["allowed_servers"] = servers
    result["capture_timeout_seconds"] = positive_number(
        result.get("capture_timeout_seconds", result["scan_timeout_seconds"]), "capture_timeout_seconds")
    return result


def sandbox_literal(value):
    """Quote one absolute path for a macOS sandbox profile."""
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise WatcherError("source_sandbox", "source sandbox path is invalid")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def source_readonly_profile(sources):
    roots = sorted({os.path.normpath(source["root"]) for source in sources})
    if not roots:
        raise WatcherError("source_sandbox", "source sandbox has no roots")
    rules = ["(version 1)", "(allow default)"]
    rules.extend(f"(deny file-write* (subpath {sandbox_literal(root)}))" for root in roots)
    return "\n".join(rules) + "\n"


def enter_source_readonly_sandbox(config):
    """Re-exec once with kernel-enforced write denial for every source root."""
    if os.environ.get(SANDBOX_FLAG) == "1":
        return
    if sys.platform != "darwin" or not SANDBOX_EXEC.is_file():
        raise WatcherError("source_sandbox", "macOS source write sandbox is unavailable")
    environment = dict(os.environ)
    environment[SANDBOX_FLAG] = "1"
    arguments = [
        str(SANDBOX_EXEC), "-p", source_readonly_profile(config["watch_sources"]),
        sys.executable, str(Path(__file__).resolve()),
    ]
    os.execve(str(SANDBOX_EXEC), arguments, environment)


def positive_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise WatcherError("source_config", f"{field} must be positive")
    return value


def positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WatcherError("source_config", f"{field} must be a positive integer")
    return value


def canonical_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or len(value) > 2048:
        raise WatcherError("source_path", "source relative path is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise WatcherError("source_path", "source relative path contains control characters")
    raw = PurePosixPath(value)
    if raw.as_posix() != value or raw.is_absolute() or any(part in ("", ".", "..") for part in raw.parts):
        raise WatcherError("source_path", "source relative path is not canonical")
    normalized = unicodedata.normalize("NFC", value)
    pure = PurePosixPath(normalized)
    return pure.as_posix()


def path_key(relative):
    return canonical_relative(relative).casefold()


def ignored_path(relative):
    parts = PurePosixPath(relative).parts
    for part in parts:
        lowered = part.casefold()
        suffix = PurePosixPath(part).suffix.casefold()
        if (
            lowered in TEMP_NAMES or lowered in CACHE_DIRECTORIES or part.startswith("~$")
            or suffix in TEMP_SUFFIXES or suffix == ".lnk"
        ):
            return True
    return False


def parse_expected_mount(mount_output, mount_root, expected_fs, expected_share, expected_server=None):
    normalized_root = os.path.normpath(mount_root)
    for line in mount_output.splitlines():
        match = re.match(r"^(.*?) on (.*?) \((.*?)\)\s*$", line)
        if not match:
            continue
        source, target, options = match.groups()
        fs_type = options.split(",", 1)[0].strip()
        if os.path.normpath(target) != normalized_root or fs_type != expected_fs:
            continue
        share = source.rstrip("/").rsplit("/", 1)[-1]
        authority = source[2:].split("/", 1)[0] if source.startswith("//") else ""
        server = authority.rsplit("@", 1)[-1]
        if share == expected_share and (expected_server is None or server.casefold() == expected_server.casefold()):
            return True
    return False


def mount_is_expected(source):
    maintenance = ROOT / "state" / "windesk_mount_maintenance.json"
    try:
        active = json.loads(maintenance.read_text())
        if active.get("expires_at", 0) > time.time():
            return False
    except (OSError, ValueError, TypeError):
        pass
    try:
        result = subprocess.run(
            ["/sbin/mount"], check=False, timeout=5,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    return any(parse_expected_mount(
        result.stdout, source["mount_root"], source["expected_fs"], source["expected_share"], server,
    ) for server in source.get("allowed_servers", [source["expected_server"]]))


def _scan_tree(root):
    root_path = Path(root)
    root_info = root_path.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise WatcherError("source_root", "source root is not a real directory")
    records = []

    def fail(error):
        raise error

    for directory, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False, onerror=fail):
        kept_dirs = []
        for name in sorted(dirnames):
            candidate = Path(directory) / name
            info = candidate.lstat()
            relative = canonical_relative(candidate.relative_to(root_path).as_posix())
            if ignored_path(relative) or stat.S_ISLNK(info.st_mode):
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            candidate = Path(directory) / name
            relative = canonical_relative(candidate.relative_to(root_path).as_posix())
            if ignored_path(relative):
                continue
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            records.append(
                {
                    "relative_path": relative,
                    "actual_relative_path": candidate.relative_to(root_path).as_posix(),
                    "path_key": path_key(relative),
                    "size_bytes": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
            )
    records.sort(key=lambda item: (item["path_key"], item["relative_path"]))
    seen = set()
    for record in records:
        if record["path_key"] in seen:
            raise WatcherError("source_path_collision", "source contains case-folding or Unicode path collisions")
        seen.add(record["path_key"])
    return records


def _scan_worker(root, sender):
    close_inherited_lock_fd()
    try:
        sender.send(("ok", _scan_tree(root)))
    except Exception as exc:
        code = exc.code if isinstance(exc, WatcherError) else "scan_error"
        sender.send(("error", code, type(exc).__name__))
    finally:
        sender.close()


def scan_with_timeout(root, timeout_seconds):
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_scan_worker, args=(str(root), sender), daemon=True)
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout_seconds):
            process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            raise WatcherError("scan_timeout", "source scan exceeded its wall-clock limit")
        result = receiver.recv()
    finally:
        receiver.close()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
    if result[0] != "ok":
        raise WatcherError(result[1], f"source scan failed: {result[2]}")
    return result[1]


def _capture_file(source_path, destination, expected_size, expected_mtime_ns, max_bytes):
    import video_hold
    video_hold.guard('win11',{'source_path_hash':hashlib.sha256(str(source_path).encode()).hexdigest()},name=Path(source_path).name,existing=True)
    before = Path(source_path).lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise WatcherError("source_changed", "source item is no longer a regular file")
    if before.st_size != expected_size or before.st_mtime_ns != expected_mtime_ns:
        raise WatcherError("source_changed", "source item changed before capture")
    digest = hashlib.sha256()
    size = 0
    with Path(source_path).open("rb") as source, Path(destination).open("xb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            if size > max_bytes:
                raise WatcherError("source_item_too_large", "source item exceeds the configured size limit")
            digest.update(chunk)
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    Path(destination).chmod(0o600)
    after_copy = Path(source_path).lstat()
    if stat.S_ISLNK(after_copy.st_mode) or not stat.S_ISREG(after_copy.st_mode):
        raise WatcherError("source_changed", "source item changed type during capture")
    if (after_copy.st_size, after_copy.st_mtime_ns) != (before.st_size, before.st_mtime_ns) or size != expected_size:
        raise WatcherError("source_changed", "source item changed during capture")
    copied_digest = hash_file(Path(destination))
    source_digest = hash_file(Path(source_path))
    after_verify = Path(source_path).lstat()
    if stat.S_ISLNK(after_verify.st_mode) or not stat.S_ISREG(after_verify.st_mode):
        raise WatcherError("source_changed", "source item changed type during verification")
    if (after_verify.st_size, after_verify.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise WatcherError("source_changed", "source item changed during verification")
    if not digest.hexdigest() == copied_digest == source_digest:
        raise WatcherError("source_changed", "source and local capture hashes differ")
    return {"sha256": copied_digest, "size_bytes": size}


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_worker(source_path, destination, expected_size, expected_mtime_ns, max_bytes, sender):
    close_inherited_lock_fd()
    try:
        sender.send(("ok", _capture_file(source_path, destination, expected_size, expected_mtime_ns, max_bytes)))
    except Exception as exc:
        code = exc.code if isinstance(exc, WatcherError) else "capture_error"
        sender.send(("error", code, type(exc).__name__))
    finally:
        sender.close()


def capture_with_timeout(source_path, destination, metadata, max_bytes, timeout_seconds):
    destination = Path(destination)
    if destination.exists():
        destination.unlink()
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_capture_worker,
        args=(
            str(source_path), str(destination), metadata["size_bytes"], metadata["mtime_ns"],
            max_bytes, sender,
        ),
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout_seconds):
            process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            raise WatcherError("capture_timeout", "source capture exceeded its wall-clock limit")
        result = receiver.recv()
    finally:
        receiver.close()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
    if result[0] != "ok":
        if destination.exists():
            destination.unlink()
        raise WatcherError(result[1], f"source capture failed: {result[2]}")
    return result[1]


def ensure_watch_inbox():
    WATCH_INBOX.mkdir(parents=True, exist_ok=True)
    if WATCH_INBOX.is_symlink():
        raise WatcherError("watch_inbox", "watch inbox must not be a symbolic link")
    WATCH_INBOX.chmod(0o700)
    info = WATCH_INBOX.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise WatcherError("watch_inbox", "watch inbox ownership or permissions are unsafe")


def pending_watch_bundle_count():
    """Serialize reconciliation batches until receiver has drained the inbox.

    A source can finish its next full scan while receiver is still committing
    the previous baseline.  Treating the partly committed database as the whole
    truth can otherwise manufacture a second set of added/baseline events for
    the same immutable bytes.  Any matching inbox entry is intentionally
    counted, including an unsafe one, so receiver gets the only chance to
    quarantine it before watcher proceeds.
    """
    ensure_watch_inbox()
    return sum(1 for _path in WATCH_INBOX.glob("business-watch-*.zip"))


def recover_local_spool():
    spool = STATE_ROOT / "spool"
    if spool.exists():
        shutil.rmtree(spool)
    spool.mkdir(parents=True, exist_ok=True)
    spool.chmod(0o700)
    for temporary in WATCH_INBOX.glob(".business-watch-*.tmp-*"):
        try:
            info = temporary.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid == os.getuid():
            temporary.unlink()


def state_path(source_id):
    return STATE_ROOT / f"{source_id}.json"


def default_state():
    return {
        "schema_version": 1,
        "was_online": False,
        "last_full_at": 0,
        "failure_count": 0,
        "candidates": {},
        "missing_counts": {},
        "replacement_counts": {},
        "pending_readds": {},
    }


def load_state(source_id):
    path = state_path(source_id)
    if not path.exists():
        return default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("unsupported watcher state")
        for field, kind in (
            ("candidates", dict), ("missing_counts", dict),
            ("replacement_counts", dict), ("pending_readds", dict),
        ):
            if not isinstance(data.get(field), kind):
                raise ValueError("invalid watcher state")
        return {**default_state(), **data}
    except Exception as exc:
        append_log("watcher_state_invalid", source_id=source_id, error=type(exc).__name__)
        return default_state()


def save_state(source_id, state):
    atomic_json(state_path(source_id), state)


def update_stability(records, state, observed_at, stable_samples, stable_span_seconds):
    candidates = state["candidates"]
    current_keys = {record["path_key"] for record in records}
    for key in list(candidates):
        if key not in current_keys:
            del candidates[key]
    stable = {}
    for record in records:
        signature = [record["size_bytes"], record["mtime_ns"]]
        candidate = candidates.get(record["path_key"])
        if candidate and candidate.get("signature") == signature and candidate.get("relative_path") == record["relative_path"]:
            candidate["samples"] = int(candidate.get("samples", 0)) + 1
            candidate["last_seen_at"] = observed_at
        else:
            candidate = {
                "relative_path": record["relative_path"], "signature": signature,
                "samples": 1, "first_seen_at": observed_at, "last_seen_at": observed_at,
            }
            candidates[record["path_key"]] = candidate
        span = observed_at - float(candidate["first_seen_at"])
        if candidate["samples"] >= stable_samples and span >= stable_span_seconds:
            stable[record["path_key"]] = record
    return stable


def read_database_state(source_id, database_path=DB_PATH):
    path = Path(database_path)
    if not path.exists():
        raise WatcherError("database_unavailable", "receiver database does not exist yet")
    uri = f"file:{quote(str(path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 3:
            raise WatcherError("database_schema", "receiver database schema is not ready for watcher events")
        source_row = conn.execute(
            "SELECT last_event_id FROM watch_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT e.source_entry_id,e.current_relative_path,e.path_key,e.current_sha256,e.source_mtime_ns,"
            "e.source_modified_at,e.size_bytes,e.version_number,e.storage_class,e.version_ref,e.mirror_relative,"
            "e.last_event_id,COALESCE(se.status,''),"
            "(SELECT COUNT(*) FROM source_events sx WHERE sx.source_id=e.source_id "
            "AND sx.source_entry_id=e.source_entry_id) FROM watch_entries e "
            "LEFT JOIN source_events se ON se.event_id=e.last_event_id WHERE e.source_id=? AND e.active=1",
            (source_id,),
        ).fetchall()
        lifecycle_rows = conn.execute(
            "SELECT source_relative_path FROM source_events WHERE source_id=? "
            "AND operation IN ('baseline','added')",
            (source_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise WatcherError("database_unavailable", f"receiver database cannot be read: {type(exc).__name__}")
    finally:
        if "conn" in locals():
            conn.close()
    entries = {}
    for row in rows:
        entries[row[2]] = {
            "source_entry_id": row[0], "current_relative_path": row[1], "path_key": row[2],
            "current_sha256": row[3], "source_mtime_ns": row[4], "source_modified_at": row[5],
            "size_bytes": row[6], "version_number": row[7], "storage_class": row[8],
            "version_ref": row[9], "mirror_relative": row[10], "last_event_id": row[11],
            "last_event_status": row[12],
            "transition_sequence": row[13],
        }
    # A watch_entries row follows a logical file across renames, so its current
    # path cannot serve as a lifecycle counter for a path it previously left.
    # baseline/added events are immutable lifecycle starts and preserve the
    # original path even after later renames.
    path_generations = {}
    for row in lifecycle_rows:
        key = path_key(row[0])
        path_generations[key] = path_generations.get(key, 0) + 1
    return {
        "source_known": source_row is not None,
        "last_event_id": source_row[0] if source_row else "initial",
        "entries": entries,
        "path_generations": path_generations,
    }


def deterministic_entry_id(source_id, relative_path, digest, mtime_ns, entry_generation):
    seed = json.dumps(
        [source_id, path_key(relative_path), digest, mtime_ns, int(entry_generation)],
        ensure_ascii=False, separators=(",", ":"),
    )
    return str(uuid.uuid5(WATCH_ENTRY_NAMESPACE, seed))


def event_identity(manifest):
    return {
        "source_id": manifest["source_id"],
        "source_entry_id": manifest["source_entry_id"],
        "operation": manifest["operation"],
        "source_relative_path": manifest["source_relative_path"],
        "previous_source_relative_path": manifest.get("previous_source_relative_path"),
        "previous_sha256": manifest.get("previous_sha256"),
        "current_sha256": manifest.get("current_sha256"),
        "source_mtime_ns": manifest["source_mtime_ns"],
        "source_modified_at": manifest["source_modified_at"],
        "source_size_bytes": manifest["source_size_bytes"],
        "capture_error": manifest.get("capture_error"),
        "previous_event_id": manifest.get("previous_event_id"),
        "transition_sequence": manifest["transition_sequence"],
        "items": manifest["items"],
    }


def deterministic_event_id(manifest):
    canonical = json.dumps(event_identity(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(WATCH_EVENT_NAMESPACE, canonical))


def build_manifest(
    source_id, source_entry_id, operation, current, previous=None,
    include_payload=False, capture_error=None,
):
    current_path = current["relative_path"]
    current_sha = current.get("sha256")
    previous_path = previous["current_relative_path"] if previous else None
    previous_sha = previous["current_sha256"] if previous else None
    previous_event_id = previous.get("last_event_id") if previous else None
    transition_sequence = int(previous.get("transition_sequence", 0)) + 1 if previous else 1
    if operation == "modified":
        previous_path = current_path
    if operation == "deleted":
        current_path = previous_path
        current_sha = None
    items = []
    if include_payload:
        items = [{"relative_path": "payload/content", "size_bytes": current["size_bytes"], "sha256": current_sha}]
    manifest = {
        "schema_version": 2,
        "event_id": "",
        "source_system": "windows_folder_watch",
        "source_id": source_id,
        "source_entry_id": source_entry_id,
        "operation": operation,
        "source_relative_path": current_path,
        "previous_source_relative_path": previous_path,
        "previous_sha256": previous_sha,
        "current_sha256": current_sha,
        "source_mtime_ns": current["mtime_ns"],
        "source_modified_at": mtime_iso(current["mtime_ns"]),
        "source_size_bytes": current["size_bytes"],
        "capture_error": capture_error,
        "previous_event_id": previous_event_id,
        "transition_sequence": transition_sequence,
        "items": items,
    }
    manifest["event_id"] = deterministic_event_id(manifest)
    return manifest


def deterministic_zip_info(name):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def read_bundle_manifest(path):
    try:
        with zipfile.ZipFile(path, "r") as archive:
            return json.loads(archive.read("manifest.json").decode("utf-8"))
    except Exception as exc:
        raise WatcherError("event_collision", f"existing local event bundle is invalid: {type(exc).__name__}")


def queue_event(manifest, payload_path=None):
    ensure_watch_inbox()
    final = WATCH_INBOX / f"business-watch-{manifest['event_id']}.zip"
    if final.exists():
        if final.is_symlink() or read_bundle_manifest(final) != manifest:
            raise WatcherError("event_collision", "existing local event id has different content")
        return final
    tmp = WATCH_INBOX / f".{final.name}.tmp-{uuid.uuid4().hex[:8]}"
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with tmp.open("xb") as raw:
            with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr(deterministic_zip_info("manifest.json"), manifest_bytes)
                if manifest["items"]:
                    if payload_path is None:
                        raise WatcherError("event_payload", "content event is missing its local payload")
                    with Path(payload_path).open("rb") as source, archive.open(deterministic_zip_info("payload/content"), "w") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
            raw.flush()
            os.fsync(raw.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, final)
        try:
            descriptor = os.open(WATCH_INBOX, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    finally:
        if tmp.exists():
            tmp.unlink()
    return final


def backoff_seconds(source, failure_count):
    values = source["disconnect_backoff_seconds"]
    return values[min(max(failure_count, 1) - 1, len(values) - 1)]


class FolderWatcher:
    def __init__(self, source, max_item_bytes, mount_checker=None, scanner=None, capturer=None, clock=None):
        self.source = validate_source_config(source)
        self.excluded_keys = {path_key(value) for value in self.source["excluded_relative_paths"]}
        self.max_item_bytes = int(max_item_bytes)
        self.mount_checker = mount_checker or mount_is_expected
        self.scanner = scanner or (lambda root: scan_with_timeout(root, self.source["scan_timeout_seconds"]))
        self.capturer = capturer or self._capture
        self.clock = clock or time.time
        self.state = load_state(self.source["source_id"])

    def _included_entries(self, database):
        # Exclusion changes collection scope; it is never evidence of deletion.
        return {key: entry for key, entry in database["entries"].items()
                if path_key(entry["current_relative_path"]) not in self.excluded_keys}

    def _capture(self, source_path, destination, metadata):
        return capture_with_timeout(
            source_path, destination, metadata, self.max_item_bytes, self.source["capture_timeout_seconds"]
        )

    def _mark_failure(self, code):
        self.state["was_online"] = False
        self.state["failure_count"] = int(self.state.get("failure_count", 0)) + 1
        # An offline or incomplete interval breaks both the file-stability
        # window and the consecutiveness required for deletion evidence.
        self.state["candidates"] = {}
        self.state["missing_counts"] = {}
        self.state["replacement_counts"] = {}
        self.state["pending_readds"] = {}
        save_state(self.source["source_id"], self.state)
        delay = backoff_seconds(self.source, self.state["failure_count"])
        note_health_failure(code)
        write_health("offline", code, source_id=self.source["source_id"], retry_seconds=delay)
        append_log("source_offline", source_id=self.source["source_id"], code=code, retry_seconds=delay)
        return delay

    def cycle(self, observed_at=None, force_full=False):
        observed_at = self.clock() if observed_at is None else observed_at
        if not self.mount_checker(self.source):
            return self._mark_failure("mount_not_expected")
        pending_bundles = pending_watch_bundle_count()
        if pending_bundles:
            write_health(
                "running", "receiver backlog", source_id=self.source["source_id"],
                pending_bundles=pending_bundles,
            )
            return self.source["poll_seconds"]
        reconnected_first = not bool(self.state.get("was_online"))
        full_scan = force_full or reconnected_first or observed_at - float(self.state.get("last_full_at", 0)) >= self.source["full_reconcile_seconds"]
        try:
            records = [record for record in self.scanner(self.source["root"])
                       if record["path_key"] not in self.excluded_keys]
            if not self.mount_checker(self.source):
                raise WatcherError("mount_changed", "mount identity changed during scan")
            database = read_database_state(self.source["source_id"])
            stable = update_stability(
                records, self.state, observed_at,
                self.source["stable_samples"], self.source["stable_span_seconds"],
            )
            queued = self._reconcile(records, stable, database, full_scan, not reconnected_first)
        except WatcherError as exc:
            return self._mark_failure(exc.code)
        except Exception as exc:
            append_log("watcher_cycle_error", source_id=self.source["source_id"], error=type(exc).__name__)
            return self._mark_failure("watcher_cycle_error")

        self.state["was_online"] = True
        self.state["failure_count"] = 0
        if full_scan:
            self.state["last_full_at"] = observed_at
        save_state(self.source["source_id"], self.state)
        note_health_success(full_scan)
        write_health(
            "running", "scan complete", source_id=self.source["source_id"],
            full_scan=full_scan, queued_events=queued,
        )
        if queued:
            append_log("watch_events_queued", source_id=self.source["source_id"], count=queued)
        return self.source["poll_seconds"]

    def _capture_record(self, record, spool_root):
        destination = spool_root / (hashlib.sha256(record["path_key"].encode("utf-8")).hexdigest() + ".payload")
        source_path = Path(self.source["root"]) / PurePosixPath(
            record.get("actual_relative_path", record["relative_path"])
        )
        if record["size_bytes"] > self.max_item_bytes:
            enriched = dict(record)
            enriched.update({"sha256": None, "payload_path": None, "capture_error": "source_item_too_large"})
            return enriched
        try:
            result = self.capturer(source_path, destination, record)
        except WatcherError as exc:
            if exc.code == "source_changed":
                return None
            enriched = dict(record)
            enriched.update({"sha256": None, "payload_path": None, "capture_error": exc.code})
            return enriched
        if result["size_bytes"] != record["size_bytes"] or not SHA_RE.fullmatch(result["sha256"]):
            raise WatcherError("capture_result", "source capture returned invalid metadata")
        enriched = dict(record)
        enriched["sha256"] = result["sha256"]
        enriched["payload_path"] = destination
        enriched["capture_error"] = None
        return enriched

    def _reconcile(self, records, stable, database, full_scan, allow_delete_confirmation):
        source_id = self.source["source_id"]
        active = self._included_entries(database)
        excluded_ids = {entry["source_entry_id"] for key, entry in database["entries"].items()
                        if key not in active}
        for field in ("missing_counts", "replacement_counts"):
            for entry_id in excluded_ids:
                self.state[field].pop(entry_id, None)
        for field in ("candidates", "pending_readds"):
            for key in self.excluded_keys:
                self.state[field].pop(key, None)
        records = [record for record in records if record["path_key"] not in self.excluded_keys]
        stable = {key: record for key, record in stable.items() if key not in self.excluded_keys}
        all_current_keys = {record["path_key"] for record in records}
        spool_root = STATE_ROOT / "spool" / source_id / uuid.uuid4().hex
        spool_root.mkdir(parents=True, exist_ok=False)
        spool_root.chmod(0o700)
        enriched = {}
        try:
            import video_hold
            for key, record in stable.items():
                hold_kind=video_hold.classify(record['relative_path'])
                if hold_kind:
                    video_hold.register('win11',{'source_id':source_id,'relative_path':record['relative_path']},
                        name=record['relative_path'],size=record['size_bytes'],
                        version=video_hold.digest([record['size_bytes'],record['mtime_ns']]),existing=True,kind=hold_kind)
                    continue
                existing = active.get(key)
                if (
                    not full_scan
                    and existing and existing["current_relative_path"] == record["relative_path"]
                    and existing["size_bytes"] == record["size_bytes"]
                    and existing["source_mtime_ns"] == record["mtime_ns"]
                    and existing["current_sha256"] is not None
                    and not str(existing.get("last_event_status", "")).startswith("capture_error:")
                ):
                    value = dict(record)
                    value["sha256"] = existing["current_sha256"]
                    value["payload_path"] = None
                    value["capture_error"] = None
                    enriched[key] = value
                else:
                    # A complete reconciliation is also a content audit.  Size
                    # and mtime are only a poll optimization; SMB clients can
                    # replace bytes while preserving both, so every stable file
                    # is captured and strongly re-hashed at least once per full
                    # scan interval.
                    captured = self._capture_record(record, spool_root)
                    if captured is not None:
                        enriched[key] = captured
            if not self.mount_checker(self.source):
                raise WatcherError("mount_changed", "mount identity changed during source capture")
            queued = self._plan_and_queue(
                all_current_keys, enriched, database, full_scan, allow_delete_confirmation
            )
            return queued
        finally:
            shutil.rmtree(spool_root, ignore_errors=True)

    def _plan_and_queue(self, all_current_keys, current, database, full_scan, allow_delete_confirmation):
        source_id = self.source["source_id"]
        active = self._included_entries(database)
        all_current_keys = all_current_keys - self.excluded_keys
        current = {key: record for key, record in current.items() if key not in self.excluded_keys}
        missing_counts = self.state["missing_counts"]
        replacement_counts = self.state["replacement_counts"]
        pending_readds = self.state["pending_readds"]
        queued = 0
        handled_current = set()
        handled_missing = set()
        forced_missing = set()

        # Presence in any complete online enumeration disproves absence even if
        # the file has not yet accumulated enough stable samples to be hashed.
        if full_scan:
            for key, old in active.items():
                if key in all_current_keys:
                    missing_counts.pop(old["source_entry_id"], None)

        def enqueue(manifest, payload=None):
            nonlocal queued
            queue_event(manifest, payload)
            queued += 1

        # Same normalized path: metadata-only changes are ignored after hashing;
        # textual case/Unicode rename plus content change is conservatively
        # treated as delete then add, never as a modification.
        for key in sorted(set(active) & set(current)):
            old = active[key]
            new = current[key]
            if old["current_relative_path"] != new["relative_path"]:
                if old["current_sha256"] is not None and old["current_sha256"] == new["sha256"]:
                    manifest = build_manifest(source_id, old["source_entry_id"], "renamed", new, old, False)
                    enqueue(manifest)
                    handled_current.add(key)
                    handled_missing.add(key)
                    missing_counts.pop(old["source_entry_id"], None)
                    replacement_counts.pop(old["source_entry_id"], None)
                else:
                    forced_missing.add(key)
                    pending_readds[key] = {
                        "relative_path": new["relative_path"], "sha256": new["sha256"],
                        "mtime_ns": new["mtime_ns"], "size_bytes": new["size_bytes"],
                    }
                continue
            handled_current.add(key)
            missing_counts.pop(old["source_entry_id"], None)
            replacement_counts.pop(old["source_entry_id"], None)
            if new.get("capture_error"):
                same_recorded_error = (
                    str(old.get("last_event_status", "")) == f"capture_error:{new['capture_error']}"
                    and old["source_mtime_ns"] == new["mtime_ns"]
                    and old["size_bytes"] == new["size_bytes"]
                )
                if not same_recorded_error:
                    manifest = build_manifest(
                        source_id, old["source_entry_id"], "modified", new, old, False,
                        new["capture_error"],
                    )
                    enqueue(manifest)
            elif old["current_sha256"] != new["sha256"]:
                manifest = build_manifest(source_id, old["source_entry_id"], "modified", new, old, True)
                enqueue(manifest, new["payload_path"])
            elif str(old.get("last_event_status", "")).startswith("capture_error:"):
                # The bytes are readable again.  A same-hash recovery carries a
                # payload so receiver can re-validate/classify and replace the
                # fixed error note without manufacturing another version.
                manifest = build_manifest(source_id, old["source_entry_id"], "modified", new, old, True)
                enqueue(manifest, new["payload_path"])
            elif (
                old["source_mtime_ns"] != new["mtime_ns"]
                or old["size_bytes"] != new["size_bytes"]
                or old["source_modified_at"] != mtime_iso(new["mtime_ns"])
            ):
                manifest = build_manifest(source_id, old["source_entry_id"], "modified", new, old, False)
                enqueue(manifest)

        new_keys = set(current) - set(active)
        missing_keys = set(active) - all_current_keys

        # A rename is accepted only for a one-to-one missing/new hash match.
        missing_by_hash = {}
        for key in missing_keys:
            if active[key]["current_sha256"] is not None:
                missing_by_hash.setdefault(active[key]["current_sha256"], []).append(key)
        new_by_hash = {}
        for key in new_keys:
            if current[key]["sha256"] is not None:
                new_by_hash.setdefault(current[key]["sha256"], []).append(key)
        for digest in sorted(set(missing_by_hash) & set(new_by_hash)):
            old_keys = missing_by_hash[digest]
            candidate_keys = new_by_hash[digest]
            if len(old_keys) != 1 or len(candidate_keys) != 1:
                continue
            old_key, new_key = old_keys[0], candidate_keys[0]
            old, new = active[old_key], current[new_key]
            manifest = build_manifest(source_id, old["source_entry_id"], "renamed", new, old, False)
            enqueue(manifest)
            handled_missing.add(old_key)
            handled_current.add(new_key)
            missing_counts.pop(old["source_entry_id"], None)

        for key in sorted(new_keys - handled_current):
            new = current[key]
            operation = "baseline" if not database["source_known"] else "added"
            entry_generation = int(database.get("path_generations", {}).get(key, 0)) + 1
            entry_id = deterministic_entry_id(
                source_id, new["relative_path"], new["sha256"], new["mtime_ns"], entry_generation
            )
            if new.get("capture_error"):
                manifest = build_manifest(
                    source_id, entry_id, operation, new, None, False, new["capture_error"]
                )
                enqueue(manifest)
            else:
                manifest = build_manifest(source_id, entry_id, operation, new, None, True)
                enqueue(manifest, new["payload_path"])
            pending_readds.pop(key, None)

        for key in sorted(missing_keys - handled_missing):
            old = active[key]
            entry_id = old["source_entry_id"]
            if not full_scan or not allow_delete_confirmation:
                continue
            missing_counts[entry_id] = int(missing_counts.get(entry_id, 0)) + 1
            if missing_counts[entry_id] < self.source["delete_confirm_scans"]:
                continue
            previous = dict(old)
            current_marker = {
                "relative_path": old["current_relative_path"],
                "mtime_ns": int(old["source_mtime_ns"] or 0),
                "size_bytes": int(old["size_bytes"] or 0),
            }
            manifest = build_manifest(source_id, entry_id, "deleted", current_marker, previous, False)
            enqueue(manifest)

        forced_entry_ids = {active[key]["source_entry_id"] for key in forced_missing}
        if full_scan:
            for entry_id in list(replacement_counts):
                if entry_id not in forced_entry_ids:
                    replacement_counts.pop(entry_id, None)
        for key in sorted(forced_missing):
            old = active[key]
            entry_id = old["source_entry_id"]
            if not full_scan or not allow_delete_confirmation:
                continue
            replacement_counts[entry_id] = int(replacement_counts.get(entry_id, 0)) + 1
            if replacement_counts[entry_id] < self.source["delete_confirm_scans"]:
                continue
            current_marker = {
                "relative_path": old["current_relative_path"],
                "mtime_ns": int(old["source_mtime_ns"] or 0),
                "size_bytes": int(old["size_bytes"] or 0),
            }
            manifest = build_manifest(source_id, entry_id, "deleted", current_marker, dict(old), False)
            enqueue(manifest)
        for key in list(pending_readds):
            if key not in all_current_keys:
                pending_readds.pop(key, None)
        return queued


def acquire_lock():
    import fcntl

    global LOCK_FD
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    LOCK_PATH.chmod(0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise WatcherError("already_running", "folder watcher is already running")
    LOCK_FD = handle.fileno()
    return handle


def close_inherited_lock_fd():
    global LOCK_FD
    if LOCK_FD is None:
        return
    try:
        os.close(LOCK_FD)
    except OSError:
        pass
    LOCK_FD = None


def release_lock(handle):
    global LOCK_FD
    try:
        descriptor = handle.fileno()
    except (AttributeError, ValueError):
        descriptor = None
    handle.close()
    if LOCK_FD == descriptor:
        LOCK_FD = None


def initialize_runtime():
    ensure_watch_inbox()
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_ROOT.chmod(0o700)
    # Lock before deleting stale local spool files so a second instance can
    # never clean the active instance's capture workspace.
    lock_handle = acquire_lock()
    try:
        recover_local_spool()
    except Exception:
        release_lock(lock_handle)
        raise
    return lock_handle


def consume_scan_trigger():
    """Consume a pending one-shot scan request left by an external tool.

    The trigger is a plain file inside the owner-only state directory, so a
    caller only needs ``touch`` and never the watcher PID.  Unlinking is the
    consume step and does not follow symlinks, so a stale or hostile link
    cannot redirect the watcher anywhere.
    """
    try:
        TRIGGER_PATH.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        append_log("scan_trigger_unreadable")
        return False
    append_log("scan_trigger_consumed")
    return True


def signal_handler(_signum, _frame):
    global RUNNING
    RUNNING = False


def main():
    global RUNNING
    process_started_at = now_iso()
    config = read_config()
    if not config["watch_sources"]:
        raise WatcherError("no_sources", "no folder watch sources are configured")
    enter_source_readonly_sandbox(config)
    lock_handle = initialize_runtime()
    # Only the process that owns the singleton lock is a real watcher start.
    # A duplicate launch must not overwrite health timestamps or inflate
    # restart/fatal counters before it is rejected by the lock.
    initialize_health_state(process_started_at)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    watchers = [FolderWatcher(source, config["max_item_bytes"]) for source in config["watch_sources"]]
    write_health("running", "folder watcher started", sources=[watcher.source["source_id"] for watcher in watchers])
    append_log("folder_watcher_started", sources=[watcher.source["source_id"] for watcher in watchers])
    try:
        force_full = consume_scan_trigger()
        while RUNNING and not __import__("owner_retirement").stopped():
            delays = []
            for watcher in watchers:
                if not RUNNING:
                    break
                delays.append(watcher.cycle(force_full=force_full))
            force_full = False
            deadline = time.monotonic() + (min(delays) if delays else 15)
            while RUNNING and time.monotonic() < deadline:
                if consume_scan_trigger():
                    force_full = True
                    break
                sleep_seconds = bounded_sleep_seconds(deadline)
                if sleep_seconds <= 0:
                    break
                time.sleep(sleep_seconds)
    finally:
        release_lock(lock_handle)
        write_health("stopped", "folder watcher stopped")
        append_log("folder_watcher_stopped")


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    try:
        main()
    except Exception as exc:
        code = exc.code if isinstance(exc, WatcherError) else "watcher_fatal"
        if code == "already_running":
            append_log("folder_watcher_duplicate_blocked")
            raise SystemExit(0)
        if HEALTH_STATE.get("process_started_at") is None:
            initialize_health_state()
        HEALTH_STATE["fatal_errors_total"] = int(HEALTH_STATE.get("fatal_errors_total", 0)) + 1
        note_health_failure(code)
        append_log("folder_watcher_fatal", code=code, error=type(exc).__name__)
        write_health("failed", code)
        raise
