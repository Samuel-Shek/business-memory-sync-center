#!/usr/bin/env python3

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import importlib.util
import io
import json
import multiprocessing
import os
import re
import sqlite3
import stat
import struct
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

RECEIVER_PATH = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/bin/receiver.py'))
WATCHER_PATH = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/bin/folder_watcher.py'))


def load_receiver():
    spec = importlib.util.spec_from_file_location("business_receiver", RECEIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_watcher():
    spec = importlib.util.spec_from_file_location("business_folder_watcher", WATCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_zip(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def make_ooxml(family):
    main = {
        "word": "word/document.xml",
        "xl": "xl/workbook.xml",
        "ppt": "ppt/presentation.xml",
    }[family]
    return make_zip({
        "[Content_Types].xml": b"<Types/>",
        "_rels/.rels": b"<Relationships/>",
        main: b"<root/>",
    })


def mark_zip_encrypted(content):
    marked = bytearray(content)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while True:
            index = marked.find(signature, start)
            if index < 0:
                break
            current = struct.unpack_from("<H", marked, index + flag_offset)[0]
            struct.pack_into("<H", marked, index + flag_offset, current | 0x1)
            start = index + 4
    return bytes(marked)


def mark_zip_member_encrypted(content, member_name):
    marked = bytearray(content)
    target = member_name.encode("utf-8")
    layouts = (
        (b"PK\x03\x04", 6, 26, 30),
        (b"PK\x01\x02", 8, 28, 46),
    )
    found = 0
    for signature, flag_offset, name_length_offset, name_offset in layouts:
        start = 0
        while True:
            index = marked.find(signature, start)
            if index < 0:
                break
            name_length = struct.unpack_from("<H", marked, index + name_length_offset)[0]
            name = bytes(marked[index + name_offset:index + name_offset + name_length])
            if name == target:
                current = struct.unpack_from("<H", marked, index + flag_offset)[0]
                struct.pack_into("<H", marked, index + flag_offset, current | 0x1)
                found += 1
            start = index + 4
    if found != 2:
        raise AssertionError(f"could not mark both ZIP records for {member_name}")
    return bytes(marked)


def raster_samples():
    webp_body = b"WEBPVP8 "
    webp = b"RIFF" + struct.pack("<I", len(webp_body)) + webp_body
    bmp = b"BM" + struct.pack("<I", 18) + b"\x00" * 12
    heic = struct.pack(">I", 24) + b"ftyp" + b"heic" + b"\x00\x00\x00\x00" + b"mif1" + b"heic"
    avif = struct.pack(">I", 24) + b"ftyp" + b"avif" + b"\x00\x00\x00\x00" + b"mif1" + b"avif"
    return {
        "image.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 25 + b"IEND\xaeB`\x82",
        "image.jpg": b"\xff\xd8\xff\xe0test\xff\xd9",
        "image.jpeg": b"\xff\xd8\xff\xe1test\xff\xd9",
        "image.gif": b"GIF89a" + b"\x00" * 20 + b";",
        "image.webp": webp,
        "image.bmp": bmp,
        "image.tif": b"II*\x00\x08\x00\x00\x00" + b"\x00" * 8,
        "image.tiff": b"MM\x00*\x00\x00\x00\x08" + b"\x00" * 8,
        "image.heic": heic,
        "image.heif": heic,
        "image.avif": avif,
    }


def make_bundle(base, event_id, items, archive_name=None, manifest_overrides=None):
    manifest_items = []
    for item in items:
        content = item["content"]
        relative_path = item.get("relative_path", f"payload/{item['name']}")
        manifest_items.append({
            "relative_path": relative_path,
            "size_bytes": item.get("declared_size", len(content)),
            "sha256": item.get("digest", hashlib.sha256(content).hexdigest()),
        })
    manifest = {
        "schema_version": 1,
        "event_id": event_id,
        "created_at": "2020-01-02T15:00:00+08:00",
        "source_device": "EXAMPLE_PC",
        "return_node": "EXAMPLE_PC",
        "capture_type": "files",
        "items": manifest_items,
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    bundle = base / (archive_name or f"business-memory-{event_id}.zip")
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        manifest_info = zipfile.ZipInfo("manifest.json", date_time=(2026, 8, 24, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(manifest_info, json.dumps(manifest))
        for item, manifest_item in zip(items, manifest_items):
            payload_info = zipfile.ZipInfo(manifest_item["relative_path"], date_time=(2026, 8, 24, 0, 0, 0))
            payload_info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(payload_info, item["content"])
    return bundle


def main():
    receiver = load_receiver()
    watcher = load_watcher()
    real_subprocess_run = subprocess.run
    tests_passed = 0

    def check(name, condition):
        nonlocal tests_passed
        if not condition:
            raise AssertionError(name)
        tests_passed += 1

    with tempfile.TemporaryDirectory(prefix="business-receiver-test-") as tmp_name:
        root = Path(tmp_name)
        receiver.video_hold.DATABASE = root / "video-holds.sqlite"
        receiver.ROOT = root
        receiver.INBOX = root / "inbox"
        receiver.WATCH_INBOX = root / "watch_inbox"
        receiver.PROCESSING = root / "processing"
        receiver.QUARANTINE = root / "quarantine"
        receiver.NON_OBSIDIAN_ROOT = root / "non_obsidian"
        receiver.ITEM_QUARANTINE_ROOT = receiver.NON_OBSIDIAN_ROOT / "quarantine" / "items"
        receiver.EXTERNAL_ROOT = receiver.NON_OBSIDIAN_ROOT / "external_payloads"
        receiver.WATCH_VERSIONS_ROOT = receiver.NON_OBSIDIAN_ROOT / "win11-folder-watch" / "versions"
        receiver.WATCH_NOTES_ROOT = receiver.NON_OBSIDIAN_ROOT / "win11-folder-watch" / "obsidian-notes-archive"
        receiver.WATCH_ITEM_QUARANTINE_ROOT = receiver.NON_OBSIDIAN_ROOT / "quarantine" / "win11-folder-watch"
        receiver.ROUTING_EXCEPTION_ROOT = receiver.NON_OBSIDIAN_ROOT / "business-routing" / "quarantine" / "quick-capture"
        receiver.ACK_OUTBOX = root / "ack_outbox"
        receiver.ACK_SENT = root / "state" / "ack_sent"
        receiver.PROCESSED_BUNDLES = root / "state" / "processed_bundles"
        receiver.LOG_PATH = root / "logs" / "receiver.jsonl"
        receiver.HEALTH_PATH = root / "state" / "health.json"
        receiver.DB_PATH = root / "state" / "bridge.sqlite"
        receiver.RECEIVER_LOCK_PATH = root / "state" / "receiver.lock"
        receiver.MEMORY_WRITE_LOCK_PATH = root / "state" / "memory-index.lock"
        receiver.REFRESH_INDEXES_SCRIPT = root / "bin" / "refresh_memory_indexes.rb"
        receiver.MEMORY_ROOT = root / "memory"
        receiver.ALLOWED_WATCH_SOURCES = {"source_a", "source_b", "source_no_mirror", "planning_source"}
        receiver.WATCH_SOURCE_SETTINGS = {"source_no_mirror": {"mirror_policy": "none"}}

        sandbox_root = root / "sandbox-source"
        sandbox_root.mkdir()
        sandbox_real_root = sandbox_root.resolve()
        sandbox_profile = watcher.source_readonly_profile([{"root": str(sandbox_real_root)}])
        check(
            "watcher sandbox profile scopes source write denial",
            "(allow default)" in sandbox_profile
            and f'(deny file-write* (subpath "{sandbox_real_root}"))' in sandbox_profile,
        )
        sandbox_read = real_subprocess_run(
            [str(watcher.SANDBOX_EXEC), "-p", sandbox_profile, "/bin/ls", str(sandbox_real_root)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        sandbox_write = real_subprocess_run(
            [str(watcher.SANDBOX_EXEC), "-p", sandbox_profile, "/bin/mkdir", str(sandbox_real_root / "blocked")],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        check(
            "watcher macOS sandbox allows reads and denies source writes",
            sandbox_read.returncode == 0
            and sandbox_write.returncode != 0
            and not (sandbox_root / "blocked").exists(),
        )

        receiver.notify_failure = lambda _message: None
        receiver.subprocess.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("payload processing invoked subprocess.run"))
        receiver.subprocess.Popen = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("payload processing invoked subprocess.Popen"))
        receiver.ensure_dirs()
        receiver.MEMORY_ROOT.mkdir(parents=True)

        watcher.ROOT = root
        watcher.WATCH_INBOX = receiver.WATCH_INBOX
        watcher.DB_PATH = receiver.DB_PATH
        watcher.STATE_ROOT = root / "state" / "folder_watcher"
        watcher.LOG_PATH = root / "logs" / "folder_watcher.jsonl"
        watcher.HEALTH_PATH = root / "state" / "folder_watcher_health.json"
        watcher.LOCK_PATH = root / "state" / "folder_watcher.lock"
        check(
            "watcher overdue deadline clamps sleep to zero",
            watcher.bounded_sleep_seconds(10.0, 10.000001) == 0.0
            and watcher.bounded_sleep_seconds(10.0, 9.5) == 0.5,
        )
        watcher.initialize_health_state()
        check(
            "watcher health separates process and scan state",
            watcher.HEALTH_STATE["process_started_at"]
            and "process_starts_total" in watcher.HEALTH_STATE
            and "restart_count" in watcher.HEALTH_STATE
            and "fatal_errors_total" in watcher.HEALTH_STATE
            and "consecutive_scan_timeouts" in watcher.HEALTH_STATE
            and "last_successful_quick_scan_at" in watcher.HEALTH_STATE
            and "last_successful_full_scan_at" in watcher.HEALTH_STATE,
        )

        legacy = sqlite3.connect(receiver.DB_PATH)
        legacy.execute(
            "CREATE TABLE events(event_id TEXT PRIMARY KEY,bundle_sha256 TEXT NOT NULL,source_device TEXT NOT NULL,"
            "created_at TEXT NOT NULL,processed_at TEXT NOT NULL,status TEXT NOT NULL,destination_relative TEXT)"
        )
        legacy.execute(
            "CREATE TABLE files(sha256 TEXT PRIMARY KEY,size_bytes INTEGER NOT NULL,destination_relative TEXT NOT NULL,"
            "first_event_id TEXT NOT NULL,stored_at TEXT NOT NULL)"
        )
        legacy.commit()
        legacy.close()
        conn = receiver.connect_db()

        file_columns = receiver.table_columns(conn, "files")
        event_columns = receiver.table_columns(conn, "events")
        primary_key = [row[1] for row in sorted(file_columns.values(), key=lambda value: value[5]) if row[5]]
        check("schema storage_class", "storage_class" in file_columns)
        check("schema destination_ref", "destination_ref" in file_columns)
        check("schema composite primary key", primary_key == ["sha256", "storage_class"])
        check("schema event counts", {"obsidian_count", "external_count", "quarantine_count"}.issubset(event_columns))
        check("schema version", conn.execute("PRAGMA user_version").fetchone()[0] == 3)
        new_tables = {
            "watch_sources", "watch_entries", "watch_versions", "source_events", "maintenance_state",
        }
        actual_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check("watch schema tables", new_tables.issubset(actual_tables))
        source_event_columns = receiver.table_columns(conn, "source_events")
        check("watch event predecessor schema", {"previous_event_id", "transition_sequence"}.issubset(source_event_columns))
        check("database WAL enabled", conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
        check("database busy timeout", conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000)
        receiver.connect_db().close()
        check("migration idempotent", conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0)

        classifier_dir = root / "classifier"
        classifier_dir.mkdir()
        obsidian_samples = {
            "note.md": b"# Note\n",
            "note.txt": b"plain text\n",
            "table.csv": b"a,b\n1,2\n",
            "table.tsv": b"a\tb\n1\t2\n",
            "static.html": b"<!doctype html><html><body><p>Safe</p></body></html>",
            "static.htm": b"<html><body><a href='https://example.com'>Link</a></body></html>",
            "report.pdf": b"%PDF-1.4\n1 0 obj <<>> endobj\n%%EOF\n",
            "report.docx": make_ooxml("word"),
            "report.xlsx": make_ooxml("xl"),
            "report.pptx": make_ooxml("ppt"),
            **raster_samples(),
        }
        for name, content in obsidian_samples.items():
            path = classifier_dir / name
            path.write_bytes(content)
            check(f"obsidian route {name}", receiver.classify_payload(path, f"payload/{name}")["storage_class"] == "obsidian")

        nested_archive = make_zip({"../escaped.txt": b"must never be unpacked", "run.sh": b"touch should-not-exist"})
        iges = bytearray(b" " * 160)
        iges[72] = ord("S")
        iso = bytearray(b"\x00" * 32774)
        iso[32769:32774] = b"CD001"
        external_samples = {
            "script.py": b"print('safe at rest')\n",
            "script.js": b"console.log('safe at rest')\n",
            "script.ps1": b"Write-Output safe\n",
            "archive.zip": nested_archive,
            "config.json": b'{"build": true}\n',
            "module.wasm": b"\x00asm\x01\x00\x00\x00",
            "program.exe": b"MZ" + b"\x00" * 62,
            "macro.docm": make_zip({"word/vbaProject.bin": b"macro"}),
            "records.sqlite": b"SQLite format 3\x00" + b"\x00" * 84,
            "analytics.parquet": b"PAR1payloadPAR1",
            "legacy.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
            "audio.wav": b"RIFF" + b"\x00\x00\x00\x00" + b"WAVEfmt ",
            "video.mp4": b"\x00\x00\x00\x18ftypisom" + b"\x00" * 12,
            "message.eml": b"From: fixture_34657fd6@example.invalid\r\nSubject: test\r\n\r\nBody",
            "mailbox.pst": b"!BDN" + b"\x00" * 60,
            "drawing.dwg": b"AC1032" + b"\x00" * 58,
            "model.step": b"ISO-10303-21;\nHEADER;\nENDSEC;\n",
            "exchange.iges": bytes(iges),
            "design.psd": b"8BPS\x00\x01" + b"\x00" * 58,
            "layout.sketch": make_zip({"document.json": b"{}"}),
            "disk.iso": bytes(iso),
        }
        for name, content in external_samples.items():
            path = classifier_dir / name
            path.write_bytes(content)
            check(f"external route {name}", receiver.classify_payload(path, f"payload/{name}")["storage_class"] == "external")

        quarantine_samples = {
            "script-tag.html": b"<html><script>alert(1)</script></html>",
            "handler.htm": b"<img src='x' OnErRoR = 'alert(1)'>",
            "iframe.html": b"<iframe src='x'></iframe>",
            "refresh.html": b"<meta http-equiv='refresh' content='0;url=x'>",
            "active-url.html": b"<a href='java&#x73;cript:alert(1)'>x</a>",
            "template.html": b"<?php echo 1 ?>",
            "duplicate-refresh.html": b"<meta http-equiv='refresh' http-equiv='x' content='0;url=x'>",
            "escaped-import.html": br"<style>@\69mport url('https://example.invalid/x.css');</style>",
            "escaped-import-crlf.html": b"<style>@\\69\r\nmport url('https://example.invalid/x.css');</style>",
            "svg-data.html": b"<img src='data:image/svg+xml,<svg onload=alert(1)>'>",
            "xhtml-data.html": b"<iframe src='data:application/xhtml+xml,<html></html>'></iframe>",
            "unknown.bin": b"unknown",
            "README": b"unknown without an extension",
            "broken.pdf": b"%PDF-1.4\nno eof",
            "active-escaped.pdf": b"%PDF-1.4\n/Open#41ction << /Java#53cript true /J#53 true /AA true >>\n%%EOF\n",
            "fake.docx": b"not a zip",
            "encrypted.docx": mark_zip_encrypted(make_ooxml("word")),
            "encrypted.zip": mark_zip_encrypted(make_zip({"secret.txt": b"flagged, not decrypted"})),
            "encrypted.sketch": mark_zip_encrypted(make_zip({"document.json": b"{}"})),
            "broken.png": b"\x89PNG\r\n\x1a\ntruncated",
            "broken.wav": b"not a wave file",
            "binary.txt": b"abc\x00def",
        }
        for name, content in quarantine_samples.items():
            path = classifier_dir / name
            path.write_bytes(content)
            check(f"quarantine route {name}", receiver.classify_payload(path, f"payload/{name}")["storage_class"] == "quarantine")
        check(
            "encrypted OOXML flag identified",
            receiver.classify_payload(classifier_dir / "encrypted.docx", "payload/encrypted.docx")["code"] == "ooxml_encrypted",
        )
        check(
            "encrypted ZIP flag identified",
            receiver.classify_payload(classifier_dir / "encrypted.zip", "payload/encrypted.zip")["code"] == "archive_encrypted",
        )
        check(
            "encrypted ZIP-backed design flag identified",
            receiver.classify_payload(classifier_dir / "encrypted.sketch", "payload/encrypted.sketch")["code"] == "archive_encrypted",
        )
        check(
            "archive names reject control characters",
            all(not receiver.safe_archive_name(f"payload/evil{character}row.md") for character in ("\r", "\n", "\t", "\x7f")),
        )

        event_id = str(uuid.uuid4())
        canary = root / "payload-executed-canary"
        mixed_items = [
            {"name": "note.md", "content": b"received knowledge\n"},
            {"name": "run.py", "content": f"from pathlib import Path\nPath({str(canary)!r}).touch()\n".encode()},
            {"name": "dynamic.html", "content": b"<button onclick='alert(1)'>x</button>"},
        ]
        mixed = make_bundle(receiver.INBOX, event_id, mixed_items)
        mixed_bundle_sha = hashlib.sha256(mixed.read_bytes()).hexdigest()
        receiver.process_bundle(mixed, conn)
        event_row = conn.execute(
            "SELECT status,obsidian_count,external_count,quarantine_count FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        check("mixed event stored", event_row == ("stored_with_quarantine", 1, 1, 1))
        check("obsidian payload placed", len(list(receiver.MEMORY_ROOT.rglob("note.md"))) == 1)
        check("external payload placed", len(list(receiver.EXTERNAL_ROOT.rglob("run.py"))) == 1)
        check("quarantine payload placed", len(list(receiver.ITEM_QUARANTINE_ROOT.rglob("dynamic.html"))) == 1)
        check("quarantine reason placed", len(list(receiver.ITEM_QUARANTINE_ROOT.rglob("dynamic.html.reason.json"))) == 1)
        check("payload not executed", not canary.exists())
        check("no wrong-root script", not list(receiver.MEMORY_ROOT.rglob("run.py")))
        check("no wrong-root dynamic html", not list(receiver.MEMORY_ROOT.rglob("dynamic.html")))

        ack_path = receiver.ACK_OUTBOX / f"business-ack-{event_id}.json"
        ack = json.loads(ack_path.read_text(encoding="utf-8"))
        old_ack_fields = {
            "schema_version", "event_id", "bundle_sha256", "status", "code", "stored_at",
            "destination_relative", "return_node", "receipt_scope",
        }
        check("ack old fields preserved", old_ack_fields.issubset(ack))
        check("ack three counts", (ack["obsidian_count"], ack["external_count"], ack["quarantine_count"]) == (1, 1, 1))
        check("ack old schema version", ack["schema_version"] == 1)
        check("ack has no absolute path", "/Users/" not in json.dumps(ack, ensure_ascii=False) and str(root) not in json.dumps(ack, ensure_ascii=False))

        receipt = next((receiver.MEMORY_ROOT / "91_归档").rglob(f"{event_id}/接收清单.md")).read_text(encoding="utf-8")
        review_note_path = receiver.ROUTING_EXCEPTION_ROOT / f"{event_id}.json"
        review_note = json.loads(review_note_path.read_text(encoding="utf-8"))
        check(
            "quick capture uses technical routing quarantine",
            review_note_path.parent == receiver.ROUTING_EXCEPTION_ROOT
            and not list((receiver.MEMORY_ROOT / "90_待确认").glob(f"*{event_id}.md")),
        )
        check("receipt has no absolute path", str(root) not in receipt and "/Users/" not in receipt)
        check("routing exception has no absolute path", str(root) not in json.dumps(review_note, ensure_ascii=False) and "/Users/" not in json.dumps(review_note, ensure_ascii=False))
        check("routing exception state", review_note["status"] == "system_exception" and review_note["code"] == "quick_capture_unroutable")
        check("routing exception event binding", review_note["event_id"] == event_id and review_note["bundle_sha256"] == mixed_bundle_sha)
        check("routing exception source archive", review_note["source_archive"].startswith("91_归档/"))

        refs = [row[0] for row in conn.execute("SELECT destination_ref FROM files WHERE first_event_id=?", (event_id,))]
        allowed_ref = re.compile(r"^(?:obsidian:91_归档|external:external_payloads|quarantine:quarantine/items)/")
        check("destination refs class-relative", all(allowed_ref.match(ref) for ref in refs))
        check("destination refs relative", all(str(root) not in ref and ".." not in ref.split(":", 1)[1].split("/") for ref in refs))
        stored_modes = [stat.S_IMODE(path.stat().st_mode) for path in (
            next(receiver.MEMORY_ROOT.rglob("note.md")),
            next(receiver.EXTERNAL_ROOT.rglob("run.py")),
            next(receiver.ITEM_QUARANTINE_ROOT.rglob("dynamic.html")),
        )]
        check("payload permissions", stored_modes == [0o600, 0o600, 0o600])

        duplicate = make_bundle(receiver.INBOX, event_id, mixed_items)
        receiver.process_bundle(duplicate, conn)
        duplicate_ack = json.loads(ack_path.read_text(encoding="utf-8"))
        check("duplicate event count stable", conn.execute("SELECT COUNT(*) FROM events WHERE event_id=?", (event_id,)).fetchone()[0] == 1)
        check("duplicate event ack status", duplicate_ack["status"] == "duplicate_event")
        check("duplicate event ack counts", tuple(duplicate_ack[key] for key in ("obsidian_count", "external_count", "quarantine_count")) == (1, 1, 1))

        collision = make_bundle(receiver.INBOX, event_id, [{"name": "different.txt", "content": b"different bundle content"}])
        collision_sha = hashlib.sha256(collision.read_bytes()).hexdigest()
        receiver.process_bundle(collision, conn)
        collision_ack = json.loads(ack_path.read_text(encoding="utf-8"))
        check("event id collision rejected", collision_ack["status"] == "rejected" and collision_ack["code"] == "event_id_collision")
        check("event id collision quarantined", bool(list(receiver.QUARANTINE.glob(f"*{event_id}*.zip"))))
        check("event id collision preserves original hash", conn.execute("SELECT bundle_sha256 FROM events WHERE event_id=?", (event_id,)).fetchone()[0] == mixed_bundle_sha)
        check("event id collision receipt identifies new bundle", collision_ack["bundle_sha256"] == collision_sha)

        shared_content = b"print('same bytes')\n"
        cross_id = str(uuid.uuid4())
        cross = make_bundle(receiver.INBOX, cross_id, [
            {"name": "same.txt", "content": shared_content},
            {"name": "same.py", "content": shared_content},
        ])
        receiver.process_bundle(cross, conn)
        shared_hash = hashlib.sha256(shared_content).hexdigest()
        cross_classes = {row[0] for row in conn.execute("SELECT storage_class FROM files WHERE sha256=?", (shared_hash,))}
        check("same hash cross class", cross_classes == {"obsidian", "external"})

        recovery_id = str(uuid.uuid4())
        recovery_items = [
            {"name": "recover.txt", "content": b"recoverable text"},
            {"name": "recover.py", "content": b"print('recoverable')\n"},
        ]
        conn.execute(
            f"CREATE TRIGGER fail_event_insert BEFORE INSERT ON events WHEN NEW.event_id='{recovery_id}' "
            "BEGIN SELECT RAISE(ABORT, 'simulated pre-commit interruption'); END"
        )
        conn.commit()
        interrupted = make_bundle(receiver.INBOX, recovery_id, recovery_items)
        receiver.process_bundle(interrupted, conn)
        check("interruption no event row", conn.execute("SELECT COUNT(*) FROM events WHERE event_id=?", (recovery_id,)).fetchone()[0] == 0)
        check("interruption no file rows", conn.execute("SELECT COUNT(*) FROM files WHERE first_event_id=?", (recovery_id,)).fetchone()[0] == 0)
        check("interruption no obsidian event dir", not list(receiver.MEMORY_ROOT.rglob(recovery_id)))
        check("interruption no external event dir", not list(receiver.EXTERNAL_ROOT.rglob(recovery_id)))
        check("interruption no routing exception", not (receiver.ROUTING_EXCEPTION_ROOT / f"{recovery_id}.json").exists())
        conn.execute("DROP TRIGGER fail_event_insert")
        conn.commit()
        recovered = make_bundle(receiver.INBOX, recovery_id, recovery_items)
        receiver.process_bundle(recovered, conn)
        check("interrupted event rerun succeeds", conn.execute("SELECT status FROM events WHERE event_id=?", (recovery_id,)).fetchone()[0] == "stored")
        check("interrupted event rerun routes both", tuple(conn.execute("SELECT obsidian_count,external_count FROM events WHERE event_id=?", (recovery_id,)).fetchone()) == (1, 1))

        preexisting_id = str(uuid.uuid4())
        preexisting_review = receiver.ROUTING_EXCEPTION_ROOT / f"{preexisting_id}.json"
        preexisting_review.parent.mkdir(parents=True, exist_ok=True)
        preexisting_review.write_text("preexisting sentinel\n", encoding="utf-8")
        preexisting_bundle = make_bundle(receiver.INBOX, preexisting_id, [{"name": "note.txt", "content": b"must not overwrite"}])
        receiver.process_bundle(preexisting_bundle, conn)
        check("preexisting routing exception survives", preexisting_review.read_text(encoding="utf-8") == "preexisting sentinel\n")
        check("preexisting routing exception blocks event", conn.execute("SELECT COUNT(*) FROM events WHERE event_id=?", (preexisting_id,)).fetchone()[0] == 0)
        check("preexisting routing exception leaves no routed event dir", not list((receiver.MEMORY_ROOT / "91_归档").rglob(preexisting_id)))

        encrypted_transport_id = str(uuid.uuid4())
        encrypted_transport = make_bundle(
            receiver.INBOX,
            encrypted_transport_id,
            [{"name": "note.txt", "content": b"encrypted transport member"}],
        )
        encrypted_transport.write_bytes(mark_zip_member_encrypted(encrypted_transport.read_bytes(), "payload/note.txt"))
        receiver.process_bundle(encrypted_transport, conn)
        encrypted_transport_ack = json.loads(
            (receiver.ACK_OUTBOX / f"business-ack-{encrypted_transport_id}.json").read_text(encoding="utf-8")
        )
        check("encrypted transport rejected with ack", encrypted_transport_ack["status"] == "rejected" and encrypted_transport_ack["code"] == "transport_encrypted")
        check("encrypted transport no event", conn.execute("SELECT COUNT(*) FROM events WHERE event_id=?", (encrypted_transport_id,)).fetchone()[0] == 0)
        check("encrypted transport quarantined", bool(list(receiver.QUARANTINE.glob(f"*{encrypted_transport_id}*.zip"))))

        archive_id = str(uuid.uuid4())
        archive_bundle = make_bundle(receiver.INBOX, archive_id, [{"name": "nested.zip", "content": nested_archive}])
        receiver.process_bundle(archive_bundle, conn)
        nested_stored = next(receiver.EXTERNAL_ROOT.rglob("nested.zip"))
        check("nested archive byte identical", hashlib.sha256(nested_stored.read_bytes()).hexdigest() == hashlib.sha256(nested_archive).hexdigest())
        check("nested archive not unpacked", not list(root.rglob("escaped.txt")) and not list(root.rglob("run.sh")))

        rejected_id = str(uuid.uuid4())
        rejected = make_bundle(receiver.INBOX, rejected_id, [{"name": "broken.pdf", "content": b"?"}])
        receiver.process_bundle(rejected, conn)
        rejected_ack = json.loads((receiver.ACK_OUTBOX / f"business-ack-{rejected_id}.json").read_text(encoding="utf-8"))
        check("all quarantine rejected ack", rejected_ack["status"] == "rejected" and rejected_ack["quarantine_count"] == 1)
        check("all quarantine event recorded", conn.execute("SELECT status FROM events WHERE event_id=?", (rejected_id,)).fetchone()[0] == "quarantined")

        held_id = str(uuid.uuid4())
        held_bundle = make_bundle(receiver.INBOX, held_id, [{"name": "mystery.bin", "content": b"?"}])
        receiver.process_bundle(held_bundle, conn)
        held_ack = json.loads((receiver.ACK_OUTBOX / f"business-ack-{held_id}.json").read_text())
        check("unknown media waits with incomplete receipt", held_ack["video_waiting_count"] == 1 and held_ack["content_complete"] is False)
        check("unknown media retains event", conn.execute("SELECT status FROM events WHERE event_id=?", (held_id,)).fetchone()[0] == "stored_with_video_wait")
        check("unknown media does not create extracted file", not list(root.rglob("mystery.bin")))
        check("held metadata uses isolated ledger", receiver.video_hold.DATABASE.is_relative_to(root))

        mismatch_id = str(uuid.uuid4())
        mismatch = make_bundle(receiver.INBOX, mismatch_id, [{"name": "note.txt", "content": b"tampered", "digest": "0" * 64}])
        receiver.process_bundle(mismatch, conn)
        mismatch_ack = json.loads((receiver.ACK_OUTBOX / f"business-ack-{mismatch_id}.json").read_text(encoding="utf-8"))
        check("checksum mismatch whole bundle quarantine", bool(list(receiver.QUARANTINE.glob(f"*{mismatch_id}*.zip"))))
        check("checksum mismatch no event", conn.execute("SELECT COUNT(*) FROM events WHERE event_id=?", (mismatch_id,)).fetchone()[0] == 0)
        check("checksum mismatch ack count", mismatch_ack["quarantine_count"] == 1)

        traversal_id = str(uuid.uuid4())
        traversal = make_bundle(receiver.INBOX, traversal_id, [{"name": "evil.txt", "relative_path": "../evil.txt", "content": b"x"}])
        try:
            receiver.inspect_bundle(traversal)
            raise AssertionError("path traversal accepted")
        except receiver.BundleError as exc:
            check("path traversal rejected", exc.code == "unsafe_path")

        invalid_id = str(uuid.uuid4())
        invalid_archive = receiver.INBOX / f"business-memory-{invalid_id}.zip"
        invalid_archive.write_bytes(b"not zip")
        try:
            receiver.inspect_bundle(invalid_archive)
            raise AssertionError("invalid transport accepted")
        except receiver.BundleError as exc:
            check("invalid transport rejected", exc.code == "archive_invalid")

        large_manifest_id = str(uuid.uuid4())
        large_manifest = receiver.INBOX / f"business-memory-{large_manifest_id}.zip"
        with zipfile.ZipFile(large_manifest, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", b" " * (receiver.MAX_MANIFEST_BYTES + 1))
        try:
            receiver.inspect_bundle(large_manifest)
            raise AssertionError("oversized manifest accepted")
        except receiver.BundleError as exc:
            check("oversized manifest rejected before read", exc.code == "manifest_too_large")

        oversized_id = str(uuid.uuid4())
        oversized = receiver.INBOX / f"business-memory-{oversized_id}.zip"
        with oversized.open("wb") as handle:
            handle.truncate(receiver.MAX_TOTAL_BYTES + 1)
        original_sha256_file = receiver.sha256_file
        hash_calls = []

        def record_hash_call(path):
            hash_calls.append(path)
            return original_sha256_file(path)

        receiver.sha256_file = record_hash_call
        try:
            receiver.process_bundle(oversized, conn)
        finally:
            receiver.sha256_file = original_sha256_file
        oversized_reason = next(receiver.QUARANTINE.glob(f"*{oversized_id}*.zip.reason.json"))
        check("oversized transport rejected before hash", not hash_calls and json.loads(oversized_reason.read_text(encoding="utf-8"))["code"] == "archive_too_large")

        # Folder watcher schema-v2 flow.  All source paths below are synthetic
        # temporary directories; this self-test never reads the configured SMB.
        watch_payloads = root / "watch_payloads"
        watch_payloads.mkdir()

        def entry_state(source_id, entry_id):
            row = conn.execute(
                "SELECT source_entry_id,current_relative_path,path_key,current_sha256,source_mtime_ns,"
                "source_modified_at,size_bytes,version_number,storage_class,version_ref,mirror_relative,last_event_id "
                "FROM watch_entries WHERE source_id=? AND source_entry_id=?",
                (source_id, entry_id),
            ).fetchone()
            if row is None:
                return None
            event_status = conn.execute(
                "SELECT status FROM source_events WHERE event_id=?", (row[11],)
            ).fetchone()
            transition_sequence = conn.execute(
                "SELECT COUNT(*) FROM source_events WHERE source_id=? AND source_entry_id=?",
                (source_id, entry_id),
            ).fetchone()[0]
            return {
                "source_entry_id": row[0], "current_relative_path": row[1], "path_key": row[2],
                "current_sha256": row[3], "source_mtime_ns": row[4], "source_modified_at": row[5],
                "size_bytes": row[6], "version_number": row[7], "storage_class": row[8],
                "version_ref": row[9], "mirror_relative": row[10], "last_event_id": row[11],
                "last_event_status": event_status[0] if event_status else "",
                "transition_sequence": transition_sequence,
            }

        def content_event(source_id, entry_id, operation, relative_path, content, mtime_ns, previous=None):
            payload = watch_payloads / f"{uuid.uuid4()}.payload"
            payload.write_bytes(content)
            payload.chmod(0o600)
            current = {
                "relative_path": relative_path,
                "path_key": watcher.path_key(relative_path),
                "size_bytes": len(content),
                "mtime_ns": mtime_ns,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            manifest = watcher.build_manifest(source_id, entry_id, operation, current, previous, True)
            bundle = watcher.queue_event(manifest, payload)
            return manifest, bundle

        def watch_rejection_code(manifest, payload=None):
            bundle = watcher.queue_event(manifest, payload)
            receiver.process_watch_bundle(bundle, conn)
            reasons = sorted(
                receiver.QUARANTINE.glob(f"*{manifest['event_id']}*.zip.reason.json"),
                key=lambda path: path.stat().st_mtime_ns,
            )
            if not reasons:
                raise AssertionError("watch rejection did not create a reason record")
            return json.loads(reasons[-1].read_text(encoding="utf-8"))["code"]

        validation_content = b"validation fixture\n"
        validation_sha = hashlib.sha256(validation_content).hexdigest()
        validation_mtime = 1_699_999_900_123_456_789
        validation_payload = watch_payloads / "validation.payload"
        validation_payload.write_bytes(validation_content)
        validation_payload.chmod(0o600)
        validation_current = {
            "relative_path": "validation.txt", "path_key": watcher.path_key("validation.txt"),
            "size_bytes": len(validation_content), "mtime_ns": validation_mtime,
            "sha256": validation_sha,
        }
        validation_manifest = watcher.build_manifest(
            "source_a", str(uuid.uuid4()), "baseline", validation_current, None, True
        )
        for invalid_field in ("event_id", "source_entry_id"):
            invalid_manifest = dict(validation_manifest)
            invalid_manifest[invalid_field] = "-" * 36
            try:
                receiver.validate_watch_manifest(invalid_manifest)
                raise AssertionError(f"invalid watch UUID accepted: {invalid_field}")
            except receiver.BundleError as exc:
                check(f"watch {invalid_field} UUID validation reachable", exc.code == invalid_field)

        size_mismatch = dict(validation_manifest)
        size_mismatch["source_entry_id"] = str(uuid.uuid4())
        size_mismatch["source_size_bytes"] += 1
        size_mismatch["event_id"] = watcher.deterministic_event_id(size_mismatch)
        check(
            "watch recomputed event rejects source and payload size mismatch",
            watch_rejection_code(size_mismatch, validation_payload) == "watch_item_source_size",
        )

        timestamp_mismatch = dict(validation_manifest)
        timestamp_mismatch["source_entry_id"] = str(uuid.uuid4())
        timestamp_mismatch["source_modified_at"] = watcher.mtime_iso(validation_mtime + 1_000)
        timestamp_mismatch["event_id"] = watcher.deterministic_event_id(timestamp_mismatch)
        check(
            "watch recomputed event rejects mtime and ISO mismatch",
            watch_rejection_code(timestamp_mismatch, validation_payload) == "watch_modified_at_mismatch",
        )

        wrong_hash_current = dict(validation_current, sha256="0" * 64)
        wrong_hash_manifest = watcher.build_manifest(
            "source_a", str(uuid.uuid4()), "baseline", wrong_hash_current, None, True
        )
        check(
            "watch v2 wrong payload hash rejected",
            watch_rejection_code(wrong_hash_manifest, validation_payload) == "watch_checksum_mismatch",
        )

        traversal_manifest = dict(validation_manifest)
        traversal_manifest["source_entry_id"] = str(uuid.uuid4())
        traversal_manifest["source_relative_path"] = "../escape.txt"
        traversal_manifest["event_id"] = watcher.deterministic_event_id(traversal_manifest)
        check(
            "watch v2 source path traversal rejected",
            watch_rejection_code(traversal_manifest, validation_payload) == "watch_path",
        )

        atomic_reason = root / "atomic-reason" / "capture.reason.json"
        atomic_reason.parent.mkdir()
        stale_reason_tmp = atomic_reason.parent / f".{atomic_reason.name}.tmp-crash"
        stale_reason_tmp.write_text('{"at":', encoding="utf-8")
        stale_reason_tmp.chmod(0o600)
        atomic_reason_fields = {"event_id": str(uuid.uuid4()), "code": "source_unreadable"}
        check(
            "reason crash residue replaced atomically",
            receiver.write_reason_json_atomic(atomic_reason, atomic_reason_fields)
            and json.loads(atomic_reason.read_text(encoding="utf-8"))["code"] == "source_unreadable"
            and not stale_reason_tmp.exists(),
        )
        check(
            "reason crash replay validates existing record",
            receiver.write_reason_json_atomic(atomic_reason, atomic_reason_fields) is False,
        )
        atomic_reason.write_text('{"at":', encoding="utf-8")
        atomic_reason.chmod(0o600)
        try:
            receiver.write_reason_json_atomic(atomic_reason, atomic_reason_fields)
            raise AssertionError("truncated reason record accepted")
        except receiver.BundleError as exc:
            check("truncated reason record rejected", exc.code == "reason_integrity")

        baseline_content = b"watch baseline\n"
        baseline_sha = hashlib.sha256(baseline_content).hexdigest()
        baseline_entry = watcher.deterministic_entry_id(
            "source_a", "部门/说明.txt", baseline_sha, 1_700_000_000_000_000_000, 1
        )
        baseline_manifest, baseline_bundle = content_event(
            "source_a", baseline_entry, "baseline", "部门/说明.txt", baseline_content,
            1_700_000_000_000_000_000,
        )
        baseline_bundle_sha = hashlib.sha256(baseline_bundle.read_bytes()).hexdigest()
        check("watch bundle owner-only mode", stat.S_IMODE(baseline_bundle.stat().st_mode) == 0o600)
        check("watch inbox owner-only mode", stat.S_IMODE(receiver.WATCH_INBOX.stat().st_mode) == 0o700)
        check("watch deterministic event id", baseline_manifest["event_id"] == receiver.deterministic_watch_event_id(baseline_manifest))
        check("watch local preflight accepted", receiver.inspect_watch_bundle(baseline_bundle)["operation"] == "baseline")
        receiver.process_watch_bundle(baseline_bundle, conn)
        baseline_row = entry_state("source_a", baseline_entry)
        check("watch baseline entry stored", baseline_row is not None and baseline_row["version_number"] == 1)
        check("watch baseline source event", conn.execute("SELECT operation FROM source_events WHERE event_id=?", (baseline_manifest["event_id"],)).fetchone()[0] == "baseline")
        version_one = receiver.NON_OBSIDIAN_ROOT / baseline_row["version_ref"].split(":", 1)[1]
        mirror_one = receiver.MEMORY_ROOT / baseline_row["mirror_relative"]
        check("watch immutable version stored", version_one.read_bytes() == baseline_content)
        check("watch latest readable mirror stored", mirror_one.read_bytes() == baseline_content)
        review_paths = list(receiver.WATCH_NOTES_ROOT.glob("Win11文件夹监控-*.md"))
        check("watch one source one review note", len(review_paths) == 1)
        check(
            "watch machine note bypasses human pending directory",
            not list((receiver.MEMORY_ROOT / "90_待确认").glob("Win11文件夹监控-*.md")),
        )
        review_text = review_paths[0].read_text(encoding="utf-8")
        check("watch review states explicit", all(value in review_text for value in (
            "source_system: windows_folder_watch", "status: review_required",
            "evidence_level: unverified", "fact_status: pending",
        )))
        check("watch review omits absolute source", "/Users/" not in review_text)

        modified_content = b"watch modified\n"
        modified_manifest, modified_bundle = content_event(
            "source_a", baseline_entry, "modified", "部门/说明.txt", modified_content,
            1_700_000_100_000_000_000, baseline_row,
        )
        receiver.process_watch_bundle(modified_bundle, conn)
        modified_row = entry_state("source_a", baseline_entry)
        check("watch modification increments version", modified_row["version_number"] == 2)
        check("watch modification keeps old immutable", version_one.read_bytes() == baseline_content)
        check("watch modification replaces mirror", mirror_one.read_bytes() == modified_content)
        check("watch no old version in Obsidian", len(list((receiver.MEMORY_ROOT / "91_归档" / "Win11镜像").rglob("*.txt"))) == 1)
        check("watch fixed external machine note reused", len(list(receiver.WATCH_NOTES_ROOT.glob("Win11文件夹监控-*.md"))) == 1)

        # Re-creating the exact event package is byte deterministic, and receiver
        # treats it as a duplicate after a crash between DB commit and archive.
        duplicate_payload = watch_payloads / "duplicate.payload"
        duplicate_payload.write_bytes(modified_content)
        duplicate_again = watcher.queue_event(modified_manifest, duplicate_payload)
        check("watch deterministic bundle bytes", hashlib.sha256(duplicate_again.read_bytes()).hexdigest() == hashlib.sha256((receiver.PROCESSED_BUNDLES / f"business-watch-{modified_manifest['event_id']}.zip").read_bytes()).hexdigest())
        receiver.process_watch_bundle(duplicate_again, conn)
        check("watch duplicate event idempotent", conn.execute("SELECT COUNT(*) FROM source_events WHERE event_id=?", (modified_manifest["event_id"],)).fetchone()[0] == 1)

        metadata_previous = entry_state("source_a", baseline_entry)
        metadata_current = {
            "relative_path": metadata_previous["current_relative_path"],
            "path_key": metadata_previous["path_key"],
            "size_bytes": metadata_previous["size_bytes"],
            "mtime_ns": 1_700_000_150_000_000_000,
            "sha256": metadata_previous["current_sha256"],
        }
        metadata_note_before = review_paths[0].read_text(encoding="utf-8")
        dirty_before_metadata = int(conn.execute(
            "SELECT value FROM maintenance_state WHERE key='index_dirty_generation'"
        ).fetchone()[0])
        metadata_manifest = watcher.build_manifest(
            "source_a", baseline_entry, "modified", metadata_current, metadata_previous, False
        )
        metadata_bundle = watcher.queue_event(metadata_manifest)
        receiver.process_watch_bundle(metadata_bundle, conn)
        metadata_row = entry_state("source_a", baseline_entry)
        check("watch metadata-only event recorded", conn.execute("SELECT status FROM source_events WHERE event_id=?", (metadata_manifest["event_id"],)).fetchone()[0] == "metadata_only")
        check("watch metadata-only updates timestamp", metadata_row["source_mtime_ns"] == metadata_current["mtime_ns"])
        check("watch metadata-only creates no version", metadata_row["version_number"] == 2 and conn.execute("SELECT COUNT(*) FROM watch_versions WHERE source_id=? AND source_entry_id=?", ("source_a", baseline_entry)).fetchone()[0] == 2)
        check("watch metadata-only creates no memory change", review_paths[0].read_text(encoding="utf-8") == metadata_note_before)
        check(
            "watch metadata-only queues index refresh",
            int(conn.execute(
                "SELECT value FROM maintenance_state WHERE key='index_dirty_generation'"
            ).fetchone()[0]) == dirty_before_metadata + 1,
        )

        rename_previous = entry_state("source_a", baseline_entry)
        renamed_current = {
            "relative_path": "部门/改名说明.txt", "path_key": watcher.path_key("部门/改名说明.txt"),
            "size_bytes": len(modified_content), "mtime_ns": 1_700_000_200_000_000_000,
            "sha256": hashlib.sha256(modified_content).hexdigest(),
        }
        rename_manifest = watcher.build_manifest(
            "source_a", baseline_entry, "renamed", renamed_current, rename_previous, False
        )
        rename_bundle = watcher.queue_event(rename_manifest)
        receiver.process_watch_bundle(rename_bundle, conn)
        renamed_row = entry_state("source_a", baseline_entry)
        renamed_mirror = receiver.MEMORY_ROOT / renamed_row["mirror_relative"]
        check("watch pure rename keeps entry", renamed_row["current_relative_path"] == "部门/改名说明.txt")
        check("watch pure rename keeps version count", renamed_row["version_number"] == 2 and conn.execute("SELECT COUNT(*) FROM watch_versions WHERE source_id=? AND source_entry_id=?", ("source_a", baseline_entry)).fetchone()[0] == 2)
        check("watch pure rename moves latest mirror", renamed_mirror.read_bytes() == modified_content and not mirror_one.exists())

        delete_previous = entry_state("source_a", baseline_entry)
        delete_marker = {
            "relative_path": delete_previous["current_relative_path"],
            "size_bytes": delete_previous["size_bytes"],
            "mtime_ns": delete_previous["source_mtime_ns"],
        }
        delete_manifest = watcher.build_manifest(
            "source_a", baseline_entry, "deleted", delete_marker, delete_previous, False
        )
        delete_bundle = watcher.queue_event(delete_manifest)
        receiver.process_watch_bundle(delete_bundle, conn)
        deleted_state = conn.execute(
            "SELECT active,current_sha256,version_number FROM watch_entries WHERE source_id=? AND source_entry_id=?",
            ("source_a", baseline_entry),
        ).fetchone()
        check("watch delete creates tombstone", deleted_state == (0, None, 2))
        check("watch delete preserves immutable history", version_one.exists() and conn.execute("SELECT COUNT(*) FROM watch_versions WHERE source_id=? AND source_entry_id=?", ("source_a", baseline_entry)).fetchone()[0] == 2)
        check("watch delete removes only latest mirror", not renamed_mirror.exists())
        check("watch delete event status", conn.execute("SELECT status FROM source_events WHERE event_id=?", (delete_manifest["event_id"],)).fetchone()[0] == "tombstoned")

        # Same bytes in a second source remain isolated by source_id.
        source_b_entry = watcher.deterministic_entry_id(
            "source_b", "说明.txt", baseline_sha, 1_700_000_300_000_000_000, 1
        )
        source_b_manifest, source_b_bundle = content_event(
            "source_b", source_b_entry, "baseline", "说明.txt", baseline_content,
            1_700_000_300_000_000_000,
        )
        receiver.process_watch_bundle(source_b_bundle, conn)
        check("watch source isolation entries", conn.execute("SELECT COUNT(*) FROM watch_entries WHERE source_id IN ('source_a','source_b')").fetchone()[0] == 2)
        check("watch source isolation versions", conn.execute("SELECT COUNT(DISTINCT source_id) FROM watch_versions WHERE sha256=?", (baseline_sha,)).fetchone()[0] == 2)

        case_mirror_content = b"case mirror\n"
        case_mirror_sha = hashlib.sha256(case_mirror_content).hexdigest()
        case_mirror_entry = watcher.deterministic_entry_id(
            "source_a", "CaseMirror.txt", case_mirror_sha, 1_700_000_301_000_000_000, 1
        )
        case_mirror_add, case_mirror_bundle = content_event(
            "source_a", case_mirror_entry, "added", "CaseMirror.txt", case_mirror_content,
            1_700_000_301_000_000_000,
        )
        receiver.process_watch_bundle(case_mirror_bundle, conn)
        case_mirror_previous = entry_state("source_a", case_mirror_entry)
        case_mirror_old_path = receiver.MEMORY_ROOT / case_mirror_previous["mirror_relative"]
        case_mirror_current = {
            "relative_path": "casemirror.txt", "path_key": watcher.path_key("casemirror.txt"),
            "size_bytes": len(case_mirror_content), "mtime_ns": 1_700_000_302_000_000_000,
            "sha256": case_mirror_sha,
        }
        case_mirror_rename = watcher.build_manifest(
            "source_a", case_mirror_entry, "renamed", case_mirror_current,
            case_mirror_previous, False,
        )
        receiver.process_watch_bundle(watcher.queue_event(case_mirror_rename), conn)
        case_mirror_after = entry_state("source_a", case_mirror_entry)
        case_mirror_new_path = receiver.MEMORY_ROOT / case_mirror_after["mirror_relative"]
        check(
            "watch case-only rename preserves published mirror",
            case_mirror_new_path.is_file()
            and hashlib.sha256(case_mirror_new_path.read_bytes()).hexdigest() == case_mirror_sha,
        )
        check(
            "watch case-only rename database mirror resolves",
            case_mirror_after["current_relative_path"] == "casemirror.txt"
            and case_mirror_after["mirror_relative"].endswith("/casemirror.txt")
            and (not case_mirror_old_path.exists() or receiver.same_existing_file(case_mirror_old_path, case_mirror_new_path)),
        )

        source_b_second_content = b"second logical source\n"
        source_b_second_sha = hashlib.sha256(source_b_second_content).hexdigest()
        source_b_second_entry = watcher.deterministic_entry_id(
            "source_b", "第二份.txt", source_b_second_sha, 1_700_000_310_000_000_000,
            1,
        )
        source_b_second_manifest, source_b_second_bundle = content_event(
            "source_b", source_b_second_entry, "added", "第二份.txt", source_b_second_content,
            1_700_000_310_000_000_000,
        )
        receiver.process_watch_bundle(source_b_second_bundle, conn)
        check("watch each logical entry has one note", all(
            (receiver.WATCH_NOTES_ROOT / f"Win11文件夹监控-{entry_id}.md").is_file()
            for entry_id in (source_b_entry, source_b_second_entry)
        ))
        check("watch multi-file external notes do not overwrite", len(list(receiver.WATCH_NOTES_ROOT.glob("Win11文件夹监控-*.md"))) == 4)

        no_mirror_content = b"raw source stays local\n"
        no_mirror_sha = hashlib.sha256(no_mirror_content).hexdigest()
        no_mirror_entry = watcher.deterministic_entry_id(
            "source_no_mirror", "邮件记忆/邮件原文/2026/source.md", no_mirror_sha,
            1_700_000_312_000_000_000, 1,
        )
        _no_mirror_manifest, no_mirror_bundle = content_event(
            "source_no_mirror", no_mirror_entry, "baseline",
            "邮件记忆/邮件原文/2026/source.md", no_mirror_content,
            1_700_000_312_000_000_000,
        )
        receiver.process_watch_bundle(no_mirror_bundle, conn)
        no_mirror_row = entry_state("source_no_mirror", no_mirror_entry)
        no_mirror_version = receiver.NON_OBSIDIAN_ROOT / no_mirror_row["version_ref"].split(":", 1)[1]
        check("watch no-mirror source keeps immutable local version", no_mirror_version.read_bytes() == no_mirror_content)
        check("watch no-mirror source records no Obsidian mirror", no_mirror_row["mirror_relative"] is None)
        check(
            "watch no-mirror source writes no raw source into Obsidian",
            not (receiver.MEMORY_ROOT / "91_归档" / "Win11镜像" / "邮件记忆").exists(),
        )

        cycle_content = b"rename cycle\n"
        cycle_sha = hashlib.sha256(cycle_content).hexdigest()
        cycle_entry = watcher.deterministic_entry_id(
            "source_b", "cycle-x.txt", cycle_sha, 1_700_000_315_000_000_000, 1
        )
        cycle_add_manifest, cycle_add_bundle = content_event(
            "source_b", cycle_entry, "added", "cycle-x.txt", cycle_content,
            1_700_000_315_000_000_000,
        )
        receiver.process_watch_bundle(cycle_add_bundle, conn)

        def rename_cycle(destination, mtime_ns):
            previous = entry_state("source_b", cycle_entry)
            current = {
                "relative_path": destination, "path_key": watcher.path_key(destination),
                "size_bytes": len(cycle_content), "mtime_ns": mtime_ns, "sha256": cycle_sha,
            }
            manifest = watcher.build_manifest(
                "source_b", cycle_entry, "renamed", current, previous, False
            )
            receiver.process_watch_bundle(watcher.queue_event(manifest), conn)
            return manifest

        cycle_xy_first = rename_cycle("cycle-y.txt", 1_700_000_316_000_000_000)
        cycle_yx = rename_cycle("cycle-x.txt", 1_700_000_317_000_000_000)
        cycle_xy_second = rename_cycle("cycle-y.txt", 1_700_000_316_000_000_000)
        check("watch rename cycle uses predecessor token", cycle_xy_first["previous_event_id"] == cycle_add_manifest["event_id"] and cycle_yx["previous_event_id"] == cycle_xy_first["event_id"])
        check("watch repeated rename gets new deterministic id", cycle_xy_second["event_id"] != cycle_xy_first["event_id"] and cycle_xy_second["transition_sequence"] == 4)
        check("watch repeated rename advances database", entry_state("source_b", cycle_entry)["current_relative_path"] == "cycle-y.txt")

        encrypted_watch_content = mark_zip_encrypted(make_ooxml("word"))
        encrypted_watch_sha = hashlib.sha256(encrypted_watch_content).hexdigest()
        encrypted_watch_entry = watcher.deterministic_entry_id(
            "source_a", "受保护.docx", encrypted_watch_sha, 1_700_000_319_000_000_000, 1
        )
        encrypted_watch_manifest, encrypted_watch_bundle = content_event(
            "source_a", encrypted_watch_entry, "added", "受保护.docx",
            encrypted_watch_content, 1_700_000_319_000_000_000,
        )
        receiver.process_watch_bundle(encrypted_watch_bundle, conn)
        encrypted_watch_row = entry_state("source_a", encrypted_watch_entry)
        encrypted_reason = next(
            (receiver.NON_OBSIDIAN_ROOT / "quarantine" / "win11-folder-watch" / "source_a" / encrypted_watch_entry)
            .rglob("*.reason.json")
        )
        check(
            "watch v2 encrypted OOXML quarantined",
            encrypted_watch_row["storage_class"] == "quarantine"
            and json.loads(encrypted_reason.read_text(encoding="utf-8"))["code"] == "ooxml_encrypted",
        )

        capture_error_entry = watcher.deterministic_entry_id(
            "source_a", "受限/超大文件.txt", None, 1_700_000_320_000_000_000,
            1,
        )
        capture_error_current = {
            "relative_path": "受限/超大文件.txt", "path_key": watcher.path_key("受限/超大文件.txt"),
            "size_bytes": receiver.MAX_ITEM_BYTES + 1, "mtime_ns": 1_700_000_320_000_000_000,
            "sha256": None,
        }
        capture_error_manifest = watcher.build_manifest(
            "source_a", capture_error_entry, "added", capture_error_current, None, False,
            "source_item_too_large",
        )
        capture_error_reason = (
            receiver.NON_OBSIDIAN_ROOT / "quarantine" / "win11-folder-watch" / "source_a"
            / capture_error_entry / "capture-errors" / f"{capture_error_manifest['event_id']}.reason.json"
        )
        capture_error_fields = {
            "event_id": capture_error_manifest["event_id"], "source_id": "source_a",
            "source_entry_id": capture_error_entry, "relative_path": "受限/超大文件.txt",
            "size_bytes": receiver.MAX_ITEM_BYTES + 1, "code": "source_item_too_large",
            "content_captured": False,
        }
        check(
            "watch capture-error crash residue precreated",
            receiver.write_reason_json_atomic(capture_error_reason, capture_error_fields),
        )
        capture_error_bundle = watcher.queue_event(capture_error_manifest)
        receiver.process_watch_bundle(capture_error_bundle, conn)
        capture_error_row = entry_state("source_a", capture_error_entry)
        capture_error_note = receiver.WATCH_NOTES_ROOT / f"Win11文件夹监控-{capture_error_entry}.md"
        check("watch capture failure quarantined as record", capture_error_row["storage_class"] == "quarantine" and capture_error_row["version_number"] == 0)
        check("watch capture failure has no false immutable bytes", conn.execute("SELECT COUNT(*) FROM watch_versions WHERE source_id=? AND source_entry_id=?", ("source_a", capture_error_entry)).fetchone()[0] == 0)
        check("watch capture failure note explicit", "捕获异常：source_item_too_large" in capture_error_note.read_text(encoding="utf-8"))
        capture_error_reasons = list((receiver.NON_OBSIDIAN_ROOT / "quarantine" / "win11-folder-watch" / "source_a" / capture_error_entry / "capture-errors").glob("*.reason.json"))
        check("watch capture failure reason stored", len(capture_error_reasons) == 1 and json.loads(capture_error_reasons[0].read_text(encoding="utf-8"))["content_captured"] is False)
        check("watch capture-error crash replay accepts intact reason", conn.execute(
            "SELECT COUNT(*) FROM source_events WHERE event_id=?", (capture_error_manifest["event_id"],)
        ).fetchone()[0] == 1 and capture_error_reason.exists())
        capture_error_note_text = capture_error_note.read_text(encoding="utf-8")
        check("watch capture failure evidence avoids false hash claim", "本次未取得当前内容哈希" in capture_error_note_text and "校验了本地事件包、SHA-256" not in capture_error_note_text)

        recovered_initial_content = b"now readable\n"
        recovered_initial_previous = entry_state("source_a", capture_error_entry)
        recovered_initial_manifest, recovered_initial_bundle = content_event(
            "source_a", capture_error_entry, "modified", "受限/超大文件.txt",
            recovered_initial_content, 1_700_000_321_000_000_000, recovered_initial_previous,
        )
        receiver.process_watch_bundle(recovered_initial_bundle, conn)
        recovered_initial_row = entry_state("source_a", capture_error_entry)
        check("watch initial capture error can recover", recovered_initial_row["current_sha256"] == hashlib.sha256(recovered_initial_content).hexdigest() and recovered_initial_row["version_number"] == 1)
        check("watch recovered initial note becomes normal", "捕获异常：无" in capture_error_note.read_text(encoding="utf-8"))

        readable_error_previous = entry_state("source_b", source_b_second_entry)
        readable_error_current = {
            "relative_path": readable_error_previous["current_relative_path"],
            "path_key": readable_error_previous["path_key"],
            "size_bytes": readable_error_previous["size_bytes"],
            "mtime_ns": readable_error_previous["source_mtime_ns"],
            "sha256": None,
        }
        readable_error_manifest = watcher.build_manifest(
            "source_b", source_b_second_entry, "modified", readable_error_current,
            readable_error_previous, False, "source_unreadable",
        )
        receiver.process_watch_bundle(watcher.queue_event(readable_error_manifest), conn)
        readable_error_row = entry_state("source_b", source_b_second_entry)
        check("watch temporary unreadable keeps last good version", readable_error_row["current_sha256"] == source_b_second_sha and readable_error_row["version_number"] == 1)
        readable_error_note = receiver.WATCH_NOTES_ROOT / f"Win11文件夹监控-{source_b_second_entry}.md"
        check("watch temporary unreadable updates fixed note", "捕获异常：source_unreadable" in readable_error_note.read_text(encoding="utf-8"))

        readable_recovery_manifest, readable_recovery_bundle = content_event(
            "source_b", source_b_second_entry, "modified", "第二份.txt",
            source_b_second_content, readable_error_previous["source_mtime_ns"],
            readable_error_row,
        )
        receiver.process_watch_bundle(readable_recovery_bundle, conn)
        readable_recovery_status = conn.execute(
            "SELECT status FROM source_events WHERE event_id=?", (readable_recovery_manifest["event_id"],)
        ).fetchone()[0]
        check("watch same-hash capture recovery recorded", readable_recovery_status == "capture_recovered")
        check("watch same-hash recovery creates no duplicate version", conn.execute("SELECT COUNT(*) FROM watch_versions WHERE source_id=? AND source_entry_id=?", ("source_b", source_b_second_entry)).fetchone()[0] == 1)
        check("watch same-hash recovery clears error note", "捕获异常：无" in readable_error_note.read_text(encoding="utf-8"))

        repeated_error_previous = entry_state("source_b", source_b_second_entry)
        repeated_error_current = {
            "relative_path": repeated_error_previous["current_relative_path"],
            "path_key": repeated_error_previous["path_key"],
            "size_bytes": repeated_error_previous["size_bytes"],
            "mtime_ns": readable_error_previous["source_mtime_ns"],
            "sha256": None,
        }
        repeated_error_manifest = watcher.build_manifest(
            "source_b", source_b_second_entry, "modified", repeated_error_current,
            repeated_error_previous, False, "source_unreadable",
        )
        check("watch repeated capture error gets new id", repeated_error_manifest["event_id"] != readable_error_manifest["event_id"] and repeated_error_manifest["transition_sequence"] == 4)
        receiver.process_watch_bundle(watcher.queue_event(repeated_error_manifest), conn)
        check("watch repeated capture error advances database", conn.execute(
            "SELECT status FROM source_events WHERE event_id=?", (repeated_error_manifest["event_id"],)
        ).fetchone()[0] == "capture_error:source_unreadable")

        corrupt_reason_entry = watcher.deterministic_entry_id(
            "source_a", "corrupt-reason.txt", None, 1_700_000_322_000_000_000, 1
        )
        corrupt_reason_current = {
            "relative_path": "corrupt-reason.txt", "path_key": "corrupt-reason.txt",
            "size_bytes": 7, "mtime_ns": 1_700_000_322_000_000_000, "sha256": None,
        }
        corrupt_reason_manifest = watcher.build_manifest(
            "source_a", corrupt_reason_entry, "added", corrupt_reason_current,
            None, False, "source_access_denied",
        )
        corrupt_reason_path = (
            receiver.NON_OBSIDIAN_ROOT / "quarantine" / "win11-folder-watch" / "source_a"
            / corrupt_reason_entry / "capture-errors" / f"{corrupt_reason_manifest['event_id']}.reason.json"
        )
        corrupt_reason_path.parent.mkdir(parents=True)
        corrupt_reason_path.write_text('{"at":', encoding="utf-8")
        corrupt_reason_path.chmod(0o600)
        corrupt_reason_bundle = watcher.queue_event(corrupt_reason_manifest)
        receiver.process_watch_bundle(corrupt_reason_bundle, conn)
        check("watch truncated capture-error reason blocks commit", conn.execute(
            "SELECT COUNT(*) FROM source_events WHERE event_id=?", (corrupt_reason_manifest["event_id"],)
        ).fetchone()[0] == 0)
        corrupt_bundle_reason = next(receiver.QUARANTINE.glob(
            f"*{corrupt_reason_manifest['event_id']}*.zip.reason.json"
        ))
        check(
            "watch truncated capture-error reason reports integrity failure",
            json.loads(corrupt_bundle_reason.read_text(encoding="utf-8"))["code"] == "reason_integrity",
        )

        rollback_content = b"watch transaction rollback\n"
        rollback_sha = hashlib.sha256(rollback_content).hexdigest()
        rollback_entry = watcher.deterministic_entry_id(
            "source_a", "rollback.txt", rollback_sha, 1_700_000_323_000_000_000, 1
        )
        rollback_manifest, rollback_bundle = content_event(
            "source_a", rollback_entry, "added", "rollback.txt", rollback_content,
            1_700_000_323_000_000_000,
        )
        original_bump_generation = receiver.bump_maintenance_generation

        def interrupt_watch_transaction(_conn, _processed_at):
            raise RuntimeError("simulated watch transaction interruption")

        receiver.bump_maintenance_generation = interrupt_watch_transaction
        try:
            receiver.process_watch_bundle(rollback_bundle, conn)
        finally:
            receiver.bump_maintenance_generation = original_bump_generation
        check("watch transaction interruption rolls back rows", conn.execute(
            "SELECT COUNT(*) FROM source_events WHERE event_id=?", (rollback_manifest["event_id"],)
        ).fetchone()[0] == 0 and entry_state("source_a", rollback_entry) is None)
        check(
            "watch transaction interruption rolls back artifacts",
            not (receiver.WATCH_NOTES_ROOT / f"Win11文件夹监控-{rollback_entry}.md").exists()
            and not (receiver.MEMORY_ROOT / "91_归档" / "Win11镜像" / "rollback.txt").exists()
            and not (receiver.WATCH_VERSIONS_ROOT / "source_a" / rollback_entry).exists(),
        )
        rollback_replay_payload = watch_payloads / "rollback-replay.payload"
        rollback_replay_payload.write_bytes(rollback_content)
        receiver.process_watch_bundle(
            watcher.queue_event(rollback_manifest, rollback_replay_payload), conn
        )
        check("watch interrupted transaction deterministic replay succeeds", conn.execute(
            "SELECT COUNT(*) FROM source_events WHERE event_id=?", (rollback_manifest["event_id"],)
        ).fetchone()[0] == 1)

        out_of_order_entry = watcher.deterministic_entry_id(
            "source_a", "ordered.txt", hashlib.sha256(b"first").hexdigest(),
            1_700_000_324_000_000_000, 1,
        )
        ordered_first = b"first"
        ordered_first_current = {
            "relative_path": "ordered.txt", "path_key": "ordered.txt",
            "size_bytes": len(ordered_first), "mtime_ns": 1_700_000_324_000_000_000,
            "sha256": hashlib.sha256(ordered_first).hexdigest(),
        }
        ordered_add = watcher.build_manifest(
            "source_a", out_of_order_entry, "added", ordered_first_current, None, True
        )
        ordered_previous = {
            "current_relative_path": "ordered.txt", "path_key": "ordered.txt",
            "current_sha256": ordered_first_current["sha256"],
            "source_mtime_ns": ordered_first_current["mtime_ns"],
            "source_modified_at": watcher.mtime_iso(ordered_first_current["mtime_ns"]),
            "size_bytes": len(ordered_first), "last_event_id": ordered_add["event_id"],
            "transition_sequence": 1,
        }
        ordered_second = b"second"
        ordered_second_current = {
            "relative_path": "ordered.txt", "path_key": "ordered.txt",
            "size_bytes": len(ordered_second), "mtime_ns": 1_700_000_325_000_000_000,
            "sha256": hashlib.sha256(ordered_second).hexdigest(),
        }
        ordered_modified = watcher.build_manifest(
            "source_a", out_of_order_entry, "modified", ordered_second_current,
            ordered_previous, True,
        )
        ordered_second_payload = watch_payloads / "ordered-second.payload"
        ordered_second_payload.write_bytes(ordered_second)
        receiver.process_watch_bundle(
            watcher.queue_event(ordered_modified, ordered_second_payload), conn
        )
        check("watch out-of-order dependent event fails closed", conn.execute(
            "SELECT COUNT(*) FROM source_events WHERE event_id=?", (ordered_modified["event_id"],)
        ).fetchone()[0] == 0)
        ordered_first_payload = watch_payloads / "ordered-first.payload"
        ordered_first_payload.write_bytes(ordered_first)
        receiver.process_watch_bundle(watcher.queue_event(ordered_add, ordered_first_payload), conn)
        ordered_second_replay = watch_payloads / "ordered-second-replay.payload"
        ordered_second_replay.write_bytes(ordered_second)
        receiver.process_watch_bundle(
            watcher.queue_event(ordered_modified, ordered_second_replay), conn
        )
        check("watch out-of-order event replays after predecessor", conn.execute(
            "SELECT operation,transition_sequence FROM source_events WHERE source_id=? AND source_entry_id=? "
            "ORDER BY transition_sequence", ("source_a", out_of_order_entry),
        ).fetchall() == [("added", 1), ("modified", 2)])

        # Unconfigured source and world-readable local bundles fail closed.
        untrusted_content = b"untrusted\n"
        untrusted_sha = hashlib.sha256(untrusted_content).hexdigest()
        untrusted_entry = watcher.deterministic_entry_id(
            "source_untrusted", "x.txt", untrusted_sha, 1_700_000_400_000_000_000, 1
        )
        untrusted_manifest, untrusted_bundle = content_event(
            "source_untrusted", untrusted_entry, "baseline", "x.txt", untrusted_content,
            1_700_000_400_000_000_000,
        )
        receiver.process_watch_bundle(untrusted_bundle, conn)
        untrusted_reason = next(receiver.QUARANTINE.glob(f"*{untrusted_manifest['event_id']}*.zip.reason.json"))
        check("watch unconfigured source rejected", json.loads(untrusted_reason.read_text(encoding="utf-8"))["code"] == "watch_source_id")
        check("watch unconfigured source not stored", conn.execute("SELECT COUNT(*) FROM source_events WHERE event_id=?", (untrusted_manifest["event_id"],)).fetchone()[0] == 0)

        insecure_entry = watcher.deterministic_entry_id(
            "source_a", "insecure.txt", untrusted_sha, 1_700_000_500_000_000_000, 1
        )
        insecure_manifest, insecure_bundle = content_event(
            "source_a", insecure_entry, "added", "insecure.txt", untrusted_content,
            1_700_000_500_000_000_000,
        )
        insecure_bundle.chmod(0o644)
        receiver.process_watch_bundle(insecure_bundle, conn)
        insecure_reason = next(receiver.QUARANTINE.glob(f"*{insecure_manifest['event_id']}*.zip.reason.json"))
        check("watch world-readable bundle rejected", json.loads(insecure_reason.read_text(encoding="utf-8"))["code"] == "watch_bundle_permissions")

        receiver.WATCH_INBOX.chmod(0o755)
        try:
            receiver.validate_watch_inbox_security()
            raise AssertionError("unsafe watch inbox accepted")
        except RuntimeError:
            check("watch inbox unsafe mode rejected", True)
        finally:
            receiver.WATCH_INBOX.chmod(0o700)

        # Taildrop must never gain schema-v2 delete/rename authority.
        taildrop_v2_id = str(uuid.uuid4())
        taildrop_v2 = receiver.INBOX / f"business-memory-{taildrop_v2_id}.zip"
        taildrop_manifest = dict(source_b_manifest)
        taildrop_manifest["event_id"] = taildrop_v2_id
        with zipfile.ZipFile(taildrop_v2, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(taildrop_manifest))
            archive.writestr("payload/content", baseline_content)
        receiver.process_bundle(taildrop_v2, conn)
        taildrop_v2_reason = next(receiver.QUARANTINE.glob(f"*{taildrop_v2_id}*.zip.reason.json"))
        check("taildrop schema v2 explicitly rejected", json.loads(taildrop_v2_reason.read_text(encoding="utf-8"))["code"] == "taildrop_v2_forbidden")

        local_v1_id = str(uuid.uuid4())
        local_v1 = receiver.WATCH_INBOX / f"business-watch-{local_v1_id}.zip"
        with zipfile.ZipFile(local_v1, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps({"schema_version": 1}))
        local_v1.chmod(0o600)
        receiver.process_watch_bundle(local_v1, conn)
        local_v1_reason = next(receiver.QUARANTINE.glob(f"*{local_v1_id}*.zip.reason.json"))
        check("local watch inbox v1 rejected", json.loads(local_v1_reason.read_text(encoding="utf-8"))["code"] == "watch_manifest_missing_fields")

        # Watcher mechanics: ignored temporary files, stable samples spanning
        # 60 seconds, mount identity, read-only capture, and disconnect backoff.
        synthetic_mount = root / "synthetic_mount"
        synthetic_source = synthetic_mount / "source"
        synthetic_source.mkdir(parents=True)
        stable_file = synthetic_source / "stable.txt"
        stable_file.write_bytes(b"stable")
        (synthetic_source / "~$office.docx").write_bytes(b"lock")
        (synthetic_source / "half.partial").write_bytes(b"partial")
        (synthetic_source / "desktop.ini").write_bytes(b"system")
        (synthetic_source / "shortcut.lnk").write_bytes(b"shortcut")
        cache_directory = synthetic_source / "$RECYCLE.BIN"
        cache_directory.mkdir()
        (cache_directory / "cached.txt").write_bytes(b"cache")
        symlink_supported = True
        try:
            (synthetic_source / "link.txt").symlink_to(stable_file)
        except OSError:
            symlink_supported = False
        fifo_supported = True
        try:
            os.mkfifo(synthetic_source / "reparse-like.pipe", 0o600)
        except OSError:
            fifo_supported = False
        scan_records = watcher._scan_tree(synthetic_source)
        check("watcher ignores temporary files", [item["relative_path"] for item in scan_records] == ["stable.txt"])
        check("watcher ignores symbolic links", not symlink_supported or all(item["relative_path"] != "link.txt" for item in scan_records))
        check("watcher ignores local non-regular entries", not fifo_supported or all(
            item["relative_path"] != "reparse-like.pipe" for item in scan_records
        ))
        check("watcher ignores lnk and cache directories", all(
            "shortcut.lnk" not in item["relative_path"] and "$RECYCLE.BIN" not in item["relative_path"]
            for item in scan_records
        ))
        stability_state = watcher.default_state()
        check("watcher first stable sample held", not watcher.update_stability(scan_records, stability_state, 0, 3, 60))
        check("watcher second stable sample held", not watcher.update_stability(scan_records, stability_state, 30, 3, 60))
        check("watcher third sample spans 60 seconds", bool(watcher.update_stability(scan_records, stability_state, 60, 3, 60)))
        changed_records = [dict(scan_records[0], mtime_ns=scan_records[0]["mtime_ns"] + 1)]
        check("watcher changed signature resets stability", not watcher.update_stability(changed_records, stability_state, 75, 3, 60))
        nfd_name = "Cafe\u0301.txt"
        (synthetic_source / nfd_name).write_bytes(b"unicode")
        unicode_records = watcher._scan_tree(synthetic_source)
        unicode_record = next(item for item in unicode_records if item["relative_path"] == "Café.txt")
        check("watcher maps NFD path to NFC event path", unicode_record["actual_relative_path"] == nfd_name and unicode_record["relative_path"] == "Café.txt")
        mount_line = f"//user@win/DeskShare on {synthetic_mount} (smbfs, nodev, nosuid)"
        check("watcher expected SMB mount accepted", watcher.parse_expected_mount(mount_line, str(synthetic_mount), "smbfs", "DeskShare", "win"))
        check("watcher wrong SMB share rejected", not watcher.parse_expected_mount(mount_line, str(synthetic_mount), "smbfs", "OtherShare", "win"))
        check("watcher wrong SMB server rejected", not watcher.parse_expected_mount(mount_line, str(synthetic_mount), "smbfs", "DeskShare", "192.0.2.10"))

        captured = root / "captured.payload"
        before_bytes = stable_file.read_bytes()
        before_mode = stat.S_IMODE(stable_file.stat().st_mode)
        capture_result = watcher._capture_file(
            stable_file, captured, stable_file.stat().st_size, stable_file.stat().st_mtime_ns, 1024
        )
        check("watcher capture hash verified", capture_result["sha256"] == hashlib.sha256(before_bytes).hexdigest())
        check("watcher source remains read-only", stable_file.read_bytes() == before_bytes and stat.S_IMODE(stable_file.stat().st_mode) == before_mode)

        original_scan_tree = watcher._scan_tree

        def hanging_scan(_root):
            time.sleep(2)
            return []

        watcher._scan_tree = hanging_scan
        try:
            try:
                watcher.scan_with_timeout(synthetic_source, 0.05)
                raise AssertionError("hanging scan did not time out")
            except watcher.WatcherError as exc:
                check("watcher hanging scan times out", exc.code == "scan_timeout")
        finally:
            watcher._scan_tree = original_scan_tree

        original_capture_file = watcher._capture_file

        def hanging_capture(*_args):
            time.sleep(2)
            return {"sha256": "0" * 64, "size_bytes": 0}

        watcher._capture_file = hanging_capture
        capture_timeout_destination = root / "capture-timeout.payload"
        try:
            try:
                watcher.capture_with_timeout(
                    stable_file, capture_timeout_destination,
                    {"size_bytes": stable_file.stat().st_size, "mtime_ns": stable_file.stat().st_mtime_ns},
                    1024, 0.05,
                )
                raise AssertionError("hanging capture did not time out")
            except watcher.WatcherError as exc:
                check(
                    "watcher hanging capture times out",
                    exc.code == "capture_timeout" and not capture_timeout_destination.exists(),
                )
        finally:
            watcher._capture_file = original_capture_file

        planning_config = {
            "source_id": "planning_source", "root": str(synthetic_source),
            "mount_root": str(synthetic_mount), "expected_fs": "smbfs", "expected_share": "DeskShare",
            "expected_server": "win",
            "poll_seconds": 15, "full_reconcile_seconds": 60, "stable_samples": 3,
            "stable_span_seconds": 60, "scan_timeout_seconds": 45,
            "delete_confirm_scans": 2, "disconnect_backoff_seconds": [15, 30, 60],
        }

        def access_denied_capture(*_args):
            raise watcher.WatcherError("source_access_denied", "synthetic permission denial")

        access_denied_planner = watcher.FolderWatcher(
            planning_config, 1024, mount_checker=lambda _source: True,
            scanner=lambda _root: [], capturer=access_denied_capture, clock=lambda: 0,
        )
        access_denied_spool = watcher.STATE_ROOT / "spool" / "access-denied"
        access_denied_spool.mkdir(parents=True)
        access_denied_result = access_denied_planner._capture_record(scan_records[0], access_denied_spool)
        check(
            "watcher protected unreadable file becomes metadata-only error",
            access_denied_result["capture_error"] == "source_access_denied"
            and access_denied_result["sha256"] is None
            and access_denied_result["payload_path"] is None,
        )
        planner = watcher.FolderWatcher(
            planning_config, 1024, mount_checker=lambda _source: False,
            scanner=lambda _root: [], capturer=lambda *_args: None, clock=lambda: 0,
        )
        watcher.update_stability(scan_records, planner.state, 0, 3, 60)
        watcher.update_stability(scan_records, planner.state, 30, 3, 60)
        planner.state["missing_counts"] = {"entry": 1}
        planner.state["replacement_counts"] = {"entry": 1}
        planner.state["pending_readds"] = {"stable.txt": {"relative_path": "stable.txt"}}
        check("watcher disconnect backoff 15", planner.cycle() == 15)
        check("watcher disconnect resets all continuity evidence", all(
            not planner.state[field] for field in (
                "candidates", "missing_counts", "replacement_counts", "pending_readds",
            )
        ))
        check(
            "watcher reconnect stability restarts first sample",
            not watcher.update_stability(scan_records, planner.state, 60, 3, 60),
        )
        check(
            "watcher reconnect stability restarts second sample",
            not watcher.update_stability(scan_records, planner.state, 90, 3, 60),
        )
        check(
            "watcher reconnect stability requires new 60 second window",
            bool(watcher.update_stability(scan_records, planner.state, 120, 3, 60)),
        )
        check("watcher disconnect backoff 30", planner.cycle() == 30)
        check("watcher disconnect backoff caps 60", planner.cycle() == 60 and planner.cycle() == 60)

        backlog_bundle = receiver.WATCH_INBOX / f"business-watch-{uuid.uuid4()}.zip"
        backlog_bundle.write_bytes(b"receiver backlog test")
        scan_during_backlog = {"called": False}

        def forbidden_backlog_scan(_root):
            scan_during_backlog["called"] = True
            raise AssertionError("watcher scanned while receiver backlog existed")

        backlog_planner = watcher.FolderWatcher(
            planning_config, 1024, mount_checker=lambda _source: True,
            scanner=forbidden_backlog_scan, capturer=lambda *_args: None, clock=lambda: 180,
        )
        check(
            "watcher receiver backlog blocks duplicate reconciliation",
            backlog_planner.cycle() == 15 and not scan_during_backlog["called"],
        )
        backlog_health = json.loads(watcher.HEALTH_PATH.read_text(encoding="utf-8"))
        check(
            "watcher backlog health remains running",
            backlog_health["status"] == "running"
            and backlog_health["detail"] == "receiver backlog"
            and backlog_health["pending_bundles"] >= 1,
        )
        backlog_bundle.unlink()

        # Delete confirmation is suppressed on reconnect, then requires two
        # consecutive complete online scans.  This uses a synthetic DB snapshot.
        planning_entry_id = str(uuid.uuid4())
        planning_old = {
            "source_entry_id": planning_entry_id, "current_relative_path": "gone.txt",
            "path_key": "gone.txt", "current_sha256": "a" * 64,
            "source_mtime_ns": 1_700_000_000_000_000_000,
            "source_modified_at": "2023-11-14T22:13:20Z", "size_bytes": 1,
            "version_number": 1, "storage_class": "obsidian",
            "version_ref": "external:win11-folder-watch/versions/x", "mirror_relative": None,
            "last_event_id": str(uuid.uuid4()),
        }
        planning_db = {"source_known": True, "last_event_id": planning_old["last_event_id"], "entries": {"gone.txt": planning_old}}
        planner.state["missing_counts"] = {}
        check("watcher reconnect first scan suppresses delete", planner._plan_and_queue(set(), {}, planning_db, True, False) == 0 and not planner.state["missing_counts"])
        check("watcher first online scan only marks missing", planner._plan_and_queue(set(), {}, planning_db, True, True) == 0 and planner.state["missing_counts"][planning_entry_id] == 1)
        check("watcher unstable reappearance clears missing", planner._plan_and_queue(
            {"gone.txt"}, {}, planning_db, True, True
        ) == 0 and planning_entry_id not in planner.state["missing_counts"])
        check("watcher absence after reappearance restarts count", planner._plan_and_queue(
            set(), {}, planning_db, True, True
        ) == 0 and planner.state["missing_counts"][planning_entry_id] == 1)
        planned_before = set(receiver.WATCH_INBOX.glob("business-watch-*.zip"))
        check("watcher second online scan queues tombstone", planner._plan_and_queue(set(), {}, planning_db, True, True) == 1)
        planned_after = set(receiver.WATCH_INBOX.glob("business-watch-*.zip")) - planned_before
        planned_delete_manifest = watcher.read_bundle_manifest(next(iter(planned_after)))
        check("watcher planned event is deleted", planned_delete_manifest["operation"] == "deleted" and planned_delete_manifest["items"] == [])

        same_meta_old_content = b"old!"
        same_meta_new_content = b"new!"
        same_meta_mtime = 1_700_000_005_123_456_789
        same_meta_file = synthetic_source / "same-meta.txt"
        same_meta_file.write_bytes(same_meta_new_content)
        os.utime(same_meta_file, ns=(same_meta_mtime, same_meta_mtime))
        same_meta_record = next(
            item for item in watcher._scan_tree(synthetic_source)
            if item["relative_path"] == "same-meta.txt"
        )
        same_meta_entry = str(uuid.uuid4())
        same_meta_old = {
            "source_entry_id": same_meta_entry, "current_relative_path": "same-meta.txt",
            "path_key": "same-meta.txt", "current_sha256": hashlib.sha256(same_meta_old_content).hexdigest(),
            "source_mtime_ns": same_meta_record["mtime_ns"],
            "source_modified_at": watcher.mtime_iso(same_meta_record["mtime_ns"]),
            "size_bytes": len(same_meta_old_content), "version_number": 1,
            "storage_class": "obsidian", "version_ref": "external:win11-folder-watch/versions/x",
            "mirror_relative": None, "last_event_id": str(uuid.uuid4()),
            "last_event_status": "stored", "transition_sequence": 1,
        }
        same_meta_database = {
            "source_known": True, "last_event_id": same_meta_old["last_event_id"],
            "entries": {"same-meta.txt": same_meta_old}, "path_generations": {"same-meta.txt": 1},
        }
        full_audit_planner = watcher.FolderWatcher(
            planning_config, 1024, mount_checker=lambda _source: True, clock=lambda: 0,
        )
        same_meta_before = set(receiver.WATCH_INBOX.glob("business-watch-*.zip"))
        check(
            "watcher full scan rehashes unchanged metadata",
            full_audit_planner._reconcile(
                [same_meta_record], {"same-meta.txt": same_meta_record},
                same_meta_database, True, True,
            ) == 1,
        )
        same_meta_package = next(iter(
            set(receiver.WATCH_INBOX.glob("business-watch-*.zip")) - same_meta_before
        ))
        same_meta_manifest = watcher.read_bundle_manifest(same_meta_package)
        check(
            "watcher full scan detects same-size same-mtime byte change",
            same_meta_manifest["operation"] == "modified"
            and same_meta_manifest["previous_sha256"] == hashlib.sha256(same_meta_old_content).hexdigest()
            and same_meta_manifest["current_sha256"] == hashlib.sha256(same_meta_new_content).hexdigest(),
        )

        stable_error_old = dict(
            planning_old,
            source_entry_id=str(uuid.uuid4()), current_relative_path="blocked.txt", path_key="blocked.txt",
            current_sha256=hashlib.sha256(b"1234567").hexdigest(), source_mtime_ns=1_700_000_010_000_000_000,
            source_modified_at=watcher.mtime_iso(1_700_000_010_000_000_000), size_bytes=7,
            last_event_status="capture_error:source_unreadable",
        )
        stable_error_current = {
            "relative_path": "blocked.txt", "path_key": "blocked.txt", "sha256": None,
            "size_bytes": 7, "mtime_ns": stable_error_old["source_mtime_ns"],
            "payload_path": None, "capture_error": "source_unreadable",
        }
        stable_error_db = {
            "source_known": True, "last_event_id": stable_error_old["last_event_id"],
            "entries": {"blocked.txt": stable_error_old},
        }
        error_packages_before = set(receiver.WATCH_INBOX.glob("business-watch-*.zip"))
        check("watcher stable unreadable item does not requeue duplicate", planner._plan_and_queue(
            {"blocked.txt"}, {"blocked.txt": stable_error_current}, stable_error_db, True, True
        ) == 0 and set(receiver.WATCH_INBOX.glob("business-watch-*.zip")) == error_packages_before)

        recovery_payload = watch_payloads / "planner-recovery.payload"
        recovery_payload.write_bytes(b"1234567")
        stable_recovery_current = dict(
            stable_error_current,
            sha256=stable_error_old["current_sha256"], payload_path=recovery_payload, capture_error=None,
        )
        recovery_packages_before = set(receiver.WATCH_INBOX.glob("business-watch-*.zip"))
        check("watcher retries capture-error state and queues recovery", planner._plan_and_queue(
            {"blocked.txt"}, {"blocked.txt": stable_recovery_current}, stable_error_db, False, True
        ) == 1)
        recovery_package = next(iter(set(receiver.WATCH_INBOX.glob("business-watch-*.zip")) - recovery_packages_before))
        check("watcher recovery carries same-hash payload", watcher.read_bundle_manifest(recovery_package)["items"][0]["sha256"] == stable_error_old["current_sha256"])

        case_entry_id = str(uuid.uuid4())
        case_old = dict(
            planning_old,
            source_entry_id=case_entry_id, current_relative_path="Case.txt", path_key="case.txt",
            current_sha256="c" * 64, source_mtime_ns=1_700_000_020_000_000_000,
            source_modified_at=watcher.mtime_iso(1_700_000_020_000_000_000), size_bytes=3,
            last_event_status="stored",
        )
        case_payload = watch_payloads / "case-readd.payload"
        case_payload.write_bytes(b"new")
        case_current = {
            "relative_path": "CASE.txt", "path_key": "case.txt",
            "sha256": hashlib.sha256(b"new").hexdigest(), "size_bytes": 3,
            "mtime_ns": 1_700_000_030_000_000_000, "payload_path": case_payload,
            "capture_error": None,
        }
        case_db = {"source_known": True, "last_event_id": case_old["last_event_id"], "entries": {"case.txt": case_old}}
        planner.state["missing_counts"].pop(case_entry_id, None)
        check("watcher rename-plus-content first confirms delete", planner._plan_and_queue(
            {"case.txt"}, {"case.txt": case_current}, case_db, True, True
        ) == 0 and planner.state["pending_readds"]["case.txt"]["relative_path"] == "CASE.txt")
        case_before_delete = set(receiver.WATCH_INBOX.glob("business-watch-*.zip"))
        check("watcher rename-plus-content queues delete", planner._plan_and_queue(
            {"case.txt"}, {"case.txt": case_current}, case_db, True, True
        ) == 1)
        case_delete_package = next(iter(set(receiver.WATCH_INBOX.glob("business-watch-*.zip")) - case_before_delete))
        check("watcher rename-plus-content is not false rename", watcher.read_bundle_manifest(case_delete_package)["operation"] == "deleted")
        case_after_delete_db = {
            "source_known": True,
            "last_event_id": watcher.read_bundle_manifest(case_delete_package)["event_id"],
            "entries": {}, "path_generations": {"case.txt": 1},
        }
        case_before_add = set(receiver.WATCH_INBOX.glob("business-watch-*.zip"))
        check("watcher rename-plus-content queues add after tombstone", planner._plan_and_queue(
            {"case.txt"}, {"case.txt": case_current}, case_after_delete_db, False, True
        ) == 1)
        case_add_package = next(iter(set(receiver.WATCH_INBOX.glob("business-watch-*.zip")) - case_before_add))
        check("watcher rename-plus-content second event is add", watcher.read_bundle_manifest(case_add_package)["operation"] == "added")

        lifecycle_database = watcher.read_database_state("source_b", receiver.DB_PATH)
        cycle_x_key = watcher.path_key("cycle-x.txt")
        cycle_y_key = watcher.path_key("cycle-y.txt")
        check(
            "watcher lifecycle ledger survives rename away",
            lifecycle_database["path_generations"].get(cycle_x_key) == 1
            and cycle_y_key in lifecycle_database["entries"],
        )
        lifecycle_planner = watcher.FolderWatcher(
            {**planning_config, "source_id": "source_b"}, 1024,
            mount_checker=lambda _source: True, scanner=lambda _root: [],
            capturer=lambda *_args: None, clock=lambda: 0,
        )
        lifecycle_old = lifecycle_database["entries"][cycle_y_key]
        lifecycle_existing = {
            "relative_path": lifecycle_old["current_relative_path"], "path_key": cycle_y_key,
            "sha256": lifecycle_old["current_sha256"], "size_bytes": lifecycle_old["size_bytes"],
            "mtime_ns": lifecycle_old["source_mtime_ns"], "payload_path": None,
            "capture_error": None,
        }
        lifecycle_payload = watch_payloads / "lifecycle-readd.payload"
        lifecycle_payload.write_bytes(cycle_content)
        lifecycle_readd = {
            "relative_path": "cycle-x.txt", "path_key": cycle_x_key, "sha256": cycle_sha,
            "size_bytes": len(cycle_content), "mtime_ns": 1_700_000_315_000_000_000,
            "payload_path": lifecycle_payload, "capture_error": None,
        }
        lifecycle_before = set(receiver.WATCH_INBOX.glob("business-watch-*.zip"))
        check(
            "watcher renamed-away path can begin second lifecycle",
            lifecycle_planner._plan_and_queue(
                {cycle_x_key, cycle_y_key},
                {cycle_x_key: lifecycle_readd, cycle_y_key: lifecycle_existing},
                lifecycle_database, False, True,
            ) == 1,
        )
        lifecycle_package = next(iter(
            set(receiver.WATCH_INBOX.glob("business-watch-*.zip")) - lifecycle_before
        ))
        lifecycle_manifest = watcher.read_bundle_manifest(lifecycle_package)
        expected_lifecycle_entry = watcher.deterministic_entry_id(
            "source_b", "cycle-x.txt", cycle_sha, 1_700_000_315_000_000_000, 2
        )
        check(
            "watcher second path lifecycle gets distinct entry id",
            lifecycle_manifest["source_entry_id"] == expected_lifecycle_entry
            and lifecycle_manifest["source_entry_id"] != cycle_entry,
        )
        receiver.process_watch_bundle(lifecycle_package, conn)
        check(
            "receiver accepts second lifecycle while renamed entry remains",
            conn.execute(
                "SELECT COUNT(*) FROM watch_entries WHERE source_id='source_b' AND active=1 "
                "AND path_key IN (?,?)", (cycle_x_key, cycle_y_key),
            ).fetchone()[0] == 2,
        )

        maintenance = conn.execute("SELECT CAST(value AS INTEGER) FROM maintenance_state WHERE key='index_dirty_generation'").fetchone()
        check("watch events mark indexes dirty", maintenance is not None and maintenance[0] >= 10)

        source_events_before_refresh = conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0]
        check("watch index refresh failure reported", receiver.refresh_dirty_indexes(conn, force=True) is False)
        applied_after_failure = conn.execute(
            "SELECT value FROM maintenance_state WHERE key='index_applied_generation'"
        ).fetchone()
        last_index_error = conn.execute(
            "SELECT value FROM maintenance_state WHERE key='index_last_error'"
        ).fetchone()
        check("watch index failure remains dirty", applied_after_failure is None and last_index_error and last_index_error[0])
        check("watch index failure does not roll back history", conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0] == source_events_before_refresh and version_one.exists())

        original_subprocess_run = receiver.subprocess.run

        class SuccessfulRefresh:
            returncode = 0
            stdout = ""
            stderr = ""

        cooldown_calls = []
        receiver.subprocess.run = lambda *args, **kwargs: cooldown_calls.append((args, kwargs))
        try:
            check(
                "watch index failure observes retry cooldown",
                receiver.refresh_dirty_indexes(conn, force=False) is False and not cooldown_calls,
            )
        finally:
            receiver.subprocess.run = original_subprocess_run

        refresh_calls = []
        refresh_timeouts = []

        def successful_refresh_with_midflight_event(args, **kwargs):
            refresh_calls.append(list(args))
            refresh_timeouts.append(kwargs.get("timeout"))
            if len(refresh_calls) == 1:
                with conn:
                    receiver.set_maintenance_value(
                        conn,
                        "index_dirty_generation",
                        str(maintenance[0] + 1),
                    )
            return SuccessfulRefresh()

        receiver.subprocess.run = successful_refresh_with_midflight_event
        try:
            check(
                "watch index dirty generation can be applied",
                receiver.refresh_dirty_indexes(conn, force=True) is True,
            )
            check(
                "watch index refresh defers status until generation commit",
                refresh_calls[0][-2:] == [
                    "--target-generation", str(maintenance[0]),
                ]
                and "--defer-status" in refresh_calls[0]
                and "--status-only" in refresh_calls[1],
            )
            check(
                "watch index refresh uses the bounded high-load timeout",
                refresh_timeouts == [receiver.INDEX_REFRESH_TIMEOUT_SECONDS] * 2,
            )
        finally:
            receiver.subprocess.run = original_subprocess_run
        applied_generation = conn.execute(
            "SELECT CAST(value AS INTEGER) FROM maintenance_state WHERE key='index_applied_generation'"
        ).fetchone()[0]
        current_dirty_generation = conn.execute(
            "SELECT CAST(value AS INTEGER) FROM maintenance_state WHERE key='index_dirty_generation'"
        ).fetchone()[0]
        check(
            "watch index records only the covered target generation",
            applied_generation == maintenance[0]
            and current_dirty_generation == maintenance[0] + 1,
        )
        check("watch index success records timestamp", bool(conn.execute(
            "SELECT value FROM maintenance_state WHERE key='index_last_refreshed_at'"
        ).fetchone()[0]))

        receiver.subprocess.run = lambda *_args, **_kwargs: SuccessfulRefresh()
        try:
            check(
                "watch index next generation converges independently",
                receiver.refresh_dirty_indexes(conn, force=True) is True,
            )
        finally:
            receiver.subprocess.run = original_subprocess_run
        check(
            "watch index convergence reaches the later generation",
            conn.execute(
                "SELECT value FROM maintenance_state WHERE key='index_applied_generation'"
            ).fetchone()[0]
            == str(current_dirty_generation),
        )

        receiver_lock = receiver.acquire_receiver_lock()
        try:
            try:
                receiver.acquire_receiver_lock()
                raise AssertionError("second receiver lock acquired")
            except RuntimeError:
                check("receiver duplicate instance rejected", True)
            check("receiver lock owner-only mode", stat.S_IMODE(receiver.RECEIVER_LOCK_PATH.stat().st_mode) == 0o600)
        finally:
            receiver_lock.close()
        receiver_lock_again = receiver.acquire_receiver_lock()
        receiver_lock_again.close()
        check("receiver lock released cleanly", True)
        memory_write_lock = receiver.acquire_memory_write_lock()
        try:
            check(
                "receiver shared memory write lock owner-only mode",
                stat.S_IMODE(receiver.MEMORY_WRITE_LOCK_PATH.stat().st_mode) == 0o600,
            )
        finally:
            memory_write_lock.close()
        check("receiver shared memory write lock released cleanly", True)

        watcher_spool = watcher.STATE_ROOT / "spool" / "active-capture"
        watcher_spool.mkdir(parents=True, exist_ok=True)
        watcher_spool_sentinel = watcher_spool / "payload.partial"
        watcher_spool_sentinel.write_bytes(b"active")
        watcher_lock = watcher.acquire_lock()
        watcher_health_before_duplicate = dict(watcher.HEALTH_STATE)
        try:
            try:
                watcher.initialize_runtime()
                raise AssertionError("second watcher acquired lock")
            except watcher.WatcherError as exc:
                check("watcher duplicate instance rejected", exc.code == "already_running")
            check(
                "watcher duplicate lock attempt leaves health counters unchanged",
                watcher.HEALTH_STATE == watcher_health_before_duplicate,
            )
            check(
                "watcher lock precedes crash spool recovery",
                watcher_spool_sentinel.exists(),
            )
            check(
                "watcher lock owner-only mode",
                stat.S_IMODE(watcher.LOCK_PATH.stat().st_mode) == 0o600,
            )
        finally:
            watcher.release_lock(watcher_lock)
        recovered_runtime_lock = watcher.initialize_runtime()
        try:
            check(
                "watcher locked crash recovery clears stale spool",
                not watcher_spool_sentinel.exists() and (watcher.STATE_ROOT / "spool").is_dir(),
            )
        finally:
            watcher.release_lock(recovered_runtime_lock)

        fork_context = multiprocessing.get_context("fork")
        inherited_ready = fork_context.Event()
        inherited_finish = fork_context.Event()

        def inherited_lock_child():
            watcher.close_inherited_lock_fd()
            inherited_ready.set()
            inherited_finish.wait(5)

        parent_watcher_lock = watcher.acquire_lock()
        inherited_process = fork_context.Process(target=inherited_lock_child)
        inherited_process.start()
        replacement_watcher_lock = None
        try:
            if not inherited_ready.wait(5):
                raise AssertionError("watcher child did not initialize")
            watcher.release_lock(parent_watcher_lock)
            parent_watcher_lock = None
            replacement_watcher_lock = watcher.acquire_lock()
            check(
                "watcher child closes inherited flock descriptor",
                inherited_process.is_alive(),
            )
        finally:
            if parent_watcher_lock is not None:
                watcher.release_lock(parent_watcher_lock)
            if replacement_watcher_lock is not None:
                watcher.release_lock(replacement_watcher_lock)
            inherited_finish.set()
            inherited_process.join(timeout=5)
            if inherited_process.is_alive():
                inherited_process.terminate()
                inherited_process.join(timeout=2)
        check("watcher inherited-lock test child exits", inherited_process.exitcode == 0)
        check("watch database quick check", conn.execute("PRAGMA quick_check").fetchone()[0] == "ok")

        counts = dict(conn.execute("SELECT storage_class,COUNT(*) FROM files GROUP BY storage_class"))
        result = {
            "verdict": "pass",
            "tests_passed": tests_passed,
            "classifier_cases": len(obsidian_samples) + len(external_samples) + len(quarantine_samples),
            "database_events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "database_files_by_class": counts,
            "boundaries": {
                "payload_execution": "not_performed",
                "nested_payload_unpack": "not_performed",
                "encrypted_detection": "zip_and_ooxml_central_flag_only",
                "deep_internal_integrity": "not_claimed",
                "vendor_dlp": "not_detected; access-denied files become metadata-only quarantine events",
                "smb_reparse": "real SMB reparse not exercised; symlink and local non-regular entries are skipped",
            },
        }
        conn.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
