#!/usr/bin/env python3
"""Coordinate one-time WeChat key enrollment without persisting key bytes."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import json
import os
import re
import selectors
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from collect_wechat_chat import (
    app_receipt,
    discover_active_account_root,
    get_wechat_pid,
    keychain_fetch,
    require_filevault_enabled,
    require_sqlcipher_version,
    snapshot_encrypted_database,
    stable_readonly_copy,
    strict_sip_state,
    target_database_paths,
)
from wechat_common import WeChatSyncError, atomic_write, ensure_private_dir, now_iso, sha256_bytes


os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"

CAPTURE_ERROR_DETAILS = {
    "task_port_unavailable": "the startup debugger could not access the WeChat process",
    "wechat_entrypoint_unavailable": "the WeChat startup entry point could not be reached",
    "wechat_module_unavailable": "the WeChat executable module was not loaded",
    "wechat_core_module_unavailable": "the signed WeChat core module was not loaded",
    "wechat_core_changed": "the signed WeChat core hash differs from the audited 4.1.12 build",
    "wechat_debug_launch_failed": "WeChat could not be started under the audited one-time capture",
    "wechat_text_unavailable": "the WeChat executable code section was unavailable",
    "wechat_text_read_failed": "the WeChat executable code section could not be read",
    "cipher_function_unavailable": "this WeChat build does not match the audited cipher-function locator",
    "cipher_breakpoint_unavailable": "the audited cipher-function breakpoint could not be installed",
    "capture_no_database_keys": "capture ran, but no candidate passed database integrity verification",
    "capture_no_breakpoint_hits": "the cipher-function breakpoint was installed but never reached",
    "capture_key_argument_unreadable": "the cipher-function ran, but its key argument format could not be read",
    "capture_key_salt_unmatched": "captured key arguments did not match any active database salt",
    "capture_key_integrity_unverified": "a database key candidate was found but failed integrity verification",
    "capture_memory_scan_unavailable": "the bounded WeChat memory scan could not be started",
    "capture_memory_no_candidates": "the bounded WeChat memory scan found no cached database-key candidates",
    "capture_internal_setup": "the startup capture stopped during debugger setup",
    "capture_internal_attach": "the startup capture stopped while attaching to WeChat",
    "capture_internal_module_load": "the startup capture stopped while loading the signed WeChat core",
    "capture_internal_locator": "the startup capture stopped while locating the cipher function",
    "capture_internal_breakpoint": "the startup capture stopped while installing the cipher breakpoint",
    "capture_internal_event_loop": "the startup capture stopped while observing cipher calls",
    "capture_internal_protocol": "the startup capture stopped while preparing the Keychain stream",
    "capture_internal_error": "the startup capture stopped on an internal error",
}

CAPTURE_DIAGNOSTICS_RE = re.compile(
    rb"^capture_diagnostics database_records=(\d+) required=(\d+) "
    rb"breakpoint_hits=(\d+) formatted_candidates=(\d+) salt_matches=(\d+) "
    rb"memory_regions=(\d+) memory_bytes=(\d+) memory_candidates=(\d+) "
    rb"memory_scope_matches=(\d+) memory_verified=(\d+)$"
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enroll or verify WeChat source keys")
    parser.add_argument("mode", choices=("enroll", "enroll-memory", "enroll-startup", "verify", "status"))
    parser.add_argument("--config", type=Path, default=CONFIG)
    return parser.parse_args()


def load_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("invalid_config", "cannot read WeChat configuration") from exc


def sip_state() -> str:
    return strict_sip_state()


def enrollment_path(config: dict) -> Path:
    return Path(config["external_root"]) / "wechat-chat" / "state" / "enrollment.json"


def pending_enrollment_path(config: dict) -> Path:
    return Path(config["external_root"]) / "wechat-chat" / "state" / "enrollment-pending.json"


def stop_wechat(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.25)
    raise WeChatSyncError("wechat_quit_failed", "WeChat did not exit cleanly; it was not force-closed")


def stop_child(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def wait_for_active_account(timeout_seconds: int = 60) -> tuple[int, Path]:
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            pid = get_wechat_pid()
            return pid, discover_active_account_root(pid)
        except WeChatSyncError as exc:
            if exc.code not in {"active_account_ambiguous", "active_account_missing"}:
                raise
            last_error = exc
            time.sleep(0.5)
    if last_error is not None:
        raise last_error
    raise WeChatSyncError("active_account_ambiguous", "cannot resolve one active WeChat account root")


def safe_capture_diagnostics(stderr: bytes) -> dict[str, int] | None:
    for raw_line in stderr.splitlines():
        match = CAPTURE_DIAGNOSTICS_RE.fullmatch(raw_line.strip())
        if match is not None:
            values = [int(value) for value in match.groups()]
            names = (
                "database_records", "required", "breakpoint_hits", "key_candidates",
                "key_scope_matches", "memory_regions", "memory_bytes", "memory_candidates",
                "memory_scope_matches", "memory_verified",
            )
            return dict(zip(names, values))
    return None


def safe_capture_error(stderr: bytes) -> tuple[str, str] | None:
    """Return only an allowlisted non-secret capture error from child stderr."""
    metrics = safe_capture_diagnostics(stderr)
    diagnostic = None
    if metrics is not None:
        diagnostic = (
            f"db={metrics['database_records']}/{metrics['required']}, "
            f"bp={metrics['breakpoint_hits']}, memory_regions={metrics['memory_regions']}, "
            f"memory_bytes={metrics['memory_bytes']}, "
            f"memory_candidates={metrics['memory_candidates']}, "
            f"memory_verified={metrics['memory_verified']}"
        )
    for raw_line in reversed(stderr.decode("ascii", errors="ignore").splitlines()):
        code = raw_line.strip()
        detail = CAPTURE_ERROR_DETAILS.get(code)
        if detail is not None:
            if diagnostic is not None:
                detail = f"{detail} ({diagnostic})"
            return code, detail
    return None


def load_pending_records(config: dict, account_fingerprint: str) -> dict[str, dict]:
    path = pending_enrollment_path(config)
    if not path.is_file():
        return {}
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("invalid_receipt", "pending enrollment metadata is invalid") from exc
    if pending.get("account_fingerprint") != account_fingerprint:
        raise WeChatSyncError("account_changed", "pending enrollment belongs to another account")
    return {
        str(item.get("relative_path")): item
        for item in pending.get("records", [])
        if isinstance(item, dict) and item.get("record_kind") in {"database", "image_aes"}
    }


def finalize_capture_metadata(
    config: dict,
    config_path: Path,
    account_fingerprint: str,
    receipt: dict,
    required_paths: set[str],
    metadata: dict,
    dry_metadata: dict,
    *,
    capture_method: str,
    wechat_relaunched: bool,
) -> dict:
    if metadata.get("account_fingerprint") != account_fingerprint:
        raise WeChatSyncError("account_changed", "captured keys do not belong to the active account")
    merged = load_pending_records(config, account_fingerprint)
    for item in metadata.get("records", []):
        if isinstance(item, dict):
            merged[str(item.get("relative_path"))] = item
    enrolled_database_paths = {
        relative
        for relative, item in merged.items()
        if item.get("record_kind") == "database"
    }
    missing = sorted(required_paths - enrolled_database_paths)
    records = [merged[key] for key in sorted(merged)]

    if missing:
        pending = {
            "schema_version": 1,
            "updated_at": now_iso(),
            "account_fingerprint": account_fingerprint,
            "wechat": receipt,
            "records": records,
            "required_database_count": len(required_paths),
            "captured_database_count": len(required_paths) - len(missing),
            "missing_database_count": len(missing),
        }
        atomic_write(
            pending_enrollment_path(config),
            json.dumps(pending, ensure_ascii=False, indent=2) + "\n",
        )
        return {
            "status": "enrollment_required",
            "partial_record_count": len(records),
            "remaining_database_count": len(missing),
            "wechat_relaunched": wechat_relaunched,
            "production_ready": False,
        }

    manifest = {
        "schema_version": 1,
        "enrolled_at": now_iso(),
        "verified_at": None,
        "production_ready": False,
        "sip_status_at_enrollment": "disabled",
        "filevault_status_at_enrollment": "enabled",
        "account_fingerprint": account_fingerprint,
        "wechat": receipt,
        "records": records,
        "image_aes_key_enrolled": any(item.get("record_kind") == "image_aes" for item in records),
        "image_key_pattern_count": int(dry_metadata.get("image_patterns", 0)),
        "image_aes_key_count": sum(item.get("record_kind") == "image_aes" for item in records),
        "capture_method": capture_method,
    }
    atomic_write(enrollment_path(config), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    config["wechat_chat"]["account_fingerprint"] = account_fingerprint
    atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    pending_enrollment_path(config).unlink(missing_ok=True)
    return {
        "status": "enrolled_not_active",
        "record_count": len(records),
        "database_record_count": len(required_paths),
        "image_aes_key_enrolled": manifest["image_aes_key_enrolled"],
        "wechat_relaunched": wechat_relaunched,
        "production_ready": False,
        "next_step": "verify captured database keys before enabling collection",
    }


def run_enroll_memory(config: dict, config_path: Path) -> dict:
    if sip_state() != "disabled":
        raise WeChatSyncError("sip_must_be_temporarily_disabled", "memory capture requires the current SIP-disabled enrollment window")
    require_filevault_enabled()
    chat = config["wechat_chat"]
    helper = Path(chat["keychain_helper"])
    capturer = ROOT / "bin" / "wechat_key_capture_lldb.py"
    scanner = Path(chat["key_scanner"])
    if not helper.is_file() or not capturer.is_file() or not scanner.is_file():
        raise WeChatSyncError("dependency_missing", "the audited memory capture tools are unavailable")

    _pid, account_root = wait_for_active_account()
    account_fingerprint = sha256_bytes(account_root.name.encode("utf-8"))
    receipt = app_receipt()
    required_paths = {
        str(path.relative_to(account_root / "db_storage"))
        for path in target_database_paths(account_root)
    }
    dry_run = subprocess.run(
        [str(scanner), "--dry-run-metadata"],
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    try:
        dry_metadata = json.loads(dry_run.stdout) if dry_run.returncode == 0 else {}
    except json.JSONDecodeError:
        dry_metadata = {}

    lldb_lookup = subprocess.run(
        ["/usr/bin/xcrun", "--find", "lldb"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    lldb_binary = Path(lldb_lookup.stdout.strip())
    if lldb_lookup.returncode != 0 or not lldb_binary.is_file():
        raise WeChatSyncError("developer_tools_unavailable", "Apple LLDB is unavailable")

    secret_read_fd, secret_write_fd = os.pipe()
    status_read_fd, status_write_fd = os.pipe()
    embedded_command = (
        "script import sys; "
        f"sys.path.insert(0,{json.dumps(str(capturer.parent))}); "
        "import wechat_key_capture_lldb as c; "
        f"c.embedded_memory_main({json.dumps(str(account_root))},{secret_write_fd},{status_write_fd})"
    )
    try:
        capture_process = subprocess.Popen(
            [str(lldb_binary), "-b", "-o", embedded_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(secret_write_fd, status_write_fd),
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        )
    except Exception:
        for descriptor in (secret_read_fd, secret_write_fd, status_read_fd, status_write_fd):
            os.close(descriptor)
        raise
    os.close(secret_write_fd)
    os.close(status_write_fd)
    status_stream = os.fdopen(status_read_fd, "rb", buffering=0)
    try:
        helper_process = subprocess.Popen(
            [str(helper), "enroll"],
            stdin=secret_read_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin"},
        )
    except Exception:
        os.close(secret_read_fd)
        status_stream.close()
        stop_child(capture_process)
        raise
    os.close(secret_read_fd)
    print("正在只读扫描微信当前进程中的数据库密钥缓存；不会重启微信。", file=sys.stderr, flush=True)
    try:
        helper_stdout, _helper_stderr = helper_process.communicate(timeout=300)
        capture_status = capture_process.wait(timeout=30)
    except subprocess.TimeoutExpired as exc:
        stop_child(capture_process)
        stop_child(helper_process)
        status_stream.close()
        raise WeChatSyncError("startup_capture_timeout", "bounded memory capture timed out and was stopped") from exc
    capture_stderr = status_stream.read()
    status_stream.close()
    if capture_status not in {0, 10}:
        safe_error = safe_capture_error(capture_stderr)
        if safe_error is not None:
            raise WeChatSyncError(*safe_error)
        raise WeChatSyncError("key_enrollment_failed", "bounded memory capture ended without a usable database key")
    if helper_process.returncode != 0:
        raise WeChatSyncError("keychain_enrollment_failed", "the local Keychain helper rejected the memory-capture stream")
    try:
        metadata = json.loads(helper_stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("key_enrollment_failed", "memory capture returned invalid metadata") from exc
    result = finalize_capture_metadata(
        config,
        config_path,
        account_fingerprint,
        receipt,
        required_paths,
        metadata,
        dry_metadata,
        capture_method="lldb_bounded_memory_hmac_verified",
        wechat_relaunched=False,
    )
    diagnostics = safe_capture_diagnostics(capture_stderr)
    if diagnostics is not None:
        result["capture_diagnostics"] = diagnostics
    return result


def run_enroll_startup(config: dict, config_path: Path) -> dict:
    if sip_state() != "disabled":
        raise WeChatSyncError("sip_must_be_temporarily_disabled", "startup capture requires the current SIP-disabled enrollment window")
    require_filevault_enabled()

    chat = config["wechat_chat"]
    helper = Path(chat["keychain_helper"])
    capturer = ROOT / "bin" / "wechat_key_capture_lldb.py"
    scanner = Path(chat["key_scanner"])
    if not helper.is_file() or not capturer.is_file() or not scanner.is_file():
        raise WeChatSyncError("dependency_missing", "the audited startup capture tools are unavailable")

    pid, account_root = wait_for_active_account()
    account_fingerprint = sha256_bytes(account_root.name.encode("utf-8"))
    receipt = app_receipt()
    required_paths = {
        str(path.relative_to(account_root / "db_storage"))
        for path in target_database_paths(account_root)
    }
    dry_run = subprocess.run(
        [str(scanner), "--dry-run-metadata"],
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    try:
        dry_metadata = json.loads(dry_run.stdout) if dry_run.returncode == 0 else {}
    except json.JSONDecodeError:
        dry_metadata = {}

    lldb_lookup = subprocess.run(
        ["/usr/bin/xcrun", "--find", "lldb"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    lldb_binary = Path(lldb_lookup.stdout.strip())
    if lldb_lookup.returncode != 0 or not lldb_binary.is_file():
        raise WeChatSyncError("developer_tools_unavailable", "Apple LLDB is unavailable")

    capture_timeout = 120
    secret_read_fd, secret_write_fd = os.pipe()
    status_read_fd, status_write_fd = os.pipe()
    embedded_command = (
        "script import sys; "
        f"sys.path.insert(0,{json.dumps(str(capturer.parent))}); "
        "import wechat_key_capture_lldb as c; "
        f"c.embedded_launch_main({json.dumps(str(account_root))},{capture_timeout},"
        f"{secret_write_fd},{status_write_fd})"
    )
    capture_command = [
        str(lldb_binary),
        "-b",
        "-o",
        embedded_command,
    ]
    print("无需管理员密码；微信会正常退出并自动重启一次。", file=sys.stderr, flush=True)
    stop_wechat(pid)
    try:
        capture_process = subprocess.Popen(
            capture_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(secret_write_fd, status_write_fd),
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": "/Users/example",
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                "LANG": "zh_CN.UTF-8",
            },
        )
    except Exception:
        for descriptor in (secret_read_fd, secret_write_fd, status_read_fd, status_write_fd):
            os.close(descriptor)
        subprocess.run(["/usr/bin/open", "-a", "WeChat"], check=False)
        raise
    os.close(secret_write_fd)
    os.close(status_write_fd)
    status_stream = os.fdopen(status_read_fd, "rb", buffering=0)
    try:
        helper_process = subprocess.Popen(
            [str(helper), "enroll"],
            stdin=secret_read_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin"},
        )
    except Exception:
        os.close(secret_read_fd)
        status_stream.close()
        stop_child(capture_process)
        subprocess.run(["/usr/bin/open", "-a", "WeChat"], check=False)
        raise
    os.close(secret_read_fd)

    ready = False
    status_prefix = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(status_stream, selectors.EVENT_READ)
    ready_deadline = time.monotonic() + 60
    while time.monotonic() < ready_deadline and capture_process.poll() is None:
        events = selector.select(timeout=0.5)
        if not events:
            continue
        line = status_stream.readline()
        status_prefix.extend(line)
        if line.strip() == b"capture_ready":
            ready = True
            break
    selector.close()
    if not ready:
        stop_child(capture_process)
        stop_child(helper_process)
        status_prefix.extend(status_stream.read())
        status_stream.close()
        subprocess.run(["/usr/bin/open", "-a", "WeChat"], check=False)
        safe_error = safe_capture_error(bytes(status_prefix))
        if safe_error is not None:
            raise WeChatSyncError(*safe_error)
        raise WeChatSyncError("startup_capture_failed", "the startup capture did not become ready")

    launch = subprocess.run(["/usr/bin/open", "-a", "WeChat"], check=False, timeout=30)
    if launch.returncode != 0:
        stop_child(capture_process)
        stop_child(helper_process)
        raise WeChatSyncError("wechat_launch_failed", "WeChat could not be relaunched")
    print("微信已由 macOS 正常重新打开；可进行只读会话触发。", file=sys.stderr, flush=True)

    try:
        helper_stdout, _helper_stderr = helper_process.communicate(timeout=capture_timeout + 120)
        capture_status = capture_process.wait(timeout=30)
    except subprocess.TimeoutExpired as exc:
        stop_child(capture_process)
        stop_child(helper_process)
        status_stream.close()
        subprocess.run(["/usr/bin/open", "-a", "WeChat"], check=False)
        raise WeChatSyncError("startup_capture_timeout", "startup capture timed out and was stopped") from exc
    capture_stderr = bytes(status_prefix) + status_stream.read()
    status_stream.close()
    if capture_status not in {0, 10}:
        subprocess.run(["/usr/bin/open", "-a", "WeChat"], check=False)
        safe_error = safe_capture_error(capture_stderr)
        if safe_error is not None:
            raise WeChatSyncError(*safe_error)
        raise WeChatSyncError("key_enrollment_failed", "startup capture ended without a usable database key")
    if helper_process.returncode != 0:
        raise WeChatSyncError("keychain_enrollment_failed", "the local Keychain enrollment helper rejected the capture stream")
    try:
        metadata = json.loads(helper_stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("key_enrollment_failed", "startup capture returned invalid metadata") from exc
    result = finalize_capture_metadata(
        config,
        config_path,
        account_fingerprint,
        receipt,
        required_paths,
        metadata,
        dry_metadata,
        capture_method="lldb_version_locked_prelaunch_breakpoint_and_bounded_memory_hmac_verified",
        wechat_relaunched=True,
    )
    diagnostics = safe_capture_diagnostics(capture_stderr)
    if diagnostics is not None:
        result["capture_diagnostics"] = diagnostics
    return result


def run_enroll(config: dict, config_path: Path) -> dict:
    if sip_state() != "disabled":
        raise WeChatSyncError("sip_must_be_temporarily_disabled", "enrollment only runs after the user disables SIP in Recovery")
    if not sys.stdin.isatty():
        raise WeChatSyncError("native_terminal_required", "run enrollment from a native interactive Terminal")
    require_filevault_enabled()
    chat = config["wechat_chat"]
    scanner = Path(chat["key_scanner"])
    helper = Path(chat["keychain_helper"])
    if not scanner.is_file() or not helper.is_file():
        raise WeChatSyncError("dependency_missing", "build the audited key tools first")
    receipt = app_receipt()
    dry_run = subprocess.run(
        [str(scanner), "--dry-run-metadata"],
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    try:
        dry_metadata = json.loads(dry_run.stdout) if dry_run.returncode == 0 else {}
    except json.JSONDecodeError as exc:
        raise WeChatSyncError("key_enrollment_failed", "key target discovery returned invalid metadata") from exc
    if dry_metadata.get("verdict") != "pass":
        raise WeChatSyncError("key_enrollment_failed", "key target discovery failed")
    scanner_process = subprocess.Popen(
        ["/usr/bin/sudo", str(scanner)],
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=None,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert scanner_process.stdout is not None
    helper_process = subprocess.Popen(
        [str(helper), "enroll"],
        stdin=scanner_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin"},
    )
    scanner_process.stdout.close()
    helper_stdout, _helper_stderr = helper_process.communicate(timeout=900)
    scanner_status = scanner_process.wait(timeout=30)
    if scanner_status != 0 or helper_process.returncode != 0:
        raise WeChatSyncError("key_enrollment_failed", "key enrollment failed without activating collection")
    try:
        metadata = json.loads(helper_stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("key_enrollment_failed", "key enrollment returned invalid metadata") from exc
    records = metadata.get("records")
    if not isinstance(records, list) or not records:
        raise WeChatSyncError("key_enrollment_failed", "no database keys were enrolled")
    required = {
        str(path.relative_to(discover_active_account_root(get_wechat_pid()) / "db_storage"))
        for path in target_database_paths(discover_active_account_root(get_wechat_pid()))
    }
    enrolled = {str(item.get("relative_path")) for item in records if isinstance(item, dict)}
    if not required.issubset(enrolled):
        raise WeChatSyncError("key_enrollment_incomplete", "not all active WeChat database keys were enrolled")
    manifest = {
        "schema_version": 1,
        "enrolled_at": now_iso(),
        "verified_at": None,
        "production_ready": False,
        "sip_status_at_enrollment": "disabled",
        "filevault_status_at_enrollment": "enabled",
        "account_fingerprint": metadata["account_fingerprint"],
        "wechat": receipt,
        "records": records,
        "image_aes_key_enrolled": any(item.get("record_kind") == "image_aes" for item in records),
        "image_key_pattern_count": int(dry_metadata.get("image_patterns", 0)),
        "image_aes_key_count": sum(item.get("record_kind") == "image_aes" for item in records),
    }
    path = enrollment_path(config)
    atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    config["wechat_chat"]["account_fingerprint"] = metadata["account_fingerprint"]
    atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return {
        "status": "enrolled_not_active",
        "record_count": len(records),
        "production_ready": False,
        "next_step": "re-enable SIP in Recovery, reboot, then run verify",
    }


def run_verify(config: dict) -> dict:
    if sip_state() != "enabled":
        raise WeChatSyncError("sip_not_enabled", "verification is blocked until SIP is re-enabled")
    require_filevault_enabled()
    path = enrollment_path(config)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("enrollment_required", "no enrollment receipt exists") from exc
    account_root = discover_active_account_root(get_wechat_pid())
    account_fingerprint = sha256_bytes(account_root.name.encode("utf-8"))
    configured_fingerprint = str(config.get("wechat_chat", {}).get("account_fingerprint", ""))
    if configured_fingerprint not in {"", "enrollment_required", account_fingerprint}:
        raise WeChatSyncError("account_changed", "configured account fingerprint differs from the active account")
    if manifest.get("account_fingerprint") != account_fingerprint:
        raise WeChatSyncError("account_changed", "active account does not match enrollment")
    receipt = app_receipt()
    if manifest.get("wechat") != receipt:
        raise WeChatSyncError("wechat_changed", "WeChat changed after enrollment")
    records = {str(item.get("relative_path")): item for item in manifest.get("records", []) if isinstance(item, dict)}
    external_root = Path(config["external_root"])
    work_root = external_root / "wechat-chat" / "work"
    verify_root = work_root / f"verify-{uuid.uuid4()}"
    ensure_private_dir(verify_root)
    helper = Path(config["wechat_chat"]["keychain_helper"])
    sqlcipher = Path(config["wechat_chat"]["sqlcipher_bin"])
    require_sqlcipher_version(sqlcipher, str(config["wechat_chat"]["sqlcipher_expected_version"]))
    verified = 0
    try:
        for source in target_database_paths(account_root):
            relative = str(source.relative_to(account_root / "db_storage"))
            record = records.get(relative)
            if not record:
                raise WeChatSyncError("key_rotation_required", "active database set changed after enrollment")
            key = keychain_fetch(helper, str(record.get("key_id", "")))
            destination = verify_root / relative
            encrypted_copy = verify_root / ".encrypted" / relative
            stable_readonly_copy(source, encrypted_copy)
            snapshot_encrypted_database(
                encrypted_copy, destination, key, str(record.get("salt_hex", "")), sqlcipher
            )
            verified += 1
    finally:
        if verify_root.parent.resolve() == work_root.resolve() and verify_root.name.startswith("verify-"):
            shutil.rmtree(verify_root)
    manifest["verified_at"] = now_iso()
    manifest["production_ready"] = True
    manifest["sip_status_after_enrollment"] = "enabled"
    manifest["verified_database_count"] = verified
    atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {"status": "verified", "production_ready": True, "verified_database_count": verified}


def run_status(config: dict) -> dict:
    path = enrollment_path(config)
    if not path.is_file():
        pending_path = pending_enrollment_path(config)
        if pending_path.is_file():
            try:
                pending = json.loads(pending_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {"status": "invalid_receipt", "sip": sip_state(), "production_ready": False}
            return {
                "status": "enrollment_required",
                "sip": sip_state(),
                "production_ready": False,
                "partial_record_count": len(pending.get("records", [])),
                "remaining_database_count": int(pending.get("missing_database_count", 0)),
            }
        return {"status": "enrollment_required", "sip": sip_state(), "production_ready": False}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "invalid_receipt", "sip": sip_state(), "production_ready": False}
    return {
        "status": "verified" if manifest.get("production_ready") else "enrolled_not_active",
        "sip": sip_state(),
        "production_ready": bool(manifest.get("production_ready")),
        "record_count": len(manifest.get("records", [])),
        "image_aes_key_enrolled": bool(manifest.get("image_aes_key_enrolled")),
    }


def main() -> int:
    args = arguments()
    try:
        config = load_config(args.config)
        if args.mode == "enroll":
            result = run_enroll(config, args.config)
        elif args.mode == "enroll-memory":
            result = run_enroll_memory(config, args.config)
        elif args.mode == "enroll-startup":
            result = run_enroll_startup(config, args.config)
        elif args.mode == "verify":
            result = run_verify(config)
        else:
            result = run_status(config)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except WeChatSyncError as exc:
        print(json.dumps({"status": "failed", "error_code": exc.code, "detail": exc.message}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "internal_error", "detail": "enrollment stopped safely"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
