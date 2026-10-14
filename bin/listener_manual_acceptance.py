"""One separately authorized acceptance session; never resets host budgets."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
import dingtalk_batch_contract as contract
from listener_retry_budget import BudgetBlocked, ListenerBudget, parse_time


def now():
    return datetime.now(timezone.utc).isoformat()


def directory(config):
    import dingtalk_memory_automation as memory
    return memory.runtime_paths(config)["status"].parent


def identity(config, kind):
    import dingtalk_memory_automation as memory
    return memory.sha256_text(json.dumps([str(memory.dws_binary(config)), config["dingtalk_automation"].get("scope"),
                                         config.get("dingtalk_chat", {}).get("profile", "default"), kind]))[:24]


def legacy_path(config, kind):
    return directory(config) / ("listener-budget-" + identity(config, kind) + ".json")


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def binding(config):
    import dingtalk_memory_automation as memory
    return {"dws_sha256": fingerprint(memory.dws_binary(config)),
            "scope_hash": contract.digest({"scope": config["dingtalk_automation"].get("scope"),
                                            "profile": config.get("dingtalk_chat", {}).get("profile", "default")}),
            "budget_hashes": {kind: fingerprint(legacy_path(config, kind)) for kind in ("all-direct", "all-group")}}


def diagnostic_paths(config):
    root = directory(config)
    return (root / "listener-diagnostic.json", root / "listener-diagnostic-supplement-01.json")


def diagnostic_provider_hold(config):
    for path in diagnostic_paths(config):
        if not path.is_file():
            continue
        session = json.loads(path.read_text())
        for kind in ("all-direct", "all-group"):
            attempt = directory(config) / ("listener-manual-" + session["action_id"] + "-" + kind + ".json")
            row = json.loads(attempt.read_text()) if attempt.is_file() else {}
            due = parse_time(row.get("next_retry_at"))
            if row.get("guard") or (due and due > datetime.now(timezone.utc)):
                return True
    return False


def gate(config):
    """Only a server-local, current correction proof can expose the button."""
    root = directory(config)
    path = root / "listener-cause-correction.json"
    if not path.is_file():
        return {"available": False, "reason": "event_cause_not_confirmed", "message": "尚未确认并修正失败原因；一次人工验收额度仍保留。"}
    try:
        proof = json.loads(path.read_text())
        valid = (proof.get("schema_version") == 1 and proof.get("cause_identified") is True
                 and proof.get("corrected") is True and proof.get("verification") == "read_only_preflight"
                 and proof.get("before_error_code") not in {None, "event_not_ready", "event_retry_budget_exhausted", "unknown"}
                 and proof.get("binding") == binding(config) and parse_time(proof.get("corrected_at")) is not None
                 and all(isinstance(proof.get(k), str) and len(proof[k]) == 64 for k in ("diagnosis_sha256", "correction_sha256")))
        if not valid:
            raise ValueError()
        # Retain provider in-flight/cooldown/terminal holds. A new host receipt
        # is never permission to bypass the provider's own restriction.
        for kind in ("all-direct", "all-group"):
            path_budget = legacy_path(config, kind)
            old = json.loads(path_budget.read_text()) if path_budget.is_file() else {}
            due = parse_time(old.get("next_retry_at"))
            if old.get("guard") or (due and due > datetime.now(timezone.utc)):
                return {"available": False, "reason": "event_provider_hold", "message": "服务端仍有限制，暂不消耗人工验收额度。"}
        if diagnostic_provider_hold(config):
            return {"available": False, "reason": "event_provider_hold", "message": "诊断发现服务端限制，验收额度保留。"}
        receipt = root / "listener-manual-acceptance.json"
        if receipt.is_file():
            previous = json.loads(receipt.read_text())
            return {"available": False, "reason": "event_manual_attempt_recorded", "message": "一次人工验收已有独立记录，不会重复执行。",
                    "state": previous.get("state")}
        return {"available": True, "proof_sha256": fingerprint(path), "binding": proof["binding"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {"available": False, "reason": "event_correction_unverified", "message": "原因修正证据不足或已过期，停止订阅重试。"}


def approve(config, action_id):
    uuid.UUID(action_id)
    root = directory(config)
    path = root / "listener-manual-acceptance.json"
    if path.is_file():
        old = json.loads(path.read_text())
        if old.get("action_id") == action_id:
            return old
        raise BudgetBlocked("event_manual_attempt_recorded")
    ready = gate(config)
    if not ready.get("available"):
        raise BudgetBlocked(ready["reason"])
    receipt = {"schema_version": 1, "action_id": action_id, "state": "approved", "approved_at": now(),
               "proof_sha256": ready["proof_sha256"], "binding": ready["binding"],
               "attempts_per_scope": 1, "automatic_budget_preserved": True}
    contract.save(path, receipt)
    return receipt


def take(config):
    """Called once under the listener lock, before any subscription process."""
    path = directory(config) / "listener-manual-acceptance.json"
    if not path.is_file():
        return None
    row = json.loads(path.read_text())
    if row.get("state") != "approved":
        return None
    if (row.get("binding") != binding(config)
            or row.get("proof_sha256") != fingerprint(path.with_name("listener-cause-correction.json"))):
        raise BudgetBlocked("event_correction_unverified")
    row.update(state="starting", started_at=now(), pid=os.getpid())
    contract.save(path, row)
    return row


def finish(config, row, status, error_code=None):
    if not row:
        return
    row.update(state=status, finished_at=now(), error_code=error_code)
    contract.save(directory(config) / "listener-manual-acceptance.json", row)


def accepted_anchor(config):
    """Read proof of completed acceptance; never grant another manual attempt."""
    root = directory(config)
    path = root / "listener-manual-acceptance.json"
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text())
        if receipt.get("state") != "accepted":
            return None
        uuid.UUID(receipt["action_id"])
        proof_path = root / "listener-cause-correction.json"
        proof = json.loads(proof_path.read_text())
        accepted_at = parse_time(receipt.get("finished_at"))
        if (receipt.get("schema_version") != 1 or receipt.get("attempts_per_scope") != 1
                or receipt.get("automatic_budget_preserved") is not True or not accepted_at
                or receipt.get("error_code") is not None
                or receipt.get("binding") != binding(config)
                or receipt.get("proof_sha256") != fingerprint(proof_path)
                or proof.get("schema_version") != 1 or proof.get("verification") != "read_only_preflight"
                or proof.get("before_error_code") in {None, "unknown", "event_not_ready", "event_retry_budget_exhausted"}
                or proof.get("binding") != receipt["binding"]
                or proof.get("cause_identified") is not True or proof.get("corrected") is not True):
            raise BudgetBlocked("event_accepted_proof_changed")
        for kind in ("all-direct", "all-group"):
            attempt_path = root / ("listener-manual-" + receipt["action_id"] + "-" + kind + ".json")
            attempt = json.loads(attempt_path.read_text())
            ready_at = parse_time(attempt.get("ready_at"))
            if (attempt.get("state") not in {"ready", "stopped"} or not ready_at or ready_at > accepted_at
                    or attempt.get("attempt_count") != 1 or attempt.get("max_attempts") != 1
                    or attempt.get("last_error_code") or attempt.get("guard")):
                raise BudgetBlocked("event_accepted_scope_unverified")
            old_path = legacy_path(config, kind)
            old = json.loads(old_path.read_text()) if old_path.is_file() else {}
            due = parse_time(old.get("next_retry_at"))
            if old.get("guard") or (due and due > datetime.now(timezone.utc)):
                raise BudgetBlocked("event_provider_hold")
        if diagnostic_provider_hold(config):
            raise BudgetBlocked("event_provider_hold")
        return {"action_id": receipt["action_id"], "receipt_sha256": fingerprint(path),
                "proof_sha256": receipt["proof_sha256"], "binding": receipt["binding"],
                "runtime_root_hash": contract.digest({"runtime_root": config.get("dingtalk_automation", {}).get("event_runtime_root")})}
    except BudgetBlocked:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise BudgetBlocked("event_accepted_proof_invalid")


class OperationalBudget(ListenerBudget):
    """Normal bounded restarts after acceptance, with the old budgets frozen."""
    def __init__(self, config, kind, anchor):
        self.config, self.anchor = config, anchor
        super().__init__(directory(config) / ("listener-operational-" + anchor["action_id"] + "-" + kind + ".json"))

    def read(self):
        row = super().read()
        if self.path.exists() and row.get("accepted_anchor") != self.anchor:
            raise BudgetBlocked("event_operational_budget_binding_changed")
        return row

    def save(self, row):
        row["accepted_anchor"] = self.anchor
        row["automatic_budget_preserved"] = True
        super().save(row)

    @staticmethod
    def owner_alive(row):
        try:
            pid = int(row.get("pid") or 0)
            if pid <= 0:
                return True  # Missing ownership is not proof of a dead owner.
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (OSError, ValueError, TypeError):
            return True

    def archive_episode(self, row, reason, **evidence):
        record = {"schema_version": 1, "budget": row, "reason": reason, **evidence}
        digest = contract.digest(record)
        path = self.path.parent / "listener-budget-history" / (digest + ".json")
        if path.exists():
            if json.loads(path.read_text()) != record:
                raise BudgetBlocked("event_budget_history_changed")
        else:
            contract.save(path, record)
        return digest

    def reconcile_pre_ready_attempts(self, prior_ready, evidence_ref):
        """Explicit repair of a legacy success carried into a failed restart.

        The immutable ready snapshot must prove the exact preceding episode.
        Preserve the failure and its maximum; only remove completed successful
        attempts from this restart's accounting. Never called on auto-start.
        """
        if accepted_anchor(self.config) != self.anchor:
            raise BudgetBlocked("event_accepted_proof_changed")
        row = self.read()
        if row.get("epoch_accounting"):
            if row["epoch_accounting"].get("evidence_ref") != evidence_ref:
                raise BudgetBlocked("event_accounting_evidence_changed")
            return row
        ready_at = parse_time(prior_ready.get("ready_at"))
        prior_at = parse_time(prior_ready.get("attempt_at"))
        attempted = parse_time(row.get("attempt_at"))
        failed = parse_time(row.get("last_failure_at"))
        prior_count = prior_ready.get("attempt_count")
        due = parse_time(row.get("next_retry_at"))
        if (not isinstance(evidence_ref, str) or len(evidence_ref) != 64
                or prior_ready.get("accepted_anchor") != self.anchor
                or prior_ready.get("state") != "ready"
                or not ready_at or not prior_at or not attempted or not failed
                or not prior_at <= ready_at < attempted <= failed
                or row.get("ready_at") != prior_ready.get("ready_at")
                or row.get("state") != "failed" or row.get("last_error_code") != "event_not_ready"
                or type(prior_count) is not int or prior_count < 1
                or row.get("attempt_count") != prior_count + 1
                or int(row.get("max_attempts") or 0) < 2
                or row.get("guard") or prior_ready.get("guard")
                or (due and self.clock() < due)
                or self.owner_alive(row) or self.owner_alive(prior_ready)):
            raise BudgetBlocked("event_accounting_evidence_unverified")
        history = self.archive_episode(row, "accepted_success_carried_into_restart",
                                       prior_ready=prior_ready, evidence_ref=evidence_ref)
        row = {k: v for k, v in row.items() if k not in {"ready_at", "stopped_at"}}
        row.update(attempt_count=1, epoch_accounting={"evidence_ref": evidence_ref,
                   "history_ref": history, "completed_success_attempts": prior_count,
                   "failed_restart_attempts_preserved": 1, "corrected_at": now()})
        self.save(row)
        return row

    def check_ready_episode(self, row):
        """Read-only preflight, also used by the listener's admission gate."""
        ready_at, attempted = parse_time(row.get("ready_at")), parse_time(row.get("attempt_at"))
        if (row.get("state") != "ready" or not ready_at or not attempted
                or ready_at < attempted or row.get("guard")):
            raise BudgetBlocked("event_ready_episode_unverified")
        if self.owner_alive(row):
            raise BudgetBlocked("event_listener_owner_alive")

    def reserve(self):
        if accepted_anchor(self.config) != self.anchor:
            raise BudgetBlocked("event_accepted_proof_changed")
        row = self.read()
        if row.get("state") == "ready":
            self.check_ready_episode(row)
            history = self.archive_episode(row, "accepted_ready_owner_ended")
            # Only a completed ready episode starts a fresh pre-ready budget.
            # Crashes before ready and failed attempts retain their old limits.
            self.save({"schema_version": 1, "attempt_count": 0, "max_attempts": 2,
                       "state": "new", "previous_episode_ref": history})
        elif row.get("state") == "stopped":
            self.archive_episode(row, "accepted_ready_stopped")
        return super().reserve()


