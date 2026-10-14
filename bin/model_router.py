#!/usr/bin/env python3
"""Ordered model failover for every governed model call.

Callers used to shell out to `codex exec` with one fixed model. Codex is a
single client in front of every provider, so when it hung (2020-01-02: stuck
refreshing its model list) all providers failed together, and one provider
running out of quota stopped the pipeline even when another had capacity.

This module walks an ordered chain instead. MiMo and Kimi are called directly
over HTTP (OpenAI-compatible chat/completions); GPT keeps its ChatGPT
subscription route through codex. A provider that fails is cooled down for a
duration that depends on why it failed, and the next one is tried. When every
provider in a pass has failed or is cooling down, one more pass ignores the
cooldowns, so a provider that recovered early is still found.

Callers pass their own parser as `validate`. An answer it rejects counts as a
failed attempt, so the chain moves on instead of handing the caller text it
cannot use. Schema compliance is still asked for in the prompt (see
dingtalk_memory_automation.mimo_schema_prompt), which works for every provider.

2020-01-02 Example Owner decisions, in force:
  * order DeepSeek → Kimi, then back to DeepSeek once, then report; Claude/GPT
    and MiMo paused (MiMo quota exhausted; kept below for restoration);
  * text and images on deepseek-flash (DeepSeek V4.1 Flash, natively
    multimodal), Kimi on K3, each at its highest thinking level;
  * only when every one of them has failed does this report on Feishu, once
    per purpose per two hours. No asking first.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import base64
import fcntl
import http.client
import json
import mimetypes
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "state"
HEALTH_PATH = STATE_DIR / "model-router-health.json"
HEALTH_LOCK = STATE_DIR / "model-router-health.lock"
LOG_PATH = ROOT / "logs" / "model-router.jsonl"
REJECTED_DIR = ROOT / "logs" / "model-rejections"
REJECTED_KEEP = 40
LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_GENERATIONS = 3

# Provider endpoints. Only these hosts ever receive a key.
# `options` are request-body extras that switch each provider to its highest
# thinking level (2020-01-02 Example Owner rule). The accepted values differ and are
# validated by the providers: MiMo rejects "max" (low/medium/high only) and
# thinks by default, Kimi K3 takes low/high/max. Claude runs through the
# subscription CLI, where the level is a flag instead.
PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {"transport": "http", "base_url": "https://api.deepseek.com",
                 "key_file": "deepseek_api_key",
                 "options": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}},
    "mimo": {"transport": "http", "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
             "key_file": "mimo_api_key",
             "options": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}},
    "kimi": {"transport": "http", "base_url": "https://api.kimi.com/coding/v1",
             "key_file": "kimi_api_key",
             "options": {"reasoning_effort": "max"}},
    "claude": {"transport": "claude_cli", "effort": "high"},
    "gpt": {"transport": "codex"},
}

DEFAULT_ROUTING: dict[str, Any] = {
    "enabled_providers": ["deepseek", "kimi"],
    "text_chain": ["deepseek:deepseek-flash", "kimi:k3"],
    "vision_chain": ["deepseek:deepseek-flash", "kimi:k3"],
    "connect_timeout_seconds": 15,
    "per_attempt_timeout_seconds": 420,
    "max_output_tokens": 32768,
    # Seconds a provider is skipped after each kind of failure. Quota and auth
    # problems do not clear in minutes; network blips do. An answer this caller
    # could not use says nothing about the provider's health, so it carries no
    # cooldown: another purpose's call may well succeed on it.
    "cooldown_seconds": {
        "quota": 1800, "auth": 21600, "timeout": 600, "server": 300,
        "network": 180, "bad_request": 300, "empty": 300, "unconfigured": 0,
        "invalid_output": 0,
    },
}


class RouterExhausted(RuntimeError):
    """Every provider in every pass failed. `category` is the last failure's kind."""

    def __init__(self, category: str, attempts: list[dict[str, Any]]) -> None:
        self.category = category
        self.attempts = attempts
        summary = "; ".join(f"{a['provider']}:{a['category']}" for a in attempts) or "no providers"
        super().__init__(f"model_chain_exhausted ({summary})")


