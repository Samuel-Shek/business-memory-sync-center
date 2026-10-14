"use strict";

// Run the real front-end handlers against isolated APIs and local storage.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { randomUUID } = require("node:crypto");
const root = path.resolve(__dirname, "../decision-center");
const id = "issue-" + "a".repeat(24);
const base = "/api/workbench/" + id;

function fixture(route) {
  const calls = [], storage = new Map();
  const ui = { alerts: { alerts: [] }, panelErrors: {}, workbench: { groups: [] } };
  const context = vm.createContext({
    ui, crypto: { randomUUID }, setTimeout, clearTimeout,
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    api: async (url, options = {}) => {
      const call = { url, method: options.method || "GET", payload: options.body ? JSON.parse(options.body) : null };
      calls.push(call); return route(call);
    }
  });
  vm.runInContext(fs.readFileSync(path.join(root, "codex-handoff.js"), "utf8") + "\nglobalThis.handoff = codexHandoff;", context);
  vm.runInContext(fs.readFileSync(path.join(root, "workbench.js"), "utf8") + "\nglobalThis.workbench = workbenchWorkspace;", context);
  return { context, handoff: context.handoff, workbench: context.workbench, ui, calls, storage };
}

test("empty shortcut keeps drafts and simultaneous clicks share one submission", async () => {
  const f = fixture((call) => call.method === "POST" ? { handoff: { status: "queued", submission_id: call.payload.client_message_id } } : { handoff: null, draft: "server draft" });
  f.storage.set("sync-center:issue-draft:" + id, "未提交的草稿");
  const rows = await Promise.all(Array.from({ length: 8 }, () => f.handoff.openSummary(base)));
  const posts = f.calls.filter((c) => c.method === "POST");
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, base + "/handoff");
  assert.equal(posts[0].payload.content, "");
  assert.equal(new Set(rows.map((r) => r.submission_id)).size, 1);
  assert.equal(f.storage.get("sync-center:issue-draft:" + id), "未提交的草稿");
  assert.equal(f.calls.filter((c) => c.url.endsWith("/conversation")).length, 1);
});

test("saved blank submission is reused after a page restart", async () => {
  const f = fixture((c) => ({ handoff: c.method === "POST" ? { status: "queued" } : null }));
  const clientId = randomUUID();
  f.storage.set("sync-center:codex-submit:" + base, JSON.stringify({ client_message_id: clientId, content: "" }));
  await f.handoff.openSummary(base);
  assert.equal(f.calls.find((c) => c.method === "POST").payload.client_message_id, clientId);
});

for (const status of ["delivered", "sending", "verifying", "failed", "uncertain"]) {
  test(`${status} handoff opens its existing task without sending again`, async () => {
    const existing = { status, thread_id: "existing-task", content: "此前回复", submission_id: randomUUID() };
    const f = fixture(() => ({ handoff: existing }));
    const result = await f.handoff.openSummary(base);
    assert.equal(result, existing);
    assert.deepEqual(f.calls.map((c) => [c.method, c.url]), [["GET", base + "/handoff"], ["POST", base + "/handoff/open"]]);
    assert.equal(f.storage.size, 0);
  });
}

test("queued handoff waits for the same task to be created", async () => {
  const f = fixture(() => ({ handoff: { status: "queued", thread_id: null } }));
  const result = await f.handoff.openSummary(base);
  assert.equal(f.handoff.isPending(result), true);
  assert.equal(f.calls.length, 1);
});

test("an already delivered submit result still opens its task", async () => {
  const existing = { status: "delivered", thread_id: "concurrent-task" };
  const f = fixture((c) => ({ handoff: c.method === "POST" ? existing : null }));
  await f.handoff.openSummary(base);
  assert.deepEqual(f.calls.filter((c) => c.method === "POST").map((c) => c.url), [base + "/handoff", base + "/handoff/open"]);
});

test("failed handoff requires explicit retry and keeps its submission", async () => {
  const existing = { status: "failed", submission_id: randomUUID() };
  const f = fixture(() => ({ handoff: existing }));
  await f.handoff.openSummary(base);
  assert.equal(f.calls.length, 1);
  await f.handoff.openSummary(base, { retrySubmissionId: existing.submission_id });
  assert.deepEqual(f.calls.at(-1), { url: base + "/handoff/retry", method: "POST", payload: { submission_id: existing.submission_id } });
  assert.equal(f.storage.size, 0);
});

