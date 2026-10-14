#!/usr/bin/env python3
"""Isolated lifecycle checks for the 4.4 case conversation workbench."""

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

import workbench_attention as attention
import case_conversation as conversation
import claim_reconciliation as reconciliation
import dingtalk_sync_v2 as syncv2
import model_router


def check(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def expect(code: str, call, *args, **kwargs) -> None:
    try:
        call(*args, **kwargs)
    except (conversation.CaseConversationError, reconciliation.ReconciliationError) as exc:
        check(exc.code == code, f"expected {code}, got {exc.code}")
    else:
        raise AssertionError(f"expected {code}")


def add_case(case_id: str, *, dimension: str = "business_choice", label: str = "业务选择") -> None:
    stamp = reconciliation.now_iso()
    connection = reconciliation.connect()
    try:
        connection.execute(
            """
            INSERT INTO cases(
              case_id,subject_key,subject_label,dimension_key,dimension_label,case_kind,
              state,processing_state,title,assessment,evidence_summary,evidence_boundary,
              manual_reason,decision_question,allowed_choices_json,required_user_authority,
              policy_version,evidence_watermark,context_hash,case_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                case_id, f"subject-{case_id[-4:]}", f"合成事项 {case_id[-4:]}", dimension, label,
                "business_choice", "manual_required", "idle", f"合成问题 {case_id[-4:]}",
                "仅用于隔离测试的判断。", "仅用于隔离测试的证据。",
                "不能升级为真实业务事实。", "需要用户参与形成下一步。",
                "下一步怎么处理？", "[]", "local_owner", 1, f"evidence-{case_id[-4:]}",
                f"context-{case_id[-4:]}", 1, stamp, stamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def answer_payload(*, plan: bool = False, memory: bool = False) -> dict:
    return {
        "answer": "我已理解你的想法，并把下一步收敛到可执行范围。",
        "conversation_state": "proposal_ready" if plan else "clarifying",
        "focused_questions": [] if plan else ["还有哪个条件会改变你的选择？"],
        "proposed_plan": "先验证合成方案 A，再根据结果复盘。" if plan else "",
        "limitations": [],
        "memory_candidate": (
            {
                "title": "合成事项采用方案 A",
                "text": "我决定先采用方案 A。",
                "statement_kind": "owner_statement",
                "evidence_boundary": "仅为用户本人决定。",
            }
            if memory else None
        ),
    }


def main() -> int:
    originals = {
        "reconciliation_database": reconciliation.DATABASE,
        "reconciliation_archive": reconciliation.RESOLUTION_ARCHIVE,
        "reconciliation_outbox": reconciliation.RELATION_OUTBOX,
        "reconciliation_maintenance": reconciliation.MAINTENANCE_STATE,
        "sync_database": syncv2.DATABASE,
        "sync_state": syncv2.STATE_DIR,
        "sync_runs": syncv2.RUNS_DIR,
        "sync_archive": syncv2.ARCHIVE_DIR,
        "sync_ai_lock": syncv2.AI_LOCK,
        "sync_decision_lock": syncv2.DECISION_LOCK,
        "spawn": conversation._spawn_worker,
    }
    checks = 0
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reconciliation.DATABASE = root / "claim.sqlite"
            reconciliation.RESOLUTION_ARCHIVE = root / "claim-archive.jsonl"
            reconciliation.RELATION_OUTBOX = root / "claim-outbox"
            reconciliation.MAINTENANCE_STATE = root / "maintenance-state.json"
            syncv2.DATABASE = root / "sync.sqlite"
            syncv2.STATE_DIR = root / "state"
            syncv2.RUNS_DIR = root / "runs"
            syncv2.ARCHIVE_DIR = root / "archive"
            syncv2.AI_LOCK = root / "ai.lock"
            syncv2.DECISION_LOCK = root / "decision.lock"
            conversation._spawn_worker = lambda *_args: None
            reconciliation.initialize()
            syncv2.initialize()
            case_a = "case-" + "a" * 20
            case_b = "case-" + "b" * 20
            case_c = "case-" + "c" * 20
            case_d = "case-" + "d" * 20
            add_case(case_a)
            add_case(case_b, dimension="price", label="价格与报价")
            add_case(case_c)
            add_case(case_d)
            check(conversation.sync_case_threads() == 4, "every stable case receives one thread")
            checks += 1
            connection = syncv2.connect_readonly()
            check(connection.execute("PRAGMA user_version").fetchone()[0] == 11, "formal conversation schema migrates to v11")
            message_columns = {row[1] for row in connection.execute("PRAGMA table_info(case_messages)")}
            run_columns = {row[1] for row in connection.execute("PRAGMA table_info(case_ai_runs)")}
            connection.close()
            check({"ai_model", "ai_reasoning_effort", "ai_speed", "archive_after_reply"}.issubset(message_columns), "messages retain the selected GPT profile and archive intent")
            check({"ai_model", "ai_reasoning_effort", "ai_speed"}.issubset(run_columns), "AI runs retain the selected GPT profile")
            checks += 3

            # Since 2020-01-02 every model call goes through model_router; the
            # selected profile must reach it (first in the chain, same effort)
            # together with a parser, so a bad answer moves the chain on.
            routed = model_router.RouteResult(text=json.dumps(answer_payload()), provider="deepseek",
                                              model="deepseek-flash", seconds=0.1, value=answer_payload())
            with mock.patch.object(model_router, "complete", return_value=routed) as route:
                conversation.invoke_case_ai(
                    {"dingtalk_automation": {"codex_bin": "/usr/bin/true", "codex_timeout_seconds": 5}},
                    {"ai_profile": {"model": "deepseek-flash", "reasoning_effort": "high", "speed": "standard"}},
                    "profile-command",
                )
            kwargs = route.call_args.kwargs
            check(kwargs["chain"][0] == "deepseek:deepseek-flash", "selected DeepSeek model leads the chain")
            check(kwargs["reasoning_effort"] == "high", "selected reasoning strength reaches the router")
            check(callable(kwargs.get("validate")), "answers are validated inside the router")
            checks += 3

            connection = reconciliation.connect()
            connection.execute("UPDATE cases SET subject_key='different-semantic-subject' WHERE case_id=?", (case_d,))
            connection.commit()
            connection.close()
            expect("case_identity_reused", conversation.sync_case_threads)
            connection = reconciliation.connect()
            connection.execute("UPDATE cases SET subject_key=? WHERE case_id=?", (f"subject-{case_d[-4:]}", case_d))
            connection.commit()
            connection.close()
            conversation.sync_case_threads()
            checks += 1

            draft = conversation.save_draft(case_a, {
                "content": "保留到重启后的草稿",
                "case_version": 1,
                "context_hash": "context-aaaa",
            })
            check(draft["content"] == "保留到重启后的草稿", "draft is saved")
            conversation.sync_case_threads()
            check(conversation.conversation_snapshot(case_a)["draft"] == "保留到重启后的草稿", "draft survives rebind/restart")
            expect(
                "message_too_long",
                conversation.save_draft,
                case_a,
                {"content": "x" * 4001, "case_version": 1, "context_hash": "context-aaaa"},
            )
            checks += 3

            first = conversation.append_message(
                root / "config.json",
                case_a,
                {
                    "content": "我先补充一个想法，可以继续澄清吗？", "case_version": 1, "context_hash": "context-aaaa",
                    "ai_profile": {"model": "k3", "reasoning_effort": "max", "speed": "fast"},
                },
            )
            expect(
                "answer_in_progress",
                conversation.append_message,
                root / "config.json",
                case_a,
                {"content": "不能并发发送。", "case_version": 1, "context_hash": "context-aaaa"},
            )
            conversation.answer_message(
                {"dingtalk_automation": {}}, first["message_id"],
                invoke=lambda *_args: answer_payload(),
            )
            snapshot = conversation.conversation_snapshot(case_a)
            check(len(snapshot["messages"]) == 2 and snapshot["completed_exchanges"] == 1, "one exchange is append-only")
            check(snapshot["messages"][0]["ai_profile"] == snapshot["messages"][1]["ai_profile"] == {"model": "k3", "reasoning_effort": "max", "speed": "fast"}, "answer preserves the exact selected GPT profile")
            check(snapshot["ai_configuration"]["default"] == conversation.DEFAULT_AI_PROFILE, "conversation API exposes the Codex-style default profile")
            check(snapshot["draft"] == "", "sending clears only the draft")
            check(not conversation.conversation_snapshot(case_b)["messages"], "case conversations remain isolated")
            checks += 7

            expect(
                "ai_model_invalid",
                conversation.append_message,
                root / "config.json",
                case_a,
                {
                    "content": "无效模型不能进入队列。", "case_version": 1, "context_hash": "context-aaaa",
                    "ai_profile": {"model": "not-a-model", "reasoning_effort": "max", "speed": "fast"},
                },
            )
            checks += 1

            second = conversation.append_message(
                root / "config.json",
                case_a,
                {
                    "content": "我决定先采用方案 A，这样处理可以吗？", "case_version": 1, "context_hash": "context-aaaa",
                    "ai_profile": {"model": "deepseek-flash", "reasoning_effort": "high", "speed": "standard"},
                },
            )
            conversation.answer_message(
                {"dingtalk_automation": {}}, second["message_id"],
                invoke=lambda *_args: answer_payload(plan=True, memory=True),
            )
            snapshot = conversation.conversation_snapshot(case_a)
            check(snapshot["latest_proposed_plan"], "proposal-ready answer exposes a plan")
            check(snapshot["messages"][-1]["ai_profile"]["model"] == "deepseek-flash", "a changed model is bound to its reply")
            check(snapshot["memory_proposal"]["fact_status"] == "owner_statement", "bounded first-person decision may be an owner statement")
            check("不代表公司审批" in snapshot["memory_proposal"]["evidence_boundary"], "owner statement keeps its business boundary")
            checks += 4

            high = conversation.append_message(
                root / "config.json",
                case_b,
                {"content": "我决定客户报价已经批准，这个判断成立吗？", "case_version": 1, "context_hash": "context-bbbb"},
            )
            conversation.answer_message(
                {"dingtalk_automation": {}}, high["message_id"],
                invoke=lambda *_args: answer_payload(plan=True, memory=True),
            )
            high_snapshot = conversation.conversation_snapshot(case_b)
            check(high_snapshot["memory_proposal"]["fact_status"] == "review_required", "price/approval remains review required")
            check("AI 回答不作为来源" in high_snapshot["memory_proposal"]["evidence_boundary"], "high-risk proposal rejects AI as evidence")
            resolve_preview = conversation.preview_action({}, case_b, {
                "action": "resolve_plan", "params": {}, "case_version": 1,
                "context_hash": "context-bbbb", "conversation_hash": high_snapshot["conversation_hash"],
            })
            resolved_plan = conversation.apply_action(
                {}, case_b,
                {"preview_token": resolve_preview["preview_token"], "idempotency_key": str(uuid.uuid4())},
            )
            check(resolved_plan["state"] == "plan_confirmed", "confirmed plan has a distinct non-fact state")
            check(not resolved_plan["business_fact_changed"], "confirmed plan does not claim a business fact changed")
            check(conversation.conversation_snapshot(case_b)["read_only"], "confirmed plan closes the episode read-only")
            checks += 5

            stale = conversation.append_message(
                root / "config.json", case_d,
                {"content": "回答期间改变证据。", "case_version": 1, "context_hash": "context-dddd"},
            )
            def mutate_context(*_args):
                connection = reconciliation.connect()
                connection.execute(
                    "UPDATE cases SET case_version=2,context_hash='context-dddd-new',evidence_watermark='evidence-dddd-new' WHERE case_id=?",
                    (case_d,),
                )
                connection.commit()
                connection.close()
                return answer_payload(plan=True)
            expect(
                "ai_response_superseded", conversation.answer_message,
                {"dingtalk_automation": {}}, stale["message_id"], invoke=mutate_context,
            )
            stale_snapshot = conversation.conversation_snapshot(case_d)
            check(len(stale_snapshot["messages"]) == 1 and stale_snapshot["messages"][0]["status"] == "failed", "late AI answer is discarded without appending")
            connection = syncv2.connect_readonly()
            check(connection.execute("SELECT state FROM case_ai_runs WHERE message_id=?", (stale["message_id"],)).fetchone()[0] == "superseded", "AI run fencing records superseded state")
            connection.close()
            checks += 3

            failing = conversation.append_message(
                root / "config.json",
                case_c,
                {"content": "触发合成超时，可以重试吗？", "case_version": 1, "context_hash": "context-cccc"},
            )
            expect(
                "codex_timeout",
                conversation.answer_message,
                {"dingtalk_automation": {}},
                failing["message_id"],
                invoke=lambda *_args: (_ for _ in ()).throw(
                    conversation.CaseConversationError("codex_timeout", "synthetic", 503)
                ),
            )
            failed_snapshot = conversation.conversation_snapshot(case_c)
            check(failed_snapshot["messages"][0]["status"] == "failed", "failure preserves the original question")
            retry = conversation.retry_message(
                root / "config.json", case_c, failing["message_id"],
                {"case_version": 1, "context_hash": "context-cccc"},
            )
            conversation.answer_message(
                {"dingtalk_automation": {}}, retry["message_id"],
                invoke=lambda *_args: answer_payload(plan=True),
            )
            retry_snapshot = conversation.conversation_snapshot(case_c)
            check(len(retry_snapshot["messages"]) == 3, "retry appends instead of replacing failed history")
            checks += 3

            maintenance_preview = conversation.preview_action({}, case_c, {
                "action": "continue_automatic", "params": {}, "case_version": 1,
                "context_hash": "context-cccc", "conversation_hash": retry_snapshot["conversation_hash"],
            })
            reconciliation.MAINTENANCE_STATE.write_text('{"status":"inactive","epoch":1}\n', encoding="utf-8")
            expect(
                "preview_stale", conversation.apply_action, {}, case_c,
                {"preview_token": maintenance_preview["preview_token"], "idempotency_key": str(uuid.uuid4())},
            )
            checks += 1

            memory_config = {"memory_root": str(root / "vault" / "00_业务记忆库")}
            memory_config["memory_root"] = str(Path(memory_config["memory_root"]))
            Path(memory_config["memory_root"]).mkdir(parents=True)
            target = root / "vault" / str(snapshot["memory_proposal"]["target_page"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("初始合成页面\n", encoding="utf-8")
            current_for_memory = conversation.conversation_snapshot(case_a)
            memory_preview = conversation.preview_action(memory_config, case_a, {
                "action": "write_memory", "params": {"proposal_id": current_for_memory["memory_proposal"]["proposal_id"]},
                "case_version": 1, "context_hash": "context-aaaa",
                "conversation_hash": current_for_memory["conversation_hash"],
            })
            target.write_text("确认后被其他写入改变\n", encoding="utf-8")
            expect(
                "memory_target_stale", conversation.apply_action, memory_config, case_a,
                {"preview_token": memory_preview["preview_token"], "idempotency_key": str(uuid.uuid4())},
            )
            checks += 1

            preview = conversation.preview_action({}, case_a, {
                "action": "continue_automatic", "params": {}, "case_version": 1,
                "context_hash": "context-aaaa", "conversation_hash": snapshot["conversation_hash"],
            })
            third = conversation.append_message(
                root / "config.json", case_a,
                {"content": "这句话会使旧预览失效吗？", "case_version": 1, "context_hash": "context-aaaa"},
            )
            expect(
                "preview_stale",
                conversation.apply_action,
                {}, case_a,
                {"preview_token": preview["preview_token"], "idempotency_key": str(uuid.uuid4())},
            )
            conversation.answer_message(
                {"dingtalk_automation": {}}, third["message_id"],
                invoke=lambda *_args: answer_payload(plan=True),
            )
            current = conversation.conversation_snapshot(case_a)
            preview = conversation.preview_action({}, case_a, {
                "action": "continue_automatic", "params": {}, "case_version": 1,
                "context_hash": "context-aaaa", "conversation_hash": current["conversation_hash"],
            })
            key = str(uuid.uuid4())
            applied = conversation.apply_action({}, case_a, {"preview_token": preview["preview_token"], "idempotency_key": key})
            replay = conversation.apply_action({}, case_a, {"preview_token": preview["preview_token"], "idempotency_key": key})
            check(applied == replay and applied["state"] == "open", "apply is bound and idempotent")
            check(not applied["business_fact_changed"], "automatic observation never changes a business fact")
            check(len(conversation.conversation_snapshot(case_a)["messages"]) == 6, "automatic observation keeps the full conversation")
            checks += 4

            deferred_snapshot = conversation.conversation_snapshot(case_c)
            defer_preview = conversation.preview_action({}, case_c, {
                "action": "defer",
                "params": {"defer_until": "2027-01-02T09:00:00+08:00"},
                "case_version": 1,
                "context_hash": "context-cccc",
                "conversation_hash": deferred_snapshot["conversation_hash"],
            })
            deferred = conversation.apply_action(
                {}, case_c,
                {"preview_token": defer_preview["preview_token"], "idempotency_key": str(uuid.uuid4())},
            )
            check(deferred["state"] == "deferred", "explicit future date defers the case")
            check(reconciliation.list_cases("automatic")["items"][-1]["state"] in {"open", "deferred"}, "deferred case leaves manual workload")
            connection = reconciliation.connect()
            connection.execute(
                """
                UPDATE cases SET state='manual_required',disposition='unhandled',
                  disposition_payload_json='{}',defer_until=NULL,disposition_context_hash=NULL,
                  disposition_conversation_hash=NULL,manual_suppressed=0,suppressed_context_hash=NULL
                WHERE case_id=?
                """,
                (case_c,),
            )
            connection.commit()
            connection.close()
            restored = conversation.restore_durable_actions()
            check(restored["restored"] >= 1, "formal action receipt restores manual disposition after derived state loss")
            check(reconciliation.case_detail(case_c)["state"] == "deferred", "restored defer returns to deferred state")
            checks += 4

            punctuation_cases = {
                "还需要确认吗?": False,
                "还需要确认吗？": False,
                "还需要确认吗？   \n": False,
                "还需要确认吗？”": True,
                "还需要确认吗?)": True,
                "这是最终回答。": True,
                "这是最终回答": True,
                "第一段还有问题？\n第二段是最终回答。": True,
            }
            check(
                all(
                    conversation.should_archive_after_reply(value) is expected
                    for value, expected in punctuation_cases.items()
                ),
                "archive intent follows only the final non-whitespace character",
            )
            checks += 1

            case_e = "case-" + "e" * 20
            case_f = "case-" + "f" * 20
            case_g = "case-" + "0" * 20
            case_h = "case-" + "1" * 20
            case_i = "case-" + "2" * 20
            for case_id in (case_e, case_f, case_g, case_h, case_i):
                add_case(case_id)
            connection = reconciliation.connect()
            connection.execute(
                "UPDATE cases SET subject_key='shared-archive-subject',subject_label='同组归档测试' WHERE case_id IN (?,?)",
                (case_e, case_f),
            )
            connection.commit()
            connection.close()
            conversation.sync_case_threads()

            client_message_id = str(uuid.uuid4())
            final_payload = {
                "content": "这是我的最终回答。   ",
                "case_version": 1,
                "context_hash": "context-eeee",
                "client_message_id": client_message_id,
            }
            submitted = conversation.append_message(root / "config.json", case_e, final_payload)
            replayed_submit = conversation.append_message(root / "config.json", case_e, final_payload)
            check(submitted["saved"] and submitted["attention_state"] == "answered" and attention.decorate(reconciliation.case_detail(case_e))["state"] == "archived", "non-question reply leaves needs-user in its save transaction, before GPT")
            check(
                replayed_submit["client_message_id"] == client_message_id
                and replayed_submit["idempotent_replay"],
                "client message replay does not duplicate the message",
            )
            expect(
                "client_message_conflict",
                conversation.append_message,
                root / "config.json",
                case_e,
                {**final_payload, "content": "同一个编号不能换内容。"},
            )
            answered = conversation.answer_message(
                {"dingtalk_automation": {}}, submitted["message_id"],
                invoke=lambda *_args: answer_payload(),
            )
            check(answered["archived"], "GPT answer continues after saved reply has already left needs-user")
            check(attention.decorate(reconciliation.case_detail(case_e))["state"] == "archived", "only the replied case is archived")
            check(reconciliation.case_detail(case_f)["state"] == "manual_required", "a sibling in the same subject remains in the workbench")
            archived_ids = {item["case_id"] for item in reconciliation.list_cases("archived_questions")["items"]}
            history_ids = {item["case_id"] for item in reconciliation.list_cases("history")["items"]}
            check(case_e in archived_ids and case_e not in history_ids, "question archive has one dedicated scope without history duplication")
            archived_snapshot = conversation.conversation_snapshot(case_e)
            check(archived_snapshot["read_only"] and len(archived_snapshot["messages"]) == 2, "archived conversation is complete and read-only")
            completed_replay = conversation.append_message(root / "config.json", case_e, final_payload)
            check(completed_replay["status"] == "completed", "network replay succeeds even after the case has archived")
            checks += 9

            failing_archive = conversation.append_message(
                root / "config.json", case_g,
                {"content": "这是失败后仍要归档的最终决定。", "case_version": 1, "context_hash": "context-0000"},
            )
            expect(
                "codex_timeout",
                conversation.answer_message,
                {"dingtalk_automation": {}},
                failing_archive["message_id"],
                invoke=lambda *_args: (_ for _ in ()).throw(
                    conversation.CaseConversationError("codex_timeout", "synthetic", 503)
                ),
            )
            check(attention.decorate(reconciliation.case_detail(case_g))["state"] == "archived", "saved reply stays handled when GPT fails")
            archive_retry = conversation.retry_message(
                root / "config.json", case_g, failing_archive["message_id"],
                {"case_version": 1, "context_hash": "context-0000"},
            )
            check(archive_retry["archive_after_reply"], "retry inherits the original archive intent")
            retry_answer = conversation.answer_message(
                {"dingtalk_automation": {}}, archive_retry["message_id"],
                invoke=lambda *_args: answer_payload(),
            )
            check(retry_answer["archived"] and attention.decorate(reconciliation.case_detail(case_g))["state"] == "archived", "successful retry archives after preserving failed history")
            checks += 4

            pending_manual = conversation.append_message(
                root / "config.json", case_h,
                {"content": "请继续回答这个问题？", "case_version": 1, "context_hash": "context-1111"},
            )
            manual_snapshot = conversation.conversation_snapshot(case_h)
            manual_key = str(uuid.uuid4())
            manual_payload = {
                "case_version": 1,
                "context_hash": "context-1111",
                "conversation_hash": manual_snapshot["conversation_hash"],
                "idempotency_key": manual_key,
            }
            manual_result = conversation.archive_case({}, case_h, manual_payload)
            check(manual_result["cancelled_active_reply"], "manual archive cancels an active GPT writeback")
            manual_messages = conversation.conversation_snapshot(case_h)["messages"]
            check(len(manual_messages) == 1 and manual_messages[0]["status"] == "cancelled", "manual archive preserves the user message and isolates the late answer")
            connection = syncv2.connect_readonly()
            manual_run_state = connection.execute(
                "SELECT status FROM action_runs WHERE run_id=(SELECT run_id FROM case_messages WHERE message_id=?)",
                (pending_manual["message_id"],),
            ).fetchone()[0]
            connection.close()
            check(manual_run_state == "interrupted", "manual archive terminates the active GPT run receipt")
            expect(
                "message_not_pending",
                conversation.answer_message,
                {"dingtalk_automation": {}},
                pending_manual["message_id"],
                invoke=lambda *_args: answer_payload(),
            )
            check(conversation.archive_case({}, case_h, manual_payload) == manual_result, "manual archive is idempotent")
            checks += 5

            interrupted = conversation.append_message(
                root / "config.json", case_i,
                {"content": "回答落库后由重启恢复归档。", "case_version": 1, "context_hash": "context-2222"},
            )
            with mock.patch.object(reconciliation, "archive_case_direct", side_effect=RuntimeError("synthetic crash")):
                interrupted_answer = conversation.answer_message(
                    {"dingtalk_automation": {}}, interrupted["message_id"],
                    invoke=lambda *_args: answer_payload(),
                )
            check(interrupted_answer["archived"] and attention.decorate(reconciliation.case_detail(case_i))["state"] == "archived", "saved disposition does not depend on later archive writeback")
            recovery = conversation.recover_pending_archives({})
            check(attention.decorate(reconciliation.case_detail(case_i))["state"] == "archived", "restart preserves the durable saved reply disposition")
            checks += 2

            connection = reconciliation.connect()
            connection.execute(
                """
                UPDATE cases SET state='manual_required',disposition='unhandled',
                  disposition_payload_json='{}',disposition_context_hash=NULL,
                  disposition_conversation_hash=NULL,disposition_at=NULL,resolved_at=NULL
                WHERE case_id=?
                """,
                (case_g,),
            )
            connection.commit()
            connection.close()
            restored_archives = conversation.restore_durable_actions()
            check(attention.decorate(reconciliation.case_detail(case_g))["state"] == "archived", "user disposition survives a rebuilt derived case database")
            connection = reconciliation.connect()
            connection.execute(
                """
                UPDATE cases SET state='manual_required',case_version=case_version+1,
                  context_hash='context-0000-new',evidence_watermark='evidence-0000-new',
                  disposition='unhandled',disposition_payload_json='{}',
                  disposition_context_hash=NULL,disposition_conversation_hash=NULL,
                  disposition_at=NULL,resolved_at=NULL
                WHERE case_id=?
                """,
                (case_g,),
            )
            connection.commit()
            connection.close()
            stale_restore = conversation.restore_durable_actions()
            check(attention.decorate(reconciliation.case_detail(case_g))["state"] == "archived", "context hash changes alone never reopen an answered question")
            check(reconciliation.work_counts()["archived_questions"] >= 3, "archived question count is separate from active workload")
            checks += 3

            check(attention.decorate(reconciliation.case_detail(case_d))["state"] == "archived", "context drift does not erase a saved user reply")
            checks += 1

            for database in (reconciliation.DATABASE, syncv2.DATABASE):
                connection = sqlite3.connect(database)
                check(connection.execute("PRAGMA quick_check").fetchone()[0] == "ok", "database quick_check passes")
                check(not connection.execute("PRAGMA foreign_key_check").fetchall(), "database foreign keys pass")
                connection.close()
                check(database.stat().st_mode & 0o777 == 0o600, "formal database is private")
            checks += 6
    finally:
        reconciliation.DATABASE = originals["reconciliation_database"]
        reconciliation.RESOLUTION_ARCHIVE = originals["reconciliation_archive"]
        reconciliation.RELATION_OUTBOX = originals["reconciliation_outbox"]
        reconciliation.MAINTENANCE_STATE = originals["reconciliation_maintenance"]
        syncv2.DATABASE = originals["sync_database"]
        syncv2.STATE_DIR = originals["sync_state"]
        syncv2.RUNS_DIR = originals["sync_runs"]
        syncv2.ARCHIVE_DIR = originals["sync_archive"]
        syncv2.AI_LOCK = originals["sync_ai_lock"]
        syncv2.DECISION_LOCK = originals["sync_decision_lock"]
        conversation._spawn_worker = originals["spawn"]
    print(json.dumps({"verdict": "pass", "checks": checks}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