@dataclass
class RouteResult:
    text: str
    provider: str
    model: str
    seconds: float
    attempts: list[dict[str, Any]] = field(default_factory=list)
    # What the caller's `validate` returned for `text`, so the answer is parsed
    # once. None when the caller passed no validator.
    value: Any = None
    classification: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    operation_id: str | None = None
    cached: bool = False


class ProviderReply(str):
    def __new__(cls, text, response_id=None, usage=None, finish_error=None):
        obj = str.__new__(cls, text)
        obj.response_id, obj.usage, obj.finish_error = response_id, usage, finish_error
        return obj


class _AttemptFailed(Exception):
    def __init__(self, category: str, detail: str = "", code: str = "") -> None:
        self.category = category
        self.detail = detail[:300]
        self.code = code  # the caller's own rejection code, for invalid_output
        super().__init__(category)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def routing(config: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULT_ROUTING, **(config.get("model_routing") or {})}
    merged["cooldown_seconds"] = {**DEFAULT_ROUTING["cooldown_seconds"],
                                  **((config.get("model_routing") or {}).get("cooldown_seconds") or {})}
    enabled = merged["enabled_providers"]
    if not isinstance(enabled, list) or not enabled or any(p not in PROVIDERS for p in enabled):
        raise ValueError("enabled_providers_invalid")
    for kind in ("text", "vision"):
        merged[kind + "_chain"] = [entry for entry in merged[kind + "_chain"]
                                    if str(entry).partition(":")[0] in enabled]
        if not merged[kind + "_chain"]:
            raise ValueError("enabled_model_chain_empty")
    return merged


def provider_for(model: str) -> str:
    """Map a bare model id to its provider by family prefix."""
    if str(model) == "k3":
        return "kimi"
    for prefix, provider in (("deepseek", "deepseek"), ("mimo", "mimo"), ("kimi", "kimi"),
                              ("claude", "claude"), ("gpt", "gpt")):
        if str(model).startswith(prefix):
            return provider
    raise ValueError(f"no provider for model {model!r}")


def chain_preferring(config: dict[str, Any], model: str, *, vision: bool = False) -> list[str]:
    """The configured chain with `model` moved to the front.

    For callers where a person picked the model (the case workbench), that
    choice is honoured first and the usual chain still backs it up.
    """
    provider = provider_for(model)
    base = routing(config)["vision_chain" if vision else "text_chain"]
    if provider not in routing(config)["enabled_providers"]:
        return list(base)
    return [f"{provider}:{model}"] + [entry for entry in base if not str(entry).startswith(provider + ":")]


def parse_chain(entries: Sequence[str]) -> list[tuple[str, str]]:
    chain = []
    for entry in entries:
        provider, _, model = str(entry).partition(":")
        if provider not in PROVIDERS or not model:
            raise ValueError(f"invalid model_routing entry: {entry!r}")
        chain.append((provider, model))
    return chain


def _read_key(provider: str) -> str | None:
    name = PROVIDERS[provider].get("key_file")
    if not name:
        return None
    path = STATE_DIR / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    # Same guard the MiMo key already had: a regular, private, owned file only.
    if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return None
    key = path.read_text(encoding="utf-8").strip()
    return key or None


# ---------------------------------------------------------------- health state

@contextmanager
def _health_locked() -> Iterator[dict[str, Any]]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(HEALTH_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("schema_version", 1)
        data.setdefault("providers", {})
        yield data
        tmp = HEALTH_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, HEALTH_PATH)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _cooldown_remaining(provider: str) -> float:
    try:
        data = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        until = float(((data.get("providers") or {}).get(provider) or {}).get("cooldown_until") or 0)
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return 0.0
    return max(0.0, until - time.time())


def _mark(provider: str, *, ok: bool, category: str = "", detail: str = "", cooldown: float = 0) -> None:
    with _health_locked() as data:
        entry = data["providers"].setdefault(provider, {})
        stamp = _now_iso()
        if ok:
            entry.update(last_success_at=stamp, consecutive_failures=0, cooldown_until=0)
        else:
            entry.update(last_failure_at=stamp, last_failure_category=category,
                         last_failure_detail=detail[:300],
                         consecutive_failures=int(entry.get("consecutive_failures") or 0) + 1,
                         cooldown_until=time.time() + cooldown if cooldown > 0 else 0)


