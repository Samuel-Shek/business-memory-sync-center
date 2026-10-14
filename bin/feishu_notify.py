#!/usr/bin/env python3
"""Send one plain-text Feishu message to an allowlisted group.

Extracted from email_sync.send_feishu_direct so other pipelines can report to
Example Owner without importing the whole mail module. The rules it keeps:

  * only a verified `oc_...` chat id from a target-map file is ever used;
  * credentials come from the feishubot profile's .env, never from arguments;
  * a caller-supplied dedupe key throttles repeats, so a failure that recurs
    every hour does not become an hourly message.

Delivery is best effort: a report that cannot be sent must never turn into a
second failure inside the pipeline that was already failing.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import fcntl
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "feishu-notify-state.json"
LOCK_PATH = ROOT / "state" / "feishu-notify.lock"
DEFAULT_TARGET_MAP = Path(_archive_path('/Users/example/.hermes/profiles/feishubot/cron/dingtalk_minutes_notice_target.json'))
DEFAULT_PROFILE_ROOT = Path(_archive_path('/Users/example/.hermes/profiles/feishubot'))
TARGET_ID_RE = re.compile(r"oc_[A-Za-z0-9_-]{16,}")
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
MAX_TEXT = 1500


def _env_value(path: Path, name: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _target_id(config: dict[str, Any]) -> str:
    routing = config.get("model_routing") or {}
    path = Path(str(routing.get("feishu_target_map") or DEFAULT_TARGET_MAP))
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    target = str(mapping.get("channel_id") or "") if isinstance(mapping, dict) else ""
    return target if TARGET_ID_RE.fullmatch(target) else ""


@contextmanager
def _state_locked():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data.get("sent"), dict):
            data["sent"] = {}
        yield data
        cutoff = time.time() - 30 * 86400
        data["sent"] = {key: value for key, value in data["sent"].items() if float(value or 0) >= cutoff}
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, STATE_PATH)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _throttled(dedupe_key: str, min_interval_seconds: float) -> bool:
    """True when an identical report already went out inside the window."""
    with _state_locked() as data:
        return time.time() - float(data["sent"].get(dedupe_key) or 0) < float(min_interval_seconds)


def _record_sent(dedupe_key: str) -> None:
    # Only a message Feishu accepted counts: a failed send must not silence
    # the next attempt for the whole window.
    with _state_locked() as data:
        data["sent"][dedupe_key] = time.time()


def _ssl_context() -> ssl.SSLContext:
    """Same trust-store lookup as email_sync.feishu_ssl_context, the proven sender."""
    for candidate in (Path("/etc/ssl/cert.pem"), Path("/opt/homebrew/etc/ca-certificates/cert.pem"),
                      Path("/opt/homebrew/etc/openssl@3/cert.pem")):
        if candidate.is_file():
            try:
                return ssl.create_default_context(cafile=str(candidate))
            except (OSError, ssl.SSLError):
                continue
    return ssl.create_default_context()


def send(config: dict[str, Any], text: str, *, dedupe_key: str,
         min_interval_seconds: float = 7200) -> dict[str, str]:
    target_id = _target_id(config)
    if not target_id:
        return {"status": "failed", "reason_code": "target_unavailable"}
    profile_root = Path(str((config.get("model_routing") or {}).get("feishu_profile_root") or DEFAULT_PROFILE_ROOT))
    app_id = os.environ.get("FEISHU_APP_ID") or _env_value(profile_root / ".env", "FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or _env_value(profile_root / ".env", "FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        return {"status": "failed", "reason_code": "credentials_unavailable"}
    try:
        if _throttled(dedupe_key, min_interval_seconds):
            return {"status": "skipped", "reason_code": "throttled"}
    except OSError:
        pass  # never let bookkeeping stop a real report
    context = _ssl_context()
    token_request = urllib.request.Request(
        TOKEN_URL, data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(token_request, timeout=15, context=context) as response:
            token = (json.load(response) or {}).get("tenant_access_token")
    except (TimeoutError, urllib.error.URLError, OSError, ValueError):
        return {"status": "failed", "reason_code": "token_request_failed"}
    if not token:
        return {"status": "failed", "reason_code": "token_unavailable"}
    body = {"receive_id": target_id, "msg_type": "text",
            "content": json.dumps({"text": str(text)[:MAX_TEXT]}, ensure_ascii=False)}
    request = urllib.request.Request(
        MESSAGE_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + str(token), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            payload = json.load(response)
    except urllib.error.HTTPError:
        return {"status": "failed", "reason_code": "feishu_rejected"}
    except (TimeoutError, urllib.error.URLError, OSError, ValueError):
        return {"status": "uncertain", "reason_code": "send_timeout_or_network"}
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return {"status": "failed", "reason_code": "feishu_rejected"}
    message_id = str(((payload.get("data") or {}) if isinstance(payload.get("data"), dict) else {}).get("message_id") or "")
    try:
        _record_sent(dedupe_key)
    except OSError:
        pass
    return {"status": "delivered" if message_id else "uncertain",
            "reason_code": "" if message_id else "message_id_missing"}
