#!/usr/bin/env python3
"""Shared integrity contract for claim-bearing BUSINESS business pages.

The module deliberately separates three operations:

* :func:`inspect_page` is read-only and never repairs content;
* :func:`prepare_base_update` accepts a caller-authorized body replacement
  while requiring every governed claim block to remain byte-identical;
* the ``rebase`` command accepts an already-authorized body only inside the
  owned information-routing maintenance window and changes the hidden base
  marker, never the visible page.

No customer prose is written to receipts or command output.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = 1
VALIDATION_VERSION = "claim-base-v2"
CLAIM_START_RE = re.compile(
    r"<!-- business-claim:v(?P<version>[12]) (?P<payload>[A-Za-z0-9_-]+) -->"
)
CLAIM_BASE_RE = re.compile(r"<!-- business-claim-base:v1 ([A-Za-z0-9_-]+) -->")
CLAIM_BASE_SENTINEL = "<!-- business-claim-base:"
CLIENT_ALPHA_EXTERNAL_ASSETS_START = "<!-- CLIENT_ALPHA-EXTERNAL-ASSETS:START -->"
CLIENT_ALPHA_EXTERNAL_ASSETS_END = "<!-- CLIENT_ALPHA-EXTERNAL-ASSETS:END -->"
# Seed of top-level business folders. business_roots() adds every folder that
# currently holds a business page, so this list no longer has to track renames;
# it stays as the floor so a known folder keeps its standing while empty.
CANONICAL_ROOTS = (
    "公司资料",
    "客户及展会",
    "竞品研究",
    # Names in use before the 2020-01-02 reorganisation. Kept because claim
    # metadata written earlier still records target_page under them.
    "市场与竞品研究",
    "外部调研",
    "AI 赋能",
    "船舶业务网站与品牌架构",
)
# Never business folders, whatever they hold: standalone memories live here
# under their own rules (see dingtalk_memory_automation.safe_target).
EXCLUDED_ROOTS = frozenset({"00_业务记忆库"})
CLAIM_MARKERS = ("<!-- business-claim:", CLAIM_BASE_SENTINEL)
BRIDGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VAULT_ROOT = Path(
    _archive_path('/Users/example/Library/Mobile Documents/iCloud~md~obsidian/Documents/Example Vault/✦ 示例企业')
)
_ROOTS_TTL_SECONDS = 30.0
_roots_cache: dict[Path, tuple[float, tuple[str, ...]]] = {}


def business_roots(vault_root: Path | None = None) -> tuple[str, ...]:
    """Discover eligible roots from seed folders and claim-bearing pages."""
    try:
        root = (vault_root or DEFAULT_VAULT_ROOT).expanduser().resolve(strict=True)
    except OSError:
        return CANONICAL_ROOTS
    now = time.monotonic()
    cached = _roots_cache.get(root)
    if cached and now - cached[0] < _ROOTS_TTL_SECONDS:
        return cached[1]
    found = set(CANONICAL_ROOTS)
    try:
        tops = sorted(root.iterdir())
    except OSError:
        tops = []
    for top in tops:
        if (top.name in found or top.name in EXCLUDED_ROOTS or top.name.startswith(".")
                or top.is_symlink() or not top.is_dir()):
            continue
        for page in top.rglob("*.md"):
            if page.is_symlink() or any(part.startswith(".") for part in page.relative_to(top).parts):
                continue
            try:
                text = page.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(marker in text for marker in CLAIM_MARKERS):
                found.add(top.name)
                break
    result = tuple(sorted(found))
    _roots_cache[root] = (now, result)
    return result


class IntegrityError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IntegrityResult:
    schema_version: int
    validation_version: str
    marker_present: bool
    claim_count: int
    stored_base_sha256: str | None
    actual_base_sha256: str | None
    valid: bool
    errors: tuple[str, ...]
    created_heading: bool | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["errors"] = list(self.errors)
        return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_text_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def encode_payload(payload: dict[str, Any]) -> str:
    raw = canonical_json(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_payload(encoded: str, *, code: str = "claim_base_marker_malformed") -> dict[str, Any]:
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(code, "business page marker payload is invalid") from exc
    if not isinstance(value, dict):
        raise IntegrityError(code, "business page marker payload is not an object")
    return value


def trailing_whitespace(text: str) -> str:
    match = re.search(r"\s*\Z", text)
    trailing = match.group(0) if match else ""
    if len(trailing.encode("utf-8")) > 4096:
        raise IntegrityError(
            "claim_base_trailing_invalid",
            "business page has excessive trailing whitespace",
        )
    return trailing


def base_marker(payload: dict[str, Any]) -> str:
    return f"<!-- business-claim-base:v1 {encode_payload(payload)} -->"


def claim_base_state(text: str) -> tuple[dict[str, Any], tuple[int, int]] | None:
    matches = list(CLAIM_BASE_RE.finditer(text))
    if not matches:
        if CLAIM_BASE_SENTINEL in text:
            raise IntegrityError(
                "claim_base_marker_malformed",
                "business page claim-base marker is malformed",
            )
        return None
    if len(matches) != 1:
        raise IntegrityError(
            "claim_base_marker_multiple",
            "business page contains multiple claim-base markers",
        )
    match = matches[0]
    payload = decode_payload(match.group(1))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise IntegrityError(
            "claim_base_schema_invalid",
            "business page claim-base schema is invalid",
        )
    base_sha = str(payload.get("base_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", base_sha):
        raise IntegrityError(
            "claim_base_hash_invalid",
            "business page claim-base hash is invalid",
        )
    trailing = payload.get("base_trailing")
    if not isinstance(trailing, str) or len(trailing.encode("utf-8")) > 4096:
        raise IntegrityError(
            "claim_base_trailing_invalid",
            "business page claim-base trailing state is invalid",
        )
    if type(payload.get("created_heading")) is not bool:
        raise IntegrityError(
            "claim_base_heading_state_invalid",
            "business page claim-base heading state is invalid",
        )
    history = payload.get("annotation_hashes")
    if not isinstance(history, list) or any(
        not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in history
    ):
        raise IntegrityError(
            "claim_base_history_invalid",
            "business page claim-base history is invalid",
        )
    return payload, (match.start(), match.end())


def claim_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for start in CLAIM_START_RE.finditer(text):
        payload = decode_payload(start.group("payload"), code="claim_marker_malformed")
        marker_version = int(start.group("version"))
        if int(payload.get("schema_version") or marker_version) != marker_version:
            raise IntegrityError(
                "claim_marker_schema_invalid",
                "business claim marker version does not match its payload",
            )
        claim_id = str(payload.get("claim_id") or "")
        if not claim_id:
            raise IntegrityError("claim_marker_id_missing", "business claim marker has no id")
        end = re.compile(rf"<!-- /business-claim:{re.escape(claim_id)} -->").search(
            text, start.end()
        )
        if not end:
            raise IntegrityError(
                "claim_marker_end_missing",
                "business claim marker has no matching end marker",
            )
        spans.append((start.start(), end.end()))
    return spans


def claim_blocks(text: str) -> tuple[str, ...]:
    return tuple(text[start:end] for start, end in claim_spans(text))


def _created_heading_suffix_without_claims(
    text: str,
    marker_span: tuple[int, int],
) -> str:
    """Return only the generated post-marker area and reject marker overlap."""
    marker_start, marker_end = marker_span
    suffix = text[marker_end:]
    for start, end in reversed(claim_spans(text)):
        if start >= marker_end:
            relative_start = start - marker_end
            relative_end = end - marker_end
            suffix = suffix[:relative_start] + suffix[relative_end:]
        elif end > marker_start:
            raise IntegrityError(
                "claim_base_marker_overlap",
                "business claim annotation overlaps the claim-base marker",
            )
    return suffix


def _validate_created_heading_suffix(text: str, marker_span: tuple[int, int]) -> None:
    """Allow only generated annotations after a base that predates its heading.

    Older migrated pages intentionally keep the generated ``核验说明`` heading
    and governed claim blocks outside the preserved business body.  Five legacy
    Client Alpha pages also have one explicitly delimited, machine-managed external
    asset index.  Everything else is visible business text and must fail closed
    instead of silently falling outside the stored base hash.
    """
    suffix = _created_heading_suffix_without_claims(text, marker_span)
    heading = re.match(r"\A\r?\n## 核验说明[ \t]*(?:\r?\n|\Z)", suffix)
    if heading is None:
        raise IntegrityError(
            "claim_base_untracked_suffix",
            "business page contains visible text outside the governed base",
        )
    remainder = suffix[heading.end() :]
    if re.fullmatch(r"[ \t\r\n]*", remainder):
        return
    if (
        remainder.count(CLIENT_ALPHA_EXTERNAL_ASSETS_START) != 1
        or remainder.count(CLIENT_ALPHA_EXTERNAL_ASSETS_END) != 1
    ):
        raise IntegrityError(
            "claim_base_untracked_suffix",
            "business page contains an unrecognized post-base annotation region",
        )
    start = remainder.find(CLIENT_ALPHA_EXTERNAL_ASSETS_START)
    end = remainder.find(CLIENT_ALPHA_EXTERNAL_ASSETS_END, start + len(CLIENT_ALPHA_EXTERNAL_ASSETS_START))
    if end < 0:
        raise IntegrityError(
            "claim_base_untracked_suffix",
            "business page external-asset annotation is incomplete",
        )
    before = remainder[:start]
    after = remainder[end + len(CLIENT_ALPHA_EXTERNAL_ASSETS_END) :]
    if not re.fullmatch(r"[ \t\r\n]*", before) or not re.fullmatch(
        r"[ \t\r\n]*", after
    ):
        raise IntegrityError(
            "claim_base_untracked_suffix",
            "business page contains visible text outside managed annotation regions",
        )


def business_base_text(text: str) -> str:
    state = claim_base_state(text)
    if state is None:
        raise IntegrityError(
            "claim_base_marker_missing",
            "business page with governed claims lacks a claim-base marker",
        )
    payload, (marker_start, marker_end) = state
    trailing = str(payload["base_trailing"])
    if bool(payload["created_heading"]):
        _validate_created_heading_suffix(text, (marker_start, marker_end))
        return text[:marker_start].rstrip() + trailing
    cleaned = text
    for start, end in reversed(claim_spans(text)):
        cleaned = cleaned[:start] + cleaned[end:]
    state_after = claim_base_state(cleaned)
    if state_after is None:
        raise IntegrityError(
            "claim_base_marker_missing",
            "business page claim-base marker disappeared",
        )
    _, (start, end) = state_after
    suffix = cleaned[end:]
    if suffix.startswith("\r\n"):
        suffix = suffix[2:]
    elif suffix.startswith("\n"):
        suffix = suffix[1:]
    return (cleaned[:start] + suffix).rstrip() + trailing


def without_base_marker(text: str) -> str:
    state = claim_base_state(text)
    if state is None:
        return text
    _, (start, end) = state
    return text[:start] + text[end:]


def inspect_page(text: str) -> IntegrityResult:
    stored: str | None = None
    actual: str | None = None
    created: bool | None = None
    marker_present = CLAIM_BASE_SENTINEL in text
    try:
        spans = claim_spans(text)
        state = claim_base_state(text)
        marker_present = state is not None
        if state is None:
            errors = ("claim_base_marker_missing",) if spans else ()
            return IntegrityResult(
                SCHEMA_VERSION,
                VALIDATION_VERSION,
                False,
                len(spans),
                None,
                None,
                not errors,
                errors,
                None,
            )
        payload, _ = state
        stored = str(payload["base_sha256"])
        created = bool(payload["created_heading"])
        actual = sha256_text(business_base_text(text))
        errors = () if stored == actual else ("claim_base_mismatch",)
        return IntegrityResult(
            SCHEMA_VERSION,
            VALIDATION_VERSION,
            True,
            len(spans),
            stored,
            actual,
            not errors,
            errors,
            created,
        )
    except IntegrityError as exc:
        return IntegrityResult(
            SCHEMA_VERSION,
            VALIDATION_VERSION,
            marker_present,
            len(CLAIM_START_RE.findall(text)),
            stored,
            actual,
            False,
            (exc.code,),
            created,
        )


def require_valid(text: str) -> IntegrityResult:
    result = inspect_page(text)
    if not result.valid:
        raise IntegrityError(result.errors[0], "business page integrity check failed")
    return result


def ensure_claim_base(text: str) -> str:
    """Create the first marker, or advance annotation history on a valid page.

    An existing mismatch is never repaired here.  Body changes must use
    :func:`prepare_base_update` or the explicit maintenance-only rebase path.
    """
    state = claim_base_state(text)
    if state is None:
        if claim_spans(text):
            raise IntegrityError(
                "claim_base_marker_missing",
                "claim-bearing page lacks a claim-base marker",
            )
        heading = re.search(r"^## 核验说明\s*$", text, re.MULTILINE)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "base_sha256": sha256_text(text),
            "base_trailing": trailing_whitespace(text),
            "created_heading": heading is None,
            "annotation_hashes": [],
        }
        marker = base_marker(payload)
        if heading:
            return text[: heading.start()] + marker + "\n" + text[heading.start() :]
        return text.rstrip() + "\n\n" + marker + "\n## 核验说明\n"

    result = require_valid(text)
    payload, span = state
    history = [str(value) for value in payload.get("annotation_hashes", [])]
    current_sha = sha256_text(text)
    if current_sha not in history:
        history.append(current_sha)
    payload = dict(payload)
    payload["annotation_hashes"] = history[-64:]
    updated = text[: span[0]] + base_marker(payload) + text[span[1] :]
    require_valid(updated)
    if result.actual_base_sha256 != inspect_page(updated).actual_base_sha256:
        raise IntegrityError("claim_base_internal_error", "annotation history changed page base")
    return updated


def prepare_base_update(
    old_text: str,
    candidate_text: str,
    *,
    allow_base_change: bool,
) -> str:
    """Prepare an authorized page-body replacement without changing claims."""
    old_state = claim_base_state(old_text)
    if old_state is None:
        old_result = inspect_page(old_text)
        candidate_result = inspect_page(candidate_text)
        if not old_result.valid or not candidate_result.valid:
            raise IntegrityError(
                "claim_base_integrity_failed",
                "unguarded page unexpectedly contains governed markers",
            )
        return candidate_text

    require_valid(old_text)
    candidate_state = claim_base_state(candidate_text)
    if candidate_state is None:
        raise IntegrityError(
            "claim_base_marker_missing",
            "authorized candidate omitted the existing claim-base marker",
        )
    old_payload, old_span = old_state
    candidate_payload, candidate_span = candidate_state
    if old_text[old_span[0] : old_span[1]] != candidate_text[
        candidate_span[0] : candidate_span[1]
    ] or candidate_payload != old_payload:
        raise IntegrityError(
            "claim_base_marker_tampered",
            "authorized candidate changed the hidden claim-base marker",
        )
    if claim_blocks(old_text) != claim_blocks(candidate_text):
        raise IntegrityError(
            "claim_annotation_tampered",
            "authorized candidate changed governed claim annotations",
        )

    old_base = business_base_text(old_text)
    candidate_base = business_base_text(candidate_text)
    base_changed = old_base != candidate_base
    if base_changed and not allow_base_change:
        raise IntegrityError(
            "claim_base_change_not_authorized",
            "business page body change was not authorized",
        )

    payload = dict(old_payload)
    if base_changed:
        payload["base_sha256"] = sha256_text(candidate_base)
        payload["base_trailing"] = trailing_whitespace(candidate_base)
        payload["annotation_hashes"] = [sha256_text(old_text)]
    else:
        history = [str(value) for value in payload.get("annotation_hashes", [])]
        old_sha = sha256_text(old_text)
        if old_sha not in history:
            history.append(old_sha)
        payload["annotation_hashes"] = history[-64:]
    prepared = (
        candidate_text[: candidate_span[0]]
        + base_marker(payload)
        + candidate_text[candidate_span[1] :]
    )
    require_valid(prepared)
    if claim_blocks(old_text) != claim_blocks(prepared):
        raise IntegrityError(
            "claim_annotation_tampered",
            "prepared update changed governed claim annotations",
        )
    return prepared


def rebase_text(text: str, *, expected_old_base_sha256: str) -> str:
    result = inspect_page(text)
    if result.errors != ("claim_base_mismatch",):
        raise IntegrityError(
            "claim_base_rebase_not_applicable",
            "rebase requires exactly one existing claim-base mismatch",
        )
    if result.stored_base_sha256 != expected_old_base_sha256:
        raise IntegrityError(
            "claim_base_expected_hash_mismatch",
            "stored claim-base hash changed before rebase",
        )
    state = claim_base_state(text)
    assert state is not None and result.actual_base_sha256 is not None
    payload, span = state
    payload = dict(payload)
    payload["base_sha256"] = result.actual_base_sha256
    payload["base_trailing"] = trailing_whitespace(business_base_text(text))
    payload["annotation_hashes"] = [sha256_text(text)]
    rebased = text[: span[0]] + base_marker(payload) + text[span[1] :]
    if without_base_marker(rebased) != without_base_marker(text):
        raise IntegrityError(
            "claim_base_visible_content_changed",
            "rebase changed content outside the hidden marker",
        )
    require_valid(rebased)
    return rebased


def _safe_page(vault_root: Path, relative: str) -> tuple[str, Path]:
    raw = str(relative or "").strip().replace("\\", "/")
    posix = PurePosixPath(raw)
    if (
        not raw
        or posix.is_absolute()
        or ".." in posix.parts
        or posix.suffix.lower() != ".md"
        or not posix.parts
        or posix.parts[0] not in business_roots(vault_root)
    ):
        raise IntegrityError("page_path_invalid", "business page path is outside canonical roots")
    root = vault_root.expanduser().resolve(strict=True)
    path = root.joinpath(*posix.parts)
    if path.is_symlink() or not path.is_file():
        raise IntegrityError("page_unavailable", "business page is unavailable or unsafe")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise IntegrityError("page_path_invalid", "business page escaped vault root") from exc
    return posix.as_posix(), resolved


def _require_owned_maintenance() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(BRIDGE_ROOT / "bin/maintenance_mode.py"), "status"],
        cwd=BRIDGE_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    status_payload = json.loads(completed.stdout)
    reason = str(status_payload.get("reason") or "")
    if status_payload.get("status") != "active" or not reason.startswith("info-routing-"):
        raise IntegrityError(
            "maintenance_required",
            "claim-base rebase requires the owned information-routing maintenance window",
        )
    if status_payload.get("writer_processes") or status_payload.get("unregistered_writer_processes"):
        raise IntegrityError("writer_active", "a writer is active during claim-base rebase")
    if any(int(value) for value in dict(status_payload.get("queues") or {}).values()):
        raise IntegrityError("queue_not_empty", "an ingestion queue is not empty")
    return status_payload


def _atomic_write(path: Path, text: str, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)


def rebase_page(
    *,
    vault_root: Path,
    relative_page: str,
    expected_file_sha256: str,
    expected_old_base_sha256: str,
    receipt_root: Path,
) -> dict[str, Any]:
    _require_owned_maintenance()
    relative, path = _safe_page(vault_root, relative_page)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_file_sha256):
        raise IntegrityError("expected_file_hash_invalid", "expected file hash is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_old_base_sha256):
        raise IntegrityError("expected_base_hash_invalid", "expected base hash is invalid")
    before_sha = sha256_file(path)
    if before_sha != expected_file_sha256:
        raise IntegrityError("page_concurrent_drift", "business page changed before rebase")
    before = read_text_exact(path)
    before_result = inspect_page(before)
    rebased = rebase_text(before, expected_old_base_sha256=expected_old_base_sha256)
    after_sha = sha256_text(rebased)
    mode = stat.S_IMODE(path.stat().st_mode)

    root = receipt_root.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    run_root = root / f"{stamp}-{uuid.uuid4().hex[:8]}"
    backup_path = run_root / "backup" / Path(*PurePosixPath(relative).parts)
    backup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(path, backup_path)
    if sha256_file(backup_path) != before_sha:
        raise IntegrityError("backup_verification_failed", "business page backup is not identical")

    if sha256_file(path) != before_sha:
        raise IntegrityError("page_concurrent_drift", "business page changed during backup")
    _atomic_write(path, rebased, mode)
    if sha256_file(path) != after_sha:
        raise IntegrityError("rebase_verification_failed", "rebased page hash is not stable")
    after_result = inspect_page(read_text_exact(path))
    if not after_result.valid:
        raise IntegrityError("rebase_verification_failed", "rebased page is not valid")

    receipt = {
        "schema_version": 1,
        "operation": "claim_base_rebase",
        "validation_version": VALIDATION_VERSION,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "relative_path": relative,
        "before_file_sha256": before_sha,
        "after_file_sha256": after_sha,
        "stored_base_sha256_before": before_result.stored_base_sha256,
        "actual_base_sha256_before": before_result.actual_base_sha256,
        "base_sha256_after": after_result.stored_base_sha256,
        "visible_content_equivalent": True,
        "backup_sha256": sha256_file(backup_path),
    }
    receipt_path = run_root / "receipt.json"
    _atomic_json(receipt_path, receipt)
    return {
        "status": "rebased",
        "relative_path": relative,
        "before_file_sha256": before_sha,
        "after_file_sha256": after_sha,
        "visible_content_equivalent": True,
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_file(receipt_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BUSINESS business-page integrity")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT)
    inspect.add_argument("--page", required=True)
    rebase = commands.add_parser("rebase")
    rebase.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT)
    rebase.add_argument("--page", required=True)
    rebase.add_argument("--expected-file-sha256", required=True)
    rebase.add_argument("--expected-old-base-sha256", required=True)
    rebase.add_argument("--receipt-root", required=True, type=Path)
    rebase.add_argument("--approve-base-change", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "inspect":
            relative, path = _safe_page(args.vault_root, args.page)
            result = inspect_page(read_text_exact(path))
            payload = {"relative_path": relative, **result.to_dict()}
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if result.valid else 1
        if not args.approve_base_change:
            raise IntegrityError(
                "base_change_approval_missing",
                "explicit --approve-base-change is required",
            )
        result = rebase_page(
            vault_root=args.vault_root,
            relative_page=args.page,
            expected_file_sha256=args.expected_file_sha256,
            expected_old_base_sha256=args.expected_old_base_sha256,
            receipt_root=args.receipt_root,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (IntegrityError, OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        code = getattr(exc, "code", "claim_base_operation_failed")
        print(
            json.dumps(
                {"status": "failed", "error_code": str(code)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