def health() -> dict[str, Any]:
    """Read-only snapshot for dashboards and diagnostics."""
    try:
        data = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {"providers": {}}
    for entry in (data.get("providers") or {}).values():
        entry["cooldown_remaining_seconds"] = round(max(0.0, float(entry.get("cooldown_until") or 0) - time.time()))
    return data


def _log(record: dict[str, Any]) -> None:
    """Append one line; rotate by size so this can never grow without bound."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            if LOG_PATH.stat().st_size >= LOG_MAX_BYTES:
                for n in range(LOG_GENERATIONS - 1, 0, -1):
                    src = LOG_PATH if n == 1 else Path(f"{LOG_PATH}.{n - 1}")
                    if src.exists():
                        os.replace(src, f"{LOG_PATH}.{n}")
        except FileNotFoundError:
            pass
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass  # Diagnostics must never turn a model answer into a failure.


def _keep_rejected(purpose: str, provider: str, model: str, text: str) -> None:
    """Keep the answer a caller refused, so the next reader sees what came back.

    Until now a MiMo reply that was not JSON was discarded by every caller, and
    the only evidence left was the error code. Keep the newest few, privately.
    """
    try:
        REJECTED_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-z0-9_]+", "-", f"{purpose}-{provider}-{model}".lower())[:80]
        path = REJECTED_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}-{safe}.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(text)[:200_000])
        keep = sorted(REJECTED_DIR.glob("*.txt"))[:-REJECTED_KEEP]
        for stale in keep:
            stale.unlink(missing_ok=True)
    except OSError:
        pass  # diagnostics must never add a second failure


PURPOSE_NAMES = {
    "dingtalk_semantic_proposal": "钉钉聊天整理", "chat_semantic_proposal": "钉钉聊天整理",
    "minutes_semantic": "钉钉听记整理", "email_semantic": "邮件整理",
    "email_attachment_analysis": "附件来源分析",
    "win11_semantic_proposal": "Win11 资料整理", "media_understanding": "图片与媒体理解",
    "case_conversation": "案例对话", "decision_center": "决策中心",
}
CATEGORY_NAMES = {
    "quota": "额度用完", "auth": "登录或密钥无效", "timeout": "超时", "server": "服务端报错",
    "network": "网络不通", "bad_request": "请求被拒绝", "empty": "没有返回内容",
    "invalid_output": "返回内容未通过核验", "unconfigured": "未配置", "cooling_down": "冷却中",
    "duplicate_exhausted": "本份内容的调用次数已用完",
    "uncertain": "请求结果待确认，已暂停重复调用",
}
REJECTION_NAMES = {'attachment_analysis_too_large':'恢复原文后的分析结果超过长度上限',
                   'attachment_analysis_evidence':'引用无法精确对应原文',
                   'attachment_analysis_shape':'分析结果结构不符合要求',
                   'attachment_analysis_empty':'没有可核验的分析内容'}
PROVIDER_NAMES = {"deepseek": "DeepSeek", "mimo": "小米", "kimi": "Kimi", "claude": "Claude", "gpt": "GPT"}


def exhausted_report(purpose: str, exc: "RouterExhausted") -> str:
    """Plain-language failure report for Example Owner (Feishu), not a log dump."""
    job = PURPOSE_NAMES.get(purpose, purpose)
    lines = [f"{job}未完成",
             f"后果：这一步没完成，相关内容没有写进业务记忆。原始消息和文件都还在，没有丢。",
             "原因：按顺序试过的结果——"]
    for attempt in exc.attempts:
        name = PROVIDER_NAMES.get(str(attempt.get("provider")), str(attempt.get("provider")))
        reason = CATEGORY_NAMES.get(str(attempt.get("category")), str(attempt.get("category")))
        reason = REJECTION_NAMES.get(attempt.get('rejection_code'),reason)
        # Provider HTML and raw validation logs are not useful in a user alert.
        lines.append(f"　· {name}（{attempt.get('model')}）：{reason}")
    lines += ["修复：保留待处理内容，按恢复控制器核查；已用完的调用次数不会自动重置。同一问题两小时内只报一次。",
              "需要你操作：暂时不用。若这条反复出现，再看同步中心的待处理项。"]
    return "\n".join(lines)


def report_exhausted(config: dict[str, Any], purpose: str, exc: "RouterExhausted") -> dict[str, str]:
    """2020-01-02 Example Owner rule: only after every model failed, report on Feishu."""
    if _ISOLATED:
        return {"status": "skipped", "reason_code": "isolated_for_tests"}
    try:
        import feishu_notify
        rejection_family = ','.join(sorted({str(a.get('rejection_code')) for a in exc.attempts if a.get('rejection_code')}))
        result = feishu_notify.send(config, exhausted_report(purpose, exc),
                                    dedupe_key=f"model_chain_exhausted:{purpose}:{exc.category}:{rejection_family}")
    except Exception as failure:  # notification must never mask the model failure
        result = {"status": "failed", "reason_code": type(failure).__name__}
    _log({"at": _now_iso(), "purpose": purpose, "outcome": "feishu_report", **result})
    return result


# ------------------------------------------------------------------ transports

def _classify_http(status: int, body: str) -> str:
    lower = body.lower()
    if status == 429 or any(s in lower for s in ("quota", "rate limit", "usage limit", "insufficient", "exceeded")):
        return "quota"
    if status in (401, 403):
        return "auth"
    if status >= 500:
        return "server"
    return "bad_request"


def _image_part(path: Path) -> dict[str, Any]:
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def provider_options(provider: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Request-body extras for a provider, config overriding the defaults."""
    override = (cfg.get("provider_options") or {}).get(provider)
    return {**(PROVIDERS[provider].get("options") or {}), **(override or {})}


