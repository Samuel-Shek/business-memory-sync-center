#!/usr/bin/env python3
"""Offline checks for recording version invalidation and precise incident recovery."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import tempfile
from pathlib import Path
from unittest import mock

import dingtalk_minutes_memory as minutes
import dingtalk_sync_v2 as syncv2


def check(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        state_root = root / "sync"
        replacements = {
            "ROOT": state_root,
            "STATE_DIR": state_root / "state",
            "RUNS_DIR": state_root / "runs",
            "ARCHIVE_DIR": state_root / "archive",
            "DATABASE": state_root / "state" / "sync.sqlite",
            "AI_LOCK": state_root / "state" / "ai.lock",
            "SOURCE_LOCK": state_root / "state" / "source.lock",
            "SEMANTIC_LOCK": state_root / "state" / "semantic.lock",
            "DECISION_LOCK": state_root / "state" / "decision.lock",
            "MEDIA_LOCK": state_root / "state" / "media.lock",
            "MANUAL_SYNC_LOCK": state_root / "state" / "manual.lock",
            "MANUAL_SYNC_FLAG": state_root / "state" / "manual.active",
            "REVIEW_LOCK": state_root / "state" / "review.lock",
        }
        with mock.patch.multiple(syncv2, **replacements):
            syncv2.initialize()
            transcript = root / "transcript.json"
            transcript.write_text("{}", encoding="utf-8")
            entry = {
                "title": "版本测试录音",
                "start_time": 1787716800000,
                "filename": "version.md",
                "media_sha256": "c" * 64,
            }
            row, created, changed = minutes.upsert_discovered_recording(
                "task-version", entry, transcript, "a" * 64
            )
            check(created and not changed and int(row["transcript_version"]) == 1, "first transcript creates version one")
            connection = syncv2.connect()
            try:
                connection.execute(
                    """
                    INSERT INTO recording_chunks(
                      recording_id,chunk_index,paragraph_start,paragraph_end,input_chars,input_sha256,status
                    ) VALUES(?,0,1,1,2,?,'complete')
                    """,
                    (str(row["recording_id"]), "d" * 64),
                )
                connection.commit()
            finally:
                connection.close()
            candidate = root / "candidate.md"
            candidate.write_text("candidate", encoding="utf-8")
            registered = syncv2.register_decision(
                recording_id=str(row["recording_id"]),
                source_ref=str(row["source_ref"]),
                candidate={
                    "action": "annotate_page", "title": "候选", "category": "business_cognition",
                    "memory_kind": "fact", "summary": "候选", "evidence_boundary": "待核对",
                    "related_memory_ids": [], "content": "候选",
                },
                candidate_memory_id="BUSINESS-20260826-999",
                candidate_path=candidate,
                related_memory_ids=[],
                related_hashes={},
            )
            connection = syncv2.connect()
            try:
                connection.execute(
                    """
                    INSERT INTO decision_previews(
                      token_hash,decision_id,revision,action,merge_text_hash,candidate_hash,
                      candidate_file_hash,related_hashes_json,created_at,expires_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "e" * 64, registered["decision_id"], 1, "accept_new", "f" * 64,
                        syncv2.sha256_text(syncv2.canonical_json({
                            "action": "annotate_page", "title": "候选", "category": "business_cognition",
                            "memory_kind": "fact", "summary": "候选", "evidence_boundary": "待核对",
                            "related_memory_ids": [], "content": "候选",
                        })),
                        syncv2.sha256_file(candidate), "{}", syncv2.now_iso(), "2099-01-01T00:00:00+08:00",
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            updated, created_again, changed = minutes.upsert_discovered_recording(
                "task-version", entry, transcript, "b" * 64
            )
            check(not created_again and changed, "changed transcript is detected")
            check(int(updated["transcript_version"]) == 2 and updated["status"] == "discovered", "changed transcript returns to semantic intake")
            connection = syncv2.connect_readonly()
            try:
                check(connection.execute("SELECT COUNT(*) FROM recording_chunks").fetchone()[0] == 0, "old chunks are invalidated")
                check(connection.execute("SELECT status FROM decisions").fetchone()[0] == "invalidated", "old candidate is invalidated")
                check(connection.execute("SELECT COUNT(*) FROM decision_previews").fetchone()[0] == 0, "old previews are invalidated")
            finally:
                connection.close()

            broad = minutes.record_missing_window(
                "2020-01-02T08:00:00+08:00", "2020-01-02T12:00:00+08:00", "上午缺失录音"
            )
            recovered = minutes.resolve_visible_incidents([
                ("different-task", {"title": "同时段其他录音", "start_time": 1787716800000})
            ])
            check(recovered == 0, "a broad time window cannot close a missing recording incident")
            precise = minutes.record_missing_window(
                "2020-01-02T13:00:00+08:00", "2020-01-02T14:00:00+08:00", "精确测试",
                title="精确目标", expected_start="2020-01-02T13:00:00+08:00",
            )
            exact_ms = int(__import__("datetime").datetime.fromisoformat("2020-01-02T13:00:00+08:00").timestamp() * 1000)
            recovered = minutes.resolve_visible_incidents([
                ("matching-task", {"title": "精确目标", "start_time": exact_ms})
            ])
            check(recovered == 1, "a controlled title and time fingerprint may close the exact incident")
            connection = syncv2.connect_readonly()
            try:
                broad_status = connection.execute("SELECT status FROM incidents WHERE incident_id=?", (broad["incident_id"],)).fetchone()[0]
                precise_status = connection.execute("SELECT status FROM incidents WHERE incident_id=?", (precise["incident_id"],)).fetchone()[0]
            finally:
                connection.close()
            check(broad_status == "open" and precise_status == "resolved", "only the precise incident closes")

    print('{"verdict":"pass","checks":11}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
