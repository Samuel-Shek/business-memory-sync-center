"""Read-only operational evidence for a Codex handoff; never returns raw files."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import re
from datetime import datetime
from pathlib import Path

VERSION = 2
HISTORY_BYTES = 262144


def fields(value, names):
    return {key: value.get(key) for key in names if key in value}


def read_record(root, name, gaps):
    path = root / "state" / name
    try:
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError("unsafe record")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("invalid record")
        return value
    except (OSError, ValueError):
        gaps.append({"record": name, "reason": "记录不可读或格式不完整，不能推断为零次或已恢复。"})
        return {}


def parsed_time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def validation_history(root, since, gaps):
    name = "automatic-memory-validation-history.jsonl"
    path = root / "state" / name
    try:
        if path.is_symlink():
            raise ValueError("unsafe history")
        with path.open("rb") as handle:
            size = path.stat().st_size
            start = max(0, size - HISTORY_BYTES)
            handle.seek(start)
            if start:
                handle.readline()
            lines = handle.read(HISTORY_BYTES).splitlines()
    except (OSError, ValueError):
        gaps.append({"record": name, "reason": "后台校验历史不可读，尝试次数未知。"})
        return {"observed_run_count": None, "complete": False, "runs": []}
    cutoff, runs, malformed = parsed_time(since), {}, 0
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("invalid history row")
        except (ValueError, UnicodeError):
            malformed += 1
            continue
        observed = parsed_time(row.get("updated_at"))
        if cutoff is not None and (observed is None or observed < cutoff):
            continue
        run_id = row.get("run_id")
        if run_id and row.get("status") in {"running", "failed", "verified", "success", "deferred", "superseded"}:
            runs[str(run_id)] = fields(row, ("run_id", "status", "started_at", "completed_at", "updated_at", "error_code"))
    if malformed:
        gaps.append({"record": name, "reason": "部分历史行无法读取，已读记录不能代表完整次数。"})
    return {"observed_run_count": len(runs), "complete": not start and not malformed and cutoff is not None,
            "scope": "本次问题首次出现之后的已读历史，按运行编号去重；不与自动修复次数相加。",
            "runs": list(runs.values())[-12:], "records_limited": len(runs) > 12}


def safe_validation_error(error, config):
    # Interpret known validator messages without passing paths or arbitrary log text.
    match = re.search(r": forbidden ([a-zA-Z /-]+) file type (\.[a-zA-Z0-9]+)$", str(error))
    if match:
        extension = match.group(2).lower()
        present = None
        if config.get("memory_root"):
            source = Path(str(error)[:match.start()])
            vault = Path(config["memory_root"]).parent
            if source.is_absolute() and source.is_relative_to(vault) and ".." not in source.parts:
                present = source.is_file()
        return {"kind": "forbidden_storage_type", "file_type": extension,
                "summary": f"业务资料区放入了不允许存放的 {extension} 文件，完整校验因此失败。",
                "file_still_present": present}
    code = re.search(r"\b(claim_base_[a-z_]+)\b", str(error))
    if code:
        return {"kind": code.group(1), "summary": "业务页面与受治理的校验基线不一致，需要核对原始回执。"}
    return {"kind": "unclassified_validation_error", "summary": "完整校验还有未分类错误，需要只读核对原始诊断。"}


def core_evidence(root, config, detail):
    gaps = []
    failure = read_record(root, "last-memory-validation-failure.json", gaps)
    success = read_record(root, "last-memory-validation-success.json", gaps)
    job = read_record(root, "automatic-memory-validation.json", gaps)
    errors = failure.get("errors")
    if not isinstance(errors, list):
        errors = []
    return {
        "latest_failure": {**fields(failure, ("recorded_at", "verdict")),
                           "errors": [safe_validation_error(error, config) for error in errors[:8]],
                           "error_count": len(errors), "record": "last-memory-validation-failure.json"},
        "latest_success": {**fields(success, ("recorded_at", "verdict", "full_validation", "index_dirty_generation", "index_applied_generation")),
                           "record": "last-memory-validation-success.json",
                           "boundary": "旧成功回执不证明当前恢复；还需匹配当前资料与程序。"},
        "current_validation": fields(job, ("status", "phase", "updated_at", "started_at", "completed_at", "next_retry_at", "error_code", "attempt_count")),
        "validation_history": validation_history(root, detail.get("first_seen"), gaps),
        "repair_history": [fields(row, ("repair_run_id", "repair_status", "repair_attempt_count", "repair_started_at", "repair_finished_at", "repair_error_code"))
                           for row in detail.get("alert_health", []) if row.get("repair_run_id") or row.get("repair_attempt_count")],
        "count_boundary": "工作台操作次数、当前校验任务次数、后台历史校验和自动修复分别统计；某一项为零不代表从未处理。",
        "evidence_gaps": gaps,
    }


def operational(config, detail, root):
    grouped = detail.get("kind") == "group"
    members = detail.get("members", []) if grouped else [detail]
    value = {
        "context_version": VERSION,
        "title": detail.get("title"), "kind": "operational_issue_group" if grouped else detail.get("kind"),
        "source": detail.get("source_label"), "status": detail.get("status"),
        "error_code": detail.get("error_code") or detail.get("observation", {}).get("error_code"),
        "reason": detail.get("reason"), "impact": detail.get("impact"),
        "first_seen": detail.get("first_seen"), "last_seen": detail.get("last_seen"),
        "checked_at": detail.get("checked_at"), "resolved_at": detail.get("resolved_at"),
        "resolution_kind": detail.get("resolution_kind"), "affected_count": detail.get("affected_count", 1),
        "attention_state": detail.get("attention_state"),
        "workbench_attempt_count": detail.get("total_attempts") if grouped else detail.get("observation", {}).get("attempt_count"),
        "attempts_note": detail.get("attempts_note"),
        "members": [{**fields(member, ("issue_id", "kind", "status", "attention_state", "reason", "impact", "recovery_boundary")),
                     "observation": fields(member.get("observation", {}), ("error_code", "original_failure", "original_failure_at", "current_blocker", "last_attempt", "last_success", "next_due", "reading", "evidence_gaps", "event_verification_count", "actual_repair_count", "historical_database_attempts", "technical_investigation_required", "decision_required", "listener_acceptance"))}
                    for member in members[:24]],
        "members_limited": len(members) > 24,
        "source_attempts": detail.get("attempts", [])[-12:],
        "workbench_actions": [fields(row, ("action", "state", "run_id", "created_at", "updated_at")) for row in detail.get("action_history", [])[-12:]],
        "completion_rule": detail.get("completion_rule"), "recovery_boundary": detail.get("recovery_boundary"),
        "available_actions": [fields(row, ("id", "label", "reason", "requires_confirmation")) for row in detail.get("actions", []) if row.get("available")],
        "decision": {"required": None, "summary": "需你处理标签本身不是决策题；先查清阻断及可行步骤，再说明是否有必须由用户判断的事项。"},
    }
    if detail.get("source_id") == "dingtalk_chat":
        import dingtalk_commit_guard
        projection = detail.get("chat_recovery") or dingtalk_commit_guard.recovery_evidence(config, detail.get("first_seen"), detail.get("resolved_at"))
        exported = []
        for candidate in projection.get("candidates", []):
            item = dict(candidate)
            failures = item.pop("failures", [])
            if failures:
                item["first_failure"] = failures[0]
                if len(failures) > 1:
                    item["latest_failure"] = failures[-1]
            exported.append(item)
        value["chat_recovery"] = {**projection, "candidates": exported}
    # Keep this bounded receipt within the handoff redactor's nesting limit.
    for member in value["members"]:
        observation = member["observation"]
        acceptance = observation.get("listener_acceptance")
        if isinstance(acceptance, dict):
            exported = fields(acceptance, ("ready_at", "checked_at", "evidence_current_episode", "status",
                "event_counts_status", "subscription_verification", "server_subscription_listing",
                "accepted_at", "historical_accepted_at"))
            for kind in ("direct", "group"):
                scope = (acceptance.get("scopes") or {}).get(kind) or {}
                for key in ("accepted", "accepted_at", "received_events", "older_message_events"):
                    if key in scope:
                        exported[kind + "_" + key] = scope[key]
            observation["listener_acceptance"] = exported
    if detail.get("source_id") == "memory_core":
        value["diagnostics"] = core_evidence(root, config, detail)
        value["decision"] = {"required": False, "kind": "technical_issue",
                             "summary": "当前记录是技术校验故障，没有已确定的客户业务决策；先给出具体修复方案，执行仍依据用户本轮授权。"}
        if any(row["kind"] == "forbidden_storage_type" for row in value["diagnostics"]["latest_failure"]["errors"]):
            value["recommended_next_step"] = "核对违规文件是否仍在资料区；保留原件，迁到外置机器记录目录，核对引用和迁移凭据，再做匹配当前资料的完整校验。"
        else:
            value["recommended_next_step"] = "先核对完整失败回执和当前资料，定位阻断后提出具体修复步骤；不能靠重复点击或跳过校验结束问题。"
    elif detail.get("attention_state") in {"automatic", "waiting"}:
        value["decision"] = {"required": False, "summary": "当前由系统处理或等待外部条件；没有明确决策题时，不要求用户选择。"}
        value["recommended_next_step"] = detail.get("recovery_boundary") or detail.get("reason")
    if detail.get("status") == "closed":
        value["decision"] = {"required": False, "summary": "该问题已有结束记录，先核对完成证据，无需为旧故障再次决策。"}
        value["recommended_next_step"] = "说明结束依据和当前状态；不要把保留的旧失败回执当成新故障，也不要重复执行修复。"
    return value