def _call_http(provider: str, model: str, prompt: str, images: Sequence[Path], cfg: dict[str, Any],
               timeout: float) -> str:
    key = _read_key(provider)
    if not key:
        raise _AttemptFailed("unconfigured", f"{provider} key file missing or not private")
    content: Any = prompt
    if images:
        content = [{"type": "text", "text": prompt}, *(_image_part(p) for p in images)]
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                       "max_tokens": int(cfg["max_output_tokens"]),
                       **provider_options(provider, cfg)}).encode("utf-8")
    request = urllib.request.Request(
        PROVIDERS[provider]["base_url"] + "/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise _AttemptFailed(_classify_http(exc.code, detail), f"HTTP {exc.code}: {detail}") from None
    except (TimeoutError, OSError) as exc:  # socket.timeout is a TimeoutError on 3.9+
        category = "timeout" if "timed out" in str(exc).lower() or isinstance(exc, TimeoutError) else "network"
        raise _AttemptFailed(category, str(exc)) from None
    except http.client.HTTPException as exc:
        # A reply cut off mid-body (IncompleteRead) is neither OSError nor
        # HTTPError; on 2020-01-02 it escaped the router and failed the Win11
        # run outright instead of moving on to Kimi.
        raise _AttemptFailed("network", f"{type(exc).__name__}: {exc}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _AttemptFailed("server", f"non-JSON response: {exc}") from None
    try:
        choice = payload["choices"][0]
        text = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        if isinstance(payload, dict):
            return ProviderReply('', payload.get('id'), payload.get('usage'), 'invalid_output')
        raise _AttemptFailed('server', 'response is not an object') from None
    finish_error = 'empty' if choice.get('finish_reason') == 'length' or not str(text).strip() else None
    return ProviderReply(str(text), payload.get('id'), payload.get('usage'), finish_error)


def _call_codex(model: str, prompt: str, images: Sequence[Path], cfg: dict[str, Any], timeout: float,
                config: dict[str, Any], workdir: Path | None, reasoning_effort: str,
                extra_flags: Sequence[str]) -> str:
    import dingtalk_memory_automation as memory
    codex_bin = str((config.get("dingtalk_automation") or {}).get("codex_bin") or _archive_path('/Users/example/.local/bin/codex'))
    command = [codex_bin, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
               *memory.isolated_codex_flags(), *memory.governed_provider_flags(model),
               "-m", model, "-c", f'model_reasoning_effort="{reasoning_effort}"', *extra_flags,
               "-s", "read-only", "--skip-git-repo-check"]
    if workdir is not None:
        command += ["-C", str(workdir)]
    for image in images:
        command += ["--image", str(image)]
    command.append("-")
    # Own process group: codex spawns helpers, and on 2020-01-02 a hung helper
    # outlived its parent. Timing out must take the whole group down.
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=memory.subscription_only_environment(), start_new_session=True)
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise _AttemptFailed("timeout", f"codex exceeded {int(timeout)}s") from None
    if process.returncode != 0:
        code = memory.safe_codex_error(stderr, process.returncode)
        category = {"codex_subscription_limit": "quota", "codex_subscription_auth": "auth",
                    "codex_model_unavailable": "bad_request"}.get(code, "server")
        raise _AttemptFailed(category, code)
    if not stdout.strip():
        raise _AttemptFailed("empty", "codex returned no output")
    return stdout


# Claude has no API key here; the Claude Code CLI carries Example Owner's own
# subscription, the same route the Hermes claude-subscription-bridge has run
# under launchd since August. --safe-mode keeps this subprocess away from
# CLAUDE.md, hooks, skills and MCP servers, and --tools "" leaves it no way to
# touch the machine: it answers from the prompt alone, like the HTTP providers.
CLAUDE_BIN_DEFAULT = _archive_path('/Users/example/.local/bin/claude')
_CLAUDE_SCRUB = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                 "CLAUDE_API_KEY", "CLAUDE_BASE_URL", "MIMO_API_KEY", "KIMI_API_KEY", "DEEPSEEK_API_KEY")


