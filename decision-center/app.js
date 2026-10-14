"use strict";

let csrf = document.querySelector('meta[name="csrf-token"]').content;
const ui = {
  view: "overview", visible: true, loading: false, core: null, system: null,
  panelErrors: {}, panelUpdated: {}, caseDetailError: null, caseConversationError: null,
  alerts: null, events: null, workbench: null, needs: [], archived: [], automatic: [], history: [],
  selectedCase: null, conversation: null, preview: null, mobilePane: "detail",
  needGroups: [], archiveGroups: [], selectedGroupKey: null, groupDetails: [],
  draftTimer: null, localDraftDirty: false, conversationPoll: null,
  chatCaseId: null, chatMessageCount: 0,
  aiProfile: null, selectingCaseId: null, caseRequestId: 0,
  pendingClientMessage: null, archiveRequest: null,
  activeAction: null, actionPoll: null, elapsedTimer: null,
  trackedRunId: null, dismissedRunId: null, homeWorkbench: null, homeWorkbenchError: null, homeUpdatedAt: null,
};

const pageMeta = {
  overview: ["概览", "当前状态与待处理事项"],
  sources: ["同步来源", "各来源的同步状态与最近成功记录"],
  "needs-user": ["待处理", "需要关注和处理的事项都在这里，相同问题只显示一次"],
  "archived-questions": ["已归档问题", "完整保留问题、证据和只读对话，不再计入当前工作量"],
  automatic: ["自动处理", "进度在这里，逐条证据留在审计层"],
  activity: ["运行记录", "最近的本机同步与收口活动"],
  processes: ["进程", "高级运行诊断"],
  locks: ["锁与维护", "写入安全和维护状态"], history: ["历史", "已收口、旧台账与信息型记录"],
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const statusContract = window.SyncStatusContract;
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[character]);
const text = (selector, value) => { const node = $(selector); if (node) node.textContent = value; };
const statusCode = (value) => String(value?.code || value?.status || value || "unknown").toLowerCase();
const statusTone = (value) => statusContract?.toneFor(value) || "warning";

