#!/usr/bin/env python3
"""Read-only "alerts and failed items" overview for the Sync Center page.

Nothing here writes. Every database is opened read-only, and each section
degrades to an explicit "unreadable" card instead of failing the whole page.
Message text never leaves the chat database; only conversation names and times.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import alert_workbench
import dingtalk_memory_automation as memory

BEIJING = timezone(timedelta(hours=8))
STUCK_HOURS = 24
LONG_HOURS = 72

SOURCE_LABELS = alert_workbench.SOURCE_LABELS
SOURCE_IMPACT = {
    "dingtalk_mail": "钉钉邮件里的新内容可能还没进业务记忆。",
    "dingtalk_chat": "钉钉聊天里的新消息可能还没整理进业务记忆。",
    "dingtalk_minutes": "钉钉 AI 听记的内容可能不完整。",
    "win11": "Win11 那边的资料可能还没同步过来。",
    "memory_core": "业务记忆库本身可能受影响。",
    "system": "同步中心后台程序可能没有按时完成工作。",
}
CATEGORY_IMPACT = {
    "manual_attention": "有一部分内容在等人工指令才会处理，其余内容照常同步。",
    "waiting_source": "还没就绪的那部分资料暂时进不了业务记忆，已有的文字内容不受影响。",
}
CHANNEL_STATUS = {
    "delivered": "已送达", "visible": "已弹出", "failed": "发送失败", "pending": "等待发送",
    "retrying": "正在重试", "sending": "正在发送", "not_requested": "未发送",
    "permission_off": "通知权限已关闭", "blocked": "被拦截", "uncertain": "结果不明",
    "superseded": "已被新通知取代",
}
QUEUE_ERRORS = {
    "ContractError": "AI 返回的整理结果没通过程序的格式检查",
    "stale_batch": "整理到一半被中断",
    "model_quota_unavailable": "模型额度暂时不够",
    "codex_subscription_limit": "订阅额度用完了",
    "recovery_conflict_requires_review": "和以前的记录有冲突，需要人工核对",
    "proposal_commit_content_conflict": "整理结果和已有记忆内容冲突",
}
CHAT_ALERT_ERRORS = {
    "resource_name_missing": "钉钉返回的附件信息里缺少文件名，程序没法确定这个附件该怎么保存",
    "ContractError": "AI 返回的整理结果没通过程序的格式检查",
    "event_retry_budget_exhausted": "实时消息的重试次数用完了",
    "incremental_partial": "定期补同步只完成了一部分",
}
CHANGE_KIND = {"insert": "新消息", "update": "改过的消息", "hidden": "撤回或隐藏的消息", "visible_again": "重新可见的消息"}
LISTENER_STATE = {"ready": "在运行", "starting": "正在启动", "failed": "出故障", "stopped": "已停止"}


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=BEIJING)


def _fmt(value: Any) -> str:
    parsed = _parse(value)
    if not parsed:
        return "未知"
    local = parsed.astimezone(BEIJING)
    return f"{local.month}月{local.day}日 {local:%H:%M}"


def _hours(value: Any, now: datetime) -> Optional[float]:
    parsed = _parse(value)
    return None if parsed is None else (now - parsed).total_seconds() / 3600


def _age(value: Any, now: datetime) -> str:
    hours = _hours(value, now)
    if hours is None:
        return "未知时长"
    if hours < 1:
        return "不到 1 小时"
    if hours < 48:
        return f"约 {int(hours)} 小时"
    return f"约 {int(hours // 24)} 天"


def _readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    connection = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _item(section: str, level: str, title: str, what: str, impact: str, action: str,
          facts: list[list[str]], rows: Optional[list[list[str]]] = None) -> dict[str, Any]:
    return {"section": section, "level": level, "title": title, "what": what, "impact": impact,
            "action": action, "facts": facts, "rows": rows or []}


def _unreadable(section: str, title: str, exc: BaseException) -> dict[str, Any]:
    return _item(section, "act", title + "：这一块读不出来",
                 f"本页读取这部分数据时出错（{type(exc).__name__}），所以下面看不到它的内容。",
                 "这一块的告警或失败项可能存在，但本页现在显示不出来。",
                 "需要让 AI 排查本页为什么读不出这部分数据。", [])


def sync_alert_items(alerts: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    items = []
    for alert in alerts:
        members = alert.get("group_member_ids") or [alert["alert_id"]]
        if alert.get("group_primary_id") not in (None, alert["alert_id"]):
            continue
        source = str(alert.get("source_id") or "")
        delivery = alert.get("delivery") or {}
        # Feishu is a retired channel for sync alerts; its columns only hold pre-September history.
        channels = "Mac 通知 {} · Miso {}".format(
            CHANNEL_STATUS.get(str((delivery.get("macos") or {}).get("status")), "未知"),
            CHANNEL_STATUS.get(str((delivery.get("miso") or {}).get("status")), "未知"),
        )
        hours = _hours(alert.get("first_seen"), now)
        impact = CATEGORY_IMPACT.get(str(alert.get("category") or ""), SOURCE_IMPACT.get(source, "影响范围未知。"))
        if alert.get("action_required"):
            level, action = "act", f"需要你：{alert.get('action_label')}。"
        elif alert.get("acknowledged_at"):
            level, action = "watch", f"你已在 {_fmt(alert['acknowledged_at'])} 标记知道了；这条告警没有要求你本人操作。"
        elif alert.get("category") == "waiting_source":
            level, action = "watch", "在等原始资料就绪，后台会继续探测；不用你动手。"
        elif source == "dingtalk_chat" and alert.get("error_code") == "chat_queue_not_drained":
            level, action = "watch", "原因见本页下方「聊天消息整理失败」那一项，按那一项处理即可。"
        elif hours is not None and hours >= LONG_HOURS:
            level, action = "act", f"没有要求你本人操作，但已持续{_age(alert.get('first_seen'), now)}，后台一直没能自己恢复，建议让 AI 排查原因。"
        else:
            level, action = "watch", "这条告警没有要求你本人操作，后台会继续检查。"
        facts = [["来源", SOURCE_LABELS.get(source, source or "未知")],
                 ["严重程度", "严重" if alert.get("effective_state") == "critical" else "提醒"],
                 ["第一次出现", f"{_fmt(alert.get('first_seen'))}（已持续{_age(alert.get('first_seen'), now)}）"],
                 ["最近一次确认", _fmt(alert.get("last_seen"))],
                 ["推送情况", channels]]
        if len(members) > 1:
            facts.append(["合并显示", f"同类告警 {len(members)} 条"])
        detail = str(alert.get("detail") or "").strip()
        items.append(_item("sync", level, str(alert.get("title") or "未命名告警"),
                           detail or "告警没有附带说明。", impact, action, facts))
    return items


def chat_alert_items(config: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    connection = _readonly(memory.runtime_paths(config)["database"])
    try:
        rows = connection.execute(
            "SELECT title,error_code,severity,created_at,last_seen_at FROM automation_alerts "
            "WHERE status!='resolved' ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((str(row["title"]), str(row["error_code"] or ""), str(row["severity"])), []).append(row)
    items = []
    for (title, code, severity), members in groups.items():
        reason = CHAT_ALERT_ERRORS.get(code, f"错误代码 {code or '未记录'}")
        manual = "人工" in title
        action = ("告警本身写明需要人工介入。本页只展示，不会自动处理或删除；要处理时让 AI 逐条核对。"
                  if manual else "这类告警没有要求你本人操作。")
        items.append(_item(
            "chat_alert", "act" if manual else "watch",
            title + (f"（{len(members)} 条）" if len(members) > 1 else ""),
            reason + "。", SOURCE_IMPACT["dingtalk_chat"], action,
            [["严重程度", "严重" if severity == "critical" else "提醒"],
             ["最早出现", f"{_fmt(members[0]['created_at'])}（已过去{_age(members[0]['created_at'], now)}）"],
             ["最近出现", _fmt(max(str(m["last_seen_at"] or "") for m in members))],
             ["推送情况", "只记在钉钉群聊同步库里，没有单独推送给你"]],
        ))
    return items


def chat_failed_items(config: dict[str, Any], related: Optional[str], now: datetime) -> list[dict[str, Any]]:
    connection = _readonly(memory.runtime_paths(config)["database"])
    try:
        rows = connection.execute(
            """
            SELECT q.change_kind,q.attempt_count,q.last_attempt_at,q.last_error_code,
                   m.create_time,c.conversation_name
            FROM semantic_queue q JOIN messages m ON m.message_id=q.message_id
            LEFT JOIN conversations c ON c.conversation_id=m.conversation_id
            WHERE q.state='failed' ORDER BY q.last_attempt_at DESC,q.queue_id DESC LIMIT 200
            """
        ).fetchall()
    finally:
        connection.close()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row["last_error_code"] or ""), []).append(row)
    items = []
    for code, members in groups.items():
        latest = max((str(m["last_attempt_at"] or "") for m in members), default="")
        hours = _hours(latest, now)
        if hours is not None and hours >= STUCK_HOURS:
            level = "act"
            action = (f"最近一次尝试在 {_fmt(latest)}，之后{_age(latest, now)}没再重试，看起来不会自己恢复。"
                      "需要让 AI 排查原因后重新整理。")
        else:
            level, action = "watch", "刚失败不久，后台可能还会再试；先不用管。"
        facts = [["失败原因", QUEUE_ERRORS.get(code, f"错误代码 {code or '未记录'}")],
                 ["已尝试次数", "、".join(sorted({str(m["attempt_count"]) for m in members})) + " 次"],
                 ["最近一次尝试", _fmt(latest)]]
        if related:
            facts.append(["相关总告警", related])
        items.append(_item(
            "chat_failed", level, f"{len(members)} 条聊天消息整理失败",
            f"这些消息交给 AI 整理成业务记忆时失败了：{QUEUE_ERRORS.get(code, '原因见错误代码')}。",
            "这些消息里的业务信息还没进业务记忆；聊天原文已保存在本机同步库，不会丢。",
            action, facts,
            [[str(m["conversation_name"] or "未知会话"), str(m["create_time"] or "未知时间")[:16],
              CHANGE_KIND.get(str(m["change_kind"]), str(m["change_kind"]))] for m in members],
        ))
    return items


def listener_item(config: dict[str, Any], now: datetime) -> dict[str, Any]:
    paths = memory.runtime_paths(config)
    status = json.loads(paths["status"].read_text(encoding="utf-8"))
    incremental_path = paths["database"].parent / "incremental_status.json"
    incremental = json.loads(incremental_path.read_text(encoding="utf-8")) if incremental_path.is_file() else {}
    state = str(status.get("listener_state") or "unknown")
    receipts = status.get("live_event_receipts") or {}
    seen = status.get("incoming_by_kind") or {}

    def kind_line(kind: str, label: str) -> str:
        at = (receipts.get(kind) or {}).get("received_at")
        if seen.get(kind) and at:
            return f"{label}新消息：最近一次实时收到在 {_fmt(at)}。"
        return f"{label}新消息：这次启动后还没实时收到。"

    since = status.get("listener_ready_at") if state == "ready" else status.get("listener_started_at")
    what = f"实时监听{LISTENER_STATE.get(state, '状态未知')}" + (f"（{_fmt(since)} 起）。" if since else "。")
    what += kind_line("direct", "单聊") + kind_line("group", "群聊")
    passed = status.get("live_acceptance") == "passed"
    if passed:
        what += "实时验收已通过。"
    else:
        what += "实时验收还没通过：要单聊、群聊各收到一条这次启动后的真实新消息才算通过。"
        if status.get("server_subscription_listing") == "empty":
            what += "钉钉服务器返回的订阅清单是空的，所以只能靠收到真实消息来证明订阅有效。"
    missing = [label for kind, label in (("direct", "单聊"), ("group", "群聊")) if not seen.get(kind)]
    if state in {"failed", "stopped"}:
        code = status.get("listener_error_code")
        level, action = "act", f"实时监听{LISTENER_STATE[state]}" + (f"（错误代码 {code}）" if code else "") + "，需要让 AI 排查。"
    elif state == "ready" and passed:
        level, action = "info", "不用你动手。"
    elif state == "ready":
        level = "watch"
        action = "不用你动手；" + ("、".join(missing) + "里有人发新消息后" if missing else "下次检查时") + "会自动通过验收。"
    else:
        level, action = "watch", "正在启动或状态未知，先不用管；如果过几个小时还是这样，再让 AI 排查。"
    facts = [["订阅最近检查", _fmt(status.get("subscription_checked_at"))]]
    if status.get("last_hourly_at"):
        facts.append(["最近一轮整理", f"{_fmt(status['last_hourly_at'])}，{'成功' if status.get('last_hourly_status') == 'success' else '未成功'}"
                      f"（距今{_age(status['last_hourly_at'], now)}）"])
    if incremental:
        facts.append(["定期补同步", f"{_fmt(incremental.get('completed_at'))}，{'成功' if incremental.get('status') == 'success' else '未成功'}，"
                      f"已核对 {incremental.get('conversations_complete', '?')}/{incremental.get('conversations_discovered', '?')} 个会话"])
    return _item("listener", level, "钉钉实时监听", what,
                 "影响钉钉新消息进入业务记忆的速度；定期补同步另外在跑。", action, facts)


def collect(config: dict[str, Any], *, maintenance: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    related = None
    unsent = None
    try:
        open_alerts = alert_workbench.listing(config, maintenance=maintenance, limit=1000, status="open")["alerts"]
        sync_items = sync_alert_items(open_alerts, now)
        unsent = sum((a.get("delivery") or {}).get("summary") in {"pending", "not_requested"}
                     and (_hours(a.get("first_seen"), now) or 0) >= 1
                     for a in open_alerts if a.get("group_primary_id") in (None, a["alert_id"]))
        if any(a.get("source_id") == "dingtalk_chat" and a.get("error_code") == "chat_queue_not_drained" for a in open_alerts):
            related = "「钉钉聊天持续失败」（整理队列没清空）通常就是因为这些失败项还留在队列里"
    except Exception as exc:  # One broken source must not hide the others.
        sync_items = [_unreadable("sync", "同步中心总告警", exc)]
    for section, title, build in (
        ("chat_alert", "钉钉群聊同步库里的告警", lambda: chat_alert_items(config, now)),
        ("chat_failed", "失败的聊天整理项", lambda: chat_failed_items(config, related, now)),
        ("listener", "钉钉实时监听", lambda: [listener_item(config, now)]),
    ):
        try:
            items.extend(build())
        except Exception as exc:
            items.append(_unreadable(section, title, exc))
    items = sync_items + items
    return {
        "schema_version": 1,
        "generated_at": now.astimezone(BEIJING).isoformat(timespec="seconds"),
        "generated_label": _fmt(now),
        "act_count": sum(item["level"] == "act" for item in items),
        "watch_count": sum(item["level"] == "watch" for item in items),
        "unsent_count": unsent,
        "items": items,
    }
