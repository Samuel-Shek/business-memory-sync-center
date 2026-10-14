"""Read-only recovery policy. No provider calls, credentials or subscription changes."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import os
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# These codes describe retrieval, never semantic validation or subscription creation.
READ_CODES = frozenset({"chat_reconcile_incomplete", "chat_read_transient", "chat_read_unclassified",
                       "incremental_partial", "business_error", "dws_timeout", "chat_read_cooldown", "chat_checkpoint_busy"})


def scheduled_read_recovery(code):
    return any(code == known or code.endswith("_" + known) for known in READ_CODES)


def failure_evidence(payload, code="", detail=""):
    # Inspect error fields only: message bodies and identities must never become diagnostics.
    roots = [payload.get("error"), payload.get("failures")]
    values, retry_flags, delay = [str(code), str(detail)], [], 0
    stack = [x for x in roots if x is not None]
    for _ in range(100):
        if not stack:
            break
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item[:20])
        elif isinstance(item, dict):
            for key, val in item.items():
                if key in {"retryable", "retryableRead"} and isinstance(val, bool):
                    retry_flags.append(val)
                elif key in {"retry_after_seconds", "retryAfterSeconds"}:
                    try:
                        delay = max(delay, max(0, int(val)))
                    except (TypeError, ValueError, OverflowError):
                        pass
                elif key in {"next_retry_at", "nextRetryAt"}:
                    try:
                        due = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                        if due.tzinfo is not None:
                            delay = max(delay, max(0, math.ceil((due - datetime.now().astimezone()).total_seconds())))
                    except (TypeError, ValueError, OverflowError):
                        pass
                elif key in {"code", "reason", "category", "message", "error", "details", "failures"}:
                    if isinstance(val, (dict, list)):
                        stack.append(val)
                    elif isinstance(val, str):
                        values.append(val[:1000])
        elif isinstance(item, str):
            values.append(item[:1000])
    text = " ".join(values).lower()
    if any(x.lower() in {"auth", "authorization", "permission"} for x in values) or any(x in text for x in ("auth_", "_auth", "http 401", "http 403", "unauthorized", "forbidden", "permission", "oauth", "login", "token expired", "access denied", "authentication")):
        category = "chat_read_auth_required"
    elif code == "chat_read_blocked" or False in retry_flags or any(x in text for x in ("invalid argument", "unknown flag", "invalid parameter")):
        category = "chat_read_blocked"
    elif True in retry_flags or any(x in text for x in ("timeout", "timed out", "connection reset", "temporarily unavailable", "rate_limit", "too many requests")):
        category = "chat_read_transient"
    else:
        category = "chat_read_unclassified"
    digest = hashlib.sha256(json.dumps(roots, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # Export a bounded public classification, never a raw provider error string.
    source_code = next((label for needles, label in (
        (("http 429", "rate_limit", "too many requests"), "rate_limited"),
        (("keychain", "read dek"), "keychain_read"),
        (("timeout", "timed out", "deadline exceeded"), "timeout"),
        (("connection reset", "connection refused", "dns"), "connection_failure"),
        (("http 401", "unauthorized", "token expired"), "authentication"),
        (("http 403", "forbidden", "permission denied"), "permission"),
        (("invalid argument", "unknown flag", "invalid parameter"), "invalid_request"),
    ) if any(needle in text for needle in needles)), "unclassified")
    return {"error_code": category, "source_code": source_code,
            "retryable": True if True in retry_flags and False not in retry_flags else False if False in retry_flags else None,
            "retry_after_seconds": delay, "failure_sha256": digest}


def summarize_failures(failures):
    if not failures:
        return {}
    selected = next((x for x in failures if x["error_code"] in {"chat_read_auth_required", "chat_read_blocked"}), failures[0])
    value = dict(selected)
    delay = max(int(x.get("retry_after_seconds") or 0) for x in failures)
    value["retry_after_seconds"] = delay
    if delay:
        value["next_retry_at"] = (datetime.now().astimezone() + timedelta(seconds=delay)).isoformat(timespec="seconds")
    return value


def cooldown_active(path):
    try:
        row = json.loads(path.read_text())
        due = datetime.fromisoformat(row.get("read_recovery", {}).get("next_retry_at", ""))
        return due > datetime.now().astimezone()
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def listener_acceptance_view(config, live):
    """Episode-bound display evidence, independent of source-wide success times."""
    ready = _stamp(live.get('listener_ready_at'))
    if ready is None:
        return {}
    checked = _stamp(live.get('subscription_checked_at'))
    now = datetime.now().astimezone().timestamp()
    fresh = ready <= (checked or 0) <= now
    receipts = live.get('live_event_receipts') or {}
    scopes = {}
    for kind in ('direct', 'group'):
        stamp = _stamp((receipts.get(kind) or {}).get('received_at'))
        accepted = bool(fresh and ready <= (stamp or 0) <= checked
                        and (live.get('incoming_by_kind') or {}).get(kind) is True)
        scopes[kind] = {'accepted': accepted,
                        'accepted_at': receipts[kind]['received_at'] if accepted else None,
                        'received_events': None, 'older_message_events': None}
    count_status = 'unavailable'
    database = Path(config['external_root']) / 'dingtalk-chat/state/chat.sqlite'
    # Counts explain pending evidence; they never independently grant acceptance.
    if database.is_file():
        try:
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1) as connection:
                rows = connection.execute(
                    "SELECT e.event_kind,COUNT(*),SUM(CASE WHEN datetime(m.create_time)<datetime(?, 'localtime') THEN 1 ELSE 0 END) "
                    "FROM event_inbox e LEFT JOIN messages m ON m.message_id=e.message_id "
                    "WHERE e.received_at>=? GROUP BY e.event_kind",
                    (live['listener_ready_at'], live['listener_ready_at'])).fetchall()
            for value in scopes.values():
                value.update(received_events=0, older_message_events=0)
            for kind, count, older in rows:
                if kind in scopes:
                    scopes[kind].update(received_events=count, older_message_events=older)
            count_status = 'available'
        except (OSError, sqlite3.Error):
            pass
    subscription = live.get('subscription_verification')
    valid = fresh and live.get('subscriptions_valid') is True and subscription in {'server_listing', 'live_delivery'}
    passed_at = _stamp(live.get('live_acceptance_at'))
    passed = (live.get('live_acceptance') == 'passed' and valid and all(v['accepted'] for v in scopes.values())
              and ready <= (passed_at or 0) <= now)
    return {'ready_at': live['listener_ready_at'], 'checked_at': live.get('subscription_checked_at'),
            'evidence_current_episode': fresh, 'status': 'passed' if passed else 'pending',
            'scopes': scopes, 'event_counts_status': count_status,
            'subscription_verification': subscription if valid else 'pending',
            'server_subscription_listing': live.get('server_subscription_listing', 'unconfirmed'),
            'accepted_at': live.get('live_acceptance_at') if passed else None,
            'historical_accepted_at': live.get('live_acceptance_at') if passed_at and passed_at < ready else None}


def listener_view(config):
    """Keep original incident identity; decorate it with current, process-bound progress."""
    if not config.get("external_root"):
        return {}
    try:
        path = Path(config["external_root"]) / "dingtalk-chat/state/automation_status.json"
        live = json.loads(path.read_text())
        if live.get("listener_state") != "ready" or not all(live.get(k) for k in ("direct_ready", "group_ready", "listener_ready_at")):
            return {}
        pid = int(live.get("listener_pid") or 0)
        if pid <= 1:
            return {}
        os.kill(pid, 0)
        ready_at = str(live["listener_ready_at"])
        acceptance = listener_acceptance_view(config, live)
        if not acceptance:
            return {}
        received = {kind: acceptance['scopes'][kind]['accepted'] for kind in ('direct', 'group')}
        missing = ["私聊" if kind == "direct" else "群聊" for kind, seen in received.items() if not seen]
        reason = ("监听本轮真实消息验收已通过" if acceptance['status'] == 'passed' else
                  "监听运行中，等待" + "、".join(missing) + "新消息验收" if missing else
                  "监听已收到真实消息，等待订阅与完整验收")
        return {"current_reason": reason, "listener_state": "ready", "listener_ready_at": ready_at,
                "incoming_by_kind": received, "live_acceptance": live.get("live_acceptance"),
                "subscription_verification": live.get("subscription_verification"),
                "listener_acceptance": acceptance,
                "current_blocker": None if acceptance["status"] == "passed" else "live_acceptance_pending", "next_due": None,
                "impact": reason + "；历史补同步与业务记忆处理分别跟踪。",
                "recovery_boundary": "保持现有监听，自动核对真实私聊、群聊和订阅回执；无新消息不算故障，也不重建订阅。"}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
