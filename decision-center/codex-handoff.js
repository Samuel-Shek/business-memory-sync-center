"use strict";

// One submission stays attached to its problem across refreshes and reconnects.
const codexHandoff = (() => {
  const views = new WeakMap();
  const shortcuts = new Map();
  const active = new Set(["queued", "preparing", "opening", "sending", "verifying"]);
  function badge(handoff, closed = false) {
    const delivered = handoff?.status === "delivered" && Boolean(handoff.thread_id);
    const pending = active.has(handoff?.status);
    if (!delivered && !pending) return "";
    const label = delivered ? (closed ? "曾转入 Codex" : "Codex 处理中") : "正在转交 Codex";
    const description = delivered ? (closed ? "转交记录与处理结果已保留" : "已取得 Codex 接收回执，问题尚未结束") : "正在核对 Codex 接收回执";
    return `<span class="codex-status-badge ${closed ? "historical" : pending ? "pending" : ""}" title="${esc(description)}"><span aria-hidden="true">↗</span> ${esc(label)}</span>`;
  }
  const read = (key) => { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch (_) { return null; } };
  function submission(base, content) {
    const key = "sync-center:codex-submit:" + base;
    let payload = read(key);
    if (!payload || payload.content !== content) payload = { client_message_id: crypto.randomUUID(), content };
    localStorage.setItem(key, JSON.stringify(payload));
    return payload;
  }
  async function change(base, suffix, payload) {
    try {
      return (await api(base + "/handoff" + suffix, { method: "POST", body: JSON.stringify(payload) })).handoff;
    } catch (error) {
      // A timeout does not imply rejection. Reconcile without resending.
      try { error.handoff = (await api(base + "/handoff")).handoff; } catch (_) { /* Keep the saved submission. */ }
      throw error;
    }
  }
  function openSummary(base, { retrySubmissionId = null } = {}) {
    if (shortcuts.has(base)) return shortcuts.get(base);
    const task = (async () => {
      const h = (await api(base + "/handoff")).handoff;
      if (retrySubmissionId) return change(base, "/retry", { submission_id: retrySubmissionId });
      if (h) return h.thread_id ? change(base, "/open", {}) : h;
      // Match the workbench's record-readiness check without reading or replacing its draft.
      await api(base + "/conversation");
      const result = await change(base, "", submission(base, ""));
      // A concurrent/replayed submission may already have a receipt, so no worker will reopen it.
      return result?.status === "delivered" && result.thread_id ? change(base, "/open", {}) : result;
    })().finally(() => shortcuts.delete(base));
    shortcuts.set(base, task);
    return task;
  }
  function history(chat) {
    const groups = [{ label: "此前对话（只读保留）", messages: chat?.messages || [] }, ...(chat?.legacy_conversations || [])];
    return groups.filter((g) => g.messages?.length || g.draft).map((g) => `<details class="legacy-conversation"><summary>${esc(g.label)}</summary>${(g.messages || []).map((m) => `<p><strong>${m.author === "user" ? "你" : "ChatGPT"}：</strong>${esc(m.content)}</p>`).join("")}${g.draft ? `<p><strong>此前草稿：</strong>${esc(g.draft)}</p>` : ""}</details>`).join("");
  }
  function current(v) { return views.get(v.node) === v && v.node.isConnected; }
  function draw(v) {
    if (!current(v)) return;
    const h = v.handoff;
    const key = JSON.stringify([h, v.loaded, v.busy, v.error, v.options.disabled, v.options.disabledReason, v.options.readOnly, v.options.history]);
    if (v.renderKey === key) return;
    const focusedInput = v.node.querySelector("[data-handoff-input]");
    const focused = focusedInput === document.activeElement;
    const selection = focused ? [focusedInput.selectionStart, focusedInput.selectionEnd] : null;
    const scroll = v.node.closest("#case-scroll, .issue-detail-scroll") || v.node.parentElement;
    const priorScrollTop = scroll?.scrollTop || 0;
    const expanded = [...v.node.querySelectorAll("details")].map((d) => d.open);
    v.renderKey = key;
    const draft = localStorage.getItem(v.options.draftKey) ?? v.options.draft ?? "";
    let body;
    if (h) {
      const delivered = h.status === "delivered";
      const pending = active.has(h.status);
      const summary = h.request_kind === "summary";
      body = `<section class="handoff-status" aria-live="polite"><h3>${delivered ? "已转入 Codex" : pending ? "正在转到 Codex…" : "转交尚未完成"}</h3><p>${esc(delivered ? "请在业务记忆同步中心项目的对应对话中继续。" : h.error || (h.native_received ? "Codex 已接收请求，正在核对消息记录。" : "请求已保存，正在打开对应对话并核对接收回执。"))}</p><details><summary>${summary ? "查看本次总结请求" : "查看本次回复"}</summary><p class="handoff-reply">${esc(h.request_text ?? h.content)}</p></details><div class="handoff-buttons">${h.thread_id ? '<button type="button" data-handoff-open class="send-button">在 Codex 中继续</button>' : ""}${!delivered && !pending ? '<button type="button" data-handoff-retry class="secondary">重试转交</button>' : ""}</div><small>转交不会关闭或归档问题，处理结果仍按实际回执更新。</small></section>`;
    } else if (v.options.readOnly) {
      body = `<p>这个问题的旧记录保留为只读。</p>${draft ? `<details><summary>此前草稿（只读保留）</summary><p class="handoff-reply">${esc(draft)}</p></details>` : ""}`;
    } else {
      body = `<div class="composer-wrap"><p>留空会在 Codex 中自动总结当前问题；也可以先输入你的想法。模型、推理强度与操作权限沿用 Codex 设置。</p><label class="sr-only" for="handoff-input-${v.options.id}">给 Codex 的回复</label><textarea data-handoff-input id="handoff-input-${v.options.id}" rows="4" maxlength="4000" placeholder="可留空直接总结，或输入你的想法…" ${v.busy ? "disabled" : ""}>${esc(draft)}</textarea><div class="composer-footer"><span>草稿保留在本机。⌘ / Ctrl + Enter 发送。</span><button type="button" data-handoff-send class="send-button" ${!v.loaded || v.busy || v.options.disabled ? "disabled" : ""}>${draft.trim() ? "发送并转到 Codex" : "转到 Codex 并总结"}</button></div></div>`;
    }
    const notice = v.error || v.options.disabledReason || (!v.loaded ? "正在核对转交记录，完成后即可转交…" : "");
    v.node.innerHTML = `<header class="conversation-header"><div><span class="eyebrow">本问题的 Codex 对话</span><h2>在 Codex 中继续处理</h2></div></header>${v.options.history || ""}${body}<p class="message-failure" role="status" data-handoff-error>${esc(notice)}</p>${(!v.loaded && v.error || v.options.disabledReason && v.options.reload) ? '<button type="button" data-handoff-refresh>重新读取转交状态和旧记录</button>' : ""}`;
    const input = v.node.querySelector("[data-handoff-input]");
    input?.addEventListener("input", () => {
      localStorage.setItem(v.options.draftKey, input.value);
      const sendButton = v.node.querySelector("[data-handoff-send]");
      sendButton.disabled = !v.loaded || v.busy || v.options.disabled;
      sendButton.textContent = input.value.trim() ? "发送并转到 Codex" : "转到 Codex 并总结";
    });
    input?.addEventListener("keydown", (e) => {
      if (!e.isComposing && (e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); send(v); }
    });
    v.node.querySelector("[data-handoff-send]")?.addEventListener("click", () => send(v));
    v.node.querySelector("[data-handoff-retry]")?.addEventListener("click", () => mutate(v, "/retry", { submission_id: h.submission_id }));
    v.node.querySelector("[data-handoff-open]")?.addEventListener("click", () => mutate(v, "/open", {}));
    v.node.querySelector("[data-handoff-refresh]")?.addEventListener("click", () => { v.options.reload?.(); load(v); });
    v.node.querySelectorAll("button").forEach((b) => { if (v.busy || v.options.disabled && !b.hasAttribute("data-handoff-open") && !b.hasAttribute("data-handoff-refresh")) b.disabled = true; });
    v.node.querySelectorAll("details").forEach((d, i) => { if (expanded[i]) d.open = true; });
    if (focused && input && !input.disabled) { input.focus({ preventScroll: true }); input.setSelectionRange(...selection); }
    if (scroll) scroll.scrollTop = priorScrollTop;
  }
  function poll(v) {
    clearTimeout(v.timer);
    if (current(v) && active.has(v.handoff?.status)) v.timer = setTimeout(() => load(v), 1800);
  }
  async function load(v) {
    try {
      const response = await api(v.options.base + "/handoff");
      v.handoff = response.handoff; v.loaded = true; v.error = "";
    } catch (e) { v.error = "回复保留在本机：" + e.message; }
    draw(v); poll(v);
  }
  async function mutate(v, suffix, payload) {
    if (v.busy || !current(v)) return;
    v.busy = true; v.error = ""; draw(v);
    try {
      v.handoff = await change(v.options.base, suffix, payload); v.loaded = true;
    } catch (e) {
      if (e.handoff) { v.handoff = e.handoff; v.loaded = true; }
      v.error = e.message;
    } finally { v.busy = false; draw(v); poll(v); }
  }
  async function send(v) {
    const button = v.node.querySelector("[data-handoff-send]");
    const input = v.node.querySelector("[data-handoff-input]");
    if (!input || button?.disabled || v.handoff || v.busy) return;
    const content = input.value;
    localStorage.setItem(v.options.draftKey, content);
    await mutate(v, "", submission(v.options.base, content));
  }
  function mount(node, options) {
    let v = views.get(node);
    if (!v || v.options.base !== options.base) {
      if (v) clearTimeout(v.timer);
      v = { node, options, loaded: false, busy: false, handoff: null, error: "" };
      views.set(node, v); draw(v); load(v);
    } else { v.options = options; draw(v); }
  }
  return { mount, history, openSummary, badge, isPending: (handoff) => active.has(handoff?.status) };
})();
