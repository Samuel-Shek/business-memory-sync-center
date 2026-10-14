#!/usr/bin/env python3
"""Win11 business-memory semantic merge automation.

Fills the semantic link in the Win11 memory pipeline: the folder watcher and
receiver keep mechanical source notes in the external owner-only machine layer,
while turning those notes into curated Obsidian memory needs a model in the
middle.  win11_semantic_merge.rb owns preparation, validation and commit; this
script owns the orchestration around it:

    prepare -> inline source content -> Codex proposal -> allocate ids ->
    stage notes -> commit

Design constraints that must not be relaxed without re-reading the Ruby merger:
  * The model never touches the filesystem.  Source content is inlined into the
    prompt and the model only returns JSON, so a hostile source file cannot make
    it read or write anything.
  * The model may only ADD notes.  Rewriting existing curated notes is out of
    scope here so an unattended hourly run can never damage reviewed memory.
  * Every write still goes through win11_semantic_merge.rb commit, which keeps
    the batch-hash binding, coverage check, privacy scan, backup and rollback.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import business_page_router as business_router
import claim_reconciliation as reconciliation

os.umask(0o077)

ROOT = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync'))
CONFIG_PATH = ROOT / "config.json"
MERGE_SCRIPT = ROOT / "bin" / "win11_semantic_merge.rb"
PROMPT_PATH = ROOT / "prompts" / "win11_memory_proposal.md"
SCHEMA_PATH = ROOT / "schemas" / "win11_memory_proposal.schema.json"
LOCK_PATH = ROOT / "state" / "win11_memory_merge.lock"
LOG_PATH = ROOT / "logs" / "win11_memory_automation.jsonl"
HEALTH_PATH = ROOT / "state" / "win11_memory_health.json"
MAINTENANCE_STATE_PATH = ROOT / "state" / "maintenance-mode.json"
RUBY_BIN = "/usr/bin/env"

CN_TZ = timezone(timedelta(hours=8))

MEMORY_ID_RE = re.compile(r"^BUSINESS-\d{8}-\d{3}$")
RUN_ID_RE = re.compile(r"^win11-\d{8}T\d{6}-[0-9a-f]{8}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Mirrors the rejection rules inside win11_semantic_merge.rb validate_staged_note.
# Catching them here turns a wasted commit into a cheap local failure.
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
SECRET_RE = re.compile(
    r"\bsk-[A-Za-z0-9_-]{20,}\b|\bAKIA[0-9A-Z]{16}\b|BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"
)
RAW_MAIL_RE = re.compile(
    r"^(?:Return-Path|Received|Message-ID|Authentication-Results|DKIM-Signature|X-[A-Za-z0-9-]+):",
    re.I,
)

WRITE_DIRS = ("01_关键业务认知", "02_内部业务资料", "03_外部沟通")
CATEGORY_FOR_DIR = {
    "01_关键业务认知": "business_cognition",
    "02_内部业务资料": "internal_material",
    "03_外部沟通": "external_communication",
}
PLACEHOLDER_HASH = "sha256:" + ("0" * 64)
MAX_INLINE_CHARS = 60000
ALLOCATE_MAX = 20

RETRYABLE_PROPOSAL_ERRORS = {
    "codex_invalid_json",
    "privacy",
    "secret",
    "raw_mail",
    "too_many_notes",
    "ai_run_id",
    "ai_coverage",
    "ai_ref",
    "ai_directory",
    "ai_category",
    "ai_slug",
    "ai_body",
    "ai_supersedes",
    "ai_target",
    "ai_disposition",
    "ai_claim_relation",
}

CODEX_DISABLED_FEATURES = (
    "plugins", "apps", "remote_plugin", "plugin_sharing", "skill_search",
    "skill_mcp_dependency_install", "hooks", "memories", "multi_agent",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "image_generation", "shell_tool", "unified_exec",
)


class AutomationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def append_log(event: str, **fields: Any) -> None:
    record = {"at": now_iso(), "event": event}
    record.update(fields)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    try:
        LOG_PATH.chmod(0o600)
    except OSError:
        pass


def write_health(status: str, detail: str, **fields: Any) -> None:
    payload = {"schema_version": 1, "status": status, "detail": detail, "updated_at": now_iso()}
    payload.update(fields)
    tmp = HEALTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(HEALTH_PATH)


def ensure_maintenance_inactive() -> None:
    """Refuse a merge while unified maintenance is active."""
    if not MAINTENANCE_STATE_PATH.is_file():
        return
    try:
        payload = json.loads(MAINTENANCE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutomationError("maintenance_state_unreadable", "maintenance state cannot be verified") from exc
    if not isinstance(payload, dict):
        raise AutomationError("maintenance_state_unreadable", "maintenance state is not an object")
    if str(payload.get("status")) in {"entering", "active", "blocked", "exiting"}:
        raise AutomationError("maintenance_active", "unified maintenance mode is active")


def acquire_lock():
    """Hold an exclusive lock so the hourly agent and the Launchpad button
    can never run a merge at the same time."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    LOCK_PATH.chmod(0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise AutomationError("already_running", "another win11 memory merge is already running")
    return handle


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# --- Xiaomi MiMo Token Plan provider (2020-01-02 Example Owner decision) ---
# Mirrors dingtalk_memory_automation.mimo_* helpers; this module stays standalone.
# 2020-01-02: MiMo quota exhausted; paused in favour of DeepSeek V4.1 Flash.
MIMO_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5-pro"
MIMO_API_KEY_FILE = ROOT / "state" / "mimo_api_key"

# --- DeepSeek provider (2020-01-02 Example Owner decision) ---
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"  # DeepSeek V4.1 Flash, natively multimodal
DEEPSEEK_API_KEY_FILE = ROOT / "state" / "deepseek_api_key"


def deepseek_api_key() -> str:
    try:
        key = DEEPSEEK_API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AutomationError("deepseek_key_unavailable", "DeepSeek key file is missing") from exc
    if not key.startswith("sk-") or len(key) < 20:
        raise AutomationError("deepseek_key_invalid", "DeepSeek key file is malformed")
    return key


def mimo_api_key() -> str:
    try:
        key = MIMO_API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AutomationError("mimo_key_unavailable", "MiMo Token Plan key file is missing") from exc
    if not key.startswith("tp-") or len(key) < 20:
        raise AutomationError("mimo_key_invalid", "MiMo Token Plan key file is malformed")
    return key