def _claude_environment() -> dict[str, str]:
    import dingtalk_memory_automation as memory
    environment = {k: v for k, v in memory.subscription_only_environment().items() if k not in _CLAUDE_SCRUB}
    environment["NO_COLOR"] = "1"
    return environment


def _call_claude(model: str, prompt: str, images: Sequence[Path], cfg: dict[str, Any], timeout: float,
                 config: dict[str, Any], effort: str) -> str:
    binary = str((config.get("dingtalk_automation") or {}).get("claude_bin") or CLAUDE_BIN_DEFAULT)
    command = [binary, "-p", "--safe-mode", "--tools", "", "--disable-slash-commands", "--no-chrome",
               "--no-session-persistence", "--max-turns", "1", "--model", model, "--effort", effort]
    if images:
        command += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            path = Path(image)
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": mimetypes.guess_type(str(path))[0] or "image/jpeg",
                "data": base64.b64encode(path.read_bytes()).decode("ascii")}})
        stdin = json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                           ensure_ascii=False) + "\n"
    else:
        command += ["--output-format", "json"]
        stdin = prompt
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=_claude_environment(),
                                   start_new_session=True)
    except OSError as exc:
        raise _AttemptFailed("unconfigured", f"claude CLI could not be started: {exc}") from None
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise _AttemptFailed("timeout", f"claude exceeded {int(timeout)}s") from None
    if process.returncode != 0:
        detail = (stderr or stdout or "").strip()
        lower = detail.lower()
        if any(word in lower for word in ("usage limit", "rate limit", "quota", "exceeded your")):
            raise _AttemptFailed("quota", detail)
        if any(word in lower for word in ("not logged in", "authentication", "unauthorized", "login")):
            raise _AttemptFailed("auth", detail)
        raise _AttemptFailed("server", detail or f"claude exited {process.returncode}")
    text = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            if event.get("is_error"):
                raise _AttemptFailed("server", str(event.get("result") or event.get("subtype") or "")[:300])
            text = str(event.get("result") or "")
    if not text.strip():
        raise _AttemptFailed("empty", "claude returned no result event")
    return text


# ---------------------------------------------------------------------- router