def operational_budgets(config):
    anchor = accepted_anchor(config)
    if anchor is None:
        return None
    return {kind: OperationalBudget(config, kind, anchor) for kind in ("all-direct", "all-group")}


def maintenance_restart_preflight(config):
    """Do not stop a one-shot live listener without a proven restart path."""
    root = directory(config)
    status_path = root / "automation_status.json"
    status = json.loads(status_path.read_text()) if status_path.is_file() else {}
    if status.get("listener_state") not in {"starting", "ready"}:
        return
    path = root / "listener-manual-acceptance.json"
    if not path.is_file():
        return  # Ordinary ready -> stopped lifecycle already preserves restarts.
    row = json.loads(path.read_text())
    if row.get("state") in {"starting", "ready_acceptance_pending"}:
        raise BudgetBlocked("maintenance_listener_acceptance_pending")
    if row.get("state") == "accepted":
        operational_budgets(config)


class ManualBudget:
    def __init__(self, config, kind, receipt):
        self.path = directory(config) / ("listener-manual-" + receipt["action_id"] + "-" + kind + ".json")
        self.legacy = legacy_path(config, kind)
        self.legacy_hash = receipt["binding"]["budget_hashes"][kind]

    def read(self):
        return json.loads(self.path.read_text()) if self.path.is_file() else {"schema_version": 1, "state": "new", "attempt_count": 0, "max_attempts": 1}

    def reserve(self):
        row = self.read()
        if fingerprint(self.legacy) != self.legacy_hash:
            raise BudgetBlocked("event_legacy_budget_changed")
        old = json.loads(self.legacy.read_text()) if self.legacy.is_file() else {}
        due = parse_time(old.get("next_retry_at"))
        if old.get("guard") or (due and due > datetime.now(timezone.utc)):
            raise BudgetBlocked("event_provider_hold")
        if row["attempt_count"] >= 1:
            raise BudgetBlocked("event_manual_attempt_exhausted")
        row.update(state="starting", attempt_count=1, attempt_at=now(), automatic_budget_preserved=True)
        contract.save(self.path, row)
        return row

    def ready(self):
        row = self.read()
        row.update(state="ready", ready_at=now())
        contract.save(self.path, row)

    def failed(self, retryable, *, retry_after=0, next_retry_at=None, code="event_not_ready", guard=None):
        row = self.read()
        due = datetime.now(timezone.utc) + timedelta(seconds=max(0, retry_after))
        explicit = parse_time(next_retry_at)
        if explicit and explicit > due:
            due = explicit
        next_retry_at = due.isoformat() if retry_after or explicit else None
        row.update(state="failed", last_error_code=code, retryable=retryable, guard=guard, last_failure_at=now(), next_retry_at=next_retry_at)
        contract.save(self.path, row)
        return row

    def stopped(self):
        row = self.read()
        row.update(state="stopped", stopped_at=now())
        contract.save(self.path, row)  # A stop never resets this one-shot receipt.
