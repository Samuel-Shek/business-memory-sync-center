#!/usr/bin/env python3
"""Shared, privacy-preserving helpers for the BUSINESS WeChat source layer."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import json
import os
import re
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any


os.umask(0o077)

RUN_STATES = {
    "enrollment_required",
    "collecting",
    "media_pending",
    "ready_for_ai",
    "paused_budget",
    "failed",
    "success",
}

SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(password|passwd|pwd|passcode|secret|token|appsecret|client[_-]?secret|"
    r"api[_-]?key|密码|口令|验证码|校验码)(\s*[:=：]\s*)([^\s,;，；]{4,})"
)
OTP_RE = re.compile(r"(?i)((?:验证码|校验码|otp|code)\s*(?:是|为|[:=：])?\s*)(\d{4,8})")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
PII_ASSIGNMENT_RE = re.compile(
    r"(?i)(身份证(?:号)?|社保(?:电脑)?号|公积金(?:账号|号)?|银行卡(?:号)?|银行账号|"
    r"手机号|手机号码)(\s*[:=：]?\s*)([A-Za-z0-9-]{6,32})"
)
STANDALONE_LONG_NUMBER_RE = re.compile(r"^\s*\d{8,24}\s*$")
CHINA_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
MOBILE_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
BANK_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){16,19}(?!\d)")
STANDALONE_SECRET_RE = re.compile(r"^\s*[A-Za-z0-9+/=_-]{32,256}\s*$")
HIGH_ENTROPY_PAIR_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9._-]{1,24})\s+(\S{8,64})\s*$")
URL_RE = re.compile(r"(?i)(?:https?|ftp|weixin|wechat)://[^\s<>'\"]+")
GROUP_SENDER_RE = re.compile(r"^([^\s:：]{2,128})[:：]\n(.*)$", re.DOTALL)
XML_UNSAFE_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


class WeChatSyncError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_hash(*parts: Any) -> str:
    return sha256_bytes(canonical_json([str(part) for part in parts]).encode("utf-8"))


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_metadata_log(path: Path, event: str, **fields: Any) -> None:
    ensure_private_dir(path.parent)
    safe_fields = {
        key: value
        for key, value in fields.items()
        if key not in {"text", "content", "conversation_id", "username", "display_name", "path", "key"}
    }
    record = {"at": now_iso(), "event": event, **safe_fields}
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (canonical_json(record) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def redact_text(value: Any, max_chars: int = 200_000) -> tuple[str, int]:
    text = value if isinstance(value, str) else ""
    redactions = 0

    private_key_hits = len(PRIVATE_KEY_RE.findall(text))
    if private_key_hits:
        text = PRIVATE_KEY_RE.sub("[私钥内容已脱敏]", text)
        redactions += private_key_hits

    jwt_hits = len(JWT_RE.findall(text))
    if jwt_hits:
        text = JWT_RE.sub("[Token已脱敏]", text)
        redactions += jwt_hits

    def assignment(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}{match.group(2)}[已脱敏]"

    def otp(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}[已脱敏]"

    def pii(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}{match.group(2)}[已脱敏]"

    text = SENSITIVE_ASSIGNMENT_RE.sub(assignment, text)
    text = OTP_RE.sub(otp, text)
    text = PII_ASSIGNMENT_RE.sub(pii, text)

    for pattern, marker in (
        (CHINA_ID_RE, "[身份证号已脱敏]"),
        (MOBILE_PHONE_RE, "[手机号已脱敏]"),
        (BANK_CARD_RE, "[银行卡号已脱敏]"),
    ):
        hits = len(pattern.findall(text))
        if hits:
            text = pattern.sub(marker, text)
            redactions += hits

    if STANDALONE_LONG_NUMBER_RE.fullmatch(text):
        text = "[长号码已脱敏]"
        redactions += 1

    if STANDALONE_SECRET_RE.fullmatch(text) and any(character.isdigit() for character in text) and any(character.isalpha() for character in text):
        text = "[疑似密钥或Token已脱敏]"
        redactions += 1

    pair = HIGH_ENTROPY_PAIR_RE.fullmatch(text)
    if pair:
        candidate = pair.group(2)
        classes = sum(
            bool(pattern.search(candidate))
            for pattern in (
                re.compile(r"[A-Z]"),
                re.compile(r"[a-z]"),
                re.compile(r"\d"),
                re.compile(r"[^A-Za-z0-9]"),
            )
        )
        if classes >= 3:
            text = f"{pair.group(1)} [疑似口令已脱敏]"
            redactions += 1

    url_hits = len(URL_RE.findall(text))
    if url_hits:
        text = URL_RE.sub("[链接已省略]", text)
        redactions += url_hits

    if len(text) > max_chars:
        text = text[:max_chars] + "\n[超长消息已截断；原文及其指纹未保留]"
        redactions += 1
    return text, redactions


def normalize_group_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\s\-‐‑‒–—―]+", "", normalized)


def group_matches(value: str, aliases: list[str], excludes: list[str]) -> bool:
    normalized = normalize_group_name(value)
    if any(normalize_group_name(item) in normalized for item in excludes):
        return False
    return any(normalize_group_name(item) in normalized for item in aliases)


def extract_pb_field_30(data: Any) -> str | None:
    """Extract the comma-separated WeChat contact label IDs from protobuf field 30."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    position = 0
    while position < len(raw):
        tag = 0
        shift = 0
        while position < len(raw):
            byte = raw[position]
            position += 1
            tag |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
            if shift > 63:
                return None
        field_number = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:
            while position < len(raw) and raw[position] & 0x80:
                position += 1
            position += 1
        elif wire_type == 1:
            position += 8
        elif wire_type == 5:
            position += 4
        elif wire_type == 2:
            length = 0
            shift = 0
            while position < len(raw):
                byte = raw[position]
                position += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            end = position + length
            if end > len(raw):
                return None
            if field_number == 30:
                try:
                    return raw[position:end].decode("utf-8")
                except UnicodeDecodeError:
                    return None
            position = end
        else:
            return None
    return None