def complete(config: dict[str, Any], *, prompt: str, purpose: str, images: Sequence[Path] = (),
             timeout: float | None = None, workdir: Path | None = None,
             chain: Sequence[str] | None = None, reasoning_effort: str = "high",
             codex_extra_flags: Sequence[str] = (), validate: Any = None,
             request_context: dict[str, Any] | None = None) -> RouteResult:
    original_validate = validate
    volatile = (request_context or {}).get('volatile_values', {}) if (config.get('model_routing') or {}).get('persistent_guard', False) else {}
    for key, value in volatile.items():
        if not isinstance(value, str) or len(value) < 16:
            raise ValueError('volatile identifier must be a full task identifier')
        prompt = prompt.replace(value, '__stable_' + key + '__')
    def restore(text):
        for key, value in volatile.items():
            text = text.replace('__stable_' + key + '__', value)
        return text
    if volatile and validate:
        validate = lambda text: original_validate(restore(str(text)))
    args = dict(prompt=prompt, purpose=purpose, images=images, timeout=timeout, workdir=workdir,
                chain=chain, reasoning_effort=reasoning_effort,
                codex_extra_flags=codex_extra_flags, validate=validate)
    if not (config.get('model_routing') or {}).get('persistent_guard', False):
        return _complete_original(config, **args)
    import model_call_ledger as ledger
    context = {'scope': str(workdir.resolve()) if workdir else purpose, **(request_context or {})}
    try:
        with ledger.Admission(STATE_DIR / 'model-calls', prompt, images, purpose,
                              context, (config.get('model_routing') or {}).get('usage_tariff')) as guard:
            rows = guard.rows()
            for row in rows:
                if not row.get('response_sha'):
                    continue
                obj = guard.response(row)
                if obj.get('finish_error'):
                    continue
                try:
                    value = validate(obj['text']) if validate else None
                except (TypeError, AttributeError, KeyError, NameError, AssertionError):
                    guard.settle(row['attempt_id'], 'validation_error'); guard.status('validation_error')
                    raise ledger.Held('validation_error') from None
                except Exception:
                    guard.settle(row['attempt_id'], 'invalid_output')
                    continue
                guard.settle(row['attempt_id'], 'validated')
                guard.status('validated'); guard.event('cache_reused', row['provider'])
                return RouteResult(text=restore(obj['text']), provider=row['provider'], model=row['model'], seconds=0,
                                   value=value, usage=obj['usage'], operation_id=guard.key, cached=True)
            if any(r['status'] in ('started', 'uncertain') for r in rows):
                raise ledger.Held('request_uncertain')
            result = _complete_original(config, _guard=guard, **args)
            result.text = restore(str(result.text))
            return result
    except ledger.Held as exc:
        raise RouterExhausted(exc.category, []) from None


