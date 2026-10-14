"use strict";

// Alert shortcuts share the workbench's problem identity and Codex handoff.
const alertWorkspace = (() => {
  let filter = "history";
  const transfers = new Map();
  function scheduleTransfer(alertId, transfer) {
    clearTimeout(transfer.timer);
    if (!codexHandoff.isPending(transfer.handoff) || transfer.error) return;
    transfer.timer = setTimeout(async () => {
      if (transfers.get(alertId) !== transfer) return;
      try { transfer.handoff = (await api(transfer.base + "/handoff")).handoff; }
      catch (error) { transfer.error = error.message; }
      renderList(); scheduleTransfer(alertId, transfer);
    }, 1800);
  }
  async function toCodex(row, retry = false) {
    const prior = transfers.get(row.alert_id);
    if (prior?.busy || ui.core?.maintenance?.active) return;
    clearTimeout(prior?.timer);
    const transfer = { ...prior, handoff: prior?.handoff || row.codex_handoff, busy: true, error: "" };
    transfers.set(row.alert_id, transfer); renderList();
    try {
      if (!retry || !transfer.base) transfer.base = row.codex_handoff?.base || (await workbenchWorkspace.resolveAlertTarget(row)).base;
      transfer.handoff = await codexHandoff.openSummary(transfer.base, { retrySubmissionId: retry ? transfer.handoff?.submission_id : null });
    } catch (error) {
      transfer.error = error.message;
      if (error.handoff) transfer.handoff = error.handoff;
    } finally { transfer.busy = false; renderList(); scheduleTransfer(row.alert_id, transfer); }
  }
  function renderList() {
    const history = $("#alert-history");
    if (history && !history.open) return;
    const payload = ui.alerts || {};
    const all = payload.alerts || [];
    const rows = all.filter((row) => filter === "history" ? row.status === "resolved"
      : row.status === "open" && (!row.group_primary_id || row.group_primary_id === row.alert_id));
    text("#alert-current-count", ui.panelErrors.alerts ? "—" : payload.open_total ?? "—");
    text("#alert-list-health", ui.panelErrors.alerts ? ui.panelErrors.alerts.message + " 已知告警继续保留。" : "");
    $("#alert-list").innerHTML = rows.length ? rows.map((row) => {
      const memberIds = new Set([row.alert_id, ...(row.group_member_ids || [])]);
      const issues = [...new Map(all.filter((member) => memberIds.has(member.alert_id)).flatMap((member) => member.issues || []).map((item) => [item.issue_id, item])).values()];
      const open = issues.filter((item) => item.status === "open");
      const manual = open.filter((item) => item.attention_state === "needs_user").length;
      const disposed = issues.some((item) => item.resolution_kind === "disposed");
      const closed = row.status === "resolved";
      const transfer = transfers.get(row.alert_id);
      const handoff = (row.codex_handoff?.updated || 0) > (transfer?.handoff?.updated || 0) ? row.codex_handoff : transfer?.handoff || row.codex_handoff;
      const delivered = handoff?.status === "delivered" && Boolean(handoff.thread_id);
      const pending = codexHandoff.isPending(handoff);
      const maintenance = Boolean(ui.core?.maintenance?.active);
      const busy = Boolean(transfer?.busy || pending && !transfer?.error);
      const notice = transfer?.error || (maintenance ? "维护期间暂缓转交。" : handoff?.error)
        || (busy ? "正在转到 Codex，核对接收回执…" : "");
      const retry = ["failed", "uncertain"].includes(handoff?.status);
      const status = row.legacy_recovery_unverified ? "历史恢复缺少完整回执 · 待核验" : closed ? (disposed ? "已结束 · 含人工处置" : issues.some((item) => item.resolution_kind === "recovered") ? "已恢复 · 证据保留" : "已结束 · 查看对应证据") : row.effective_state === "maintenance_pending" ? "等待维护后复验" : row.effective_state === "critical" ? "严重 · 未结束" : "未结束";
      const delivery = [deliveryLabel("Mac", row.delivery?.macos), deliveryLabel("Miso", row.delivery?.miso)].filter(Boolean).join(" · ");
      return `<article class="alert-summary ${closed ? "ended" : row.effective_state === "critical" ? "critical" : ""}"><header><span class="state-dot ${closed ? "neutral" : row.effective_state === "critical" ? "critical" : "warning"}" aria-hidden="true"></span><div><span class="eyebrow">${esc(status)}</span><h2>${esc(row.title)}</h2></div>${codexHandoff.badge(handoff, closed)}</header><p>${esc(closed && row.category !== "recovery" ? "当时的告警：" + row.detail : row.detail)}</p><p class="alert-summary-count">${issues.length ? closed ? `关联 ${issues.length} 项，处理记录保留` : `关联 ${open.length} 项未结束问题，其中 ${manual} 项需你处理` : "关联问题待核验；不会自动判为完成"}</p>${delivery ? `<p class="alert-summary-delivery">通知：${esc(delivery)}</p>` : ""}<footer><time>${esc(formatTime(closed ? row.resolved_at || row.last_seen : row.last_seen, true))}</time><div class="alert-summary-actions">${!closed ? `<button type="button" class="secondary-button" data-alert-codex="${esc(row.alert_id)}" ${busy || maintenance ? "disabled" : ""}>${busy ? "正在转到 Codex…" : delivered ? "在 Codex 中继续" : "转到 Codex 处理"}</button>` : ""}<button type="button" class="secondary-button" data-alert-workbench="${esc(row.alert_id)}">${closed ? "查看处理记录" : "前往问题工作台"} →</button></div></footer>${!closed && notice ? `<p class="alert-codex-status ${transfer?.error || handoff?.error ? "error-text" : ""}" role="status">${esc(notice)}</p>` : ""}${!closed && retry ? `<button type="button" class="secondary-button" data-alert-codex-retry="${esc(row.alert_id)}" ${busy || maintenance ? "disabled" : ""}>重试转交</button>` : ""}</article>`;
    }).join("") : `<p class="empty">${ui.panelErrors.alerts ? "告警状态未能读取，请重试。" : filter === "history" ? "暂无已结束记录。" : "当前没有未结束告警。"}</p>`;
    $$("[data-alert-workbench]").forEach((button) => button.addEventListener("click", () => {
      const row = all.find((item) => item.alert_id === button.dataset.alertWorkbench);
      if (row) workbenchWorkspace.fromAlert(row);
    }));
    $$("[data-alert-codex], [data-alert-codex-retry]").forEach((button) => button.addEventListener("click", () => {
      const row = all.find((item) => item.alert_id === (button.dataset.alertCodex || button.dataset.alertCodexRetry));
      if (row) toCodex(row, Boolean(button.dataset.alertCodexRetry));
    }));
    $$("[data-alert-filter]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.alertFilter === filter)));
  }
  function bind() {
    $("#alert-history")?.addEventListener("toggle", () => renderList());
    $$("[data-alert-filter]").forEach((button) => button.addEventListener("click", () => { filter = button.dataset.alertFilter; renderList(); }));
  }
  return { renderList, bind, refreshSelected: async () => {}, confirmMail: (value) => workbenchWorkspace.confirmMail(value) };
})();
