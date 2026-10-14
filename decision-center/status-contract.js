"use strict";

(function statusContractFactory(root, factory) {
  const contract = factory();
  if (typeof module === "object" && module.exports) module.exports = contract;
  if (root) root.SyncStatusContract = contract;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  const TONES = Object.freeze({
    failed: "critical",
    persistent_failure: "critical",
    error: "critical",
    notification_failed: "critical",
    interrupted: "critical",

    retrying: "warning",
    partial: "warning",
    manual_required: "warning",
    waiting_source: "warning",
    waiting_idle: "warning",
    waiting_power: "warning",
    waiting: "warning",
    deferred: "warning",
    stale: "warning",
    unknown: "warning",
    maintenance: "warning",
    maintenance_pending: "warning",
    power_unknown: "warning",
    blocked: "warning",
    attention: "warning",
    pending: "warning",
    queued: "warning",
    notification_pending: "warning",

    paused: "neutral",
    not_enrolled: "neutral",
    skipped: "neutral",
    inactive: "neutral",

    healthy: "success",
    success: "success",
    noop: "success",
    recovered: "success",
    accepted: "success",
    delivered: "success",

    syncing: "syncing",
    running: "syncing",
    writing: "syncing",
    in_progress: "syncing",
    started: "syncing",
  });

  const TERMINAL = new Set([
    "success", "noop", "partial", "failed", "skipped", "deferred",
    "interrupted", "notification_pending", "notification_failed",
  ]);

  function code(value) {
    return String(value?.code || value?.status || value || "unknown").trim().toLowerCase() || "unknown";
  }

  function toneFor(value) {
    return TONES[code(value)] || "warning";
  }

  function isTerminal(value) {
    return TERMINAL.has(code(value));
  }

  function labelFor(value) {
    const labels = {
      failed: "失败", persistent_failure: "持续失败", error: "错误",
      retrying: "正在重试", partial: "部分完成", manual_required: "需要人工处理",
      waiting_source: "等待来源", waiting_idle: "等待空闲", waiting_power: "等待接电",
      waiting: "等待条件", deferred: "暂缓", stale: "状态已过期", unknown: "状态未知",
      maintenance: "维护中", blocked: "等待解除阻断", pending: "等待处理", queued: "等待执行",
      maintenance_pending: "维护后待复验", power_unknown: "电源状态未知",
      paused: "已暂停", not_enrolled: "尚未登记", skipped: "已跳过", inactive: "未启用",
      healthy: "正常", success: "成功", noop: "无变化", recovered: "已恢复",
      accepted: "已接受", delivered: "已送达", syncing: "正在同步", running: "正在执行",
      writing: "正在写入", in_progress: "正在处理", started: "已经开始",
      notification_pending: "结果已完成，通知待确认", notification_failed: "结果已完成，通知失败",
      interrupted: "已中断",
    };
    return labels[code(value)] || "需要确认状态";
  }

  return Object.freeze({ code, toneFor, isTerminal, labelFor });
});
