#!/usr/bin/env python3
"""Offline and read-only acceptance tests for DingTalk Minutes memory sync."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import inspect
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import dingtalk_minutes_memory as minutes
import dingtalk_sync_v2 as syncv2


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    config = minutes.load_config()
    governed = config["dingtalk_minutes_memory"]
    check(governed["secondary_transcription_review"] is True and governed["audio_relisten"] is False, "lock-screen review is enabled without changing source extraction")
    check(governed["source_sync_minutes"] == [2, 32] and governed["semantic_minutes"] == [10, 40], "governed cadence is fixed")
    check(
        governed["summary_comparison_policy_version"] == 1
        and governed["summary_comparison_reasoning_effort"] == "high",
        "summary comparison policy is pinned",
    )
    check(
        minutes.required_missing_artifacts({"missing_artifacts": ["todos"]}) == []
        and minutes.required_missing_artifacts({"missing_artifacts": ["transcript", "todos"]}) == ["transcript"],
        "optional todo data never blocks source or memory synchronization",
    )
    review = config["dingtalk_review"]
    check(
        review["trigger"] == "screen_locked"
        and review["drain_while_locked"] is True
        and review["batch_limit"] == 2
        and review["check_interval_seconds"] == 60
        and review["finish_current_on_unlock"] is True
        and review["manual_limit"] == 2
        and review["require_ac_power"] is True
        and review["retry_failed_automatically"] is False,
        "review resource gates and retry policy are fixed",
    )

    paragraphs = [
        {"token": f"P{index:06d}", "speaker": "发言人", "text": "业务信息" * 260}
        for index in range(1, 280)
    ]
    chunks = minutes.split_chunks(paragraphs, 100000)
    check(len(chunks) >= 2, "long transcript is split")
    check(all(len(syncv2.canonical_json(chunk)) <= 100000 for chunk in chunks), "every serialized chunk stays within 100000 characters")
    flattened = [paragraph for chunk in chunks for paragraph in chunk]
    check(flattened == paragraphs, "chunking covers every normalized paragraph without truncation")
    try:
        minutes.split_chunks([{"token": "P000001", "speaker": "发言人", "text": "字" * 2000}], 1000)
        raise AssertionError("oversized source paragraph was silently truncated")
    except minutes.MinutesMemoryError as exc:
        check(exc.code == "transcript_paragraph_oversized", "oversized paragraph fails closed")

    with tempfile.TemporaryDirectory() as temporary:
        raw_root = Path(temporary)
        task_uuid = "safe-task-uuid"
        task_root = raw_root / task_uuid
        task_root.mkdir()
        transcript_path = task_root / "transcript.json"
        payload = {
            "complete": True,
            "taskUuid": task_uuid,
            "paragraphCount": 2,
            "paragraphList": [
                {"paragraph": "第一段完整文字", "speakerDisplay": "甲"},
                {"paragraph": "第二段完整文字", "speakerDisplay": "乙"},
            ],
        }
        transcript_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(minutes, "RAW_ROOT", raw_root):
            _path, loaded = minutes.transcript_payload(task_uuid)
            check(len(minutes.paragraph_documents(loaded)) == 2, "complete transcript identity and paragraph count are accepted")
            payload["paragraphCount"] = 3
            transcript_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            try:
                minutes.transcript_payload(task_uuid)
                raise AssertionError("incomplete paragraph coverage was accepted")
            except minutes.MinutesMemoryError as exc:
                check(exc.code == "transcript_coverage", "paragraph coverage mismatch fails closed")

    source_sync_source = inspect.getsource(minutes.run_source_sync)
    check(
        "write_source_note" not in source_sync_source
        and "memory.apply_changes" not in source_sync_source
        and not hasattr(minutes, "write_source_note"),
        "raw source sync cannot publish a native-summary placeholder",
    )
    check(minutes.source_hard_failures(["media_incomplete"]) == [], "upstream media delay is not duplicated as a source-writer failure")
    check(minutes.source_hard_failures(["media_incomplete", "source_incomplete"]) == ["source_incomplete"], "independent source defects still fail closed")
    sensitive_task_ref = "76327569643432333137393137345"
    sanitized_summary = minutes.sanitize_native_summary(
        f"摘要 ![图片](https://example.invalid/{sensitive_task_ref}?OSSAccessKeyId=secret&Signature=signed)",
        sensitive_task_ref,
    )
    check(
        "https://" not in sanitized_summary
        and sensitive_task_ref not in sanitized_summary
        and "OSSAccessKeyId" not in sanitized_summary
        and "Signature=" not in sanitized_summary,
        "native summary removes signed links and embedded task identifiers",
    )

    final_row = {
        "recording_id": 'cf8d2ee1-74f1-53c7-a625-7848b0c23241',
        "source_memory_id": "source-id",
        "source_memory_path": "/tmp/dingtalk-final-source-selftest.md",
        "source_ref": 'dingtalk-minutes-batch:cf8d2ee1-74f1-53c7-a625-7848b0c23241',
        "title": "测试听记",
        "start_time": "2020-01-02T09:00:00+08:00",
        "transcript_sha256": "safe-hash",
        "completed_at": None,
    }
    final_proposal = {
        "recording_ref": final_row["recording_id"],
        "source_summary": {
            "title": "测试听记",
            "summary": "这是在未读取钉钉纪要时，由完整转写独立形成的纪要。",
            "evidence_boundary": "已覆盖完整文字转写，尚未复听音频。",
        },
        "candidates": [],
        "no_business_change": True,
    }
    final_comparison = {
        "recording_ref": final_row["recording_id"],
        "native_summary_available": True,
        "overall_relation": "partially_matched",
        "agreements": ["会议主题一致"],
        "independent_only": ["完整转写包含一项补充讨论"],
        "native_only": [],
        "conflicts": [],
        "publication_note": "两份纪要部分一致，正文仍采用全文独立纪要。",
        "evidence_boundary": "本阶段只对比文字纪要，没有复听音频。",
    }
    with mock.patch.object(minutes.memory, "runtime_paths", return_value={"memory_root": Path("/tmp")}), mock.patch.object(
        minutes.memory, "memory_records", return_value=[]
    ):
        validated_final = minutes.validate_final_result(config, final_proposal, final_row)
    check(validated_final["source_summary"]["summary"].startswith("这是"), "independent full-transcript summary is mandatory")
    try:
        invalid_final = dict(final_proposal)
        invalid_final["source_summary"] = None
        with mock.patch.object(minutes.memory, "runtime_paths", return_value={"memory_root": Path("/tmp")}), mock.patch.object(
            minutes.memory, "memory_records", return_value=[]
        ):
            minutes.validate_final_result(config, invalid_final, final_row)
        raise AssertionError("missing independent source summary was accepted")
    except minutes.MinutesMemoryError as exc:
        check(exc.code == "final_result_shape", "missing independent summary fails closed")
    validated_comparison = minutes.validate_summary_comparison(
        final_comparison,
        final_row,
        {"available": True, "text": "钉钉纪要", "sha256": "native-hash"},
    )
    check(validated_comparison["overall_relation"] == "partially_matched", "native summary comparison is validated")
    missing_comparison = minutes.validate_summary_comparison(
        minutes.missing_native_comparison(final_row),
        final_row,
        {"available": False, "text": "", "sha256": None},
    )
    check(missing_comparison["overall_relation"] == "native_missing", "missing native summary does not block independent publication")

    final_lock = mock.MagicMock()
    final_db = mock.MagicMock()
    with mock.patch.object(minutes.memory, "runtime_paths", return_value={"memory_root": Path("/tmp")}), mock.patch.object(
        minutes.memory,
        "memory_records",
        return_value=[],
    ), mock.patch.object(
        minutes,
        "source_note_publish_change",
        return_value=(Path("/tmp/dingtalk-final-source-selftest.md"), "safe final note", "source-id"),
    ), mock.patch.object(
        minutes.memory,
        "acquire_lock",
        return_value=final_lock,
    ), mock.patch.object(
        minutes.memory,
        "apply_changes",
        return_value={"writes": 1, "validated": True},
    ) as final_apply, mock.patch.object(minutes.syncv2, "connect", return_value=final_db), mock.patch.dict(
        minutes.os.environ,
        {"DINGTALK_SUPPRESS_COMPLETION_OUTBOX": "1"},
    ):
        final_result = minutes.apply_final_result(
            config,
            final_row,
            validated_final,
            validated_comparison,
            "native-hash",
        )
    check(final_result["writes"] == 1, "semantic finalization completes under the shared lock")
    check(final_apply.call_args.kwargs.get("lock_held") is True, "semantic finalization passes the already-held shared lock")
    final_lock.close.assert_called_once()
    final_db.close.assert_called_once()

    backfill_db = mock.MagicMock()
    backfill_db.execute.return_value.fetchone.return_value = {
        **final_row,
        "status": "completed",
    }
    with mock.patch.object(minutes.syncv2, "connect", return_value=backfill_db), mock.patch.object(
        minutes,
        "process_summary_comparison",
        return_value={"kind": "summary_comparison", "status": "noop", "model_called": False},
    ) as process_comparison:
        backfill_result = minutes.backfill_summary_comparison(config, final_row["recording_id"])
    check(backfill_result["status"] == "noop", "comparison backfill is idempotent")
    check(
        process_comparison.call_args.kwargs == {"publish_candidates": False, "allow_legacy_proposal": True},
        "comparison backfill cannot republish business candidates",
    )
    current_contract = {
        "transcript_sha256": "a" * 64,
        "prompt_sha256": "b" * 64,
        "schema_sha256": "c" * 64,
    }
    prior_prompt_metadata = {**current_contract, "prompt_sha256": "d" * 64}
    check(
        not minutes._artifact_contract_matches(prior_prompt_metadata, current_contract),
        "automatic processing rejects a prior prompt contract",
    )
    check(
        minutes._artifact_contract_matches(
            prior_prompt_metadata,
            current_contract,
            allow_prior_prompt=True,
        ),
        "explicit comparison backfill accepts a prior prompt with intact data contracts",
    )
    check(
        not minutes._artifact_contract_matches(
            {**prior_prompt_metadata, "schema_sha256": "e" * 64},
            current_contract,
            allow_prior_prompt=True,
        ),
        "explicit backfill never relaxes the schema contract",
    )

    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        recording_id = final_row["recording_id"]
        final_dir = temporary_root / recording_id / "final"
        final_dir.mkdir(parents=True)
        prompt = temporary_root / "current-prompt.md"
        schema = temporary_root / "current-schema.json"
        prompt.write_text("current independent-summary prompt", encoding="utf-8")
        schema.write_text("{}", encoding="utf-8")
        proposal_path = final_dir / "proposal.json"
        proposal_path.write_text(json.dumps(final_proposal, ensure_ascii=False), encoding="utf-8")
        proposal_sha = syncv2.sha256_text(syncv2.canonical_json(final_proposal))
        (final_dir / "independent-summary-meta.json").write_text(json.dumps({
            "schema_version": 1,
            "transcript_sha256": final_row["transcript_sha256"],
            "proposal_sha256": proposal_sha,
            "prompt_sha256": "f" * 64,
            "schema_sha256": syncv2.sha256_file(schema),
        }), encoding="utf-8")
        with mock.patch.object(minutes.syncv2, "RUNS_DIR", temporary_root), mock.patch.object(
            minutes, "FINAL_PROMPT", prompt
        ), mock.patch.object(minutes, "FINAL_SCHEMA", schema), mock.patch.object(
            minutes, "validate_final_result", return_value=final_proposal
        ):
            check(
                minutes.independent_proposal(config, final_row) is None,
                "normal semantic processing rejects a prior prompt contract",
            )
            check(
                minutes.independent_proposal(config, final_row, allow_legacy=True) == final_proposal,
                "explicit comparison-only backfill reuses an integrity-checked prior summary",
            )
        with mock.patch.object(minutes.syncv2, "RUNS_DIR", temporary_root), mock.patch.object(
            minutes, "validate_final_result", side_effect=minutes.MinutesMemoryError(
                "final_result_enum", "stale action enum"
            )
        ):
            check(
                minutes.independent_proposal(config, final_row) is None,
                "stale saved proposal is project_thetated under the current routing contract",
            )

    row = {"recording_id": '8a21ae61-066d-546b-b54e-fa10a0cced7c'}
    chunk_row = {"chunk_index": 0}
    valid_chunk = {
        "recording_ref": row["recording_id"],
        "chunk_index": 0,
        "chunk_summary": {
            "summary": "本分段讨论船舶客户需求。",
            "evidence_boundary": "已读取本分段全部文字，尚未复听音频。",
            "source_tokens": ["P000001"],
        },
        "business_points": [
            {
                "dedupe_key": "marine-customer-requirement",
                "title": "船舶客户需求待确认",
                "category": "external_communication",
                "memory_kind": "open_question",
                "summary": "会议中出现一项船舶客户需求。",
                "evidence_boundary": "仅为转写中的会议表述，需责任人确认。",
                "source_tokens": ["P000001"],
            }
        ],
        "no_business_change": False,
    }
    validated = minutes.validate_chunk_result(valid_chunk, row, chunk_row, {"P000001"})
    check(len(validated["business_points"]) == 1, "valid chunk result is accepted")
    try:
        incomplete_summary = json.loads(json.dumps(valid_chunk, ensure_ascii=False))
        incomplete_summary["chunk_summary"]["source_tokens"] = []
        minutes.validate_chunk_result(incomplete_summary, row, chunk_row, {"P000001"})
        raise AssertionError("incomplete chunk summary coverage was accepted")
    except minutes.MinutesMemoryError as exc:
        check(exc.code == "chunk_result_coverage", "chunk summary must cover every source paragraph")
    try:
        invalid = json.loads(json.dumps(valid_chunk, ensure_ascii=False))
        invalid["business_points"][0]["source_tokens"] = ["P999999"]
        minutes.validate_chunk_result(invalid, row, chunk_row, {"P000001"})
        raise AssertionError("unknown paragraph token was accepted")
    except minutes.MinutesMemoryError as exc:
        check(exc.code == "chunk_result_source", "unknown paragraph token fails closed")

    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "interrupted.sqlite"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE recordings(
              recording_id TEXT PRIMARY KEY,status TEXT,last_error_code TEXT,updated_at TEXT
            );
            CREATE TABLE recording_chunks(
              recording_id TEXT,status TEXT,started_at TEXT,last_error_code TEXT
            );
            INSERT INTO recordings VALUES('recording-1','chunking',NULL,'before');
            INSERT INTO recording_chunks VALUES('recording-1','running','before',NULL);
            """
        )
        connection.commit()
        connection.close()

        def test_connect():
            test_connection = sqlite3.connect(database)
            test_connection.row_factory = sqlite3.Row
            return test_connection

        with mock.patch.object(minutes.syncv2, "connect", side_effect=test_connect):
            check(minutes.recover_interrupted_chunks() == 1, "terminated semantic chunk is safely requeued")
        connection = test_connect()
        try:
            recovered = connection.execute("SELECT * FROM recording_chunks").fetchone()
            check(
                recovered["status"] == "pending" and recovered["started_at"] is None,
                "requeued chunk no longer remains falsely running",
            )
        finally:
            connection.close()

    isolated = tempfile.TemporaryDirectory()
    syncv2.ROOT = Path(isolated.name)
    syncv2.RUNS_DIR = syncv2.ROOT / "runs"
    syncv2.ARCHIVE_DIR = syncv2.ROOT / "archive"
    syncv2.AI_LOCK = syncv2.ROOT / "ai.lock"
    syncv2.STATE_DIR = syncv2.ROOT / "state"
    syncv2.DATABASE = syncv2.STATE_DIR / "sync.sqlite"
    syncv2.initialize()
    connection = syncv2.connect()
    try:
        recording_rows = connection.execute("SELECT * FROM recordings").fetchall()
    finally:
        connection.close()
    for recording in recording_rows:
        source_path = recording["source_memory_path"]
        if not source_path:
            continue
        text = Path(str(source_path)).read_text(encoding="utf-8")
        check(str(recording["task_uuid"]) not in text and str(recording["transcript_path"]) not in text, "source note hides task ID and local transcript path")

    snapshot = syncv2.status_snapshot()
    check(snapshot["database_integrity"] == "ok" and snapshot["secure_delete"] == 1, "live state database is healthy")
    print('{"verdict":"pass","checks":19}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
