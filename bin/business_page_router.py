#!/usr/bin/env python3
"""Route unresolved BUSINESS claims into their authoritative business pages.

The human-facing contract is deliberately simple:

* business content lives on the page where the topic belongs;
* unresolved content is a visibly labelled callout at the bottom of that page;
* ``90_待确认`` is generated from hidden, machine-readable markers and never
  stores the claim body;
* raw source material remains in the existing owner-only source stores.

This module has no model or network dependency.  Source-specific pipelines may
suggest a topic, but the trusted writer derives and validates the final path.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

import business_page_integrity as page_integrity

CLAIM_START_RE = page_integrity.CLAIM_START_RE
CLAIM_END_RE = re.compile(r"<!-- /business-claim:([A-Za-z0-9-]+) -->")
CLAIM_BASE_RE = page_integrity.CLAIM_BASE_RE
MEMORY_ID_RE = re.compile(r"^BUSINESS-[0-9]{8}-[0-9]{3}$")
SAFE_STATES = {"pending", "conflicting", "time_bounded"}
SAFE_RISKS = {"normal", "high"}
SAFE_FACT_STATUSES = {"owner_statement", "review_required"}
SAFE_LIFECYCLES = {"open", "resolved", "archived"}
SAFE_RESOLUTION_REASONS = {
    "confirmed",
    "refuted",
    "duplicate",
    "history_only",
    "expired",
    "superseded",
    "user_decided",
}
CLAIM_POLICY_VERSION = 1
CANONICAL_ROOTS = page_integrity.CANONICAL_ROOTS
DEFAULT_SOURCE_INDEX = "00_业务记忆库/99_规则与模板/外置资产说明与索引"
CONTEXT_PREFLIGHT_ENGINE = Path(
    os.environ.get(
        "BUSINESS_CONTEXT_PREFLIGHT_ENGINE",
        _archive_path('/Users/example/AI/shared-skills/business-customer-context-preflight/scripts/preflight_v2.py'),
    )
)


class RoutingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RoutedText(str):
    """String carrying the source hash used to build it.

    Transaction writers can compare ``expected_sha256`` immediately before
    replacement and fail closed when a person edited the page in between.
    """

    def __new__(
        cls,
        value: str,
        *,
        expected_sha256: Optional[str],
        claim_id: str,
        route_action: str,
    ) -> "RoutedText":
        instance = str.__new__(cls, value)
        instance.expected_sha256 = expected_sha256
        instance.claim_id = claim_id
        instance.route_action = route_action
        return instance


@dataclass(frozen=True)
class BusinessRoute:
    action: str
    target_page: str
    target_section: str
    evidence_state: str
    risk_level: str
    source_ref: str
    observed_at: str
    expected_sha256: Optional[str]
    claim_id: str
    dedupe_hash: str
    archive_ref: Optional[str]


def sha256_text(value: str) -> str:
    return page_integrity.sha256_text(value)


def sha256_file(path: Path) -> str:
    return page_integrity.sha256_file(path)


def opaque_claim_handle(claim_id: object) -> str:
    """Return the non-derivable handle used outside the governed page store."""
    return f"claim-{sha256_text(str(claim_id or ''))[:20]}"


def vault_root_from_config(config: dict[str, Any]) -> Path:
    memory_root = Path(str(config["memory_root"])).expanduser().resolve()
    if memory_root.name != "00_业务记忆库":
        raise RoutingError("memory_root", "configured memory root is not the BUSINESS business-memory root")
    return memory_root.parent


def canonical_roots(vault_root: Path) -> tuple[Path, ...]:
    return tuple((vault_root / name).resolve() for name in page_integrity.business_roots(vault_root))


def normalize_text(value: object, limit: int = 4000) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("\x00", " ").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def single_line(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", normalize_text(value, limit * 2)).strip()[:limit]


def safe_relative_page(relative: str, vault_root: Path | None = None) -> str:
    normalized = unicodedata.normalize("NFC", str(relative or "")).strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix.lower() != ".md"
        or path.parts[0] not in page_integrity.business_roots(vault_root)
    ):
        raise RoutingError("target_page", "business page target is outside the canonical allowlist")
    if any(part.startswith(".") for part in path.parts):
        raise RoutingError("target_page", "hidden business-page targets are not allowed")
    return path.as_posix()


def _contains(text: str, *keywords: str) -> bool:
    lowered = text.casefold()
    return any(keyword.casefold() in lowered for keyword in keywords)


def current_page_location(relative: str, vault_root: Path) -> str:
    """Locate an existing routed page after folder renames without creating duplicates."""
    literal = vault_root / relative
    if literal.exists():
        return relative
    parts = PurePosixPath(relative).parts
    matches = []
    for found in vault_root.rglob(parts[-1]):
        rel = found.relative_to(vault_root)
        if any(part.startswith(".") for part in rel.parts) or found.is_symlink() or not found.is_file():
            continue
        tail = rel.parts
        renamed = len(tail) == len(parts) and tail[1:] == parts[1:]
        nested = len(tail) == len(parts) + 1 and tail[1:] == parts
        if renamed or nested:
            matches.append(rel.as_posix())
    return matches[0] if len(matches) == 1 else relative


def infer_target_page(candidate: dict[str, Any]) -> str:
    """Use explicit targets or user-supplied rules; no embedded customer map."""
    supplied = candidate.get("target_page")
    if supplied:
        return safe_relative_page(str(supplied))
    rule_file = Path(os.environ["SYNC_CENTER_HOME"]) / "routing-rules.json"
    if not rule_file.is_file() or rule_file.is_symlink():
        raise RoutingError("routing_not_configured", "Configure routing-rules.json or supply target_page")
    rules = json.loads(rule_file.read_text(encoding="utf-8"))
    if not isinstance(rules, list):
        raise RoutingError("routing_invalid", "Routing rules must be a list")
    title = single_line(candidate.get("title"), 180)
    text = " ".join((title, single_line(candidate.get("summary") or candidate.get("memory_content"), 1000), single_line(candidate.get("source_ref"), 500)))
    for subject in (title, text):
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("keywords"), list):
                raise RoutingError("routing_invalid", "Invalid routing rule")
            words = rule["keywords"]
            if not words or not all(isinstance(w, str) and w.strip() for w in words):
                raise RoutingError("routing_invalid", "Each rule needs nonempty keywords")
            if _contains(subject, *words):
                return safe_relative_page(str(rule.get("target_page") or ""))
    raise RoutingError("routing_not_found", "No explicit route matches this item")



def classify_risk(candidate: dict[str, Any], state: str) -> str:
    supplied = str(candidate.get("risk_level") or "")
    if supplied in SAFE_RISKS:
        return supplied
    if state == "conflicting":
        return "high"
    text = " ".join(
        single_line(candidate.get(key), 1000)
        for key in ("title", "summary", "conflict_reason")
    )
    high_risk = (
        "价格", "报价", "交期", "截止", "认证", "证书", "dnv", "feoc", "pfe",
        "合规", "合同", "中标", "授权", "法定", "原产地", "产能", "立项", "质保",
        "回款", "付款", "汇率", "发货", "组织架构", "组织职责", "部门归属", "审批", "负责人",
        "商标", "客户承诺", "已发送", "已批准",
    )
    return "high" if _contains(text, *high_risk) else "normal"


IMPACT_LABELS = {
    "公司资料": "内部管理或对外公司口径",
    "客户及展会": "客户推进、项目判断或对外回复",
    "竞品研究": "市场判断与竞品分析",
    "市场与竞品研究": "市场判断与竞品分析",  # pre-rename name, still in older claim metadata
    "外部调研": "市场判断与竞品分析",
    "AI 赋能": "AI 建设方案与内部决策",
    "船舶业务网站与品牌架构": "品牌、网站或产品命名",
}
DEFAULT_IMPACT_LABEL = "业务资料与内部判断"


def impact_label(target_page: str) -> str:
    # Match the nearest known folder at any depth, not just the top, so a folder
    # nested elsewhere keeps its label (船舶业务网站与品牌架构 now sits under
    # 市场部). A folder with no label gets a neutral one instead of a KeyError.
    for part in PurePosixPath(target_page).parts[:-1]:
        if part in IMPACT_LABELS:
            return IMPACT_LABELS[part]
    return DEFAULT_IMPACT_LABEL


def _encode_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_payload(encoded: str) -> dict[str, Any]:
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RoutingError("claim_marker", "invalid business claim marker") from exc
    if not isinstance(value, dict):
        raise RoutingError("claim_marker", "business claim marker is not an object")
    return value


def claim_visible_fields(block: str) -> dict[str, str]:
    """Extract bounded human-facing fields without returning marker contents."""
    text = str(block or "")

    def field(label: str, limit: int) -> str:
        match = re.search(rf"^> - {re.escape(label)}[：:]\s*(.*?)\s*$", text, re.MULTILINE)
        return single_line(match.group(1), limit) if match else ""

    heading = re.search(r"^> \[![^\]]+\]\s+[^｜|]+[｜|](.*?)\s*$", text, re.MULTILINE)
    return {
        "title": single_line(heading.group(1), 180) if heading else "",
        "summary": field("说明", 1200),
        "evidence_boundary": field("证据边界", 900),
        "conflict_reason": field("冲突", 700),
        "fact_status": field("事实状态", 80),
        "source_attribution": field("内容归属", 300),
    }


def claim_dimension(title: object, summary: object = "") -> str:
    title_text = single_line(title, 240)
    text = " ".join((title_text, single_line(summary, 1000)))
    if _contains(title_text, "优先级待确认", "归属与优先级", "采用哪", "方案选择", "路线选择", "是否进入", "是否推进"):
        return "business_choice"
    if _contains(title_text, "请批准", "待批准", "请审批", "待审批", "待签字", "待签署", "是否授权"):
        return "authorization"
    if _contains(title_text, "技术签字", "技术签署", "待最终技术确认", "待终审"):
        return "technical_signoff"
    if _contains(text, "已批准", "已审批", "已授权", "授权表述", "批准状态", "审批状态"):
        return "approval_record"
    if _contains(text, "认证", "证书", "dnv", "feoc", "pfe", "合规"):
        return "certification"
    if _contains(text, "参数", "容量", "电芯", "pcs", "bms", "功率", "规格", "技术"):
        return "technical_specification"
    if _contains(text, "价格", "报价", "汇率", "折扣"):
        return "price"
    if _contains(text, "合同", "条款", "质保", "账期"):
        return "contract"
    if _contains(text, "已发送", "发送状态", "已发出"):
        return "sent"
    if _contains(text, "已收到", "已接收", "送达"):
        return "received"
    if _contains(text, "已完成", "完成状态", "验收完成"):
        return "completed"
    if _contains(text, "交期", "发货", "出货", "交付"):
        return "delivery"
    if _contains(text, "法定", "主体", "公司名称", "原产地", "商标"):
        return "legal_identity"
    if _contains(text, "状态", "进展", "阶段", "中标", "项目"):
        return "project_status"
    return "ordinary_fact"


def claim_kind_for_dimension(dimension: str) -> str:
    if dimension == "business_choice":
        return "human_decision"
    if dimension in {"authorization", "technical_signoff"}:
        return "human_authorization"
    if dimension in {"sent", "received", "completed"}:
        return "recorded_event"
    return "fact"


def governance_class_for_dimension(dimension: str) -> str:
    return {
        "certification": "certification",
        "technical_specification": "technical_specification",
        "technical_signoff": "technical_specification",
        "price": "price",
        "contract": "contract",
        "authorization": "approval_record",
        "approval_record": "approval_record",
        "sent": "sent",
        "received": "received",
        "completed": "completed",
    }.get(dimension, "ordinary")


def evidence_claim_v2(
    payload: dict[str, Any],
    *,
    relative_path: str = "",
    block: str = "",
) -> dict[str, Any]:
    """Normalize v1/v2 markers into the EvidenceClaimV2 compatibility view."""
    visible = claim_visible_fields(block)
    title = single_line(payload.get("title") or visible["title"], 180)
    summary = visible["summary"]
    dimension = str(payload.get("dimension_key") or claim_dimension(title, summary))
    source_system = single_line(payload.get("source_system"), 40) or "unknown"
    source_ref = single_line(payload.get("source_ref"), 500)
    source_fingerprint = str(payload.get("source_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", source_fingerprint):
        source_fingerprint = sha256_text(f"{source_system}|{source_ref}")[:32]
    lifecycle = str(payload.get("lifecycle") or "open")
    if lifecycle not in SAFE_LIFECYCLES:
        lifecycle = "open"
    resolution_reason = payload.get("resolution_reason")
    if resolution_reason not in SAFE_RESOLUTION_REASONS:
        resolution_reason = None
    subject_material = relative_path or str(payload.get("target_page") or title)
    subject_key = str(payload.get("subject_key") or f"subject-{sha256_text(subject_material)[:20]}")
    proposition_fingerprint = str(payload.get("proposition_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", proposition_fingerprint):
        proposition_fingerprint = sha256_text("|".join((title.casefold(), summary.casefold())))[:32]
    result = dict(payload)
    result.update(
        {
            "schema_version": 2,
            "claim_handle": opaque_claim_handle(payload.get("claim_id")),
            "subject_key": subject_key,
            "dimension_key": dimension,
            "claim_kind": str(payload.get("claim_kind") or claim_kind_for_dimension(dimension)),
            "governance_class": str(
                payload.get("governance_class") or governance_class_for_dimension(dimension)
            ),
            "link_status": str(payload.get("link_status") or "verified"),
            "lifecycle": lifecycle,
            "resolution_reason": resolution_reason,
            "source_fingerprint": source_fingerprint,
            "root_fingerprint": str(payload.get("root_fingerprint") or source_fingerprint),
            "proposition_fingerprint": proposition_fingerprint,
            "policy_version": int(payload.get("policy_version") or CLAIM_POLICY_VERSION),
            "record_version": int(payload.get("record_version") or 1),
            "evidence_watermark": str(
                payload.get("evidence_watermark")
                or sha256_text("|".join((source_fingerprint, proposition_fingerprint, str(payload.get("observed_at") or ""))))[:32]
            ),
            "fact_status": (
                str(payload.get("fact_status"))
                if str(payload.get("fact_status")) in SAFE_FACT_STATUSES
                else "review_required"
            ),
            "source_attribution": single_line(payload.get("source_attribution"), 300),
        }
    )
    if relative_path:
        result["target_page"] = relative_path
    result.update({key: value for key, value in visible.items() if value})
    return result


def _trailing_whitespace(text: str) -> str:
    try:
        return page_integrity.trailing_whitespace(text)
    except page_integrity.IntegrityError as exc:
        raise RoutingError(exc.code, exc.message) from exc


def _base_marker(payload: dict[str, Any]) -> str:
    return page_integrity.base_marker(payload)


def claim_base_state(text: str) -> tuple[dict[str, Any], tuple[int, int]] | None:
    try:
        return page_integrity.claim_base_state(text)
    except page_integrity.IntegrityError as exc:
        raise RoutingError(exc.code, exc.message) from exc


def business_base_text(text: str) -> str:
    try:
        return page_integrity.business_base_text(text)
    except page_integrity.IntegrityError as exc:
        raise RoutingError(exc.code, exc.message) from exc


def _ensure_claim_base(text: str) -> str:
    try:
        return page_integrity.ensure_claim_base(text)
    except page_integrity.IntegrityError as exc:
        raise RoutingError(exc.code, exc.message) from exc


def iter_claims(text: str) -> Iterable[tuple[dict[str, Any], tuple[int, int]]]:
    for start in CLAIM_START_RE.finditer(text):
        payload = _decode_payload(start.group("payload"))
        marker_version = int(start.group("version"))
        if int(payload.get("schema_version") or marker_version) != marker_version:
            raise RoutingError("claim_marker", "business claim marker version does not match its payload")
        claim_id = str(payload.get("claim_id") or "")
        end_pattern = re.compile(rf"<!-- /business-claim:{re.escape(claim_id)} -->")
        end = end_pattern.search(text, start.end())
        if not end:
            raise RoutingError("claim_marker", f"claim {claim_id or 'unknown'} is missing its end marker")
        yield payload, (start.start(), end.end())


def scan_claims(vault_root: Path) -> list[dict[str, Any]]:
    vault_root = Path(vault_root).resolve()
    claims: list[dict[str, Any]] = []
    for root in canonical_roots(vault_root):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for payload, span in iter_claims(text):
                relative = path.relative_to(vault_root).as_posix()
                block = text[span[0]:span[1]]
                item = evidence_claim_v2(payload, relative_path=relative, block=block)
                item["path"] = path
                item["relative_path"] = relative
                item["block"] = block
                claims.append(item)
    return claims


def next_claim_id(vault_root: Path, observed_at: str, extra_ids: Iterable[str] = ()) -> str:
    date_key = re.sub(r"[^0-9]", "", observed_at[:10])
    if len(date_key) != 8:
        date_key = datetime.now().astimezone().strftime("%Y%m%d")
    existing: list[str] = []
    memory_root = vault_root / "00_业务记忆库"
    for path in memory_root.glob("[0-9][0-9]_*/**/*.md"):
        if not path.is_file() or path.is_symlink():
            continue
        head = path.read_text(encoding="utf-8", errors="replace")[:1200]
        match = re.search(r"^memory_id:\s*['\"]?([^'\"\n]+)", head, re.MULTILINE)
        if match:
            existing.append(match.group(1).strip())
    existing.extend(str(claim.get("claim_id") or "") for claim in scan_claims(vault_root))
    existing.extend(str(value) for value in extra_ids)
    maximum = 0
    pattern = re.compile(rf"^BUSINESS-{date_key}-([0-9]{{3}})$")
    for value in existing:
        match = pattern.match(value)
        if match:
            maximum = max(maximum, int(match.group(1)))
    if maximum >= 999:
        raise RoutingError("claim_id", "daily BUSINESS claim id range is exhausted")
    return f"BUSINESS-{date_key}-{maximum + 1:03d}"


def find_claim(vault_root: Path, *, claim_id: str = "", dedupe_hash: str = "") -> Optional[dict[str, Any]]:
    matches = [
        item for item in scan_claims(vault_root)
        if (claim_id and item.get("claim_id") == claim_id)
        or (dedupe_hash and item.get("dedupe_hash") == dedupe_hash)
    ]
    if len(matches) > 1:
        raise RoutingError("claim_collision", "multiple business pages contain the same claim identity")
    return matches[0] if matches else None


def _source_link(archive_ref: Optional[str]) -> str:
    if archive_ref:
        normalized = str(archive_ref).replace("\\", "/").removesuffix(".md")
        return f"[[{normalized}|原始记录]]"
    return f"[[{DEFAULT_SOURCE_INDEX}|来源索引]]"


def render_claim_block(route: BusinessRoute, candidate: dict[str, Any]) -> str:
    title = single_line(candidate.get("title"), 120) or route.claim_id
    summary = single_line(candidate.get("summary") or candidate.get("memory_content"), 900)
    boundary = single_line(candidate.get("evidence_boundary"), 600)
    conflict_reason = single_line(candidate.get("conflict_reason"), 500)
    fact_status = str(candidate.get("fact_status") or "review_required")
    if fact_status not in SAFE_FACT_STATUSES:
        raise RoutingError("fact_status", "business claim fact status is invalid")
    source_attribution = single_line(candidate.get("source_attribution"), 300) or (
        f"受治理来源：{single_line(route.source_ref, 240)}"
    )
    if not summary or not boundary:
        raise RoutingError("claim_content", "business claim summary and evidence boundary are required")
    payload = evidence_claim_v2({
        "schema_version": 2,
        "claim_id": route.claim_id,
        "title": title,
        "state": route.evidence_state,
        "risk": route.risk_level,
        "source_system": single_line(candidate.get("source_system"), 40),
        "source_ref": route.source_ref,
        "observed_at": route.observed_at,
        "dedupe_hash": route.dedupe_hash,
        "target_page": route.target_page,
        "target_section": route.target_section,
        "archive_ref": route.archive_ref,
        "fact_status": fact_status,
        "source_attribution": source_attribution,
    }, relative_path=route.target_page)
    callout = "danger" if route.evidence_state == "conflicting" else "warning"
    label = "存在冲突" if route.evidence_state == "conflicting" else (
        "按日期有效" if route.evidence_state == "time_bounded" else "来源待核"
    )
    lines = [
        f"<!-- business-claim:v2 {_encode_payload(payload)} -->",
        f"> [!{callout}] {label}｜{title}",
        f"> - 说明：{summary}",
        f"> - 事实状态：{fact_status}",
        f"> - 内容归属：{source_attribution}",
        f"> - 证据边界：{boundary}",
    ]
    if conflict_reason:
        lines.append(f"> - 冲突：{conflict_reason}")
    lines.extend(
        [
            f"> - 影响：{impact_label(route.target_page)}",
            f"> - 观察日期：{route.observed_at[:10] or '未记录'}",
            f"> - 来源：{_source_link(route.archive_ref)}；`{single_line(route.source_ref, 300)}`",
            f"^business-claim-{route.claim_id.lower()}",
            f"<!-- /business-claim:{route.claim_id} -->",
        ]
    )
    return "\n".join(lines)


def upgrade_claim_markers(text: str, relative_path: str) -> RoutedText:
    """Upgrade marker payloads additively while preserving every visible byte."""
    # Preserve the complete pre-migration page hash in the claim-base history.
    # Existing immutable derivation receipts can then recognize this as an
    # annotation-only change instead of incorrectly treating governed source
    # prose as stale.
    prepared = _ensure_claim_base(text)
    matches = list(iter_claims(prepared))
    updated = prepared
    for payload, (block_start, block_end) in reversed(matches):
        block = prepared[block_start:block_end]
        start = CLAIM_START_RE.match(block)
        if not start:
            raise RoutingError("claim_marker", "business claim start marker is malformed")
        normalized = evidence_claim_v2(payload, relative_path=relative_path, block=block)
        normalized["record_version"] = max(int(normalized.get("record_version") or 1), 2)
        marker = f"<!-- business-claim:v2 {_encode_payload(normalized)} -->"
        absolute_end = block_start + start.end()
        updated = updated[:block_start] + marker + updated[absolute_end:]
    return RoutedText(
        updated,
        expected_sha256=sha256_text(text),
        claim_id="migration",
        route_action="upgrade_v2",
    )


def remove_claim_blocks(text: str, claim_ids: Iterable[str]) -> RoutedText:
    """Remove resolved callouts while preserving the governed page body.

    The caller must first persist a resolution receipt in the owner-only
    archive.  This function never decides that a claim is resolved; it only
    performs the bounded page edit after policy evaluation.
    """
    wanted = {str(value) for value in claim_ids}
    if not wanted:
        return RoutedText(
            text,
            expected_sha256=sha256_text(text),
            claim_id="none",
            route_action="no_change",
        )
    matches = [
        (payload, span)
        for payload, span in iter_claims(text)
        if str(payload.get("claim_id") or "") in wanted
    ]
    found = {str(payload.get("claim_id") or "") for payload, _span in matches}
    if found != wanted:
        missing = sorted(wanted - found)
        raise RoutingError("claim_changed", f"resolved claim is missing from its page: {missing[:3]}")
    updated = text
    for _payload, (start, end) in reversed(matches):
        left = start
        right = end
        while left > 0 and updated[left - 1] == "\n" and start - left < 1:
            left -= 1
        while right < len(updated) and updated[right] == "\n" and right - end < 2:
            right += 1
        updated = updated[:left] + "\n" + updated[right:]
    updated = re.sub(r"\n{4,}", "\n\n\n", updated)
    return RoutedText(
        updated,
        expected_sha256=sha256_text(text),
        claim_id=",".join(sorted(wanted)),
        route_action="archive_resolved",
    )


def _new_topic_page(relative: str) -> str:
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    return (
        "---\n"
        "document_type: business_business_topic\n"
        "applies_to:\n"
        "  - project:business-energy\n"
        f"created_at: {created}\n"
        "---\n\n"
        "本页用于归集该主题的业务信息；带核验标记的内容只作为线索，不作为已确认结论。\n"
    )


def _upsert_block(text: str, claim_id: str, block: str) -> str:
    matches = [item for item in iter_claims(text) if item[0].get("claim_id") == claim_id]
    if len(matches) > 1:
        raise RoutingError("claim_collision", f"page contains duplicate claim id {claim_id}")
    if matches:
        _, (start, end) = matches[0]
        return text[:start].rstrip() + "\n\n" + block + "\n" + text[end:].lstrip("\n")
    base = text.rstrip()
    heading = "## 核验说明"
    if not re.search(r"^## 核验说明\s*$", base, re.MULTILINE):
        base += f"\n\n{heading}"
    return base + "\n\n" + block + "\n"


def route_claim_change(
    config: dict[str, Any],
    candidate: dict[str, Any],
    *,
    source_system: str,
    source_ref: str,
    observed_at: str,
    claim_id: Optional[str] = None,
    archive_ref: Optional[str] = None,
    page_overrides: Optional[dict[Path, str]] = None,
) -> tuple[Path, RoutedText, BusinessRoute]:
    vault_root = vault_root_from_config(config)
    normalized_source = single_line(source_ref, 500)
    if not normalized_source:
        raise RoutingError("source_ref", "business claim source reference is required")
    state = str(candidate.get("evidence_state") or "")
    if not state:
        state = "conflicting" if str(candidate.get("action") or "") == "conflict" else "pending"
    if state not in SAFE_STATES:
        raise RoutingError("evidence_state", "business claim evidence state is invalid")
    dedupe_material = str(candidate.get("dedupe_key") or "").strip()
    if not dedupe_material:
        dedupe_material = "|".join((source_system, normalized_source, single_line(candidate.get("title"), 160)))
    dedupe_hash = sha256_text(dedupe_material)[:32]

    override_claims: list[dict[str, Any]] = []
    staged_ids: list[str] = []
    for override_path, override_text in (page_overrides or {}).items():
        try:
            relative_override = Path(override_path).resolve().relative_to(vault_root).as_posix()
        except ValueError:
            continue
        override_value = str(override_text)
        formal_match = re.search(r"^memory_id:\s*['\"]?([^'\"\n]+)", override_value[:1600], re.MULTILINE)
        if formal_match and MEMORY_ID_RE.match(formal_match.group(1).strip()):
            staged_ids.append(formal_match.group(1).strip())
        for payload, _span in iter_claims(override_value):
            override_claims.append({**payload, "relative_path": relative_override})
            staged_ids.append(str(payload.get("claim_id") or ""))
    override_matches = [
        item for item in override_claims
        if (claim_id and item.get("claim_id") == claim_id) or item.get("dedupe_hash") == dedupe_hash
    ]
    if len(override_matches) > 1:
        raise RoutingError("claim_collision", "multiple staged business claims have the same identity")
    existing = override_matches[0] if override_matches else find_claim(
        vault_root, claim_id=claim_id or "", dedupe_hash=dedupe_hash
    )
    if existing:
        claim_id = str(existing["claim_id"])
        relative = str(existing["relative_path"])
    else:
        relative = current_page_location(
            infer_target_page({**candidate, "source_ref": normalized_source}), vault_root)
        claim_id = claim_id or next_claim_id(
            vault_root,
            observed_at,
            staged_ids,
        )
    if not MEMORY_ID_RE.match(str(claim_id)):
        raise RoutingError("claim_id", "business claim id is invalid")
    relative = safe_relative_page(relative, vault_root)
    path = (vault_root / relative).resolve()
    if not any(path == root or root in path.parents for root in canonical_roots(vault_root)):
        raise RoutingError("target_page", "resolved business page target escaped the allowlist")
    if path.exists() and (not path.is_file() or path.is_symlink()):
        raise RoutingError("target_page", "business page target is not a regular file")

    override = (page_overrides or {}).get(path)
    if override is not None:
        current_text = str(override)
        expected_sha = getattr(override, "expected_sha256", None)
    elif path.exists():
        current_text = path.read_text(encoding="utf-8")
        expected_sha = sha256_file(path)
    else:
        current_text = _new_topic_page(relative)
        expected_sha = None
    route = BusinessRoute(
        action="update_page" if path.exists() or override is not None else "create_topic_page",
        target_page=relative,
        target_section="核验说明",
        evidence_state=state,
        risk_level=classify_risk(candidate, state),
        source_ref=normalized_source,
        observed_at=single_line(observed_at, 64),
        expected_sha256=expected_sha,
        claim_id=str(claim_id),
        dedupe_hash=dedupe_hash,
        archive_ref=archive_ref,
    )
    enriched = {**candidate, "source_system": source_system}
    current_text = _ensure_claim_base(current_text)
    updated = _upsert_block(current_text, route.claim_id, render_claim_block(route, enriched))
    return path, RoutedText(
        updated,
        expected_sha256=expected_sha,
        claim_id=route.claim_id,
        route_action=route.action,
    ), route


def verify_expected_hash(path: Path, content: str) -> None:
    expected = getattr(content, "expected_sha256", None)
    if expected is None:
        if path.exists():
            raise RoutingError("write_race", "new business page appeared after routing")
        return
    if not path.is_file() or path.is_symlink() or sha256_file(path) != expected:
        raise RoutingError("write_race", "business page changed after routing")


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def quarantine_candidate(config: dict[str, Any], candidate: dict[str, Any], code: str) -> Path:
    external_root = Path(str(config["external_root"])).expanduser().resolve()
    root = external_root / "business-routing" / "quarantine"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    fingerprint = sha256_text(json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str))[:12]
    path = root / f"{stamp}-{fingerprint}.json"
    payload = {
        "schema_version": 1,
        "status": "system_exception",
        "code": single_line(code, 80),
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidate": candidate,
    }
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
    return path


def pending_index_text(vault_root: Path, claims: Optional[list[dict[str, Any]]] = None, max_chars: int = 27000) -> str:
    rows = [
        item for item in (claims if claims is not None else scan_claims(vault_root))
        if str(item.get("lifecycle") or "open") == "open"
    ]
    rows.sort(
        key=lambda item: (
            0 if item.get("risk") == "high" else 1,
            0 if item.get("state") == "conflicting" else 1,
            str(item.get("observed_at") or ""),
            str(item.get("title") or ""),
        ),
        reverse=False,
    )
    stem_counts: dict[str, int] = {}
    for note_path in vault_root.rglob("*.md"):
        stem_counts[note_path.stem] = stem_counts.get(note_path.stem, 0) + 1

    def compact_ref(relative: str) -> str:
        normalized = str(relative).replace("\\", "/").removesuffix(".md")
        stem = PurePosixPath(normalized).name
        return stem if stem_counts.get(stem) == 1 else normalized

    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "---",
        "document_type: business_unresolved_claim_index",
        "version: 1.0",
        f"generated_at: {generated}",
        f"total_records: {len(rows)}",
        "applies_to:",
        "  - project:business-energy",
        "---",
        "",
        "本页由系统自动生成，只提供导航；未核信息正文和来源位于对应业务页面，不在这里处理。",
        "",
        f"当前共 {len(rows)} 条原位核验说明，其中高风险 {sum(1 for row in rows if row.get('risk') == 'high')} 条、冲突 {sum(1 for row in rows if row.get('state') == 'conflicting')} 条。",
        "",
        "| 风险 | 状态 | 事项与正式页面 | 观察日期 |",
        "|---|---|---|---|",
    ]
    for item in rows:
        title = str(item.get("title") or item.get("claim_id") or "未命名").replace("|", "／")
        relative = str(item["relative_path"])
        claim_id = str(item.get("claim_id") or "").lower()
        page_link = f"[[{relative}#^business-claim-{claim_id}|{title}]]"
        state = {"pending": "待核", "conflicting": "冲突", "time_bounded": "按日期有效"}.get(str(item.get("state")), "异常")
        risk = "高" if item.get("risk") == "high" else "普通"
        lines.append(f"| {risk} | {state} | {page_link} | {str(item.get('observed_at') or '')[:10]} |")
    lines.append("")
    if len("\n".join(lines)) > max_chars:
        # Keep explicit full paths for small indexes; uniquely named pages can
        # use Obsidian's unambiguous short form when the navigation grows.
        replacements = {str(row['relative_path']): compact_ref(str(row['relative_path'])) for row in rows}
        for index, line in enumerate(lines):
            for full, compact in replacements.items():
                line = line.replace('[[' + full + '#^', '[[' + compact + '#^')
            lines[index] = line
    if len("\n".join(lines)) > max_chars:
        # This is only a navigation view. Every claim and source stays on its
        # original business page; omit table rows before exceeding the budget.
        table_start = next(i for i, line in enumerate(lines) if line.startswith('|---')) + 1
        prefix = lines[:table_start]
        displayed = 0
        for line in lines[table_start:]:
            if not line.startswith('| '):
                continue
            if len('\n'.join(prefix)) + len(line) + 300 > max_chars:
                break
            prefix.append(line)
            displayed += 1
        prefix.extend(['', f'本导航按风险顺序展示 {displayed}／{len(rows)} 条；完整条目及来源仍保留在对应业务页面。', ''])
        lines = prefix
    return "\n".join(lines)


def _load_context_preflight_engine() -> Any:
    path = CONTEXT_PREFLIGHT_ENGINE.expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"shared V2 preflight engine unavailable: {path}")
    spec = importlib.util.spec_from_file_location(
        "business_customer_context_preflight_v2", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"shared V2 preflight engine cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_context_preflight_pages(vault_root: Path) -> list[str]:
    """Validate the V2 registry, every active page, and derived decision refs."""
    root = Path(vault_root).resolve()
    registry_path = (
        root / "客户及展会/10_客户与项目/00_活跃客户实体注册表.md"
    )
    try:
        engine = _load_context_preflight_engine()
        registry = engine.load_registry(registry_path)
        errors = engine.validate_registry(
            registry, vault_root=root, scan_pages=True
        )
    except Exception as exc:
        return [
            "customer context V2 gate unavailable: "
            f"{type(exc).__name__}: {exc}"
        ]
    return [f"customer context V2: {error}" for error in errors]

def validate_claims(vault_root: Path) -> list[str]:
    vault_root = Path(vault_root).resolve()
    errors: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_dedupe: dict[str, str] = {}
    try:
        claims = scan_claims(vault_root)
    except (OSError, UnicodeError, RoutingError) as exc:
        return [f"claim scan failed: {exc}"]
    for item in claims:
        relative = str(item.get("relative_path") or "")
        claim_id = str(item.get("claim_id") or "")
        dedupe = str(item.get("dedupe_hash") or "")
        if not MEMORY_ID_RE.match(claim_id):
            errors.append(f"{relative}: invalid claim id {claim_id or 'missing'}")
        if item.get("state") not in SAFE_STATES:
            errors.append(f"{relative}: invalid evidence state")
        if item.get("risk") not in SAFE_RISKS:
            errors.append(f"{relative}: invalid risk level")
        if item.get("lifecycle") not in SAFE_LIFECYCLES:
            errors.append(f"{relative}: invalid claim lifecycle")
        if item.get("resolution_reason") not in SAFE_RESOLUTION_REASONS | {None}:
            errors.append(f"{relative}: invalid claim resolution reason")
        if not re.fullmatch(r"subject-[0-9a-f]{20}", str(item.get("subject_key") or "")):
            errors.append(f"{relative}: invalid stable subject key")
        if not str(item.get("dimension_key") or "").strip():
            errors.append(f"{relative}: claim dimension is missing")
        if not re.fullmatch(r"[0-9a-f]{32}", str(item.get("source_fingerprint") or "")):
            errors.append(f"{relative}: invalid source fingerprint")
        if not str(item.get("source_ref") or "").strip():
            errors.append(f"{relative}: claim source_ref is missing")
        if str(item.get("target_page") or "") != relative:
            errors.append(f"{relative}: marker target_page does not match its page")
        block = str(item.get("block") or "")
        if "## 记忆内容" in block:
            errors.append(f"{relative}: standalone memory body leaked into an annotation")
        if "[[" not in block or "来源：" not in block:
            errors.append(f"{relative}: claim lacks a visible Obsidian source link")
        if claim_id in seen_ids:
            errors.append(f"{relative}: duplicate claim id also found in {seen_ids[claim_id]}")
        else:
            seen_ids[claim_id] = relative
        if dedupe:
            if dedupe in seen_dedupe:
                errors.append(f"{relative}: duplicate dedupe marker also found in {seen_dedupe[dedupe]}")
            else:
                seen_dedupe[dedupe] = relative
    integrity_pages = {Path(item["path"]) for item in claims}
    for root in canonical_roots(vault_root):
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                if page_integrity.CLAIM_BASE_SENTINEL in page_integrity.read_text_exact(path):
                    integrity_pages.add(path)
            except (OSError, UnicodeError):
                errors.append(f"{path.relative_to(vault_root).as_posix()}: business page is unreadable")
    for path in sorted(integrity_pages):
        relative = path.relative_to(vault_root).as_posix()
        try:
            inspected = page_integrity.inspect_page(page_integrity.read_text_exact(path))
        except (OSError, UnicodeError):
            errors.append(f"{relative}: business page is unreadable")
            continue
        for code in inspected.errors:
            errors.append(f"{relative}: {code}")
    pending_root = vault_root / "00_业务记忆库" / "90_待确认"
    if pending_root.is_dir():
        unexpected = [path.name for path in pending_root.glob("*.md") if path.name != "00_待确认总表.md"]
        if unexpected:
            errors.append(f"90_待确认 still contains {len(unexpected)} standalone business notes")
    errors.extend(validate_context_preflight_pages(vault_root))
    return errors


def route_to_dict(route: BusinessRoute) -> dict[str, Any]:
    return asdict(route)


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSINESS 业务页面原位核验路由")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config.json",
    )
    parser.add_argument("command", choices=("list", "write-index", "validate"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    vault_root = vault_root_from_config(config)
    if args.command == "list":
        claims = []
        for item in scan_claims(vault_root):
            claims.append({key: value for key, value in item.items() if key not in {"path", "block"}})
        print(json.dumps({"claims": claims, "count": len(claims)}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "write-index":
        claims = scan_claims(vault_root)
        target = vault_root / "00_业务记忆库" / "90_待确认" / "00_待确认总表.md"
        atomic_write(target, pending_index_text(vault_root, claims), 0o644)
        print(json.dumps({"status": "written", "claims": len(claims), "path": str(target)}, ensure_ascii=False))
        return 0
    errors = validate_claims(vault_root)
    print(json.dumps({"verdict": "pass" if not errors else "fail", "errors": errors}, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
