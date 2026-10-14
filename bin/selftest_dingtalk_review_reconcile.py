#!/usr/bin/env python3
"""Offline review-to-memory reconciliation and crash recovery checks."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import contextlib
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dingtalk_memory_automation as memory
import dingtalk_review_reconcile as review
import dingtalk_sync_v2 as syncv2


def check(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def note(memory_id: str, *, scope: str, status: str, fact_status: str, source_ref: str, body: str) -> str:
    return memory.render_note(
        [
            ("memory_id", memory_id),
            ("category", "conversation_summary" if scope == "conversation_source" else "business_cognition"),
            ("memory_scope", scope),
            ("memory_kind", "conversation_summary" if scope == "conversation_source" else "status"),
            ("origin_type", "internal_communication"),
            ("status", status),
            ("evidence_level", "internal_source" if status == "active" else "unverified"),
            ("sensitivity", "confidential"),
            ("source_system", "dingtalk"),
            ("source_check", "matched" if status == "active" else "unverified"),
            ("fact_status", fact_status),
            ("source_ref", source_ref),
            ("observed_at", "2020-01-02T10:00:00+08:00"),
            ("reviewed_at", None),
            ("valid_until", None),
            ("supersedes", []),
            ("content_hash", ""),
            ("applies_to", ["project:business-energy"]),
        ],
        body,
    )


class DummyLock:
    def close(self) -> None:
        return


def main() -> int:
    legacy_excerpt = review.sanitize_existing_note_excerpt(
        "<!-- legacy -->\n## 旧标题\n\n[[MEM-001|旧记忆]]\n\n---\n正文。",
        3000,
    )
    check("legacy" not in legacy_excerpt and "##" not in legacy_excerpt and "旧记忆" in legacy_excerpt,
          "legacy governed Markdown is safely flattened for read-only model context")
    unsafe_proposal = {
        "status": "completed", "no_business_change": True, "source_summary": "## 不允许的新结构",
        "candidates": [],
    }
    try:
        review.validate_proposal(unsafe_proposal, [])
        raise AssertionError("unsafe model markup was accepted")
    except memory.AutomationError as exc:
        check(exc.code == "proposal_markup", "new model output remains under strict markup validation")

    with tempfile.TemporaryDirectory() as lock_temporary:
        lock_root = Path(lock_temporary)
        ai_lock = lock_root / "shared-ai.lock"
        prompt = lock_root / "prompt.md"
        schema = lock_root / "schema.json"
        codex = lock_root / "codex"
        prompt.write_text("contract", encoding="utf-8")
        schema.write_text("{}", encoding="utf-8")
        codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex.chmod(0o700)
        invoke_config = {
            "dingtalk_automation": {"codex_bin": str(codex)},
            "dingtalk_minutes_memory": {
                "model": "gpt-test",
                "codex_timeout_seconds": 30,
            },
        }
        completed = SimpleNamespace(returncode=0, stdout=json.dumps({"status": "ok"}), stderr="")
        with mock.patch.object(syncv2, "AI_LOCK", ai_lock), \
                mock.patch.object(review.minutes.memory, "isolated_codex_flags", return_value=[]), \
                mock.patch.object(review.minutes.memory, "subscription_only_environment", return_value={}), \
                mock.patch.object(review.minutes.subprocess, "run", return_value=completed) as model_run:
            with syncv2.file_lock(ai_lock, blocking=True) as inherited:
                result = review.minutes.invoke_codex(
                    invoke_config,
                    prompt_path=prompt,
                    schema_path=schema,
                    payload_tag="payload",
                    payload={},
                    effort="low",
                    run_dir=lock_root / "run-valid",
                    shared_ai_lock_fd=inherited.fileno(),
                )
            check(result == {"status": "ok"} and model_run.call_count == 1,
                  "validated inherited lock avoids the parent-child self-lock")
            with syncv2.file_lock(ai_lock, blocking=True):
                try:
                    review.minutes.invoke_codex(
                        invoke_config,
                        prompt_path=prompt,
                        schema_path=schema,
                        payload_tag="payload",
                        payload={},
                        effort="low",
                        run_dir=lock_root / "run-collision",
                    )
                    raise AssertionError("direct lock collision was accepted")
                except review.minutes.MinutesMemoryError as exc:
                    check(exc.code == "chat_ai_priority", "direct caller retains nonblocking lock priority")
            other = lock_root / "other.lock"
            with other.open("a+") as invalid:
                try:
                    review.minutes.invoke_codex(
                        invoke_config,
                        prompt_path=prompt,
                        schema_path=schema,
                        payload_tag="payload",
                        payload={},
                        effort="low",
                        run_dir=lock_root / "run-invalid",
                        shared_ai_lock_fd=invalid.fileno(),
                    )
                    raise AssertionError("invalid inherited lock was accepted")
                except review.minutes.MinutesMemoryError as exc:
                    check(exc.code == "shared_ai_lock_fd_invalid", "invalid inherited lock fails closed")
            with ai_lock.open("a+") as forged:
                try:
                    review.minutes.invoke_codex(
                        invoke_config,
                        prompt_path=prompt,
                        schema_path=schema,
                        payload_tag="payload",
                        payload={},
                        effort="low",
                        run_dir=lock_root / "run-forged",
                        shared_ai_lock_fd=forged.fileno(),
                    )
                    raise AssertionError("unlocked matching lock descriptor was accepted")
                except review.minutes.MinutesMemoryError as exc:
                    check(exc.code == "shared_ai_lock_fd_unheld",
                          "matching but unlocked inherited descriptor fails closed")
            check(model_run.call_count == 1, "lock failures never call the model")

    receipt_buffer = io.StringIO()
    main_args = SimpleNamespace(
        config=Path("config"),
        context_output=None,
        task_uuid="task-review",
        report=Path("report.md"),
        shared_ai_lock_fd=None,
    )
    with mock.patch.object(review, "parse_args", return_value=main_args), \
            mock.patch.object(review.syncv2, "initialize"), \
            mock.patch.object(review.minutes, "load_config", return_value={}), \
            mock.patch.object(
                review,
                "reconcile",
                side_effect=review.minutes.MinutesMemoryError("chat_ai_priority", "busy"),
            ), contextlib.redirect_stdout(receipt_buffer):
        check(review.main() == 0, "shared AI collision exits successfully as deferred")
    deferred_receipt = json.loads(receipt_buffer.getvalue())
    check(
        deferred_receipt == {
            "model_called": False,
            "reason": "chat_ai_priority",
            "status": "deferred",
        },
        "shared AI collision emits a no-model deferred receipt",
    )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        memory_root = root / "00_业务记忆库"
        for directory in ("01_关键业务认知", "02_内部业务资料", "03_外部沟通", "04_对话来源摘要", "90_待确认", "99_规则与模板"):
            (memory_root / directory).mkdir(parents=True, exist_ok=True)
        source_ref = 'dingtalk-minutes-batch:e2b869dc-64d9-5c3e-87ab-f675ac45e9e2'
        source_path = memory_root / "04_对话来源摘要" / "source.md"
        candidate_path = memory_root / "90_待确认" / "candidate.md"
        source_path.write_text(
            note(
                "BUSINESS-20260826-800", scope="conversation_source", status="review_required",
                fact_status="not_applicable", source_ref=source_ref,
                body="## 对话摘要\n\n原转写摘要。\n\n## 证据边界\n\n二查未完成。\n\n## 更新记录\n\n- 建立。\n",
            ), encoding="utf-8",
        )
        candidate_path.write_text(
            note(
                "BUSINESS-20260826-801", scope="internal_nonpublic", status="review_required",
                fact_status="pending", source_ref=source_ref,
                body="## 记忆内容\n\n原候选。\n\n## 证据边界\n\n基于原转写。\n\n## 关联记忆\n\n无已指定关联条目\n\n## 更新记录\n\n- 建立。\n",
            ), encoding="utf-8",
        )
        state_root = root / "state-root"
        replacements = {
            "ROOT": state_root, "STATE_DIR": state_root / "state", "RUNS_DIR": state_root / "runs",
            "ARCHIVE_DIR": state_root / "archive", "DATABASE": state_root / "state" / "sync.sqlite",
            "AI_LOCK": state_root / "state" / "ai.lock", "SOURCE_LOCK": state_root / "state" / "source.lock",
            "SEMANTIC_LOCK": state_root / "state" / "semantic.lock", "DECISION_LOCK": state_root / "state" / "decision.lock",
            "MEDIA_LOCK": state_root / "state" / "media.lock", "MANUAL_SYNC_LOCK": state_root / "state" / "manual.lock",
            "MANUAL_SYNC_FLAG": state_root / "state" / "manual.active", "REVIEW_LOCK": state_root / "state" / "review.lock",
        }
        config = {
            "external_root": str(root / "external"), "memory_root": str(memory_root),
            "dingtalk_minutes_memory": {"review_reconcile_reasoning_effort": "high"},
        }
        prepared_sha = "d" * 64
        prepared_row = {
            "recording_id": "prepared-before-apply",
            "source_memory_id": "BUSINESS-20260826-800",
            "source_memory_path": str(source_path),
        }
        prepared_paths = {"memory_root": memory_root, "runs": root / "prepared-runs"}
        with mock.patch.object(review.memory, "runtime_paths", return_value=prepared_paths):
            check(
                review.prepared_without_apply_can_restart(config, prepared_row, prepared_sha),
                "journal-free prepared state can restart only before any file apply",
            )
            original_source = source_path.read_text(encoding="utf-8")
            source_path.write_text(
                original_source + f"\n<!-- dingtalk-review-reconcile:{prepared_sha[:24]} -->\n",
                encoding="utf-8",
            )
            check(
                not review.prepared_without_apply_can_restart(config, prepared_row, prepared_sha),
                "an existing reconciliation marker blocks a fresh proposal",
            )
            source_path.write_text(original_source, encoding="utf-8")
            (prepared_paths["runs"] / f"review-reconcile-prepared-before-apply-{prepared_sha[:12]}").mkdir(parents=True)
            check(
                not review.prepared_without_apply_can_restart(config, prepared_row, prepared_sha),
                "any transaction directory keeps missing-journal recovery fail-closed",
            )
        context_path = root / "private.nosync" / "受治理记忆上下文.json"
        first_context = review.write_review_memory_context(config, context_path)
        first_bytes = context_path.read_bytes()
        second_context = review.write_review_memory_context(config, context_path)
        context_payload = json.loads(context_path.read_text(encoding="utf-8"))
        check(first_context["status"] == "written" and second_context["status"] == "noop",
              "governed review memory context is deterministic and idempotent")
        check(context_path.read_bytes() == first_bytes and context_payload["policy"] == "memory_inclination_only",
              "review context pins memory to inclination-only use")
        check(
            all(set(item) == {"memory_id", "title", "category", "memory_kind", "status", "fact_status", "source_check", "summary"}
                for item in context_payload["memories"]),
            "review context exposes only governed summaries and verification metadata",
        )
        report_one = root / "review-one.md"
        report_two = root / "review-two.md"
        report_one.write_text("核对稿一", encoding="utf-8")
        report_two.write_text("核对稿二", encoding="utf-8")
        first_proposal = {
            "status": "completed", "no_business_change": False, "source_summary": "二查后的来源摘要。",
            "candidates": [{
                "candidate_memory_id": "BUSINESS-20260826-801", "disposition": "revise",
                "title": "核对后候选", "category": "business_cognition", "memory_kind": "status",
                "summary": "核对后新表述。", "evidence_boundary": "二查已完成，业务事实待确认。",
                "related_memory_ids": [], "conflict_reason": None,
            }],
        }
        second_proposal = {
            "status": "completed", "no_business_change": False, "source_summary": "二查后的第二版摘要。",
            "candidates": [{
                "candidate_memory_id": "BUSINESS-20260826-801", "disposition": "revise",
                "title": "已确认内容的新冲突", "category": "business_cognition", "memory_kind": "status",
                "summary": "二查再次改变了业务含义。", "evidence_boundary": "不得覆盖已确认内容。",
                "related_memory_ids": [], "conflict_reason": None,
            }],
        }

        apply_lock_flags = []

        def apply_changes(_config, _run_id, changes, *, lock_held=False):
            apply_lock_flags.append(bool(lock_held))
            for path, text in changes.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                memory.atomic_write(path, text, mode=0o644)
            return {"writes": len(changes)}

        with mock.patch.multiple(syncv2, **replacements), \
                mock.patch.object(memory, "MEMORY_LOCK", root / "memory.lock"), \
                mock.patch.object(memory, "acquire_lock", return_value=DummyLock()), \
                mock.patch.object(memory, "apply_changes", side_effect=apply_changes), \
                mock.patch.object(review.minutes, "invoke_codex", side_effect=[first_proposal, first_proposal, second_proposal]) as invoke:
            syncv2.initialize()
            connection = syncv2.connect()
            try:
                stamp = syncv2.now_iso()
                connection.execute(
                    """
                    INSERT INTO recordings(
                      recording_id,task_uuid,task_key_sha256,source_ref,title,start_time,source_filename,
                      transcript_path,transcript_sha256,media_sha256,source_memory_id,source_memory_path,
                      status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        'e2b869dc-64d9-5c3e-87ab-f675ac45e9e2', "task-review", "a" * 64,
                        source_ref, "核对测试", "2020-01-02T10:00:00+08:00", "source.md",
                        str(root / "transcript.json"), "b" * 64, "c" * 64,
                        "BUSINESS-20260826-800", str(source_path), "completed", stamp, stamp,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            candidate = {
                "action": "create_pending", "dedupe_key": "review-test", "title": "原候选",
                "category": "business_cognition", "memory_kind": "status", "summary": "原候选。",
                "evidence_boundary": "基于原转写。", "related_memory_ids": [], "conflict_reason": None,
                "content": "原候选。",
            }
            registered = syncv2.register_decision(
                recording_id='e2b869dc-64d9-5c3e-87ab-f675ac45e9e2', source_ref=source_ref,
                candidate=candidate, candidate_memory_id="BUSINESS-20260826-801", candidate_path=candidate_path,
                related_memory_ids=[], related_hashes={},
            )
            original_register = syncv2.register_decision
            calls = {"count": 0}
            def fail_once(**kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("simulated_database_crash")
                return original_register(**kwargs)

            with mock.patch.object(review.syncv2, "register_decision", side_effect=fail_once):
                try:
                    review.reconcile(config, "task-review", report_one)
                    raise AssertionError("simulated database crash was ignored")
                except RuntimeError as exc:
                    check(str(exc) == "simulated_database_crash", "database crash is surfaced")
                connection = syncv2.connect_readonly()
                try:
                    staged = connection.execute("SELECT status FROM review_reconciliations").fetchone()[0]
                finally:
                    connection.close()
                check(staged == "file_committed", "file-first crash leaves a recoverable transaction stage")
                recovered = review.reconcile(config, "task-review", report_one)
            check(recovered["reconciliation_status"] == "review_candidates_revised", "pending candidate is revised after recovery")
            source_after_review = source_path.read_text(encoding="utf-8")
            check("reviewed_at: null" in source_after_review and "processed_at:" in source_after_review,
                  "machine transcription review records processing time without claiming human review")
            connection = syncv2.connect_readonly()
            try:
                decision = connection.execute("SELECT status,revision,candidate_json FROM decisions WHERE decision_id=?", (registered["decision_id"],)).fetchone()
            finally:
                connection.close()
            check(decision["status"] == "pending" and int(decision["revision"]) == 2, "candidate revision increments and remains pending")
            check("核对后新表述" in decision["candidate_json"], "reviewed meaning replaces the unresolved candidate")
            check(review.reconcile(config, "task-review", report_one)["status"] == "noop", "same reviewed report is idempotent")

            connection = syncv2.connect()
            try:
                connection.execute("UPDATE decisions SET status='accepted',resolution='accept_new',resolved_at=? WHERE decision_id=?", (syncv2.now_iso(), registered["decision_id"]))
                connection.commit()
            finally:
                connection.close()
            before = candidate_path.read_bytes()
            conflict = review.reconcile(config, "task-review", report_two)
            check(conflict["reconciliation_status"] == "review_conflict_created" and conflict["conflicts_created"] == 1, "review never overwrites an accepted decision")
            check(candidate_path.read_bytes() == before, "accepted candidate file is not overwritten")
            connection = syncv2.connect_readonly()
            try:
                new_row = connection.execute(
                    "SELECT candidate_path,severity,related_memory_ids_json FROM decisions WHERE decision_id<>? AND status='pending'",
                    (registered["decision_id"],),
                ).fetchone()
            finally:
                connection.close()
            check(new_row and new_row["severity"] == "conflict", "review change creates a high-priority conflict")
            check("BUSINESS-20260826-801" in new_row["related_memory_ids_json"], "new conflict points to the accepted memory")
            check("二次核对稿" in Path(new_row["candidate_path"]).read_text(encoding="utf-8"), "review-created conflict has the correct evidence boundary")
            check(invoke.call_count == 3, "completed reconciliation is reused without another model call")
            check(apply_lock_flags and all(apply_lock_flags), "review reconciliation passes the already-held shared lock")

    print('{"verdict":"pass","checks":28}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
