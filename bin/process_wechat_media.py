#!/usr/bin/env python3
"""Process only locally present WeChat media for the private BUSINESS source layer."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from collect_wechat_chat import (
    app_receipt,
    discover_active_account_root,
    get_wechat_pid,
    keychain_fetch,
    read_enrollment,
    refresh_ai_batches,
    require_filevault_enabled,
    require_sip_enabled,
)
from wechat_common import (
    WeChatSyncError,
    append_metadata_log,
    atomic_write,
    ensure_private_dir,
    now_iso,
    redact_text,
    sha256_bytes,
    stable_hash,
)


os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"
LOCK = ROOT / "state" / "wechat_media.lock"
MAX_ASSET_BYTES = 1024 * 1024 * 1024
MAX_SEARCH_FILES = 50_000
SAFE_DOCUMENTS = {".pdf", ".docx", ".xlsx", ".pptx"}
QUARANTINE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".dmg", ".pkg",
    ".app", ".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh", ".js", ".jar",
    ".docm", ".xlsm", ".pptm", ".doc", ".xls", ".ppt", ".dot", ".xlt", ".pot", ".pps",
}
IMAGE_MAGIC = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"GIF8": ".gif",
    b"BM": ".bmp",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process local WeChat media")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--fixture-account-root", type=Path)
    parser.add_argument("--limit", type=int, default=100)
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("invalid_config", "cannot read WeChat media configuration") from exc
    if not isinstance(config.get("wechat_chat"), dict):
        raise WeChatSyncError("invalid_config", "wechat_chat config is missing")
    return config


def acquire_lock() -> Any:
    ensure_private_dir(LOCK.parent)
    handle = LOCK.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise WeChatSyncError("already_running", "another WeChat media job is active") from exc
    return handle


def connect_store(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise WeChatSyncError("source_store_missing", "WeChat source database has not been created")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def production_context(config: dict[str, Any], source_root: Path) -> tuple[Path, dict[str, Any]]:
    require_sip_enabled()
    require_filevault_enabled()
    account_root = discover_active_account_root(get_wechat_pid())
    fingerprint = sha256_bytes(account_root.name.encode("utf-8"))
    enrollment = read_enrollment(
        source_root / "state" / "enrollment.json", fingerprint, app_receipt()
    )
    return account_root, enrollment


def safe_locator(value: str) -> str:
    return value if re.fullmatch(r"[0-9a-f]{32,64}", value or "") else ""


def candidate_roots(account_root: Path, media_type: str, locator: dict[str, Any]) -> list[Path]:
    chat_hash = str(locator.get("chat_path_hash", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", chat_hash):
        return []
    try:
        month = datetime.fromtimestamp(int(locator.get("create_time", 0))).astimezone().strftime("%Y-%m")
    except (TypeError, ValueError, OSError):
        month = ""
    msg = account_root / "msg"
    roots: list[Path] = []
    if media_type in {"image", "emoji"}:
        roots.extend((msg / "attach" / chat_hash / month / "Img", msg / "attach" / chat_hash / month))
    elif media_type == "video":
        roots.extend((msg / "video" / month, msg / "video" / chat_hash / month, msg / "attach" / chat_hash / month / "Video"))
    elif media_type == "app":
        roots.extend((msg / "file" / month, msg / "file" / chat_hash / month, msg / "attach" / chat_hash / month / "File"))
    return [root for root in roots if root.is_dir() and not root.is_symlink()]


def resolve_local_source(account_root: Path, media_type: str, locator: dict[str, Any]) -> Path | None:
    digest = safe_locator(str(locator.get("content_digest", "")))
    name_hash = safe_locator(str(locator.get("attachment_name_sha256", "")))
    if not digest and not name_hash:
        return None
    roots = candidate_roots(account_root, media_type, locator)
    examined = 0
    for root in roots:
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
            for name in files:
                examined += 1
                if examined > MAX_SEARCH_FILES:
                    return None
                candidate = Path(directory) / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                lower = name.casefold()
                matches_digest = bool(digest and digest in lower)
                matches_name = bool(
                    name_hash and sha256_bytes(name.casefold().encode("utf-8")) == name_hash
                )
                if not matches_digest and not matches_name:
                    continue
                if 0 < candidate.stat().st_size <= MAX_ASSET_BYTES:
                    return candidate
    return None


def copy_private(source: Path, destination: Path) -> None:
    ensure_private_dir(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(source_handle, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_version(command: Path, arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            [str(command), *arguments], text=True, capture_output=True, check=False, timeout=20
        )
        first = (result.stdout or result.stderr).splitlines()
        return first[0][:160] if first else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def read_prefix(path: Path, length: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(length)


def image_extension(path: Path) -> str:
    header = read_prefix(path, 16)
    for magic, extension in IMAGE_MAGIC.items():
        if header.startswith(magic):
            return extension
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif"
    return ""


def image_records(enrollment: dict[str, Any], cipher_block: bytes) -> list[dict[str, Any]]:
    block = cipher_block.hex()
    records = [
        item for item in enrollment.get("records", [])
        if isinstance(item, dict) and item.get("record_kind") == "image_aes"
    ]
    records.sort(key=lambda item: item.get("salt_hex") != block)
    return records


def decode_image(
    source: Path,
    asset_dir: Path,
    config: dict[str, Any],
    enrollment: dict[str, Any],
) -> tuple[Path | None, str]:
    direct_extension = image_extension(source)
    if direct_extension:
        destination = asset_dir / ("decoded" + direct_extension)
        copy_private(source, destination)
        return destination, ""
    header = read_prefix(source, 31)
    decoder = Path(config["wechat_chat"]["image_decoder"])
    if not decoder.is_file():
        return None, "image_decoder_missing"
    output = asset_dir / "decoded.image"
    if output.exists():
        output.unlink()
    candidate_records: list[dict[str, Any] | None] = [None]
    if header.startswith(b"\x07\x08V2\x08\x07"):
        if len(header) < 31:
            return None, "image_header_invalid"
        candidate_records = image_records(enrollment, header[15:31])
        if not candidate_records:
            return None, "image_aes_key_not_enrolled"
    decoded = False
    for record in candidate_records:
        if output.exists():
            output.unlink()
        input_bytes = b""
        if record is not None:
            input_bytes = keychain_fetch(
                Path(config["wechat_chat"]["keychain_helper"]), str(record.get("key_id", "")), 16
            )
        result = subprocess.run(
            [str(decoder), str(source), str(output)],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=180,
            env={"PATH": "/usr/bin:/bin"},
        )
        if input_bytes:
            secret = bytearray(input_bytes)
            for index in range(len(secret)):
                secret[index] = 0
        if result.returncode == 0 and output.is_file():
            decoded = True
            break
    if not decoded:
        return None, "image_decode_failed"
    extension = image_extension(output)
    if not extension:
        return None, "image_format_unknown"
    destination = asset_dir / ("decoded" + extension)
    os.replace(output, destination)
    destination.chmod(0o600)
    return destination, ""


def add_derivation(
    connection: sqlite3.Connection,
    asset_id: str,
    kind: str,
    tool: str,
    version: str,
    text: str,
    confidence: float | None,
    status: str,
) -> None:
    redacted, redactions = redact_text(text)
    if redactions and status == "success":
        status = "review_required"
    result_hash = sha256_bytes(redacted.encode("utf-8"))
    derivation_id = stable_hash(asset_id, kind, tool, version, result_hash)
    connection.execute(
        """
        INSERT INTO media_derivations(
          derivation_id,asset_id,derivation_type,tool_name,tool_version,
          result_text,result_sha256,confidence,status,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(derivation_id) DO NOTHING
        """,
        (derivation_id, asset_id, kind, tool, version, redacted, result_hash, confidence, status, now_iso()),
    )


def run_ocr(connection: sqlite3.Connection, asset_id: str, image: Path, config: dict[str, Any]) -> bool:
    tesseract = Path(config["wechat_chat"]["tesseract_bin"])
    if not tesseract.is_file():
        return False
    languages = str(config["wechat_chat"].get("tesseract_languages", "chi_sim+eng"))
    result = subprocess.run(
        [str(tesseract), str(image), "stdout", "-l", languages, "--psm", "6"],
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
    )
    if result.returncode != 0:
        return False
    text = result.stdout.strip()
    add_derivation(
        connection, asset_id, "ocr", "tesseract",
        command_version(tesseract, ["--version"]), text or "[未识别到文字]",
        0.75 if text else 0.4, "success" if text else "review_required",
    )
    return bool(text)


def ffprobe_duration(path: Path, ffprobe: Path) -> float | None:
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
        text=True, capture_output=True, check=False, timeout=60,
    )
    try:
        return float(result.stdout.strip()) if result.returncode == 0 else None
    except ValueError:
        return None


def transcribe_audio(
    connection: sqlite3.Connection, asset_id: str, source: Path, config: dict[str, Any]
) -> tuple[bool, float | None]:
    import video_hold
    video_hold.guard('wechat',{'asset_id':asset_id},name=Path(source).name,existing=True)
    chat = config["wechat_chat"]
    ffmpeg = Path(chat["ffmpeg_bin"])
    ffprobe = Path(chat["ffprobe_bin"])
    whisper = Path(chat["whisper_bin"])
    model = Path(chat["whisper_model"])
    if not all(path.is_file() for path in (ffmpeg, ffprobe, whisper, model)):
        return False, None
    duration = ffprobe_duration(source, ffprobe)
    used_silk = False
    with tempfile.TemporaryDirectory(prefix="business-wechat-audio-") as temporary:
        temp = Path(temporary)
        wav = temp / "audio.wav"
        converted = subprocess.run(
            [str(ffmpeg), "-nostdin", "-v", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
            capture_output=True, check=False, timeout=900,
        )
        if converted.returncode != 0 or not wav.is_file():
            node = Path(chat.get("node_bin", ""))
            silk_decoder = Path(chat.get("silk_decoder", ""))
            sample_rate = int(chat.get("silk_sample_rate", 24000))
            if not node.is_file() or not silk_decoder.is_file():
                return False, duration
            pcm = temp / "audio.pcm"
            silk = subprocess.run(
                [str(node), str(silk_decoder), str(source), str(pcm), str(sample_rate)],
                text=True,
                capture_output=True,
                check=False,
                timeout=900,
                env={"PATH": "/usr/bin:/bin", "NODE_NO_WARNINGS": "1"},
            )
            if silk.returncode != 0 or not pcm.is_file():
                return False, duration
            try:
                silk_receipt = json.loads(silk.stdout)
                duration = float(silk_receipt.get("duration_ms", 0)) / 1000.0 or duration
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            converted = subprocess.run(
                [str(ffmpeg), "-nostdin", "-v", "error", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", str(pcm), "-ac", "1", "-ar", "16000", str(wav)],
                capture_output=True,
                check=False,
                timeout=900,
            )
            if converted.returncode != 0 or not wav.is_file():
                return False, duration
            used_silk = True
        prefix = temp / "transcript"
        result = subprocess.run(
            [str(whisper), "-m", str(model), "-f", str(wav), "-l", "auto", "-otxt", "-of", str(prefix), "-np"],
            capture_output=True, check=False, timeout=3600,
        )
        transcript = prefix.with_suffix(".txt")
        if result.returncode != 0 or not transcript.is_file():
            return False, duration
        text = transcript.read_text(encoding="utf-8", errors="replace").strip()
    add_derivation(
        connection, asset_id, "transcript", "silk-wasm+whisper.cpp" if used_silk else "whisper.cpp",
        ("silk-wasm 3.7.1; " if used_silk else "") + command_version(whisper, ["--version"]), text or "[未识别到语音]",
        0.72 if text else 0.35, "review_required",
    )
    return True, duration


def process_video(
    connection: sqlite3.Connection, asset_id: str, source: Path, config: dict[str, Any]
) -> tuple[bool, float | None]:
    import video_hold
    video_hold.guard('wechat',{'asset_id':asset_id},name=Path(source).name,media_type='video',existing=True)
    chat = config["wechat_chat"]
    ffmpeg = Path(chat["ffmpeg_bin"])
    ffprobe = Path(chat["ffprobe_bin"])
    duration = ffprobe_duration(source, ffprobe) if ffprobe.is_file() else None
    audio_ok, _ = transcribe_audio(connection, asset_id, source, config)
    if not ffmpeg.is_file():
        return audio_ok, duration
    ocr_parts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="business-wechat-video-") as temporary:
        frames = Path(temporary)
        interval = max(1.0, (duration or 30.0) / 6.0)
        result = subprocess.run(
            [str(ffmpeg), "-nostdin", "-v", "error", "-i", str(source), "-vf", f"fps=1/{interval:.3f}", "-frames:v", "6", str(frames / "frame-%02d.jpg")],
            capture_output=True, check=False, timeout=900,
        )
        if result.returncode == 0:
            for frame in sorted(frames.glob("frame-*.jpg")):
                before = connection.total_changes
                run_ocr(connection, asset_id, frame, config)
                if connection.total_changes > before:
                    row = connection.execute(
                        "SELECT result_text FROM media_derivations WHERE asset_id=? AND derivation_type='ocr' ORDER BY created_at DESC LIMIT 1",
                        (asset_id,),
                    ).fetchone()
                    if row:
                        ocr_parts.append(str(row[0]))
    if ocr_parts:
        add_derivation(connection, asset_id, "video_keyframe_ocr", "ffmpeg+tesseract", "bounded-6-frames", "\n".join(ocr_parts), 0.6, "review_required")
    return audio_ok or bool(ocr_parts), duration


def extract_document(
    connection: sqlite3.Connection, asset_id: str, source: Path, config: dict[str, Any]
) -> tuple[bool, str]:
    python = Path(config["wechat_chat"]["document_python"])
    extractor = ROOT / "bin" / "extract_wechat_document.py"
    if not python.is_file():
        return False, "document_python_missing"
    result = subprocess.run(
        [str(python), str(extractor), str(source)], text=True, capture_output=True,
        check=False, timeout=900, env={"PATH": "/usr/bin:/bin"},
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, "document_parse_failed"
    if result.returncode != 0 or payload.get("status") != "success":
        return False, str(payload.get("error_code", "document_parse_failed"))
    metadata = {key: value for key, value in payload.items() if key not in {"status", "text"}}
    text = str(payload.get("text", ""))
    add_derivation(connection, asset_id, "document_text", "passive-office-extractor", "1", json.dumps(metadata, ensure_ascii=False) + "\n" + text, 0.9, "success")
    return True, ""


def update_asset(
    connection: sqlite3.Connection, asset_id: str, path: Path | None, status: str,
    error: str = "", duration: float | None = None,
) -> None:
    digest = file_sha256(path) if path and path.is_file() else ""
    size = path.stat().st_size if path and path.is_file() else 0
    connection.execute(
        "UPDATE media_assets SET local_path=?,content_sha256=?,byte_size=?,duration_seconds=?,status=?,error_code=?,updated_at=? WHERE asset_id=?",
        (str(path) if path else "", digest, size, duration, status, error, now_iso(), asset_id),
    )


import video_hold

def process_asset(
    connection: sqlite3.Connection, row: sqlite3.Row, account_root: Path,
    enrollment: dict[str, Any], config: dict[str, Any], source_root: Path,
) -> str:
    asset_id = str(row["asset_id"])
    media_type = str(row["media_type"])
    try:
        locator = json.loads(str(row["locator_json"]))
    except json.JSONDecodeError:
        update_asset(connection, asset_id, None, "failed", "locator_invalid")
        return "failed"
    existing = Path(str(row["local_path"])) if row["local_path"] else None
    hold_name=str(locator.get('name') or locator.get('filename') or (existing.name if existing else ''))
    hold_kind=video_hold.classify(hold_name, media_type=media_type)
    if hold_kind:
        video_hold.register('wechat', {'asset_id':asset_id},name=hold_name or ('视频' if media_type=='video' else '附件'),
            version=str(row['content_sha256'] or '') or None,existing=bool(existing and existing.is_file()),kind=hold_kind)
        connection.execute("UPDATE media_assets SET error_code='video_waiting_instruction',updated_at=? WHERE asset_id=?",(now_iso(),asset_id))
        return 'waiting_instruction' 
    source = existing if existing and existing.is_file() else resolve_local_source(account_root, media_type, locator)
    if source is None:
        update_asset(connection, asset_id, None, "unavailable", "local_media_missing")
        return "unavailable"
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_ASSET_BYTES:
        update_asset(connection, asset_id, None, "quarantined", "media_invalid_or_too_large")
        return "quarantined"
    video_hold.guard('wechat',{'asset_id':asset_id},name=source.name,media_type=media_type,existing=True)
    asset_dir = source_root / "media" / asset_id
    ensure_private_dir(asset_dir)
    if existing and existing == source:
        local = source
    else:
        extension = source.suffix.casefold()[:16] if re.fullmatch(r"\.[a-zA-Z0-9]{1,15}", source.suffix) else ".bin"
        local = asset_dir / ("source" + extension)
        copy_private(source, local)

    if local.suffix.casefold() in QUARANTINE_EXTENSIONS:
        quarantine = source_root / "quarantine" / asset_id / local.name
        copy_private(local, quarantine)
        update_asset(connection, asset_id, quarantine, "quarantined", "active_or_archive_content")
        return "quarantined"

    video_hold.inspect_container(local,'wechat',{'asset_id':asset_id},version=file_sha256(local),parent=hold_name)
    processed = False
    duration: float | None = None
    error = ""
    if media_type in {"image", "emoji"}:
        decoded, error = decode_image(local, asset_dir, config, enrollment)
        if decoded:
            ocr_ok = run_ocr(connection, asset_id, decoded, config)
            add_derivation(connection, asset_id, "image_metadata", "local-image-inspector", "1", f"format={decoded.suffix.lstrip('.')} bytes={decoded.stat().st_size}", 1.0, "success")
            if not ocr_ok:
                add_derivation(
                    connection, asset_id, "visual_summary", "local-image-inspector", "1",
                    "[未识别到可用文字；画面语义需要人工或经用量批准的视觉模型复核]",
                    0.2, "review_required",
                )
            processed = True
            local = decoded
    elif media_type == "voice":
        processed, duration = transcribe_audio(connection, asset_id, local, config)
        if not processed:
            error = "voice_decode_or_transcription_failed"
    elif media_type == "video":
        processed, duration = process_video(connection, asset_id, local, config)
        if not processed:
            error = "video_derivation_failed"
    elif media_type == "app":
        extension = local.suffix.casefold()
        if extension in SAFE_DOCUMENTS:
            processed, error = extract_document(connection, asset_id, local, config)
            if not processed:
                if error in {
                    "archive_entry_limit_exceeded", "archive_path_invalid", "archive_encrypted",
                    "archive_size_limit_exceeded", "active_content_blocked",
                }:
                    quarantine = source_root / "quarantine" / asset_id / local.name
                    copy_private(local, quarantine)
                    update_asset(connection, asset_id, quarantine, "quarantined", error)
                    return "quarantined"
        else:
            error = "structured_message_or_unsupported_attachment"
    update_asset(connection, asset_id, local, "processed" if processed else "failed", error, duration)
    return "processed" if processed else "failed"


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = load_config(args.config)
    source_root = Path(config["external_root"]) / "wechat-chat"
    ensure_private_dir(source_root)
    for child in ("state", "logs", "media", "quarantine"):
        ensure_private_dir(source_root / child)
    if args.fixture_account_root:
        account_root = args.fixture_account_root.resolve()
        enrollment: dict[str, Any] = {"records": []}
    else:
        account_root, enrollment = production_context(config, source_root)
    connection = connect_store(source_root / "state" / "chat.sqlite")
    counts = {"waiting_instruction": 0, "processed": 0, "failed": 0, "unavailable": 0, "quarantined": 0}
    try:
        rows = connection.execute(
            "SELECT * FROM media_assets WHERE status IN ('queued','resolved','failed') AND COALESCE(error_code,'') NOT IN ('video_waiting_instruction','media_type_waiting_confirmation') ORDER BY first_seen_at LIMIT ?",
            (max(1, min(args.limit, 1000)),),
        ).fetchall()
        for row in rows:
            outcome = process_asset(connection, row, account_root, enrollment, config, source_root)
            counts[outcome] += 1
            connection.commit()
        ai_stats = refresh_ai_batches(
            connection, int(config["wechat_chat"].get("approved_ai_input_tokens", 0)), now_iso()
        )
        connection.commit()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        connection.close()
    receipt = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "status": "success" if not counts["failed"] else "failed",
        "items_seen": sum(counts.values()),
        **counts,
        **ai_stats,
        "database_quick_check": quick_check,
    }
    atomic_write(source_root / "state" / "media_status.json", json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    append_metadata_log(source_root / "logs" / "media.jsonl", "media_finished", **receipt)
    return receipt, 0 if not counts["failed"] else 2


def main() -> int:
    args = arguments()
    lock = None
    try:
        lock = acquire_lock()
        result, code = run(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return code
    except WeChatSyncError as exc:
        print(json.dumps({"status": "failed", "error_code": exc.code, "detail": exc.message}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "internal_error", "detail": "media processing stopped safely"}, ensure_ascii=False))
        return 2
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