def _complete_original(config: dict[str, Any], *, prompt: str, purpose: str, images: Sequence[Path] = (),
             timeout: float | None = None, workdir: Path | None = None,
             chain: Sequence[str] | None = None, reasoning_effort: str = "high",
             codex_extra_flags: Sequence[str] = (),
             validate: Any = None, _guard: Any = None) -> RouteResult:
    """Return the first model answer along the chain that this caller can use.

    `images` switches to the vision chain. `timeout` caps the whole call
    (default: sum of per-attempt limits); each attempt is also capped by
    per_attempt_timeout_seconds so one slow provider cannot eat the budget.
    `reasoning_effort` and `codex_extra_flags` (e.g. a service tier) only
    reach the GPT route; the HTTP providers carry their own thinking options
    and Claude its --effort flag.

    `validate` is the caller's own parser (JSON decode, schema check). An
    answer it rejects is a failed attempt, so the next provider is tried:
    2020-01-02 Example Owner rule, after a MiMo reply that was not JSON stopped the
    chat and minutes pipelines for days while Kimi and GPT were never asked.
    The validated value is returned as RouteResult.value, so no caller parses
    the same text twice.
    """
    cfg = routing(config)
    kind = "vision" if images else "text"
    requested = chain or cfg[f"{kind}_chain"]
    allowed = [entry for entry in requested if str(entry).partition(":")[0] in cfg["enabled_providers"]]
    order = parse_chain(allowed or cfg[f"{kind}_chain"])
    if _guard:
        order = _guard.plan(order)
        if any(provider not in cfg["enabled_providers"] for provider, _ in order):
            raise __import__("model_call_ledger").Held("guard_route_changed")
    # One pass down the chain, then back to the first provider (DeepSeek) once
    # more, ignoring cooldowns. Only after that is the chain exhausted and reported.
    passes = [(order, True)] + ([(order[:1], False)] if order else [])
    deadline = time.monotonic() + float(timeout) if timeout else None
    attempts: list[dict[str, Any]] = []
    value: Any = None
    started = time.monotonic()
    last_category = "unconfigured"

    for cycle, (entries, honour_cooldown) in enumerate(passes, start=1):
        tried_this_cycle = 0
        for provider, model in entries:
            slot = order.index((provider, model)) if cycle == 1 else len(order)
            if _guard and any(r['slot'] == slot for r in _guard.rows()):
                continue
            attempt_id = None
            if deadline is not None and time.monotonic() >= deadline:
                break
            if PROVIDERS[provider]["transport"] == "unconfigured" and not _read_key(provider):
                attempts.append({"provider": provider, "model": model, "cycle": cycle, "category": "unconfigured",
                                 "seconds": 0})
                continue
            remaining_cooldown = _cooldown_remaining(provider)
            if honour_cooldown and remaining_cooldown > 0:
                if _guard:
                    _guard.event('cooling_down', provider)
                attempts.append({"provider": provider, "model": model, "cycle": cycle, "category": "cooling_down",
                                 "seconds": 0, "cooldown_remaining": round(remaining_cooldown)})
                continue
            per_attempt = float(cfg["per_attempt_timeout_seconds"])
            if deadline is not None:
                per_attempt = min(per_attempt, max(1.0, deadline - time.monotonic()))
            tried_this_cycle += 1
            t0 = time.monotonic()
            if _guard:
                attempt_id = _guard.begin(slot, provider, model)
            try:
                transport = PROVIDERS[provider]["transport"]
                if transport == "http":
                    text = _call_http(provider, model, prompt, images, cfg, per_attempt)
                elif transport == "codex":
                    text = _call_codex(model, prompt, images, cfg, per_attempt, config, workdir,
                                       reasoning_effort, codex_extra_flags)
                elif transport == "claude_cli":
                    if _ISOLATED:
                        raise _AttemptFailed("unconfigured", "claude CLI is not started under isolate_for_tests")
                    text = _call_claude(model, prompt, images, cfg, per_attempt, config,
                                        str(PROVIDERS[provider].get("effort") or "high"))
                else:
                    raise _AttemptFailed("unconfigured", f"{provider} has no transport")
                if _guard:
                    _guard.received(attempt_id, text)
                if getattr(text, 'finish_error', None):
                    raise _AttemptFailed(text.finish_error, 'provider output incomplete')
                if validate is not None:
                    try:
                        value = validate(text)
                    except Exception as rejection:  # the caller's own parser said no
                        if _guard and isinstance(rejection, (TypeError, AttributeError, KeyError, NameError, AssertionError)):
                            _guard.settle(attempt_id, 'validation_error'); _guard.status('validation_error')
                            raise __import__('model_call_ledger').Held('validation_error') from None
                        _keep_rejected(purpose, provider, model, text)
                        code = str(getattr(rejection, "code", "") or type(rejection).__name__)
                        raise _AttemptFailed("invalid_output", f"{code}: {rejection}", code=code) from None
            except _AttemptFailed as failure:
                if _guard:
                    state = 'uncertain' if failure.category in ('network', 'timeout') else failure.category
                    _guard.settle(attempt_id, state)
                    if state == 'uncertain':
                        _guard.status('uncertain')
                        raise RouterExhausted('request_uncertain', [{'provider': provider, 'model': model, 'category': state, 'cycle': cycle}]) from None
                seconds = round(time.monotonic() - t0, 1)
                last_category = failure.category
                cooldown = float(cfg["cooldown_seconds"].get(failure.category, 300))
                _mark(provider, ok=False, category=failure.category, detail=failure.detail, cooldown=cooldown)
                attempts.append({"provider": provider, "model": model, "cycle": cycle,
                                 "category": failure.category, "detail": failure.detail[:160], "seconds": seconds,
                                 **({"rejection_code": failure.code} if failure.code else {})})
                continue
            seconds = round(time.monotonic() - t0, 1)
            if _guard:
                _guard.settle(attempt_id, 'validated'); _guard.status('validated')
            _mark(provider, ok=True)
            attempts.append({"provider": provider, "model": model, "cycle": cycle, "category": "ok",
                             "seconds": seconds})
            total = round(time.monotonic() - started, 1)
            _log({"at": _now_iso(), "purpose": purpose, "kind": kind, "outcome": "ok", "provider": provider,
                  "model": model, "seconds": total, "fell_back": len([a for a in attempts if a["category"] != "ok"]) > 0,
                  "attempts": attempts})
            return RouteResult(text=text, provider=provider, model=model, seconds=total,
                               attempts=attempts, value=value,
                               usage=__import__('model_call_ledger').usage_fields(getattr(text, 'usage', None)),
                               operation_id=_guard.key if _guard else None)
        # A pass where every provider was only skipped (cooling down) still earns
        # the retry pass: that is the "come back round to MiMo" rule.
        if deadline is not None and time.monotonic() >= deadline:
            break
        if not _guard and tried_this_cycle == 0 and not any(a["category"] == "cooling_down" for a in attempts if a["cycle"] == cycle):
            break  # nothing usable at all (all unconfigured); another pass cannot help

    if _guard:
        _guard.status('exhausted')
        if not attempts:
            raise RouterExhausted('duplicate_exhausted', [])
    _log({"at": _now_iso(), "purpose": purpose, "kind": kind, "outcome": "exhausted",
          "seconds": round(time.monotonic() - started, 1), "last_category": last_category, "attempts": attempts, "failure_projection": failure_projection(RouterExhausted(last_category, attempts))})
    exhausted = RouterExhausted(last_category, attempts)
    # Example Owner's rule is to hear about it only once every model has had its
    # turn. A chain cut short by the caller's time budget has not: that is
    # logged and retried next round, not reported (a Win11 run on 2020-01-02
    # reported "all failed" before GPT and the MiMo retry were reached).
    reached = {a["provider"] for a in attempts if a["category"] not in ("cooling_down",)}
    final_retry_done = any(a.get("cycle") == len(passes) for a in attempts)
    if reached >= {provider for provider, _ in order} and final_retry_done:
        report_exhausted(config, purpose, exhausted)
    else:
        _log({"at": _now_iso(), "purpose": purpose, "outcome": "feishu_report", "status": "skipped",
              "reason_code": "chain_cut_by_time_budget"})
    raise exhausted


