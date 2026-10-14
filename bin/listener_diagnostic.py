"""An explicitly authorized single diagnostic session, separate from acceptance."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import queue
import re
import threading
import uuid
from datetime import datetime, timezone
import dingtalk_batch_contract as contract
import listener_manual_acceptance as manual
from listener_retry_budget import BudgetBlocked, parse_time


def sanitized_stderr(lines):
    """Bounded diagnostic stderr only; never pass event stdout to this helper."""
    result = []
    for raw in lines[:200]:
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw[:4000])
        # Verbose transport output can contain private payloads or credentials.
        if re.search(r"(?i)(token|secret|password|authorization|cookie|bearer|\bpayload\b|\bbody\b|\bcontent\b|\bsender\b|\bconversation\b)", line):
            line = "[private transport detail omitted]"
        else:
            line = re.sub(r"https?://[^\s\"'<>]+", "[url]", line)
            line = re.sub(_archive_path('/Users/example/\\s]+'), "/Users/example", line)
            line = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", line)
            line = re.sub(r"(?<!\w)(?:\+?\d[\d -]{9,}\d)(?!\w)", "[number]", line)
            line = re.sub(r"\b[A-Za-z0-9_+/=-]{24,}\b", "[opaque-id]", line)
            line = re.sub(r"[^\S\t ]|[\x00-\x1f\x7f]", " ", line)
        line = line[:1200]
        if line and (not result or result[-1] != line):
            result.append(line)
    return result[:60]


def diagnose(config, authorization, *, additional=False):
    import dingtalk_memory_automation as memory
    if not authorization:
        raise BudgetBlocked("event_diagnostic_not_authorized")
    lock = memory.acquire_lock(memory.LISTENER_LOCK)
    original, supplement = manual.diagnostic_paths(config)
    path = supplement if additional else original
    consumers = []
    stop = threading.Event()
    row = None
    try:
        if path.exists():
            raise BudgetBlocked("event_diagnostic_attempt_recorded")
        if additional and not original.is_file():
            raise BudgetBlocked("event_original_diagnostic_missing")
        if manual.diagnostic_provider_hold(config):
            raise BudgetBlocked("event_provider_hold")
        for kind in ("all-direct", "all-group"):
            prior = manual.legacy_path(config, kind)
            old = json.loads(prior.read_text()) if prior.is_file() else {}
            due = parse_time(old.get("next_retry_at"))
            if old.get("guard") or (due and due > datetime.now(timezone.utc)):
                raise BudgetBlocked("event_provider_hold")
        row = {"schema_version": 1, "action_id": str(uuid.uuid4()), "phase": "diagnostic",
               "state": "starting", "authorized_by": authorization, "started_at": manual.now(),
               "binding": manual.binding(config), "session_limit": 1,
               "automatic_budget_preserved": True, "scopes": {}}
        if additional:
            row.update(phase="additional_diagnostic", original_diagnostic_sha256=manual.fingerprint(original))
        contract.save(path, row)
        for kind in ("all-direct", "all-group"):
            consumer = memory.EventConsumer(config, kind, queue.Queue(), stop)
            consumer.diagnostic_verbose = True
            consumers.append(consumer)
            consumer.budget = manual.ManualBudget(config, kind, row)
            try:
                consumer.start_with_budget()
            except Exception as exc:
                row["error_code"] = getattr(exc, "code", type(exc).__name__)
                row["state"] = "failed"
                break
        else:
            row["state"] = "ready_diagnostic_only"
        return row
    finally:
        stop.set()
        for consumer in reversed(consumers):
            consumer.shutdown()
            for thread in consumer.threads:
                thread.join(timeout=1)
            if row:
                detail = memory.safe_event_failure(consumer.stderr_lines + consumer.pre_ready)
                detail.update(ready=consumer.ready.is_set(),
                              process_exit_code=consumer.process.poll() if consumer.process else None,
                              subscription_ref=consumer.subscription_ref)
                detail["sanitized_stderr"] = sanitized_stderr(consumer.stderr_lines)
                row["scopes"][consumer.kind] = detail
        if row:
            row.update(finished_at=manual.now(), cleanup_complete=all(
                c.process is None or c.process.poll() is not None for c in consumers))
            contract.save(path, row)
        lock.close()
