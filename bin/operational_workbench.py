#!/usr/bin/env python3
"""Operational issues are durable; alerts are projections, never a second inbox.

Only the local workbench can see object labels. AI and notification projections
contain bounded operational facts, not mail bodies, paths or attachment content.
GET handlers are read-only. The supervisor/startup and explicit actions reconcile.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import sync_rule_runtime
import chat_read_recovery
import hashlib
import json
from contextlib import closing
from datetime import datetime
import os
import re
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any

import dingtalk_sync_v2 as syncv2
import business_sync_state as states
import email_sync
import business_page_integrity as page_integrity
import video_hold

VALIDATION_SUCCESS_PATH = (
    Path(__file__).resolve().parents[1] / "state/last-memory-validation-success.json"
)
MINUTES_STATE_PATH = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/.state/sync-state.json'))

def _minutes_hold_current(held, proof):
    """Cheap, same-object recheck of a previously hashed source-controller result."""
    try:
        ref = json.loads(held['object_ref'])
        if not isinstance(ref.get('task_uuid'), str) or not re.fullmatch(r'[A-Za-z0-9_-]+', ref['task_uuid']):
            return False
        entry = json.loads(MINUTES_STATE_PATH.read_text())['entries'][ref['task_uuid']]
        if digest(entry) != proof['source_record_sha256']:
            return False
        basic = MINUTES_STATE_PATH.parent.parent / '.raw' / ref['task_uuid'] / 'basic.json'
        if basic.is_symlink() or hashlib.sha256(basic.read_bytes()).hexdigest() != proof['basic_sha256']:
            return False
        if proof['media_kind'] == 'video':
            return entry.get('media_hold_kind') == 'video' and entry.get('video_hold_id') == proof['current_hold_id']
        path = MINUTES_STATE_PATH.parent.parent / '音频' / entry['media_filename']
        stat = path.stat()
        return not path.is_symlink() and [stat.st_size, stat.st_mtime_ns, stat.st_ino] == proof['artifact_stat']
    except (OSError, ValueError, KeyError, TypeError):
        return False

def _minutes_hold_proof(held):
    """Resolve only the old type uncertainty; review and memory keep separate states."""
    if held.get('source_id') != 'dingtalk_minutes' or held.get('kind') != 'unknown_media':
        return None
    try:
        uuid = json.loads(held['object_ref'])['task_uuid']
        if not isinstance(uuid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', uuid):
            return None
        root = MINUTES_STATE_PATH.parent.parent
        entry = json.loads(MINUTES_STATE_PATH.read_text())['entries'][uuid]
        basic_path = root / '.raw' / uuid / 'basic.json'
        if basic_path.is_symlink() or json.loads(basic_path.read_text()).get('taskUuid') != uuid:
            return None
        proof = {'method': 'minutes_source_media_verified', 'task_sha256': digest(uuid),
                 'source_record_sha256': digest(entry), 'basic_sha256': hashlib.sha256(basic_path.read_bytes()).hexdigest()}
        if entry.get('media_hold_kind') == 'video' and entry.get('video_waiting_instruction') is True:
            current = video_hold.by_id(entry.get('video_hold_id'))
            if not current or current.get('kind') != 'video' or current.get('source_id') != 'dingtalk_minutes' or json.loads(current['object_ref']).get('task_uuid') != uuid:
                return None
            proof.update(media_kind='video', current_hold_id=current['hold_id'], body_reads=0)
        else:
            name = entry.get('media_filename', '')
            expected = entry.get('media_sha256', '')
            if (entry.get('video_waiting_instruction') is not False or Path(name).name != name
                    or not str(entry.get('media_content_type', '')).startswith('audio/')
                    or video_hold.classify(name, entry.get('media_content_type'), unknown=False)
                    or not re.fullmatch(r'[a-f0-9]{64}', expected)):
                return None
            path = root / '音频' / name
            if path.is_symlink() or path.resolve().parent != (root / '音频').resolve():
                return None
            stat = path.stat()
            if not stat.st_size or stat.st_size != entry.get('media_bytes'):
                return None
            actual = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(8*1024*1024), b''):
                    actual.update(block)
            if actual.hexdigest() != expected:
                return None
            proof.update(media_kind='audio', media_sha256=expected,
                         artifact_stat=[stat.st_size, stat.st_mtime_ns, stat.st_ino])
        return proof if _minutes_hold_current(held, proof) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None

ISSUE_RE = re.compile(r"issue-[a-f0-9]{24}")
GROUP_RE = re.compile(r"group-[a-f0-9]{24}")
SCOPES = ("needs_user", "automatic", "waiting", "closed")
LABELS = {"needs_user": "需你处理", "automatic": "系统处理中", "waiting": "等待条件", "closed": "已结束"}
SOURCE_NAMES = {"wechat": "微信聊天","dingtalk_mail": "钉钉邮件", "dingtalk_chat": "钉钉聊天",
                "dingtalk_minutes": "钉钉 AI 听记", "win11": "Win11",
                "memory_core": "记忆库核心", "system": "同步中心"}
REASONS = {
    "codex_subscription_limit": ("模型链未取得合格结果", "此为历史兼容错误码；实际服务商及失败原因以本次逐模型回执为准，系统按已有模型策略继续恢复。"),
    "chat_memory_codex_subscription_limit": ("聊天模型链未取得合格结果", "DeepSeek、Kimi 按现行配置与冷却策略处理；额度、来源变化与输出校验失败分别保留，不能跳过证据检查。"),
    "model_source_changed": ("当前来源与模型结果不匹配", "重新读取当前来源并核验引用，保留不匹配结果；禁止跳过关联检查。"),
    "model_quota_unavailable": ("模型服务余额或额度暂不可用", "系统沿用既有备用模型与退避策略，不自动充值。"),
    "source_note_collision": ("邮件来源笔记定位冲突", "系统核对笔记类型和来源标记，保留所有笔记及历史；技术冲突由自动调查处理。"),
    "image_download_retryable": ("图片下载暂时失败，等待自动重试", "尚未进入图片识别；保留失败原因，遵守来源冷却时间和有界重试次数。"),
    "image_download_auth_required": ("图片下载的认证或权限受阻", "尚未进入图片识别；需恢复原账号访问条件，不能据此认定图片损坏。"),
    "image_download_blocked": ("图片下载被来源阻止重试", "尚未进入图片识别；来源明确不允许自动重试，需按具体失败回执处理。"),
    "image_download_unclassified": ("图片下载失败，原因待核验", "尚未进入图片识别；已保留脱敏失败回执，未知错误不作为图片损坏或内容不完整的证据。"),
    "chat_reconcile_incomplete": ("聊天补同步尚未完整，后台将自动续跑", "保留已取得消息和失败记录；按退避间隔重试，全部页面完整后才结束。"),
    "chat_read_transient": ("聊天读取暂时失败，后台自动重试", "遵守来源冷却时间；短暂失败限次重试，持续失败转低频探测。"),
    "chat_read_unclassified": ("聊天来源读取失败，后台等待再探测", "已保存脱敏失败证据；不会快速反复请求或把未知原因写成已解决。"),
    "chat_read_auth_required": ("聊天来源认证或权限受阻", "保留现有消息，停止重复来源读取；需要恢复原账号权限后重新核验。"),
    "chat_read_blocked": ("聊天来源读取受阻，等待具体原因核验", "保留消息和失败证据；仅明确认证、结构损坏或冲突需人工，其他读取失败按退避计划复查。"),
    'video_waiting_instruction': ('视频待命', '按你的指令只登记视频，暂不下载或解析；其他内容继续同步。'),
    'media_type_waiting_confirmation': ('媒体类型待确认', '信息不足，暂不下载或解析；其他内容继续同步。'),
    "validation_inputs_changed": ("已同步，等待资料稳定后校验", "后台自动等待稳定并重验；已有同步步骤保留，取得匹配当前资料的完整回执后自动更新此问题。"),
    "partial_static": ("附件内容提取不完整", "邮件归档、记忆处理与附件读取完整度分别核对；部分提取不代表附件损坏或打不开。相同读取限制下重复检查不会补齐内容。"),
    "recovery_conflict_requires_review": ("旧批次与后来的业务修改冲突", "当前文件与两份历史快照均不相同。已保留当前内容和关联消息；需核对后明确决定，不能靠重复回退解决。"),
    "proposal_claim_relation": ("聊天提案未通过证据关联检查", "消息和失败批次已保留，尚未写入业务记忆。不会跳过检查或自动重复调用模型，需先核对提案与引用关系。"),
    "chat_reconcile_proposal_claim_relation": ("聊天提案未通过证据关联检查", "消息和失败批次已保留，尚未写入业务记忆。不会跳过检查或自动重复调用模型，需先核对提案与引用关系。"),
    "chat_memory_proposal_claim_relation": ("聊天提案未通过证据关联检查", "消息和失败批次已保留，尚未写入业务记忆。不会跳过检查或自动重复调用模型，需先核对提案与引用关系。"),
    "event_retry_budget_exhausted": ("实时监听达到受限重试上限", "停止重新建立同一订阅。需要排除实际阻断；订阅有效、监听就绪且收到真实新消息后才结束。"),
    "event_not_ready": ("实时监听尚未就绪", "历史补同步不能代替实时验收；必须核实订阅与就绪后的真实新消息。"),
    "validation_failed": ("完整记忆校验未通过", "只刷新索引或看到进程运行不算恢复；必须取得匹配当前资料与程序的完整成功回执。"),
    "claim_base_mismatch": ("业务页面校验基线失效", "页面正文与受治理基线不一致。必须通过受治理写入器恢复，并取得匹配当前资料的完整成功回执。"),
    "claim_base_untracked_suffix": ("业务页面存在未受基线保护的正文", "页面的受治理区域之外出现了正文。必须通过统一写入器核对并纳入基线，不能用重复校验或直接重建基线掩盖。"),
    "archive_not_opened": ("压缩包按安全策略未展开", "安全限制，不是重试失败。系统不会自动展开未知压缩包。"),
    "archive_invalid": ("压缩结构无法读取", "现有读取器无法解析此文件；反复读取不能证明文件内容可用。"),
    "blocked_active_content": ("活动内容已被安全拦截", "文件可能包含可执行内容或宏；系统不能替你批准执行。"),
    "pdf_extract_failed": ("PDF 文字提取失败", "本机文字提取未成功；可受控重试，但不会绕过文件安全检查。"),
    "read_failed": ("文件读取失败", "尚未取得可用的读取结果，需要检查原件或文件访问条件。"),
    "file_unavailable": ("原附件暂不可用", "没有可核验的原件，不能继续提取或声称恢复。"),
    "archive_path_risk": ("压缩包包含异常路径", "自动处理可能越过文件目录边界，已安全停止。"),
    "archive_entry_limit": ("压缩包内容数量超限", "安全上限阻止继续展开，不会自动放宽限制。"),
    "archive_expansion_risk": ("解压后内容可能过大", "存在资源耗尽风险，不会自动绕过容量限制。"),
    "file_too_large_for_extraction": ("附件超过安全提取大小", "现有提取器不能安全处理该大小的文件。"),
    "body_length_mismatch": ("邮件正文长度校验失败", "接口内容与长度校验不一致。无法证明正文完整，系统不会跳过校验入库。"),
    "model_chain_exhausted": ("所有模型都试过仍未成功", "小米、Kimi、Claude、GPT 依次失败，已在飞书汇报。原始消息和文件都保留，后台按原节奏重试；不会跳过检查把不合格的结果写进记忆。"),
    'model_request_uncertain': ('调用结果尚未确认，已阻止重发', '保留回复与调用台账；先找回已落盘回复或核对服务端回执。没有确定证据不重发此任务，其他新资料继续。'),
    'model_duplicate_exhausted': ('此任务已用完容灾尝试，已阻止重复调用', '先修复校验并复验已有回复；不会因调度、重启或换模型重新获得整套尝试。'),
    'model_duplicate_inflight': ('同一任务正在处理', '等待当前执行者完成，再复验并复用结果；不并发重复请求。'),
    'model_guard_cache_corrupt': ('已有回复证据校验失败', '保留现场并核对完整性，不以损坏为由重新付费请求。'),
    'model_guard_source_conflict': ('调用来源版本不一致', '核对来源归属、版本和调用契约后恢复，不复用不一致的结果。'),
    'model_guard_state_invalid': ('调用保护状态需要核查', '核查持久调用台账与文件，不清除历史尝试。'),
    'model_guard_route_changed': ('模型顺序已变，旧任务保留原尝试', '已有有效结果仍可复用；未完成尝试需要核对原路由后恢复。'),
    'attachment_analysis_cache_corrupt': ('附件分析缓存证据损坏', '保留缓存和原始资料，核对来源指纹、范围、分析规则和原始回复后恢复。'),
    'attachment_analysis_merge_capacity': ('附件分析已保存，等待汇总容量调整', '来源分析和精确引用已核验并完整保存；当前汇总输入容量不足。保留全部分析，不删项凑长度，也不重新调用附件分析模型。此状态不代表已写入业务记忆。'),
    'attachment_analysis_saved_reply_unavailable': ('已有回复不足以完成附件分析', '尚未找到完整且通过来源校验的回复；原文件、已有片段及调用历史保留。本次本地复验没有新增模型调用，不把部分结果标为完成。'),
    'model_validation_error': ('本地校验需要修复，回复已保留', '修复校验后优先复验已有回复，不因校验程序异常换模型重发。'),
    'model_context_changed': ('提交前上下文发生变化', '保留旧提案并重新判断当前业务；未变附件的来源分析仍可复用。'),
    "model_output_invalid": ("模型回复未通过内容或格式校验", "部分回复未通过校验；各模型的失败原因以当前任务调用回执为准。内容未写入记忆，优先复验已保存回复；严格证据校验与累计调用预算继续生效。"),
    "memory_core_unavailable": ("记忆库核心当前不可写", "同步写入受核心安全门禁阻止，需要先恢复索引和校验。"),
    "core_failure": ("记忆库索引或校验尚未通过", "核心完整性校验尚未通过，系统已停止写入。没有可安全自动执行的修复步骤，需先根据脱敏诊断核对索引和校验记录；不能跳过门禁。"),
    "media_pending": ("原始媒体尚未就绪", "文字已保留；系统无法生成缺失的原始媒体，按原计划继续探测。"),
    "waiting_power": ("等待接通电源", "后台任务按现有电源策略等待，不是新的同步失败。"),
    "waiting_win11_power": ("大邮件等待 Win11 接通电源", "普通邮件和不大的附件已照常处理；超过 10MB 的邮件由 Win11 读附件，Win11 接着电源时自动继续。"),
    "maintenance_active": ("正在维护", "维护期间禁止写入，退出维护后还需要复验。"),
    "email_items_failed": ("邮件仍有技术失败项", "应逐项查看失败依据；不能用附件人工处置代替邮件同步成功。"),
}


class WorkbenchError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def digest(value: Any) -> str:
    return hashlib.sha256(syncv2.canonical_json(value).encode()).hexdigest()


def initialize(connection: sqlite3.Connection) -> None:
    required={'operational_issues','operational_issue_groups','operational_issue_group_members',
              'issue_alert_links','issue_dispositions','operational_issue_events','operational_actions',
              'operational_workbench_health','operational_issue_object','operational_group_open','operational_group_member_issue'}
    present={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    if required <= present: return
    connection.executescript("""
      CREATE TABLE IF NOT EXISTS operational_issues(
        issue_id TEXT PRIMARY KEY, object_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
        source_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', attention_state TEXT NOT NULL,
        resolution_kind TEXT, observation_json TEXT NOT NULL, source_ref_json TEXT NOT NULL,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, resolved_at TEXT,
        revision INTEGER NOT NULL DEFAULT 1, evidence_json TEXT NOT NULL DEFAULT '{}'
      );
      CREATE INDEX IF NOT EXISTS operational_issue_object ON operational_issues(object_key,status);
      CREATE TABLE IF NOT EXISTS operational_issue_groups(
        group_id TEXT PRIMARY KEY, group_key TEXT NOT NULL, episode INTEGER NOT NULL,
        source_id TEXT NOT NULL, kind_family TEXT NOT NULL, error_code TEXT NOT NULL,
        title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
        attention_state TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        resolved_at TEXT, revision INTEGER NOT NULL DEFAULT 1,
        UNIQUE(group_key,episode)
      );
      CREATE INDEX IF NOT EXISTS operational_group_open
        ON operational_issue_groups(group_key,status);
      CREATE TABLE IF NOT EXISTS operational_issue_group_members(
        group_id TEXT NOT NULL REFERENCES operational_issue_groups(group_id),
        issue_id TEXT NOT NULL REFERENCES operational_issues(issue_id),
        joined_at TEXT NOT NULL,
        PRIMARY KEY(group_id,issue_id)
      );
      CREATE INDEX IF NOT EXISTS operational_group_member_issue
        ON operational_issue_group_members(issue_id,group_id);
      CREATE TABLE IF NOT EXISTS issue_alert_links(
        issue_id TEXT NOT NULL REFERENCES operational_issues(issue_id),
        alert_id TEXT NOT NULL, alert_snapshot_json TEXT NOT NULL,
        PRIMARY KEY(issue_id,alert_id)
      );
      CREATE TABLE IF NOT EXISTS issue_dispositions(
        object_key TEXT NOT NULL, fingerprint TEXT NOT NULL, issue_id TEXT NOT NULL,
        resolution_kind TEXT NOT NULL, action_id TEXT NOT NULL, reason TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(object_key,fingerprint)
      );
      CREATE TABLE IF NOT EXISTS operational_issue_events(
        event_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, event_type TEXT NOT NULL,
        detail_json TEXT NOT NULL, created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS operational_actions(
        action_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, action TEXT NOT NULL,
        request_hash TEXT NOT NULL, state TEXT NOT NULL, run_id TEXT,
        result_json TEXT NOT NULL DEFAULT '{}', worker_pid INTEGER,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS operational_workbench_health(
        scope TEXT PRIMARY KEY, available INTEGER NOT NULL, error_code TEXT,
        checked_at TEXT NOT NULL
      );
    """)


def _db(database: Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    return states._connect(database, readonly=readonly)


def _legacy_missing(exc: sqlite3.Error) -> bool:
    return "no such table" in str(exc)


def attachment_key(row: dict[str, Any]) -> str:
    return "attachment:" + digest([row["message_key"], row["attachment_id"]])


def attachment_fingerprint(row: dict[str, Any]) -> str:
    # No current status: a re-scan cannot revoke an explicit content-bound decision.
    return digest([row.get("sha256") or "", row.get("actual_size"), row.get("stored_name")])


def _claim_base_failures(config: dict[str, Any]) -> list[dict[str, Any]]:
    memory_root = Path(str(config.get("memory_root") or "")).expanduser()
    if memory_root.name != "00_业务记忆库" or not memory_root.is_dir():
        return []
    vault_root = memory_root.parent.resolve()
    failures: list[dict[str, Any]] = []
    for section in page_integrity.business_roots(vault_root):
        root = vault_root / section
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = page_integrity.read_text_exact(path)
            except (OSError, UnicodeError):
                continue
            if (
                page_integrity.CLAIM_BASE_SENTINEL not in text
                and page_integrity.CLAIM_START_RE.search(text) is None
            ):
                continue
            inspected = page_integrity.inspect_page(text)
            base_errors = [code for code in inspected.errors if code.startswith("claim_base_")]
            if not base_errors:
                continue
            error_code = base_errors[0]
            relative = path.relative_to(vault_root).as_posix()
            page_label = " / ".join(Path(relative).parts[-2:])
            object_key = "validation-page:" + digest(relative)
            fingerprint = digest(
                [
                    error_code,
                    relative,
                    inspected.stored_base_sha256,
                    inspected.actual_base_sha256,
                ]
            )
            failures.append(
                {
                    "object_key": object_key,
                    "fingerprint": fingerprint,
                    "relative_path": relative,
                    "page_label": page_label,
                    "error_code": error_code,
                    "stored_base_sha256": inspected.stored_base_sha256,
                    "actual_base_sha256": inspected.actual_base_sha256,
                }
            )
    return failures


def _full_validation_success_at() -> str | None:
    try:
        payload = json.loads(VALIDATION_SUCCESS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        isinstance(payload, dict)
        and payload.get("schema_version") == 2
        and payload.get("full_validation") is True
        and payload.get("verdict") == "pass"
    ):
        return str(payload.get("recorded_at") or "") or None
    return None


def dispositions(database: Path | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    connection = _db(database, readonly=True)
    try:
        return {(row["object_key"], row["fingerprint"]): dict(row)
                for row in connection.execute("SELECT * FROM issue_dispositions")}
    except sqlite3.OperationalError as exc:
        if _legacy_missing(exc):
            return {}
        raise
    finally:
        connection.close()


def pending_attachment_rows(connection: sqlite3.Connection, database: Path | None = None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM attachments WHERE status='needs_review' ORDER BY original_name,message_key,attachment_id")]
    import email_phases
    if email_phases.present(connection):
        existing={(r['message_key'],r['attachment_id']) for r in rows}
        for r in connection.execute("SELECT a.*,j.last_error_code AS phase_error FROM attachments a JOIN attachment_jobs j ON j.message_key=a.message_key AND j.attachment_id_hash=a.attachment_id WHERE j.state='needs_review'"):
            v=dict(r)
            if (v['message_key'],v['attachment_id']) not in existing:
                v.update(status='needs_review',extract_status=v['phase_error'] or v['extract_status']);rows.append(v)
    handled = dispositions(database)
    pending = [row for row in rows if (attachment_key(row), attachment_fingerprint(row)) not in handled]
    known_keys = {attachment_key(dict(r)) for r in connection.execute("SELECT message_key,attachment_id FROM attachments")}
    ledger = _db(database, readonly=True)
    try:
        for current in pending:
            current['video_waiting']=current.get('extract_status')=='waiting_instruction'
        for old in ledger.execute("SELECT * FROM operational_issues WHERE kind='attachment' AND status='open'"):
            if json.loads(old['observation_json']).get('video_waiting'):
                for current in pending:
                    if attachment_key(current)==old['object_key'] and attachment_fingerprint(current)==old['fingerprint']:
                        current['video_waiting']=True
            if old["object_key"] not in known_keys:
                ref = json.loads(old["source_ref_json"])
                pending.append({**ref, "original_name": "原附件记录暂不可用", "status": "needs_review",
                                "risk": "unknown", "extract_status": "file_unavailable", "last_error_code": "file_unavailable"})
    except sqlite3.OperationalError as exc:
        if not _legacy_missing(exc):
            raise
    finally:
        ledger.close()
    return pending


def _attachment_mail_status(row: dict[str, Any]) -> str:
    archive = {"archived": "已归档", "failed": "归档失败"}.get(row.get("archive_state"), "归档待确认")
    memory = {"applied": "已处理", "failed": "处理失败"}.get(row.get("memory_state"), "处理未完成")
    return f"邮件{archive}；记忆{memory}"


def _attachment_reading(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Project bounded receipt metadata; never expose names, paths or source text."""
    result = {"mail_status": _attachment_mail_status(row),
              "status": "partial_static", "status_label": "内容提取不完整",
              "receipt_verified": False, "text_chars": None, "reviewed_at": None,
              "reason_codes": [], "reason_labels": [],
              "summary": "附件台账标为部分提取；对应提取回执尚未核实，不能据此判断损坏或打不开。"}
    try:
        full = email_sync.win11_attachments.full_read_receipt(config, row)
        if full and full.get("coverage", {}).get("complete"):
            chars = full["text_chars"]
            return {**result, "status": "extracted", "status_label": "已完成读取",
                    "receipt_verified": True, "text_chars": chars,
                    "reviewed_at": full.get("completed_at"), "receipt_source": "Win11 完整读取回执",
                    "summary": f"已读取并保存 {chars:,} 字符；全文和读取范围已核对。图片文字为机器识别结果。"}
    except (KeyError, OSError, ValueError, TypeError, email_sync.win11_attachments.AttachmentError):
        pass
    summary=video_hold.archived_reading(row)
    if summary or row.get('extract_status') == 'waiting_instruction':
        summary=summary or {'videos':[],'non_video_complete':True,'receipt_verified':False}
        return {**result,'status':'waiting_instruction','status_label':'视频待命',
            'video_waiting':summary['non_video_complete'],'non_video_complete':summary['non_video_complete'],
            'receipt_verified':summary.get('receipt_verified',False),'text_chars':summary.get('text_chars'),
            'reviewed_at':summary.get('completed_at'),'receipt_source':'原始归档回执',
            'summary':('非视频内容已有读取回执；视频等待指令。' if summary['non_video_complete'] else '视频等待指令；另有非视频读取错误待处理。')}
    segments = [str(row.get(key) or "") for key in ("message_key", "attachment_id", "sha256")]
    if (not all(re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value) for value in segments[:2])
            or not re.fullmatch(r"[0-9a-f]{64}", segments[2])):
        return result
    labels = {"embedded_content_not_opened": "其中的嵌入内容未读取",
              "text_limit": "达到当时的文字提取上限",
              "nontext_member_preserved": "压缩包中的非文字文件已保留，未提取文字",
              "xml_declaration_not_parsed": "部分文档结构未解析"}
    try:
        root = email_sync.runtime_paths(config)["state_root"] / "attachment-static"
        target = root.joinpath(*segments)
        receipts = []
        for path in list(target.glob("*/receipt.json"))[:16]:
            if not re.fullmatch(r"[0-9a-f]{64}", path.parent.name):
                continue
            if any(p.is_symlink() for p in [path, path.parent, target, target.parent, target.parent.parent, root]):
                continue
            if path.stat().st_size > 16384:
                continue
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                continue
            if (receipt.get("schema_version") != 1 or receipt.get("source_sha256") != row["sha256"]
                    or receipt.get("parser_sha256") != path.parent.name
                    or receipt.get("status") != row.get("extract_status")
                    or receipt.get("active_content_executed") is not False):
                continue
            stamp = str(receipt.get("reviewed_at") or "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)", stamp):
                continue
            text_path = path.with_name("text.txt")
            if text_path.is_symlink() or text_path.stat().st_size > 8000000:
                continue
            text_bytes = text_path.read_bytes()
            if hashlib.sha256(text_bytes).hexdigest() != receipt.get("text_sha256"):
                continue
            chars = len(text_bytes.decode("utf-8"))
            if type(receipt.get("text_chars")) is not int or receipt["text_chars"] != chars:
                continue
            reasons = receipt.get("reasons")
            if not isinstance(reasons, list) or not all(isinstance(code, str) for code in reasons):
                continue
            receipts.append((stamp, chars, sorted(set(code for code in reasons if code in labels)),
                             any(code not in labels for code in reasons)))
        if receipts:
            stamp, chars, codes, other = max(receipts)
            descriptions = [labels[code] for code in codes]
            if other or not descriptions:
                descriptions.append("另有读取限制，需要核对")
            result.update(receipt_verified=True, text_chars=chars, reviewed_at=stamp,
                          reason_codes=codes, reason_labels=descriptions,
                          summary=f"已提取 {chars:,} 字符；" + "；".join(descriptions) + "。",
                          receipt_source="历史静态提取回执")
    except (KeyError, OSError, ValueError, TypeError, email_sync.EmailSyncError):
        pass
    return result


def _mail(config: dict[str, Any]) -> dict[str, Any]:
    connection = email_sync.connect_database_readonly(config)
    try:
        attachments = [dict(row) for row in connection.execute("""
          SELECT a.*,m.archive_path,m.archive_state,m.memory_state,m.manifest_sha256,m.updated_at AS message_updated_at
          FROM attachments a JOIN messages m ON m.message_key=a.message_key
          ORDER BY a.original_name,a.message_key,a.attachment_id
        """)]
        import email_phases
        phases={}
        if email_phases.present(connection):
            phases={(j['message_key'],j['attachment_id_hash']):dict(j) for j in connection.execute('SELECT * FROM attachment_jobs')}
        for attachment in attachments:
            phase=phases.get((attachment['message_key'],attachment['attachment_id']))
            if phase and phase['state']=='needs_review':
                attachment.update(status='needs_review',extract_status=phase['last_error_code'] or attachment['extract_status'])
            if phase and phase.get('result_path'):
                try:
                    result=email_phases.verified_result(config,phase)
                    attachment['reading']={'mail_status':_attachment_mail_status(attachment),
                        'status':'extracted' if result['complete'] else 'incomplete_content',
                        'receipt_verified':True,'text_chars':len(result['text']),'reviewed_at':result['created_at'],
                        'summary':'已有分阶段读取回执；补录状态独立核验。','receipt_source':'邮件分阶段补充回执'}
                    if result['kind']=='win11' and not result['complete']:
                        remote=json.loads((Path(phase['result_path']).parent/'win11.json').read_text())
                        summary=video_hold.coverage_summary(remote)
                        if summary and summary['non_video_complete']:
                            attachment['reading'].update(status='waiting_instruction',status_label='视频待命',video_waiting=True,non_video_complete=True,summary='非视频内容已有完整读取回执；视频按指令待命。')
                except (email_sync.EmailSyncError,ValueError,OSError):
                    attachment.update(status='needs_review',extract_status='phase_result_invalid')
            partial = attachment.get("extract_status") == "partial_static"
            if partial or attachment.get("status") == "needs_review":
                reading = _attachment_reading(config, attachment)
                if partial or reading.get("status") in {"extracted", "waiting_instruction"}:
                    attachment["reading"] = reading
            reading = attachment.get("reading") or {}
            if sync_rule_runtime.attachment_complete(reading):
                attachment.update(status="verified", extract_status="extracted", last_error_code=None)
        # Subject and archive path remain local and are excluded from all AI projections.
        messages = [dict(row) for row in connection.execute("""
          SELECT message_key,subject,received_at,archive_path,archive_state,memory_state,
                 last_error_code,archive_attempts,archive_failure_attempts,
                 archive_failure_class,archive_failure_fingerprint,
                 archive_validator_version,archive_next_retry_at,
                 archive_retry_exhausted_at,semantic_attempts,memory_applied_at,updated_at
          FROM messages
        """)]
        return {"attachments": attachments, "messages": messages}
    finally:
        connection.close()


def _event(connection: sqlite3.Connection, issue_id: str, kind: str, detail: dict[str, Any], stamp: str) -> None:
    connection.execute("INSERT INTO operational_issue_events VALUES(?,?,?,?,?)",
                       (str(uuid.uuid4()), issue_id, kind, json.dumps(detail, ensure_ascii=False), stamp))


def _finish(connection: sqlite3.Connection, issue_id: str, kind: str, evidence: dict[str, Any], stamp: str) -> None:
    changed = connection.execute("""
      UPDATE operational_issues SET status='closed',attention_state='closed',resolution_kind=?,
        resolved_at=?,evidence_json=?,revision=revision+1 WHERE issue_id=? AND status='open'
    """, (kind, stamp, json.dumps(evidence, ensure_ascii=False), issue_id)).rowcount
    if changed:
        _event(connection, issue_id, kind, evidence, stamp)


CHAT_PROGRESS_FIELDS = frozenset({
    "window_id", "status", "list_complete", "conversations_discovered",
    "conversations_complete", "conversations_incomplete", "attempt_count",
    "retry_round", "probe_count", "next_due", "error_code", "last_run_id",
    "window_start", "window_end", "updated_at",
})


def read_chat_checkpoint(config: dict[str, Any]) -> dict[str, Any]:
    """Read a bounded projection; missing legacy ledger is not a failure."""
    try:
        import chat_reconcile_checkpoint
    except ModuleNotFoundError as exc:
        if exc.name != "chat_reconcile_checkpoint":
            raise
        return {}
    progress = chat_reconcile_checkpoint.read_progress(config)
    return {key: value for key, value in progress.items() if key in CHAT_PROGRESS_FIELDS}


def chat_recovery_view(progress: dict[str, Any]) -> dict[str, Any]:
    phase = progress.get("status")
    if phase == "unavailable":
        return {"read_recovery": progress, "attention_state": "waiting",
                "current_reason": "补查进度暂时无法读取，等待下轮检查；原始失败保留",
                "recovery_boundary": "暂时读不到台账不代表恢复或损坏；保留原计数、失败与到期时间，下一轮重新核对。"}
    code = str(progress.get("error_code") or "chat_reconcile_incomplete")
    complete = int(progress.get("conversations_complete") or 0)
    discovered = int(progress.get("conversations_discovered") or 0)
    remaining = int(progress.get("conversations_incomplete") or 0)
    counts = (f"已完成 {complete}/{discovered} 个会话，剩余 {remaining} 个"
              if "conversations_discovered" in progress else "会话进度暂不可核验")
    if progress.get("list_complete") is False:
        counts += "；会话列表仍未完整"
    if phase == "complete":
        reason, attention = "固定窗口补查已完成，等待整条同步流程完整验收", "automatic"
    elif phase == "manual_required":
        reason = REASONS.get(code, ("补查台账、来源权限或内容冲突需要核对", ""))[0]
        attention = "needs_user"
    else:
        reason = REASONS.get(code, ("来源读取暂不可用，后台自动恢复中", ""))[0]
        reason += "；每 6 小时探测" if phase == "probing" else "；按台账到期时间续跑"
        attention = "automatic"
    return {"read_recovery": progress, "current_reason": reason + "；" + counts,
            "current_blocker": code if phase != "complete" else "controller_acceptance_pending",
            "next_due": progress.get("next_due"), "attention_state": attention,
            "impact": "固定补查窗口与已落库实时消息分别处理；补查未完整不代表已落库消息丢失。",
            "recovery_boundary": "保留窗口、已完成会话与失败历史；仅续跑未完成会话。已落库消息继续受治理处理；补查完成后仍需整条同步流程完整验收。"}


def _observation(source: dict[str, Any], code: str, impact: str, boundary: str) -> dict[str, Any]:
    return {"error_code": code, "impact": impact, "recovery_boundary": boundary,
            "source_status": source.get("status", "unknown"),
            "current_blocker": source.get("error_code"), "attempt_count": int(source.get("attempt_count") or 0),
            "probe_count": int(source.get("probe_count") or 0),
            "last_attempt": source.get("last_attempt"), "last_success": source.get("last_success"),
            "next_due": source.get("next_due"), "last_run_id": source.get("last_run_id")}


def pending_media_ids() -> list[str] | None:
    """Freeze a media disposition to the current recordings, never the whole source."""
    try:
        state = json.loads(MINUTES_STATE_PATH.read_text(encoding="utf-8"))
        entries = state["entries"]
        if not isinstance(entries, dict) or not entries or not all(isinstance(row, dict) for row in entries.values()):
            return None
        return sorted(key for key, row in entries.items() if not row.get("video_waiting_instruction") and ("media" in (row.get("missing_artifacts") or []) or not row.get("media_filename")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _upsert(connection: sqlite3.Connection, item: dict[str, Any], links: list[dict[str, Any]], stamp: str) -> str:
    issue_id = item["issue_id"]
    old = connection.execute("SELECT * FROM operational_issues WHERE issue_id=?", (issue_id,)).fetchone()
    observation = json.dumps(item["observation"], ensure_ascii=False, sort_keys=True)
    reference = json.dumps(item.get("source_ref") or {}, ensure_ascii=False, sort_keys=True)
    if old is None:
        connection.execute("""
          INSERT INTO operational_issues(issue_id,object_key,fingerprint,source_id,kind,title,
             attention_state,observation_json,source_ref_json,first_seen,last_seen)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (issue_id, item["object_key"], item["fingerprint"], item["source_id"], item["kind"],
              item["title"], item["attention_state"], observation, reference, item.get("first_seen") or stamp, stamp))
        _event(connection, issue_id, "detected", {"reason": item["observation"]["error_code"]}, stamp)
    elif old["status"] == "open":
        changed = (old["observation_json"] != observation or old["attention_state"] != item["attention_state"]
                   or old["title"] != item["title"])
        refresh = changed or old['source_ref_json'] != reference or not states.parse_time(old['last_seen']) or (states.parse_time(stamp)-states.parse_time(old['last_seen'])).total_seconds() >= 60
        if refresh: connection.execute("""
          UPDATE operational_issues SET title=?,attention_state=?,observation_json=?,source_ref_json=?,
            last_seen=?,revision=revision+? WHERE issue_id=?
        """, (item["title"], item["attention_state"], observation, reference, stamp, int(changed), issue_id))
    _link(connection, issue_id, links)
    return issue_id


def _link(connection: sqlite3.Connection, issue_id: str, links: list[dict[str, Any]]) -> None:
    for alert in links:
        safe = {key: alert.get(key) for key in ("alert_id", "source_id", "category", "severity", "title",
                                               "detail", "status", "first_seen", "resolved_at")}
        connection.execute("""
          INSERT INTO issue_alert_links VALUES(?,?,?) ON CONFLICT(issue_id,alert_id)
          DO UPDATE SET alert_snapshot_json=excluded.alert_snapshot_json
          WHERE issue_alert_links.alert_snapshot_json != excluded.alert_snapshot_json
        """, (issue_id, alert["alert_id"], json.dumps(safe, ensure_ascii=False)))


def _new_item(kind: str, key: str, fingerprint: str, source: str, title: str,
              observation: dict[str, Any], reference: dict[str, Any], attention: str = "needs_user") -> dict[str, Any]:
    return {"issue_id": "issue-" + digest([key, fingerprint])[:24], "object_key": key,
            "fingerprint": fingerprint, "source_id": source, "kind": kind, "title": title,
            "observation": observation, "source_ref": reference, "attention_state": attention}


def _group_key(row: sqlite3.Row) -> str:
    observation = json.loads(row["observation_json"])
    return ":".join((str(row["source_id"]), str(row["kind"]), str(observation.get("error_code") or "unknown")))


def _group_attention(rows: list[sqlite3.Row]) -> str:
    priority = {"needs_user": 3, "automatic": 2, "waiting": 1, "closed": 0}
    return max((str(row["attention_state"]) for row in rows), key=lambda value: priority.get(value, -1))


def _reconcile_groups(connection: sqlite3.Connection, stamp: str) -> None:
    active = [row for row in connection.execute(
        "SELECT * FROM operational_issues WHERE status='open' ORDER BY first_seen,issue_id"
    )]
    by_key: dict[str, list[sqlite3.Row]] = {}
    for row in active:
        by_key.setdefault(_group_key(row), []).append(row)
    touched: set[str] = set()
    for key, members in by_key.items():
        group = connection.execute(
            "SELECT * FROM operational_issue_groups WHERE group_key=? AND status='open'",
            (key,),
        ).fetchone()
        if group is None:
            episode = int(connection.execute(
                "SELECT COALESCE(MAX(episode),0)+1 FROM operational_issue_groups WHERE group_key=?",
                (key,),
            ).fetchone()[0])
            group_id = "group-" + digest([key, episode])[:24]
            observation = json.loads(members[0]["observation_json"])
            connection.execute(
                """
                INSERT INTO operational_issue_groups(
                  group_id,group_key,episode,source_id,kind_family,error_code,title,
                  attention_state,first_seen,last_seen
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    group_id, key, episode, members[0]["source_id"], members[0]["kind"],
                    str(observation.get("error_code") or "unknown"), members[0]["title"],
                    _group_attention(members), min(str(row["first_seen"]) for row in members),
                    max(str(row["last_seen"]) for row in members),
                ),
            )
            group = connection.execute(
                "SELECT * FROM operational_issue_groups WHERE group_id=?", (group_id,)
            ).fetchone()
        group_id = str(group["group_id"])
        touched.add(group_id)
        before = {
            str(row[0]) for row in connection.execute(
                "SELECT issue_id FROM operational_issue_group_members WHERE group_id=?", (group_id,)
            )
        }
        for member in members:
            if str(member["issue_id"]) in before: continue
            connection.execute(
                "INSERT OR IGNORE INTO operational_issue_group_members(group_id,issue_id,joined_at) VALUES(?,?,?)",
                (group_id, member["issue_id"], stamp),
            )
        after = before | {str(member["issue_id"]) for member in members}
        attention = _group_attention(members)
        title = str(members[0]["title"])
        last_seen = max(str(row["last_seen"]) for row in members)
        changed = before != after or attention != group["attention_state"] or title != group["title"]
        if changed or last_seen != group['last_seen']: connection.execute(
            """
            UPDATE operational_issue_groups SET title=?,attention_state=?,last_seen=?,
              revision=revision+? WHERE group_id=?
            """,
            (title, attention, last_seen, int(changed), group_id),
        )
    for group in connection.execute(
        "SELECT * FROM operational_issue_groups WHERE status='open'"
    ).fetchall():
        if str(group["group_id"]) in touched:
            continue
        open_members = connection.execute(
            """
            SELECT i.* FROM operational_issue_group_members gm
            JOIN operational_issues i ON i.issue_id=gm.issue_id
            WHERE gm.group_id=? AND i.status='open'
            """,
            (group["group_id"],),
        ).fetchall()
        # A source issue keeps its id while its error code changes in place.
        # Such a member still being open does not keep this episode alive: the
        # group only stands for members whose current code is still its own.
        if any(_group_key(row) == str(group["group_key"]) for row in open_members):
            continue
        connection.execute(
            """
            UPDATE operational_issue_groups SET status='closed',attention_state='closed',
              resolved_at=?,last_seen=?,revision=revision+1 WHERE group_id=?
            """,
            (stamp, stamp, group["group_id"]),
        )
        for row in open_members:
            _event(connection, str(row["issue_id"]), "group_superseded", {
                "group_id": str(group["group_id"]),
                "from_error_code": str(group["error_code"]),
                "to_error_code": str(json.loads(row["observation_json"]).get("error_code") or "unknown"),
            }, stamp)


def _repair_closed_body_group_evidence(
    connection: sqlite3.Connection,
    message_map: dict[str, dict[str, Any]],
) -> None:
    """Freeze legacy body groups to their own archive-stage evidence.

    Before failure-stage tracking existed, an open mail issue could be updated
    in place with a later memory error.  Its durable body-length group
    membership is the authority for the historical root cause.  Repair each
    affected member once; later recurrence attempts must not rewrite the old
    episode's count.
    """
    repaired_groups: set[str] = set()
    for row in connection.execute(
        """
        SELECT g.group_id,i.issue_id,i.title,i.observation_json,i.source_ref_json
        FROM operational_issue_groups g
        JOIN operational_issue_group_members gm ON gm.group_id=g.group_id
        JOIN operational_issues i ON i.issue_id=gm.issue_id
        WHERE g.status='closed' AND g.error_code='body_length_mismatch' AND i.status<>'open'
        """
    ).fetchall():
        observation = json.loads(row["observation_json"])
        if (
            observation.get("error_code") == "body_length_mismatch"
            and observation.get("failure_stage") == "archive"
            and "archive_attempt_count" in observation
        ):
            continue
        reference = json.loads(row["source_ref_json"])
        current = message_map.get(str(reference.get("message_key") or ""), {})
        archive_attempts = int(
            current.get("archive_attempts")
            if current.get("archive_attempts") is not None
            else observation.get("archive_attempt_count")
            or observation.get("item_attempt_count")
            or 0
        )
        observation.update({
            "error_code": "body_length_mismatch",
            "failure_stage": "archive",
            "archive_attempt_count": archive_attempts,
            "item_attempt_count": archive_attempts,
            "item_attempt_detail": "归档尝试累计；历史记录未逐次区分人工和自动。",
            "recovery_boundary": REASONS["body_length_mismatch"][1],
        })
        connection.execute(
            """
            UPDATE operational_issues SET title=?,observation_json=?,revision=revision+1
            WHERE issue_id=?
            """,
            (
                REASONS["body_length_mismatch"][0],
                json.dumps(observation, ensure_ascii=False, sort_keys=True),
                row["issue_id"],
            ),
        )
        repaired_groups.add(str(row["group_id"]))
    for group_id in repaired_groups:
        connection.execute(
            "UPDATE operational_issue_groups SET revision=revision+1 WHERE group_id=?",
            (group_id,),
        )


def _repair_premature_mail_recoveries(connection: sqlite3.Connection,
                                    message_map: dict[str, dict[str, Any]], stamp: str) -> None:
    """Retract only the old archive-only receipt for a generic mail issue."""
    for old in connection.execute(
        "SELECT * FROM operational_issues WHERE kind='mail_message' AND status='closed' "
        "AND resolution_kind='recovered'"
    ).fetchall():
        obs = json.loads(old['observation_json'])
        evidence = json.loads(old['evidence_json'])
        current = message_map.get(json.loads(old['source_ref_json']).get('message_key'))
        if (not current or obs.get('failure_stage') != 'archive'
                or obs.get('error_code') == 'body_length_mismatch'
                or evidence.get('reason') != '该邮件已通过正文归档校验；下游记忆接收另行验收。'):
            continue
        applied_at = states.parse_time(current.get('memory_applied_at'))
        closed_at = states.parse_time(old['resolved_at'])
        if applied_at and closed_at and applied_at <= closed_at:
            continue
        newer = connection.execute(
            "SELECT 1 FROM operational_issues WHERE object_key=? AND issue_id<>? AND first_seen>? LIMIT 1",
            (old['object_key'], old['issue_id'], old['first_seen']),
        ).fetchone()
        if newer:
            continue
        connection.execute(
            "UPDATE operational_issues SET status='open',attention_state='waiting',resolution_kind=NULL,"
            "resolved_at=NULL,evidence_json='{}',revision=revision+1 WHERE issue_id=?",
            (old['issue_id'],),
        )
        connection.execute(
            "UPDATE operational_issue_groups SET status='open',attention_state='waiting',resolved_at=NULL,"
            "revision=revision+1 WHERE status='closed' AND group_id IN "
            "(SELECT group_id FROM operational_issue_group_members WHERE issue_id=?)",
            (old['issue_id'],),
        )
        _event(connection, old['issue_id'], 'recovery_retracted', {
            'reason': '旧回执仅证明归档完成，未证明记忆接收；恢复逐项核验。',
            'previous_resolved_at': old['resolved_at'],
        }, stamp)


def _lock_path(database: Path | None = None) -> Path:
    return Path(database or syncv2.DATABASE).with_suffix(".workbench.lock")


def reconcile(config: dict[str, Any], *, database: Path | None = None, at: Any = None, _lock_held: bool = False) -> dict[str, Any]:
    if _lock_held:
        return _reconcile(config, database=database, at=at)
    with syncv2.file_lock(_lock_path(database), blocking=True):
        return _reconcile(config, database=database, at=at)


def _reconcile(config: dict[str, Any], *, database: Path | None = None, at: Any = None) -> dict[str, Any]:
    """Prepare external observations without a write lock; commit ledger-only phases."""
    stamp = states.now_iso(at)
    connection = _db(database)
    try:
        # Keep the existing additive schema setup outside retried transactions.
        initialize(connection)
    finally:
        connection.close()
    import media_actions
    mail_state_root = (config.get('email_sync') or {}).get('state_root')
    mail_snapshot = media_actions.snapshot_signatures(list(Path(mail_state_root).glob('*.sqlite*'))) if mail_state_root else {}
    mail_available, mail = True, {"attachments": [], "messages": []}
    try:
        mail = _mail(config)
    except (OSError, KeyError, sqlite3.Error, email_sync.EmailSyncError):
        mail_available = False
    failed_messages = [row for row in mail["messages"] if row["archive_state"] == "failed"
                       or (row["archive_state"] == "archived" and row["memory_state"] != "applied"
                           and row["last_error_code"])]
    claim_base_failures = _claim_base_failures(config)
    active_claim_base = {row["object_key"]: row["fingerprint"] for row in claim_base_failures}
    validation_success_at = _full_validation_success_at()
    prepared_media_ids = pending_media_ids()
    listener_progress = chat_read_recovery.listener_view(config)
    read_progress = read_chat_checkpoint(config)
    try:
        import mail_auto_recovery
        mail_recovery = mail_auto_recovery.workbench_view(config)
    except (OSError, KeyError, ValueError, sqlite3.Error):
        mail_recovery = {"phase": "unavailable", "enabled": False}
    import ledger_recovery
    ledger_progress = ledger_recovery.summary(Path(database or states.DATABASE))
    recovery_view = ledger_recovery.read(Path(__file__).resolve().parents[1]/'state/supervisor-recovery-status.json')
    if recovery_view and recovery_view.get('phase')!='verified':
        ledger_progress = {**recovery_view, **ledger_progress, 'requires_user': bool(recovery_view.get('requires_user') or ledger_progress.get('requires_user'))}
        ledger_progress.pop('code_binding',None)
    import index_runtime_recovery
    index_progress = index_runtime_recovery.read(Path(__file__).resolve().parents[1]/'state/index-recovery-status.json')
    if index_progress:
        ledger_progress = {**ledger_progress, **{k:v for k,v in index_progress.items() if k not in {'cycles','code_binding','failure_snapshot'}}}
    wait_views = {}
    import validation_retry
    for source in states.sources(database):
        if source.get("error_code") == "validation_inputs_changed":
            try:
                view = validation_retry.wait_view(Path(__file__).resolve().parents[1], source,
                                                 at or states.now_dt(), database=database)
            except (OSError, ValueError, RuntimeError):
                view = {"phase": "waiting_stable", "next_retry_at": source.get("next_due")}
            wait_views[source["source_id"]] = (source.get("last_run_id"), view)
    message_map = {r["message_key"]: r for r in mail["messages"]}
    for row in mail['attachments']:
        summary=video_hold.archived_reading(row)
        if summary:video_hold.register_attachment(row,summary)
    held_rows=video_hold.rows()
    # Hash large saved audio outside the shared database write transaction.
    minutes_proofs = {}
    for held in held_rows:
        if held.get('source_id') != 'dingtalk_minutes' or held.get('kind') != 'unknown_media':
            continue
        prior_connection = _db(database, readonly=True)
        try:
            prior = prior_connection.execute("SELECT evidence_json FROM operational_issues WHERE object_key=? AND fingerprint=? AND status='closed'",
                                             ('video:'+held['hold_id'], held['version'])).fetchone()
        finally:
            prior_connection.close()
        previous = json.loads(prior['evidence_json']) if prior else {}
        proof = previous if previous.get('method') == 'minutes_source_media_verified' and _minutes_hold_current(held, previous) else _minutes_hold_proof(held)
        if proof:
            minutes_proofs[held['hold_id']] = proof

    def reconcile_items(connection):
        import minutes_recovery
        minutes_recovery.refresh_workbench(connection, stamp, _new_item, _upsert, _finish, MINUTES_STATE_PATH)
        # Refresh all shared ledger state after BEGIN IMMEDIATE on each attempt.
        raw_alerts = [dict(row) for row in connection.execute("SELECT * FROM sync_alerts")]
        source_rows = {row["source_id"]: dict(row) for row in connection.execute("SELECT * FROM supervisor_sources")}
        open_alerts = [row for row in raw_alerts if row["status"] == "open"]
        handled = {(row["object_key"], row["fingerprint"]): dict(row)
                   for row in connection.execute("SELECT * FROM issue_dispositions")}
        mail_source = source_rows.get("dingtalk_mail", {})
        pending_attachments = [row for row in mail["attachments"] if row["status"] == "needs_review"
                               and (attachment_key(row), attachment_fingerprint(row)) not in handled]
        active_mail_issues: dict[str, str] = {}
        connection.execute("INSERT OR REPLACE INTO operational_workbench_health VALUES(?,?,?,?)",
                           ("mail", int(mail_available), None if mail_available else "mail_state_unavailable", stamp))
        for row in pending_attachments:
            code = row["last_error_code"] or row["extract_status"] or "unknown"
            reason, boundary = REASONS.get(code, ("附件需要进一步核对", "现有记录尚不足以确认附件可安全解析。"))
            obs = _observation(mail_source, code, "此附件尚未完成安全解析；邮件的其他故障单独保留。", boundary)
            obs.update({"risk": row["risk"], "extract_status": row["extract_status"],
                        "automatic_attempts_known": False, "reason_label": reason,
                        "current_blocker": None})
            if row.get("reading"):
                obs["reading"] = row["reading"]
                obs["impact"] = row["reading"]["mail_status"] + "；附件内容尚未完整读取。"
            if row.get('reading',{}).get('video_waiting'):
                obs.update(video_waiting=True,severity='info',next_due=None,last_attempt=None,last_success=None,
                    impact=row['reading']['mail_status']+'；视频等待指令，其他内容继续同步。',
                    recovery_boundary='当前按指令等待；视频未解析，不能标记为完整读取。')
            item = _new_item("attachment", attachment_key(row), attachment_fingerprint(row), "dingtalk_mail",
                             reason, obs, {"message_key": row["message_key"], "attachment_id": row["attachment_id"]},
                             attention="waiting" if obs.get("video_waiting") else ("needs_user" if email_sync.mail_auto_recovery.category(code)=="manual" else "automatic"))
            _upsert(connection, item, [a for a in open_alerts if a["source_id"] == "dingtalk_mail"
                                      and a["category"] == "manual_attention"], stamp)
        for row in failed_messages:
            code = row["last_error_code"] or "unknown"
            reason, boundary = REASONS.get(code, ("邮件处理未完成", "尚未定位到可自动修复的原因；不会跳过完整性校验。"))
            key = "mail:" + digest(row["message_key"])
            failure_stage = "archive" if row["archive_state"] == "failed" else "memory"
            # Recurrent failures create a new episode only after a confirmed closure.
            previous = connection.execute("SELECT * FROM operational_issues WHERE object_key=? ORDER BY first_seen DESC LIMIT 1", (key,)).fetchone()
            previous_observation = json.loads(previous["observation_json"]) if previous else {}
            same_open_failure = bool(
                previous and previous["status"] == "open"
                and previous_observation.get("error_code") == code
                and previous_observation.get("failure_stage", "archive") == failure_stage
            )
            fingerprint = previous["fingerprint"] if same_open_failure else digest([failure_stage, code, row["updated_at"]])
            obs = _observation(mail_source, code, "这封邮件尚未完成归档或记忆接收，不能算同步成功。", boundary)
            obs["failure_stage"] = failure_stage
            obs["archive_attempt_count"] = int(row["archive_attempts"] or 0)
            obs["semantic_attempt_count"] = int(row["semantic_attempts"] or 0)
            obs["item_attempt_count"] = (
                obs["archive_attempt_count"] if failure_stage == "archive"
                else obs["semantic_attempt_count"]
            )
            obs["item_attempt_detail"] = "当前失败阶段的尝试累计；历史记录未逐次区分人工和自动。"
            obs["failure_class"] = row.get("archive_failure_class")
            obs["failure_attempt_count"] = int(row.get("archive_failure_attempts") or 0)
            obs["validator_version"] = row.get("archive_validator_version")
            obs["next_retry_at"] = row.get("archive_next_retry_at")
            obs["retry_exhausted_at"] = row.get("archive_retry_exhausted_at")
            frozen = (bool(row.get("archive_retry_exhausted_at")) and row.get("archive_failure_class") != "transient") or row.get("archive_failure_class") == "deterministic"
            frozen = frozen or (row.get("archive_failure_class") == "transient" and email_sync.archive_failure_class(code) != "transient")
            if frozen:
                obs["retry_state"] = "frozen"
                obs["recovery_boundary"] = "同一输入和程序版本下已停止无效重试；只有来源、DWS、校验版本或对应修复条件变化后才重新检查。"
            elif row.get("archive_next_retry_at"):
                obs["retry_state"] = "scheduled"
                if row.get("archive_retry_exhausted_at"):
                    obs["recovery_boundary"] = "短间隔重试已结束；每 6 小时继续检查一次，保留累计次数和失败记录。"
            attention = 'needs_user' if mail_auto_recovery.category(code) == 'manual' else 'automatic'
            source_code = str(mail_source.get("error_code") or "")
            source_status = mail_source.get("status")
            if source_code.startswith('read_safety') or source_status == 'manual_required':
                attention = 'needs_user'
            if not frozen and not source_code.startswith("read_safety"):
                if source_code in {
                    "memory_core_unavailable", "memory_core_unverified", "memory_core_busy",
                    "validation_inputs_changed", "maintenance_active", "waiting_power",
                    "control_lock_unavailable", "already_running",
                } or source_status in {"waiting_power", "waiting_idle"}:
                    attention = "waiting"
                elif source_status in {"running", "syncing", "retrying"}:
                    attention = "automatic"
                elif (source_status == "persistent_failure" and failure_stage == "archive"
                      and row.get("archive_failure_class") == "transient"
                      and row.get("archive_next_retry_at") and mail_source.get("next_due")):
                    attention = "waiting"
            if mail_recovery.get('enabled'):
                obs['automatic_recovery'] = mail_recovery
                if mail_recovery.get('phase') in {'pending','repairing','waiting'} and mail_auto_recovery.category(code) in {'transient','probe_required','input_changed','waiting'} and not source_code.startswith('read_safety'):
                    attention = 'waiting' if mail_recovery['phase']=='waiting' else 'automatic'
                    obs['retry_state'] = mail_recovery['phase']
                    obs['recovery_boundary'] = '后台按当前来源与输入版本自动恢复；仅逐项成功回执能够结束问题。'
            item = _new_item("mail_message", key, fingerprint, "dingtalk_mail", reason, obs,
                             {"message_key": row["message_key"]}, attention)
            item["first_seen"] = row["updated_at"] or stamp
            active_mail_issues[key] = _upsert(
                connection,
                item,
                [a for a in open_alerts if a["source_id"] == "dingtalk_mail"
                 and a["category"] in {"sync_failure", "sync_block", "sync_blocked"}],
                stamp,
            )
        claim_error_codes = {
            str(failure["error_code"]) for failure in claim_base_failures
        }
        memory_source = source_rows.get("memory_core", {})
        for failure in claim_base_failures:
            error_code = str(failure["error_code"])
            reason, boundary = REASONS.get(
                error_code,
                ("业务页面校验基线失效", "必须先恢复页面完整性并取得完整成功回执。"),
            )
            observation = _observation(
                memory_source,
                error_code,
                "该业务页面当前不能作为客户事实或派生记忆的可信来源。",
                boundary,
            )
            observation.update(
                {
                    "reason_label": reason,
                    "page_handle": digest(failure["relative_path"])[:20],
                    "stored_base_sha256_prefix": str(failure["stored_base_sha256"] or "")[:12],
                    "actual_base_sha256_prefix": str(failure["actual_base_sha256"] or "")[:12],
                    "item_attempt_count": 0,
                    "item_attempt_detail": "确定性完整性失败不通过重复校验解决。",
                }
            )
            item = _new_item(
                "validation_page",
                failure["object_key"],
                failure["fingerprint"],
                "memory_core",
                reason,
                observation,
                {
                    "relative_path": failure["relative_path"],
                    "page_label": failure["page_label"],
                },
                "needs_user",
            )
            claim_alerts = [
                alert
                for alert in open_alerts
                if alert["source_id"] == "memory_core"
                and alert.get("error_code") == error_code
            ]
            _upsert(connection, item, claim_alerts, stamp)
        groups: dict[str, list[dict[str, Any]]] = {}
        for alert in open_alerts:
            if mail_available and alert["source_id"] == "dingtalk_mail":
                if alert["category"] == "manual_attention":
                    continue
                if failed_messages and alert["category"] in {"sync_failure", "sync_block", "sync_blocked"}:
                    continue
            if (
                claim_base_failures
                and alert["source_id"] == "memory_core"
                and alert.get("error_code") in claim_error_codes
            ):
                continue
            family = "source_health" if alert["category"] in {"sync_failure", "sync_block", "sync_blocked", "persistent_block"} else alert["category"]
            groups.setdefault(alert["source_id"] + ":" + family, []).append(alert)
        for key, members in groups.items():
            source_id = members[0]["source_id"]
            source = source_rows.get(source_id, {})
            primary = sorted(members, key=lambda r: (r["severity"] == "error", r["last_seen"]), reverse=True)[0]
            placeholders = ",".join("?" for _ in members)
            previous = connection.execute(
                "SELECT i.* FROM operational_issues i JOIN issue_alert_links l ON l.issue_id=i.issue_id "
                f"WHERE i.kind='source' AND i.status='open' AND l.alert_id IN ({placeholders}) LIMIT 1",
                [m["alert_id"] for m in members]).fetchone()
            fingerprint = previous["fingerprint"] if previous else digest(sorted(m["alert_id"] for m in members))
            category = primary["category"]
            code = "media_pending" if category == "waiting_source" else str(primary.get("error_code") or source.get("error_code") or category)
            reason, boundary = REASONS.get(code, (primary["title"], "尚未定位到可自动修复的根因。请查看最近失败步骤和脱敏诊断。"))
            obs = _observation(source, code, primary["detail"], boundary)
            if code == "validation_inputs_changed":
                cached = wait_views.get(source_id)
                if cached and cached[0] == source.get("last_run_id"):
                    obs["validation_wait"] = cached[1]
                else:
                    obs["validation_wait"] = {"phase": "waiting_stable", "next_retry_at": source.get("next_due")}
            obs.update({"category": category, "severity": primary["severity"],
                        "original_failure": primary["detail"], "original_failure_at": primary["first_seen"]})
            if source_id == "dingtalk_minutes" and code == "media_pending":
                media_ids = prepared_media_ids
                if media_ids is not None:
                    obs["pending_media_ids"] = media_ids
                    obs["impact"] = f"{len(media_ids)} 条听记仍缺原始媒体；已有文字保留，缺少的文字或媒体等待来源，按原计划继续探测。"
                    prior = previous or connection.execute(
                        "SELECT i.* FROM operational_issues i JOIN issue_alert_links l ON l.issue_id=i.issue_id "
                        f"WHERE i.kind='source' AND l.alert_id IN ({placeholders}) ORDER BY i.last_seen DESC,i.rowid DESC LIMIT 1",
                        [m["alert_id"] for m in members]).fetchone()
                    prior_scope = json.loads(prior["observation_json"]).get("pending_media_ids") if prior else None
                    if prior and prior_scope == media_ids:
                        fingerprint = prior["fingerprint"]
                    elif prior_scope is not None and prior_scope != media_ids:
                        if prior["status"] == "open":
                            _finish(connection, prior["issue_id"], "superseded", {"reason": "待补媒体集合已变化，旧处置不适用于其他听记。"}, stamp)
                        fingerprint = digest([sorted(m["alert_id"] for m in members), media_ids])
            attention = "needs_user"
            if code == "supervisor_runtime_error_pending" and ledger_progress:
                blockers={'writes_pending':'正在按计划重试受阻的数据库写入。','stable_observation':'数据库写入已恢复，正在完成十分钟稳定观察。',
                          'source_busy':'同步仍在运行，结束后自动核验。','index_busy':'等待索引访问锁，释放后自动核验。',
                          'validation_pending':'等待新版程序和当前资料的完整校验。','validation_failed':'完整校验尚未通过，系统正在按原流程处理。',
                          'validation_changed':'资料已更新，等待与最新资料匹配的完整校验。','evidence_changed':'运行证据发生变化，下一周期重新核验。',
                          'operation_receipt_pending':'其余历史异常已核验恢复；等待剩余原失败步骤的实际成功回执。'}
                reason=blockers.get(ledger_progress.get('check_blocker'),'数据库争用正在自动恢复，系统会核验实际结果后闭环。')
                if ledger_progress.get('missing_paths')==['mark_delivery']:
                    reason+=' 通知结果登记尚缺实际成功回执；下次正常通知发生后会自动核对，无需重复发送。'
                obs.update(current_reason=reason,ledger_recovery=ledger_progress,automatic_attempts=ledger_progress.get('automatic_attempts',0),
                           last_attempt=ledger_progress.get('last_attempt'),last_success=ledger_progress.get('last_success'),
                           next_due=ledger_progress.get('next_retry_at') or ledger_progress.get('next_check_at'),recovery_boundary='系统自动重试并核验；只有原失败路径与当前完整校验通过才闭环。')
                if index_progress:
                    from index_runtime_recovery import workbench_view
                    visible = workbench_view(index_progress)
                    reason = visible.pop('current_reason')
                    obs.update(visible)
                    obs['current_reason'] = reason
                    attention = 'waiting' if index_progress.get('technical_investigation_required') else 'automatic'
                else:
                    attention = 'needs_user' if ledger_progress.get('requires_user') else 'automatic' 
            elif category == "supervisor_health":
                import supervisor_health_recovery
                progress=supervisor_health_recovery.view(primary, Path(__file__).resolve().parents[1])
                attention = 'needs_user' if progress.get('requires_user') else 'automatic'
                obs.update(supervisor_recovery=progress,current_reason=progress['reason_label'],
                           next_due=progress['next_check_at'],repair_phase=progress['phase'])
            elif category == "listener_failure" and listener_progress:
                obs.update(listener_progress)
                attention = "waiting"
            elif category == "recovery_conflict":
                attention = "waiting"
                obs["repair_phase"] = "investigating"
            elif category in {"sync_failure", "sync_block", "sync_blocked", "persistent_block"} and source.get("status") == "persistent_failure":
                attention = "automatic"
                obs["repair_phase"] = "scheduled" if source.get("next_due") else "investigating"
            elif primary.get("repair_status") in {"queued", "running"}:
                attention = "automatic"
            elif category == "waiting_source" or source.get("error_code") in {"memory_core_unavailable", "maintenance_active"} or source.get("status") in {"deferred", "waiting_power", "waiting_source"}:
                attention = "waiting"
            elif source.get("status") in {"retrying", "running", "syncing", "pending"} or (source.get("status") == "persistent_failure" and chat_read_recovery.scheduled_read_recovery(str(source.get("error_code") or ""))):
                attention = "automatic"
            if source_id == "dingtalk_chat" and read_progress:
                obs["read_recovery"] = read_progress
                prior_obs = json.loads(previous["observation_json"]) if previous else {}
                # Keep original incident/group identity even as the current code changes.
                for field in ("error_code", "original_failure", "original_failure_at"):
                    if field in prior_obs:
                        obs[field] = prior_obs[field]
                current_code = str(source.get("error_code") or "")
                read_owned = (not current_code or chat_read_recovery.scheduled_read_recovery(current_code)
                              or current_code.startswith(("chat_read_", "chat_checkpoint_", "chat_reconcile_checkpoint_")))
                if category in {"sync_failure", "sync_block", "sync_blocked", "persistent_block"} and read_owned:
                    view = chat_recovery_view(read_progress)
                    attention = view.pop("attention_state")
                    obs.update(view)
                # Listener and conflict issues retain their own current reasons/actions.
            if source_id == 'dingtalk_mail' and mail_recovery.get('enabled'):
                obs['automatic_recovery'] = mail_recovery
                phase = mail_recovery.get('phase')
                if phase in {'pending','repairing','waiting'} and not str(source.get('error_code') or '').startswith('read_safety'):
                    attention = 'waiting' if phase == 'waiting' else 'automatic'
                    obs['recovery_boundary'] = '后台按逐项证据自动恢复；匹配当前版本的成功回执齐备后才结束。'
                elif phase == 'needs_maintenance':
                    attention = 'waiting'
                    obs['recovery_boundary'] = '自动恢复遇到尚未支持的故障，已保留诊断等待维护；没有需要用户代选的业务事项。'
            item = _new_item("source", "source:" + key, fingerprint, source_id, primary["title"], obs, {}, attention)
            item["first_seen"] = min(m["first_seen"] for m in members)
            _upsert(connection, item, members, stamp)
        # Absence is not recovery. Each type requires positive, same-object evidence.
        attachment_map = {(r["message_key"], r["attachment_id"]): r for r in mail["attachments"]}
        message_map = {r["message_key"]: r for r in mail["messages"]}
        raw_map = {r["alert_id"]: r for r in raw_alerts}
        if mail_available:
            _repair_premature_mail_recoveries(connection, message_map, stamp)
        for old in connection.execute("SELECT * FROM operational_issues WHERE status='open'").fetchall():
            ref = json.loads(old["source_ref_json"])
            disposition = handled.get((old["object_key"], old["fingerprint"]))
            if disposition:
                _finish(connection, old["issue_id"], disposition["resolution_kind"],
                        {"action_id": disposition["action_id"], "reason": disposition["reason"]}, stamp)
            elif old["kind"] == "attachment" and mail_available:
                current = attachment_map.get((ref.get("message_key"), ref.get("attachment_id")))
                if current and attachment_fingerprint(current) != old["fingerprint"]:
                    _finish(connection, old["issue_id"], "superseded", {"reason": "附件内容已经变化，旧处置不适用于新内容。"}, stamp)
                elif current and current["status"] in {"verified", "completed"} and current["extract_status"] in {"extracted", "extracted_static", "no_text", "image_understood"}:
                    _finish(connection, old["issue_id"], "recovered", {"reason": "同一附件已有匹配来源、哈希与覆盖范围的成功校验结果。", "reading": current.get("reading", {})}, stamp)
            elif old["kind"] == "mail_message" and mail_available:
                current = message_map.get(ref.get("message_key"))
                old_observation = json.loads(old["observation_json"])
                old_stage = old_observation.get("failure_stage")
                if old_stage is None:
                    # V1 mail issues predate explicit stage tracking. Body-length
                    # failures were archive failures. An older runtime may already
                    # have refreshed the issue text to a later memory error, so the
                    # durable group membership is the migration authority.
                    legacy_archive_group = connection.execute(
                        """
                        SELECT 1 FROM operational_issue_group_members gm
                        JOIN operational_issue_groups g ON g.group_id=gm.group_id
                        WHERE gm.issue_id=? AND g.error_code='body_length_mismatch'
                        LIMIT 1
                        """,
                        (old["issue_id"],),
                    ).fetchone()
                    old_stage = "archive" if (
                        old_observation.get("error_code") == "body_length_mismatch"
                        or legacy_archive_group
                    ) else "memory"
                archive_only = old_observation.get('error_code') == 'body_length_mismatch'
                if old_observation.get('failure_stage') is None:
                    archive_only = archive_only or bool(legacy_archive_group)
                if (current and old_stage == "archive" and archive_only and current["archive_state"] == "archived"
                        and str(current["updated_at"]) > old["first_seen"]):
                    _finish(connection, old["issue_id"], "recovered", {
                        "reason": "该邮件已通过正文归档校验；下游记忆接收另行验收。",
                        "observed_at": current["updated_at"],
                    }, stamp)
                elif (current and old_stage in {"archive", "memory"}
                      and active_mail_issues.get(old["object_key"])
                      and active_mail_issues[old["object_key"]] != old["issue_id"]):
                    _finish(connection, old["issue_id"], "superseded", {
                        "reason": "该邮件当前失败证据已变化；旧问题保留，并由当前根因批次继续跟踪。",
                        "observed_at": current["updated_at"],
                    }, stamp)
                elif (current and old_stage == "archive" and not archive_only
                      and current['archive_state'] == 'archived'
                      and current['memory_state'] == 'applied' and current.get('memory_applied_at')
                      and not current['last_error_code'] and str(current['updated_at']) > old['first_seen']):
                    _finish(connection, old['issue_id'], 'recovered', {
                        'reason': '该邮件已完成归档和记忆接收。',
                        'observed_at': current['updated_at'],
                        'memory_applied_at': current['memory_applied_at'],
                    }, stamp)
                elif (current and old_stage == 'archive' and not archive_only
                      and current['archive_state'] == 'archived' and current['memory_state'] != 'applied'
                      and not current['last_error_code']):
                    pending_obs = {**old_observation, 'current_stage': 'memory',
                                   'current_blocker': 'email_memory_pending',
                                   'impact': '正文和附件已归档，正在等待记忆接收回执。'}
                    running = connection.execute(
                        "SELECT 1 FROM action_runs WHERE action_type='dingtalk_mail' AND status='running' LIMIT 1"
                    ).fetchone()
                    attention = 'automatic' if running else 'waiting'
                    if str(mail_source.get('error_code') or '').startswith('read_safety'):
                        attention = 'needs_user'
                    item = _new_item(old['kind'], old['object_key'], old['fingerprint'], old['source_id'],
                                     old['title'], pending_obs, ref, attention)
                    _upsert(connection, item, [], stamp)
                elif (current and old_stage == "memory" and current["archive_state"] == "archived"
                      and current["memory_state"] == "applied" and not current["last_error_code"]
                      and str(current["updated_at"]) > old["first_seen"]):
                    _finish(connection, old["issue_id"], "recovered", {
                        "reason": "该邮件已完成记忆接收。", "observed_at": current["updated_at"],
                    }, stamp)
            elif old["kind"] == "validation_page":
                current_fingerprint = active_claim_base.get(old["object_key"])
                if current_fingerprint and current_fingerprint != old["fingerprint"]:
                    _finish(
                        connection,
                        old["issue_id"],
                        "superseded",
                        {"reason": "该页面的完整性指纹已变化，旧问题保留并由新批次继续跟踪。"},
                        stamp,
                    )
                elif (
                    not current_fingerprint
                    and validation_success_at
                    and states.parse_time(validation_success_at)
                    and states.parse_time(old["first_seen"])
                    and states.parse_time(validation_success_at) > states.parse_time(old["first_seen"])
                ):
                    _finish(
                        connection,
                        old["issue_id"],
                        "recovered",
                        {
                            "reason": "该页面已恢复完整性，并由后续全量校验通过确认。",
                            "observed_at": validation_success_at,
                        },
                        stamp,
                    )
            elif old["kind"] == "source":
                links = [dict(row) for row in connection.execute("SELECT * FROM issue_alert_links WHERE issue_id=?", (old["issue_id"],))]
                if (mail_available and old["source_id"] == "dingtalk_mail"
                        and json.loads(old["observation_json"]).get("category") == "manual_attention" and pending_attachments):
                    _finish(connection, old["issue_id"], "superseded", {"reason": "原始清单已恢复读取，改为逐项处理，未宣称故障恢复。"}, stamp)
                    continue
                if any(raw_map.get(link["alert_id"], {}).get("status") == "open" for link in links):
                    continue
                recoveries = [r for r in raw_alerts if r["category"] == "recovery" and r["resolved_at"]
                              and r["resolved_at"] >= old["first_seen"]
                              and any(r["dedupe_key"].endswith(":" + link["alert_id"]) for link in links)]
                if recoveries:
                    _finish(connection, old["issue_id"], "recovered",
                            {"reason": "对应告警已取得后台恢复回执。", "observed_at": max(r["resolved_at"] for r in recoveries)}, stamp)

        # Same Sync Center issues, no new notifier/channel. No prompt or source names.
        import model_call_ledger, model_router
        guard_data = model_call_ledger.report(model_router.STATE_DIR / 'model-calls', operations_only=True)
        codes = {'validation_error':'model_validation_error', 'uncertain':'model_request_uncertain', 'exhausted':'model_duplicate_exhausted',
                 'cache_corrupt':'model_guard_cache_corrupt'}
        for operation in guard_data['operations']:
            key = 'model-call:' + operation['operation_id']
            if operation['status'] in codes:
                code = codes[operation['status']]
                title, boundary = REASONS[code]
                purpose = operation['purpose']
                source = ('dingtalk_mail' if purpose.startswith('email_') else 'win11' if purpose.startswith('win11_')
                          else 'dingtalk_minutes' if purpose == 'minutes_semantic' else 'dingtalk')
                obs = {'error_code':code, 'severity':'warning', 'current_blocker':code,
                       'impact':'只隔离对应模型任务；其他新内容继续。', 'recovery_boundary':boundary,
                       'reused_results':operation['cache_hits'], 'purpose':purpose, 'next_due':None}
                _upsert(connection, _new_item('model_call',key,operation['operation_id'],source,
                    title,obs,{'operation_id':operation['operation_id']},attention='waiting'),[],stamp)
            elif operation['status'] == 'validated':
                for old in connection.execute("SELECT issue_id FROM operational_issues WHERE object_key=? AND status='open'",(key,)).fetchall():
                    _finish(connection,old['issue_id'],'recovered',{'reason':'模型回复已通过当前校验；业务提交另按原流程验收。','reused_results':operation['cache_hits']},stamp)

        import media_classification
        classifications=media_classification.records()
        for held in held_rows:
            proof = minutes_proofs.get(held['hold_id'])
            if proof and _minutes_hold_current(held, proof):
                for old in connection.execute("SELECT issue_id FROM operational_issues WHERE object_key=? AND fingerprint=? AND status='open'",
                                              ('video:'+held['hold_id'], held['version'])).fetchall():
                    _finish(connection, old['issue_id'], 'source_media_type_verified', proof, stamp)
                continue
            held={**held,'kind':media_classification.effective(held,classifications)}
            held_ref=json.loads(held['object_ref'])
            # Each media has its own durable issue, including embedded mail media.
            code='video_waiting_instruction' if held['kind']=='video' else 'media_type_waiting_confirmation'
            obs={'error_code':code,'video_waiting':True,'severity':'info','current_blocker':None,
                 'impact':'其他内容继续按各自回执同步；此媒体等待指令。',
                 'recovery_boundary':REASONS[code][1],'hold_id':held['hold_id'],'next_due':None}
            classification=classifications.get(held['hold_id'],{})
            routing=json.loads(classification.get('evidence_json','{}')) if classification.get('version')==held['version'] else {}
            attention='waiting'
            if held['kind']!='video':
                phase=routing.get('repair_phase','processing' if held['kind']!='unknown_media' else 'scheduled')
                attention='waiting' if phase=='waiting_source' else 'automatic'
                obs.update(video_waiting=False,reason_code=routing.get('reason_code','media_type_unknown' if held['kind']=='unknown_media' else 'media_processing'),
                    repair_phase=phase,current_blocker=routing.get('reason_code'),
                    impact=routing.get('reason','系统继续核验和处理，无需你选择。'),
                    recovery_boundary='系统逐项核验原件、读取覆盖和业务回执；技术问题继续调查。',
                    attempt_count=classification.get('attempts',0),no_progress_count=routing.get('no_progress_count',0),
                    next_due=datetime.fromtimestamp(classification['next_attempt']).astimezone().isoformat(timespec='seconds') if classification.get('next_attempt') else None,
                    evidence_ref=routing.get('evidence_ref'))
            _upsert(connection,_new_item('video_hold','video:'+held['hold_id'],held['version'],held['source_id'],
                REASONS[code][0],obs,{'hold_id':held['hold_id']}, attention=attention),[],stamp)
        waiting=[r for r in pending_attachments if r.get('reading',{}).get('video_waiting')]
        pending_attachments=[r for r in pending_attachments if not r.get('reading',{}).get('video_waiting')]
        unresolved = connection.execute("SELECT COUNT(*) FROM operational_issues WHERE kind='attachment' AND status='open' AND COALESCE(json_extract(observation_json,'$.video_waiting'),0)=0").fetchone()[0]
        if waiting and not pending_attachments and not unresolved:
            connection.execute("UPDATE sync_alerts SET title='视频待命',detail='非视频内容按回执处理；视频等待指令。',acknowledged_at=COALESCE(acknowledged_at,?),next_reminder=NULL WHERE source_id='dingtalk_mail' AND category='manual_attention' AND status='open'",(stamp,))
        if media_actions.snapshot_signatures([Path(p) for p in mail_snapshot]) != mail_snapshot:
            raise media_actions.SnapshotChanged('mail_snapshot_changed')
        return pending_attachments, unresolved, waiting

    try:
        pending_attachments, unresolved_attachments, waiting_attachments = states._write(database, "workbench_items", reconcile_items)
    except ledger_recovery.Deferred:
        import workbench_read_recovery
        pending_attachments, unresolved_attachments, waiting_attachments = workbench_read_recovery.retry_projection(
            Path(__file__).resolve().parents[1],Path(database or states.DATABASE),reconcile_items)

    # These phases are independently committed. Never replay already committed
    # item updates or alert occurrences if a later phase fails.
    if mail_available and (pending_attachments or unresolved_attachments):
        manual = states.open_alert("dingtalk_mail", "manual_attention", "邮件附件仍有待核验项",
                                   f"{max(len(pending_attachments), unresolved_attachments)} 项附件读取待核验；邮件归档和记忆处理结果单独显示，请查看逐项原因。",
                                   database=database, at=at)
        def link_manual(connection):
            for pending in pending_attachments:
                issue = connection.execute("SELECT issue_id FROM operational_issues WHERE object_key=? AND fingerprint=? AND status='open'",
                                           (attachment_key(pending), attachment_fingerprint(pending))).fetchone()
                if issue:
                    _link(connection, issue["issue_id"], [manual])
        states._write(database, "workbench_manual_links", link_manual)
    elif mail_available and not waiting_attachments:
        states.resolve_alert("dingtalk_mail", "manual_attention", title="钉钉邮件人工项已结束",
                             detail="附件已逐项恢复或明确处置；不代表其他邮件技术故障恢复。",
                             database=database, at=at)
    import media_actions
    with closing(_db(database, readonly=True)) as media_reader:
        media_snapshot=media_actions.prepare(media_reader,config,mail=mail)
        media_snapshot['signatures'].update(mail_snapshot)
    def reconcile_groups(connection):
        media_actions.reconcile(connection,config,stamp,prepared=media_snapshot)
        _reconcile_groups(connection, stamp)
        _repair_closed_body_group_evidence(connection, message_map)
    states._write(database, "workbench_groups", reconcile_groups)
    return {"available": mail_available, "checked_at": stamp}


def raw_issue(issue_id: str, database: Path | None = None) -> dict[str, Any]:
    if not ISSUE_RE.fullmatch(issue_id):
        raise WorkbenchError("issue_invalid", "问题编号无效。")
    connection = _db(database, readonly=True)
    try:
        row = connection.execute("SELECT * FROM operational_issues WHERE issue_id=?", (issue_id,)).fetchone()
        if row is None:
            raise WorkbenchError("issue_missing", "问题记录暂不可用；不会因此清除告警。", 404)
        return dict(row)
    finally:
        connection.close()


def raw_group(group_id: str, database: Path | None = None) -> dict[str, Any]:
    if not GROUP_RE.fullmatch(group_id):
        raise WorkbenchError("group_invalid", "问题组编号无效。")
    connection = _db(database, readonly=True)
    try:
        row = connection.execute(
            "SELECT * FROM operational_issue_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        if row is None:
            raise WorkbenchError("group_missing", "问题组暂不可用；单项证据仍保留。", 404)
        return dict(row)
    finally:
        connection.close()


def group_member_rows(group_id: str, database: Path | None = None) -> list[dict[str, Any]]:
    raw_group(group_id, database)
    connection = _db(database, readonly=True)
    try:
        return [dict(row) for row in connection.execute(
            """
            SELECT i.* FROM operational_issues i
            JOIN operational_issue_group_members gm ON gm.issue_id=i.issue_id
            WHERE gm.group_id=? ORDER BY i.first_seen,i.issue_id
            """,
            (group_id,),
        )]
    finally:
        connection.close()


def links_for(issue_id: str, database: Path | None = None) -> list[dict[str, Any]]:
    connection = _db(database, readonly=True)
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM issue_alert_links WHERE issue_id=?", (issue_id,))]
    finally:
        connection.close()


def alert_links(database: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    connection = _db(database, readonly=True)
    try:
        result: dict[str, list[dict[str, Any]]] = {}
        for row in connection.execute("""
          SELECT l.alert_id,i.issue_id,i.title,i.status,i.attention_state,i.resolution_kind,i.resolved_at,
            (SELECT gm.group_id FROM operational_issue_group_members gm
             JOIN operational_issue_groups g ON g.group_id=gm.group_id
             JOIN operational_issues m ON m.issue_id=gm.issue_id WHERE gm.issue_id=i.issue_id
             ORDER BY g.status='open' DESC,
               g.error_code=COALESCE(json_extract(m.observation_json,'$.error_code'),'unknown') DESC,
               g.last_seen DESC,g.episode DESC LIMIT 1) AS group_id
          FROM issue_alert_links l JOIN operational_issues i ON i.issue_id=l.issue_id ORDER BY i.first_seen
        """):
            result.setdefault(row["alert_id"], []).append({key: row[key] for key in row.keys() if key != "alert_id"})
        for recovery in connection.execute("SELECT alert_id,dedupe_key FROM sync_alerts WHERE category='recovery'"):
            prior_id = recovery["dedupe_key"].rsplit(":", 1)[-1]
            if prior_id in result:
                result[recovery["alert_id"]] = result[prior_id]
        return result
    except sqlite3.OperationalError as exc:
        if _legacy_missing(exc):
            return {}
        raise
    finally:
        connection.close()


def _object(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
    if row["kind"] not in {"attachment", "mail_message"}:
        return None
    ref = json.loads(row["source_ref_json"])
    mail = _mail(config)
    if row["kind"] == "attachment":
        return next((r for r in mail["attachments"] if r["message_key"] == ref["message_key"]
                     and r["attachment_id"] == ref["attachment_id"]), None)
    return next((r for r in mail["messages"] if r["message_key"] == ref["message_key"]), None)


def _media_snapshot() -> dict[str, Any]:
    """One read of the media tables, shared across every row of one listing."""
    import media_actions, media_classification
    return {"holds": video_hold.rows(), "classifications": media_classification.records(),
            "decisions": media_actions.decisions()}


def _public(row: dict[str, Any], object_row: dict[str, Any] | None = None,
            snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    # snapshot (see _media_snapshot) lets a caller building many items share one
    # read of the hold, classification and decision tables. Without it each item
    # reopened all three databases and rescanned them, which dominated listing
    # time once those tables grew. Omitting it keeps the original per-call reads,
    # so single-item callers are unaffected.
    obs = json.loads(row["observation_json"])
    reference = json.loads(row["source_ref_json"])
    label = (
        (object_row or {}).get("original_name")
        or (object_row or {}).get("subject")
        or (reference.get("page_label") if row["kind"] == "validation_page" else None)
    )
    if row["kind"] == "attachment" and object_row and attachment_fingerprint(object_row) != row["fingerprint"]:
        label = "原附件内容已变化；此处保留旧问题与处置记录"
    elif row["kind"] == "attachment" and object_row and object_row.get("reading"):
        obs["reading"] = {**object_row["reading"], "mail_status":
                          object_row["reading"].get("mail_status") or _attachment_mail_status(object_row)}
        complete = object_row["reading"].get("status") == "extracted" and object_row["reading"].get("receipt_verified")
        obs["impact"] = obs["reading"]["mail_status"] + ("；附件已完成读取。" if complete else "；附件内容尚未完整读取。")
    held_items=[]
    if row['kind'] in {'attachment','video_hold'}:
        for held in (video_hold.rows() if snapshot is None else snapshot["holds"]):
            ref=json.loads(held['object_ref'])
            if (row['kind']=='video_hold' and held['hold_id']==reference.get('hold_id')) or (
                row['kind']=='attachment' and held['source_id']=='dingtalk_mail' and
                ref.get('message_key')==reference.get('message_key') and ref.get('attachment_id')==reference.get('attachment_id')):
                held_items.append(held)
        if row['kind']=='video_hold' and held_items:label=held_items[0]['name']
        elif row['kind']=='attachment' and held_items and not label:label=held_items[0]['parent_label']
    complete_read = row["status"] == "closed" and obs.get("reading", {}).get("status") == "extracted" and obs.get("reading", {}).get("receipt_verified")
    value = {"issue_id": row["issue_id"], "kind": row["kind"], "source_id": row["source_id"],
            "source_label": "同步中心后台检查程序" if obs.get("category")=="supervisor_health" else SOURCE_NAMES.get(row["source_id"], row["source_id"]),
            "held_media":[video_hold.public_item(h) for h in held_items],
            "video_waiting":obs.get('video_waiting',False),
            "title": ('媒体类型待确认' if held_items and all(h['kind']=='unknown_media' for h in held_items) else '视频待命') if obs.get('video_waiting') else "附件已完成读取" if complete_read else REASONS["partial_static"][0] if obs.get("error_code") == "partial_static" else row["title"],
            "object_label": str(label or ("原附件" if row["kind"] == "attachment" else "原邮件" if row["kind"] == "mail_message" else ""))[:240],
            "status": row["status"], "attention_state": row["attention_state"],
            "attention_label": "等待指令" if obs.get("video_waiting") else LABELS[row["attention_state"]], "resolution_kind": row["resolution_kind"],
            "severity": obs.get("severity") or ("warning" if row["kind"] == "attachment" else "error"),
            "impact": obs.get("impact"), "reason": "按你的指令等待，普通重试不会下载或解析视频。" if obs.get("video_waiting") else "已完成读取，原件与全文回执已核对。" if complete_read else obs.get("current_reason") or REASONS.get(obs["error_code"], (row["title"], ""))[0],
            "recovery_boundary": obs.get("recovery_boundary"), "first_seen": row["first_seen"],
            "last_seen": row["last_seen"], "resolved_at": row["resolved_at"], "revision": row["revision"],
            "observation": obs, "evidence": json.loads(row["evidence_json"])}
    if obs.get("read_recovery"):
        value["read_recovery"] = obs["read_recovery"]

    import media_actions
    if row['kind'] == 'video_hold' and row['status'] == 'closed' and row.get('resolution_kind') == 'source_media_type_verified':
        proof = json.loads(row.get('evidence_json') or '{}')
        value.update(attention_state='closed', attention_label='已结束', video_waiting=False,
                     title='录音来源已恢复' if proof.get('media_kind') == 'audio' else '媒体类型已核验为视频',
                     reason='已保存的音频与当前来源校验一致；核对和业务记忆分别验收。' if proof.get('media_kind') == 'audio' else '类型未明事项已结束；对应视频继续按现行限制待命。',
                     recovery_boundary='仅结束来源类型问题，保留原登记和历史，不代替核对或业务处理回执。')
        return value
    return media_actions.decorate(value,row,held_items,
                                  (snapshot or {}).get("classifications"),
                                  (snapshot or {}).get("decisions"))


def _public_group(group: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    active = [item for item in members if item["status"] == "open"]
    affected = active if group["status"] == "open" else members
    affected = affected or members
    severity = "error" if any(item.get("severity") == "error" for item in affected) else "warning"
    total_attempts = sum(
        int((item.get("observation") or {}).get("item_attempt_count") or 0)
        for item in affected if item.get("kind") == "mail_message"
    )
    common_impact = next((str(item.get("impact") or "") for item in affected if item.get("impact")), "")
    count = len(affected)
    current_reason = affected[0].get("reason") if affected and all((x.get("observation") or {}).get("current_reason") for x in affected) else None
    current_boundary = affected[0].get("recovery_boundary") if current_reason else None
    video_waiting=bool(affected) and all(x.get('video_waiting') for x in affected)
    completed_read_group = group["status"] == "closed" and bool(affected) and all(
        (item.get("observation") or {}).get("reading", {}).get("status") == "extracted"
        and (item.get("observation") or {}).get("reading", {}).get("receipt_verified") for item in affected)
    if group["kind_family"] == "attachment":
        total_attempts = None  # Legacy attachment receipts never recorded a complete attempt count.
        common_impact = "附件读取完整度与邮件归档、记忆处理分别核对。"
        if group["error_code"] == "partial_static":
            processed = sum((item.get("observation") or {}).get("reading", {}).get("mail_status")
                            == "邮件已归档；记忆已处理" for item in affected)
            common_impact = (f"本组 {count} 项附件均已有完整读取回执。" if completed_read_group
                             else f"{count} 项附件内容未完整读取，其中 {processed} 项所属邮件已归档、记忆已处理。")
    value = {
        "group_id": group["group_id"],
        "issue_id": group["group_id"],
        "kind": "group",
        "member_kind": group["kind_family"],
        "source_id": group["source_id"],
        "source_label": SOURCE_NAMES.get(group["source_id"], group["source_id"]),
        "video_waiting":video_waiting,
        "held_media":[h for x in affected for h in x.get('held_media',[])],
        "title": "视频待命" if video_waiting else "附件已完成读取" if completed_read_group else REASONS["partial_static"][0] if group["error_code"] == "partial_static" else group["title"],
        "object_label": f"影响 {count} 项" if count > 1 else (affected[0].get("object_label") if affected else ""),
        "status": group["status"],
        "attention_state": group["attention_state"],
        "attention_label": "等待指令" if video_waiting else LABELS[group["attention_state"]],
        "resolution_kind": "group_closed" if group["status"] == "closed" else None,
        "severity": "info" if video_waiting else severity,
        "impact": common_impact if completed_read_group else f"同一根因影响 {count} 项；每项证据和操作仍独立保留。" if count > 1 else common_impact,
        "reason": "按你的指令等待；其他内容继续同步。" if video_waiting else "本组附件均已完成读取，逐项回执如下。" if completed_read_group else current_reason or REASONS.get(group["error_code"], (group["title"], ""))[0],
        "reading_complete": completed_read_group,
        **({"read_recovery": affected[0]["read_recovery"]} if affected and affected[0].get("read_recovery") else {}),
        "recovery_boundary": "视频等待指令；普通重试不会下载或解析，不把等待标记为完整读取。" if video_waiting else current_boundary or REASONS.get(group["error_code"], (group["title"], ""))[1],
        "error_code": group["error_code"],
        "affected_count": count,
        "member_issue_ids": [item["issue_id"] for item in members],
        "active_member_issue_ids": [item["issue_id"] for item in active],
        "total_attempts": total_attempts,
        "first_seen": group["first_seen"],
        "last_seen": group["last_seen"],
        "resolved_at": group["resolved_at"],
        "revision": group["revision"],
    }

    import media_actions
    return media_actions.decorate_group(value,affected)


def listing(config: dict[str, Any], *, scope: str = "all", limit: int = 200, offset: int = 0,
            include_business: bool = True, issue_ids: set[str] | None = None) -> dict[str, Any]:
    if scope not in (*SCOPES, "all"):
        raise WorkbenchError("scope_invalid", "问题筛选无效。")
    connection = _db(readonly=True)
    try:
        rows = [dict(row) for row in connection.execute("SELECT * FROM operational_issues ORDER BY first_seen,issue_id")]
        health = [dict(row) for row in connection.execute("SELECT * FROM operational_workbench_health")]
        try:
            group_rows = [dict(row) for row in connection.execute(
                "SELECT * FROM operational_issue_groups ORDER BY first_seen,group_id"
            )]
            memberships: dict[str, list[str]] = {}
            for member in connection.execute(
                "SELECT group_id,issue_id FROM operational_issue_group_members ORDER BY joined_at,issue_id"
            ):
                memberships.setdefault(str(member["group_id"]), []).append(str(member["issue_id"]))
        except sqlite3.OperationalError as exc:
            if not _legacy_missing(exc):
                raise
            group_rows, memberships = [], {}
    finally:
        connection.close()
    objects: dict[str, dict[str, Any]] = {}
    try:
        mail = _mail(config)
        objects.update({attachment_key(r): r for r in mail["attachments"]})
        objects.update({"mail:" + digest(r["message_key"]): r for r in mail["messages"]})
    except (KeyError, OSError, sqlite3.Error, email_sync.EmailSyncError):
        health = [*health, {"scope": "mail", "available": 0, "error_code": "mail_state_unavailable", "checked_at": syncv2.now_iso()}]
    media = _media_snapshot()
    items = [_public(row, objects.get(row["object_key"]), media) for row in rows]
    item_map = {item["issue_id"]: item for item in items}
    groups = [
        _public_group(group, [item_map[issue_id] for issue_id in memberships.get(group["group_id"], []) if issue_id in item_map])
        for group in group_rows
        if any(issue_id in item_map for issue_id in memberships.get(group["group_id"], []))
    ]
    if not groups:
        # Read-only compatibility before startup has applied the additive group migration.
        groups = [
            {**item, "group_id": item["issue_id"], "affected_count": 1,
             "member_issue_ids": [item["issue_id"]], "active_member_issue_ids": [item["issue_id"]] if item["status"] == "open" else [],
             "total_attempts": int((item.get("observation") or {}).get("item_attempt_count") or 0)}
            for item in items
        ]
    if include_business:
        import claim_reconciliation as reconciliation
        for case_scope, target_scope in (("needs_user", "needs_user"), ("automatic", "automatic"), ("archived_questions", "closed")):
            for case in reconciliation.list_cases(case_scope)["items"]:
                business = {"issue_id": "business-" + case["case_id"], "case_id": case["case_id"], "kind": "business",
                              "title": case.get("display_title") or case.get("title") or case.get("subject") or "业务事项",
                              "object_label": "", "source_label": "业务事项", "status": "closed" if target_scope == "closed" else "open",
                              "attention_state": target_scope, "attention_label": LABELS[target_scope],
                              "severity": "warning", "impact": case.get("decision_question") or case.get("assessment") or "",
                              "last_seen": case.get("updated_at")}
                items.append(business)
                groups.append({**business, "group_id": business["issue_id"], "affected_count": 1,
                               "member_issue_ids": [business["issue_id"]], "active_member_issue_ids": [business["issue_id"]] if business["status"] == "open" else [],
                               "total_attempts": 0})
    counts = {name: sum(item["attention_state"] == name for item in items) for name in SCOPES}
    group_counts = {name: sum(item["attention_state"] == name for item in groups) for name in SCOPES}
    selected = [item for item in items if scope == "all" or item["attention_state"] == scope]
    selected_groups = [item for item in groups if scope == "all" or item["attention_state"] == scope]
    if issue_ids is not None:
        if len(issue_ids) > 1000 or any(not ISSUE_RE.fullmatch(value) for value in issue_ids):
            raise WorkbenchError("issue_filter_invalid", "关联问题筛选无效。")
        selected = [item for item in selected if item["issue_id"] in issue_ids]
        selected_groups = [
            item for item in selected_groups
            if issue_ids.intersection(item.get("member_issue_ids") or [])
        ]
    selected.sort(key=lambda r: (r["status"] == "closed", r.get("severity") != "error", r.get("first_seen") or r.get("last_seen") or "", r["issue_id"]))
    selected_groups.sort(key=lambda r: (r["status"] == "closed", r.get("severity") != "error", r.get("first_seen") or r.get("last_seen") or "", r["issue_id"]))
    limit, offset = max(1, min(500, limit)), max(0, offset)
    import codex_handoff
    codex_handoff.annotate_items(selected[offset:offset + limit] + selected_groups[offset:offset + limit])
    return {"schema_version": 2, "generated_at": syncv2.now_iso(), "counts": counts, "total": len(selected),
            "items": selected[offset:offset + limit], "offset": offset, "has_more": len(selected) > offset + limit,
            "next_offset": offset + limit, "health": health,
            "groups": selected_groups[offset:offset + limit], "group_counts": group_counts,
            "group_total": len(selected_groups),
            "affected_total": sum(int(item.get("affected_count") or 1) for item in selected_groups),
            "group_has_more": len(selected_groups) > offset + limit,
            "group_next_offset": offset + limit}


def _attempts(row: dict[str, Any]) -> list[dict[str, Any]]:
    connection = _db(readonly=True)
    try:
        # Source-bound runs since this incident. Never apply source counts to a file.
        if row["kind"] in {"attachment","video_hold"}:
            return []
        runs = connection.execute("""
          SELECT run_id,status,trigger,error_code,started_at,completed_at FROM action_runs
          WHERE action_type=? AND (started_at>=? OR run_id=?) ORDER BY started_at DESC LIMIT 20
        """, (row["source_id"], row["first_seen"], json.loads(row["observation_json"]).get("last_run_id"))).fetchall()
        result = []
        for run in runs:
            item = dict(run)
            item["steps"] = [dict(s) for s in connection.execute(
                "SELECT label,status,error_code,started_at,completed_at FROM action_steps WHERE run_id=? ORDER BY step_order", (run["run_id"],))]
            result.append(item)
        return result
    finally:
        connection.close()


def _linked_live_alerts(issue_id: str) -> list[dict[str, Any]]:
    with _db(readonly=True) as connection:
        return [dict(r) for r in connection.execute(
            "SELECT a.* FROM sync_alerts a JOIN issue_alert_links l ON l.alert_id=a.alert_id WHERE l.issue_id=?", (issue_id,))]


def _actions(row: dict[str, Any], current: dict[str, Any], control: dict[str, Any] | None, maintenance: bool) -> list[dict[str, Any]]:
    def action(name: str, label: str, why: str, enabled: bool = True, confirm: bool = False) -> dict[str, Any]:
        return {"id": name, "label": label, "reason": "维护期间只可查看，暂不执行。" if maintenance else why,
                "available": enabled and not maintenance, "requires_confirmation": confirm}
    actions = [action("recheck", "重新核验此问题", "只核对对应对象和真实回执，不把检查完成当恢复。"),
               action("diagnostics", "查看脱敏诊断", "可复制给维护者；不会发送邮件正文、附件内容或凭据。")]
    if current.get('video_waiting'):
        reading = json.loads(row['observation_json']).get('reading') or {}
        if (row['kind'] == 'attachment' and row['status'] == 'open'
                and current.get('fingerprint_current') and current.get('object_available')
                and reading.get('non_video_complete') and reading.get('receipt_verified')):
            actions.append(action('dispose', '忽略当前内嵌视频',
                                  '只结束此内容版本的视频提醒；原件和未解析记录保留，其他邮件处理继续。', True, True))
        return actions
    if row["kind"] in {"attachment", "mail_message"}:
        available = current.get("object_available", False) and (row["kind"] != "attachment" or current.get("fingerprint_current", False))
        if current.get("original_on_win11"):
            actions.insert(0, action("reveal", "查看 Win11 原件信息", "原件保存在 Win11，本机只保存文字和回执。", available))
        else:
            actions.insert(0, action("reveal", "在 Finder 定位原件", "只显示所在位置，不打开或执行文件。" if available else "原件尚未完成归档，或其记录暂不可读。", available))
    if row["status"] != "open":
        return actions
    observation = json.loads(row["observation_json"])
    if row["source_id"] == "dingtalk_minutes" and observation.get("error_code") == "media_pending":
        media_ids = observation.get("pending_media_ids")
        actions.append(action("dismiss_media", "这几条听记无需继续处理",
                              "只结束当前这些听记的待处理提醒，保留文字和处理记录；其他听记不受影响。",
                              bool(media_ids) and media_ids == pending_media_ids(), True))
    import local_sync_repair
    linked = _linked_live_alerts(row["issue_id"])
    if any(a.get("status") == "open" and not a.get("acknowledged_at") for a in linked):
        actions.append(action("acknowledge", "确认知悉，停止重复提醒", "仅停止提醒；不关闭故障，也不停止修复验收。"))
    repairable = [a for a in linked if local_sync_repair.eligible(a)]
    if repairable:
        actions.insert(0, action("repair_local", "安全本地修复", "仅恢复可验证的回退记录、刷新派生索引并完整校验；内容冲突保持原样。",
                                not any(a.get("repair_status") in {"running", "queued"} for a in repairable)))
    if row["kind"] == "attachment":
        actions.append(action("review_note", "记录核对结论", "保存人工核对说明；不会直接关闭问题。"))
        available = current.get("fingerprint_current", False) and current.get("object_available", False)
        actions.append(action("dispose", "保留原件，不再自动解析", "仅结束当前内容的人工待办，保留原件和风险记录。" if available else "原件记录或内容指纹无法核验，请先重新检查。", available, True))
        if current.get("remote_attachment_policy"):
            actions.insert(1, action("retry_extract", "按 Win11 规则重新处理", "本人发件附件跳过；其他附件由 Win11 直接下载和读取，回传文字与回执。", available))
        elif json.loads(row["observation_json"]).get("error_code") == "pdf_extract_failed":
            actions.insert(1, action("retry_extract", "重试 PDF 文字提取", "只使用本机受限提取器，不写入业务记忆。", current.get("object_available", False) and current.get("fingerprint_current", False)))
    elif row["source_id"] in states.SOURCE_IDS and json.loads(row["observation_json"]).get("error_code") != "validation_inputs_changed":
        source = next((v for v in (control or {}).get("sources", []) if v.get("id") == row["source_id"]), {})
        eligibility = source.get("control") or {}
        actions.insert(0, action("retry_source", "重试此来源并复验", str(eligibility.get("reason") or "按现有安全条件运行，结果按问题分别复验。"),
                                  eligibility.get("available") is True and not eligibility.get("running")))
        safety = current.get("mail_safety") or {}
        if row["source_id"] == "dingtalk_mail" and str(current["observation"].get("current_blocker") or "").startswith("read_safety"):
            if safety.get("server_side_pass") and not safety.get("cross_device_confirmed"):
                actions.insert(0, action("confirm_mail", "核对另一台设备的未读状态", "需你实际查看测试邮件，不会默认选择。", True, True))
    return actions


def detail(config: dict[str, Any], issue_id: str, *, control: dict[str, Any] | None = None, maintenance: bool = False) -> dict[str, Any]:
    row = raw_issue(issue_id)
    obj, object_error = None, None
    try:
        obj = _object(config, row)
    except (OSError, KeyError, sqlite3.Error, email_sync.EmailSyncError):
        object_error = "原始状态暂时读不到；问题保持未完成。"
    if row["kind"] in {"attachment", "mail_message"} and obj is None and object_error is None:
        object_error = "对应原件记录暂不可用；已知问题保持未完成。"
    result = _public(row, obj)
    result["object_error"] = object_error
    result["object_available"] = bool(obj and obj.get("archive_path"))
    result["original_on_win11"] = bool(obj and str(obj.get("stored_name") or "").startswith("win11/"))
    result["remote_attachment_policy"] = bool(email_sync.win11_attachments.policy(config))
    result["fingerprint_current"] = bool(obj and row["kind"] == "attachment" and attachment_fingerprint(obj) == row["fingerprint"])
    links = links_for(issue_id)
    result["related_alert_ids"] = [link["alert_id"] for link in links]
    result["alert_health"] = [_health_public(a) for a in _linked_live_alerts(issue_id)]
    result["attempts"] = _attempts(row)
    result["attempts_note"] = ("附件缺少完整的逐次尝试记录，次数未知；已有提取回执单独展示，不能把未知写成 0 次。" if row["kind"] == "attachment"
                               else "下列是该来源的运行记录；不代表每个附件均执行了这些步骤。")
    result["blocked_by"] = []
    if row["source_id"] == "dingtalk_mail":
        import alert_workbench
        result["mail_safety"] = alert_workbench.safe_safety(config)
    connection = _db(readonly=True)
    try:
        if result["observation"].get("current_blocker") == "memory_core_unavailable":
            result["blocked_by"] = [{"issue_id": r["issue_id"], "title": r["title"]} for r in connection.execute(
                "SELECT issue_id,title FROM operational_issues WHERE source_id='memory_core' AND status='open'")]
            if not result["blocked_by"]:
                result["blocker_note"] = "先前被核心门禁阻止；当前未见同一核心未关闭问题，仍需本来源的新成功回执。"
        result["events"] = [{"event_type": r["event_type"], "detail": json.loads(r["detail_json"]), "created_at": r["created_at"]}
                            for r in connection.execute("SELECT * FROM operational_issue_events WHERE issue_id=? ORDER BY created_at,rowid", (issue_id,))]
        result["action_history"] = [public_action(dict(r)) for r in connection.execute(
            "SELECT * FROM operational_actions WHERE issue_id=? ORDER BY created_at DESC", (issue_id,))]
    finally:
        connection.close()
    result["completion_rule"] = ("你明确处置并保留原件，或同一附件成功复验后结束；其他邮件故障不受影响。"
                                 if row["kind"] == "attachment" else "必须取得本次问题之后、对应对象的成功证据；聊天和知悉不算完成。")
    if row["source_id"] == "dingtalk_minutes" and result["observation"].get("error_code") == "media_pending":
        result["completion_rule"] = "当前这些听记取得媒体成功回执，或你明确决定无需继续处理后结束；其他听记仍分别核验。"
    if result.get('video_waiting'):
        result['completion_rule']='保持待命，收到你针对这些视频的明确指令后再处理；现有非视频回执继续有效。'
    result["actions"] = _actions(row, result, control, maintenance)
    result["revision"] = digest([row["revision"], row["status"], result["fingerprint_current"], object_error, result.get("mail_safety"),
                                 result["actions"], result["observation"].get("reading"), maintenance])
    if result.get("source_id") == "dingtalk_chat":
        import dingtalk_commit_guard
        result["chat_recovery"] = dingtalk_commit_guard.recovery_evidence(config, result.get("first_seen"), result.get("resolved_at"))
    result["checked_at"] = syncv2.now_iso()
    return result


def _group_actions(
    row: dict[str, Any], control: dict[str, Any] | None, maintenance: bool
) -> list[dict[str, Any]]:
    def action(name: str, label: str, why: str, enabled: bool = True) -> dict[str, Any]:
        return {
            "id": name, "label": label,
            "reason": "维护期间只可查看，暂不执行。" if maintenance else why,
            "available": enabled and not maintenance,
            "requires_confirmation": False,
        }

    actions = [
        action("recheck", "重新核验这一组", "只重新读取每个成员的状态和回执，不会批量关闭问题。"),
        action("diagnostics", "查看组级脱敏诊断", "展示逐项读取结果和未完成原因，不包含邮件正文、附件内容或本机路径。"),
    ]
    if not row.get("video_waiting") and row["status"] == "open" and row["source_id"] in states.SOURCE_IDS and row.get("error_code") != "validation_inputs_changed":
        source = next(
            (item for item in (control or {}).get("sources", []) if item.get("id") == row["source_id"]),
            {},
        )
        eligibility = source.get("control") or {}
        actions.insert(0, action(
            "retry_source", "重试此来源并逐项复验",
            str(eligibility.get("reason") or "按现有安全条件运行一次；每项分别验收，不会一并抹去。"),
            eligibility.get("available") is True and not eligibility.get("running"),
        ))
    return actions


def group_detail(
    config: dict[str, Any], group_id: str, *, control: dict[str, Any] | None = None,
    maintenance: bool = False,
) -> dict[str, Any]:
    group = raw_group(group_id)
    rows = group_member_rows(group_id)
    objects: dict[str, dict[str, Any]] = {}
    object_error = None
    if any(row["kind"] in {"attachment", "mail_message"} for row in rows):
        try:
            mail = _mail(config)
            objects.update({attachment_key(item): item for item in mail["attachments"]})
            objects.update({"mail:" + digest(item["message_key"]): item for item in mail["messages"]})
        except (KeyError, OSError, sqlite3.Error, email_sync.EmailSyncError):
            object_error = "部分原始状态暂时读不到；问题组和已知成员保持未完成。"
    media = _media_snapshot()
    members = [_public(row, objects.get(row["object_key"]), media) for row in rows]
    result = _public_group(group, members)
    active = [item for item in members if item["status"] == "open"]
    representative = next((row for row in rows if row["status"] == "open"), rows[0] if rows else None)
    base_observation = json.loads(representative["observation_json"]) if representative else {}
    result["observation"] = {
        **base_observation,
        "error_code": group["error_code"],
        "grouped": True,
        "affected_count": result["affected_count"],
        "item_attempt_count": result["total_attempts"],
        "item_attempt_detail": (
            "组内各邮件的归档／语义尝试相加；每项明细仍独立保留。"
            if result.get("member_kind") == "mail_message"
            else "附件逐次尝试次数未知；已提取内容和读取限制见逐项回执。"
            if result.get("member_kind") == "attachment"
            else "组内每个受影响页面保留独立完整性证据；组级次数不代替逐项验收。"
        ),
    }
    result["observation"].pop("reading", None)  # A representative receipt is not group-wide evidence.
    if result.get("reading_complete"):
        result["observation"].update(
            historical_error_code=group["error_code"], impact=result["impact"],
            extract_status="extracted", reason_label=result["reason"],
            current_blocker=None, full_read_confirmed=True,
        )
    result["members"] = members
    result["object_error"] = object_error
    result["object_available"] = False
    result["fingerprint_current"] = False
    related: dict[str, dict[str, Any]] = {}
    for member in rows:
        for alert in _linked_live_alerts(member["issue_id"]):
            related[str(alert["alert_id"])] = alert
    result["related_alert_ids"] = sorted(related)
    result["alert_health"] = [_health_public(alert) for alert in related.values()]
    result["attempts"] = _attempts(representative) if representative and representative["kind"] != "attachment" else []
    result["attempts_note"] = "组级次数不会代替单项证据；展开受影响项目可查看和处理具体对象。"
    result["blocked_by"] = []
    result["events"] = []
    connection = _db(readonly=True)
    try:
        result["action_history"] = [public_action(dict(row)) for row in connection.execute(
            "SELECT * FROM operational_actions WHERE issue_id=? ORDER BY created_at DESC", (group_id,)
        )]
    finally:
        connection.close()
    result["completion_rule"] = "组内全部项目都取得各自成功回执或明确处置后才结束；组级聊天和重试本身不算完成。"
    if result.get('video_waiting'):
        result['completion_rule']='保持视频待命，等待你的明确指令；现有非视频处理结果继续有效。'
    result["actions"] = _group_actions({**group,'video_waiting':result.get('video_waiting',False)}, control, maintenance)
    result["revision"] = digest([
        group["revision"], group["status"],
        [(item["issue_id"], item["status"], item["revision"], item["observation"].get("reading")) for item in members],
        result["actions"], maintenance,
    ])
    if result.get("source_id") == "dingtalk_chat":
        import dingtalk_commit_guard
        result["chat_recovery"] = dingtalk_commit_guard.recovery_evidence(config, result.get("first_seen"), result.get("resolved_at"))
    result["checked_at"] = syncv2.now_iso()
    return result


def chat_context(config: dict[str, Any], issue_id: str) -> dict[str, Any]:
    row = raw_issue(issue_id)
    value = detail(config, issue_id)
    obs = value["observation"]
    return {"title": value["title"], "kind": row["kind"], "source": SOURCE_NAMES.get(row["source_id"], "同步来源"),
            "status": row["status"], "resolution_kind": row["resolution_kind"], "observation": obs,
            "attempts": _attempts(row), "completion_rule": "只有对应成功回执或明确人工处置才能结束；对话不执行动作。",
            "available_action_names": [a["label"] for a in value["actions"] if a["available"]]}


def group_chat_context(config: dict[str, Any], group_id: str) -> dict[str, Any]:
    value = group_detail(config, group_id)
    return {
        "title": value["title"],
        "kind": "operational_issue_group",
        "source": value["source_label"],
        "status": value["status"],
        "error_code": value["error_code"],
        "affected_count": value["affected_count"],
        "total_attempts": value["total_attempts"],
        "members": [
            {
                "kind": member["kind"], "status": member["status"],
                "attention_state": member["attention_state"],
                "error_code": (member.get("observation") or {}).get("error_code"),
                "attempts": None if member["kind"] == "attachment" else (member.get("observation") or {}).get("item_attempt_count", 0),
                **({"reading": member["observation"]["reading"]} if member["observation"].get("reading") else {}),
                **({"automatic_recovery": member["observation"]["automatic_recovery"]} if member["observation"].get("automatic_recovery") else {}),
            }
            for member in value["members"]
        ],
        "completion_rule": value["completion_rule"],
        "available_action_names": [action["label"] for action in value["actions"] if action["available"]],
    }


def legacy_conversation(identifier: str) -> list[dict[str, Any]]:
    import alert_workbench
    issue_ids = (
        [row["issue_id"] for row in group_member_rows(identifier)]
        if GROUP_RE.fullmatch(identifier) else [identifier]
    )
    alert_groups = {
        alert_workbench.group_key(json.loads(link["alert_snapshot_json"]))
        for issue_id in issue_ids for link in links_for(issue_id)
    }
    connection = _db(readonly=True)
    try:
        result = []
        if GROUP_RE.fullmatch(identifier):
            for issue_id in issue_ids:
                thread_id = "issue:" + issue_id
                messages = [dict(row) for row in connection.execute(
                    "SELECT message_id,author,content,status,created_at FROM alert_messages WHERE thread_id=? ORDER BY created_at,rowid",
                    (thread_id,),
                )]
                draft = connection.execute(
                    "SELECT draft FROM alert_threads WHERE thread_id=?", (thread_id,)
                ).fetchone()
                actions = [dict(row) for row in connection.execute(
                    "SELECT action,state,created_at,result_json FROM operational_actions WHERE issue_id=? ORDER BY created_at",
                    (issue_id,),
                )]
                if messages or actions or draft and draft["draft"]:
                    result.append({
                        "label": "此前单项问题的记录（只读保留）",
                        "messages": messages,
                        "draft": draft["draft"] if draft else "",
                        "actions": actions,
                    })
        for group in sorted(alert_groups):
            messages = [dict(r) for r in connection.execute(
                "SELECT message_id,author,content,status,created_at FROM alert_messages WHERE thread_id=? ORDER BY created_at,rowid", (group,))]
            draft = connection.execute("SELECT draft FROM alert_threads WHERE thread_id=?", (group,)).fetchone()
            actions = [dict(r) for r in connection.execute(
                "SELECT action,state,created_at,result_json FROM alert_action_receipts WHERE thread_id=? ORDER BY created_at", (group,))]
            if messages or actions or draft and draft["draft"]:
                result.append({"label": "此前同组告警的记录（保留原意，不作为本项处置）",
                               "messages": messages, "draft": draft["draft"] if draft else "", "actions": actions})
        return result
    finally:
        connection.close()


def public_action(row: dict[str, Any]) -> dict[str, Any]:
    value = {**{k: row[k] for k in ("action_id", "issue_id", "action", "state", "run_id", "created_at", "updated_at")},
             "result": json.loads(row["result_json"])}
    if row["run_id"]:
        connection = _db(readonly=True)
        try:
            run = connection.execute("SELECT status,error_code FROM action_runs WHERE run_id=?", (row["run_id"],)).fetchone()
            if run and run["status"] != "running":
                value["state"] = "completed" if run["status"] in {"success", "noop"} else "failed"
                value["execution"] = dict(run)
        finally:
            connection.close()
    return value


def safe_original(config: dict[str, Any], row: dict[str, Any]) -> Path:
    obj = _object(config, row)
    if obj and str(obj.get("stored_name") or "").startswith("win11/"):
        raise WorkbenchError("original_on_win11", "原件保存在 Win11；本机只保存提取文字和回执，请通过 SSH 在 Win11 继续处理。", 409)
    if not obj or not obj.get("archive_path"):
        raise WorkbenchError("original_unavailable", "原件尚未归档或暂不可用；不会打开其他文件。", 409)
    root = email_sync.runtime_paths(config)["archive_root"].resolve()
    archive = Path(obj["archive_path"])
    target = archive / str(obj["stored_name"]) if row["kind"] == "attachment" else archive
    # Reject links below the trusted archive root, not macOS's system /var alias.
    if ".." in target.parts:
        raise WorkbenchError("original_unsafe", "原件路径越过目录边界，已停止定位。", 409)
    for part in [target, *target.parents]:
        if part.resolve() == root:
            break
        if part.is_symlink():
            raise WorkbenchError("original_unsafe", "原件路径含符号链接，已停止定位。", 409)
    try:
        target.resolve(strict=True).relative_to(root)
    except (ValueError, OSError) as exc:
        raise WorkbenchError("original_unavailable", "原件路径无法通过安全核验。", 409) from exc
    if row["kind"] == "attachment":
        if not target.is_file() or attachment_fingerprint(obj) != row["fingerprint"]:
            raise WorkbenchError("issue_stale", "附件已经变化，请先重新检查。", 409)
        h = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != obj["sha256"]:
            raise WorkbenchError("original_hash_mismatch", "原件内容与已登记指纹不一致，问题保持未完成。", 409)
    return target


def _save_result(action_id: str, result: dict[str, Any], state: str = "completed") -> dict[str, Any]:
    connection = _db()
    try:
        with connection:
            connection.execute("UPDATE operational_actions SET state=?,result_json=?,updated_at=? WHERE action_id=?",
                               (state, json.dumps(result, ensure_ascii=False), syncv2.now_iso(), action_id))
        return public_action(dict(connection.execute("SELECT * FROM operational_actions WHERE action_id=?", (action_id,)).fetchone()))
    finally:
        connection.close()


def _dispose(row: dict[str, Any], action_id: str, reason: str, kind: str = "disposed") -> None:
    stamp = syncv2.now_iso()
    connection = _db()
    try:
        with connection:
            connection.execute("INSERT OR IGNORE INTO issue_dispositions VALUES(?,?,?,?,?,?,?)",
                               (row["object_key"], row["fingerprint"], row["issue_id"], kind, action_id, reason, stamp))
            _finish(connection, row["issue_id"], kind, {"action_id": action_id, "reason": reason}, stamp)
    finally:
        connection.close()


def spawn_extract_worker(config_path: Path, action_id: str) -> Any:
    return subprocess.Popen([os.sys.executable, str(Path(__file__).resolve()), "--config", str(config_path), "--extract-action", action_id],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def execute_action(app: Any, issue_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    import dingtalk_decision_center as center
    import alert_workbench
    import media_actions
    if payload.get("action") in media_actions.ACTIONS:
        return media_actions.execute(app,issue_id,payload)
    if not {"action_id", "action", "revision"}.issubset(payload) or set(payload) - {"action_id", "action", "revision", "confirmed", "note", "still_unread"}:
        raise WorkbenchError("action_contract", "处理请求字段无效。")
    action_id = alert_workbench.require_id(payload["action_id"])
    action = str(payload["action"])
    request_hash = digest([issue_id, payload])
    with syncv2.file_lock(_lock_path(), blocking=True):
        connection = _db()
        try:
            old = connection.execute("SELECT * FROM operational_actions WHERE action_id=?", (action_id,)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise WorkbenchError("action_conflict", "提交编号已用于另一项操作。", 409)
                return {**public_action(dict(old)), "saved": True, "idempotent_replay": True}
            try:
                control = center.current_control_state(app)
            except center.DecisionError:
                control = None
            current = detail(app.config, issue_id, control=control, maintenance=center.maintenance_active())
            if payload["revision"] != current["revision"]:
                raise WorkbenchError("issue_stale", "问题、原件或安全条件已有变化，旧操作已失效。", 409)
            choice = next((a for a in current["actions"] if a["id"] == action), None)
            if not choice or not choice["available"]:
                raise WorkbenchError("action_unavailable", (choice or {}).get("reason") or "此问题当前不能执行该操作。", 409)
            if action in {"dispose", "dismiss_media"} and payload.get("confirmed") is not True:
                raise WorkbenchError("choice_required", "请明确确认保留原件、不再自动解析；不会替你选择。")
            if action == "confirm_mail" and not isinstance(payload.get("still_unread"), bool):
                raise WorkbenchError("choice_required", "请明确选择另一台设备上测试邮件的未读状态。")
            note = str(payload.get("note") or "").strip()
            if len(note) > 1000 or action == "review_note" and not note:
                raise WorkbenchError("note_invalid", "核对说明须为 1 至 1,000 字。")
            stamp = syncv2.now_iso()
            with connection:
                connection.execute("INSERT INTO operational_actions(action_id,issue_id,action,request_hash,state,created_at,updated_at) VALUES(?,?,?,?,'prepared',?,?)",
                                   (action_id, issue_id, action, request_hash, stamp, stamp))
        finally:
            connection.close()
        row = raw_issue(issue_id)
        try:
            if action == "dismiss_media":
                media_ids = json.loads(row["observation_json"]).get("pending_media_ids")
                if not media_ids or media_ids != pending_media_ids():
                    raise WorkbenchError("issue_stale", "待补听记已经变化，未记录处置。", 409)
                _dispose(row, action_id, note or "你已明确选择：当前这些听记无需继续处理，文字与原始记录保留。")
                reconcile(app.config, _lock_held=True)
                result = {"message": "这些听记已按你的决定结束提醒，处理记录保留；未将媒体标为已补齐。", "resolution_kind": "disposed"}
            elif action == "dispose":
                # Re-read metadata within the explicit operation; no original is deleted.
                obj = _object(app.config, row)
                if not obj or attachment_fingerprint(obj) != row["fingerprint"]:
                    raise WorkbenchError("issue_stale", "附件内容已变化，未记录处置。", 409)
                # Video disposition changes only a version-bound reminder. It does not
                # open, alter or certify the original, which may be archived remotely.
                # The action was admitted against a current fingerprint and verified
                # non-video receipt above; ordinary attachment disposal keeps its guard.
                video_only = (current.get("video_waiting")
                              and current.get("fingerprint_current")
                              and current.get("object_available")
                              and json.loads(row["observation_json"]).get("reading", {}).get("non_video_complete")
                              and json.loads(row["observation_json"]).get("reading", {}).get("receipt_verified"))
                if not video_only:
                    safe_original(app.config, row)
                _dispose(row, action_id, note or "你已明确选择：保留原件，不再自动解析。")
                reconcile(app.config, _lock_held=True)
                result = {"message": "已处置：原件与风险记录保留，未声称解析成功。其他故障不受影响。", "resolution_kind": "disposed"}
            elif action == "recheck":
                reconcile(app.config, _lock_held=True)
                updated = raw_issue(issue_id)
                result = {"message": "已核验对应状态；只有取得成功证据或明确处置才会结束。", "fault_cleared": updated["status"] == "closed",
                          "resolution_kind": updated["resolution_kind"]}
            elif action == "diagnostics":
                result = {"message": "以下诊断仅含本问题的运行状态，可复制给维护者。", "diagnostics": chat_context(app.config, issue_id)}
            elif action == "review_note":
                connection = _db()
                try:
                    with connection:
                        _event(connection, issue_id, "review_note", {"note": note, "action_id": action_id}, syncv2.now_iso())
                finally:
                    connection.close()
                result = {"message": "核对结论已保存，问题仍待处理。", "fault_cleared": False}
            elif action == "confirm_mail":
                result = center.confirm_email_read_safety(app.config, payload["still_unread"])
                reconcile(app.config, _lock_held=True)
                result["message"] = result.get("next_action")
            elif action == "reveal":
                obj = _object(app.config, row)
                if obj and str(obj.get("stored_name") or "").startswith("win11/"):
                    result = {"message": "原件保存在 Win11，由 SSH 调用本地阅读器处理；M2 保存提取文字和校验回执。", "fault_cleared": False}
                else:
                    target = safe_original(app.config, row)
                    subprocess.run(["/usr/bin/open", "-R", str(target)], check=True, timeout=10,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    result = {"message": "已在 Finder 定位原件；没有打开、执行文件或关闭问题。", "fault_cleared": False}
            elif action == "retry_source":
                run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "workbench-source:" + action_id))
                result = center.control_action_gate(center.current_control_state(app), row["source_id"]) or center.start_control_action(
                    app.config_path, row["source_id"], requested_run_id=run_id)
                connection = _db()
                try:
                    with connection:
                        connection.execute("UPDATE operational_actions SET run_id=? WHERE action_id=?", (result.get("run_id"), action_id))
                finally:
                    connection.close()
                result["message"] = "来源任务已提交；每项问题分别复验，不会一并抹去。"
                return {**_save_result(action_id, result, "running" if result.get("run_id") else "failed"), "saved": True}
            elif action == "acknowledge":
                for alert in _linked_live_alerts(issue_id):
                    if alert["status"] == "open":
                        states.acknowledge(alert["alert_id"])
                result = {"message": "已知悉：停止重复提醒，故障仍未关闭；真实恢复会另行通知。", "fault_cleared": False}
            elif action == "repair_local":
                import local_sync_repair
                alert = next(a for a in _linked_live_alerts(issue_id) if local_sync_repair.eligible(a))
                run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "workbench-repair:" + action_id))
                result = local_sync_repair.start(app.config_path, alert["alert_id"], requested_run_id=run_id)
                with _db() as connection:
                    connection.execute("UPDATE operational_actions SET run_id=? WHERE action_id=?", (result.get("run_id"), action_id))
                result["message"] = "受限本地修复已提交；按文件与校验回执分别验收，不会覆盖后来的业务修改。"
                return {**_save_result(action_id, result, "running" if result.get("run_id") else "failed"), "saved": True}
            elif action == "retry_extract":
                # Worker has a fixed action and config path; user text can never become a command.
                run_id = syncv2.start_action_run("workbench_extract", requested_run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "workbench-extract:" + action_id)))
                connection = _db()
                try:
                    with connection:
                        connection.execute("UPDATE operational_actions SET run_id=?,state='running' WHERE action_id=?", (run_id, action_id))
                finally:
                    connection.close()
                _save_result(action_id, {"message": ("Win11 附件处理已开始，完成后按实际回执更新。" if current.get("remote_attachment_policy") else "本机 PDF 提取正在执行；原件保持不变。")}, "running")
                process = spawn_extract_worker(app.config_path, action_id)
                connection = _db()
                try:
                    with connection:
                        connection.execute("UPDATE operational_actions SET worker_pid=? WHERE action_id=?", (process.pid, action_id))
                    saved = public_action(dict(connection.execute("SELECT * FROM operational_actions WHERE action_id=?", (action_id,)).fetchone()))
                finally:
                    connection.close()
                return {**saved, "saved": True}
            else:
                raise WorkbenchError("action_invalid", "操作无效。")
            return {**_save_result(action_id, result), "saved": True}
        except Exception as exc:
            code = str(getattr(exc, "code", "workbench_action_failed"))
            if action == "retry_extract" and "run_id" in locals():
                syncv2.finish_action_run(run_id, "failed", error_code=code)
            return {**_save_result(action_id, {"message": str(getattr(exc, "message", "操作未完成；原始记录保持不变。")), "error_code": code}, "failed"), "saved": True}


def execute_group_action(app: Any, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    import dingtalk_decision_center as center
    import alert_workbench
    if set(payload) != {"action_id", "action", "revision"}:
        raise WorkbenchError("action_contract", "问题组处理请求字段无效。")
    action_id = alert_workbench.require_id(payload["action_id"])
    action = str(payload["action"])
    request_hash = digest([group_id, payload])
    with syncv2.file_lock(_lock_path(), blocking=True):
        connection = _db()
        try:
            old = connection.execute(
                "SELECT * FROM operational_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise WorkbenchError("action_conflict", "提交编号已用于另一项操作。", 409)
                return {**public_action(dict(old)), "saved": True, "idempotent_replay": True}
            try:
                control = center.current_control_state(app)
            except center.DecisionError:
                control = None
            current = group_detail(
                app.config, group_id, control=control, maintenance=center.maintenance_active()
            )
            if payload["revision"] != current["revision"]:
                raise WorkbenchError("group_stale", "问题组或成员状态已有变化，请查看刷新后的操作。", 409)
            choice = next((item for item in current["actions"] if item["id"] == action), None)
            if not choice or not choice["available"]:
                raise WorkbenchError(
                    "action_unavailable", (choice or {}).get("reason") or "当前不能执行此组级操作。", 409
                )
            stamp = syncv2.now_iso()
            with connection:
                connection.execute(
                    """
                    INSERT INTO operational_actions(
                      action_id,issue_id,action,request_hash,state,created_at,updated_at
                    ) VALUES(?,?,?,?,'prepared',?,?)
                    """,
                    (action_id, group_id, action, request_hash, stamp, stamp),
                )
        finally:
            connection.close()
        try:
            if action == "recheck":
                reconcile(app.config, _lock_held=True)
                updated = raw_group(group_id)
                result = {
                    "message": "已逐项重新核验；没有成功证据的成员仍保持未完成。",
                    "fault_cleared": updated["status"] == "closed",
                }
            elif action == "diagnostics":
                result = {
                    "message": "逐项诊断已显示在下方，包含处理状态、已核实的读取结果与未完成原因。",
                    "diagnostics": group_chat_context(app.config, group_id),
                }
            elif action == "retry_source":
                group = raw_group(group_id)
                run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "workbench-group-source:" + action_id))
                result = center.control_action_gate(
                    center.current_control_state(app), group["source_id"]
                ) or center.start_control_action(
                    app.config_path, group["source_id"], requested_run_id=run_id
                )
                with _db() as connection:
                    connection.execute(
                        "UPDATE operational_actions SET run_id=? WHERE action_id=?",
                        (result.get("run_id"), action_id),
                    )
                result["message"] = "来源任务已提交；组内每项分别复验，不会批量抹去。"
                return {
                    **_save_result(action_id, result, "running" if result.get("run_id") else "failed"),
                    "saved": True,
                }
            else:
                raise WorkbenchError("action_invalid", "组级操作无效。")
            return {**_save_result(action_id, result), "saved": True}
        except Exception as exc:
            code = str(getattr(exc, "code", "workbench_group_action_failed"))
            return {
                **_save_result(
                    action_id,
                    {
                        "message": str(getattr(exc, "message", "组级操作未完成；成员记录保持不变。")),
                        "error_code": code,
                    },
                    "failed",
                ),
                "saved": True,
            }


def extract_action(config: dict[str, Any], action_id: str) -> None:
    import dingtalk_decision_center as center
    connection = _db()
    try:
        action = dict(connection.execute("SELECT * FROM operational_actions WHERE action_id=?", (action_id,)).fetchone())
        if action["state"] == "running":
            with connection:
                connection.execute("UPDATE operational_actions SET worker_pid=? WHERE action_id=?", (os.getpid(), action_id))
    finally:
        connection.close()
    if action["action"] != "retry_extract" or action["state"] != "running":
        return
    run_id = action["run_id"]
    try:
        with center.maintenance_write_guard():
            lock = email_sync.acquire_run_lock()
            try:
                row = raw_issue(action["issue_id"])
                if email_sync.win11_attachments.policy(config):
                    obj = _object(config, row)
                    if not obj or attachment_fingerprint(obj) != row["fingerprint"]:
                        raise WorkbenchError("issue_stale", "附件记录已变化，请先重新核验。")
                    remote = email_sync.retry_attachment_on_windows(config, obj)
                    current_obj = _object(config, row)
                    if not current_obj or attachment_fingerprint(current_obj) != row["fingerprint"]:
                        raise WorkbenchError("issue_stale", "处理期间附件记录已变化，原问题仍保留。")
                    skipped = remote["extract_status"] == "skipped_self_sender"
                    if not skipped and remote["extract_status"] != "extracted":
                        raise WorkbenchError(remote["extract_status"], "Win11 尚未完成解析，原问题继续保留。")
                    output = syncv2.STATE_DIR / "workbench-extractions" / (action_id + ".txt")
                    text = remote.pop("text", "")
                    if text:
                        email_sync.atomic_write_text(output, text)
                    receipt_path = output.with_suffix(".json")
                    email_sync.atomic_write_text(receipt_path, json.dumps(remote, ensure_ascii=False, indent=2) + "\n")
                    kind = "disposed" if skipped else "recovered"
                    reason = ("按本人发件免解析规则跳过附件；未声称解析成功。" if skipped else "Win11 已直接下载并提取，同一来源及回传文字校验通过；未写入业务记忆。")
                    _dispose(row, action_id, reason, kind)
                    reconcile(config)
                    _save_result(action_id, {"message": reason, "resolution_kind": kind, "method": "win11_ssh",
                        "receipt_path": str(receipt_path), "text_path": str(output) if text else None})
                    syncv2.finish_action_run(run_id, "success", result={"extracted": 0 if skipped else 1, "skipped_self_sender": int(skipped), "business_writes": 0})
                    return
                original = safe_original(config, row)
                obj = _object(config, row)
                if original.suffix.lower() != ".pdf" or not obj or obj["risk"] != "ordinary":
                    raise WorkbenchError("extraction_unsafe", "此文件不满足普通 PDF 安全提取条件。")
                extracted, extraction, risk = email_sync.extract_attachment_text(original, 30000)
                # Recheck the same original after extraction, including its actual content hash.
                safe_original(config, row)
                if risk != "ordinary" or extraction not in {"extracted", "no_text"}:
                    raise WorkbenchError(extraction, "本机提取仍未成功，原问题和原件均已保留。")
                if extraction == "no_text":
                    raise WorkbenchError("pdf_no_text", "PDF 未提取到文字；不能把空结果当作恢复。")
                output = syncv2.STATE_DIR / "workbench-extractions" / (action_id + ".txt")
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with output.open("x", encoding="utf-8") as handle:
                    handle.write(extracted)
                output.chmod(0o600)
                _dispose(row, action_id, "同一 PDF 已完成本机文字提取；结果仅留本机，未自动写入业务记忆。", "recovered")
                reconcile(config)
                _save_result(action_id, {"message": "PDF 已成功提取，结果仅保留在本机；没有写入业务记忆。", "resolution_kind": "recovered"})
                syncv2.finish_action_run(run_id, "success", result={"extracted": 1, "business_writes": 0})
            finally:
                lock.close()
    except Exception as exc:
        code = str(getattr(exc, "code", "extraction_failed"))
        _save_result(action_id, {"message": str(getattr(exc, "message", "提取未完成，原件和问题保留。")), "error_code": code}, "failed")
        syncv2.finish_action_run(run_id, "failed", error_code=code)


def recover_actions(database: Path | None = None) -> None:
    with syncv2.file_lock(_lock_path(database), blocking=True):
        _recover_actions(database)


def _recover_actions(database: Path | None = None) -> None:
    connection = _db(database)
    try:
        with connection:
            for row in connection.execute("SELECT * FROM operational_actions WHERE state IN ('prepared','running')").fetchall():
                row = dict(row)
                if not row["run_id"] and row["action"] in {"retry_source", "retry_extract", "repair_local"}:
                    prefix = {"retry_source": "workbench-source:", "retry_extract": "workbench-extract:", "repair_local": "workbench-repair:"}[row["action"]]
                    candidate = str(uuid.uuid5(uuid.NAMESPACE_URL, prefix + row["action_id"]))
                    if connection.execute("SELECT 1 FROM action_runs WHERE run_id=?", (candidate,)).fetchone():
                        row["run_id"] = candidate
                        connection.execute("UPDATE operational_actions SET run_id=? WHERE action_id=?", (candidate, row["action_id"]))
                run = connection.execute("SELECT status,error_code FROM action_runs WHERE run_id=?", (row["run_id"],)).fetchone() if row["run_id"] else None
                state = None
                disposition = connection.execute("SELECT resolution_kind FROM issue_dispositions WHERE action_id=?", (row["action_id"],)).fetchone()
                if disposition:
                    state = "completed"
                elif run and run["status"] != "running":
                    state = "completed" if run["status"] in {"success", "noop"} else "failed"
                elif row["action"] == "retry_extract" and row["worker_pid"]:
                    try:
                        os.kill(row["worker_pid"], 0)
                    except ProcessLookupError:
                        state = "failed"
                    except PermissionError:
                        pass
                elif row["action"] == "retry_extract" and not row["worker_pid"]:
                    started = states.parse_time(row["created_at"])
                    if started and (states.now_dt() - started).total_seconds() > 30:
                        state = "failed"
                elif row["state"] == "prepared" and not run:
                    state = "failed"
                if state:
                    result = json.loads(row["result_json"])
                    result["message"] = ("人工处置或同一文件成功提取的记录已恢复。" if disposition else
                                         "执行已结束，问题是否结束以对应复验为准。" if state == "completed" else
                                         "执行中断或未成功，原问题与原件保留。")
                    if state == "failed":
                        result["error_code"] = (run["error_code"] if run else None) or "execution_interrupted"
                    connection.execute("UPDATE operational_actions SET state=?,result_json=?,updated_at=? WHERE action_id=?",
                                       (state, json.dumps(result, ensure_ascii=False),
                                        syncv2.now_iso(), row["action_id"]))
                    if row["action"] == "retry_extract" and run and run["status"] == "running" and state == "failed":
                        connection.execute("UPDATE action_runs SET status='failed',error_code='execution_interrupted',completed_at=?,updated_at=? WHERE run_id=?",
                                           (syncv2.now_iso(), syncv2.now_iso(), row["run_id"]))
    finally:
        connection.close()


def _health_public(alert):
    import supervisor_health_recovery
    value=states.public_alert(alert)
    progress=supervisor_health_recovery.view(alert, Path(__file__).resolve().parents[1])
    if progress:value['supervisor_recovery']=progress
    return value


if __name__ == "__main__":
    import argparse
    import dingtalk_memory_automation as memory
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--extract-action")
    args = parser.parse_args()
    if args.extract_action:
        extract_action(memory.load_config(args.config), args.extract_action)