LEGACY_ERROR_CODES = {
    # Callers and their downstream consumers already understand these codes.
    # They now mean "every provider failed, the last one this way", not "Codex
    # alone said so" — which is why a quota code no longer holds a source for a
    # person to clear (see business_sync_supervisor.hold_chat_failure).
    "quota": "codex_subscription_limit", "auth": "codex_subscription_auth", "timeout": "codex_timeout",
    "bad_request": "codex_model_unavailable", "invalid_output": "model_output_invalid",
}


_ISOLATED = False


def isolate_for_tests(root: Path) -> None:
    """Keep a caller's self-test away from everything live.

    Health, log and rejected answers go under `root`; keys are looked up there
    too, so MiMo and Kimi count as unconfigured unless the test puts a key in;
    the Claude CLI is not started and no Feishu report is sent. On 2020-01-02
    caller tests without this reached real MiMo, Claude and Feishu and put live
    providers into cooldown.
    """
    global STATE_DIR, HEALTH_PATH, HEALTH_LOCK, LOG_PATH, REJECTED_DIR, _ISOLATED
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    STATE_DIR, HEALTH_PATH, HEALTH_LOCK = root, root / "health.json", root / "health.lock"
    LOG_PATH, REJECTED_DIR = root / "router.jsonl", root / "rejected"
    _ISOLATED = True


def legacy_error_code(exc: RouterExhausted) -> str:
    if exc.category in {'request_uncertain','duplicate_exhausted','duplicate_inflight','guard_cache_corrupt',
                        'guard_source_conflict','guard_state_invalid','guard_context_invalid','guard_route_changed','validation_error'}:
        return 'model_' + exc.category
    from sync_rule_runtime import model_error
    return model_error(exc.category, exc.attempts)


def failure_projection(exc: RouterExhausted) -> dict[str, Any]:
    return {"error_code": legacy_error_code(exc), "providers": [
        {key: a[key] for key in ("provider", "model", "category", "cycle", "rejection_code", "cooldown_remaining") if key in a}
        for a in exc.attempts]}



if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "health":
        print(json.dumps(health(), ensure_ascii=False, indent=2))
    else:
        print("usage: model_router.py health", file=sys.stderr)
        raise SystemExit(2)