GOVERNED_ALLOWED_MODELS = {
    "deepseek-flash",                                                 # default (DeepSeek V4.1 Flash, 2020-01-02)
    "mimo-v2.5-pro", "mimo-v2.5",                                    # paused (MiMo quota exhausted; restorable)
    "kimi-for-coding",
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "claude-opus-5",
}


def deepseek_provider_flags() -> list[str]:
    """Codex routing flags for the DeepSeek provider (Responses API, official)."""
    return [
        "-c", 'model_provider="deepseek"',
        "-c", 'model_providers.deepseek.name="deepseek"',
        "-c", f'model_providers.deepseek.base_url="{DEEPSEEK_BASE_URL}"',
        "-c", 'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"',
        "-c", 'model_providers.deepseek.wire_api="responses"',
        "-c", 'web_search="disabled"',
    ]


def mimo_provider_flags() -> list[str]:
    return [
        "-c", 'model_provider="mimo"',
        "-c", 'model_providers.mimo.name="mimo"',
        "-c", f'model_providers.mimo.base_url="{MIMO_BASE_URL}"',
        "-c", 'model_providers.mimo.env_key="MIMO_API_KEY"',
        "-c", 'model_providers.mimo.wire_api="responses"',
        "-c", 'web_search="disabled"',
    ]


def governed_provider_flags(model: str) -> list[str]:
    """Provider routing by model family; DeepSeek is the default (2020-01-02)."""
    model = str(model or DEEPSEEK_MODEL)
    if model.startswith("deepseek"):
        deepseek_api_key()  # fail fast when the default family lacks its key
        return deepseek_provider_flags()
    if model.startswith("mimo"):
        mimo_api_key()
        return mimo_provider_flags()
    if model.startswith("claude"):
        return [
            "-c", 'model_provider="claude"',
            "-c", 'model_providers.claude.name="claude"',
            "-c", f'model_providers.claude.base_url="{os.environ.get("CLAUDE_BASE_URL", "https://api.anthropic.com/v1")}"',
            "-c", 'model_providers.claude.env_key="CLAUDE_API_KEY"',
            "-c", 'model_providers.claude.wire_api="responses"',
            "-c", 'web_search="disabled"',
        ]
    return []


def mimo_schema_prompt(schema_path: Path) -> str:
    schema_text = Path(schema_path).read_text(encoding="utf-8")
    return (
        "\n\n<output_contract>\n"
        "你的最终回复必须只是一个 JSON 对象：不得包含任何解释、前后缀文字或 Markdown 代码块标记。\n"
        "该 JSON 对象必须严格符合以下 JSON Schema：\n"
        + schema_text
        + "\n</output_contract>\n"
    )


def parse_model_json(text: str) -> Any:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"\A```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*\Z", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AutomationError("codex_invalid_json", "Model did not return valid structured JSON") from exc


def subscription_only_environment() -> dict[str, str]:
    """Strip any API-key style credential so Codex can only use the
    subscription session, matching the dingtalk automation's guarantee."""
    blocked = {
        "OPENAI_API_KEY", "CODEX_API_KEY", "AZURE_OPENAI_API_KEY",
        "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
    }
    environment = {k: v for k, v in os.environ.items() if k not in blocked}
    environment["LC_ALL"] = "en_US.UTF-8"
    # DeepSeek key is the default family since 2020-01-02 (MiMo paused).
    # Other retained families (GPT subscription / Claude) do not require it.
    try:
        environment["DEEPSEEK_API_KEY"] = deepseek_api_key()
    except AutomationError:
        environment.pop("DEEPSEEK_API_KEY", None)
    return environment


def run_merge(*args: str, timeout: int = 300) -> dict[str, Any]:
    """Invoke win11_semantic_merge.rb and decode its single JSON line."""
    command = [RUBY_BIN, "ruby", str(MERGE_SCRIPT), *args]
    # launchd hands us no locale.  Without a UTF-8 one, Ruby decodes the Chinese
    # vault paths in ARGV as ASCII-8BIT and the validator dies with an encoding
    # error, which surfaces only as an empty "validation failed" message.
    environment = dict(os.environ)
    environment.setdefault("LANG", "en_US.UTF-8")
    environment["LC_ALL"] = "en_US.UTF-8"
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(ROOT / "bin"), env=environment, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        raise
    payload_text = stdout.strip() or stderr.strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        raise AutomationError(
            "merge_output", f"win11_semantic_merge.rb {args[0]} returned non-JSON output"
        )
    if process.returncode == 42:
        raise AutomationError("batch_limit", payload.get("message", "batch exceeds safety limit"))
    if process.returncode != 0:
        raise AutomationError(
            f"merge_{payload.get('code', 'error')}", payload.get("message", "merge command failed")
        )
    return payload