def decode_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        for encoding in ("utf-8", "utf-16-le", "latin-1"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
    return ""


def split_message_type(local_type: Any) -> tuple[int, int]:
    try:
        raw = int(local_type)
    except (TypeError, ValueError):
        return 0, 0
    return raw & 0xFFFFFFFF, (raw >> 32) & 0xFFFFFFFF


def classify_message(local_type: Any) -> str:
    base_type, _sub_type = split_message_type(local_type)
    return {
        1: "text",
        3: "image",
        34: "voice",
        42: "contact_card",
        43: "video",
        47: "emoji",
        48: "location",
        49: "app",
        50: "call",
        10000: "system",
        10002: "system",
    }.get(base_type, f"type_{base_type}")


def parse_safe_xml(content: str, max_chars: int = 200_000) -> ET.Element | None:
    if not content or len(content) > max_chars or XML_UNSAFE_RE.search(content):
        return None
    start = content.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(content[start:])
    except ET.ParseError:
        return None


def _first_text(root: ET.Element, paths: tuple[str, ...]) -> str:
    for path in paths:
        value = root.findtext(path)
        if value and value.strip():
            return re.sub(r"\s+", " ", value).strip()
    return ""


def render_message_content(local_type: Any, raw_content: Any, is_group: bool) -> tuple[str, str, dict[str, Any]]:
    content = decode_content(raw_content)
    sender_hint = ""
    if is_group:
        match = GROUP_SENDER_RE.match(content)
        if match:
            sender_hint = match.group(1)
            content = match.group(2)

    kind = classify_message(local_type)
    metadata: dict[str, Any] = {}
    rendered = content

    root = parse_safe_xml(content)
    if kind == "text":
        rendered = content
    elif kind == "location" and root is not None:
        location = root.find(".//location")
        if location is not None:
            name = location.attrib.get("poiname") or location.attrib.get("label") or ""
            rendered = f"[位置] {name}".strip()
        else:
            rendered = "[位置]"
    elif kind == "app" and root is not None:
        title = _first_text(root, (".//appmsg/title", ".//title"))
        description = _first_text(root, (".//appmsg/des", ".//des"))
        file_extension = _first_text(root, (".//appmsg/appattach/fileext", ".//appattach/fileext"))
        attachment_name = _first_text(root, (".//appmsg/appattach/filename", ".//appattach/filename"))
        app_type = _first_text(root, (".//appmsg/type", ".//type"))
        md5 = _first_text(root, (".//appattach/md5", ".//md5"))
        if not attachment_name and app_type == "6":
            attachment_name = title
        if file_extension:
            metadata["file_extension"] = file_extension[:32]
        if attachment_name:
            metadata["attachment_name_sha256"] = sha256_bytes(attachment_name.casefold().encode("utf-8"))
        if md5 and re.fullmatch(r"[0-9a-fA-F]{32,64}", md5):
            metadata["content_digest"] = md5.lower()
        if app_type:
            metadata["app_type"] = app_type[:16]
        if app_type == "57":
            quoted = _first_text(root, (".//refermsg/content",))
            quoted_sender = _first_text(root, (".//refermsg/displayname",))
            quoted_id = _first_text(root, (".//refermsg/svrid",))
            safe_quoted, quoted_redactions = redact_text(quoted, max_chars=500)
            safe_sender, sender_redactions = redact_text(quoted_sender, max_chars=120)
            if quoted_id:
                metadata["quoted_message_id_sha256"] = sha256_bytes(quoted_id.encode("utf-8"))
            if safe_sender:
                metadata["quoted_sender"] = safe_sender
            if safe_quoted:
                metadata["quoted_excerpt"] = safe_quoted
            metadata["redaction_count"] = quoted_redactions + sender_redactions
            rendered = " ".join(part for part in ("[回复]", title, description) if part)
        else:
            rendered = " ".join(part for part in ("[应用消息]", title, description) if part)
    elif kind in {"image", "voice", "video", "emoji"}:
        rendered = f"[{kind}]"
        if root is not None:
            md5 = _first_text(root, (".//md5", ".//cdnmd5", ".//filemd5"))
            if not md5:
                for tag in (".//img", ".//emoji", ".//videomsg", ".//voicemsg"):
                    element = root.find(tag)
                    if element is None:
                        continue
                    md5 = element.attrib.get("md5") or element.attrib.get("cdnmd5") or element.attrib.get("filemd5") or ""
                    if md5:
                        break
            if md5 and re.fullmatch(r"[0-9a-fA-F]{32,64}", md5):
                metadata["content_digest"] = md5.lower()
    elif kind == "contact_card" and root is not None:
        nickname = root.attrib.get("nickname") or root.attrib.get("alias") or ""
        rendered = f"[联系人名片] {nickname}".strip()
    elif kind == "system":
        rendered = f"[系统消息] {content}"
    else:
        rendered = f"[{kind}] {content}".strip()

    rendered, redactions = redact_text(rendered)
    metadata["redaction_count"] = int(metadata.get("redaction_count", 0)) + redactions
    return sender_hint, rendered, metadata


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_count = sum(ord(character) < 128 for character in text)
    non_ascii_count = len(text) - ascii_count
    return max(1, (ascii_count + 3) // 4 + (non_ascii_count + 1) // 2)