for (const failedPart of ["handoff", "conversation"]) {
  test(`${failedPart} read failure does not submit`, async () => {
    const f = fixture((c) => {
      if (c.url.endsWith("/" + failedPart)) throw new Error("读取失败");
      return { handoff: null };
    });
    await assert.rejects(f.handoff.openSummary(base), /读取失败/);
    assert.equal(f.calls.some((c) => c.method === "POST"), false);
  });
}

test("uncertain POST reconciles the receipt without resending", async () => {
  let existing = null;
  const f = fixture((c) => {
    if (c.method === "POST") {
      existing = { status: "verifying", submission_id: c.payload.client_message_id };
      throw new Error("请求超时");
    }
    return { handoff: existing };
  });
  await assert.rejects(f.handoff.openSummary(base), (error) => error.handoff === existing);
  const result = await f.handoff.openSummary(base);
  assert.equal(result, existing);
  assert.equal(f.calls.filter((c) => c.method === "POST").length, 1);
});

test("maintenance rejection remains visible and does not auto-retry", async () => {
  const f = fixture((c) => {
    if (c.method === "POST") throw Object.assign(new Error("维护中"), { status: 423 });
    return { handoff: null };
  });
  await assert.rejects(f.handoff.openSummary(base), (error) => error.status === 423);
  assert.equal(f.calls.filter((c) => c.method === "POST").length, 1);
});

test("grouped alert resolves the workbench group without changing selected page or draft", async () => {
  const second = "issue-" + "b".repeat(24), group = "group-" + "c".repeat(24);
  const f = fixture(() => ({ groups: [{ group_id: group, member_issue_ids: [id, second] }] }));
  const row = { alert_id: "alert-a", group_member_ids: ["alert-b"], issues: [{ issue_id: id, status: "open" }] };
  f.ui.alerts.alerts = [row, { alert_id: "alert-b", issues: [{ issue_id: second, status: "open" }, { issue_id: id, status: "open" }] }];
  const before = JSON.stringify(f.ui), previousPath = f.workbench.listPath();
  const result = await f.workbench.resolveAlertTarget(row);
  assert.equal(result.id, group);
  assert.equal(result.base, "/api/workbench/" + group);
  assert.deepEqual([...result.groupIds], [id, second]);
  const query = new URL(f.calls[0].url, "http://localhost").searchParams;
  assert.equal(query.get("ids"), id + "," + second);
  assert.equal(query.get("scope"), "all");
  assert.equal(JSON.stringify(f.ui), before);
  assert.equal(f.workbench.listPath(), previousPath);
  assert.equal(f.workbench.selected, null);
});

test("active issue takes precedence over closed links and individual issues stay valid", async () => {
  const f = fixture(() => ({ groups: [] }));
  const row = { alert_id: "a", issues: [{ issue_id: "issue-" + "b".repeat(24), status: "closed" }, { issue_id: id, status: "open" }] };
  const result = await f.workbench.resolveAlertTarget(row);
  assert.equal(result.id, id);
  assert.deepEqual([...result.groupIds], [id]);
});

test("closed alert links remain browsable in the closed scope", async () => {
  const f = fixture(() => ({ groups: [] }));
  const result = await f.workbench.resolveAlertTarget({ alert_id: "a", issues: [{ issue_id: id, status: "closed" }] });
  assert.equal(result.scope, "closed");
});

test("missing links and failed reads cannot reuse a stale group", async () => {
  const f = fixture(() => { throw new Error("列表读取失败"); });
  await assert.rejects(f.workbench.resolveAlertTarget({ alert_id: "a", issues: [] }), /关联记录/);
  assert.equal(f.calls.length, 0);
  f.ui.workbench.groups = [{ group_id: "stale-group", member_issue_ids: [id] }];
  await assert.rejects(f.workbench.resolveAlertTarget({ alert_id: "a", issues: [{ issue_id: id, status: "open" }] }), /列表读取失败/);
  assert.equal(f.workbench.selected, null);
});