def is_probably_text(path: Path) -> tuple[bool, str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return False, ""
    if b"\0" in raw[:8192]:
        return False, ""
    try:
        return True, raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, ""


def build_payload(batch: dict[str, Any]) -> dict[str, Any]:
    """Inline every source item's content so the model needs no file access."""
    items = []
    for item in batch["items"]:
        if not item.get("analysis_required"):
            continue
        entry = {
            "event_id": item["event_id"],
            "source_entry_id": item["source_entry_id"],
            "operation": item["operation"],
            "relative_path": item["relative_path"],
            "version_number": item.get("version_number"),
            "size_bytes": item.get("size_bytes"),
            "analysis_required": bool(item.get("analysis_required")),
            "is_deleted": item.get("current_sha256") is None,
            "existing_target_memory_ids": item.get("existing_target_memory_ids") or [],
        }
        content_path = item.get("content_path")
        if entry["analysis_required"] and content_path:
            readable, text = is_probably_text(Path(content_path))
            if not readable:
                entry["content"] = None
                entry["content_note"] = "二进制或不可解码文件，只有元数据可用，不得推断其内容"
            else:
                if len(text) > MAX_INLINE_CHARS:
                    text = text[:MAX_INLINE_CHARS] + "\n…（内容过长已截断，未展示部分不得推断）"
                entry["content"] = text
                entry["content_note"] = None
        else:
            entry["content"] = None
            entry["content_note"] = "无需内容分析（元数据变化或来源已删除）"
        items.append(entry)
    source_text = "\n".join(
        " ".join(
            (
                str(item.get("relative_path") or ""),
                str(item.get("content") or ""),
            )
        )
        for item in items
    )
    try:
        open_claims = reconciliation.open_claim_context(source_text, limit=24)
    except reconciliation.ReconciliationError:
        # A not-yet-initialized derived index must never block the canonical
        # Win11 ingestion path.  The reconciliation migration initializes it.
        open_claims = []
    return {
        "run_id": batch["run_id"],
        "source_id": batch["source_id"],
        "cutoff_event_at": batch.get("cutoff_event_at"),
        "governance": batch.get("governance", {}),
        # Stated explicitly because a missed or duplicated event_id is the most
        # common way a proposal gets rejected.
        "required_source_results_count": len(items),
        "items": items,
        "open_claims": open_claims,
    }


def memory_index() -> list[dict[str, str]]:
    """A compact list of existing curated notes so the model can link instead of
    duplicating.  Only frontmatter facts are exposed, never note bodies."""
    config = load_config()
    memory_root = Path(config["memory_root"])
    index = []
    for directory in ("01_关键业务认知", "02_内部业务资料", "03_外部沟通"):
        base = memory_root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            name = path.name
            if name == "00_待确认总表.md" or name.startswith("Win11文件夹监控-"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            match = re.match(r"^---\n(.*?)\n---", text, re.S)
            if not match:
                continue
            fields = {}
            for line in match.group(1).split("\n"):
                kv = re.match(r"^([a-z_]+):\s*(.*)$", line)
                if kv:
                    fields[kv.group(1)] = kv.group(2).strip().strip('"')
            memory_id = fields.get("memory_id", "")
            if not MEMORY_ID_RE.match(memory_id):
                continue
            index.append({
                "memory_id": memory_id,
                "title": path.stem,
                "directory": directory,
                "memory_kind": fields.get("memory_kind", ""),
                "status": fields.get("status", ""),
                "fact_status": fields.get("fact_status", ""),
            })
    vault_root = business_router.vault_root_from_config(config)
    for claim in business_router.scan_claims(vault_root):
        index.append({
            "memory_id": str(claim.get("claim_id") or ""),
            "title": str(claim.get("title") or ""),
            "directory": str(claim.get("relative_path") or "").split("/", 1)[0],
            "memory_kind": "open_question",
            "status": "review_required",
            "fact_status": str(claim.get("state") or "pending"),
        })
    return index


def persist_codex_failure(run_dir: Path, retry_hint: str | None, *, kind: str,
                          stderr: str | bytes | None, returncode: int | None) -> None:
    """Keep owner-only diagnostics for an infrastructure failure.

    The prompt and source payload are intentionally not duplicated here.  The
    run already owns batch.json, while this receipt captures only bounded
    process diagnostics needed to distinguish a model rejection from a crashed
    Codex process.
    """
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    payload = {
        "schema_version": 1,
        "observed_at": now_iso(),
        "kind": kind,
        "returncode": returncode,
        "stderr": (stderr or "")[-65536:],
    }
    name = "codex_failure_retry.json" if retry_hint else "codex_failure.json"
    try:
        path = run_dir / name
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError:
        pass


def codex_error_summary(stderr: str | None) -> str:
    text = stderr or ""
    messages = re.findall(r'"message"\s*:\s*("(?:[^"\\]|\\.)*")', text)
    if messages:
        try:
            return str(json.loads(messages[-1]))[:300]
        except json.JSONDecodeError:
            pass
    meaningful = [
        line.strip() for line in text.splitlines()
        if line.strip() and line.strip() not in {"{", "}", "ERROR: {"}
    ]
    return (meaningful[-1] if meaningful else "no stderr detail")[:300]


def invoke_codex(config: dict[str, Any], payload: dict[str, Any], run_dir: Path,
                 retry_hint: str | None = None, accept: Any = None) -> dict[str, Any]:
    automation = config.get("dingtalk_automation", {})
    # No codex precondition: MiMo and Kimi are called directly, so a missing or
    # hung codex only removes the GPT fallback rather than stopping the run.
    if not PROMPT_PATH.is_file() or not SCHEMA_PATH.is_file():
        raise AutomationError("codex_contract_missing", "prompt or output schema is missing")

    prompt = (
        PROMPT_PATH.read_text(encoding="utf-8").rstrip()
        + "\n\n<current_memory_index>\n"
        + json.dumps(memory_index(), ensure_ascii=False, indent=1, sort_keys=True)
        + "\n</current_memory_index>\n\n<batch_data>\n"
        + json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
        + "\n</batch_data>\n"
    )
    if retry_hint and not (config.get('model_routing') or {}).get('persistent_guard', False):
        prompt += (
            "\n<previous_attempt_rejected>\n"
            "你上一次的输出被本地校验拒绝了，原因如下。请重新输出完整提案，修正这个问题：\n"
            f"{retry_hint}\n"
            f"提醒：`source_results` 必须恰好包含 {len(payload['items'])} 条，"
            "每个 event_id 出现且只出现一次，不能遗漏也不能重复。\n"
            "</previous_attempt_rejected>\n"
        )
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    event_ids = [item["event_id"] for item in payload["items"]]
    schema["properties"]["run_id"]["enum"] = [payload["run_id"]]
    results_schema = schema["properties"]["source_results"]
    results_schema["minItems"] = len(event_ids)
    results_schema["maxItems"] = len(event_ids)
    results_schema["items"]["properties"]["event_id"]["enum"] = event_ids
    output_schema = run_dir / "output-schema.json"
    output_schema.write_text(json.dumps(schema, ensure_ascii=False) + "\n", encoding="utf-8")
    output_schema.chmod(0o600)
    import model_router
    prompt += mimo_schema_prompt(output_schema)
    timeout = int(automation.get("codex_timeout_seconds", 900))
    append_log("codex_started", run_id=payload["run_id"], items=len(payload["items"]),
               prompt_chars=len(prompt))
    started = time.monotonic()
    try:
        routed = model_router.complete(config, prompt=prompt, purpose="win11_semantic_proposal",
                                       timeout=timeout, workdir=run_dir, validate=accept or parse_model_json,
                                       request_context={"scope":"win11-import", "volatile_values":{"run_id":payload["run_id"]}})
    except model_router.RouterExhausted as exc:
        if exc.category == "invalid_output":
            # Every model's answer broke the contract. Keep the caller's code so
            # the one hinted retry (RETRYABLE_PROPOSAL_ERRORS) still happens.
            last = next((a for a in reversed(exc.attempts) if a.get("rejection_code")), {})
            code = str(last.get("rejection_code") or "codex_invalid_json")
            raise AutomationError(code, str(last.get("detail") or exc)[:300]) from exc
        timed_out = exc.category == "timeout"
        persist_codex_failure(
            run_dir, retry_hint, kind="timeout" if timed_out else "nonzero_exit",
            stderr=str(exc), returncode=None,
        )
        # Keep the two codes downstream already distinguishes.
        raise AutomationError(model_router.legacy_error_code(exc) if exc.category.startswith(("guard_", "duplicate_", "request_")) else "codex_timeout" if timed_out else "codex_failed",
                              f"Model chain exhausted: {exc}") from exc
    elapsed = round(time.monotonic() - started, 1)
    reply = routed.text
    # Persist the raw reply before validation: when a proposal is rejected the
    # reply is the only evidence of why, and re-running costs another AI call.
    try:
        raw_name = "ai_raw_retry.json" if retry_hint else "ai_raw.json"
        (run_dir / raw_name).write_text(reply, encoding="utf-8")
        (run_dir / raw_name).chmod(0o600)
    except OSError:
        pass
    proposal = routed.value
    append_log("codex_completed", run_id=payload["run_id"], seconds=elapsed,
               provider=routed.provider, model=routed.model,
               new_notes=len(proposal.get("new_notes", [])))
    return proposal


def scan_forbidden(text: str, where: str) -> None:
    if EMAIL_RE.search(text):
        raise AutomationError("privacy", f"{where} contains an email address")
    if MOBILE_RE.search(text):
        raise AutomationError("privacy", f"{where} contains a mainland mobile number")
    if SECRET_RE.search(text):
        raise AutomationError("secret", f"{where} contains a secret-like value")
    for line in text.splitlines():
        if RAW_MAIL_RE.match(line):
            raise AutomationError("raw_mail", f"{where} contains raw mail headers")


def normalize_note(note: dict[str, Any]) -> list[str]:
    """Coerce the model's metadata into a combination validate_memory.rb accepts.

    The validator enforces a small state machine over status / fact_status /
    source_check / memory_kind / directory.  A model that violates any one rule
    fails the whole commit after the AI call is already paid for, so we repair
    it here instead -- and always in the conservative direction: an uncertain
    note remains review_required and is later routed to its business page,
    never promoted into a confirmed fact.
    """
    changes: list[str] = []

    def set_field(field: str, value: str, why: str) -> None:
        if note[field] != value:
            changes.append(f"{note['ref']}.{field}: {note[field]} -> {value} ({why})")
            note[field] = value

    special = ("guardrail", "source_index")

    def uncertain() -> str:
        # A discrepant source check is only ever valid alongside "conflicting",
        # so every demotion has to respect that rather than default to pending.
        return "conflicting" if note["source_check"] == "discrepant" else "pending"

    # not_applicable is reserved for guardrail / source_index notes.
    if note["fact_status"] == "not_applicable" and note["memory_kind"] not in special:
        set_field("fact_status", uncertain(), "not_applicable 仅限 guardrail/source_index")
    # A discrepant source check only pairs with a conflicting fact.
    if note["source_check"] == "discrepant":
        set_field("fact_status", "conflicting", "discrepant 必须配 conflicting")
    # An unverified source can never back a confirmed fact.
    if note["source_check"] == "unverified" and note["fact_status"] == "confirmed":
        set_field("fact_status", "pending", "unverified 不能是 confirmed")

    if note["memory_kind"] in special:
        if note["source_check"] == "matched":
            # A guardrail must be active + not_applicable; review_required is
            # unreachable for it because that state demands pending/conflicting.
            set_field("fact_status", "not_applicable", "guardrail/source_index 要求 not_applicable")
            set_field("status", "active", "guardrail/source_index 只能是 active")
        else:
            set_field("memory_kind", "judgment", "来源未核实，达不到 guardrail 的确定性要求")
            set_field("fact_status", uncertain(), "降级为待确认")

    if note["memory_kind"] not in special:
        if note["fact_status"] == "confirmed" and note["source_check"] == "matched":
            set_field("status", "active", "confirmed + matched 才可 active")
        else:
            if note["fact_status"] not in ("pending", "conflicting"):
                set_field("fact_status", uncertain(), "证据不足，按待确认处理")
            set_field("status", "review_required", "非确认事实必须待复核")

    if note["directory"] not in WRITE_DIRS:
        set_field("directory", "02_内部业务资料", "业务分类缺失时按内部资料保守路由")

    # category has to keep matching whatever directory we ended up with.
    expected = CATEGORY_FOR_DIR.get(note["directory"])
    if expected and note["category"] != expected:
        set_field("category", expected, "category 必须与目录一致")

    return changes


def include_administrative_results(batch: dict[str, Any], ai: dict[str, Any]) -> dict[str, Any]:
    """Filesystem lifecycle events are deterministic and consume no model output."""
    ai = json.loads(json.dumps(ai))
    for item in batch["items"]:
        if item.get("analysis_required"):
            continue
        excluded = item.get("exclusion_reason") == "source_path_excluded"
        deleted = item.get("current_sha256") is None
        ai.setdefault("source_results", []).append({
            "event_id": item["event_id"],
            "disposition": "tombstone_recorded" if deleted else "no_business_change",
            "new_note_refs": [],
            "existing_target_memory_ids": item.get("existing_target_memory_ids") or [],
            "note": ("来源路径已排除采集，仅完成历史事件登记，未调用 AI 分析。" if excluded else
                     "来源删除事件已登记，既有业务记忆保留。" if deleted else
                     "来源内容指纹已处理，仅登记本次元数据变化。"),
        })
    return ai


def validate_ai(batch: dict[str, Any], ai: dict[str, Any]) -> None:
    """Enforce the merger's contract locally, before anything is written."""
    if ai.get("run_id") != batch["run_id"]:
        raise AutomationError("ai_run_id", "model returned a different run id")
    items = {item["event_id"]: item for item in batch["items"]}
    results = ai.get("source_results") or []
    seen = [r.get("event_id") for r in results]
    if sorted(seen) != sorted(items) or len(seen) != len(set(seen)):
        missing = sorted(set(items) - set(seen))
        extra = sorted(set(seen) - set(items))
        dupes = sorted({e for e in seen if seen.count(e) > 1})
        detail = (f"应覆盖 {len(items)} 条，实际 {len(seen)} 条"
                  f"；漏 {len(missing)}：{missing[:4]}"
                  f"；多 {len(extra)}：{extra[:4]}"
                  f"；重复 {len(dupes)}：{dupes[:4]}")
        raise AutomationError("ai_coverage", f"proposal must cover each source item exactly once（{detail}）")

    refs = {}
    for note in ai.get("new_notes") or []:
        ref = note["ref"]
        if ref in refs:
            raise AutomationError("ai_ref", f"duplicate new note ref {ref}")
        if note["directory"] not in WRITE_DIRS:
            raise AutomationError("ai_directory", f"disallowed target directory {note['directory']}")
        expected = CATEGORY_FOR_DIR.get(note["directory"])
        if expected and note["category"] != expected:
            raise AutomationError(
                "ai_category", f"category {note['category']} does not match {note['directory']}"
            )
        slug = note["filename_slug"]
        if not slug or "/" in slug or "\\" in slug or slug.startswith("."):
            raise AutomationError("ai_slug", f"unsafe filename slug {slug!r}")
        for field in ("memory_content", "evidence_boundary", "source_section", "update_log"):
            value = (note.get(field) or "").strip()
            if not value:
                raise AutomationError("ai_body", f"new note {ref} has an empty {field}")
            scan_forbidden(value, f"new note {ref} {field}")
        scan_forbidden(note.get("source_ref", ""), f"new note {ref} source_ref")
        supersedes = note.get("supersedes") or []
        if len(supersedes) != len(set(supersedes)) or any(
            not MEMORY_ID_RE.match(str(memory_id)) for memory_id in supersedes
        ):
            raise AutomationError("ai_supersedes", f"new note {ref} has invalid supersedes")
        known_ids = {item["memory_id"] for item in memory_index()}
        unknown = sorted(set(supersedes) - known_ids)
        if unknown:
            raise AutomationError("ai_supersedes", f"new note {ref} supersedes unknown IDs: {unknown}")
        refs[ref] = note

    used_refs: set[str] = set()
    for result in results:
        item = items[result["event_id"]]
        disposition = result["disposition"]
        new_refs = result.get("new_note_refs") or []
        existing = result.get("existing_target_memory_ids") or []
        for ref in new_refs:
            if ref not in refs:
                raise AutomationError("ai_ref", f"result references unknown new note {ref}")
            used_refs.add(ref)
        for memory_id in existing:
            if not MEMORY_ID_RE.match(memory_id):
                raise AutomationError("ai_target", f"invalid memory id {memory_id}")
        if item.get("analysis_required") and disposition == "tombstone_recorded":
            raise AutomationError("ai_disposition", "content item cannot be a tombstone")
        if not item.get("analysis_required") and item.get("current_sha256") is None \
                and disposition != "tombstone_recorded":
            raise AutomationError("ai_disposition", "deleted item must be a tombstone")
        if disposition == "merged" and not new_refs and not existing:
            raise AutomationError("ai_disposition", "merged result needs at least one target")
        if disposition == "tombstone_recorded" and new_refs:
            raise AutomationError("ai_disposition", "tombstone result cannot create notes")
        scan_forbidden(result.get("note", ""), "result note")

    orphans = set(refs) - used_refs
    if orphans:
        raise AutomationError("ai_ref", f"new notes never referenced: {sorted(orphans)}")
    raw_relations = ai.get("claim_relations") or []
    try:
        ai["claim_relations"] = reconciliation.validate_relations(
            raw_relations,
            allowed_handles=reconciliation.open_claim_handles() if raw_relations else set(),
            allowed_tokens=set(items),
        )
    except reconciliation.ReconciliationError as exc:
        raise AutomationError("ai_claim_relation", exc.message) from exc


def render_note(memory_id: str, note: dict[str, Any], observed_at: str) -> str:
    """Build the staged note exactly in the shape validate_staged_note expects.

    content_hash is a placeholder on purpose: commit runs repair_content_hash and
    rewrites it from the canonical body, so computing it here would only risk
    disagreeing with the merger's own canonicalisation.
    """
    source_ref = note["source_ref"].replace('"', "'")
    frontmatter = [
        "---",
        f"memory_id: {memory_id}",
        f"category: {note['category']}",
        "memory_scope: internal_nonpublic",
        f"memory_kind: {note['memory_kind']}",
        f"origin_type: {note['origin_type']}",
        f"status: {note['status']}",
        f"evidence_level: {note['evidence_level']}",
        f"sensitivity: {note['sensitivity']}",
        "source_system: codex",
        f"source_check: {note['source_check']}",
        f"fact_status: {note['fact_status']}",
        f'source_ref: "{source_ref}"',
        f"observed_at: {observed_at}",
        f"processed_at: {observed_at}",
        "reviewed_at: null",
        "valid_until: null",
        "supersedes:" if note.get("supersedes") else "supersedes: []",
        *[f"  - {memory_id}" for memory_id in note.get("supersedes", [])],
        f'content_hash: "{PLACEHOLDER_HASH}"',
        "applies_to:",
        "  - project:business-energy",
        "---",
        "",
    ]
    body = [
        "## 记忆内容",
        "",
        note["memory_content"].strip(),
        "",
        "## 证据边界",
        "",
        note["evidence_boundary"].strip(),
        "",
        "## 来源",
        "",
        note["source_section"].strip(),
        "",
        "## 更新记录",
        "",
        note["update_log"].strip(),
        "",
    ]
    return "\n".join(frontmatter + body)


def safe_slug(slug: str) -> str:
    cleaned = unicodedata.normalize("NFC", slug).strip()
    cleaned = re.sub(r"[\\/:\0*?\"<>|]", "", cleaned)
    cleaned = cleaned.strip(". ")
    return cleaned or "未命名"


def stage_notes(run_dir: Path, ai: dict[str, Any], memory_ids: list[str],
                config: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    staging = run_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    staging.chmod(0o700)
    observed_at = now_iso()
    today = datetime.now(CN_TZ).strftime("%Y%m%d")
    memory_root = Path(config["memory_root"])
    vault_root = business_router.vault_root_from_config(config)

    ref_to_id: dict[str, str] = {}
    writes: list[dict[str, Any]] = []
    taken: set[str] = set()
    page_overrides: dict[Path, str] = {}
    page_claim_ids: dict[Path, list[str]] = {}
    local_dedupe: dict[str, str] = {}
    for note, memory_id in zip(ai.get("new_notes") or [], memory_ids):
        uncertain = note["status"] == "review_required" or note["fact_status"] in ("pending", "conflicting")
        if uncertain:
            dedupe_material = "|".join((note["source_ref"], note["filename_slug"]))
            dedupe_hash = business_router.sha256_text(dedupe_material)[:32]
            if dedupe_hash in local_dedupe:
                ref_to_id[note["ref"]] = local_dedupe[dedupe_hash]
                continue
            candidate = {
                "action": "conflict" if note["fact_status"] == "conflicting" else "annotate_page",
                "title": note["filename_slug"],
                "summary": note["memory_content"],
                "memory_content": note["memory_content"],
                "evidence_boundary": note["evidence_boundary"],
                "conflict_reason": note["evidence_boundary"] if note["fact_status"] == "conflicting" else "",
                "category": note["category"],
                "evidence_state": "conflicting" if note["fact_status"] == "conflicting" else "pending",
                "dedupe_key": dedupe_material,
            }
            path, updated, route = business_router.route_claim_change(
                config,
                candidate,
                source_system="windows_folder_watch",
                source_ref=note["source_ref"],
                observed_at=observed_at,
                claim_id=memory_id,
                page_overrides=page_overrides,
            )
            ref_to_id[note["ref"]] = route.claim_id
            local_dedupe[route.dedupe_hash] = route.claim_id
            page_overrides[path] = updated
            page_claim_ids.setdefault(path, []).append(route.claim_id)
            continue

        ref_to_id[note["ref"]] = memory_id
        slug = safe_slug(note["filename_slug"])
        directory = note["directory"]
        # Never collide with an existing note: this pipeline only adds.
        candidate = f"{today}-{slug}.md"
        suffix = 1
        while (memory_root / directory / candidate).exists() or f"{directory}/{candidate}" in taken:
            suffix += 1
            candidate = f"{today}-{slug}-{suffix}.md"
        taken.add(f"{directory}/{candidate}")
        relative = f"{directory}/{candidate}"

        text = render_note(memory_id, note, observed_at)
        scan_forbidden(text, f"staged note {memory_id}")
        staged_name = f"{memory_id}.md"
        staged_path = staging / staged_name
        staged_path.write_text(text, encoding="utf-8")
        staged_path.chmod(0o600)
        writes.append({
            "target_scope": "memory",
            "relative_path": relative,
            "staged_file": staged_name,
            "expected_sha256": None,
            "claim_ids": [memory_id],
        })

    for index, path in enumerate(sorted(page_overrides, key=lambda value: value.as_posix()), start=1):
        updated = page_overrides[path]
        relative = path.relative_to(vault_root).as_posix()
        staged_claim_ids = set(page_claim_ids[path])
        changed_blocks = [
            str(updated)[span[0]:span[1]]
            for payload, span in business_router.iter_claims(str(updated))
            if str(payload.get("claim_id") or "") in staged_claim_ids
        ]
        scan_forbidden("\n".join(changed_blocks), f"business claim page {relative}")
        staged_name = f"business-{index:03d}.md"
        staged_path = staging / staged_name
        staged_path.write_text(str(updated), encoding="utf-8")
        staged_path.chmod(0o600)
        writes.append({
            "target_scope": "business",
            "relative_path": relative,
            "staged_file": staged_name,
            "expected_sha256": getattr(updated, "expected_sha256", None),
            "claim_ids": page_claim_ids[path],
        })
    return ref_to_id, writes


def build_proposal(batch: dict[str, Any], batch_sha: str, ai: dict[str, Any],
                   ref_to_id: dict[str, str], writes: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for result in ai["source_results"]:
        targets = list(result.get("existing_target_memory_ids") or [])
        for ref in result.get("new_note_refs") or []:
            memory_id = ref_to_id[ref]
            if memory_id not in targets:
                targets.append(memory_id)
        results.append({
            "event_id": result["event_id"],
            "disposition": result["disposition"],
            "target_memory_ids": targets,
            "note": result.get("note", "").strip(),
        })
    return {
        "schema_version": 1,
        "run_id": batch["run_id"],
        "batch_sha256": batch_sha,
        "source_results": results,
        "writes": writes,
    }


def rebuild_indexes(run_id: str) -> None:
    """Bring the Obsidian index files back in line with the notes on disk."""
    environment = dict(os.environ)
    environment.setdefault("LANG", "en_US.UTF-8")
    environment["LC_ALL"] = "en_US.UTF-8"
    try:
        result = subprocess.run(
            [RUBY_BIN, "ruby", str(ROOT / "bin" / "refresh_memory_indexes.rb")],
            capture_output=True, text=True, timeout=300, check=False, env=environment,
        )
        append_log("indexes_rebuilt", run_id=run_id, ok=result.returncode == 0)
    except Exception as exc:  # noqa: BLE001 - never mask the original failure
        append_log("index_rebuild_failed", run_id=run_id, error=type(exc).__name__)


def run_once(config: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    prepared = run_merge("prepare")
    if prepared.get("status") == "noop":
        append_log("prepare_noop")
        return {"status": "noop", "pending_events": 0, "committed": 0, "new_notes": 0}

    run_id = prepared["run_id"]
    batch_path = Path(prepared["batch_path"])
    batch_sha = prepared["batch_sha256"]
    run_dir = batch_path.parent
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    append_log("prepared", run_id=run_id, content_items=prepared.get("content_items"),
               administrative_items=prepared.get("administrative_items"),
               total_bytes=prepared.get("total_bytes"))

    try:
        payload = build_payload(batch)

        def acceptable(text: str) -> dict[str, Any]:
            # The same checks propose() applies, run on a copy inside the model
            # router so one model's rejected answer moves the chain on.
            parsed = parse_model_json(text)
            probe = include_administrative_results(batch, json.loads(json.dumps(parsed)))
            for note in probe.get("new_notes") or []:
                normalize_note(note)
            validate_ai(batch, probe)
            if len(probe.get("new_notes") or []) > ALLOCATE_MAX:
                raise AutomationError("too_many_notes", "model proposed too many notes")
            return parsed

        def propose(hint: str | None) -> dict[str, Any]:
            candidate = invoke_codex(config, payload, run_dir, retry_hint=hint,
                                     accept=acceptable) if payload["items"] else {
                "run_id": batch["run_id"], "source_results": [], "new_notes": [], "claim_relations": []}
            candidate = include_administrative_results(batch, candidate)
            repairs: list[str] = []
            for note in candidate.get("new_notes") or []:
                repairs.extend(normalize_note(note))
            if repairs:
                append_log("metadata_normalized", run_id=run_id, changes=repairs)
            validate_ai(batch, candidate)
            proposed_count = len(candidate.get("new_notes") or [])
            if proposed_count > ALLOCATE_MAX:
                raise AutomationError(
                    "too_many_notes", f"model proposed {proposed_count} notes, limit {ALLOCATE_MAX}"
                )
            return candidate

        try:
            ai = propose(None)
        except AutomationError as exc:
            if exc.code not in RETRYABLE_PROPOSAL_ERRORS:
                raise
            # Only malformed or policy-rejected model output gets one retry.
            # Missing binaries, timeouts and nonzero exits are infrastructure
            # failures; retrying them would double an already unhealthy run.
            append_log("proposal_rejected_retrying", run_id=run_id, code=exc.code,
                       message=exc.message[:300])
            ai = propose(f"[{exc.code}] {exc.message}")
            append_log("retry_succeeded", run_id=run_id)

        new_notes = ai.get("new_notes") or []
        memory_ids: list[str] = []
        if new_notes:
            reserved = run_merge("allocate", run_id, str(len(new_notes)))
            memory_ids = reserved["memory_ids"]
            if len(memory_ids) != len(new_notes):
                raise AutomationError("allocate", "reserved id count does not match proposed notes")

        ref_to_id, writes = stage_notes(run_dir, ai, memory_ids, config)
        proposal = build_proposal(batch, batch_sha, ai, ref_to_id, writes)
        proposal_path = run_dir / "proposal.json"
        proposal_path.write_text(
            json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        proposal_path.chmod(0o600)

        dispositions: dict[str, int] = {}
        for result in proposal["source_results"]:
            dispositions[result["disposition"]] = dispositions.get(result["disposition"], 0) + 1

        if dry_run:
            run_merge("fail", run_id, "dry_run", "dry run completed without database commit")
            append_log("dry_run_complete", run_id=run_id, new_notes=len(new_notes),
                       dispositions=dispositions)
            return {"status": "dry_run", "run_id": run_id, "new_notes": len(new_notes),
                    "dispositions": dispositions, "proposal_path": str(proposal_path),
                    "committed": 0}

        try:
            committed = run_merge("commit", str(proposal_path), timeout=600)
        except AutomationError:
            # A failed commit rolls the notes back but leaves the already-refreshed
            # indexes pointing at them.  That mismatch fails validation on every
            # later run too, so an unattended agent would be stuck forever.  Rebuild
            # the indexes before re-raising so the next run starts clean.
            rebuild_indexes(run_id)
            raise
        append_log("committed", run_id=run_id, new_notes=len(new_notes),
                   dispositions=dispositions, result=committed.get("status"))
        source_material = {
            str(item["event_id"]): {
                "material": " ".join((str(item.get("relative_path") or ""), str(item.get("content") or ""))),
                "directness": "direct",
                "verification_method": "model_proposed",
            }
            for item in payload["items"]
        }
        relation_receipt = reconciliation.submit_relations(
            config,
            list(ai.get("claim_relations") or []),
            source_system="win11",
            source_material=source_material,
            observed_at=str(batch.get("cutoff_event_at") or now_iso()),
        )
        try:
            refresh_receipt: dict[str, Any] = reconciliation.refresh_index(
                config,
                run_id=f"win11-refresh-{run_id}",
            )
        except (OSError, sqlite3.Error, reconciliation.ReconciliationError, business_router.RoutingError) as exc:
            refresh_receipt = {
                "status": "deferred",
                "error_code": getattr(exc, "code", type(exc).__name__),
            }
        return {
            "status": committed.get("status", "committed"),
            "run_id": run_id,
            "new_notes": len(new_notes),
            "dispositions": dispositions,
            "committed": 1,
            "remaining_content_items": int(prepared.get("remaining_content_items") or 0),
            "remaining_batches": int(prepared.get("remaining_batches") or 0),
            "blocked_items": int(prepared.get("blocked_items") or 0),
            "claim_relations": relation_receipt,
            "reconciliation": refresh_receipt,
        }
    except Exception as exc:
        code = exc.code if isinstance(exc, AutomationError) else type(exc).__name__
        message = exc.message if isinstance(exc, AutomationError) else str(exc)
        try:
            closed = run_merge("fail", run_id, code, message[:500] or code)
            append_log("prepared_run_closed", run_id=run_id,
                       error_code=closed.get("error_code"), original_code=code)
        except AutomationError as close_error:
            if close_error.code == "merge_memory_lock_timeout":
                import win11_precommit_closure
                win11_precommit_closure.request(ROOT, run_id, batch_sha, code)
            append_log("prepared_run_close_failed", run_id=run_id, code=close_error.code,
                       message=close_error.message[:300], original_code=code)
        raise


def self_test() -> dict[str, Any]:
    tests = 0

    def check(condition: bool, label: str) -> None:
        nonlocal tests
        if not condition:
            raise AssertionError(f"self-test failed: {label}")
        tests += 1

    check("ai_coverage" in RETRYABLE_PROPOSAL_ERRORS, "model validation error is retryable")
    check("codex_failed" not in RETRYABLE_PROPOSAL_ERRORS, "nonzero Codex exit is not retryable")
    check("codex_timeout" not in RETRYABLE_PROPOSAL_ERRORS, "Codex timeout is not retryable")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def schema_contains(value: Any, key: str) -> bool:
        if isinstance(value, dict):
            return key in value or any(schema_contains(item, key) for item in value.values())
        if isinstance(value, list):
            return any(schema_contains(item, key) for item in value)
        return False

    check(not schema_contains(schema, "uniqueItems"), "Codex response schema uses supported subset")
    check(
        codex_error_summary('ERROR: {"error":{"message":"synthetic schema failure"}}')
        == "synthetic schema failure",
        "Codex structured stderr summary extracted",
    )
    duplicate_id = "BUSINESS-20260824-001"
    duplicate_candidate = {
        "run_id": "win11-20260826T120000-1234abcd",
        "source_results": [{
            "event_id": 'a2b81d52-6623-5666-90a1-f6551d14424d',
            "disposition": "merged", "existing_target_memory_ids": [],
            "new_note_refs": ["N1"], "note": "合成测试",
        }],
        "new_notes": [{
            "ref": "N1", "directory": "02_内部业务资料", "filename_slug": "合成测试",
            "category": "internal_material", "memory_kind": "status",
            "origin_type": "internal_file", "status": "review_required",
            "evidence_level": "unverified", "sensitivity": "internal",
            "source_check": "unverified", "fact_status": "pending", "source_ref": "synthetic",
            "supersedes": [duplicate_id, duplicate_id], "memory_content": "合成测试内容",
            "evidence_boundary": "仅用于本地自测", "source_section": "合成来源",
            "update_log": "建立合成记录",
        }],
    }
    duplicate_batch = {
        "run_id": duplicate_candidate["run_id"],
        "items": [{
            "event_id": 'a2b81d52-6623-5666-90a1-f6551d14424d',
            "analysis_required": True, "current_sha256": "0" * 64,
        }],
    }
    try:
        validate_ai(duplicate_batch, duplicate_candidate)
        check(False, "duplicate supersedes rejected locally")
    except AutomationError as exc:
        check(exc.code == "ai_supersedes", "duplicate supersedes rejected locally")

    with tempfile.TemporaryDirectory(prefix="business-win11-automation-") as temporary:
        run_dir = Path(temporary)
        persist_codex_failure(
            run_dir, None, kind="nonzero_exit", stderr=b"diagnostic\n", returncode=1
        )
        receipt = json.loads((run_dir / "codex_failure.json").read_text(encoding="utf-8"))
        check(receipt["stderr"] == "diagnostic\n" and receipt["returncode"] == 1,
              "Codex failure receipt persisted")
        check((run_dir / "codex_failure.json").stat().st_mode & 0o777 == 0o600,
              "Codex failure receipt is owner-only")

        run_id = "win11-20260826T120000-1234abcd"
        batch_path = run_dir / "batch.json"
        batch_path.write_text(
            json.dumps({"run_id": run_id, "source_id": "test", "items": [{
                "event_id": 'a2b81d52-6623-5666-90a1-f6551d14424d',
                "source_entry_id": "fixture", "operation": "upsert",
                "relative_path": "fixture.txt", "analysis_required": True,
                "current_sha256": "0" * 64, "size_bytes": 1,
            }]}),
            encoding="utf-8",
        )
        calls: list[tuple[str, ...]] = []
        invocations = 0
        original_merge = globals()["run_merge"]
        original_invoke = globals()["invoke_codex"]
        original_append = globals()["append_log"]

        def fake_merge(*args: str, timeout: int = 300) -> dict[str, Any]:
            del timeout
            calls.append(args)
            if args[0] == "prepare":
                return {
                    "status": "prepared", "run_id": run_id, "batch_path": str(batch_path),
                    "batch_sha256": "0" * 64, "content_items": 1,
                    "administrative_items": 0, "total_bytes": 1,
                }
            if args[0] == "fail":
                return {"status": "failed", "error_code": args[2]}
            raise AssertionError(f"unexpected merge call: {args}")

        def fake_invoke(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal invocations
            invocations += 1
            raise AutomationError("codex_failed", "simulated infrastructure failure")

        globals()["run_merge"] = fake_merge
        globals()["invoke_codex"] = fake_invoke
        globals()["append_log"] = lambda *_args, **_kwargs: None
        try:
            try:
                run_once({"memory_root": str(run_dir)}, False)
                check(False, "infrastructure failure propagated")
            except AutomationError as exc:
                check(exc.code == "codex_failed", "infrastructure failure propagated")
        finally:
            globals()["run_merge"] = original_merge
            globals()["invoke_codex"] = original_invoke
            globals()["append_log"] = original_append
        check(invocations == 1, "infrastructure failure is not retried")
        check(len([call for call in calls if call[0] == "fail"]) == 1,
              "prepared run is closed after infrastructure failure")

    return {"verdict": "pass", "tests_passed": tests}


def main() -> int:
    parser = argparse.ArgumentParser(description="Win11 记忆语义合并自动化")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="只跑到 proposal，不写入 Obsidian")
    parser.add_argument("--max-rounds", type=int, default=3,
                        help="单次运行最多连续处理多少批（默认 3）")
    args = parser.parse_args()

    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, sort_keys=True))
        return 0

    try:
        ensure_maintenance_inactive()
    except AutomationError as exc:
        append_log("blocked", code=exc.code)
        print(json.dumps({"status": "blocked", "code": exc.code}, ensure_ascii=False))
        return 0

    try:
        lock = acquire_lock()
    except AutomationError as exc:
        append_log("skipped", code=exc.code)
        print(json.dumps({"status": "skipped", "code": exc.code}, ensure_ascii=False))
        return 0

    summary = {"rounds": 0, "new_notes": 0, "committed": 0, "status": "noop"}
    try:
        write_health("running", "merge started")
        for round_index in range(max(1, args.max_rounds)):
            outcome = run_once(load_config(), args.dry_run)
            summary["rounds"] = round_index + 1
            summary["status"] = outcome["status"]
            summary["new_notes"] += outcome.get("new_notes", 0)
            summary["committed"] += outcome.get("committed", 0)
            for field in ("remaining_content_items", "remaining_batches", "blocked_items"):
                summary[field] = int(outcome.get(field) or 0)
            if outcome["status"] in ("noop", "dry_run"):
                break
            if summary["blocked_items"] and not summary["remaining_batches"]:
                break
        remaining = run_merge("status")
        summary["pending_events"] = remaining.get("pending_events")
        if summary.get("pending_events"):
            summary["status"] = "partial"
            # Never let a bounded run look like full coverage.
            append_log("pending_remaining", pending_events=summary["pending_events"],
                       rounds=summary["rounds"])
        # summary carries its own "status" key, so pass it under a distinct name.
        write_health("idle", "merge finished",
                     **{k: v for k, v in summary.items() if k != "status"},
                     merge_status=summary["status"])
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except AutomationError as exc:
        append_log("failed", code=exc.code, message=exc.message[:300])
        write_health("failed", exc.code)
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort guard for an unattended agent
        append_log("failed", code=type(exc).__name__, message=str(exc)[:300])
        write_health("failed", type(exc).__name__)
        print(json.dumps({"status": "error", "code": type(exc).__name__, "message": str(exc)[:300]},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
