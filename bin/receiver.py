#!/usr/bin/env python3

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import codecs
import hashlib
import html
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import time
import unicodedata
import uuid
import zipfile
import content_observer
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

os.umask(0o077)

ROOT = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync'))
CONFIG_PATH = ROOT / "config.json"
INBOX = ROOT / "inbox"
WATCH_INBOX = ROOT / "watch_inbox"
PROCESSING = ROOT / "processing"
QUARANTINE = ROOT / "quarantine"
ACK_OUTBOX = ROOT / "ack_outbox"
ACK_SENT = ROOT / "state" / "ack_sent"
PROCESSED_BUNDLES = ROOT / "state" / "processed_bundles"
UNRELATED_DOWNLOADS = Path.home() / "Downloads" / "Tailscale Received"
LOG_PATH = ROOT / "logs" / "receiver.jsonl"
HEALTH_PATH = ROOT / "state" / "health.json"
DB_PATH = ROOT / "state" / "bridge.sqlite"
RECEIVER_LOCK_PATH = ROOT / "state" / "receiver.lock"
MEMORY_WRITE_LOCK_PATH = ROOT / "state" / "memory-index.lock"
REFRESH_INDEXES_SCRIPT = ROOT / "bin" / "refresh_memory_indexes.rb"
INDEX_REFRESH_TIMEOUT_SECONDS = 10 * 60
INDEX_REFRESH_RETRY_COOLDOWN_SECONDS = 60

OBSIDIAN_TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".tsv"}
HTML_EXTENSIONS = {".html", ".htm"}
OOXML_EXTENSIONS = {".docx": "word", ".xlsx": "xl", ".pptx": "ppt"}
RASTER_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif",
}
EXTERNAL_TEXT_EXTENSIONS = {
    ".bat", ".bash", ".c", ".cc", ".cfg", ".cjs", ".cmd", ".command", ".conf",
    ".cpp", ".cs", ".css", ".csvs", ".cxx", ".fish", ".go", ".gradle",
    ".h", ".hh", ".hpp", ".hta", ".ini", ".ipynb", ".java", ".js",
    ".jse", ".json", ".jsonl", ".jsx", ".kt", ".kts", ".less", ".lock",
    ".lua", ".map", ".mjs", ".php", ".pl", ".properties", ".ps1", ".psm1",
    ".py", ".pyw", ".r", ".rb", ".reg", ".rs", ".sass", ".scss", ".sh",
    ".sln", ".sql", ".svg", ".swift", ".toml", ".ts", ".tsx", ".vbe",
    ".url", ".vbs", ".vue", ".was", ".wsf", ".wsh", ".xml", ".yaml", ".yml",
    ".zsh",
}
EXTERNAL_ZIP_EXTENSIONS = {
    ".docm", ".dotm", ".jar", ".nupkg", ".potm", ".ppam", ".pptm", ".sldm",
    ".war", ".whl", ".xlam", ".xlsm", ".xltm", ".zip",
}
EXTERNAL_ARCHIVE_EXTENSIONS = {
    ".7z", ".bz2", ".gz", ".rar", ".tar", ".tbz", ".tbz2", ".tgz",
    ".txz", ".xz",
}
EXTERNAL_BINARY_EXTENSIONS = {
    ".a", ".class", ".com", ".dll", ".dmg", ".dylib", ".exe", ".lib",
    ".lnk", ".msi", ".msp", ".o", ".pkg", ".scr", ".so", ".wasm",
}
DATABASE_EXTENSIONS = {".accdb", ".db", ".duckdb", ".mdb", ".parquet", ".sqlite", ".sqlite3"}
LEGACY_OFFICE_EXTENSIONS = {".doc", ".xls", ".ppt"}
MEDIA_EXTENSIONS = {".avi", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".wav"}
MAIL_EXPORT_EXTENSIONS = {".eml", ".mbox", ".ost", ".pst"}
DESIGN_SOURCE_EXTENSIONS = {
    ".ai", ".dwg", ".dxf", ".fig", ".iges", ".igs", ".indd", ".psd",
    ".sketch", ".step", ".stp",
}
DISK_IMAGE_EXTENSIONS = {".iso"}
KNOWN_EXTERNAL_EXTENSIONS = (
    EXTERNAL_TEXT_EXTENSIONS | EXTERNAL_ZIP_EXTENSIONS | EXTERNAL_ARCHIVE_EXTENSIONS |
    EXTERNAL_BINARY_EXTENSIONS | DATABASE_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS |
    MEDIA_EXTENSIONS | MAIL_EXPORT_EXTENSIONS | DESIGN_SOURCE_EXTENSIONS | DISK_IMAGE_EXTENSIONS
)
STORAGE_CLASSES = {"obsidian", "external", "quarantine"}
EVENT_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
BUNDLE_RE = re.compile(r"^business-memory-([0-9a-fA-F-]{36})\.zip$", re.IGNORECASE)
WATCH_BUNDLE_RE = re.compile(r"^business-watch-([0-9a-fA-F-]{36})\.zip$", re.IGNORECASE)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_NODE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SAFE_SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_MANIFEST_BYTES = 1024 * 1024
WATCH_EVENT_NAMESPACE = uuid.UUID('ea6e317b-1a39-5ddc-bb6c-8282a7f53461')
WATCH_SOURCE_NAMESPACE = uuid.UUID('a3d3714a-c70f-5443-9386-b42b81df6555')
WATCH_OPERATIONS = {"baseline", "added", "modified", "deleted", "renamed"}
RUNNING = True


