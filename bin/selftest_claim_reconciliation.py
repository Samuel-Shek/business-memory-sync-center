#!/usr/bin/env python3
"""Offline policy and lifecycle checks for EvidenceClaimV2 / ReviewCase."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import sqlite3
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import business_page_router as router
import claim_reconciliation as reconciliation
import dingtalk_sync_v2 as syncv2


def check(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def expect_error(code: str, call, *args, **kwargs) -> None:
    try:
        call(*args, **kwargs)
    except reconciliation.ReconciliationError as exc:
        check(exc.code == code, f"expected {code}, got {exc.code}")
    else:
        raise AssertionError(f"expected {code}")


def add_claim(
    config: dict,
    claim_id: str,
    title: str,
    source_ref: str,
    *,
    summary: str,
    dedupe_key: str,
    category: str = "external_communication",
) -> Path:
    path, text, _route = router.route_claim_change(
        config,
        {
            "action": "annotate_page",
            "title": title,
            "summary": summary,
            "evidence_boundary": "合成测试证据；不能据此升级真实业务事实。",
            "category": category,
            "dedupe_key": dedupe_key,
        },
        source_system="dingtalk",
        source_ref=source_ref,
        observed_at="2020-01-02T08:00:00+08:00",
        claim_id=claim_id,
    )
    router.verify_expected_hash(path, text)
    router.atomic_write(path, str(text))
    return path


def relation(handle: str, kind: str, token: str, reason: str = "合成关系测试。") -> dict:
    return {
        "claim_handle": handle,
        "relation": kind,
        "source_tokens": [token],
        "reason": reason,
    }


def descriptor(
    material: str,
    root: str,
    independence: str,
    *,
    authority: str = "ordinary",
    verification: str = "deterministic",
) -> dict:
    return {
        "material": material,
        "root_fingerprint": root,
        "provenance_family": f"synthetic:{independence}",
        "independence_group": independence,
        "directness": "direct",
        "authority_class": authority,
        "verification_method": verification,
    }


def claim_row(claim_id: str) -> dict:
    connection = reconciliation.connect(readonly=True)
    try:
        row = connection.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        check(row is not None, f"missing claim {claim_id}")
        return dict(row)
    finally:
        connection.close()


def main() -> int:
    sync_original = {name: getattr(syncv2, name) for name in ("DATABASE", "STATE_DIR", "RUNS_DIR", "ARCHIVE_DIR", "AI_LOCK", "DECISION_LOCK")}
    original = {
        "DATABASE": reconciliation.DATABASE,
        "RESOLUTION_ARCHIVE": reconciliation.RESOLUTION_ARCHIVE,
        "RELATION_OUTBOX": reconciliation.RELATION_OUTBOX,
        "MAINTENANCE_STATE": reconciliation.MAINTENANCE_STATE,
    }
    checks = 0
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "✦ 示例企业"
            memory = vault / "00_业务记忆库"
            external = root / "external"
            memory.mkdir(parents=True)
            external.mkdir()
            config = {"memory_root": str(memory), "external_root": str(external)}
            reconciliation.DATABASE = root / "state" / "claim-reconciliation.sqlite"
            reconciliation.RESOLUTION_ARCHIVE = root / "state" / "claim-resolution-archive.jsonl"
            reconciliation.RELATION_OUTBOX = root / "state" / "reconciliation-outbox"
            reconciliation.MAINTENANCE_STATE = root / "state" / "maintenance-mode.json"
            syncv2.STATE_DIR = root / "sync-state"
            syncv2.DATABASE = syncv2.STATE_DIR / "sync.sqlite"
            syncv2.RUNS_DIR = root / "sync-runs"
            syncv2.ARCHIVE_DIR = root / "sync-archive"
            syncv2.AI_LOCK = root / "ai.lock"
            syncv2.DECISION_LOCK = root / "decision.lock"
            syncv2.initialize()

            ordinary_page = add_claim(
                config,
                "BUSINESS-20260831-001",
                "开发信客户画像核验",
                "dingtalk:synthetic:ordinary",
                summary="客户画像属于普通事实线索。",
                dedupe_key="ordinary-1",
            )
            add_claim(
                config,
                "BUSINESS-20260831-002",
                "DNV 认证状态待确认",
                "dingtalk:synthetic:dnv",
                summary="DNV 认证状态属于高风险事实。",
                dedupe_key="high-risk-1",
                category="internal_material",
            )
            add_claim(
                config,
                "BUSINESS-20260831-003",
                "船舶产品线归属与优先级待确认",
                "dingtalk:synthetic:choice",
                summary="产品线归属需要业务负责人选择。",
                dedupe_key="manual-1",
                category="internal_material",
            )
            add_claim(
                config,
                "BUSINESS-20260831-004",
                "海外合作事实线索",
                "dingtalk:synthetic:duplicate-root",
                summary="同一根来源的相同命题。",
                dedupe_key="duplicate-a",
            )
            add_claim(
                config,
                "BUSINESS-20260831-005",
                "海外合作事实线索",
                "dingtalk:synthetic:duplicate-root",
                summary="同一根来源的相同命题。",
                dedupe_key="duplicate-b",
            )

            stale_plan = reconciliation.plan_reconciliation(config)
            add_claim(
                config,
                "BUSINESS-20260831-006",
                "技术参数核验",
                "dingtalk:synthetic:technical",
                summary="技术参数需要权威原件。",
                dedupe_key="technical-1",
                category="internal_material",
            )
            expect_error(
                "source_changed",
                reconciliation.apply_plan,
                config,
                stale_plan,
                run_id="stale-plan",
                require_maintenance=False,
            )
            checks += 1

            plan = reconciliation.plan_reconciliation(config)
            check(plan["counts"]["total_claims"] == 6, "every claim has a migration disposition")
            check(plan["counts"]["archived_duplicate"] == 1, "same root and proposition are deduplicated")
            check(plan["counts"]["needs_user_cases"] == 1, "only the real business choice needs the user")
            check(
                sum(plan["counts"][key] for key in ("automatic_observation", "manual_required_claims", "archived_duplicate"))
                == plan["counts"]["active_claims"],
                "migration accounting is complete",
            )
            checks += 4
            reconciliation.apply_plan(config, plan, run_id="initial", require_maintenance=False)

            writer = reconciliation.connect()
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO metadata(key,value,updated_at) VALUES('hot-wal-test','visible',?)",
                (reconciliation.now_iso(),),
            )
            writer.commit()
            reader = reconciliation.connect(readonly=True)
            check(reader.execute("SELECT value FROM metadata WHERE key='hot-wal-test'").fetchone()[0] == "visible", "read path opens a live WAL without blanking the workbench")
            check(reader.execute("PRAGMA query_only").fetchone()[0] == 1, "recovered read handles remain query-only")
            try:
                reader.execute("UPDATE metadata SET value='changed' WHERE key='hot-wal-test'")
                raise AssertionError("query-only handle accepted a write")
            except sqlite3.OperationalError:
                pass
            reader.close()
            writer.close()
            real_connect = reconciliation.sqlite3.connect
            attempts = {"count": 0}

            def flaky_connect(*args, **kwargs):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise sqlite3.OperationalError("synthetic transient open failure")
                return real_connect(*args, **kwargs)

            with mock.patch.object(reconciliation.sqlite3, "connect", side_effect=flaky_connect):
                retried = reconciliation.connect(readonly=True)
                check(retried.execute("SELECT COUNT(*) FROM cases").fetchone()[0] > 0, "transient SQLite open errors are retried")
                retried.close()
            check(attempts["count"] == 2, "read retry is bounded and stops after recovery")
            checks += 4

            handles = {row["claim_id"]: row["claim_handle"] for row in plan["claims"]}
            check(reconciliation.work_counts()["needs_user"] == 1, "manual badge counts cases, not claims")
            check(claim_row("BUSINESS-20260831-005")["lifecycle"] == "archived", "duplicate exits active work")
            checks += 2

            allowed = reconciliation.open_claim_handles()
            expect_error(
                "relation_invalid",
                reconciliation.validate_relations,
                [{"claim_handle": "claim-unknown", "relation": "support", "source_tokens": ["S1"], "reason": "x"}],
                allowed_handles=allowed,
                allowed_tokens={"S1"},
            )
            expect_error(
                "relation_source",
                reconciliation.validate_relations,
                [relation(handles["BUSINESS-20260831-001"], "support", "BAD")],
                allowed_handles=allowed,
                allowed_tokens={"S1"},
            )
            checks += 2

            ordinary_handle = handles["BUSINESS-20260831-001"]
            reconciliation.record_relations(
                [relation(ordinary_handle, "support", "M1")],
                source_system="dingtalk_chat",
                source_material={
                    "M1": descriptor(
                        "model suggestion",
                        "1" * 32,
                        "model",
                        verification="model_proposed",
                    )
                },
            )
            check(claim_row("BUSINESS-20260831-001")["lifecycle"] == "open", "model inference cannot confirm a fact")
            checks += 1

            for token, root_hash in (("F1", "2" * 32), ("F2", "3" * 32)):
                reconciliation.record_relations(
                    [relation(ordinary_handle, "support", token)],
                    source_system="email",
                    source_material={token: descriptor(token, root_hash, "forward-chain")},
                )
            check(
                claim_row("BUSINESS-20260831-001")["lifecycle"] == "open",
                "forwarded copies in one independence group cannot double vote",
            )
            before = reconciliation.work_counts()["automatic"]
            reconciliation.record_relations(
                [relation(ordinary_handle, "support", "I1")],
                source_system="dingtalk_minutes",
                source_material={"I1": descriptor("independent minutes", "4" * 32, "independent-minutes")},
            )
            check(claim_row("BUSINESS-20260831-001")["resolution_reason"] == "confirmed", "two independent direct sources can close an ordinary fact")
            check(reconciliation.work_counts()["automatic"] < before, "related later evidence reduces open work")
            checks += 3

            high_handle = handles["BUSINESS-20260831-002"]
            for token, root_hash, group in (("H1", "5" * 32, "one"), ("H2", "6" * 32, "two")):
                reconciliation.record_relations(
                    [relation(high_handle, "support", token)],
                    source_system="email",
                    source_material={token: descriptor(token, root_hash, group)},
                )
            check(claim_row("BUSINESS-20260831-002")["lifecycle"] == "open", "high-risk facts reject ordinary corroboration")
            reconciliation.record_relations(
                [relation(high_handle, "support", "H3")],
                source_system="email",
                source_material={
                    "H3": descriptor(
                        "controlled original",
                        "7" * 32,
                        "controlled-original",
                        authority="controlled_original",
                        verification="controlled_original_hash",
                    )
                },
            )
            check(claim_row("BUSINESS-20260831-002")["resolution_reason"] == "confirmed", "controlled original can close a high-risk fact")
            checks += 2

            manual_case = reconciliation.list_cases("needs_user")["items"][0]
            manual_detail = reconciliation.case_detail(manual_case["case_id"])
            guidance = manual_detail["decision_guidance"]
            check("船舶产品线归属与优先级待确认" in guidance["question"], "decision question names the actual matter")
            check("不能证明负责人已经选定" in guidance["system_finding"], "detail states what the system did and did not establish")
            check("你需要" in guidance["owner_must_decide"] and "不能由证据数量或 AI 自动代选" in guidance["why_not_automatic"], "detail states the owner decision and automation boundary")
            checks += 3
            expect_error(
                "preview_stale",
                reconciliation.preview_case,
                manual_case["case_id"],
                {
                    "action": "continue_automatic",
                    "params": {},
                    "context_hash": "stale",
                    "conversation_hash": "c" * 64,
                    "case_version": manual_case["case_version"],
                },
                current_conversation_hash="c" * 64,
                completed_exchanges=1,
            )
            preview = reconciliation.preview_case(
                manual_case["case_id"],
                {
                    "action": "continue_automatic",
                    "params": {},
                    "context_hash": manual_case["context_hash"],
                    "conversation_hash": "c" * 64,
                    "case_version": manual_case["case_version"],
                },
                current_conversation_hash="c" * 64,
                completed_exchanges=1,
            )
            key = str(uuid.uuid4())
            applied = reconciliation.apply_case(
                manual_case["case_id"],
                {"preview_token": preview["preview_token"], "idempotency_key": key},
                current_conversation_hash="c" * 64,
            )
            replay = reconciliation.apply_case(
                manual_case["case_id"],
                {"preview_token": preview["preview_token"], "idempotency_key": key},
                current_conversation_hash="c" * 64,
            )
            check(applied == replay and not applied["business_fact_changed"], "explicit choice is idempotent and does not write a fact")
            check(reconciliation.work_counts()["needs_user"] == 0, "manual suppression changes only workload state")
            checks += 3

            manual_handle = handles["BUSINESS-20260831-003"]
            reconciliation.record_relations(
                [relation(manual_handle, "support", "A1")],
                source_system="email",
                source_material={
                    "A1": descriptor(
                        "signed authority",
                        "8" * 32,
                        "authority",
                        authority="controlled_original",
                        verification="controlled_original_hash",
                    )
                },
            )
            check(claim_row("BUSINESS-20260831-003")["lifecycle"] == "open", "evidence cannot make a business choice for the user")
            checks += 1

            unrelated_handle = handles["BUSINESS-20260831-006"]
            reconciliation.record_relations(
                [relation(unrelated_handle, "unrelated", "U1")],
                source_system="dingtalk_chat",
                source_material={"U1": descriptor("unrelated evidence", "9" * 32, "unrelated")},
            )
            check(claim_row("BUSINESS-20260831-006")["lifecycle"] == "open", "unrelated evidence cannot close a claim")
            checks += 1

            secret_text = "raw-private-message-that-must-not-enter-outbox"
            result = reconciliation.submit_relations(
                config,
                [relation(unrelated_handle, "unrelated", "O1")],
                source_system="dingtalk_chat",
                source_material={"O1": descriptor(secret_text, "a" * 32, "outbox")},
            )
            receipt = next(reconciliation.RELATION_OUTBOX.glob(f"{result['receipt_id']}.*.json"))
            outbox_text = receipt.read_text(encoding="utf-8")
            check(secret_text not in outbox_text and '"material": ""' in outbox_text, "outbox stores descriptors, never raw prose")
            checks += 1

            with mock.patch.object(
                reconciliation,
                "persist_resolved_claims",
                side_effect=OSError("synthetic persistence failure"),
            ):
                try:
                    reconciliation.record_relations(
                        [relation(unrelated_handle, "support", "P1")],
                        source_system="email",
                        source_material={
                            "P1": descriptor(
                                "controlled technical original",
                                "b" * 32,
                                "controlled-technical",
                                authority="controlled_original",
                                verification="controlled_original_hash",
                            )
                        },
                        config=config,
                    )
                except OSError:
                    pass
                else:
                    raise AssertionError("synthetic persistence failure must propagate")
            failed_row = claim_row("BUSINESS-20260831-006")
            check(failed_row["lifecycle"] == "open", "failed persistence reopens the derived claim state")
            failed_case = reconciliation.case_detail(str(failed_row["case_id"]))
            check(failed_case["processing_state"] == "failed", "failed persistence remains visible for retry")
            checks += 2

            detail = reconciliation.case_detail(manual_case["case_id"])
            public_text = json.dumps(detail, ensure_ascii=False)
            check("business-claim:" not in public_text and "business-claim-base" not in public_text, "public API omits raw markers")
            check(router.remove_claim_blocks(ordinary_page.read_text(encoding="utf-8"), ["BUSINESS-20260831-001"]), "resolved block can be removed with CAS metadata")
            checks += 2

            check(router.validate_claims(vault) == ["customer context V2 gate unavailable: PreflightError: registry_unavailable"], "claim checks pass while the synthetic vault without an entity registry fails its context gate closed")
            checks += 1

            reconciliation.DATABASE = original["DATABASE"]
            expect_error("storage_scope_mixed", reconciliation.validate_write_scope, config)
            reconciliation.RESOLUTION_ARCHIVE = original["RESOLUTION_ARCHIVE"]
            reconciliation.RELATION_OUTBOX = original["RELATION_OUTBOX"]
            expect_error("storage_scope_mismatch", reconciliation.validate_write_scope, config)
            checks += 2
    finally:
        for name, value in sync_original.items():
            setattr(syncv2, name, value)
        for name, value in original.items():
            setattr(reconciliation, name, value)

    print(json.dumps({"verdict": "pass", "checks": checks}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