function formatTime(value, withDate = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const options = withDate
    ? { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit" };
  return new Intl.DateTimeFormat("zh-CN", options).format(date);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function formatDuration(startedAt, completedAt = null) {
  const started = new Date(startedAt || "");
  const ended = completedAt ? new Date(completedAt) : new Date();
  if (Number.isNaN(started.getTime()) || Number.isNaN(ended.getTime())) return "已用时 —";
  const seconds = Math.max(0, Math.floor((ended.getTime() - started.getTime()) / 1000));
  if (seconds < 60) return `已用时 ${seconds} 秒`;
  const minutes = Math.floor(seconds / 60); const remainder = seconds % 60;
  return `已用时 ${minutes} 分 ${remainder} 秒`;
}

function actionTargetLabel(action) {
  return ({
    all: "全部来源", dingtalk_all: "钉钉来源", dingtalk_chat: "钉钉聊天", local_repair: "安全本地修复",
    dingtalk_minutes: "钉钉听记", dingtalk_mail: "企业邮件", win11: "Win11 资料",
    wechat: "微信资料", sync_now: "钉钉来源",
  })[action?.target || action?.action_type] || "同步任务";
}

function safeErrorLabel(code) {
  return ({
    validation_failed: "钉钉记忆校验失败",
    claim_base_mismatch: "业务页面校验基线失效",
    chat_reconcile_rollback_concurrent_change: "钉钉聊天同步遇到并发变化，已安全回滚",
    controller_unavailable: "同步控制器暂时不可用",
    interrupted_after_process_exit: "同步进程意外退出",
    interrupted_after_timeout: "同步超时后已安全中断",
    memory_core_unavailable: "记忆库核心当前不可写",
    email_items_failed: "部分邮件或附件处理失败，已保留待重试记录",
    read_safety_unconfirmed: "等待跨设备未读状态核对",
    read_safety_server_unverified: "需要重新完成服务器端安全核验",
    read_safety_failed: "未读状态核验未通过",
    codex_timeout: "ChatGPT 回答超时，消息已保存",
    worker_interrupted: "回答进程已中断，可继续重试",
    maintenance_active: "记忆库正在维护",
    already_running: "已有同步正在运行",
    waiting_power: "等待接通电源后继续；已完成的结果保留",
  })[String(code || "")] || (code ? `错误：${String(code).replaceAll("_", " ")}` : "请查看失败步骤");
}

function latestManualAll() {
  return ui.core?.latest_manual_actions?.all || ui.core?.latest_manual_actions?.sync_now || null;
}

function latestSyncAction() {
  const targets = new Set(["all", "sync_now", "dingtalk_all", "dingtalk_chat", "dingtalk_minutes", "dingtalk_mail", "win11", "wechat"]);
  const rows = [...Object.values(ui.core?.latest_actions || {}), latestManualAll()].filter(item => item?.run_id && targets.has(item.action_type));
  const time = item => Date.parse(item.completed_at || item.updated_at || item.started_at || "") || 0;
  return rows.sort((a, b) => time(b) - time(a))[0] || null;
}

function normalizeAiProfile(configuration) {
  const config = configuration || ui.conversation?.ai_configuration || {};
  const fallback = config.default || { model: "gpt-6-astra", reasoning_effort: "max", speed: "fast" };
  let stored = ui.aiProfile;
  if (!stored) {
    try { stored = JSON.parse(localStorage.getItem("sync-center:case-ai-profile") || "null"); } catch (_) { stored = null; }
  }
  const models = Array.isArray(config.models) ? config.models : [];
  const migrationKey = "sync-center:case-ai-profile-migration";
  const migrationVersion = "astra-20260907";
  const migrateModel = models.some((item) => item.id === "gpt-6-astra")
    && localStorage.getItem(migrationKey) !== migrationVersion;
  if (migrateModel) stored = { ...stored, model: "gpt-6-astra" };
  const model = models.find((item) => item.id === stored?.model) || models.find((item) => item.id === fallback.model) || models[0];
  const effortIds = Array.isArray(model?.reasoning_efforts) ? model.reasoning_efforts : [];
  const effort = effortIds.includes(stored?.reasoning_effort) ? stored.reasoning_effort
    : (effortIds.includes(fallback.reasoning_effort) ? fallback.reasoning_effort : effortIds[0]);
  const speeds = Array.isArray(config.speeds) ? config.speeds : [];
  const speed = speeds.some((item) => item.id === stored?.speed) ? stored.speed
    : (speeds.some((item) => item.id === fallback.speed) ? fallback.speed : speeds[0]?.id);
  ui.aiProfile = { model: model?.id || fallback.model, reasoning_effort: effort || fallback.reasoning_effort, speed: speed || fallback.speed };
  localStorage.setItem("sync-center:case-ai-profile", JSON.stringify(ui.aiProfile));
  if (migrateModel) localStorage.setItem(migrationKey, migrationVersion);
  return ui.aiProfile;
}

function aiLabel(kind, value, configuration = ui.conversation?.ai_configuration) {
  const rows = kind === "model" ? configuration?.models : (kind === "reasoning_effort" ? configuration?.reasoning_efforts : configuration?.speeds);
  return (Array.isArray(rows) ? rows.find((item) => item.id === value)?.label : null) || value || "—";
}

function profileSummary(profile) {
  if (!profile) return "";
  return `${aiLabel("model", profile.model)} · ${aiLabel("reasoning_effort", profile.reasoning_effort)} · ${aiLabel("speed", profile.speed)}`;
}

function decisionLabel(item) {
  return ({
    business_choice: "业务方向",
    technical_signoff: "技术确认",
    approval_record: "批准记录",
    authorization: "授权确认",
  })[item?.dimension_key] || item?.dimension || "待确认事项";
}

function representativeMatter(item) {
  const summary = String(item?.evidence_summary || "");
  const marker = "代表性事项：";
  const matter = summary.includes(marker) ? summary.split(marker)[1].split("；")[0].trim() : "";
  return matter || decisionLabel(item);
}

function fallbackDecisionQuestion(item) {
  const matter = representativeMatter(item);
  if (item?.case_kind === "authorization" || item?.dimension_key === "technical_signoff") {
    return `请确认“${matter}”是否已完成最终技术签署；如果没有，由谁补齐、何时再确认？`;
  }
  return `请确认“${matter}”应采用什么业务口径和优先顺序；如果现在还不能决定，需要等待什么信息？`;
}

function groupNeeds(items) {
  const groups = [];
  const byKey = new Map();
  (Array.isArray(items) ? items : []).forEach((item) => {
    const key = item.case_id;
    let group = byKey.get(key);
    if (!group) {
      group = { groupKey: key, subject: item.display_title || item.title || item.subject, cases: [] };
      byKey.set(key, group); groups.push(group);
    }
    group.cases.push(item);
  });
  return groups;
}

function isArchiveView() { return ui.view === "archived-questions"; }

function workbenchGroups() { return isArchiveView() ? ui.archiveGroups : ui.needGroups; }

function refreshNeedGroups() {
  ui.needGroups = groupNeeds(ui.needs);
  ui.archiveGroups = groupNeeds(ui.archived);
  if (ui.selectedCase) {
    ui.selectedGroupKey = workbenchGroups().find((group) => group.cases.some((item) => item.case_id === ui.selectedCase.case_id))?.groupKey || null;
  }
}

function currentNeedGroup() {
  return workbenchGroups().find((group) => group.groupKey === ui.selectedGroupKey)
    || workbenchGroups().find((group) => group.cases.some((item) => item.case_id === ui.selectedCase?.case_id))
    || null;
}

function captureWorkbenchScroll() {
  const node = $("#case-scroll");
  if (!node || !ui.selectedCase) return null;
  return {
    caseId: ui.selectedCase.case_id,
    top: node.scrollTop,
    nearBottom: node.scrollHeight - node.scrollTop - node.clientHeight < 72,
    messageCount: ui.chatMessageCount,
  };
}

function selectedCaseSummaryChanged(summary) {
  if (!summary || !ui.selectedCase) return false;
  return ["case_version", "context_hash", "evidence_watermark", "state", "processing_state", "updated_at"]
    .some((key) => String(summary[key] ?? "") !== String(ui.selectedCase[key] ?? ""));
}

let renewingSession = null;
async function renewSession() {
  if (!renewingSession) renewingSession = (async () => {
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch("/", { credentials: "same-origin", cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("本机登录状态暂时无法恢复，草稿仍保留。");
      const documentCopy = new DOMParser().parseFromString(await response.text(), "text/html");
      const token = documentCopy.querySelector('meta[name="csrf-token"]')?.content;
      if (!token) throw new Error("本机登录状态暂时无法恢复，草稿仍保留。");
      csrf = token;
    } finally { clearTimeout(timer); }
  })().finally(() => { renewingSession = null; });
  return renewingSession;
}
async function api(path, options = {}) {
  const { timeoutMs = 12000, ...requestOptions } = options;
  const read = !requestOptions.method || requestOptions.method === "GET";
  let authRenewed = false;
  for (let attempt = 0; ; attempt += 1) {
    const headers = { Accept: "application/json", ...(requestOptions.headers || {}) };
    if (!read) { headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = csrf; }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.max(1000, Number(timeoutMs)));
    try {
      const response = await fetch(path, { credentials: "same-origin", ...requestOptions, headers, signal: requestOptions.signal || controller.signal });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 403 && ["unauthorized", "csrf_rejected"].includes(payload.error_code) && !authRenewed) {
        authRenewed = true; clearTimeout(timer); await renewSession(); continue;
      }
      if (!response.ok) {
        const fallback = response.status === 404 ? "此事项已不存在，请刷新列表；其他窗口仍保留。" : response.status === 403 ? "本机登录状态失效，请重新加载；草稿仍保留。" : "本机服务暂时无法完成请求。";
        const error = new Error(payload.message || fallback); error.code = payload.error_code; error.status = response.status; throw error;
      }
      return payload;
    } catch (original) {
      let error = original;
      if (original?.name === "AbortError" || original instanceof TypeError) {
        error = new Error(read ? "连接中断或读取超时，已保留最后成功内容和草稿。" : "暂未收到保存回执；正在核对原提交，草稿已保留。");
        error.code = original?.name === "AbortError" ? "request_timeout" : "network_unavailable"; error.uncertain = !read;
      }
      if (read && attempt < 1 && (!error.status || error.status >= 500)) {
        await new Promise((resolve) => setTimeout(resolve, 400)); continue;
      }
      throw error;
    } finally { clearTimeout(timer); }
  }
}
function panelFailure(error, at) {
  return `<div class="panel-failure" role="status">${esc(error.message)}${at ? `<small>最后成功读取：${esc(formatTime(at, true))}</small>` : ""}</div>`;
}

function setService(ok, label) {
  $("#service-dot").classList.toggle("error", !ok);
  text("#service-label", label);
}

function setView(view, focus = true, autoSelect = true, preserveSelection = false) {
  if (view === "alerts") view = "needs-user";
  if (!pageMeta[view]) return;
  const targetGroups = view === "archived-questions" ? ui.archiveGroups : ui.needGroups;
  if (["needs-user", "archived-questions"].includes(view) && !preserveSelection) {
    const selectedInTarget = targetGroups.some((group) => group.cases.some((item) => item.case_id === ui.selectedCase?.case_id));
    if (ui.selectedCase && !selectedInTarget) {
      ui.caseRequestId += 1; ui.selectingCaseId = null; clearTimeout(ui.conversationPoll);
      ui.selectedCase = null; ui.conversation = null; ui.groupDetails = []; ui.selectedGroupKey = null;
    }
  }
  ui.view = view; localStorage.setItem("sync-center:last-view", view);
  $$(".page").forEach((node) => {
    const views = String(node.dataset.views || node.dataset.view || "").split(/\s+/);
    node.hidden = !views.includes(view);
  });
  $$('[data-page]').forEach((button) => {
    const active = button.dataset.page === view;
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  workbenchWorkspace.activate(view);
  if (["processes", "locks"].includes(view)) $("#system-nav").open = true;
  text("#page-title", pageMeta[view][0]); text("#page-subtitle", pageMeta[view][1]);
  if (focus) $("#workspace").focus({ preventScroll: true });
  if (view === "overview" && focus) { renderHomeNeeds(); loadAll(); }
  if (["needs-user", "archived-questions"].includes(view)) {
    renderCounts(); renderNeeds();
    if (!ui.selectedCase && !workbenchWorkspace.selected) clearWorkbench(isArchiveView() ? "选择一个已归档主题，查看完整只读记录。" : "选择左侧一项，查看原因和处理办法。");
    if (autoSelect && isArchiveView() && targetGroups.length && !ui.selectedCase) selectGroup(targetGroups[0].groupKey);
    else if (autoSelect && view === "needs-user") workbenchWorkspace.refreshSelected();
  }
}

function renderCounts() {
  const counts = ui.core?.work_counts || {};
  const rawNeeds = ui.panelUpdated.needs ? ui.needs.length : Number(counts.needs_user || 0);
  const needs = ui.needGroups.length || rawNeeds;
  const countLabel = key => ui.core && !ui.panelErrors.core && counts[key] != null ? Number(counts[key]) : "—";
  text("#metric-needs", needs); text("#metric-automatic", countLabel("automatic"));
  text("#metric-resolved", countLabel("resolved_7d")); text("#metric-history", countLabel("history"));
  if (isArchiveView()) {
    const archivedItems = ui.panelUpdated.archived ? ui.archived.length : Number(counts.archived_questions || 0);
    const archivedTopics = ui.archiveGroups.length || archivedItems;
    text("#needs-count-label", `已归档主题 · ${archivedTopics}`);
    text("#needs-item-count", archivedItems > archivedTopics ? `共 ${archivedItems} 个已归档事项，相近事项已归纳` : `共 ${archivedItems} 个已归档事项`);
  } else {
    text("#needs-count-label", `待处理主题 · ${needs}`);
    text("#needs-item-count", rawNeeds > needs ? `共 ${rawNeeds} 个待确认事项，相近事项已归纳` : `共 ${rawNeeds} 个待确认事项`);
  }
  text("#automatic-count", countLabel("automatic"));
  const badge = $("#needs-badge"); badge.textContent = needs; badge.hidden = needs === 0;
  document.title = needs ? `(${needs}) 业务记忆同步中心` : "业务记忆同步中心";
  workbenchWorkspace.renderCounts();
}

function sourceName(source) {
  return source.label || source.name || source.title || ({
    dingtalk_chat: "钉钉聊天", dingtalk_minutes: "钉钉听记", dingtalk_mail: "企业邮件",
    win11: "Win11 资料", wechat: "微信资料",
  })[source.id] || source.id || "同步来源";
}

function homeNeedsCount() {
  const data = ui.homeWorkbench;
  if (!data || ui.homeWorkbenchError) return null;
  const count = Number(data.attention_total);
  return Number.isFinite(count) ? count : null;
}

function renderHomeNeeds() {
  const list = $("#overview-needs-list");
  const data = ui.homeWorkbench;
  const needs = homeNeedsCount();
  text("#metric-needs", needs ?? "—");
  const rows = (data?.groups || data?.items || []).filter(row => row.status !== "closed").slice(0, 3);
  const error = ui.homeWorkbenchError;
  const unavailable = (data?.health || []).some(row => row.available === false);
  const notice = error ? '<p class="panel-failure">待处理事项暂时无法更新；请刷新重试。'+(data ? '<small>下方保留上次读取的事项。</small>' : '')+'</p>'
    : unavailable ? '<p class="panel-failure">部分来源暂时读不到，待处理数量可能不完整。</p>' : '';
  const key = JSON.stringify([rows, error?.message, unavailable, needs]);
  if (list.dataset.renderKey !== key) {
    list.dataset.renderKey = key;
    list.innerHTML = notice + (rows.length ? rows.map(row => `<button type="button" class="attention-row" data-home-issue="${esc(row.group_id || row.issue_id)}"><span class="attention-symbol" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 8v5m0 4h.01"/><circle cx="12" cy="12" r="9"/></svg></span><span class="attention-copy"><small>${esc(row.source_label || "待处理事项")}</small><strong>${esc(row.title || "查看具体问题")}</strong><span>${esc(row.impact || "查看原因与下一步")}</span></span><span class="row-chevron" aria-hidden="true">›</span></button>`).join('')
      : error ? '' : needs === 0 && !unavailable ? '<div class="home-empty"><span aria-hidden="true">✓</span><div><strong>暂无需要你处理的事项</strong><p>同步状态见上方，后续变化会在这里提示。</p></div></div>'
      : '<p class="empty">'+(data ? '打开工作台查看完整事项。' : '正在读取待处理事项…')+'</p>');
    $$('[data-home-issue]', list).forEach(button => button.addEventListener('click', () => workbenchWorkspace.showNeeds(button.dataset.homeIssue)));
  }
  text('#overview-needs-all', needs > 3 ? `查看全部 ${needs} 项 →` : '打开问题工作台 →');
}

function renderHero() {
  const overall = ui.system?.overall || {};
  const overallCode = statusCode(overall);
  const overallTone = statusTone(overallCode);
  const action = ui.activeAction;
  const actionRunning = action && !statusContract.isTerminal(action.display_status || action.status);
  const needs = homeNeedsCount();
  const unknown = !ui.system || Boolean(ui.panelErrors.system);
  let tone = unknown ? "warning" : overallTone;
  let eyebrow = "同步状态";
  let title = overall.label || statusContract.labelFor(overallCode);
  let detail = overall.detail || "各来源的详细状态可在同步来源中查看。";
  if (unknown) {
    eyebrow = "状态待确认"; title = ui.system ? "同步状态暂时无法更新" : "正在读取同步状态";
    detail = ui.system ? "显示的是上次读取的信息，请刷新重试。" : "正在连接本机同步服务。";
  } else if (overallTone === "critical") {
    eyebrow = "需要关注";
  } else if (actionRunning) {
    tone = "syncing"; eyebrow = "正在同步"; title = `${actionTargetLabel(action)}正在同步`;
    detail = action.stage?.label || "任务正在后台运行，你可以继续处理其他事项。";
  } else if (["warning", "neutral"].includes(overallTone)) {
    eyebrow = overallCode === "manual_required" ? "需要处理" : "同步状态";
  } else if (needs > 0) {
    eyebrow = "同步正常"; title = `有 ${needs} 项需要你处理`;
    detail = "查看下方事项，了解原因并决定下一步。";
  } else {
    eyebrow = overallCode === "recovered" ? "已经恢复" : "同步状态";
  }
  $("#hero").className = `hero ${tone}`;
  text("#hero-eyebrow", eyebrow); text("#hero-title", title); text("#hero-detail", detail);
  const icon = ["success"].includes(tone) ? '<path d="m7 12 3 3 7-7"/>' : tone === "syncing" ? '<path d="M18 8a7 7 0 1 0 1 7M18 4v4h-4"/>' : '<path d="M12 7v6m0 4h.01"/>';
  $("#hero-icon").innerHTML = `<svg viewBox="0 0 24 24">${tone === "syncing" ? '' : '<circle cx="12" cy="12" r="9"/>'}${icon}</svg>`;
  $("#hero-next").hidden = !(needs > 0);
  $("#sync-all").className = !unknown && overallTone === "success" && needs === 0 ? "primary-button" : "secondary-button";
}

function renderSyncControl() {
  const button = $("#sync-all"); const action = ui.system?.global_action || {};
  const maintenance = ui.core?.maintenance || {};
  const maintenanceActive = Boolean(maintenance.active || ["active", "entering", "exiting", "blocked"].includes(maintenance.status));
  const running = Boolean(action.running || (ui.activeAction && !statusContract.isTerminal(ui.activeAction.display_status || ui.activeAction.status)));
  const unavailable = !ui.system || Boolean(ui.panelErrors.system) || action.available !== true;
  const disabled = maintenanceActive || running || unavailable;
  const reason = maintenanceActive ? "记忆库维护中" : (running ? "已有同步正在运行" : (unavailable ? action.reason || "当前不满足安全同步条件" : ""));
  button.disabled = disabled;
  button.title = reason || "同步全部来源";
  button.textContent = running ? "正在同步…" : maintenanceActive ? "维护中" : "立即同步";
  text("#sync-availability", reason);
  $("#sync-confirm").disabled = disabled;
}

function actionOutcome(action) {
  const code = statusCode(action?.display_status || action?.status);
  const failedSteps = (action?.steps || []).filter((step) => ["failed", "interrupted"].includes(statusCode(step.status)));
  const skipped = (action?.steps || []).filter((step) => statusCode(step.status) === "skipped");
  if (code === "running") return { title: `${actionTargetLabel(action)}正在同步`, detail: action.stage?.label || "正在准备下一个步骤" };
  if (code === "success") return { title: `${actionTargetLabel(action)}同步成功`, detail: "全部计划步骤已经完成。" };
  if (code === "noop") return { title: `${actionTargetLabel(action)}检查完成`, detail: "没有发现需要写入的新内容。" };
  if (code === "partial") return { title: `${actionTargetLabel(action)}部分完成`, detail: "已完成的结果会保留；仍有步骤需要后续处理。" };
  if (["failed", "interrupted", "notification_failed"].includes(code)) {
    const chain = failedSteps.map((step) => `${step.label || "步骤"}失败`);
    if (skipped.length) chain.push(`后续 ${skipped.length} 个步骤已跳过`);
    return { title: `${actionTargetLabel(action)}同步失败`, detail: chain.length ? `失败链路：${chain.join(" → ")}` : safeErrorLabel(action.error_code) };
  }
  if (code === "deferred") return { title: `${actionTargetLabel(action)}暂未启动`, detail: safeErrorLabel(action.error_code) };
  return { title: `${actionTargetLabel(action)}${statusContract.labelFor(code)}`, detail: action.stage?.label || safeErrorLabel(action.error_code) };
}

function renderAction() {
  const action = ui.activeAction; const card = $("#action-card");
  if (!action || action.run_id === ui.dismissedRunId) { card.hidden = true; return; }
  const code = statusCode(action.display_status || action.status);
  const tone = statusTone(code); const terminal = statusContract.isTerminal(code);
  const outcome = actionOutcome(action);
  card.hidden = false; card.className = `action-card ${tone}`;
  $("#action-state-dot").className = `state-dot ${tone}`;
  text("#action-title", terminal ? `上次同步 · ${statusContract.labelFor(code)}` : outcome.title);
  text("#action-detail", terminal ? `${outcome.detail} 当前状态见上方。` : outcome.detail);
  text("#action-elapsed", formatDuration(action.started_at, action.completed_at));
  $("#action-dismiss").hidden = !terminal;
  const progress = action.progress || {}; const current = Math.max(0, Number(progress.current || 0));
  const total = Math.max(0, Number(progress.total || 0)); const progressNode = $("#action-progress");
  progressNode.max = Math.max(1, total);
  if (total > 0) progressNode.value = Math.min(current, total); else progressNode.removeAttribute("value");
  $("#action-progress-row").hidden = terminal;
  text("#action-progress-label", total > 0 ? `${current} / ${total} ${progress.unit === "items" ? "项" : "步"}` : "准备中");
  text("#action-stage", terminal ? `${statusContract.labelFor(code)} · ${formatTime(action.completed_at || action.updated_at, true)}` : `当前步骤：${action.stage?.label || "正在准备"}`);
  const steps = Array.isArray(action.steps) ? action.steps : [];
  const disclosure = $("#action-disclosure");
  const disclosureKey = action.run_id;
  if (disclosure.dataset.runKey !== disclosureKey) { disclosure.open = false; disclosure.dataset.runKey = disclosureKey; }
  text("#action-disclosure-label", terminal ? `查看 ${steps.length} 步执行结果` : "查看执行步骤");
  $("#action-steps").innerHTML = steps.map((step) => {
    const stepCode = statusCode(step.status); const stepTone = statusTone(stepCode);
    const detail = step.error_code ? safeErrorLabel(step.error_code) : statusContract.labelFor(stepCode);
    return `<li><span class="state-dot ${esc(stepTone)}" aria-hidden="true"></span><span title="${esc(detail)}">${esc(step.label || "同步步骤")}</span><small>${esc(statusContract.labelFor(stepCode))}</small></li>`;
  }).join("");
}

function stopActionPolling() {
  clearInterval(ui.actionPoll); clearInterval(ui.elapsedTimer);
  ui.actionPoll = null; ui.elapsedTimer = null;
}

function rememberAction(runId) {
  ui.trackedRunId = runId || null;
  if (runId) localStorage.setItem("sync-center:tracked-run", runId);
  else localStorage.removeItem("sync-center:tracked-run");
}

async function fetchAction(runId, { refreshOnTerminal = false } = {}) {
  if (!runId) return null;
  const wasTerminal = ui.activeAction ? statusContract.isTerminal(ui.activeAction.display_status || ui.activeAction.status) : false;
  try {
    const action = await api(`/api/actions/${encodeURIComponent(runId)}`);
    if (ui.trackedRunId && ui.trackedRunId !== runId) return null;
    ui.activeAction = action; rememberAction(action.run_id);
    renderHero(); renderAction(); renderSyncControl();
    const terminal = statusContract.isTerminal(action.display_status || action.status);
    if (terminal) {
      stopActionPolling();
      if (refreshOnTerminal && !wasTerminal) setTimeout(() => loadAll({ force: true }), 0);
    }
    return action;
  } catch (error) {
    if (error.code === "action_missing") rememberAction(null);
    return null;
  }
}

function scheduleActionPolling() {
  stopActionPolling();
  const action = ui.activeAction;
  if (!action || statusContract.isTerminal(action.display_status || action.status)) return;
  ui.actionPoll = setInterval(() => {
    if (ui.visible && !document.hidden) fetchAction(ui.trackedRunId, { refreshOnTerminal: true });
  }, 1000);
  ui.elapsedTimer = setInterval(() => {
    if (ui.activeAction) text("#action-elapsed", formatDuration(ui.activeAction.started_at, ui.activeAction.completed_at));
  }, 1000);
}

async function syncActionState() {
  ui.dismissedRunId = localStorage.getItem("sync-center:dismissed-run");
  const active = (ui.core?.active_actions || []).find((item) => item?.run_id);
  const stored = localStorage.getItem("sync-center:tracked-run");
  const latest = latestSyncAction();
  const globalRun = ui.system?.global_action?.running ? ui.system.global_action.run_id : null;
  const candidate = active?.run_id || globalRun || latest?.run_id || stored || null;
  if (!candidate || (candidate === ui.dismissedRunId && !active)) {
    ui.activeAction = null; rememberAction(null); stopActionPolling(); renderAction(); return;
  }
  if (candidate !== ui.dismissedRunId) localStorage.removeItem("sync-center:dismissed-run");
  rememberAction(candidate);
  await fetchAction(candidate);
  scheduleActionPolling();
}

function dismissActionResult() {
  if (!ui.activeAction?.run_id) return;
  ui.dismissedRunId = ui.activeAction.run_id;
  localStorage.setItem("sync-center:dismissed-run", ui.dismissedRunId);
  rememberAction(null); ui.activeAction = null; stopActionPolling(); renderAction(); renderHero(); renderSyncControl();
}

function renderSystemOverview() {
  const system = ui.system || {}; const overall = system.overall || {};
  const code = statusCode(overall.display_status || overall);
  const overallLabel = $("#overall-state");
  overallLabel.className = `status-label ${statusTone(code)}`;
  text("#overall-state", overall.label || overall.detail || statusContract.labelFor(code));
  const sources = Array.isArray(system.sources) ? system.sources : [];
  const tones = sources.map(source => statusTone(statusCode(source.display_status || {})));
  const sourceAttention = tones.filter(tone => ["critical", "warning"].includes(tone)).length;
  const sourceFailures = tones.filter(tone => tone === "critical").length;
  text("#source-summary", !ui.system || ui.panelErrors.system ? "来源状态待确认" : `${sources.length} 个来源${sourceFailures ? ` · ${sourceFailures} 个同步异常` : sourceAttention ? ` · ${sourceAttention} 个等待或待确认` : ""}`);
  $("#overview-sources").classList.toggle("has-attention", sourceAttention > 0);
  const priorOpen = new Set($$("#source-list details[open]").map(node => node.dataset.sourceId));
  const sourceMarkup = sources.length ? sources.map((source) => {
    const display = source.display_status || {}; const sourceCode = statusCode(display);
    const sourceTone = statusTone(sourceCode);
    const hasAlert = (ui.alerts?.alerts || []).some(alert => alert.status === "open" && alert.source_id === source.id);
    let detail = display.detail || display.label || source.detail || "状态已读取";
    if (source.id === "win11" && Number(source.remaining_batches) > 0) detail += `；剩余 ${source.remaining_batches} 批`;
    const check = source.listener_diagnostic;
    const diagnostic = check?.diagnostic_sessions_used ? `监听诊断 ${check.diagnostic_sessions_used}/1，验收 ${check.acceptance_sessions_used}/1` : '';
    return `<article class="source-row"><span class="state-dot ${esc(sourceTone)}" aria-hidden="true"></span><div class="source-copy"><div class="source-heading"><strong>${esc(sourceName(source))}</strong><span class="status-pill ${esc(sourceTone)}">${esc(display.label || statusContract.labelFor(sourceCode))}</span></div><p>${esc(detail)}</p><div class="source-times"><span>最近成功 <time>${esc(formatTime(source.last_success, true))}</time></span><span>最近尝试 <time>${esc(formatTime(source.last_attempt, true))}</time></span></div>${diagnostic ? `<details data-source-id="${esc(source.id)}" ${priorOpen.has(source.id) ? 'open' : ''}><summary>技术详情</summary><p>${esc(diagnostic)}</p></details>` : ''}</div><button type="button" class="secondary-button source-issue-link" data-jump="needs-user" ${hasAlert ? '' : 'hidden'}>查看待处理</button></article>`;
  }).join("") : '<p class="empty">来源状态暂时不可用，但不会被误显示为零。</p>';
  if ($("#source-list").dataset.renderKey !== sourceMarkup) {
    $("#source-list").dataset.renderKey = sourceMarkup;
    $("#source-list").innerHTML = sourceMarkup;
    $$('#source-list [data-jump]').forEach(button => button.addEventListener('click', () => workbenchWorkspace.showNeeds()));
  }
  const pipelines = Array.isArray(system.pipelines) ? system.pipelines : [];
  $("#process-list").innerHTML = pipelines.length ? pipelines.map((item) => {
    const display = item.display_status || {};
    return `<article class="diagnostic-card"><h3>${esc(sourceName(item))}</h3><p>${esc(display.detail || display.label || "运行状态已读取")}</p><dl><dt>当前状态</dt><dd>${esc(display.label || statusCode(display))}</dd><dt>最近尝试</dt><dd>${esc(formatTime(item.last_attempt, true))}</dd><dt>最近成功</dt><dd>${esc(formatTime(item.last_success, true))}</dd></dl></article>`;
  }).join("") : '<p class="empty">没有可显示的进程诊断。</p>';
  const lockItems = Array.isArray(system.locks) ? system.locks : []; const maintenance = ui.core?.maintenance || {};
  const lockCards = [{ label: "维护模式", detail: maintenance.active ? "维护中，写入已暂停" : "未启用，可按正常规则写入", state: maintenance.status || (maintenance.active ? "active" : "inactive") }, ...lockItems];
  $("#lock-list").innerHTML = lockCards.map((item) => `<article class="diagnostic-card"><h3>${esc(item.label || item.name || "写入锁")}</h3><p>${esc(item.detail || item.reason || "状态已读取")}</p><dl><dt>状态</dt><dd>${esc(item.state || item.status || "已读取")}</dd></dl></article>`).join("");
}

function renderRecentResolved() {
  const items = ui.history.filter((item) => ["plan_confirmed", "resolved", "archived"].includes(item.state)).slice(0, 4);
  $("#recent-resolved").innerHTML = items.length ? items.map((item) => `<article class="quiet-row"><div><strong>${esc(item.title)}</strong><p>${esc(item.assessment || item.evidence_summary || "已退出当前工作量")}</p></div><time>${esc(formatTime(item.resolved_at || item.updated_at, true))}</time></article>`).join("") : '<p class="empty">尚无新的自动收口记录。</p>';
}

function clearWorkbench(message = "系统会继续在后台吸收后续证据。") {
  const detail = $("#needs-detail"); detail.className = "detail-pane empty-detail"; detail.textContent = message;
  const conversation = $("#case-conversation"); conversation.className = "conversation-pane empty-conversation";
  conversation.innerHTML = '<div class="empty-conversation-copy"><strong>选择一个主题后开始对话</strong><span>问题说明、处理动作和 GPT 对话会在右侧连续呈现。</span></div>';
}

function renderNeeds() {
  if (!isArchiveView()) { workbenchWorkspace.renderList(); return; }
  const list = $("#needs-list");
  const listError = ui.panelErrors[isArchiveView() ? "archived" : "needs"];
  if (listError && !workbenchGroups().length) { list.innerHTML = panelFailure(listError, ui.panelUpdated[isArchiveView() ? "archived" : "needs"]); return; }

  const groups = workbenchGroups();
  list.setAttribute("aria-label", isArchiveView() ? "已归档主题" : "待处理主题");
  if (!groups.length) {
    list.innerHTML = isArchiveView() ? '<p class="empty">还没有已归档问题。</p>' : '<p class="empty">当前没有需要你处理的问题。</p>';
    if (!ui.selectedCase) clearWorkbench(isArchiveView() ? "还没有已归档问题；归档不会删除问题、证据或对话。" : undefined); return;
  }
  const selectingGroup = groups.find((group) => group.cases.some((item) => item.case_id === ui.selectingCaseId))?.groupKey;
  list.innerHTML = groups.map((group) => {
    const selected = (selectingGroup || ui.selectedGroupKey) === group.groupKey;
    const labels = [...new Set(group.cases.map(decisionLabel))];
    const summary = isArchiveView() ? (group.cases[0].attention_reason || "已处理，完整记录保留") : group.cases.length > 1
      ? `${group.cases.length} 个${isArchiveView() ? "已归档" : "待确认"}事项 · ${labels.join("、")}`
      : (group.cases[0].decision_question || fallbackDecisionQuestion(group.cases[0]));
    const matters = [...new Set(group.cases.map(representativeMatter))].join("；");
    return `<button type="button" class="case-row" role="option" data-group-key="${esc(group.groupKey)}" aria-selected="${selected}"><strong>${esc(group.subject)}</strong><span>${esc(summary)}</span><small>${esc(matters)}</small></button>`;
  }).join("");
  $$('[data-group-key]', list).forEach((button) => button.addEventListener("click", () => selectGroup(button.dataset.groupKey)));
}

async function selectGroup(groupKey) {
  const group = workbenchGroups().find((item) => item.groupKey === groupKey);
  if (!group?.cases.length) return;
  const current = group.cases.find((item) => item.case_id === ui.selectedCase?.case_id);
  await selectCase((current || group.cases[0]).case_id, Boolean(current), groupKey);
}

async function selectCase(caseId, preserveDraft = false, groupKey = null, options = {}) {
  workbenchWorkspace.showBusiness();
  const sameCase = ui.selectedCase?.case_id === caseId;
  const scrollState = options.scrollState || (sameCase ? captureWorkbenchScroll() : null);
  if (!sameCase && ui.selectedCase && $("#case-input")) localStorage.setItem(`sync-center:case-draft:${ui.selectedCase.case_id}`, $("#case-input").value);
  const localDraft = sameCase ? $("#case-input")?.value : null;
  const requestId = ++ui.caseRequestId; ui.selectingCaseId = caseId;
  const group = workbenchGroups().find((item) => item.groupKey === groupKey || item.cases.some((candidate) => candidate.case_id === caseId));
  ui.selectedGroupKey = group?.groupKey || caseId;
  if (!sameCase) {
    ui.selectedCase = [...ui.needs, ...ui.archived, ...ui.history].find((item) => item.case_id === caseId) || null;
    ui.conversation = null; ui.groupDetails = [];
    $("#needs-detail").innerHTML = '<div class="workbench-loading">正在读取问题…</div>';
    $("#case-conversation").innerHTML = '<div class="workbench-loading">正在恢复对话和草稿…</div>';
  }
  renderNeeds();
  const [detailResult, chatResult] = await Promise.allSettled([
    api(`/api/reconciliation/${encodeURIComponent(caseId)}`).then((value) => {
      if (requestId === ui.caseRequestId) { ui.selectedCase = value; ui.groupDetails = [value]; ui.caseDetailError = null; renderCaseDetail(); }
      return value;
    }),
    api(`/api/reconciliation/${encodeURIComponent(caseId)}/conversation`).then((value) => {
      if (requestId === ui.caseRequestId) {
        const draft = localStorage.getItem(`sync-center:case-draft:${caseId}`);
        if (draft !== null) value.draft = draft;
        ui.conversation = value; ui.caseConversationError = null;
        if (ui.selectedCase) renderConversation({ scrollState });
      }
      return value;
    }),
  ]);
  if (requestId !== ui.caseRequestId) return;
  ui.selectingCaseId = null;
  ui.caseDetailError = detailResult.status === "rejected" ? detailResult.reason : null;
  ui.caseConversationError = chatResult.status === "rejected" ? chatResult.reason : null;
  if (detailResult.status === "fulfilled") {
    ui.selectedCase = detailResult.value; ui.groupDetails = [detailResult.value];
    ui.panelUpdated[`case:${caseId}`] = new Date().toISOString();
  }
  if (chatResult.status === "fulfilled") {
    const conversation = chatResult.value;
    const browserDraft = localStorage.getItem(`sync-center:case-draft:${caseId}`);
    if (browserDraft !== null) conversation.draft = browserDraft;
    else if (localDraft !== null && ui.localDraftDirty) conversation.draft = localDraft;
    ui.conversation = conversation; normalizeAiProfile(conversation.ai_configuration);
    ui.panelUpdated[`chat:${caseId}`] = new Date().toISOString();
  }
  if (ui.selectedCase) renderCaseDetail();
  if (ui.selectedCase && ui.conversation) renderConversation({ scrollState });
  for (const [selector, error, key] of [["#needs-detail", ui.caseDetailError, `case:${caseId}`], ["#case-conversation", ui.caseConversationError, `chat:${caseId}`]]) {
    if (!error) continue;
    const node = $(selector); if (!sameCase && selector === "#case-conversation") node.innerHTML = "";
    node.insertAdjacentHTML("afterbegin", panelFailure(error, ui.panelUpdated[key]) + `<button type="button" class="text-button" data-retry-case="${esc(caseId)}">重新读取此部分</button>`);
  }
  $$('[data-retry-case]').forEach((button) => button.addEventListener("click", () => selectCase(caseId, true)));
  renderNeeds(); scheduleConversationPoll();
}

function actionDisabledReason() {
  if (ui.caseDetailError || ui.caseConversationError) return "当前部分内容未能核验，请重新读取后再操作";
  if (ui.core?.maintenance?.active) return "维护中，当前只允许查看历史";
  if (ui.conversation?.read_only) return "问题已结案，对话和动作只读";
  if (ui.conversation?.active_message) return "请等待 GPT 完成本轮回答";
  if (!ui.conversation?.completed_exchanges) return "请先与 GPT 完成至少一轮对话";
  return "";
}

function guidanceFor(item) {
  const guidance = item?.decision_guidance || {};
  const authorization = item?.case_kind === "authorization" || item?.dimension_key === "technical_signoff";
  return {
    question: guidance.question || item?.decision_question || fallbackDecisionQuestion(item),
    systemFinding: guidance.system_finding || item?.assessment || "系统已经整理现有证据，但没有得到足够明确的结论。",
    systemCanHelp: guidance.system_can_help || "系统可以整理证据、比较方案并继续追踪后续变化。",
    ownerMustDecide: guidance.owner_must_decide || (authorization
      ? "你需要确认是否已有有效签署；若没有，需要指定责任人和补齐条件。"
      : "你需要说明倾向采用的方向、优先级和前提条件，或者明确延期与继续观察的条件。"),
    whyNotAutomatic: guidance.why_not_automatic || item?.manual_reason || "这个选择会改变后续行动，系统不能自动代选。",
    afterDecision: guidance.after_decision || "形成的方案只记录处理路径，不会自动升级为公司批准或已确认事实。",
  };
}

async function switchGroupMember(caseId) {
  if (!caseId || caseId === ui.selectedCase?.case_id) return;
  await saveDraft();
  await selectCase(caseId, false, ui.selectedGroupKey);
}

function renderCaseDetail() {
  const item = ui.selectedCase; const conversation = ui.conversation || { read_only: true }; if (!item) return;
  const pane = $("#needs-detail"); pane.className = "detail-pane";
  const evidence = Array.isArray(item.evidence) ? item.evidence : [];
  const group = currentNeedGroup();
  const members = ui.groupDetails.length ? ui.groupDetails : [item];
  const guidance = guidanceFor(item);
  const archived = item.state === "archived" && item.disposition === "archive";
  const archiveMethod = ({ reply_saved: "回复已保存", legacy_reply: "按历史回复补记处置", owner_instruction: "按你本次说明登记", new_version: "已被新版替代", reply_after_answer: "回复后自动归档" })[item.archive_trigger] || "手动归档";
  const archiveTime = item.disposition_at || item.resolved_at || item.updated_at;
  const disabledReason = actionDisabledReason(); const actionDisabled = Boolean(disabledReason);
  const plan = String(conversation.latest_proposed_plan || ""); const proposal = conversation.memory_proposal;
  const memberSelector = members.length > 1 ? `<section class="group-summary"><div><span class="eyebrow">已归纳为一个主题</span><h3>这个主题包含 ${members.length} 个${archived ? "分别归档" : "需要分别确认"}的事项</h3><p>相近事项只在左侧出现一次；每项仍保留独立对话和审计，避免一个业务选择误处理另一项技术签署。</p></div><div class="member-switcher" role="group" aria-label="选择${archived ? "已归档" : "待确认"}事项">${members.map((member) => `<button type="button" data-member-case-id="${esc(member.case_id)}" aria-pressed="${member.case_id === item.case_id}"><span>${esc(decisionLabel(member))}</span><small>${esc(representativeMatter(member))}</small></button>`).join("")}</div></section>` : "";
  const archiveButton = !archived && item.state === "manual_required"
    ? `<button type="button" class="archive-button" data-archive-case ${ui.core?.maintenance?.active || ui.caseDetailError || ui.caseConversationError ? "disabled" : ""}>归档</button>`
    : "";
  const archiveReceipt = archived ? `<section class="archive-receipt"><div><span class="eyebrow">已退出当前工作量</span><strong>${esc(archiveMethod)}</strong></div><dl><dt>归档时间</dt><dd>${esc(formatDate(archiveTime))}</dd><dt>归档范围</dt><dd>仅当前具体事项</dd></dl><p>${esc(item.attention_reason || "问题、证据和对话完整保留。")}</p></section>` : "";
  const actionDock = archived ? "" : `<section class="action-dock" aria-label="问题处理动作"><div><h3>下一步怎么处理</h3><p>${esc(disabledReason || "选择前会先展示影响范围；没有默认选项，也不会超时代选。")}</p><button type="button" class="chat-jump-button" data-jump-chat>到下方和 GPT 讨论</button></div><div class="action-buttons">
      <button type="button" class="primary-button" data-case-action="resolve_plan" ${actionDisabled || !plan ? "disabled" : ""} title="${esc(!plan ? "GPT 尚未形成可确认方案" : disabledReason)}">形成明确方案</button>
      <button type="button" class="secondary-button" data-case-action="defer" ${actionDisabled ? "disabled" : ""} title="${esc(disabledReason)}">延期处理</button>
      <button type="button" class="secondary-button" data-case-action="continue_automatic" ${actionDisabled ? "disabled" : ""} title="${esc(disabledReason)}">继续自动观察</button>
      ${proposal ? `<button type="button" class="memory-button" data-case-action="write_memory" ${actionDisabled ? "disabled" : ""} title="${esc(disabledReason)}">补充到业务记忆</button>` : ""}
    </div></section>`;
  pane.innerHTML = `
    <div class="detail-heading"><div class="detail-title-row"><div><span class="eyebrow">问题主题</span><h2>${esc(group?.subject || item.subject)}</h2></div>${archiveButton}</div><div class="detail-tags"><span>${esc(decisionLabel(item))}</span><span>${members.length > 1 ? `${members.length} 个${archived ? "已归档" : "待确认"}事项` : `1 个${archived ? "已归档" : "待确认"}事项`}</span></div></div>
    ${archiveReceipt}
    <p class="material-time">资料核对：${esc(formatTime(item.materials_checked_at, true))}</p>
    ${(item.related_context || []).length ? `<section class="related-context"><h3>相关资料与处置依据</h3>${item.related_context.map((row) => `<p><strong>${esc(row.title)}</strong> <span class="status-pill neutral">${esc(row.artifact_kind === "technical_checklist" ? "技术确认表" : row.artifact_kind === "catalogue" ? "目录册" : "资料")}</span> ${esc(row.version || "")}<br>${esc(row.reason || row.source_label || "")}${row.case_id ? `<br><button type="button" class="text-button" data-related-case="${esc(row.case_id)}">查看这条资料索引</button>` : ""}</p>`).join("")}</section><div id="related-source-preview"></div>` : ""}
    ${memberSelector}
    <section class="decision-brief"><span class="eyebrow">${archived ? "归档前的问题" : "这次需要你决定"}</span><h3>${esc(guidance.question)}</h3><p>${esc(guidance.ownerMustDecide)}</p></section>
    <div class="decision-boundaries">
      <section><h3>系统已经判断了什么</h3><p>${esc(guidance.systemFinding)}</p><small>${esc(guidance.systemCanHelp)}</small></section>
      <section class="owner-boundary"><h3>为什么不能自动替你决定</h3><p>${esc(guidance.whyNotAutomatic)}</p><small>${esc(guidance.afterDecision)}</small></section>
    </div>
    <section class="detail-block"><h3>现有证据摘要</h3><p>${esc(item.evidence_summary)}</p></section>
    <details class="boundary-details"><summary>查看完整证据边界</summary><p>${esc(item.evidence_boundary)}</p></details>
    ${evidence.length ? `<details class="evidence-details"><summary>查看代表性证据 · ${evidence.length}</summary><ul class="evidence-list">${evidence.slice(0, 8).map((row) => `<li><strong>${esc(row.title)}</strong><p>${esc(row.summary)}</p><span>${esc(row.source_label)} · ${esc(formatTime(row.observed_at, true))}</span></li>`).join("")}</ul></details>` : ""}
    ${plan ? `<section class="plan-card"><span class="eyebrow">GPT 形成的待确认方案</span><p>${esc(plan)}</p><small>确认后只代表这个问题有了处理方案，不代表业务事实或审批成立。</small></section>` : ""}
    ${actionDock}`;
  $$('[data-member-case-id]', pane).forEach((button) => button.addEventListener("click", () => switchGroupMember(button.dataset.memberCaseId)));
  $("[data-jump-chat]", pane)?.addEventListener("click", () => setWorkbenchTab("chat"));
  $$('[data-case-action]', pane).forEach((button) => button.addEventListener("click", () => beginAction(button.dataset.caseAction)));
  $("[data-archive-case]", pane)?.addEventListener("click", archiveSelectedCase);
  $$('[data-related-case]', pane).forEach((button) => button.addEventListener("click", async () => {
    let target = $("#related-source-preview", pane);
    if (!target) { target = document.createElement("div"); target.id = "related-source-preview"; button.parentElement.appendChild(target); }
    try {
      const source = await api(`/api/reconciliation/${encodeURIComponent(button.dataset.relatedCase)}`);
      target.innerHTML = `<section class="related-context"><strong>${esc(source.display_title || source.title)}</strong><p>${esc(source.evidence_summary)}</p><small>资料索引不代表已批准或已发送。</small><ul>${(source.evidence || []).map((item) => `<li>${esc(item.title)}<p>${esc(item.summary)}</p><small>${esc(item.source_label)} · ${esc(formatTime(item.observed_at, true))}</small></li>`).join("")}</ul></section>`;
    } catch (error) { target.innerHTML = panelFailure(error); }
  }));

}

async function archiveSelectedCase() {
  if (!ui.selectedCase || !ui.conversation || ui.core?.maintenance?.active) return;
  const caseId = ui.selectedCase.case_id;
  const request = ui.archiveRequest?.caseId === caseId
    ? ui.archiveRequest
    : { caseId, idempotencyKey: crypto.randomUUID() };
  ui.archiveRequest = request;
  const button = $("[data-archive-case]");
  if (button) { button.disabled = true; button.textContent = "归档中…"; }
  try {
    await api(`/api/reconciliation/${encodeURIComponent(caseId)}/archive`, {
      method: "POST",
      body: JSON.stringify({
        case_version: ui.selectedCase.case_version,
        context_hash: ui.selectedCase.context_hash,
        conversation_hash: ui.conversation.conversation_hash,
        idempotency_key: request.idempotencyKey,
      }),
    });
    ui.archiveRequest = null;
    announce("当前事项已归档；同一主题的其他待确认事项仍保留在工作台。");
    await loadAll({ force: true, refreshSelected: true });
    await selectCase(caseId, false, null, { silent: true });
  } catch (error) {
    await loadAll({ force: true, refreshSelected: true });
    const archived = ui.archived.some((item) => item.case_id === caseId);
    if (archived) {
      ui.archiveRequest = null;
      await selectCase(caseId, false, null, { silent: true });
      announce("当前事项已经归档，完整对话已打开。");
    } else {
      announce(`归档失败：${error.message}`);
      const currentButton = $("[data-archive-case]");
      if (currentButton) { currentButton.disabled = false; currentButton.textContent = "归档"; }
    }
  }
}

function conversationStateLabel(value) {
  return ({ clarifying: "正在澄清", proposal_ready: "方案可确认", waiting_evidence: "等待外部证据" })[value] || "正在澄清";
}

function messageMarkup(message, readOnly = false) {
  const isUser = message.author === "user";
  const state = message.conversation_state ? `<span class="message-state">${esc(conversationStateLabel(message.conversation_state))}</span>` : "";
  const profile = message.ai_profile ? `<span class="message-profile">${esc(profileSummary(message.ai_profile))}</span>` : "";
  const questions = !readOnly && Array.isArray(message.focused_questions) && message.focused_questions.length
    ? `<div class="focused-questions"><strong>接下来需要你确认</strong><ol>${message.focused_questions.map((question) => `<li>${esc(question)}</li>`).join("")}</ol></div>` : "";
  const progress = message.status === "running" ? "GPT 正在思考…" : (message.status === "pending" ? "等待 GPT 接手…" : "");
  const failure = message.status === "failed" ? `<div class="message-failure"><span>本轮回答失败；你的回复与处置记录已保存。</span>${`<button type="button" data-retry-message="${esc(message.message_id)}">重试回答</button>`}</div>` : "";
  const cancelled = message.status === "cancelled" ? '<div class="message-cancelled">归档时已安全终止本轮 GPT 写回。</div>' : "";
  return `<article class="conversation-message ${isUser ? "from-user" : "from-ai"}"><header><strong>${isUser ? "你" : "GPT"}</strong>${state}${profile}<time>${esc(formatTime(message.created_at))}</time></header><p>${esc(message.content)}</p>${progress ? `<div class="message-progress">${esc(progress)}</div>` : ""}${questions}${failure}${cancelled}</article>`;
}

function proposalMarkup(proposal) {
  if (!proposal) return "";
  const statusLabel = proposal.fact_status === "owner_statement" ? "本人陈述" : "待权威复核";
  return `<article class="memory-proposal-card"><header><span class="eyebrow">可补充到业务记忆</span><span class="fact-status ${esc(proposal.fact_status)}">${esc(statusLabel)}</span></header><strong>${esc(proposal.title)}</strong><p>${esc(proposal.text)}</p><dl><dt>归属页面</dt><dd>${esc(proposal.target_page)}</dd><dt>内容归属</dt><dd>${esc(proposal.source_attribution)}</dd><dt>证据边界</dt><dd>${esc(proposal.evidence_boundary)}</dd></dl><small>这只是候选预览；仍需点击“补充到业务记忆”并再次确认。</small></article>`;
}

function renderConversation({ scrollState = null } = {}) {
  const conversation = ui.conversation; const item = ui.selectedCase;
  if (!conversation || !item) return;
  const pane = $("#case-conversation"); pane.className = "conversation-pane";
  codexHandoff.mount(pane, {
    id: item.case_id, base: `/api/reconciliation/${encodeURIComponent(item.case_id)}`,
    draftKey: `sync-center:case-draft:${item.case_id}`, draft: conversation.draft || "",
    disabled: Boolean(conversation.read_only || ui.core?.maintenance?.active || ui.caseDetailError || ui.caseConversationError),
    readOnly: conversation.read_only,
    disabledReason: ui.caseDetailError?.message || ui.caseConversationError?.message || (ui.core?.maintenance?.active ? "维护期间回复保留在本机，暂缓转交。" : null),
    reload: () => selectCase(item.case_id, true),
    history: codexHandoff.history(conversation)
  });
  ui.chatCaseId = item.case_id; ui.chatMessageCount = (conversation.messages || []).length;
}

async function saveDraft() {
  if (!ui.selectedCase || !ui.conversation || ui.core?.maintenance?.active || ui.conversation.read_only) return;
  const item = ui.selectedCase; const caseId = item.case_id;
  const content = $("#case-input")?.value ?? ui.conversation.draft ?? "";
  try {
    await api(`/api/reconciliation/${encodeURIComponent(caseId)}/draft`, { method: "PUT", body: JSON.stringify({ content, case_version: item.case_version, context_hash: item.context_hash }) });
    if (localStorage.getItem(`sync-center:case-draft:${caseId}`) === content) localStorage.removeItem(`sync-center:case-draft:${caseId}`);
    if (ui.selectedCase?.case_id === caseId && $("#case-input")?.value === content) ui.localDraftDirty = false;
  } catch (error) { if (error.code !== "maintenance_active") announce(`草稿保留在本机：${error.message}`); }
}


function scheduleConversationPoll() {
  clearTimeout(ui.conversationPoll);
}

async function beginAction(action) {
  if (!ui.selectedCase || !ui.conversation) return;
  if (action === "defer") {
    $("#defer-form").reset(); $("#defer-date").disabled = true; text("#defer-error", ""); $("#defer-dialog").showModal(); return;
  }
  const params = action === "write_memory" ? { proposal_id: ui.conversation.memory_proposal?.proposal_id } : {};
  await previewAction(action, params);
}

async function previewAction(action, params) {
  try {
    const preview = await api(`/api/reconciliation/${encodeURIComponent(ui.selectedCase.case_id)}/preview`, {
      method: "POST", body: JSON.stringify({ action, params, context_hash: ui.selectedCase.context_hash, case_version: ui.selectedCase.case_version, conversation_hash: ui.conversation.conversation_hash }),
    });
    ui.preview = preview;
    const titles = { resolve_plan: "确认按这份方案结案？", defer: "确认延期处理？", continue_automatic: "确认继续自动观察？", write_memory: "确认写入这条业务记忆候选？" };
    text("#choice-title", titles[action] || "确认这个处理动作？"); text("#choice-effect", preview.effect || "该动作会按预览应用。"); text("#choice-error", "");
    const detail = $("#choice-preview-detail"); let markup = "";
    if (action === "resolve_plan") markup = `<dl><dt>将保存的方案</dt><dd>${esc(preview.action_params?.plan || "")}</dd><dt>对话校验</dt><dd>${esc(preview.conversation_hash)}</dd></dl>`;
    else if (action === "defer") markup = `<dl><dt>重新提醒时间</dt><dd>${esc(formatDate(preview.action_params?.defer_until))}</dd><dt>提前返回条件</dt><dd>证据发生实质变化</dd></dl>`;
    else if (action === "write_memory" && ui.conversation.memory_proposal) {
      const proposal = ui.conversation.memory_proposal;
      markup = `<dl><dt>准确文本</dt><dd>${esc(proposal.text)}</dd><dt>归属页面</dt><dd>${esc(proposal.target_page)}</dd><dt>来源归属</dt><dd>${esc(proposal.source_attribution)}</dd><dt>事实状态</dt><dd>${esc(proposal.fact_status)}</dd><dt>证据边界</dt><dd>${esc(proposal.evidence_boundary)}</dd></dl>`;
    }
    detail.innerHTML = markup; detail.hidden = !markup; $("#choice-dialog").showModal();
  } catch (error) { announce(`预览失败：${error.message}`); if (ui.selectedCase) await selectCase(ui.selectedCase.case_id, true); }
}

function selectedDeferDate() {
  const choice = $('input[name="defer-choice"]:checked'); if (!choice) return null;
  if (choice.value === "custom") {
    const raw = $("#defer-date").value; if (!raw) return null;
    const date = new Date(`${raw}T09:00:00`); return Number.isNaN(date.getTime()) ? null : date;
  }
  const date = new Date(); date.setDate(date.getDate() + Number(choice.value)); return date;
}

async function confirmDefer(event) {
  event.preventDefault(); const date = selectedDeferDate();
  if (!date || date <= new Date()) { text("#defer-error", "请选择一个未来日期。"); return; }
  $("#defer-dialog").close(); await previewAction("defer", { defer_until: date.toISOString(), reason: "用户在问题工作台明确选择延期" });
}

async function applyChoice(event) {
  event.preventDefault(); if (!ui.preview) return;
  const button = $("#choice-confirm"); button.disabled = true; text("#choice-error", "");
  try {
    const result = await api(`/api/reconciliation/${encodeURIComponent(ui.preview.case_id)}/apply`, {
      method: "POST", body: JSON.stringify({ preview_token: ui.preview.preview_token, idempotency_key: crypto.randomUUID() }),
    });
    $("#choice-dialog").close(); ui.preview = null;
    const messages = { resolve_plan: "方案已保存并移入历史；没有把方案当成业务事实或审批。", defer: "问题已延期；到期或证据提前变化时会重新出现。", continue_automatic: "问题已回到自动观察；完整对话仍然保留。", write_memory: "记忆候选已通过受治理写入和验证；GPT 回复没有被当成事实来源。" };
    announce(messages[result.action] || "处理动作已应用。");
    if (result.action !== "write_memory") { ui.selectedCase = null; ui.conversation = null; }
    await loadAll({ force: true });
  } catch (error) { text("#choice-error", error.message); } finally { button.disabled = false; }
}

function renderAutomatic() {
  const list = $("#automatic-list");
  list.innerHTML = ui.automatic.length ? ui.automatic.map((item) => {
    const state = String(item.processing_state || "idle");
    const status = ({ running: "正在核验", queued: "等待下一轮", blocked: "等待条件", failed: "稍后重试", deferred: "已延期", idle: "持续观察" })[state] || "持续观察";
    const footer = item.state === "deferred" ? `将在 ${formatDate(item.defer_until)} 重新出现` : (item.next_action || "有相关新证据时自动再次核验");
    return `<article class="automatic-card"><header><h3>${esc(item.title)}</h3><span class="process">${esc(status)}</span></header><p>${esc(item.assessment || item.evidence_summary)}</p><footer>${esc(footer)}</footer></article>`;
  }).join("") : '<p class="empty">当前没有自动处理事项。</p>';
}

function renderActivity() {
  const items = ui.events?.events || ui.core?.activity || [];
  $("#activity-list").innerHTML = items.length ? items.map((item) => `<li><span class="dot ${esc(statusTone(item.status))}" aria-hidden="true"></span><div><strong>${esc(item.title || item.label || "后台任务")}</strong><span>${esc(item.detail || item.summary || item.status || "")}</span></div><time>${esc(formatTime(item.time || item.updated_at, true))}</time></li>`).join("") : '<li><div></div><div><strong>暂无运行记录</strong></div></li>';
}

function renderAlerts() { alertWorkspace.renderList(); }

function deliveryLabel(channel, delivery) {
  const status = String(delivery?.status || "not_requested");
  const policyLabel = ({ policy_no_material_change: "本轮无需重复通知", policy_obsolete_event: "已跳过过期通知", policy_recovery_recurred: "故障再次出现，暂不发恢复通知", policy_evidence_missing: "恢复依据待核对", policy_stabilizing: "恢复情况观察中" })[delivery?.error_code];
  if (policyLabel) return `${channel} ${policyLabel}`;
  return ({
    requested: `${channel} 已请求（未证实显示）`, visible: `${channel} 通知中心有记录（非已读）`,
    delivered: delivery?.receipt_confirmed ? `${channel} 已取得消息回执` : `${channel} 历史送达未核实`, pending: `${channel} 待投递`,
    retrying: `${channel} 将重试一次`, sending: `${channel} 投递中`, failed: `${channel} 汇报失败`,
    permission_off: `${channel} 通知未授权`, blocked: `${channel} 被安全门阻断`,
    not_needed: `${channel} 本轮无需重复通知`, uncertain: `${channel} 结果不确定`, superseded: `${channel} 已停止重发`,
    unverified: `${channel} 缺少消息回执`, permission_required: `${channel} 待启用通知权限`,
  })[status] || "";
}

async function confirmMailReadSafety(stillUnread) {
  await alertWorkspace.confirmMail(stillUnread);
}

function renderHistory() {
  const query = $("#history-search").value.trim().toLowerCase();
  const items = ui.history.filter((item) => !query || `${item.title} ${item.subject} ${item.dimension} ${item.assessment}`.toLowerCase().includes(query));
  $("#history-list").innerHTML = items.length ? items.map((item) => {
    const browsable = /^case-[0-9a-f]{20}$/.test(String(item.case_id || ""));
    const body = `<div><strong>${esc(item.title)}</strong><p>${esc(item.assessment || item.evidence_summary || "历史记录")}</p></div><span>${esc(item.resolution_reason || formatTime(item.resolved_at || item.updated_at, true))}</span>`;
    return browsable ? `<button type="button" class="quiet-row history-case" data-history-case-id="${esc(item.case_id)}">${body}</button>` : `<article class="quiet-row">${body}</article>`;
  }).join("") : '<p class="empty">没有匹配的历史事项。</p>';
  $$('[data-history-case-id]').forEach((button) => button.addEventListener("click", async () => {
    await selectCase(button.dataset.historyCaseId); setView("needs-user", true, false, true); setWorkbenchTab("chat");
  }));
}

function renderMaintenance() {
  const maintenance = ui.core?.maintenance || {};
  const active = Boolean(maintenance.active || ["active", "entering", "exiting", "blocked"].includes(maintenance.status));
  $("#maintenance-banner").hidden = !active;
}

function renderAll({ renderWorkbench = true, scrollState = null } = {}) {
  const preserved = scrollState || captureWorkbenchScroll();
  refreshNeedGroups(); renderCounts(); renderHomeNeeds(); renderSystemOverview(); renderHero(); renderAction(); renderSyncControl(); renderRecentResolved(); renderNeeds();
  if (renderWorkbench && ui.selectedCase && ui.conversation) { renderCaseDetail(); renderConversation({ scrollState: preserved }); }
  renderAutomatic(); renderActivity(); renderAlerts(); renderHistory(); renderMaintenance();
  text("#updated-at", `更新于 ${formatTime(ui.core?.generated_at)}`);
}

function announce(message) { text("#live-status", message); }

async function loadAll({ force = false, refreshSelected = false } = {}) {
  if (ui.loading) {
    if (force || refreshSelected || ui.workbenchQuery !== workbenchWorkspace.queryKey) ui.reloadQueued = true;
    return;
  }
  ui.loading = true; $("#refresh").classList.add("loading");
  const workbenchQuery = workbenchWorkspace.queryKey;
  ui.workbenchQuery = workbenchQuery;
  try {
  const selectedId = ui.selectedCase?.case_id;
  const scrollState = captureWorkbenchScroll();
  const priorMaintenance = Boolean(ui.core?.maintenance?.active);
  const endpoints = [["core", "/api/state"], ["system", `/api/system-state${force ? "?force=1" : ""}`], ["needs", "/api/reconciliation?scope=needs_user"], ["archived", "/api/reconciliation?scope=archived_questions"], ["automatic", "/api/reconciliation?scope=automatic"], ["history", "/api/reconciliation?scope=history"], ["events", "/api/system-events?limit=60"], ["alerts", "/api/alerts?limit=500"], ["workbench", workbenchWorkspace.listPath()]];
  const reuseHomeWorkbench = workbenchWorkspace.isAttentionScope;
  const alertsRequest = api("/api/alerts?limit=500", { timeoutMs: 12000 });
  if (!reuseHomeWorkbench) endpoints.push(["homeWorkbench", null]);
  const results = await Promise.allSettled(endpoints.map(async ([key, path]) => {
    const value = key === "workbench" ? await workbenchWorkspace.fetchList(alertsRequest)
      : key === "homeWorkbench" ? await workbenchWorkspace.fetchAttention(3, alertsRequest)
      : key === "alerts" ? await alertsRequest : await api(path, { timeoutMs: 12000 });
    if (key === "workbench" && workbenchQuery !== workbenchWorkspace.queryKey) return;
    if (["needs", "archived", "automatic", "history"].includes(key)) ui[key] = value.items || []; else ui[key] = value;
    delete ui.panelErrors[key]; ui.panelUpdated[key] = new Date().toISOString();
    if (key === "homeWorkbench" || (key === "workbench" && reuseHomeWorkbench)) { ui.homeWorkbench = value; ui.homeWorkbenchError = null; ui.homeUpdatedAt = new Date().toISOString(); renderHomeNeeds(); renderHero(); workbenchWorkspace.renderCounts(); }
    if (key === "alerts") renderAlerts();
    if (key === "needs" || key === "archived" || key === "workbench") { refreshNeedGroups(); renderCounts(); renderNeeds(); }
    return value;
  })); let failures = 0;
  results.forEach((result, index) => {
    const key = endpoints[index][0];
    if (key === "workbench" && workbenchQuery !== workbenchWorkspace.queryKey) return;
    if (result.status === "rejected") { failures += 1; ui.panelErrors[key] = result.reason; if (key === "homeWorkbench" || (key === "workbench" && reuseHomeWorkbench)) ui.homeWorkbenchError = result.reason; return; }
    delete ui.panelErrors[key]; ui.panelUpdated[key] = new Date().toISOString(); if (["needs", "archived", "automatic", "history"].includes(key)) ui[key] = result.value.items || []; else ui[key] = result.value;
  });
  refreshNeedGroups();
  const selectedArchived = selectedId ? ui.archived.find((item) => item.case_id === selectedId) : null;
  const selectedNeeds = selectedId ? ui.needs.find((item) => item.case_id === selectedId) : null;
  const selectedSummary = selectedId
    ? [...ui.needs, ...ui.archived, ...ui.history].find((item) => item.case_id === selectedId)
    : null;
  const stillExists = Boolean(selectedSummary);
  const maintenanceChanged = priorMaintenance !== Boolean(ui.core?.maintenance?.active);
  const shouldRefreshSelected = stillExists
    && (force || refreshSelected || maintenanceChanged || selectedCaseSummaryChanged(selectedSummary));
  // A saved reply can leave the list without closing its current conversation.

  await syncActionState().catch((error) => { failures += 1; announce(error.message); });
  renderAll({ renderWorkbench: !stillExists, scrollState });
  if (shouldRefreshSelected) await selectCase(selectedId, true, null, { silent: true, scrollState });
  else if (!ui.selectedCase && isArchiveView() && workbenchGroups().length) await selectGroup(workbenchGroups()[0].groupKey);
  setService(failures === 0, failures ? "部分界面状态暂不可用" : "界面连接正常");
  announce(failures ? "部分状态读取失败，未误显示为零。" : "状态已更新。");
  if (ui.view === "needs-user") await workbenchWorkspace.refreshSelected();
  } catch (error) { announce(`刷新暂未完成：${error.message}`); }
  finally {
    ui.loading = false; $("#refresh").classList.remove("loading");
    if (ui.reloadQueued) { ui.reloadQueued = false; loadAll(); }
  }
}

async function startSync(event) {
  event.preventDefault(); const button = $("#sync-confirm"); button.disabled = true;
  try {
    const result = await api("/api/actions/run", { method: "POST", body: JSON.stringify({ target: "all" }) });
    $("#sync-dialog").close();
    if (result.run_id) {
      localStorage.removeItem("sync-center:dismissed-run"); ui.dismissedRunId = null;
      rememberAction(result.run_id); await fetchAction(result.run_id); scheduleActionPolling();
    }
    announce(result.status === "started" ? "同步已经开始，进度会持续显示。" : "已有同步正在运行，已经恢复任务进度。");
    await loadAll({ force: true });
  } catch (error) { announce(`无法启动同步：${error.message}`); } finally { button.disabled = false; }
}

function applyAppearance(value) {
  if (value === "system") document.documentElement.removeAttribute("data-theme"); else document.documentElement.dataset.theme = value;
  localStorage.setItem("sync-center:appearance", value);
}

function bind() {
  alertWorkspace.bind();
  workbenchWorkspace.bind();
  $$('[data-page]').forEach((button) => button.addEventListener("click", () => button.dataset.page === "needs-user" ? workbenchWorkspace.showNeeds() : setView(button.dataset.page)));
  $$('[data-jump]').forEach((button) => button.addEventListener("click", () => button.dataset.jump === "needs-user" ? workbenchWorkspace.showNeeds() : setView(button.dataset.jump)));
  $("#refresh").addEventListener("click", () => loadAll({ force: true })); $("#sync-all").addEventListener("click", () => $("#sync-dialog").showModal());
  $("#sync-confirm").addEventListener("click", startSync); $("#choice-confirm").addEventListener("click", applyChoice); $("#defer-confirm").addEventListener("click", confirmDefer);
  $("#action-dismiss").addEventListener("click", dismissActionResult);
  $("#hero-next").addEventListener("click", () => workbenchWorkspace.showNeeds());
  $("#overview-needs-all").addEventListener("click", () => workbenchWorkspace.showNeeds());
  $("#mail-safety-changed").addEventListener("click", () => confirmMailReadSafety(false));
  $("#mail-safety-confirm").addEventListener("click", () => confirmMailReadSafety(true));
  $$('input[name="defer-choice"]').forEach((radio) => radio.addEventListener("change", () => {
    $("#defer-date").disabled = radio.value !== "custom" || !radio.checked;
    if (radio.value === "custom" && radio.checked) $("#defer-date").focus();
  }));
  $("#history-search").addEventListener("input", renderHistory);
  const appearance = localStorage.getItem("sync-center:appearance") || "system"; $("#appearance").value = appearance; applyAppearance(appearance);
  $("#appearance").addEventListener("change", (event) => applyAppearance(event.target.value));
  const previousView = localStorage.getItem("sync-center:last-view");
  if (previousView && (pageMeta[previousView] || previousView === "alerts")) setView(previousView, false, false, true);
  document.addEventListener("visibilitychange", () => { ui.visible = !document.hidden; if (ui.visible) loadAll({ refreshSelected: true }); });
}

window.setDashboardVisibility = (visible) => { ui.visible = Boolean(visible); if (ui.visible) loadAll({ refreshSelected: true }); };

bind(); loadAll({ force: true });
setInterval(() => { if (ui.visible && !document.hidden && !$("dialog[open]")) loadAll(); }, 15000);