class BundleError(Exception):
    def __init__(self, code, message, manifest=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.manifest = manifest


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def watch_mtime_iso(mtime_ns):
    seconds, nanoseconds = divmod(int(mtime_ns), 1_000_000_000)
    stamp = datetime.fromtimestamp(seconds, timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanoseconds // 1_000:06d}Z"


def ensure_dirs():
    for path in [
        INBOX, WATCH_INBOX, PROCESSING, QUARANTINE, NON_OBSIDIAN_ROOT, ITEM_QUARANTINE_ROOT, EXTERNAL_ROOT,
        WATCH_VERSIONS_ROOT, WATCH_NOTES_ROOT, WATCH_ITEM_QUARANTINE_ROOT, ROUTING_EXCEPTION_ROOT,
        ACK_OUTBOX, ACK_SENT,
        PROCESSED_BUNDLES, LOG_PATH.parent, DB_PATH.parent
    ]:
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def read_config():
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") not in {1, 2}:
        raise RuntimeError("unsupported config schema")
    if data.get("schema_version") == 2 and not isinstance(data.get("watch_sources", []), list):
        raise RuntimeError("watch_sources must be a list")
    for source in data.get("watch_sources", []):
        if not isinstance(source, dict):
            raise RuntimeError("watch source must be an object")
        if source.get("mirror_policy", "classified") not in {"classified", "none"}:
            raise RuntimeError("watch source mirror_policy must be classified or none")
    return data


CONFIG = read_config()
TAILSCALE = CONFIG["tailscale_bin"]
MEMORY_ROOT = Path(CONFIG["memory_root"])
NON_OBSIDIAN_ROOT = Path(CONFIG["external_root"])
EXTERNAL_ROOT = NON_OBSIDIAN_ROOT / "external_payloads"
ITEM_QUARANTINE_ROOT = NON_OBSIDIAN_ROOT / "quarantine" / "items"
ROUTING_EXCEPTION_ROOT = NON_OBSIDIAN_ROOT / "business-routing" / "quarantine" / "quick-capture"
WATCH_VERSIONS_ROOT = NON_OBSIDIAN_ROOT / "win11-folder-watch" / "versions"
WATCH_NOTES_ROOT = NON_OBSIDIAN_ROOT / "win11-folder-watch" / "obsidian-notes-archive"
WATCH_ITEM_QUARANTINE_ROOT = NON_OBSIDIAN_ROOT / "quarantine" / "win11-folder-watch"
ALLOWED_RETURN_NODES = set(CONFIG["allowed_return_nodes"])
ALLOWED_WATCH_SOURCES = {
    str(source.get("source_id"))
    for source in CONFIG.get("watch_sources", [])
    if isinstance(source, dict) and SAFE_SOURCE_RE.fullmatch(str(source.get("source_id", "")))
}
WATCH_SOURCE_SETTINGS = {
    str(source.get("source_id")): source
    for source in CONFIG.get("watch_sources", [])
    if isinstance(source, dict) and SAFE_SOURCE_RE.fullmatch(str(source.get("source_id", "")))
}
MAX_ITEM_BYTES = int(CONFIG["max_item_bytes"])
MAX_ITEMS = int(CONFIG["max_items"])
MAX_TOTAL_BYTES = int(CONFIG["max_total_bytes"])


def watch_mirror_enabled(source_id):
    source = WATCH_SOURCE_SETTINGS.get(str(source_id), {})
    return source.get("mirror_policy", "classified") == "classified"


def append_log(event, **fields):
    record = {"at": now_iso(), "event": event, **fields}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_health(status, detail, **extra):
    payload = {
        "schema_version": 1,
        "status": status,
        "detail": detail,
        "updated_at": now_iso(),
        **extra,
    }
    tmp = HEALTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, HEALTH_PATH)


def notify_failure(message):
    safe = message.replace("\\", " ").replace('"', "'")[:180]
    script = f'display notification "{safe}" with title "示例企业记忆传输异常"'
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def acquire_receiver_lock():
    import fcntl

    RECEIVER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = RECEIVER_LOCK_PATH.open("a+")
    RECEIVER_LOCK_PATH.chmod(0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("receiver is already running")
    return handle


def acquire_memory_write_lock():
    """Serialize every file/SQLite mutation with index and semantic writers."""
    import fcntl

    MEMORY_WRITE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = MEMORY_WRITE_LOCK_PATH.open("a+")
    MEMORY_WRITE_LOCK_PATH.chmod(0o600)
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def table_columns(conn, table):
    return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def create_files_table(conn, table="files"):
    conn.execute(
        f"""
        CREATE TABLE {table} (
          sha256 TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          destination_relative TEXT NOT NULL,
          first_event_id TEXT NOT NULL,
          stored_at TEXT NOT NULL,
          storage_class TEXT NOT NULL CHECK(storage_class IN ('obsidian','external','quarantine')),
          destination_ref TEXT NOT NULL,
          PRIMARY KEY (sha256, storage_class)
        )
        """
    )


def migrate_files_schema(conn):
    columns = table_columns(conn, "files")
    if not columns:
        create_files_table(conn)
        return
    primary_key = [
        row[1] for row in sorted(columns.values(), key=lambda value: value[5]) if row[5]
    ]
    if {"storage_class", "destination_ref"}.issubset(columns) and primary_key == ["sha256", "storage_class"]:
        return

    conn.execute("DROP TABLE IF EXISTS files_v2")
    create_files_table(conn, "files_v2")
    storage_expr = "COALESCE(storage_class, 'obsidian')" if "storage_class" in columns else "'obsidian'"
    if "destination_ref" in columns:
        reference_expr = "COALESCE(destination_ref, 'obsidian:' || destination_relative)"
    else:
        reference_expr = "'obsidian:' || destination_relative"
    conn.execute(
        "INSERT INTO files_v2(sha256,size_bytes,destination_relative,first_event_id,stored_at,storage_class,destination_ref) "
        f"SELECT sha256,size_bytes,destination_relative,first_event_id,stored_at,{storage_expr},{reference_expr} FROM files"
    )
    conn.execute("DROP TABLE files")
    conn.execute("ALTER TABLE files_v2 RENAME TO files")


def add_column_if_missing(conn, table, column, declaration):
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
          event_id TEXT PRIMARY KEY,
          bundle_sha256 TEXT NOT NULL,
          source_device TEXT NOT NULL,
          created_at TEXT NOT NULL,
          processed_at TEXT NOT NULL,
          status TEXT NOT NULL,
          destination_relative TEXT,
          obsidian_count INTEGER NOT NULL DEFAULT 0,
          external_count INTEGER NOT NULL DEFAULT 0,
          quarantine_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ack_attempts (
          ack_file TEXT PRIMARY KEY,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_attempt_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_sources (
          source_id TEXT PRIMARY KEY,
          source_uuid TEXT NOT NULL UNIQUE,
          source_system TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,
          last_event_at TEXT NOT NULL,
          last_event_id TEXT NOT NULL,
          active_entries INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_entries (
          source_id TEXT NOT NULL,
          source_entry_id TEXT NOT NULL,
          current_relative_path TEXT NOT NULL,
          path_key TEXT NOT NULL,
          current_sha256 TEXT,
          source_mtime_ns INTEGER,
          source_modified_at TEXT,
          size_bytes INTEGER,
          version_number INTEGER NOT NULL DEFAULT 0,
          storage_class TEXT,
          version_ref TEXT,
          mirror_relative TEXT,
          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
          deleted_at TEXT,
          last_event_id TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (source_id, source_entry_id),
          FOREIGN KEY (source_id) REFERENCES watch_sources(source_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS watch_entries_path_state
        ON watch_entries(source_id, path_key, active)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_versions (
          source_id TEXT NOT NULL,
          source_entry_id TEXT NOT NULL,
          version_number INTEGER NOT NULL,
          event_id TEXT NOT NULL UNIQUE,
          sha256 TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          source_relative_path TEXT NOT NULL,
          source_modified_at TEXT,
          storage_class TEXT NOT NULL CHECK(storage_class IN ('obsidian','external','quarantine')),
          destination_ref TEXT NOT NULL,
          stored_at TEXT NOT NULL,
          PRIMARY KEY (source_id, source_entry_id, version_number),
          FOREIGN KEY (source_id, source_entry_id) REFERENCES watch_entries(source_id, source_entry_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_events (
          event_id TEXT PRIMARY KEY,
          bundle_sha256 TEXT NOT NULL,
          source_system TEXT NOT NULL,
          source_id TEXT NOT NULL,
          source_entry_id TEXT NOT NULL,
          operation TEXT NOT NULL CHECK(operation IN ('baseline','added','modified','deleted','renamed')),
          source_relative_path TEXT NOT NULL,
          previous_source_relative_path TEXT,
          previous_sha256 TEXT,
          current_sha256 TEXT,
          source_modified_at TEXT,
          items_json TEXT NOT NULL,
          previous_event_id TEXT,
          transition_sequence INTEGER NOT NULL DEFAULT 1,
          processed_at TEXT NOT NULL,
          status TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    with conn:
        add_column_if_missing(conn, "events", "obsidian_count", "INTEGER NOT NULL DEFAULT 0")
        add_column_if_missing(conn, "events", "external_count", "INTEGER NOT NULL DEFAULT 0")
        add_column_if_missing(conn, "events", "quarantine_count", "INTEGER NOT NULL DEFAULT 0")
        add_column_if_missing(conn, "source_events", "previous_event_id", "TEXT")
        add_column_if_missing(conn, "source_events", "transition_sequence", "INTEGER NOT NULL DEFAULT 1")
        migrate_files_schema(conn)
        conn.execute("PRAGMA user_version = 3")
    conn.commit()
    return conn


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_archive_name(name):
    if "\\" in name or any(ord(character) < 32 or ord(character) == 127 for character in name):
        return False
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        return False
    return len(name) <= 512


def is_symlink(info):
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def validate_manifest(manifest):
    required = {"schema_version", "event_id", "created_at", "source_device", "return_node", "capture_type", "items"}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise BundleError("manifest_missing_fields", "manifest fields are incomplete")
    if manifest["schema_version"] != 1:
        raise BundleError("manifest_schema", "unsupported manifest schema")
    event_id = str(manifest["event_id"])
    if not EVENT_RE.fullmatch(event_id):
        raise BundleError("event_id", "invalid event id")
    try:
        uuid.UUID(event_id)
    except ValueError:
        raise BundleError("event_id", "invalid event uuid")
    node = str(manifest["return_node"])
    if node not in ALLOWED_RETURN_NODES or not SAFE_NODE_RE.fullmatch(node):
        raise BundleError("return_node", "return node is not allowed")
    source_device = str(manifest["source_device"])
    if source_device not in ALLOWED_RETURN_NODES:
        raise BundleError("source_device", "source device is not allowed")
    if manifest["capture_type"] not in {"text", "files"}:
        raise BundleError("capture_type", "unsupported capture type")
    items = manifest["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise BundleError("item_count", "invalid item count")
    return manifest


def validate_bundle_preflight(bundle_path):
    bundle_match = BUNDLE_RE.fullmatch(bundle_path.name)
    if not bundle_match:
        raise BundleError("bundle_name", "unexpected bundle name")
    if bundle_path.stat().st_size > MAX_TOTAL_BYTES:
        raise BundleError("archive_too_large", "archive exceeds total size limit")
    return bundle_match


def inspect_bundle(bundle_path):
    bundle_match = validate_bundle_preflight(bundle_path)

    try:
        archive = zipfile.ZipFile(bundle_path, "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise BundleError("archive_invalid", "transport archive is invalid")
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > MAX_ITEMS + 1:
            raise BundleError("archive_item_count", "archive contains too many files")
        total = 0
        names = set()
        encrypted_members = []
        for info in infos:
            if not safe_archive_name(info.filename):
                raise BundleError("unsafe_path", "archive contains an unsafe path")
            if is_symlink(info):
                raise BundleError("symlink", "archive contains a symbolic link")
            if info.flag_bits & 0x1:
                encrypted_members.append(info.filename)
            if info.filename == "manifest.json" and info.file_size > MAX_MANIFEST_BYTES:
                raise BundleError("manifest_too_large", "manifest exceeds 1 MB limit")
            if info.file_size > MAX_ITEM_BYTES and info.filename != "manifest.json":
                raise BundleError("item_too_large", "an item exceeds 250 MB")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise BundleError("total_too_large", "uncompressed content exceeds total limit")
            if info.filename in names:
                raise BundleError("duplicate_archive_path", "archive contains duplicate paths")
            names.add(info.filename)

        if "manifest.json" not in names:
            raise BundleError("manifest_missing", "manifest.json is missing")
        if "manifest.json" in encrypted_members:
            raise BundleError("transport_encrypted", "transport manifest is encrypted")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            raise BundleError("manifest_invalid", f"manifest cannot be read: {type(exc).__name__}")
        if isinstance(manifest, dict) and manifest.get("schema_version") == 2:
            # Schema v2 carries authoritative folder delete/rename semantics and
            # is trusted only when it arrives through the owner-only local inbox.
            raise BundleError("taildrop_v2_forbidden", "schema v2 is not accepted from Taildrop")
        manifest = validate_manifest(manifest)
        if manifest["event_id"].lower() != bundle_match.group(1).lower():
            raise BundleError("bundle_event_mismatch", "bundle name and manifest event id do not match", manifest)
        if encrypted_members:
            raise BundleError("transport_encrypted", "transport archive contains encrypted members", manifest)

        declared = set()
        for item in manifest["items"]:
            if not isinstance(item, dict):
                raise BundleError("item_invalid", "manifest item is invalid", manifest)
            rel = str(item.get("relative_path", ""))
            size = item.get("size_bytes")
            digest = str(item.get("sha256", "")).lower()
            if not safe_archive_name(rel) or not rel.startswith("payload/"):
                raise BundleError("payload_path", "payload path is invalid", manifest)
            if rel in declared:
                raise BundleError("payload_duplicate", "manifest contains duplicate payload paths", manifest)
            if not isinstance(size, int) or size < 0 or size > MAX_ITEM_BYTES:
                raise BundleError("payload_size", "payload size is invalid", manifest)
            if not SHA_RE.fullmatch(digest):
                raise BundleError("payload_hash", "payload hash is invalid", manifest)
            declared.add(rel)

        archived_payloads = {name for name in names if name.startswith("payload/")}
        if declared != archived_payloads:
            raise BundleError("payload_mismatch", "manifest and archive payloads do not match", manifest)
        return manifest


def canonical_source_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or len(value) > 2048:
        raise BundleError("watch_path", "source relative path is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BundleError("watch_path", "source relative path contains control characters")
    normalized = unicodedata.normalize("NFC", value)
    pure = PurePosixPath(normalized)
    if normalized != value or pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise BundleError("watch_path", "source relative path is not canonical")
    return pure.as_posix()


def watch_path_key(relative):
    return canonical_source_relative(relative).casefold()


def watch_event_identity(manifest):
    items = [
        {
            "relative_path": item["relative_path"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in manifest["items"]
    ]
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
        "items": items,
    }


def deterministic_watch_event_id(manifest):
    canonical = json.dumps(
        watch_event_identity(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return str(uuid.uuid5(WATCH_EVENT_NAMESPACE, canonical))


def validate_watch_inbox_security():
    try:
        info = WATCH_INBOX.lstat()
    except OSError as exc:
        raise RuntimeError(f"watch inbox unavailable: {type(exc).__name__}")
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError("watch inbox is not a real directory")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("watch inbox ownership or permissions are unsafe")


def validate_local_watch_bundle_file(bundle_path):
    validate_watch_inbox_security()
    try:
        if bundle_path.parent.resolve() != WATCH_INBOX.resolve():
            raise BundleError("watch_inbox_source", "watch bundle is outside the local watch inbox")
        info = bundle_path.lstat()
    except BundleError:
        raise
    except OSError as exc:
        raise BundleError("watch_bundle_stat", f"watch bundle cannot be inspected: {type(exc).__name__}")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise BundleError("watch_bundle_type", "watch bundle must be a regular file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise BundleError("watch_bundle_permissions", "watch bundle permissions are unsafe")


def validate_watch_manifest(manifest):
    required = {
        "schema_version", "event_id", "source_system", "source_id", "source_entry_id",
        "operation", "source_relative_path", "previous_source_relative_path", "previous_sha256",
        "current_sha256", "source_mtime_ns", "source_modified_at", "source_size_bytes",
        "capture_error", "previous_event_id", "transition_sequence", "items",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise BundleError("watch_manifest_missing_fields", "watch manifest fields are incomplete")
    if manifest["schema_version"] != 2:
        raise BundleError("watch_manifest_schema", "local watch inbox accepts only schema v2")
    if manifest["source_system"] != "windows_folder_watch":
        raise BundleError("watch_source_system", "watch source system is not allowed")
    source_id = manifest["source_id"]
    if not isinstance(source_id, str) or not SAFE_SOURCE_RE.fullmatch(source_id) or source_id not in ALLOWED_WATCH_SOURCES:
        raise BundleError("watch_source_id", "watch source id is not configured")
    for field in ("event_id", "source_entry_id"):
        value = manifest[field]
        if not isinstance(value, str) or not EVENT_RE.fullmatch(value):
            raise BundleError(field, f"invalid {field}")
        try:
            uuid.UUID(value)
        except ValueError:
            raise BundleError(field, f"invalid {field}")
    previous_event_id = manifest["previous_event_id"]
    if previous_event_id is not None:
        if not isinstance(previous_event_id, str) or not EVENT_RE.fullmatch(previous_event_id):
            raise BundleError("watch_previous_event_id", "previous event id is invalid")
        try:
            uuid.UUID(previous_event_id)
        except ValueError:
            raise BundleError("watch_previous_event_id", "previous event id is invalid")
    transition_sequence = manifest["transition_sequence"]
    if not isinstance(transition_sequence, int) or isinstance(transition_sequence, bool) or transition_sequence < 1:
        raise BundleError("watch_transition_sequence", "transition sequence is invalid")
    operation = manifest["operation"]
    if operation not in WATCH_OPERATIONS:
        raise BundleError("watch_operation", "watch operation is not allowed")
    current_path = canonical_source_relative(manifest["source_relative_path"])
    previous_path = manifest["previous_source_relative_path"]
    if previous_path is not None:
        previous_path = canonical_source_relative(previous_path)
    previous_sha = manifest["previous_sha256"]
    current_sha = manifest["current_sha256"]
    if previous_sha is not None and (not isinstance(previous_sha, str) or not SHA_RE.fullmatch(previous_sha)):
        raise BundleError("watch_previous_hash", "previous hash is invalid")
    if current_sha is not None and (not isinstance(current_sha, str) or not SHA_RE.fullmatch(current_sha)):
        raise BundleError("watch_current_hash", "current hash is invalid")
    mtime_ns = manifest["source_mtime_ns"]
    if not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool) or mtime_ns < 0:
        raise BundleError("watch_mtime", "source mtime is invalid")
    try:
        datetime.fromisoformat(manifest["source_modified_at"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise BundleError("watch_modified_at", "source modified timestamp is invalid")
    if manifest["source_modified_at"] != watch_mtime_iso(mtime_ns):
        raise BundleError("watch_modified_at_mismatch", "source modified timestamp does not match source mtime")
    source_size = manifest["source_size_bytes"]
    if not isinstance(source_size, int) or isinstance(source_size, bool) or source_size < 0 or source_size > (2 ** 63 - 1):
        raise BundleError("watch_source_size", "source size is invalid")
    capture_error = manifest["capture_error"]
    if capture_error is not None and (
        not isinstance(capture_error, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", capture_error)
    ):
        raise BundleError("watch_capture_error", "capture error code is invalid")

    items = manifest["items"]
    if not isinstance(items, list):
        raise BundleError("watch_items", "watch items must be a list")
    has_payload = len(items) == 1
    metadata_only = (
        operation == "modified" and capture_error is None and not items
        and previous_sha is not None and previous_sha == current_sha
    )
    capture_failure = capture_error is not None
    capture_recovered = (
        operation == "modified" and capture_error is None and has_payload
        and previous_sha is not None and previous_sha == current_sha
    )
    expects_payload = operation in {"baseline", "added", "modified"} and not metadata_only and not capture_failure
    if len(items) not in {0, 1} or has_payload != expects_payload:
        raise BundleError("watch_item_count", "watch operation has an invalid item count")
    if has_payload:
        item = items[0]
        if not isinstance(item, dict) or set(item) != {"relative_path", "size_bytes", "sha256"}:
            raise BundleError("watch_item", "watch item fields are invalid")
        if item["relative_path"] != "payload/content":
            raise BundleError("watch_item_path", "watch payload path is invalid")
        if not isinstance(item["size_bytes"], int) or isinstance(item["size_bytes"], bool) or not 0 <= item["size_bytes"] <= MAX_ITEM_BYTES:
            raise BundleError("watch_item_size", "watch payload size is invalid")
        if item["size_bytes"] != source_size:
            raise BundleError("watch_item_source_size", "watch payload size differs from source metadata")
        if not isinstance(item["sha256"], str) or not SHA_RE.fullmatch(item["sha256"]):
            raise BundleError("watch_item_hash", "watch payload hash is invalid")
        if item["sha256"] != current_sha:
            raise BundleError("watch_item_hash", "watch payload and current hashes differ")

    if operation in {"baseline", "added"}:
        valid_state = (
            previous_path is None and previous_sha is None
            and ((capture_failure and current_sha is None) or (not capture_failure and current_sha is not None))
        )
    elif operation == "modified":
        valid_state = previous_path == current_path and (
            metadata_only
            or capture_recovered
            or (capture_failure and current_sha is None)
            or (not capture_failure and previous_sha != current_sha and current_sha is not None)
        )
    elif operation == "deleted":
        valid_state = capture_error is None and previous_path == current_path and current_sha is None
    else:
        valid_state = capture_error is None and previous_path is not None and previous_path != current_path and previous_sha is not None and previous_sha == current_sha
    if not valid_state:
        raise BundleError("watch_transition", "watch operation transition is invalid")
    if deterministic_watch_event_id(manifest) != manifest["event_id"]:
        raise BundleError("watch_event_id", "watch event id is not deterministic for this manifest")
    return manifest


def inspect_watch_bundle(bundle_path):
    validate_local_watch_bundle_file(bundle_path)
    match = WATCH_BUNDLE_RE.fullmatch(bundle_path.name)
    if not match:
        raise BundleError("watch_bundle_name", "unexpected local watch bundle name")
    if bundle_path.stat().st_size > MAX_TOTAL_BYTES:
        raise BundleError("watch_archive_too_large", "watch archive exceeds total size limit")
    try:
        archive = zipfile.ZipFile(bundle_path, "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise BundleError("watch_archive_invalid", "watch archive is invalid")
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > 2:
            raise BundleError("watch_archive_item_count", "watch archive contains too many files")
        names = set()
        for info in infos:
            if not safe_archive_name(info.filename) or is_symlink(info):
                raise BundleError("watch_archive_path", "watch archive contains an unsafe path")
            if info.flag_bits & 0x1:
                raise BundleError("watch_transport_encrypted", "watch archive contains encrypted members")
            if info.filename == "manifest.json" and info.file_size > MAX_MANIFEST_BYTES:
                raise BundleError("watch_manifest_too_large", "watch manifest exceeds 1 MB")
            if info.file_size > MAX_ITEM_BYTES and info.filename != "manifest.json":
                raise BundleError("watch_item_too_large", "watch item exceeds the size limit")
            if info.filename in names:
                raise BundleError("watch_duplicate_archive_path", "watch archive contains duplicate paths")
            names.add(info.filename)
        if "manifest.json" not in names:
            raise BundleError("watch_manifest_missing", "watch manifest is missing")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            raise BundleError("watch_manifest_invalid", f"watch manifest cannot be read: {type(exc).__name__}")
        manifest = validate_watch_manifest(manifest)
        if manifest["event_id"].lower() != match.group(1).lower():
            raise BundleError("watch_bundle_event_mismatch", "watch bundle name and event id differ")
        expected_names = {"manifest.json", *(item["relative_path"] for item in manifest["items"])}
        if names != expected_names:
            raise BundleError("watch_payload_mismatch", "watch manifest and archive payloads differ")
        return manifest


def extract_watch_payload(bundle_path, manifest, work_dir):
    if not manifest["items"]:
        return None
    item = manifest["items"][0]
    destination = work_dir / "content"
    digest = hashlib.sha256()
    size = 0
    with zipfile.ZipFile(bundle_path, "r") as archive, archive.open(item["relative_path"], "r") as source, destination.open("wb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_ITEM_BYTES:
                raise BundleError("watch_payload_too_large", "watch payload exceeded the size limit")
            digest.update(chunk)
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    destination.chmod(0o600)
    if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
        raise BundleError("watch_checksum_mismatch", "watch payload size or hash differs from manifest")
    return destination


import video_hold

def extract_and_verify(bundle_path, manifest, work_dir):
    # This only removes the outer transport wrapper. A payload that is itself an
    # archive or package is copied byte-for-byte and is never recursively opened.
    extracted = {}
    with zipfile.ZipFile(bundle_path, "r") as archive:
        for item in manifest["items"]:
            rel = item["relative_path"]
            kind=video_hold.classify(rel)
            if kind:
                video_hold.register('win11',{'event_id':manifest['event_id'],'member':rel},name=PurePosixPath(rel).name,
                    size=item.get('size_bytes'),version=item.get('sha256'),existing=True,embedded=True,kind=kind)
                continue
            destination = work_dir / PurePosixPath(rel)
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(rel, "r") as source, destination.open("wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ITEM_BYTES:
                        raise BundleError("payload_too_large", "payload exceeded size limit while extracting")
                    digest.update(chunk)
                    target.write(chunk)
            destination.chmod(0o600)
            if size != item["size_bytes"] or digest.hexdigest() != item["sha256"].lower():
                raise BundleError("checksum_mismatch", "payload size or hash does not match manifest")
            video_hold.inspect_container(destination,'win11',{'event_id':manifest['event_id']},version=item['sha256'],parent=rel)
            extracted[rel] = destination
    return extracted


def read_edges(path, prefix_bytes=4096, tail_bytes=65557):
    size = path.stat().st_size
    with path.open("rb") as handle:
        prefix = handle.read(prefix_bytes)
        if size <= tail_bytes:
            tail = prefix if size <= prefix_bytes else path.read_bytes()
        else:
            handle.seek(-tail_bytes, os.SEEK_END)
            tail = handle.read(tail_bytes)
    return size, prefix, tail


def validate_utf8_text(path, consumer=None):
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    try:
        with path.open("rb") as handle:
            for raw in iter(lambda: handle.read(64 * 1024), b""):
                text = decoder.decode(raw)
                if "\x00" in text:
                    return "text_contains_nul"
                if consumer:
                    consumer(text)
            final = decoder.decode(b"", final=True)
            if "\x00" in final:
                return "text_contains_nul"
            if consumer and final:
                consumer(final)
    except UnicodeDecodeError:
        return "text_not_utf8"
    return None


class StaticHTMLInspector(HTMLParser):
    DYNAMIC_TAGS = {
        "applet", "base", "embed", "form", "frame", "frameset", "iframe", "object", "script",
    }
    ACTIVE_SCHEMES = (
        "javascript:", "vbscript:", "data:text/html", "data:image/svg+xml",
        "data:application/xhtml+xml", "data:application/xml", "data:text/xml",
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.reason = None

    def mark(self, reason):
        if self.reason is None:
            self.reason = reason

    def inspect_tag(self, tag, attrs):
        lowered_tag = tag.lower()
        if lowered_tag in self.DYNAMIC_TAGS:
            self.mark(f"dynamic_html_tag:{lowered_tag}")
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            value = html.unescape(raw_value or "").strip()
            if re.fullmatch(r"on[a-z0-9_-]+", name):
                self.mark("dynamic_html_event_handler")
            if name == "srcdoc":
                self.mark("dynamic_html_srcdoc")
            compact = re.sub(r"[\x00-\x20]+", "", value.lower())
            if compact.startswith(self.ACTIVE_SCHEMES):
                self.mark("dynamic_html_active_url")
            if lowered_tag == "meta" and name == "http-equiv" and value.lower() == "refresh":
                self.mark("dynamic_html_meta_refresh")
            css_reason = active_css_reason(value)
            if css_reason:
                self.mark(css_reason)

    def handle_starttag(self, tag, attrs):
        self.inspect_tag(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self.inspect_tag(tag, attrs)


def validate_static_html(path):
    inspector = StaticHTMLInspector()
    raw_tail = ""
    raw_reason = None

    def consume(text):
        nonlocal raw_tail, raw_reason
        combined = raw_tail + text
        lowered = combined.lower()
        if "<?php" in lowered or "<%" in combined:
            raw_reason = raw_reason or "dynamic_html_template_code"
        raw_reason = raw_reason or active_css_reason(html.unescape(combined))
        inspector.feed(text)
        raw_tail = combined[-512:]

    error = validate_utf8_text(path, consume)
    if error:
        return error
    try:
        inspector.close()
    except Exception:
        return "html_parse_error"
    return raw_reason or inspector.reason


def decode_css_escapes(value):
    def replace_escape(match):
        hexadecimal = match.group(1)
        if hexadecimal:
            codepoint = int(hexadecimal, 16)
            return chr(codepoint) if 0 < codepoint <= 0x10FFFF else "\ufffd"
        return match.group(2) or ""

    return re.sub(
        r"\\(?:([0-9a-fA-F]{1,6})(?:\r\n|[\t\n\f\r ])?|([^\r\n]))",
        replace_escape,
        value,
    )


def active_css_reason(value):
    lowered = decode_css_escapes(value).lower()
    if re.search(r"expression\s*\(|@import\b", lowered):
        return "dynamic_html_active_css"
    if re.search(
        r"url\s*\(\s*['\"]?\s*(?:(?:javascript|vbscript)\s*:|data\s*:\s*"
        r"(?:text/html|image/svg\+xml|application/xhtml\+xml))",
        lowered,
    ):
        return "dynamic_html_active_css_url"
    return None


def contains_pdf_active_content(path):
    markers = (b"/javascript", b"/js", b"/launch", b"/openaction", b"/richmedia", b"/aa")
    overlap = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            combined = overlap + chunk
            decoded_names = re.sub(
                rb"#([0-9a-fA-F]{2})",
                lambda match: bytes([int(match.group(1), 16)]),
                combined,
            )
            lowered = decoded_names.lower()
            if any(marker in lowered for marker in markers):
                return True
            overlap = combined[-128:]
    return False


def validate_pdf_envelope(path):
    size, prefix, tail = read_edges(path)
    if size < 12 or not prefix.startswith(b"%PDF-"):
        return "pdf_signature_mismatch"
    if b"%%EOF" not in tail:
        return "pdf_missing_eof"
    if contains_pdf_active_content(path):
        return "pdf_active_content"
    return None


def validate_ooxml_envelope(path, family):
    required_main = {
        "word": "word/document.xml",
        "xl": "xl/workbook.xml",
        "ppt": "ppt/presentation.xml",
    }[family]
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 20000:
                return "ooxml_entry_count"
            names = set()
            total_uncompressed = 0
            for info in infos:
                if info.flag_bits & 0x1:
                    return "ooxml_encrypted"
                if not safe_archive_name(info.filename) or is_symlink(info):
                    return "ooxml_unsafe_member"
                names.add(info.filename)
                total_uncompressed += info.file_size
                if info.file_size > MAX_ITEM_BYTES or total_uncompressed > MAX_TOTAL_BYTES:
                    return "ooxml_expansion_limit"
                if info.file_size > 10 * 1024 * 1024 and info.compress_size * 1000 < info.file_size:
                    return "ooxml_suspicious_ratio"
            if {"[Content_Types].xml", "_rels/.rels", required_main} - names:
                return "ooxml_required_member_missing"
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                return "ooxml_active_content"
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return "ooxml_invalid_zip"
    return None


def validate_raster_envelope(path, suffix):
    size, prefix, tail = read_edges(path)
    if suffix == ".png":
        return None if size >= 33 and prefix.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND\xaeB`\x82" in tail else "image_signature_mismatch"
    if suffix in {".jpg", ".jpeg"}:
        clean_tail = tail.rstrip(b"\x00\r\n\t ")
        return None if size >= 4 and prefix.startswith(b"\xff\xd8\xff") and clean_tail.endswith(b"\xff\xd9") else "image_signature_mismatch"
    if suffix == ".gif":
        return None if prefix[:6] in {b"GIF87a", b"GIF89a"} and tail.endswith(b";") else "image_signature_mismatch"
    if suffix == ".webp":
        if size < 12 or prefix[:4] != b"RIFF" or prefix[8:12] != b"WEBP":
            return "image_signature_mismatch"
        declared = struct.unpack("<I", prefix[4:8])[0] + 8
        return None if declared == size else "image_size_mismatch"
    if suffix == ".bmp":
        if size < 14 or prefix[:2] != b"BM":
            return "image_signature_mismatch"
        declared = struct.unpack("<I", prefix[2:6])[0]
        return None if declared == size else "image_size_mismatch"
    if suffix in {".tif", ".tiff"}:
        if prefix[:4] == b"II*\x00":
            offset = struct.unpack("<I", prefix[4:8])[0] if len(prefix) >= 8 else size
        elif prefix[:4] == b"MM\x00*":
            offset = struct.unpack(">I", prefix[4:8])[0] if len(prefix) >= 8 else size
        else:
            return "image_signature_mismatch"
        return None if 8 <= offset < size else "image_structure_invalid"
    if suffix in {".heic", ".heif", ".avif"}:
        if size < 16 or prefix[4:8] != b"ftyp":
            return "image_signature_mismatch"
        box_size = struct.unpack(">I", prefix[:4])[0]
        brands = {prefix[index:index + 4] for index in range(8, min(len(prefix), box_size), 4)}
        allowed = {b"avif", b"avis"} if suffix == ".avif" else {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}
        return None if 16 <= box_size <= size and brands.intersection(allowed) else "image_structure_invalid"
    return "image_type_unknown"


def zip_envelope_valid(prefix, tail):
    return prefix[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"} and (b"PK\x05\x06" in tail or b"PK\x06\x06" in tail)


def validate_zip_central_directory(path):
    # Reading ZipInfo records does not read, decrypt, or decompress member data.
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > 200000:
                return "archive_entry_count"
            if any(info.flag_bits & 0x1 for info in infos):
                return "archive_encrypted"
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return "archive_structure_invalid"
    return None


def validate_external_envelope(path, suffix):
    if suffix in EXTERNAL_TEXT_EXTENSIONS:
        error = validate_utf8_text(path)
        if error:
            return error
        if suffix == ".svg":
            with path.open("rb") as handle:
                prefix = handle.read(4096).lower()
            if b"<svg" not in prefix:
                return "svg_signature_mismatch"
        return None

    size, prefix, tail = read_edges(path)
    if not size:
        return "external_file_empty"
    ole_magic = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    if suffix in {".sqlite", ".sqlite3"}:
        return None if prefix.startswith(b"SQLite format 3\x00") else "database_signature_mismatch"
    if suffix == ".db":
        # .db is intentionally generic; a non-empty opaque database stays external.
        return None
    if suffix == ".duckdb":
        return None if len(prefix) >= 12 and prefix[8:12] == b"DUCK" else "database_signature_mismatch"
    if suffix == ".parquet":
        return None if prefix.startswith(b"PAR1") and tail.endswith(b"PAR1") else "database_signature_mismatch"
    if suffix in {".mdb", ".accdb"}:
        access_magic = b"Standard Jet DB" in prefix[:64] or b"Standard ACE DB" in prefix[:64]
        return None if prefix.startswith(ole_magic) or access_magic else "database_signature_mismatch"
    if suffix in LEGACY_OFFICE_EXTENSIONS:
        return None if prefix.startswith(ole_magic) else "legacy_office_signature_mismatch"
    if suffix == ".mp3":
        frame_sync = len(prefix) >= 2 and prefix[0] == 0xFF and (prefix[1] & 0xE0) == 0xE0
        return None if prefix.startswith(b"ID3") or frame_sync else "media_signature_mismatch"
    if suffix == ".wav":
        return None if prefix[:4] == b"RIFF" and prefix[8:12] == b"WAVE" else "media_signature_mismatch"
    if suffix == ".avi":
        return None if prefix[:4] == b"RIFF" and prefix[8:12] == b"AVI " else "media_signature_mismatch"
    if suffix == ".mkv":
        return None if prefix.startswith(b"\x1aE\xdf\xa3") else "media_signature_mismatch"
    if suffix in {".m4a", ".mp4", ".mov"}:
        atom = prefix[4:8]
        return None if atom in {b"ftyp", b"moov", b"mdat", b"wide", b"free"} else "media_signature_mismatch"
    if suffix == ".eml":
        header = re.search(br"(?im)^(?:from|date|subject|message-id|mime-version|content-type):", prefix)
        return None if header else "mail_signature_mismatch"
    if suffix == ".mbox":
        return None if prefix.startswith(b"From ") else "mail_signature_mismatch"
    if suffix in {".pst", ".ost"}:
        return None if prefix.startswith(b"!BDN") else "mail_signature_mismatch"
    if suffix == ".dwg":
        return None if prefix.startswith(b"AC10") else "design_signature_mismatch"
    if suffix == ".dxf":
        binary_dxf = prefix.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00")
        text_dxf = b"SECTION" in prefix[:2048].upper() and not prefix.startswith(b"\x00")
        return None if binary_dxf or text_dxf else "design_signature_mismatch"
    if suffix in {".step", ".stp"}:
        return None if prefix.lstrip().upper().startswith(b"ISO-10303-21;") else "design_signature_mismatch"
    if suffix in {".iges", ".igs"}:
        try:
            prefix[:512].decode("ascii")
        except UnicodeDecodeError:
            return "design_signature_mismatch"
        return None if size >= 80 and len(prefix) > 72 and chr(prefix[72]).upper() in "SGDPT" else "design_signature_mismatch"
    if suffix == ".psd":
        return None if prefix.startswith((b"8BPS\x00\x01", b"8BPS\x00\x02")) else "design_signature_mismatch"
    if suffix == ".ai":
        return None if prefix.startswith((b"%PDF-", b"%!PS-Adobe")) else "design_signature_mismatch"
    if suffix == ".indd":
        indd_magic = bytes.fromhex("0606edf5d81d46e5bd31efe7fe74b71d")
        return None if prefix.startswith(indd_magic) else "design_signature_mismatch"
    if suffix == ".sketch":
        if not zip_envelope_valid(prefix, tail):
            return "design_signature_mismatch"
        return validate_zip_central_directory(path)
    if suffix == ".fig":
        if prefix.startswith(b"fig-kiwi"):
            return None
        if not zip_envelope_valid(prefix, tail):
            return "design_signature_mismatch"
        return validate_zip_central_directory(path)
    if suffix == ".iso":
        if size < 32774:
            return "disk_image_signature_mismatch"
        with path.open("rb") as handle:
            handle.seek(32769)
            iso_magic = handle.read(5)
        return None if iso_magic == b"CD001" else "disk_image_signature_mismatch"
    if suffix in EXTERNAL_ZIP_EXTENSIONS:
        if not zip_envelope_valid(prefix, tail):
            return "archive_signature_mismatch"
        return validate_zip_central_directory(path)
    if suffix in {".gz", ".tgz"}:
        return None if prefix.startswith(b"\x1f\x8b") else "archive_signature_mismatch"
    if suffix in {".bz2", ".tbz", ".tbz2"}:
        return None if prefix.startswith(b"BZh") else "archive_signature_mismatch"
    if suffix in {".xz", ".txz"}:
        return None if prefix.startswith(b"\xfd7zXZ\x00") else "archive_signature_mismatch"
    if suffix == ".7z":
        return None if prefix.startswith(b"7z\xbc\xaf'\x1c") else "archive_signature_mismatch"
    if suffix == ".rar":
        return None if prefix.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")) else "archive_signature_mismatch"
    if suffix == ".tar":
        return None if len(prefix) >= 262 and prefix[257:262] == b"ustar" else "archive_signature_mismatch"
    if suffix == ".wasm":
        return None if prefix.startswith(b"\x00asm") else "binary_signature_mismatch"
    if suffix in {".exe", ".dll", ".scr"}:
        return None if prefix.startswith(b"MZ") else "binary_signature_mismatch"
    if suffix in {".msi", ".msp"}:
        return None if prefix.startswith(ole_magic) else "binary_signature_mismatch"
    if suffix == ".pkg":
        return None if prefix.startswith(b"xar!") else "binary_signature_mismatch"
    if suffix == ".dmg":
        return None if len(tail) >= 512 and tail[-512:-508] == b"koly" else "binary_signature_mismatch"
    if suffix == ".class":
        return None if prefix.startswith(b"\xca\xfe\xba\xbe") else "binary_signature_mismatch"
    if suffix == ".a":
        return None if prefix.startswith(b"!<arch>\n") else "binary_signature_mismatch"
    if suffix in {".so", ".dylib", ".o", ".lib"}:
        executable_magic = {
            b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"!<ar",
        }
        return None if prefix[:4] in executable_magic else "binary_signature_mismatch"
    if suffix in {".com", ".lnk"}:
        return None
    return "external_type_unknown"


def classify_payload(path, relative_path):
    suffix = PurePosixPath(relative_path).suffix.lower()
    if suffix in OBSIDIAN_TEXT_EXTENSIONS:
        error = validate_utf8_text(path)
        return {"storage_class": "quarantine", "media_kind": "text", "code": error} if error else {"storage_class": "obsidian", "media_kind": "text", "code": "allowed_text"}
    if suffix in HTML_EXTENSIONS:
        error = validate_static_html(path)
        return {"storage_class": "quarantine", "media_kind": "html", "code": error} if error else {"storage_class": "obsidian", "media_kind": "html", "code": "allowed_static_html"}
    if suffix == ".pdf":
        error = validate_pdf_envelope(path)
        return {"storage_class": "quarantine", "media_kind": "pdf", "code": error} if error else {"storage_class": "obsidian", "media_kind": "pdf", "code": "allowed_pdf"}
    if suffix in OOXML_EXTENSIONS:
        error = validate_ooxml_envelope(path, OOXML_EXTENSIONS[suffix])
        return {"storage_class": "quarantine", "media_kind": "ooxml", "code": error} if error else {"storage_class": "obsidian", "media_kind": "ooxml", "code": "allowed_ooxml"}
    if suffix in RASTER_EXTENSIONS:
        error = validate_raster_envelope(path, suffix)
        return {"storage_class": "quarantine", "media_kind": "raster", "code": error} if error else {"storage_class": "obsidian", "media_kind": "raster", "code": "allowed_raster"}
    if suffix in KNOWN_EXTERNAL_EXTENSIONS:
        error = validate_external_envelope(path, suffix)
        return {"storage_class": "quarantine", "media_kind": "external_asset", "code": error} if error else {"storage_class": "external", "media_kind": "external_asset", "code": "externalized_asset"}
    return {"storage_class": "quarantine", "media_kind": "unknown", "code": "unknown_file_type"}


def unique_destination(base, name):
    candidate = base / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        candidate = base / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise BundleError("name_collision", "too many filename collisions")


def yaml_quote(value):
    return json.dumps(value, ensure_ascii=False)


def markdown_escape(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def format_item_rows(item_results):
    rows = []
    labels = {"obsidian": "Obsidian", "external": "外置", "quarantine": "隔离", "waiting":"等待指令"}
    for item in item_results:
        action = "未展开，等待指令" if item["disposition"] == "waiting_instruction" else "重复未再复制" if item["disposition"] == "duplicate" else "已保存"
        row = (
            f"- {action}（{labels[item['storage_class']]}）：{markdown_escape(item['name'])} · "
            f"{item['sha256'][:12]}… · {markdown_escape(item['destination_ref'])}"
        )
        if item["storage_class"] == "quarantine":
            row += f" · 原因 {markdown_escape(item['code'])}"
        rows.append(row)
    return "\n".join(rows) if rows else "- 没有可保存项目。"


def create_event_receipt(manifest, bundle_sha, item_results, destination_relative, counts):
    body = format_item_rows(item_results)
    return f"""---
document_type: incoming_transfer_receipt
schema_version: 1
event_id: {yaml_quote(manifest['event_id'])}
source_device: {yaml_quote(manifest['source_device'])}
observed_at: {yaml_quote(manifest['created_at'])}
bundle_sha256: {yaml_quote('sha256:' + bundle_sha)}
receipt_scope: mac_canonical_path_hash_verified_not_icloud_upload
obsidian_count: {counts['obsidian']}
external_count: {counts['external']}
quarantine_count: {counts['quarantine']}
payload_handling: transport_stream_only_never_execute_or_unpack_payload
---

## 接收结果

{body}

## 证据边界

本清单只证明 Mac 已完成传输包结构、大小、SHA-256 和保守文件结构检查，并按类别写入本地权威目录；接收端未运行或自动解压 payload。该检查不能证明内容真实、已人工复核、文件内部完全无损或已上传 iCloud。

## 来源

- 来源设备：{markdown_escape(manifest['source_device'])}
- 事件编号：{manifest['event_id']}
- 相对目录：{markdown_escape(destination_relative)}
"""


def normalize_markdown_body(body):
    """Match the canonical memory-schema body normalization byte for byte."""
    return "\n\n" + body.lstrip("\r\n")


def queue_ack(event_id, return_node, bundle_sha, status, destination_relative, code, counts=None):
    counts = counts or {"obsidian": 0, "external": 0, "quarantine": 0}
    payload = {
        "schema_version": 1,
        "event_id": event_id,
        "bundle_sha256": bundle_sha,
        "status": status,
        "code": code,
        "stored_at": now_iso(),
        "destination_relative": destination_relative,
        "return_node": return_node,
        "receipt_scope": "mac_canonical_path_hash_verified_not_icloud_upload",
        "obsidian_count": int(counts["obsidian"]),
        "external_count": int(counts["external"]),
        "quarantine_count": int(counts["quarantine"]),
        "video_waiting_count": int(counts.get("waiting", 0)),
        "content_complete": not bool(counts.get("waiting") or counts["quarantine"]),
    }
    path = ACK_OUTBOX / f"business-ack-{event_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def send_ack(path, conn):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        node = payload["return_node"]
        if node not in ALLOWED_RETURN_NODES:
            raise RuntimeError("ack target is not allowed")
        result = subprocess.run(
            [TAILSCALE, "file", "cp", "--update-interval=0", str(path), f"{node}:"],
            timeout=45,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            destination = ACK_SENT / path.name
            os.replace(path, destination)
            conn.execute("DELETE FROM ack_attempts WHERE ack_file = ?", (path.name,))
            conn.commit()
            append_log("ack_sent", event_id=payload.get("event_id"), target=node)
            return True
        error = (result.stderr or result.stdout or "tailscale file cp failed")[:300]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]

    row = conn.execute("SELECT attempts FROM ack_attempts WHERE ack_file = ?", (path.name,)).fetchone()
    attempts = (row[0] if row else 0) + 1
    conn.execute(
        "INSERT INTO ack_attempts(ack_file, attempts, last_attempt_at) VALUES(?,?,?) "
        "ON CONFLICT(ack_file) DO UPDATE SET attempts=excluded.attempts,last_attempt_at=excluded.last_attempt_at",
        (path.name, attempts, now_iso()),
    )
    conn.commit()
    append_log("ack_failed", ack_file=path.name, attempts=attempts, error=error)
    if attempts == 3:
        notify_failure("Mac 已保存资料，但公司电脑回执连续发送失败；资料未丢失。")
    return False


def retry_acks(conn):
    for path in sorted(ACK_OUTBOX.glob("business-ack-*.json")):
        row = conn.execute(
            "SELECT attempts, last_attempt_at FROM ack_attempts WHERE ack_file = ?",
            (path.name,),
        ).fetchone()
        if row and row[1]:
            try:
                if time.time() - datetime.fromisoformat(row[1]).timestamp() < 300:
                    continue
            except ValueError:
                pass
        send_ack(path, conn)


def quarantine_bundle(bundle_path, code, message):
    destination = unique_destination(QUARANTINE, bundle_path.name)
    os.replace(bundle_path, destination)
    reason = destination.with_suffix(destination.suffix + ".reason.json")
    write_reason_json_atomic(
        reason, {"code": code, "message": message},
    )
    append_log("bundle_quarantined", bundle=destination.name, code=code, message=message)
    notify_failure(f"收到的资料包被隔离：{code}。")


def _reason_existing_matches(path, fields, match_fields=None, allowed_optional=()):
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BundleError("reason_integrity", "reason record is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise BundleError("reason_integrity", "reason record permissions are unsafe")
        existing = json.loads(path.read_text(encoding="utf-8"))
    except BundleError:
        raise
    except Exception as exc:
        raise BundleError("reason_integrity", f"reason record is unreadable: {type(exc).__name__}")
    if not isinstance(existing, dict) or not isinstance(existing.get("at"), str):
        raise BundleError("reason_integrity", "reason record structure is invalid")
    try:
        datetime.fromisoformat(existing["at"].replace("Z", "+00:00"))
    except ValueError:
        raise BundleError("reason_integrity", "reason record timestamp is invalid")
    optional = set(allowed_optional)
    required = {"at", *(set(fields) - optional)}
    allowed = {"at", *fields, *optional}
    if not required.issubset(existing) or set(existing) - allowed:
        raise BundleError("reason_integrity", "reason record fields are invalid")
    for key in match_fields or fields:
        if existing.get(key) != fields[key]:
            raise BundleError("reason_collision", "reason record conflicts with expected content")
    return False


def write_reason_json_atomic(path, fields, match_fields=None, allowed_optional=()):
    """Create an owner-only reason record or validate an immutable crash residue."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        return _reason_existing_matches(path, fields, match_fields, allowed_optional)
    for residual in path.parent.glob(f".{path.name}.tmp-*"):
        try:
            info = residual.lstat()
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid == os.getuid():
                residual.unlink()
        except OSError:
            continue
    tmp = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex[:8]}"
    payload = {"at": now_iso(), **fields}
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()
    return True


def route_unrelated_file(path):
    UNRELATED_DOWNLOADS.mkdir(parents=True, exist_ok=True)
    destination = unique_destination(UNRELATED_DOWNLOADS, path.name)
    os.replace(path, destination)
    append_log("unrelated_taildrop_routed", file=destination.name)
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'display notification "非示例企业资料已转存到 Downloads/Tailscale Received" '
                'with title "Tailscale 收到其他文件"',
            ],
            check=False,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def destination_ref(storage_class, relative):
    if storage_class not in STORAGE_CLASSES:
        raise BundleError("storage_class", "invalid storage class")
    pure = PurePosixPath(str(relative))
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise BundleError("destination_ref", "invalid destination reference")
    required_prefixes = {
        "obsidian": "91_归档",
        "external": "external_payloads",
        "quarantine": "quarantine/items",
    }
    value = pure.as_posix()
    prefix = required_prefixes[storage_class]
    if value != prefix and not value.startswith(prefix + "/"):
        raise BundleError("destination_ref", "destination reference does not match storage class")
    return f"{storage_class}:{value}"


def watch_source_uuid(source_id):
    return str(uuid.uuid5(WATCH_SOURCE_NAMESPACE, source_id))


def watch_content_name(relative_path):
    suffix = PurePosixPath(relative_path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
        suffix = ""
    return "content" + suffix


def watch_version_parts(source_id, source_entry_id, version_number, relative_path):
    relative = PurePosixPath(
        "win11-folder-watch", "versions", source_id, source_entry_id,
        f"v{version_number:06d}", watch_content_name(relative_path),
    ).as_posix()
    return WATCH_VERSIONS_ROOT.parent.parent / relative, f"external:{relative}"


def watch_quarantine_parts(source_id, source_entry_id, version_number, relative_path):
    relative = PurePosixPath(
        "quarantine", "win11-folder-watch", source_id, source_entry_id,
        f"v{version_number:06d}", watch_content_name(relative_path),
    ).as_posix()
    return NON_OBSIDIAN_ROOT / relative, f"quarantine:{relative}"


def ensure_safe_parent(root, relative):
    pure = PurePosixPath(str(relative))
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise BundleError("watch_destination", "watch destination is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for part in pure.parts[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            try:
                mode = current.lstat().st_mode
            except OSError as exc:
                raise BundleError("watch_destination", f"watch destination cannot be inspected: {type(exc).__name__}")
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise BundleError("watch_destination", "watch destination parent is not a real directory")
        else:
            current.mkdir(mode=0o700)
    destination = root.joinpath(*pure.parts)
    if destination.is_symlink():
        raise BundleError("watch_destination", "watch destination is a symbolic link")
    return destination


def copy_atomic(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        with source.open("rb") as input_handle, tmp.open("xb") as output_handle:
            for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, destination)
    finally:
        if tmp.exists():
            tmp.unlink()


def same_existing_file(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def remove_empty_mirror_parents(path):
    mirror_root = MEMORY_ROOT / "91_归档" / "Win11镜像"
    remove_empty_parents(path, mirror_root)


def remove_empty_parents(path, root):
    current = path.parent
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def mirror_path(relative_path):
    relative = canonical_source_relative(relative_path)
    return ensure_safe_parent(MEMORY_ROOT / "91_归档" / "Win11镜像", relative)


def resolve_watch_version_ref(reference):
    prefix = "external:win11-folder-watch/versions/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise BundleError("watch_version_ref", "stored watch version reference is invalid")
    relative = reference.split(":", 1)[1]
    destination = ensure_safe_parent(NON_OBSIDIAN_ROOT, relative)
    if not destination.is_file() or destination.is_symlink():
        raise BundleError("watch_version_missing", "stored watch version is missing")
    return destination


def create_watch_review_note(manifest, bundle_sha, storage_class, version_number, version_ref, mirror_relative, quarantine_ref):
    source_id = manifest["source_id"]
    source_entry_id = manifest["source_entry_id"]
    operation_labels = {
        "baseline": "基线", "added": "新增", "modified": "修改",
        "deleted": "删除（墓碑）", "renamed": "改名",
    }
    previous_path = manifest.get("previous_source_relative_path") or "无"
    previous_sha = manifest.get("previous_sha256") or "无"
    current_sha = manifest.get("current_sha256") or "无"
    mirror_text = f"obsidian:{mirror_relative}" if mirror_relative else "无（该类型不进入 Obsidian 最新镜像）"
    quarantine_text = quarantine_ref or "无"
    capture_error_text = manifest.get("capture_error") or "无"
    if manifest.get("capture_error"):
        evidence_text = (
            "该记录只证明接收器校验了本地事件包和来源元数据；来源内容未能读取，"
            "因此本次未取得当前内容哈希、未新增不可变版本，也未完成内容结构校验。接收器已生成本地隔离说明，"
            "等待后续重试和人工确认；不证明业务事实真实、文件内容安全或已完成 iCloud 上传。"
        )
    else:
        evidence_text = (
            "该记录是本机对 Win11 共享文件夹的机械观察，内容仍未核验。它只证明接收器校验了"
            "本地事件包、SHA-256 与保守文件结构并完成相应落库；不证明业务事实真实、已人工复核"
            "或已完成 iCloud 上传。删除只建立墓碑，外置不可变历史不会删除。"
        )
    recorded_at = now_iso()
    body = normalize_markdown_body(f"""
## 当前待确认变更

- 操作：{operation_labels[manifest['operation']]}
- 来源相对路径：{markdown_escape(manifest['source_relative_path'])}
- 之前相对路径：{markdown_escape(previous_path)}
- 之前 SHA-256：{previous_sha}
- 当前 SHA-256：{current_sha}
- 来源修改时间：{markdown_escape(manifest['source_modified_at'])}
- 当前版本号：{version_number}
- 分类：{storage_class}
- 捕获异常：{capture_error_text}
- 不可变版本：{markdown_escape(version_ref or '无（墓碑或纯改名不新增版本）')}
- Obsidian 最新镜像：{markdown_escape(mirror_text)}
- 隔离副本：{markdown_escape(quarantine_text)}

## 证据边界

{evidence_text}

## 来源

- 来源系统：windows_folder_watch
- 来源编号：{markdown_escape(source_id)}
- 来源条目：{manifest['source_entry_id']}
- 事件编号：{manifest['event_id']}

## 更新记录

- {recorded_at}：folder watcher 事件由 receiver 唯一落库，待人工确认。
""")
    content_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    frontmatter = f"""---
memory_id: BUSINESS-WIN-{source_entry_id}
category: incoming
memory_scope: internal_nonpublic
memory_kind: status
origin_type: system_observation
status: review_required
evidence_level: unverified
sensitivity: internal
source_system: windows_folder_watch
source_check: unverified
fact_status: pending
source_ref: {yaml_quote('windows_folder_watch:' + source_id + ':' + source_entry_id)}
observed_at: {yaml_quote(manifest['source_modified_at'])}
reviewed_at: null
valid_until: null
supersedes: []
content_hash: {yaml_quote('sha256:' + content_digest)}
source_bundle_sha256: {yaml_quote('sha256:' + bundle_sha)}
source_event_id: {yaml_quote(manifest['event_id'])}
source_entry_id: {yaml_quote(manifest['source_entry_id'])}
source_operation: {manifest['operation']}
applies_to:
  - project:business-energy
---"""
    return frontmatter + body


def watch_review_path(source_entry_id):
    return WATCH_NOTES_ROOT / f"Win11文件夹监控-{source_entry_id}.md"


def watch_entry_row(conn, source_id, source_entry_id):
    return conn.execute(
        "SELECT current_relative_path,path_key,current_sha256,source_mtime_ns,source_modified_at,size_bytes,"
        "version_number,storage_class,version_ref,mirror_relative,active,last_event_id "
        "FROM watch_entries WHERE source_id=? AND source_entry_id=?",
        (source_id, source_entry_id),
    ).fetchone()


def validate_watch_transition(conn, manifest):
    source_id = manifest["source_id"]
    entry_id = manifest["source_entry_id"]
    operation = manifest["operation"]
    entry = watch_entry_row(conn, source_id, entry_id)
    current_key = watch_path_key(manifest["source_relative_path"])
    path_owner = conn.execute(
        "SELECT source_entry_id FROM watch_entries WHERE source_id=? AND path_key=? AND active=1",
        (source_id, current_key),
    ).fetchone()
    if operation in {"baseline", "added"}:
        if (
            entry is not None or path_owner is not None
            or manifest["previous_event_id"] is not None
            or manifest["transition_sequence"] != 1
        ):
            raise BundleError("watch_state_conflict", "watch add or baseline conflicts with current state")
        return None
    if entry is None or not entry[10]:
        raise BundleError("watch_state_conflict", "watch entry is not active")
    if entry[0] != manifest["previous_source_relative_path"] or entry[2] != manifest["previous_sha256"]:
        raise BundleError("watch_state_conflict", "watch previous state does not match the database")
    event_count = conn.execute(
        "SELECT COUNT(*) FROM source_events WHERE source_id=? AND source_entry_id=?",
        (source_id, entry_id),
    ).fetchone()[0]
    if manifest["previous_event_id"] != entry[11] or manifest["transition_sequence"] != event_count + 1:
        raise BundleError("watch_state_conflict", "watch transition predecessor does not match the database")
    if operation == "modified" and current_key != entry[1]:
        raise BundleError("watch_state_conflict", "modified event changed the source path")
    if operation == "renamed" and path_owner is not None and path_owner[0] != entry_id:
        raise BundleError("watch_state_conflict", "renamed destination path is already active")
    return entry


def bump_maintenance_generation(conn, at):
    row = conn.execute("SELECT value FROM maintenance_state WHERE key='index_dirty_generation'").fetchone()
    try:
        generation = int(row[0]) + 1 if row else 1
    except (TypeError, ValueError):
        generation = 1
    conn.execute(
        "INSERT INTO maintenance_state(key,value,updated_at) VALUES('index_dirty_generation',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (str(generation), at),
    )


def set_maintenance_value(conn, key, value, at=None):
    conn.execute(
        "INSERT INTO maintenance_state(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, str(value), at or now_iso()),
    )


def run_index_refresher(*arguments):
    environment = dict(os.environ)
    environment["BUSINESS_MEMORY_INDEX_LOCK_HELD"] = "1"
    result = subprocess.run(
        ["/usr/bin/ruby", str(REFRESH_INDEXES_SCRIPT), *arguments],
        check=False,
        timeout=INDEX_REFRESH_TIMEOUT_SECONDS,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "index refresh failed")[:300]
        from index_incident_evidence import IndexRefreshFault
        raise IndexRefreshFault(result.returncode, detail)
    return result


def restore_index_refresh_health(generation):
    # An index repair must not hide unrelated quarantine or trust failures.
    try:
        previous = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(previous, dict) or previous.get("status") != "degraded":
        return
    if previous.get("detail") not in {
        "memory_index_refresh_failed", "memory_index_status_repair_failed",
        "memory_index_status_commit_failed",
    }:
        return
    write_health("running", "memory index refresh recovered", applied_generation=generation)


def refresh_dirty_indexes(conn, force=False, debounce_seconds=5):
    dirty = conn.execute(
        "SELECT value,updated_at FROM maintenance_state WHERE key='index_dirty_generation'"
    ).fetchone()
    applied = conn.execute(
        "SELECT value FROM maintenance_state WHERE key='index_applied_generation'"
    ).fetchone()
    last_error = conn.execute(
        "SELECT value,updated_at FROM maintenance_state WHERE key='index_last_error'"
    ).fetchone()
    if not dirty:
        return False
    try:
        dirty_generation = int(dirty[0])
        applied_generation = int(applied[0]) if applied else 0
    except (TypeError, ValueError):
        dirty_generation, applied_generation = 1, 0
    error_text = str(last_error[0]) if last_error and last_error[0] else ""
    status_only_repair = applied_generation >= dirty_generation and bool(error_text)
    if applied_generation >= dirty_generation and not status_only_repair:
        return False
    if not force:
        if error_text:
            try:
                error_age = time.time() - datetime.fromisoformat(last_error[1]).timestamp()
            except (TypeError, ValueError):
                error_age = INDEX_REFRESH_RETRY_COOLDOWN_SECONDS
            if error_age < INDEX_REFRESH_RETRY_COOLDOWN_SECONDS:
                return False
        try:
            age = time.time() - datetime.fromisoformat(dirty[1]).timestamp()
        except (TypeError, ValueError):
            age = debounce_seconds
        if age < debounce_seconds:
            return False
    if status_only_repair:
        repaired_at = now_iso()
        with conn:
            set_maintenance_value(conn, "index_last_error", "", repaired_at)
        try:
            run_index_refresher(
                "--status-only", "--target-generation", str(applied_generation)
            )
        except Exception as exc:
            from index_incident_evidence import record_failure
            failure_event = record_failure(ROOT, 'index_status_repair', applied_generation, exc)
            failed_at = now_iso()
            with conn:
                set_maintenance_value(conn, 'index_last_failure_event', failure_event['event_id'], failed_at)
                set_maintenance_value(
                    conn, "index_last_error", f"{type(exc).__name__}: {exc}"[:300], failed_at
                )
            append_log(
                "memory_index_status_repair_failed",
                generation=applied_generation,
                error=type(exc).__name__,
            )
            write_health(
                "degraded",
                "memory_index_status_repair_failed",
                dirty_generation=dirty_generation,
            )
            return False
        append_log("memory_index_status_repaired", generation=applied_generation)
        restore_index_refresh_health(applied_generation)
        return True

    try:
        run_index_refresher(
            "--defer-status", "--target-generation", str(dirty_generation)
        )
    except Exception as exc:
        from index_incident_evidence import record_failure
        failure_event = record_failure(ROOT, 'index_refresh', dirty_generation, exc)
        failed_at = now_iso()
        with conn:
            set_maintenance_value(conn, 'index_last_failure_event', failure_event['event_id'], failed_at)
            set_maintenance_value(conn, "index_last_error", f"{type(exc).__name__}: {exc}"[:300], failed_at)
        append_log("memory_index_refresh_failed", generation=dirty_generation, error=type(exc).__name__)
        write_health("degraded", "memory_index_refresh_failed", dirty_generation=dirty_generation)
        return False

    completed_at = now_iso()
    with conn:
        current = conn.execute(
            "SELECT value FROM maintenance_state WHERE key='index_dirty_generation'"
        ).fetchone()
        try:
            current_generation = int(current[0]) if current else dirty_generation
        except (TypeError, ValueError):
            current_generation = dirty_generation
        # Record only the generation actually covered by this invocation.  A
        # later event remains dirty and will trigger another debounced pass.
        set_maintenance_value(conn, "index_applied_generation", dirty_generation, completed_at)
        set_maintenance_value(conn, "index_last_refreshed_at", completed_at, completed_at)
        set_maintenance_value(conn, "index_last_error", "", completed_at)
    try:
        run_index_refresher(
            "--status-only", "--target-generation", str(dirty_generation)
        )
    except Exception as exc:
        from index_incident_evidence import record_failure
        failure_event = record_failure(ROOT, 'index_status_commit', dirty_generation, exc)
        failed_at = now_iso()
        with conn:
            set_maintenance_value(conn, 'index_last_failure_event', failure_event['event_id'], failed_at)
            set_maintenance_value(
                conn, "index_last_error", f"{type(exc).__name__}: {exc}"[:300], failed_at
            )
        append_log(
            "memory_index_status_commit_failed",
            generation=dirty_generation,
            error=type(exc).__name__,
        )
        write_health(
            "degraded",
            "memory_index_status_commit_failed",
            dirty_generation=current_generation,
        )
        return False
    append_log(
        "memory_indexes_refreshed", applied_generation=dirty_generation,
        current_dirty_generation=current_generation,
    )
    restore_index_refresh_health(dirty_generation)
    return True


def process_watch_bundle(bundle_path, conn):
    bundle_sha = None
    manifest = None
    work_dir = None
    version_created = None
    quarantine_created = None
    quarantine_reason_created = None
    mirror_destination = None
    mirror_backup = None
    mirror_published = False
    old_mirror_path = None
    old_mirror_backup = None
    old_mirror_removed = False
    review_path = None
    review_backup = None
    review_published = False
    database_committed = False
    try:
        manifest = inspect_watch_bundle(bundle_path)
        bundle_sha = sha256_file(bundle_path)
        existing_event = conn.execute(
            "SELECT bundle_sha256,status FROM source_events WHERE event_id=?", (manifest["event_id"],)
        ).fetchone()
        if existing_event:
            if existing_event[0] != bundle_sha:
                raise BundleError("watch_event_id_collision", "watch event id was stored with different content")
            destination = PROCESSED_BUNDLES / bundle_path.name
            if destination.exists():
                bundle_path.unlink()
            else:
                os.replace(bundle_path, destination)
            append_log("duplicate_watch_event", event_id=manifest["event_id"], source_id=manifest["source_id"])
            return

        kind=video_hold.classify(manifest['source_relative_path'])
        if kind and manifest['operation'] != 'deleted' and not manifest.get('capture_error'):
            video_hold.register('win11',{'source_id':manifest['source_id'],'relative_path':manifest['source_relative_path']},
                name=manifest['source_relative_path'],size=manifest['source_size_bytes'],
                version=video_hold.digest([manifest['source_size_bytes'],manifest['source_mtime_ns']]),existing=True,embedded=True,kind=kind)
            with conn:
                conn.execute("INSERT OR IGNORE INTO source_events(event_id,bundle_sha256,source_system,source_id,source_entry_id,operation,source_relative_path,previous_source_relative_path,previous_sha256,current_sha256,source_modified_at,items_json,previous_event_id,transition_sequence,processed_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (manifest['event_id'],bundle_sha,manifest['source_system'],manifest['source_id'],manifest['source_entry_id'],manifest['operation'],manifest['source_relative_path'],manifest.get('previous_source_relative_path'),manifest.get('previous_sha256'),manifest.get('current_sha256'),manifest['source_modified_at'],json.dumps(manifest['items']),manifest.get('previous_event_id'),manifest['transition_sequence'],now_iso(),'waiting_instruction'))
            os.replace(bundle_path,unique_destination(PROCESSED_BUNDLES,bundle_path.name))
            return
        entry = validate_watch_transition(conn, manifest)
        operation = manifest["operation"]
        source_id = manifest["source_id"]
        entry_id = manifest["source_entry_id"]
        previous_version = entry[6] if entry else 0
        previous_storage_class = entry[7] if entry else None
        previous_version_ref = entry[8] if entry else None
        previous_mirror_relative = entry[9] if entry else None
        metadata_only = (
            operation == "modified" and not manifest["items"] and not manifest.get("capture_error")
            and manifest.get("previous_sha256") == manifest.get("current_sha256")
        )
        capture_failure = bool(manifest.get("capture_error"))
        capture_recovered = (
            operation == "modified" and bool(manifest["items"])
            and manifest.get("previous_sha256") is not None
            and manifest.get("previous_sha256") == manifest.get("current_sha256")
        )

        work_dir = PROCESSING / f"watch-{manifest['event_id']}.tmp-{uuid.uuid4().hex[:8]}"
        work_dir.mkdir(parents=True, exist_ok=False)
        extracted = extract_watch_payload(bundle_path, manifest, work_dir)
        new_content_version = bool(
            extracted is not None
            and (not capture_recovered or previous_version == 0 or not previous_version_ref)
        )
        version_number = previous_version + 1 if new_content_version else previous_version
        storage_class = previous_storage_class
        version_ref = previous_version_ref
        quarantine_ref = None
        if capture_failure and entry is None:
            storage_class = "quarantine"
        if extracted is not None:
            video_hold.inspect_container(extracted,'win11',{'source_id':source_id,'relative_path':manifest['source_relative_path']},version=manifest['current_sha256'],parent=manifest['source_relative_path'])
            decision = classify_payload(extracted, manifest["source_relative_path"])
            storage_class = decision["storage_class"]
            version_path, version_ref = watch_version_parts(
                source_id, entry_id, version_number, manifest["source_relative_path"]
            )
            version_relative = PurePosixPath(version_path.relative_to(NON_OBSIDIAN_ROOT)).as_posix()
            version_path = ensure_safe_parent(NON_OBSIDIAN_ROOT, version_relative)
            if version_path.exists():
                if version_path.is_symlink() or not version_path.is_file() or sha256_file(version_path) != manifest["current_sha256"]:
                    raise BundleError("watch_version_collision", "immutable watch version path already contains different content")
            else:
                copy_atomic(extracted, version_path)
                version_created = version_path
            if storage_class == "quarantine":
                quarantine_path, quarantine_ref = watch_quarantine_parts(
                    source_id, entry_id, version_number, manifest["source_relative_path"]
                )
                quarantine_relative = PurePosixPath(quarantine_path.relative_to(NON_OBSIDIAN_ROOT)).as_posix()
                quarantine_path = ensure_safe_parent(NON_OBSIDIAN_ROOT, quarantine_relative)
                if quarantine_path.exists():
                    if quarantine_path.is_symlink() or not quarantine_path.is_file() or sha256_file(quarantine_path) != manifest["current_sha256"]:
                        raise BundleError("watch_quarantine_collision", "watch quarantine path contains different content")
                else:
                    copy_atomic(extracted, quarantine_path)
                    quarantine_created = quarantine_path
                reason = quarantine_path.with_suffix(quarantine_path.suffix + ".reason.json")
                reason_fields = {
                    "event_id": manifest["event_id"], "source_id": source_id,
                    "source_entry_id": entry_id, "sha256": manifest["current_sha256"],
                    "code": decision["code"],
                }
                if write_reason_json_atomic(
                    reason, reason_fields,
                    match_fields=("source_id", "source_entry_id", "sha256", "code"),
                    allowed_optional=("renamed_without_new_version",),
                ):
                    quarantine_reason_created = reason
        elif capture_failure:
            error_relative = PurePosixPath(
                "quarantine", "win11-folder-watch", source_id, entry_id,
                "capture-errors", f"{manifest['event_id']}.reason.json",
            ).as_posix()
            error_path = ensure_safe_parent(NON_OBSIDIAN_ROOT, error_relative)
            quarantine_ref = f"quarantine:{error_relative}"
            error_fields = {
                "event_id": manifest["event_id"], "source_id": source_id,
                "source_entry_id": entry_id, "relative_path": manifest["source_relative_path"],
                "size_bytes": manifest["source_size_bytes"], "code": manifest["capture_error"],
                "content_captured": False,
            }
            if write_reason_json_atomic(error_path, error_fields):
                quarantine_reason_created = error_path
        elif operation == "renamed":
            stored_payload = resolve_watch_version_ref(version_ref)
            if sha256_file(stored_payload) != manifest["current_sha256"]:
                raise BundleError("watch_version_hash", "stored watch version hash no longer matches")
            decision = classify_payload(stored_payload, manifest["source_relative_path"])
            storage_class = decision["storage_class"]
            if storage_class == "quarantine":
                quarantine_path, quarantine_ref = watch_quarantine_parts(
                    source_id, entry_id, version_number, manifest["source_relative_path"]
                )
                quarantine_relative = PurePosixPath(quarantine_path.relative_to(NON_OBSIDIAN_ROOT)).as_posix()
                quarantine_path = ensure_safe_parent(NON_OBSIDIAN_ROOT, quarantine_relative)
                if not quarantine_path.exists():
                    copy_atomic(stored_payload, quarantine_path)
                    quarantine_created = quarantine_path
                reason = quarantine_path.with_suffix(quarantine_path.suffix + ".reason.json")
                reason_fields = {
                    "event_id": manifest["event_id"], "source_id": source_id,
                    "source_entry_id": entry_id, "sha256": manifest["current_sha256"],
                    "code": decision["code"], "renamed_without_new_version": True,
                }
                if write_reason_json_atomic(
                    reason, reason_fields,
                    match_fields=("source_id", "source_entry_id", "sha256", "code"),
                    allowed_optional=("renamed_without_new_version",),
                ):
                    quarantine_reason_created = reason

        desired_mirror_relative = None
        mirror_source = None
        if metadata_only or capture_failure:
            desired_mirror_relative = previous_mirror_relative
        elif operation != "deleted" and storage_class == "obsidian" and watch_mirror_enabled(source_id):
            desired_mirror_relative = PurePosixPath(
                "91_归档", "Win11镜像", manifest["source_relative_path"]
            ).as_posix()
            mirror_destination = mirror_path(manifest["source_relative_path"])
            mirror_source = extracted if extracted is not None else resolve_watch_version_ref(version_ref)

        if previous_mirror_relative and not metadata_only and not capture_failure:
            prefix = "91_归档/Win11镜像/"
            if not previous_mirror_relative.startswith(prefix):
                raise BundleError("watch_mirror_ref", "stored watch mirror reference is invalid")
            old_mirror_path = mirror_path(previous_mirror_relative[len(prefix):])
            if old_mirror_path.exists():
                if old_mirror_path.is_symlink() or not old_mirror_path.is_file():
                    raise BundleError("watch_mirror_ref", "stored watch mirror is not a regular file")
                old_mirror_backup = work_dir / "previous-mirror"
                shutil.copyfile(old_mirror_path, old_mirror_backup)
                old_mirror_backup.chmod(0o600)

        if mirror_destination is not None:
            if mirror_destination.exists():
                if mirror_destination.is_symlink() or not mirror_destination.is_file():
                    raise BundleError("watch_mirror_collision", "watch mirror destination is unsafe")
                if old_mirror_path != mirror_destination and sha256_file(mirror_destination) != manifest["current_sha256"]:
                    raise BundleError("watch_mirror_collision", "watch mirror destination already contains different content")
                mirror_backup = work_dir / "destination-mirror"
                shutil.copyfile(mirror_destination, mirror_backup)
                mirror_backup.chmod(0o600)
            copy_atomic(mirror_source, mirror_destination)
            mirror_published = True

        if old_mirror_path is not None and old_mirror_path != mirror_destination and old_mirror_path.exists():
            # APFS is normally case-insensitive.  A case-only destination may
            # already be the same physical directory entry after copy_atomic;
            # unlinking the textual "old" path would then delete the newly
            # published mirror.  On a case-sensitive volume samefile is false,
            # so the obsolete spelling is still removed.
            if mirror_destination is None or not same_existing_file(old_mirror_path, mirror_destination):
                old_mirror_path.unlink()
                old_mirror_removed = True
                remove_empty_mirror_parents(old_mirror_path)

        if not metadata_only:
            review_path = watch_review_path(entry_id)
            review_path.parent.mkdir(parents=True, exist_ok=True)
            note = create_watch_review_note(
                manifest, bundle_sha, storage_class or "tombstone", version_number,
                version_ref, desired_mirror_relative, quarantine_ref,
            )
            if review_path.exists():
                if review_path.is_symlink() or not review_path.is_file():
                    raise BundleError("watch_review_note", "watch review note path is unsafe")
                existing_note = review_path.read_text(encoding="utf-8")
                expected_marker = f"memory_id: BUSINESS-WIN-{entry_id}"
                if expected_marker not in existing_note or "source_system: windows_folder_watch" not in existing_note:
                    raise BundleError("watch_review_note", "watch review note conflicts with another document")
                review_backup = work_dir / "previous-review.md"
                review_backup.write_text(existing_note, encoding="utf-8")
                review_backup.chmod(0o600)
            note_tmp = work_dir / "review.md"
            note_tmp.write_text(note, encoding="utf-8")
            note_tmp.chmod(0o600)
            copy_atomic(note_tmp, review_path)
            review_published = True

        processed_at = now_iso()
        source_uuid = watch_source_uuid(source_id)
        with conn:
            conn.execute(
                "INSERT INTO watch_sources(source_id,source_uuid,source_system,first_seen_at,last_event_at,last_event_id,active_entries) "
                "VALUES(?,?,?,?,?,?,0) ON CONFLICT(source_id) DO UPDATE SET "
                "last_event_at=excluded.last_event_at,last_event_id=excluded.last_event_id",
                (source_id, source_uuid, "windows_folder_watch", processed_at, processed_at, manifest["event_id"]),
            )
            if operation in {"baseline", "added"}:
                conn.execute(
                    "INSERT INTO watch_entries(source_id,source_entry_id,current_relative_path,path_key,current_sha256,"
                    "source_mtime_ns,source_modified_at,size_bytes,version_number,storage_class,version_ref,mirror_relative,"
                    "active,deleted_at,last_event_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?)",
                    (
                        source_id, entry_id, manifest["source_relative_path"], watch_path_key(manifest["source_relative_path"]),
                        manifest["current_sha256"], manifest["source_mtime_ns"], manifest["source_modified_at"],
                        manifest["source_size_bytes"], version_number, storage_class, version_ref,
                        desired_mirror_relative, manifest["event_id"], processed_at,
                    ),
                )
            elif operation == "modified":
                if extracted is not None:
                    conn.execute(
                        "UPDATE watch_entries SET current_sha256=?,source_mtime_ns=?,source_modified_at=?,size_bytes=?,"
                        "version_number=?,storage_class=?,version_ref=?,mirror_relative=?,last_event_id=?,updated_at=? "
                        "WHERE source_id=? AND source_entry_id=? AND active=1",
                        (
                            manifest["current_sha256"], manifest["source_mtime_ns"], manifest["source_modified_at"],
                            manifest["source_size_bytes"], version_number, storage_class, version_ref,
                            desired_mirror_relative, manifest["event_id"], processed_at, source_id, entry_id,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE watch_entries SET source_mtime_ns=?,source_modified_at=?,size_bytes=?,last_event_id=?,updated_at=? "
                        "WHERE source_id=? AND source_entry_id=? AND active=1",
                        (
                            manifest["source_mtime_ns"], manifest["source_modified_at"], manifest["source_size_bytes"],
                            manifest["event_id"], processed_at, source_id, entry_id,
                        ),
                    )
            elif operation == "renamed":
                conn.execute(
                    "UPDATE watch_entries SET current_relative_path=?,path_key=?,source_mtime_ns=?,source_modified_at=?,"
                    "storage_class=?,mirror_relative=?,last_event_id=?,updated_at=? "
                    "WHERE source_id=? AND source_entry_id=? AND active=1",
                    (
                        manifest["source_relative_path"], watch_path_key(manifest["source_relative_path"]),
                        manifest["source_mtime_ns"], manifest["source_modified_at"], storage_class, desired_mirror_relative,
                        manifest["event_id"], processed_at, source_id, entry_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE watch_entries SET current_sha256=NULL,mirror_relative=NULL,active=0,deleted_at=?,"
                    "last_event_id=?,updated_at=? WHERE source_id=? AND source_entry_id=? AND active=1",
                    (processed_at, manifest["event_id"], processed_at, source_id, entry_id),
                )
            if new_content_version:
                conn.execute(
                    "INSERT INTO watch_versions(source_id,source_entry_id,version_number,event_id,sha256,size_bytes,"
                    "source_relative_path,source_modified_at,storage_class,destination_ref,stored_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source_id, entry_id, version_number, manifest["event_id"], manifest["current_sha256"],
                        manifest["source_size_bytes"], manifest["source_relative_path"],
                        manifest["source_modified_at"], storage_class, version_ref, processed_at,
                    ),
                )
            if metadata_only:
                status = "metadata_only"
            elif capture_failure:
                status = f"capture_error:{manifest['capture_error']}"
            elif capture_recovered:
                status = "capture_recovered"
            else:
                status = "tombstoned" if operation == "deleted" else ("renamed" if operation == "renamed" else ("quarantined" if storage_class == "quarantine" else "stored"))
            conn.execute(
                "INSERT INTO source_events(event_id,bundle_sha256,source_system,source_id,source_entry_id,operation,"
                "source_relative_path,previous_source_relative_path,previous_sha256,current_sha256,source_modified_at,"
                "items_json,previous_event_id,transition_sequence,processed_at,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    manifest["event_id"], bundle_sha, manifest["source_system"], source_id, entry_id, operation,
                    manifest["source_relative_path"], manifest.get("previous_source_relative_path"),
                    manifest.get("previous_sha256"), manifest.get("current_sha256"), manifest["source_modified_at"],
                    json.dumps(
                        {
                            "items": manifest["items"], "capture_error": manifest.get("capture_error"),
                            "source_size_bytes": manifest["source_size_bytes"],
                        },
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    ),
                    manifest["previous_event_id"], manifest["transition_sequence"], processed_at, status,
                ),
            )
            conn.execute(
                "UPDATE watch_sources SET active_entries=(SELECT COUNT(*) FROM watch_entries "
                "WHERE source_id=? AND active=1) WHERE source_id=?",
                (source_id, source_id),
            )
            bump_maintenance_generation(conn, processed_at)
        database_committed = True

        destination = unique_destination(PROCESSED_BUNDLES, bundle_path.name)
        os.replace(bundle_path, destination)
        append_log(
            "watch_event_stored", event_id=manifest["event_id"], source_id=source_id,
            source_entry_id=entry_id, operation=operation, storage_class=storage_class,
        )
        write_health("running", "last watch event stored", last_event_id=manifest["event_id"])
    except RuntimeError as exc:
        # An unsafe local inbox invalidates the trust boundary itself.  Do not
        # read, move, or quarantine anything from it until ownership/mode are
        # repaired by the owner-controlled startup path.
        append_log("watch_inbox_trust_failed", error=type(exc).__name__)
        write_health("degraded", "watch_inbox_trust_failed")
    except BundleError as exc:
        if bundle_path.exists():
            quarantine_bundle(bundle_path, exc.code, exc.message)
        else:
            append_log("watch_bundle_error_after_archive", code=exc.code, message=exc.message)
        write_health("degraded", exc.code)
    except Exception as exc:
        code = "watch_receiver_error"
        message = f"{type(exc).__name__}: {exc}"[:500]
        if bundle_path.exists():
            quarantine_bundle(bundle_path, code, message)
        else:
            append_log("watch_receiver_error_after_archive", code=code, message=message)
        write_health("degraded", code)
    finally:
        if not database_committed:
            if review_published and review_path and review_path.exists():
                if review_backup and review_backup.exists():
                    copy_atomic(review_backup, review_path)
                else:
                    review_path.unlink()
            if mirror_published and mirror_destination and mirror_destination.exists():
                if mirror_backup and mirror_backup.exists():
                    copy_atomic(mirror_backup, mirror_destination)
                else:
                    mirror_destination.unlink()
                    remove_empty_mirror_parents(mirror_destination)
            if old_mirror_removed and old_mirror_path and old_mirror_backup and old_mirror_backup.exists():
                copy_atomic(old_mirror_backup, old_mirror_path)
            if quarantine_reason_created and quarantine_reason_created.exists():
                quarantine_reason_created.unlink()
                remove_empty_parents(quarantine_reason_created, WATCH_ITEM_QUARANTINE_ROOT)
            if quarantine_created and quarantine_created.exists():
                quarantine_created.unlink()
                remove_empty_parents(quarantine_created, WATCH_ITEM_QUARANTINE_ROOT)
            if version_created and version_created.exists():
                version_created.unlink()
                remove_empty_parents(version_created, WATCH_VERSIONS_ROOT)
        if work_dir and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def process_bundle(bundle_path, conn):
    bundle_sha = None
    manifest = None
    work_dir = None
    staging_dirs = {}
    published_dirs = []
    review_path = None
    review_tmp = None
    review_created = False
    database_committed = False
    try:
        validate_bundle_preflight(bundle_path)
        bundle_sha = sha256_file(bundle_path)
        manifest = inspect_bundle(bundle_path)
        event_id = manifest["event_id"]
        existing_event = conn.execute(
            "SELECT bundle_sha256,status,destination_relative,obsidian_count,external_count,quarantine_count "
            "FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing_event:
            if existing_event[0] != bundle_sha:
                raise BundleError("event_id_collision", "event id was already stored with different bundle content")
            existing_counts = {
                "obsidian": existing_event[3],
                "external": existing_event[4],
                "quarantine": existing_event[5],
            }
            queue_ack(
                event_id, manifest["return_node"], bundle_sha, "duplicate_event",
                existing_event[2], "already_processed", existing_counts
            )
            destination = unique_destination(PROCESSED_BUNDLES, bundle_path.name)
            os.replace(bundle_path, destination)
            append_log("duplicate_event", event_id=event_id, bundle=destination.name)
            return

        work_dir = PROCESSING / f"{event_id}.tmp-{uuid.uuid4().hex[:8]}"
        work_dir.mkdir(parents=True, exist_ok=False)
        extracted = extract_and_verify(bundle_path, manifest, work_dir)

        try:
            created_date = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00")).astimezone()
        except (AttributeError, TypeError, ValueError):
            raise BundleError("created_at", "invalid created_at timestamp")
        year = created_date.strftime("%Y")
        day = created_date.strftime("%Y-%m-%d")
        date_dir = MEMORY_ROOT / "91_归档" / created_date.strftime("%Y") / created_date.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        final_dirs = {
            "obsidian": date_dir / event_id,
            "external": EXTERNAL_ROOT / year / day / event_id,
            "quarantine": ITEM_QUARANTINE_ROOT / year / day / event_id,
        }
        temp_suffix = uuid.uuid4().hex[:8]

        def staging_dir(storage_class):
            if storage_class in staging_dirs:
                return staging_dirs[storage_class]
            final = final_dirs[storage_class]
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                raise BundleError("destination_exists", f"{storage_class} event destination already exists without database record")
            staging = final.parent / f".{event_id}.tmp-{temp_suffix}"
            staging.mkdir(parents=True, exist_ok=False)
            staging_dirs[storage_class] = staging
            return staging

        staging_dir("obsidian")
        item_results = []
        counts = {"obsidian": 0, "external": 0, "quarantine": 0, "waiting": 0}
        pending_file_rows = []
        pending_destinations = {}
        for item in manifest["items"]:
            rel = item["relative_path"]
            name = PurePosixPath(rel).name
            digest = item["sha256"].lower()
            if rel not in extracted and video_hold.classify(rel):
                counts['waiting'] += 1
                item_results.append({'name':name,'sha256':digest,'storage_class':'waiting',
                    'destination_ref':'保留于原传输包','disposition':'waiting_instruction','code':'video_waiting_instruction'})
                continue
            decision = classify_payload(extracted[rel], rel)
            storage_class = decision["storage_class"]
            counts[storage_class] += 1
            existing_file = conn.execute(
                "SELECT destination_ref FROM files WHERE sha256 = ? AND storage_class = ?",
                (digest, storage_class),
            ).fetchone()
            if existing_file:
                item_results.append({
                    "name": name,
                    "sha256": digest,
                    "storage_class": storage_class,
                    "destination_ref": existing_file[0],
                    "disposition": "duplicate",
                    "code": decision["code"],
                })
                continue
            pending_key = (digest, storage_class)
            if pending_key in pending_destinations:
                item_results.append({
                    "name": name,
                    "sha256": digest,
                    "storage_class": storage_class,
                    "destination_ref": pending_destinations[pending_key],
                    "disposition": "duplicate",
                    "code": decision["code"],
                })
                continue

            staging = staging_dir(storage_class)
            payload_dir = staging / "资料" if storage_class == "obsidian" else staging
            payload_dir.mkdir(parents=True, exist_ok=True)
            destination = unique_destination(payload_dir, name)
            shutil.copyfile(extracted[rel], destination)
            destination.chmod(0o600)
            final_base = final_dirs[storage_class] / "资料" if storage_class == "obsidian" else final_dirs[storage_class]
            final_file = final_base / destination.name
            storage_root = MEMORY_ROOT if storage_class == "obsidian" else NON_OBSIDIAN_ROOT
            relative = PurePosixPath(final_file.relative_to(storage_root)).as_posix()
            reference = destination_ref(storage_class, relative)
            legacy_relative = relative
            pending_file_rows.append(
                (digest, item["size_bytes"], legacy_relative, event_id, now_iso(), storage_class, reference)
            )
            pending_destinations[pending_key] = reference
            item_results.append({
                "name": destination.name,
                "sha256": digest,
                "storage_class": storage_class,
                "destination_ref": reference,
                "disposition": "stored",
                "code": decision["code"],
            })
            if storage_class == "quarantine":
                reason_path = destination.with_suffix(destination.suffix + ".reason.json")
                write_reason_json_atomic(
                    reason_path,
                    {"event_id": event_id, "sha256": digest, "code": decision["code"]},
                )

        final_dir = final_dirs["obsidian"]
        destination_relative = PurePosixPath(final_dir.relative_to(MEMORY_ROOT)).as_posix()
        event_receipt = create_event_receipt(
            manifest, bundle_sha, item_results, destination_relative, counts
        )
        receipt_path = staging_dirs["obsidian"] / "接收清单.md"
        receipt_path.write_text(event_receipt, encoding="utf-8")
        receipt_path.chmod(0o600)

        for storage_class in ("external", "quarantine", "obsidian"):
            staging = staging_dirs.get(storage_class)
            if staging:
                os.replace(staging, final_dirs[storage_class])
                published_dirs.append(final_dirs[storage_class])

        review_path = ROUTING_EXCEPTION_ROOT / f"{event_id}.json"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        if review_path.exists() or review_path.is_symlink():
            raise BundleError("routing_exception_exists", "routing exception already exists without database record")
        review_created = write_reason_json_atomic(
            review_path,
            {
                "schema_version": 1,
                "status": "system_exception",
                "code": "quick_capture_unroutable",
                "event_id": event_id,
                "bundle_sha256": bundle_sha,
                "source_device": manifest["source_device"],
                "observed_at": manifest["created_at"],
                "source_archive": destination_relative,
                "counts": counts,
            },
        )

        event_status = "stored_with_quarantine" if counts["quarantine"] else "stored_with_video_wait" if counts.get("waiting") else "stored"
        if counts["quarantine"] == len(manifest["items"]):
            event_status = "quarantined"
        with conn:
            conn.execute(
                "INSERT INTO events(event_id,bundle_sha256,source_device,created_at,processed_at,status,destination_relative,"
                "obsidian_count,external_count,quarantine_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, bundle_sha, manifest["source_device"], manifest["created_at"], now_iso(),
                    event_status, destination_relative, counts["obsidian"], counts["external"], counts["quarantine"],
                ),
            )
            conn.executemany(
                "INSERT INTO files(sha256,size_bytes,destination_relative,first_event_id,stored_at,storage_class,destination_ref) "
                "VALUES(?,?,?,?,?,?,?)",
                pending_file_rows,
            )
        database_committed = True

        if counts["quarantine"] == len(manifest["items"]):
            ack_status, ack_code = "rejected", "all_items_quarantined"
        elif counts["quarantine"]:
            ack_status, ack_code = "stored", "stored_with_quarantine"
        elif counts.get("waiting"):
            ack_status, ack_code = "stored", "video_waiting_instruction"
        else:
            ack_status, ack_code = "stored", "hash_verified"
        queue_ack(
            event_id, manifest["return_node"], bundle_sha, ack_status,
            destination_relative, ack_code, counts
        )
        archived_bundle = unique_destination(PROCESSED_BUNDLES, bundle_path.name)
        bundle_path.chmod(0o600)
        os.replace(bundle_path, archived_bundle)
        append_log(
            "bundle_stored",
            event_id=event_id,
            stored_count=sum(1 for item in item_results if item["disposition"] == "stored"),
            duplicate_count=sum(1 for item in item_results if item["disposition"] == "duplicate"),
            obsidian_count=counts["obsidian"],
            external_count=counts["external"],
            quarantine_count=counts["quarantine"],
            destination=destination_relative,
        )
        if counts["quarantine"]:
            if counts["quarantine"] == len(manifest["items"]):
                notify_failure(f"收到的 {counts['quarantine']} 项资料均已隔离；快速收集未自动确定业务归属。")
            else:
                notify_failure(f"收到的资料中有 {counts['quarantine']} 项被隔离；快速收集未自动确定业务归属。")
            write_health("degraded", "items_quarantined", last_event_id=event_id)
        else:
            notify_failure("Win11 快速收集已安全归档，但无法自动确定业务归属；未写入业务页面。")
            write_health("running", "last bundle stored", last_event_id=event_id)
    except BundleError as exc:
        receipt_manifest = manifest or exc.manifest
        if bundle_sha and receipt_manifest and receipt_manifest.get("return_node") in ALLOWED_RETURN_NODES and receipt_manifest.get("event_id"):
            rejected_counts = {
                "obsidian": 0,
                "external": 0,
                "quarantine": len(receipt_manifest.get("items", [])),
            }
            queue_ack(
                receipt_manifest["event_id"], receipt_manifest["return_node"], bundle_sha,
                "rejected", None, exc.code, rejected_counts
            )
        if bundle_path.exists():
            quarantine_bundle(bundle_path, exc.code, exc.message)
        else:
            append_log("bundle_error_after_archive", code=exc.code, message=exc.message)
        write_health("degraded", exc.code)
    except Exception as exc:
        code = "receiver_error"
        message = f"{type(exc).__name__}: {exc}"[:500]
        if bundle_path.exists():
            quarantine_bundle(bundle_path, code, message)
        else:
            append_log("receiver_error_after_archive", code=code, message=message)
        write_health("degraded", code)
    finally:
        if not database_committed:
            if review_created and review_path and review_path.exists():
                review_path.unlink()
            for path in reversed(published_dirs):
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
        for path in staging_dirs.values():
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        if review_tmp and review_tmp.exists():
            review_tmp.unlink()
        if work_dir and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def signal_handler(_signum, _frame):
    global RUNNING
    RUNNING = False


def start_taildrop_getter(log_handle):
    return subprocess.Popen(
        [TAILSCALE, "file", "get", "--loop", "--wait", "--conflict=rename", str(INBOX)],
        stdout=log_handle,
        stderr=log_handle,
        text=True,
    )


def drain_incoming(conn):
    """Publish each received batch and its index as one shared-lock interval."""
    found = False
    with acquire_memory_write_lock():
        for bundle in sorted(INBOX.iterdir()):
            if bundle.is_file():
                found = True
                if bundle.name.lower().startswith("business-memory-") and bundle.suffix.lower() == ".zip":
                    process_bundle(bundle, conn)
                else:
                    route_unrelated_file(bundle)
        validate_watch_inbox_security()
        for watch_bundle in sorted(WATCH_INBOX.iterdir()):
            if not watch_bundle.is_file():
                continue
            if watch_bundle.name.startswith(".business-watch-") and ".tmp-" in watch_bundle.name:
                continue
            found = True
            if WATCH_BUNDLE_RE.fullmatch(watch_bundle.name):
                process_watch_bundle(watch_bundle, conn)
            else:
                quarantine_bundle(watch_bundle, "watch_inbox_unexpected",
                                  "local watch inbox accepts only schema v2 watch bundles")
        # Another semantic writer must never acquire the lock in the interval
        # between a new dirty generation and its matching applied index.
        refresh_dirty_indexes(conn, force=found)
        retry_acks(conn)
    return found


def main():
    ensure_dirs()
    validate_watch_inbox_security()
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    lock_handle = acquire_receiver_lock()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    with acquire_memory_write_lock():
        conn = connect_db()
    getter = None
    restart_count = 0
    last_content_observation = 0
    log_handle = (ROOT / "logs" / "taildrop.log").open("a", encoding="utf-8")
    write_health("running", "receiver started")
    append_log("receiver_started")
    try:
        while RUNNING and not __import__("owner_retirement").stopped():
            if getter is None or getter.poll() is not None:
                if getter is not None:
                    restart_count += 1
                    append_log("taildrop_getter_exited", returncode=getter.returncode, restart_count=restart_count)
                    if restart_count == 3:
                        notify_failure("Mac 的 Tailscale 接收进程连续退出，正在自动重启。")
                    time.sleep(min(30, 2 ** min(restart_count, 4)))
                getter = start_taildrop_getter(log_handle)
            found = drain_incoming(conn)
            if found:
                restart_count = 0
            if time.monotonic()-last_content_observation >= 300:
                try:
                    association=content_observer.observe(json.loads(CONFIG_PATH.read_text()))
                    append_log('content_observation_cycle',**association)
                except Exception as exc:
                    append_log('content_observation_unavailable',error=type(exc).__name__)
                last_content_observation=time.monotonic()
            time.sleep(10)
    finally:
        if getter is not None and getter.poll() is None:
            getter.terminate()
            try:
                getter.wait(timeout=10)
            except subprocess.TimeoutExpired:
                getter.kill()
        log_handle.close()
        conn.close()
        lock_handle.close()
        write_health("stopped", "receiver stopped")
        append_log("receiver_stopped")


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    if os.environ.pop("BUSINESS_DINGTALK_MEMORY_HOURLY", "") == "1":
        os.execv(
            "/usr/bin/python3",
            [
                "/usr/bin/python3",
                str(ROOT / "bin" / "dingtalk_memory_automation.py"),
                "--config",
                str(CONFIG_PATH),
                "hourly",
            ],
        )
    try:
        main()
    except Exception as exc:
        ensure_dirs()
        append_log("fatal", error=f"{type(exc).__name__}: {exc}"[:500])
        write_health("failed", "receiver failed to start")
        notify_failure("Mac 示例企业记忆接收端启动失败。")
        raise
