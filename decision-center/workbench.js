"use strict";

// One root-cause group is shown by default; every member keeps its own evidence and actions.
const workbenchWorkspace = (() => {
  const labels = { attention: "待处理", needs_user: "需你处理", automatic: "系统处理中", waiting: "等待条件", closed: "已结束", all: "全部" };
  const state = { scope: "attention", selected: null, detail: null, chat: null, request: 0,
    detailError: null, chatError: null, busy: false, actionError: "", poll: null, draftTimer: null,
    saving: false, sending: null, pendingChoice: null, groupIds: null, detailKey: null, chatKey: null, listKey: null, visibleCount: 200, restoreSelection: true };
  const draftKey = (id) => "sync-center:issue-draft:" + id;
  function readStored(key) {
    try { return JSON.parse(localStorage.getItem(key) || "null"); }
    catch (_) { return null; }
  }
  const submissionKey = (id, kind) => `sync-center:issue-${kind}:${id}`;
  const endpoint = (id, suffix = "") => `/api/workbench/${encodeURIComponent(id)}${suffix}`;
  const listPath = () => `/api/workbench?scope=${state.scope === "attention" ? "all" : state.scope}&limit=${Math.min(500, state.visibleCount)}${state.groupIds ? "&ids=" + encodeURIComponent([...state.groupIds].join(",")) : ""}`;
  function ledgerRecovery(value) {
    return value.ledger_recovery || value.observation?.ledger_recovery || (value.members || []).map(m => m.observation?.ledger_recovery).find(Boolean);
  }
  function automaticLedger(value) {
    return value.error_code === "supervisor_runtime_error_pending" && value.attention_state === "automatic" && ledgerRecovery(value)?.requires_user !== true;
  }
  function scheduleReceiptWait(value) {
    const ledger = ledgerRecovery(value);
    return value.status === "open" && ledger?.requires_user === false
      && ledger.pending_incidents > 0 && ledger.missing_paths?.length === 1
      && ledger.missing_paths[0] === "mark_all_due"
      && ["writes_pending", "operation_receipt_pending"].includes(ledger.check_blocker);
  }
  function recoveryReason(value) {
    const supervisor = supervisorRecovery(value);
    if (supervisor) return supervisor.reason_label;
    if (!scheduleReceiptWait(value)) return value.reason;
    return "仍有 " + ledgerRecovery(value).pending_incidents + " 条历史调度登记异常等待验证。下一次正常唤醒、接通电源或维护结束时，系统会登记同步计划，再自动核验；定时检查本身不会重放同步。";
  }
  function recoveryBoundary(value) {
    if (supervisorRecovery(value)) return '启动请求只计为已请求恢复；取得故障之后的新运行凭据才算后台恢复。来源同步和业务记忆另行验收。';
    return scheduleReceiptWait(value)
      ? "等待正常调度登记的真实成功回执及与当前资料匹配的完整校验。无需为消除提示反复点击立即同步。"
      : value.recovery_boundary || value.observation?.recovery_boundary || "尚未取得与本问题对应的成功回执，不能自动判定恢复。";
  }
  function supervisorRecovery(value) {
    return (value.alert_health || []).map(a => a.supervisor_recovery).find(Boolean) || value.supervisor_recovery || value.observation?.supervisor_recovery || (value.members || []).map(m => m.observation?.supervisor_recovery).find(Boolean);
  }
  function attemptLabel(value) {
    const supervisor = supervisorRecovery(value);
    if (supervisor) return supervisor.request_count == null ? (supervisor.request_budget_used ? '启动请求是否执行尚未确认' : '历史恢复动作次数未记录') : `主动恢复请求 ${supervisor.request_count} 次`;
    const recovery = ledgerRecovery(value);
    if (recovery?.event_verification_count != null) return `本事件核验 ${recovery.event_verification_count} 次；实际修复 ${recovery.actual_repair_count ?? "未知"} 次`;
    if (recovery) return `历史数据库尝试 ${recovery.automatic_attempts ?? "未知"} 次`;
    return value.kind === "attachment" ? "尝试次数未完整记录" : `${value.observation?.item_attempt_count || 0} 次累计尝试`;
  }
  function attentionQueue(workbench, alerts) {
    const groups = workbench.groups || workbench.items || [];
    const allAlerts = alerts.alerts || [];
    const openAlerts = allAlerts.filter(row => row.status === "open" && (!row.group_primary_id || row.group_primary_id === row.alert_id));
    const selected = new Map();
    for (const row of groups) {
      if (row.status === "open" && row.attention_state === "needs_user") selected.set(row.group_id || row.issue_id, row);
    }
    for (const alert of openAlerts) {
      const memberIds = new Set([alert.alert_id, ...(alert.group_member_ids || [])]);
      const linked = new Set([alert, ...allAlerts.filter(row => memberIds.has(row.alert_id))].flatMap(row => row.issues || []).map(row => row.issue_id));
      const linkedGroups = groups.filter(row => row.status === "open" && [row.issue_id, ...(row.active_member_issue_ids || row.member_issue_ids || [])].some(id => linked.has(id)));
      const matches = linkedGroups.filter(row => (row.attention_state === "needs_user" || ledgerRecovery(row)?.requires_user === true));
      // Linked issues own their classification; an old alert cannot re-promote waiting/automatic work.
      if (!matches.length && linkedGroups.length) continue;
      for (const row of matches) {
        const id = row.group_id || row.issue_id;
        const prior = selected.get(id) || row;
        selected.set(id, { ...prior, attention_alert: true,
          attention_critical: prior.attention_critical || ["critical", "error"].includes(alert.effective_state || alert.severity) });
      }
      // A missing link must remain visible until the issue can be read again.
      if (!matches.length) {
        const id = "alert-" + alert.alert_id;
        selected.set(id, { group_id: id, issue_id: id, kind: "alert", status: "open", title: alert.title,
          source_label: "同步状态", impact: alert.detail, attention_state: "needs_user", attention_alert: true,
          attention_critical: ["critical", "error"].includes(alert.effective_state || alert.severity),
          affected_count: 1, member_issue_ids: [], first_seen: alert.first_seen, alert,
          codex_handoff: alert.codex_handoff });
      }
    }
    const rows = [...selected.values()].sort((a, b) => Number(Boolean(b.attention_critical || b.severity === "error")) - Number(Boolean(a.attention_critical || a.severity === "error"))
      || String(a.first_seen || a.last_seen || "").localeCompare(String(b.first_seen || b.last_seen || ""))
      || String(a.group_id || a.issue_id).localeCompare(String(b.group_id || b.issue_id)));
    return { ...workbench, attention_rows: rows, attention_total: rows.length,
      attention_critical: rows.some(row => row.attention_critical || row.severity === "error"),
      group_total: rows.length, affected_total: rows.reduce((sum, row) => sum + (row.affected_count || 1), 0),
      health: [...(workbench.health || []), ...(alerts.has_more ? [{ scope: "alerts", available: false }] : [])] };
  }
  function attentionPage(value, limit) {
    return { ...value, groups: value.attention_rows.slice(0, limit),
      group_has_more: value.attention_rows.length > limit, group_next_offset: limit };
  }
  async function fetchAttention(limit = state.visibleCount, alertsRequest = null) {
    const [first, alerts] = await Promise.all([api("/api/workbench?scope=all&limit=500", { timeoutMs: 30000 }), alertsRequest || api("/api/alerts?limit=500", { timeoutMs: 30000 })]);
    let value = first;
    while (value.group_has_more ?? value.has_more) {
      const offset = value.group_next_offset ?? value.next_offset;
      const next = await api(`/api/workbench?scope=all&limit=500&offset=${offset}`, { timeoutMs: 30000 });
      if ((next.group_has_more ?? next.has_more) && (next.group_next_offset ?? next.next_offset) <= offset) throw new Error("待处理列表未完整读取，请刷新重试。");
      value = { ...next, items: [...value.items, ...next.items], groups: [...(value.groups || value.items), ...(next.groups || next.items)] };
    }
    return attentionPage(attentionQueue(value, alerts), limit);
  }
  async function fetchList(alertsRequest = null) {
    const scope = state.scope; const path = listPath(); const visibleCount = state.visibleCount;
    if (scope === "attention") return { ...await fetchAttention(visibleCount, alertsRequest), scope };
    let value = await api(path, { timeoutMs: 30000 });
    while ((value.group_has_more ?? value.has_more) && (value.groups || value.items).length < visibleCount) {
      const next = await api(path + "&offset=" + (value.group_next_offset ?? value.next_offset));
      value = { ...next, items: [...value.items, ...next.items],
        groups: [...(value.groups || value.items), ...(next.groups || next.items)] };
    }
    return { ...value, scope };
  }
  const stateLabel = (row) => row.status === "closed"
    ? ({ recovered: "已恢复", disposed: "已处置 · 原件保留", superseded: "已被新问题替代" })[row.resolution_kind] || "已结束"
    : row.media_cards?.length ? row.attention_label || "等待选择" : row.video_waiting ? "等待指令" : scheduleReceiptWait(row) ? "等待正常调度回执" : labels[row.attention_state] || "状态待核验";
  function keepDraft() {
    const input = $("#issue-input");
    if (input && state.selected && (state.chat || input.value || input.dataset.dirty === "true")) localStorage.setItem(draftKey(state.selected), input.value);
  }
  function showBusiness() {
    keepDraft(); clearTimeout(state.poll); clearTimeout(state.draftTimer);
    state.selected = null; state.request += 1;
    localStorage.removeItem("sync-center:open-issue");
    $("#issue-window").hidden = true; $("#needs-detail").hidden = false; $("#case-conversation").hidden = false;
  }
  async function renderMediaHistory() {
    const container = $("#media-history");
    container.hidden = ui.view !== "archived-questions";
    if (container.hidden) return;
    try {
      let value = await api("/api/workbench?scope=closed&limit=500");
      let items = value.items || [];
      while (value.group_has_more) {
        value = await api("/api/workbench?scope=closed&limit=500&offset=" + value.group_next_offset);
        items.push(...(value.items || []));
      }
      if (ui.view !== "archived-questions") return;
      const cards = [...new Map(items.flatMap(x => x.media_cards || []).filter(x => ["ignored","completed"].includes(x.state)).map(x => [x.hold_id,x])).values()];
      container.innerHTML = cards.length ? `<h2>媒体处理记录</h2><p class="muted">已处理和已忽略的媒体保留在这里；忽略可以恢复。</p><div class="media-decision-list">${cards.map(mediaCard).join("")}</div>` : "";
      container.hidden = !cards.length;
      if (cards.length && !ui.archived?.length) $(".case-workbench").hidden = true;
      $$("[data-media-action]",container).forEach(button => button.addEventListener("click",async () => {
        const card = cards.find(x => x.hold_id === button.dataset.mediaId);
        const key = submissionKey(card.issue_id,"restore-"+card.hold_id);
        const payload = readStored(key) || {action:"restore_media",action_id:crypto.randomUUID(),hold_id:card.hold_id,version:card.version,revision:card.revision};
        localStorage.setItem(key,JSON.stringify(payload));button.disabled = true;
        try {
          const result = await api(endpoint(card.issue_id,"/actions"),{method:"POST",body:JSON.stringify(payload)});
          if(result.saved) localStorage.removeItem(key);
          await loadAll(); await renderMediaHistory();
        } catch(error) {
          if(["issue_stale","action_unavailable"].includes(error.code)) localStorage.removeItem(key);
          button.disabled = false;announce(error.message);
          const note=document.createElement("p");note.className="error-text";note.textContent=error.message;button.parentNode.append(note);
        }
      }));
    } catch(error) { container.innerHTML = `<p class="error-text">媒体处理记录暂时读不到：${esc(error.message)}</p>`; }
  }
  function activate(view) {
    $(".case-workbench").hidden = false;
    $("#media-history").hidden = view !== "archived-questions";
    if (view === "archived-questions") renderMediaHistory();
    $("#workbench-filters").hidden = view !== "needs-user";
    if (view === "archived-questions") showBusiness();
    if (view !== "needs-user") { keepDraft(); clearTimeout(state.poll); }
    if (view === "needs-user" && state.selected) {
      $("#issue-window").hidden = false; $("#needs-detail").hidden = true; $("#case-conversation").hidden = true;
    }
  }
  function renderCounts() {
    const known = ui.workbench && !ui.panelErrors.workbench;
    const counts = known ? (ui.workbench.group_counts || ui.workbench.counts) : null;
    const attention = ui.homeWorkbench;
    const needs = attention && !ui.homeWorkbenchError ? attention.attention_total : null;
    text("#metric-needs", needs ?? "—"); text("#needs-badge", needs ?? "!");
    $("#needs-badge").hidden = needs === 0;
    $("#needs-badge").classList.toggle("critical", Boolean(needs > 0 && attention.attention_critical));
    document.title = needs ? `(${needs}) 业务记忆同步中心` : "业务记忆同步中心";
    $$("[data-issue-scope]").forEach((button) => {
      const scope = button.dataset.issueScope;
      const count = scope === "attention" ? needs ?? "—" : counts ? (scope === "all" ? Object.values(counts).reduce((a, b) => a + b, 0) : counts[scope]) : "—";
      button.textContent = `${labels[scope]} ${count}`;
      button.setAttribute("aria-pressed", String(scope === state.scope));
    });
  }
  function renderList() {
    if (ui.view !== "needs-user") return;
    renderCounts();
    const value = ui.workbench?.scope && ui.workbench.scope !== state.scope ? null : ui.workbench; const error = ui.panelErrors.workbench;
    let rows = value?.groups || value?.items || [];
    if (state.groupIds && !value?.groups) rows = rows.filter((row) => state.groupIds.has(row.issue_id));
    text("#needs-count-label", state.groupIds ? "这条告警关联的问题" : labels[state.scope] + "的问题");
    const groupTotal = value?.group_total ?? value?.total ?? "—";
    const affectedTotal = value?.affected_total ?? value?.total ?? "—";
    text("#needs-item-count", state.groupIds ? `关联 ${groupTotal} 类 · 涉及 ${affectedTotal} 项` : `共 ${groupTotal} 类 · 涉及 ${affectedTotal} 项；逐项保留处理结果`);
    $("#issue-filter-reset").hidden = !state.groupIds;
    const key = JSON.stringify([state.scope, rows, error?.message, value?.health, value?.has_more, state.selected, ui.selectedCase?.case_id]);
    if (key === state.listKey) return; state.listKey = key;
    const list = $("#needs-list"); list.setAttribute("aria-label", "具体问题"); list.dataset.scope = value ? state.scope : "loading";
    const health = (value?.health || []).some((row) => !row.available)
      ? '<p class="panel-failure">部分来源暂时读不到。保留已知问题，缺失状态不会当作零。</p>' : "";
    list.innerHTML = (error ? panelFailure(error, ui.panelUpdated.workbench) : "") + health + (rows.length
      ? rows.map((row) => `<button type="button" class="case-row issue-row" role="option" data-issue-id="${esc(row.group_id || row.issue_id)}" aria-selected="${(row.group_id || row.issue_id) === state.selected || Boolean(row.case_id && row.case_id === ui.selectedCase?.case_id)}"><small>${esc(row.source_label)} · ${esc(stateLabel(row))}</small><strong>${esc(row.title)}</strong>${row.object_label ? `<span class="issue-object-name">${esc(row.object_label)}</span>` : ""}<span>${esc(row.impact || "查看原因与下一步")}</span>${codexHandoff.badge(row.codex_handoff, row.status === "closed")}</button>`).join("")
      : `<p class="empty">${value ? "此范围暂无问题；其他范围的记录仍保留。" : "正在读取问题…"}</p>`)
      + ((value?.group_has_more ?? value?.has_more) ? '<button type="button" class="secondary-button issue-more">继续载入问题</button>' : "");
    $$("[data-issue-id]", list).forEach((button) => button.addEventListener("click", () => open(button.dataset.issueId)));
    $(".issue-more", list)?.addEventListener("click", async (event) => {
      event.target.disabled = true;
      try {
        if (state.scope === "attention") {
          state.visibleCount += 200; ui.workbench = attentionPage(value, state.visibleCount); renderList(); return;
        }
        const next = await api(listPath() + "&offset=" + (value.group_next_offset ?? value.next_offset));
        ui.workbench = { ...next, items: [...value.items, ...next.items],
          groups: [...(value.groups || value.items), ...(next.groups || next.items)] };
        state.visibleCount = (ui.workbench.groups || ui.workbench.items).length; renderList();
      } catch (failure) { announce(failure.message); event.target.disabled = false; }
    });
  }
  async function open(id, refresh = false) {
    if (!id) return;
    if (id.startsWith("alert-")) {
      const row = (ui.homeWorkbench?.attention_rows || []).find(item => item.issue_id === id);
      if (!row) return;
      keepDraft(); clearTimeout(state.poll); clearTimeout(state.draftTimer); clearTimeout(ui.conversationPoll);
      state.request += 1; state.selected = id; ui.caseRequestId += 1; ui.selectedCase = null; ui.conversation = null;
      setView("needs-user", !refresh, false, true); activate("needs-user"); renderList();
      $("#issue-detail").innerHTML = `<h2>${esc(row.title)}</h2><p>${esc(row.impact)}</p><p class="panel-failure">关联详情暂时无法读取，这一项仍保留在待处理。刷新后再查看处理操作。</p><button type="button" class="secondary-button" id="attention-retry">刷新待处理</button>`;
      $("#issue-chat").innerHTML = "";
      $("#attention-retry").addEventListener("click", () => showNeeds());
      return;
    }
    if (id.startsWith("business-")) {
      const item = (ui.workbench?.groups || ui.workbench?.items || []).find((row) => (row.group_id || row.issue_id) === id);
      showBusiness();
      if (item?.status === "closed") setView("archived-questions", true, false, true);
      else setView("needs-user", true, false, true);
      await selectCase(id.slice("business-".length)); return;
    }
    if (!/^(?:issue|group)-[a-f0-9]{24}$/.test(id)) return;
    const same = id === state.selected; keepDraft();
    if (ui.selectedCase && $("#case-input")) localStorage.setItem("sync-center:case-draft:" + ui.selectedCase.case_id, $("#case-input").value);
    clearTimeout(ui.conversationPoll); clearTimeout(state.poll); clearTimeout(state.draftTimer);
    ui.caseRequestId += 1; ui.selectedCase = null; ui.conversation = null; ui.selectingCaseId = null;
    if (!same) {
      state.detail = null; state.chat = null; state.detailError = null; state.chatError = null; state.actionError = "";
      state.detailKey = null; state.chatKey = null;
      $("#issue-detail").innerHTML = '<p class="empty">正在读取具体问题…</p>';
      $("#issue-chat").innerHTML = '<p class="empty">正在恢复本问题的对话…</p>';
    }
    state.selected = id; localStorage.setItem("sync-center:open-issue", id);
    if (!refresh) history.replaceState(null, "", "#workbench/" + id);
    setView("needs-user", !refresh, false, true); activate("needs-user");
    const request = ++state.request; renderList();
    const results = await Promise.allSettled([api(endpoint(id), { timeoutMs: 30000 })]);
    if (request !== state.request) return;
    if (results[0].status === "fulfilled") { state.detail = results[0].value; state.detailError = null; }
    else state.detailError = results[0].reason;
    renderDetail();
    if (state.detail?.media_cards?.length) { state.chat = null; state.chatError = null; }
    else {
      const chat = await Promise.allSettled([api(endpoint(id, "/conversation"))]);
      if (request !== state.request) return;
      if (chat[0].status === "fulfilled") { state.chat = chat[0].value; state.chatError = null; }
      else state.chatError = chat[0].reason;
    }
    renderChat(); schedulePoll();
  }
  async function resolveAlertTarget(alert) {
    const ids = new Set([alert.alert_id, ...(alert.group_member_ids || [])]);
    const members = (ui.alerts?.alerts || []).filter((row) => ids.has(row.alert_id));
    const linked = [...new Map([alert, ...members].flatMap((row) => row.issues || []).map((row) => [row.issue_id, row])).values()];
    const active = linked.filter((row) => row.status === "open");
    const targets = active.length ? active : linked;
    if (!targets.length) throw new Error("关联记录暂未生成或读取失败。告警保留，请重新读取；不会当作已解决。");
    const groupIds = new Set(targets.map((row) => row.issue_id));
    const scope = active.length ? "all" : "closed";
    const listing = await api(`/api/workbench?scope=${scope}&limit=${Math.min(500, state.visibleCount)}&ids=${encodeURIComponent([...groupIds].join(","))}`);
    const group = (listing.groups || []).find((row) => (row.member_issue_ids || []).includes(targets[0].issue_id));
    const id = group?.group_id || targets[0].issue_id;
    return { id, base: id.startsWith("business-") ? `/api/reconciliation/${encodeURIComponent(id.slice("business-".length))}` : endpoint(id), groupIds, scope, listing };
  }
  async function fromAlert(alert) {
    try {
      const target = await resolveAlertTarget(alert);
      state.groupIds = target.groupIds; state.scope = target.scope;
      ui.workbench = target.listing; delete ui.panelErrors.workbench;
      await open(target.id);
    } catch (error) { text("#alert-list-health", error.message); }
  }
  function nextCheck(value) {
    if (value.video_waiting) return "等待你的明确指令；普通同步和重试不会放行视频";
    if (value.status === "closed") return "本问题已结束，无待执行检查";
    const supervisor = supervisorRecovery(value);
    if (supervisor) return supervisor.next_check_at ? formatTime(supervisor.next_check_at, true) : "下次后台检查时间尚未记录，继续核验";
    if (scheduleReceiptWait(value)) return "后台继续核验；恢复取决于下一次正常调度，不按旧重试时间重复执行";
    const recovery = ledgerRecovery(value);
    const at = value.observation?.next_due || recovery?.next_retry_at || recovery?.next_check_at;
    if (!at) return "暂无下一次执行时间；先看下方操作和前置条件";
    return new Date(at) <= new Date() ? "原计划已到期，等待条件满足或调度续跑" : formatTime(at, true);
  }
  function recoverySummary(value) {
    if (value.status === "closed") return "";
    const ledger = ledgerRecovery(value);
    if (ledger) {
      const paths = {mark_all_due:"同步计划登记",mark_delivery:"通知结果登记",claim_delivery:"通知发送资格登记",initialize:"数据库初始化",update_source:"同步来源状态登记",update_runtime:"后台运行状态登记",open_alert:"异常登记",index_state:"原索引状态检查",unknown_failure_path:"尚未定位的失败步骤"};
      const counts = ledger.event_verification_count != null
        ? `工作台操作 ${esc(value.total_attempts ?? value.observation?.attempt_count ?? 0)} 次；本事件核验 ${esc(ledger.event_verification_count)} 次；实际修复 ${esc(ledger.actual_repair_count ?? "未知")} 次；历史数据库尝试 ${esc(ledger.historical_database_attempts ?? "未知")} 次`
        : `历史数据库尝试 ${esc(ledger.automatic_attempts ?? "未知")} 次；本事件核验和实际修复次数尚无独立记录`;
      return `<div class="issue-reading"><p>${counts}；仍待核验 ${esc(ledger.pending_incidents ?? "未知")} 条历史异常。</p><p>稳定观察：${esc(Math.floor((ledger.observation_seconds || 0)/60))} 分钟；已记录 ${esc(ledger.native_cycles ?? 0)} 个自然运行周期。</p>${ledger.full_validation_current ? `<p>原索引检查与当前完整校验已通过，等待自然运行观察完成。</p>` : ledger.missing_paths?.length ? `<p>待取得实际成功回执：${esc(ledger.missing_paths.map(p => paths[p] || "原失败步骤").join("、"))}。</p>` : ""}${ledger.evidence_gaps?.length ? `<p>恢复证据缺口：${esc(ledger.evidence_gaps.join("；"))}。</p>` : ""}<p class="muted">核验不等于修复；历史数据库尝试不计入本事件修复次数，测试通过不代替实际恢复回执。</p></div>`;
    }
    const recovery = value.observation?.automatic_recovery || (value.members || []).map(m => m.observation?.automatic_recovery).find(Boolean);
    if (!recovery?.enabled) return "";
    const labels = { pending: "已安排自动恢复", repairing: "正在自动恢复", waiting: "等待恢复条件", needs_user: "需要确认权限或处理要求", needs_maintenance: "已保留证据，等待维护", idle: "暂无待恢复项" };
    return `<div class="issue-reading"><p>${esc(labels[recovery.phase] || "正在核验恢复状态")}</p><p>当前邮件来源的新恢复策略累计：实际处理 ${esc(recovery.actual_attempts ?? 0)} 次，来源探测 ${esc(recovery.probe_count ?? 0)} 次。</p><p class="muted">这些次数不包含上线前的历史尝试，也不等于本组的工作台操作次数。${recovery.next_due ? "下次自动检查：" + esc(formatTime(recovery.next_due, true)) : ""}</p></div>`;
  }
  function heldMediaSummary(items) {
    if (!items?.length) return "";
    const names={not_downloaded:"未下载",existing_original:"已有原件，未新增复制",embedded_original:"随原文档保留，未单独展开"};
    return `<div class="issue-reading"><h3>视频待命清单</h3>${items.map(x => `<p><strong>${esc(x.name || "名称未知")}</strong><br>所属：${esc(x.parent_label || "未知")} · 大小：${esc(x.size_bytes == null ? "未知" : (x.size_bytes / 1024 / 1024).toFixed(2) + " MB")}<br>${esc(names[x.original_state] || "原件状态未知")} · 等待指令<br><small>发现：${esc(formatTime(x.first_seen,true))}</small></p>`).join("")}</div>`;
  }
  function readingSummary(reading) {
    if (!reading) return "";
    return `<div class="issue-reading"><p>${esc(reading.mail_status)}</p><p>${esc(reading.summary)}</p>${reading.reviewed_at ? `<p class="muted">提取回执：${esc(formatTime(reading.reviewed_at, true))} · ${esc(reading.receipt_source || "已核实回执")}</p>` : ""}</div>`;
  }
  function diagnosticsSummary(diagnostics) {
    const members = diagnostics.members || [{ kind: diagnostics.kind, reading: diagnostics.observation?.reading, status: diagnostics.status, error_code: diagnostics.observation?.error_code }];
    return `<div class="issue-diagnostic-summary"><p>${esc(diagnostics.title || "诊断结果")} · ${esc(diagnostics.status === "closed" ? "已结束" : "仍待处理")}</p>${members.map((member, index) => `<div><strong>第 ${index + 1} 项</strong>${member.reading ? readingSummary(member.reading) : `<p>${esc(member.error_code === "partial_static" ? "内容提取不完整" : safeErrorLabel(member.error_code || "unknown"))} · ${esc(member.status === "closed" ? "已结束" : "仍待处理")}</p>`}${member.kind === "attachment" ? '<p class="muted">逐次尝试次数未完整记录，不能记作 0 次。</p>' : ""}</div>`).join("")}<details><summary>查看脱敏原始字段</summary><pre class="issue-diagnostics">${esc(JSON.stringify(diagnostics, null, 2))}</pre></details></div>`;
  }
  function mediaCard(card) {
    const result = card.result || {};
    const disabled = state.busy || state.detailError || ui.core?.maintenance?.active;
    const size = card.size_bytes == null ? "大小未知" : (card.size_bytes / 1024 / 1024).toFixed(2) + " MB";
    const button = (action,label,enabled) => `<button type="button" class="${action === "process_media" ? "primary-button" : "secondary-button"}" data-media-action="${action}" data-media-id="${esc(card.hold_id)}" ${disabled || !enabled ? "disabled" : ""}>${label}</button>`;
    return `<article class="media-decision-card" data-media-card="${esc(card.hold_id)}" tabindex="-1"><div class="media-card-heading"><span class="eyebrow">${esc(card.kind_label)} · ${esc(card.state_label)}</span><h3>${esc(card.display_name)}</h3></div>
      <p class="media-source">${esc(card.source_description)}</p><p class="muted">${esc(card.observed_at ? formatTime(card.observed_at,true) : "来源时间未知")} · ${esc(size)}</p>
      ${card.original_name && card.original_name !== card.display_name ? `<details><summary>原始文件名</summary><p>${esc(card.original_name)}</p></details>` : ""}
      ${card.unavailable_reason ? `<p class="media-limitation">${esc(card.unavailable_reason)}</p>` : ""}
      ${["queued","running"].includes(card.state) ? `<p role="status">${esc(card.stage)}</p>` : ""}
      ${card.state === "failed" ? `<p class="error-text" role="status">${esc(result.message || "处理未完成，原件保留。")}</p>` : ""}
      <div class="media-card-actions">${card.can_restore ? button("restore_media","恢复到待处理",true) : ["completed"].includes(card.state) ? "" : (card.needs_confirmation ? button("process_media","处理",card.can_process)+button("ignore_media","忽略",card.can_ignore) : "")}</div>
      ${card.needs_confirmation && (card.state === "pending" || card.state === "failed") ? '<p class="muted media-help">处理：理解内容并归档有业务价值的结果。忽略：移除本条提醒，保留原件。</p>' : ""}
      ${card.routing_message ? `<p class="muted media-routing">${esc(card.routing_message)}</p>` : ""}
      ${card.state === "ignored" ? '<p class="muted">你已忽略此版本；原文件保留，未标记为已读。</p>' : ""}
      ${result.summary ? `<section class="media-understanding"><h4>内容讲了什么</h4><p>${esc(result.summary)}</p>
        <details><summary>关键内容与时间点</summary><ol>${(result.key_points || []).map(x => `<li><strong>${Number(x.time_seconds).toFixed(1)} 秒</strong> ${esc(x.content)}</li>`).join("")}</ol></details>
        ${(result.keyframes || []).length ? `<details><summary>查看关键画面</summary><div class="media-keyframes">${result.keyframes.map(x => `<figure><img loading="lazy" src="${esc(x.url)}" alt="${Number(x.time_seconds).toFixed(1)} 秒画面"><figcaption>${Number(x.time_seconds).toFixed(1)} 秒</figcaption></figure>`).join("")}</div></details>` : ""}
        <p class="muted">${esc(result.coverage || "")}</p>${(result.uncertainties || []).map(x => `<p class="muted">待确认：${esc(x)}</p>`).join("")}
        <p>${esc(result.business_reason || "")}</p>${result.memory?.validated ? '<p>业务记忆已校验。</p>' : ""}</section>` : ""}
      </article>`;
  }
  const pendingMedia = card => card.needs_confirmation && ["pending", "failed"].includes(card.state);
  function focusMedia(id) {
    const node = $$("#issue-detail [data-media-card]").find(node => node.dataset.mediaCard === id);
    if (node) { node.focus({ preventScroll: true }); node.scrollIntoView({ block: "start", behavior: "auto" }); }
  }
  async function advanceIgnoredMedia(selected, scope, cards, ignored) {
    const stillHere = () => state.selected === selected && state.scope === scope && ui.view === "needs-user";
    if (!stillHere()) return null;
    // Invalidate any detail read started before the saved decision.
    state.request += 1;
    const remaining = cards.filter(card => card.hold_id !== ignored.hold_id);
    state.detail = { ...state.detail, media_cards: remaining };
    renderMediaDetail(state.detail);
    announce("已忽略，记录保留在已归档问题。");
    const index = cards.findIndex(card => card.hold_id === ignored.hold_id);
    const next = [...cards.slice(index + 1), ...cards.slice(0, index)].find(pendingMedia);
    if (next) {
      focusMedia(next.hold_id);
      return { selected, hold_id: next.hold_id };
    }
    // The current group has no more choices. Use a fresh queue, not an archived group.
    const priorIds = (ui.workbench?.groups || ui.workbench?.items || []).map(row => row.group_id || row.issue_id);
    const value = await fetchAttention();
    if (!stillHere()) return null;
    state.scope = "attention"; state.groupIds = null; state.listKey = null;
    ui.workbench = attentionPage(value, state.visibleCount); ui.homeWorkbench = value;
    delete ui.panelErrors.workbench; ui.homeWorkbenchError = null;
    const rows = value.attention_rows || [];
    const current = priorIds.indexOf(selected);
    const preferred = [...priorIds.slice(current + 1), ...priorIds.slice(0, Math.max(current, 0))];
    const target = preferred.map(id => rows.find(row => (row.group_id || row.issue_id) === id)).find(Boolean)
      || rows.find(row => (row.group_id || row.issue_id) !== selected);
    if (target) {
      const id = target.group_id || target.issue_id;
      await open(id);
      if (state.selected !== id) return null;
      const first = state.detail?.media_cards?.find(pendingMedia);
      if (first) focusMedia(first.hold_id);
      else $("#issue-detail")?.scrollIntoView({ block: "start", behavior: "auto" });
      return { selected: id, hold_id: first?.hold_id };
    }
    showBusiness(); history.replaceState(null, "", location.pathname);
    clearWorkbench("本轮待处理已结束。已忽略的内容可在“已归档问题”恢复。"); renderList();
    return null;
  }
  function renderMediaDetail(value) {
    const cards = (value.media_cards || []).filter(card => value.status === "closed" || card.state !== "ignored");
    $("#issue-detail").innerHTML = `<header class="issue-heading"><div><span class="eyebrow">${esc(value.source_label)} · 媒体处理</span><h2>${cards.some(c => c.needs_confirmation) ? "视频：处理或忽略" : "系统自动处理媒体"}</h2></div><button type="button" class="text-button" data-close-media>收起</button></header>
      <p class="muted">只有视频需要你确认；图片等其他媒体由系统自动处理。</p>
      <p id="issue-inline-status" role="status">${esc(state.actionError || state.detailError?.message || "")}</p>
      <div class="media-decision-list">${cards.map(mediaCard).join("")}</div>`;
    $$("[data-media-action]").forEach(button => button.addEventListener("click",async () => {
      const card = cards.find(x => x.hold_id === button.dataset.mediaId);
      const selected = state.selected, scope = state.scope, action = button.dataset.mediaAction;
      let next = null;
      try {
        await execute(action,{hold_id:card.hold_id,version:card.version},{id:card.issue_id,revision:card.revision},
          action === "ignore_media" ? async result => {
            if (result.saved && result.state === "ignored") next = await advanceIgnoredMedia(selected, scope, cards, card);
          } : null);
        if (next && state.selected === next.selected && ui.view === "needs-user") focusMedia(next.hold_id);
      } catch(error) { state.actionError = error.message; }
      finally {
        if (action !== "ignore_media" && state.selected === selected) await open(selected,true);
        else if (state.selected) renderDetail();
      }
    }));
    $("[data-close-media]")?.addEventListener("click",() => { showBusiness(); history.replaceState(null,"",location.pathname); clearWorkbench("选择左侧一项，查看原因和处理办法。"); renderList(); });
  }
  function listenerAcceptanceSummary(value) {
    const a = value.observation?.listener_acceptance;
    if (!a) return "";
    const scopes = [ ["direct", "私聊"], ["group", "群聊"] ].map(([kind, label]) => {
      const item = a.scopes?.[kind] || {};
      const counts = Number.isInteger(item.received_events)
        ? `；本轮已入库事件 ${item.received_events} 条` : "；事件计数暂不可核验";
      const older = item.older_message_events > 0 ? `，其中 ${item.older_message_events} 条对应启动前消息，不计入新消息验收` : "";
      return `<p>${label}：${item.accepted ? "新消息验收已通过" : "等待启动后的真实新消息"}${esc(counts + older)}</p>`;
    }).join("");
    const sub = {server_listing:"已由订阅清单确认",live_delivery:"已由真实投递确认",pending:"等待有效回执"}[a.subscription_verification] || "等待有效回执";
    const obs = value.observation || {};
    const history = [obs.original_failure ? `<p>原始问题：${esc(obs.original_failure)}</p>` : "",
      a.historical_accepted_at ? `<p>历史监听验收：${esc(formatTime(a.historical_accepted_at, true))}；不代表本轮通过。</p>` : "",
      obs.last_success ? `<p>历史来源流程成功：${esc(formatTime(obs.last_success, true))}；不作为监听验收依据。</p>` : ""].join("");
    return `<div class="issue-reading listener-acceptance"><p><strong>${a.status === "passed" ? "本轮监听验收已通过" : "监听运行中，真实消息验收尚未完成"}</strong></p>
      <p>本轮启动：${esc(formatTime(a.ready_at, true))}；验收检查：${esc(formatTime(a.checked_at, true))}</p>
      ${scopes}<p>订阅验证：${esc(sub)}</p>${!a.evidence_current_episode ? '<p>现有验收回执未绑定本轮启动，等待重新核验。</p>' : ''}
      <p class="muted">聊天补同步和业务记忆写入分别跟踪；无新消息不判定为监听故障。</p>
      ${history ? `<details><summary>历史记录</summary>${history}</details>` : ''}</div>`;
  }

  function chatCommitRecovery(value) {
    const evidence = value.chat_recovery;
    if (!evidence) return "";
    if (evidence.evidence_gap) return '<p class="muted">聊天恢复回执暂不可核验，不能据此判定恢复。</p>';
    if (!evidence.candidates?.length) return "";
    const reasons = {identifier_occupied:"旧编号已被其他记录占用",additive_target_change:"目标页面新增了受治理记录",prepared_recovery_not_additive:"原正文或原记录发生变化，已安全停止",prepared_recovery_limit_reached:"关联批次再次冲突，已停止重建",prepared_recovery_prewrite_unverified:"缺少未写入的完整凭据，已安全停止",prepared_recovery_evidence_invalid:"提交或回滚凭据未通过核验",prepared_recovery_backup_invalid:"备份核验未通过",proposal_source_drift:"消息来源版本已变化"};
    return `<details><summary>聊天提交与恢复回执</summary><p class="muted">按故障时间查得的关联候选，逐笔核对完成依据；历史失败继续保留，不代表其他新故障已恢复。</p>${evidence.candidates.map(row => `<article><p>${row.recovery_verified ? `该批次已恢复 · ${esc(row.message_count)} 条 · ${esc(formatTime(row.completed_at,true))}` : "该批次尚无已核验的完成依据"}</p>${row.recovery_reason ? `<p>${esc(reasons[row.recovery_reason] || "恢复条件尚需核验，具体原因已保留")}</p>` : ""}<small>原批次：${esc(row.batch_id)}<br>关联子批次：${esc(row.child_batch_id)}${row.completion_sha256 ? `<br>完成凭据指纹：${esc(row.completion_sha256)}` : ""}</small>${(row.failures || []).map(f => `<p class="muted">${esc(f.stage)}：${esc(f.original_error_code || f.error_code)}${f.rollback_error_code ? `；回滚验收：${esc(f.rollback_error_code)}` : f.rollback_status === "passed" ? "；回滚验收通过" : ""}</p>`).join("")}</article>`).join("")}</details>`;
  }
  function renderDetail() {
    const value = state.detail;
    const key = JSON.stringify([state.selected, value?.revision, value?.actions, value?.attempts, value?.events, value?.blocked_by,
      value?.media_cards, value?.chat_recovery, value?.observation, value?.members, value?.action_history, value?.alert_health, state.detailError?.message, state.busy, state.actionError, ui.core?.maintenance?.active]);
    if (key === state.detailKey) return; state.detailKey = key;
    const failure = state.detailError ? panelFailure(state.detailError, value?.checked_at) + '<button type="button" class="text-button" data-issue-reload>重新读取</button>' : "";
    if (!value) { $("#issue-detail").innerHTML = failure; bindReload(); return; }
    if (value.media_cards?.length) { renderMediaDetail(value); return; }
    const unavailable = state.busy || state.detailError || ui.core?.maintenance?.active;
    const observation = value.observation || {};
    const ledger = ledgerRecovery(value);
    const repairLabels = { not_attempted: "尚未修复", queued: "修复已排队", running: "正在有限修复", verified: "本地修复已验收", partial: "部分恢复，冲突保留", blocked: "已停止自动尝试", failed: "修复未成功" };
    const uniqueHealth = [...new Map((value.alert_health || []).map((a) => [JSON.stringify([a.repair_status, a.repair_attempt_count, a.delivery, a.status, a.acknowledged_at, a.supervisor_recovery]), a])).values()];
    const health = uniqueHealth.map((alert) => `<div class="issue-notification-health"><p>${alert.supervisor_recovery ? esc(alert.supervisor_recovery.summary) : ledger ? `${esc(attemptLabel(value))}；${ledger.phase === "verified" ? "已核验恢复" : "正在核验剩余历史异常"}` : (alert.repair_attempt_count > 0 || alert.repair_run_id || alert.repair_started_at) ? `本地修复：${esc(repairLabels[alert.repair_status] || "待核验")} · ${esc(alert.repair_attempt_count ?? '未记录')} 次` : '尚未进入专门本地修复流程；后台处理状态见当前原因'}</p><p>通知：${esc([deliveryLabel("Mac", alert.delivery?.macos), deliveryLabel("飞书", alert.delivery?.miso)].filter(Boolean).join("；") || "尚未请求")}</p><p class="muted">${esc(alert.acknowledged_at ? "已知悉，停止重复提醒；故障未关闭。" : alert.status === "open" ? (alert.next_reminder && Date.parse(alert.next_reminder) > Date.now() ? "Mac 下次提醒：" + formatTime(alert.next_reminder, true) : "Mac 提醒由后台核对投递状态") + "；飞书有实质变化时通知" : "恢复通知单独核对；通知不代表已读。")}</p></div>`).join("");
    const actions = (value.actions || []).map((action) => `<div class="issue-option"><button type="button" class="secondary-button" data-issue-action="${esc(action.id)}" ${unavailable || !action.available ? "disabled" : ""}>${esc(action.label)}</button><small>${esc(action.reason)}</small></div>`).join("");
    const members = (value.members || []).map((member) => `<button type="button" class="dependency-link" data-member-issue="${esc(member.issue_id)}"><span>${esc(member.object_label || member.title)}</span><small>${esc(stateLabel(member))} · ${esc(attemptLabel(member))}${member.observation?.reading ? `<br>${esc(member.observation.reading.mail_status)}<br>${esc(member.observation.reading.summary)}` : ""}</small></button>`).join("");
    const attempts = (value.attempts || []).map((run) => `<li><strong>${esc(formatTime(run.started_at, true))} · ${esc(({ success: "成功", failed: "失败", blocked: "被阻止", running: "执行中" })[run.status] || run.status)}</strong><span>${esc(run.error_code ? safeErrorLabel(run.error_code) : "运行回执已保留")}</span>${run.steps?.length ? `<small>${esc(run.steps.map((step) => step.label + "：" + step.status).join("；"))}</small>` : ""}</li>`).join("");
    const receipts = (value.action_history || []).map((row) => {
      const status = ({ prepared: "等待核对回执", running: "正在执行", completed: "已完成", failed: "未完成" })[row.state] || row.state;
      const result = row.result || {};
      return `<article class="alert-result ${row.state === "failed" ? "failed" : ""}"><header><strong>${esc((value.actions || []).find((a) => a.id === row.action)?.label || ({ dispose: "人工处置", retry_extract: "附件提取", review_note: "核对结论", retry_source: "来源重试", repair_local: "安全本地修复", acknowledge: "确认知悉" })[row.action] || "操作")} · ${esc(status)}</strong><time>${esc(formatTime(row.created_at, true))}</time></header><p>${esc(result.message || "处理回执已保存。")}</p>${row.execution?.error_code || result.error_code ? `<small>${esc(safeErrorLabel(row.execution?.error_code || result.error_code))}</small>` : ""}${row.run_id ? `<button type="button" class="text-button" data-issue-run="${esc(row.run_id)}">查看执行步骤</button>` : ""}${result.diagnostics ? diagnosticsSummary(result.diagnostics) : ""}</article>`;
    }).join("");
    $("#issue-detail").innerHTML = `${failure}<header class="issue-heading"><div><span class="eyebrow">${esc(value.source_label)} · ${esc(stateLabel(value))}</span><h2>${esc(value.title)}</h2></div><div class="issue-heading-links"><button type="button" class="text-button" data-issue-actions-jump>去处理 ↓</button><button type="button" class="text-button" data-issue-chat-jump>讨论 ↓</button><button type="button" class="text-button" data-close-issue>收起</button></div></header>
      ${value.status === "closed" ? `<p class="issue-ended">${esc(stateLabel(value))} · ${esc(value.evidence?.reason || "对应结束证据已保存")}<small>${esc(formatTime(value.resolved_at, true))}</small></p>` : ""}
      <section class="issue-section"><span class="issue-number">01</span><div><h3>${value.kind === "attachment" || value.member_kind === "attachment" ? "邮件与附件分别是什么状态" : "具体哪里出了问题"}</h3>${value.object_label ? `<p class="issue-object-name">${esc(value.object_label)}</p>` : ""}<p>${esc(recoveryReason(value))}</p>${readingSummary(observation.reading)}${listenerAcceptanceSummary(value)}${heldMediaSummary(value.held_media)}<p class="muted">影响：${esc(value.impact || "此来源尚未完成可信同步。")}</p>${value.object_error ? `<p class="error-text">${esc(value.object_error)}</p>` : ""}</div></section>
      ${members ? `<section class="issue-section"><span class="issue-number">↳</span><div><h3>受影响项目</h3><p class="muted">同类状态归并显示；每项的实际原因、回执和处理结果分别保留。</p><div class="issue-member-list">${members}</div></div></section>` : ""}
      <section class="issue-section"><span class="issue-number">02</span><div><h3>${value.video_waiting ? "等待你的指令" : observation.listener_acceptance ? "本轮监听验收进度" : supervisorRecovery(value) && value.status !== "closed" ? "后台恢复进度" : ledger && value.status !== "closed" ? "自动恢复进度" : value.status === "closed" ? "结束依据" : value.kind === "attachment" || value.member_kind === "attachment" ? "为什么仍待核验" : "为什么没有自动恢复"}</h3><p>${esc(value.status === "closed" ? ((value.reading_complete || (observation.reading?.status === "extracted" && observation.reading?.receipt_verified)) ? value.reason : (value.evidence?.reason || "对应结束证据已保存，原问题和处理记录继续保留。")) : recoveryBoundary(value))}</p>${ledger ? recoverySummary(value) : ""}${value.status === "closed" && health ? `<details><summary>历史提醒情况</summary>${health}</details>` : (value.video_waiting ? "" : health)}
      ${observation.current_blocker && observation.current_blocker !== observation.error_code ? `<p class="issue-blocker">当前等待条件：${esc(observation.current_reason || safeErrorLabel(observation.current_blocker))}</p>` : ""}
      ${(value.blocked_by || []).map((row) => `<button type="button" class="dependency-link" data-dependency="${esc(row.issue_id)}">先处理：${esc(row.title)} →</button>`).join("")}
      ${value.blocker_note ? `<p class="muted">${esc(value.blocker_note)}</p>` : ""}
      <dl class="issue-times"><dt>下次检查</dt><dd>${esc(nextCheck(value))}</dd><dt>本次读取</dt><dd>${esc(formatTime(value.checked_at, true))}</dd></dl>
      ${chatCommitRecovery(value)}<details><summary>系统做过哪些尝试</summary><p class="muted">${esc(value.attempts_note)}</p>${ledger ? "" : recoverySummary(value)}${observation.item_attempt_count ? `<p>本邮件归档／语义处理累计 ${esc(observation.item_attempt_count)} 次。${esc(observation.item_attempt_detail)}</p>` : ""}${attempts ? `<ol class="issue-attempts">${attempts}</ol>` : (ledger ? '<p>后台恢复汇总见上方；人工操作记录单独保留。</p>' : '<p>没有可展示的逐次自动执行记录；不推测重试次数。</p>')}</details></div></section>
      <section class="issue-section"><span class="issue-number">03</span><div><h3>现在可以做什么</h3><p class="muted">在下方回复后将转到 Codex 继续处理；这里的操作仍需点击对应按钮。</p>${actions}<p id="issue-inline-status" class="error-text" role="status">${esc(state.actionError)}</p></div></section>
      <section class="issue-section"><span class="issue-number">04</span><div><h3>${value.status === "closed" ? "本问题的结束标准" : "做到什么才算结束"}</h3><p>${esc(value.completion_rule)}</p></div></section>
      ${(value.events || []).some((row) => row.event_type === "review_note") ? `<section class="issue-notes"><h3>人工核对结论</h3>${value.events.filter((row) => row.event_type === "review_note").map((row) => `<p>${esc(row.detail.note)}<small>${esc(formatTime(row.created_at, true))}</small></p>`).join("")}</section>` : ""}
      ${receipts ? `<section class="issue-receipts"><h3>历史处理记录</h3><p class="muted">以下保留各次操作当时的结果；当前读取状态见上方逐项回执。</p>${receipts}</section>` : ""}`;
    $$("[data-issue-action]").forEach((button) => button.addEventListener("click", () => choose(button.dataset.issueAction)));
    $$("[data-dependency]").forEach((button) => button.addEventListener("click", () => open(button.dataset.dependency)));
    $$("[data-member-issue]").forEach((button) => button.addEventListener("click", () => open(button.dataset.memberIssue)));
    $("[data-issue-actions-jump]")?.addEventListener("click", () => $(".issue-option")?.scrollIntoView({ block: "start", behavior: "auto" }));
    $("[data-issue-chat-jump]")?.addEventListener("click", () => { $("#issue-chat").scrollIntoView({ block: "start", behavior: "auto" }); $("#issue-input")?.focus({ preventScroll: true }); });
    $$("[data-issue-run]").forEach((button) => button.addEventListener("click", async () => {
      try {
        const run = await api("/api/actions/" + button.dataset.issueRun); const outcome = actionOutcome(run);
        text("#issue-inline-status", `${outcome.title}：${outcome.detail} ${(run.steps || []).map((step) => step.label + "：" + step.status).join("；")}`);
      } catch (error) { text("#issue-inline-status", error.message); }
    }));
    $("[data-close-issue]")?.addEventListener("click", () => { showBusiness(); history.replaceState(null, "", location.pathname); clearWorkbench("选择左侧一项，查看原因和处理办法。"); renderList(); });
    bindReload();
  }
  function choose(action) {
    const frozen = { id: state.selected, action, revision: state.detail.revision };
    if (action === "confirm_mail") { state.pendingChoice = frozen; text("#mail-safety-error", ""); $("#mail-safety-dialog").showModal(); return; }
    if (["dispose", "dismiss_media", "review_note"].includes(action)) {
      state.pendingChoice = frozen; const dispose = action !== "review_note"; const media = action === "dismiss_media";
      text("#issue-choice-title", media ? "这几条听记无需继续处理？" : dispose ? "保留这份原件，不再自动解析？" : "记录人工核对结论");
      text("#issue-choice-description", media ? "结束当前这些听记的待处理提醒，保留文字与处理记录。其他听记出现问题时仍会提醒。" : dispose ? "仅把当前附件标为“已处置”。原件、风险和失败记录保留；不代表解析成功，也不会清除其他邮件故障。附件内容变化后会重新成为问题。" : "请写明查看结果及还需处理的事项。保存说明不会关闭问题。");
      text("#issue-note-label", dispose ? "补充说明（可选）" : "核对说明"); $("#issue-note").value = ""; $("#issue-note").required = !dispose;
      text("#issue-choice-confirm", media ? "确认无需继续处理" : dispose ? "确认保留，不再解析" : "保存核对说明"); text("#issue-choice-error", "");
      $("#issue-choice-dialog").showModal(); return;
    }
    execute(action).catch((error) => {
      state.busy = false;
      state.actionError = error?.message || "处理请求未完成，请刷新后重试。";
      text("#issue-inline-status", state.actionError);
      announce(state.actionError);
    });
  }
  async function execute(action, extra = {}, frozen = null, afterSaved = null) {
    if (!state.detail || state.busy) return;
    const id = frozen?.id || state.selected; const key = submissionKey(id, "action-" + action + (extra.hold_id ? "-" + extra.hold_id : ""));
    const request = readStored(key) || { action_id: crypto.randomUUID(), action, revision: frozen?.revision || state.detail.revision, ...extra };
    localStorage.setItem(key, JSON.stringify(request)); state.busy = true; state.actionError = ""; renderDetail();
    try {
      const result = await api(endpoint(id, "/actions"), { method: "POST", body: JSON.stringify(request), timeoutMs: 25000 });
      if (result.saved) localStorage.removeItem(key);
      state.actionError = result.result?.message || "处理请求已保存。";
      if (afterSaved) await afterSaved(result);
      return result;
    } catch (error) {
      if (["issue_stale", "group_stale", "action_unavailable", "choice_required", "note_invalid"].includes(error.code)) localStorage.removeItem(key);
      state.actionError = error.message; announce(error.message); throw error;
    } finally { state.busy = false; if (!afterSaved && state.selected === id) await open(id, true); await loadAll(); if (action === "diagnostics" && state.selected === id) $(".issue-receipts .alert-result")?.scrollIntoView({ block: "start" }); }
  }
  async function confirmMail(stillUnread) {
    if (state.pendingChoice?.action !== "confirm_mail") { text("#mail-safety-error", "请从当前问题重新打开核对入口。"); return; }
    try { await execute("confirm_mail", { still_unread: stillUnread }, state.pendingChoice); $("#mail-safety-dialog").close(); state.pendingChoice = null; }
    catch (error) { text("#mail-safety-error", error.message); }
  }
  function bindReload() { $$("[data-issue-reload]").forEach((button) => button.addEventListener("click", () => open(state.selected, true))); }
  function renderChat() {
    $("#issue-chat").hidden = Boolean(state.detail?.media_cards?.length);
    if (state.detail?.media_cards?.length) return;
    const id = state.selected;
    const chat = state.chat;
    const localLegacy = (state.detail?.related_alert_ids || []).map((key) => localStorage.getItem("sync-center:alert-draft:" + key)).filter(Boolean);
    codexHandoff.mount($("#issue-chat"), {
      id, base: endpoint(id), draftKey: draftKey(id), draft: chat?.draft || "",
      disabled: Boolean(state.chatError || !chat || ui.core?.maintenance?.active),
      disabledReason: state.chatError?.message || (!chat ? "正在读取此前记录…" : ui.core?.maintenance?.active ? "维护期间回复保留在本机，暂缓转交。" : null),
      reload: () => open(id, true),
      history: codexHandoff.history(chat) + (localLegacy.length ? `<details><summary>此前告警的本机草稿（只读保留）</summary>${localLegacy.map((content) => `<p>${esc(content)}</p>`).join("")}</details>` : "")
    });
    bindReload();
  }
  function schedulePoll() {
    clearTimeout(state.poll);
    if (ui.view === "needs-user" && state.selected && (state.detail?.action_history?.some((row) => ["prepared", "running"].includes(row.state)))) {
      state.poll = setTimeout(() => { if (ui.visible && !$("dialog[open]") && ui.view === "needs-user") open(state.selected, true); }, 2200);
    }
  }
  function selectWithinAttention() {
    if (!ui.homeWorkbench || ui.homeWorkbenchError) return;
    const rows = ui.homeWorkbench.attention_rows || [];
    const selected = state.selected || (ui.selectedCase ? "business-" + ui.selectedCase.case_id : null);
    if (!selected || rows.some(row => (row.group_id || row.issue_id) === selected || (row.active_member_issue_ids || row.member_issue_ids || []).includes(selected))) return;
    if (ui.selectedCase && $("#case-input")) localStorage.setItem("sync-center:case-draft:" + ui.selectedCase.case_id, $("#case-input").value);
    showBusiness(); clearTimeout(ui.conversationPoll); ui.caseRequestId += 1; ui.selectedCase = null; ui.conversation = null;
    clearWorkbench("选择左侧一项，查看原因和处理办法。");
  }
  async function refreshSelected() {
    if (ui.view !== "needs-user" || state.busy) return;
    if (state.restoreSelection && ui.homeWorkbench && !ui.homeWorkbenchError) {
      state.restoreSelection = false;
      if (state.scope === "attention" && !/^#workbench\//.test(location.hash)) selectWithinAttention();
    }
    if (state.selected) await open(state.selected, true);
    else {
      const first = (ui.workbench?.groups || ui.workbench?.items || [])[0];
      if (!ui.selectedCase && first) await open(first.group_id || first.issue_id, true);
    }
  }
  async function showNeeds(id = null) {
    state.scope = "attention"; state.groupIds = null; state.listKey = null;
    setView("needs-user", true, false, true);
    if (id) await open(id);
    await loadAll();
    if (!id) { selectWithinAttention(); await refreshSelected(); }
  }
  function bind() {
    state.selected = /^#workbench\/((?:issue|group)-[a-f0-9]{24})$/.exec(location.hash)?.[1] || localStorage.getItem("sync-center:open-issue");
    $$("[data-issue-scope]").forEach((button) => button.addEventListener("click", async () => {
      state.scope = button.dataset.issueScope; state.groupIds = null; state.listKey = null;
      renderList();
      await loadAll();
    }));
    $("#issue-filter-reset").addEventListener("click", () => showNeeds());
    $("#issue-choice-dialog").addEventListener("close", () => { if (!$("#issue-choice-dialog").open) state.pendingChoice = null; });
    $("#issue-choice-cancel").addEventListener("click", () => $("#issue-choice-dialog").close());
    $("#issue-choice-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const choice = state.pendingChoice; if (!choice || state.busy) return;
      $("#issue-choice-confirm").disabled = true;
      try { await execute(choice.action, { note: $("#issue-note").value, ...(["dispose", "dismiss_media"].includes(choice.action) ? { confirmed: true } : {}) }, choice); $("#issue-choice-dialog").close(); }
      catch (error) { text("#issue-choice-error", error.message); }
      finally { $("#issue-choice-confirm").disabled = false; }
    });
    window.addEventListener("pagehide", keepDraft);
  }
  return { listPath, fetchList, fetchAttention, attentionQueue, renderCounts, renderList, open, fromAlert, resolveAlertTarget, activate, showBusiness, showNeeds, refreshSelected, confirmMail, bind,
    get isAttentionScope() { return state.scope === "attention"; },
    get queryKey() { return state.scope + ":" + listPath(); },
    get selected() { return state.selected; } };
})();
