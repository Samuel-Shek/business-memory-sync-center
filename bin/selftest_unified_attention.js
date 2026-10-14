"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
function fixture(api = async () => ({})) {
  const ui = { panelErrors: {} };
  const context = vm.createContext({ ui, api });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../decision-center/workbench.js"), "utf8") + "\nglobalThis.workbench = workbenchWorkspace;", context);
  return context.workbench;
}
const group = (id, attention = "needs_user", status = "open") => ({ group_id: "group-" + id, issue_id: "group-" + id, status, attention_state: attention, member_issue_ids: [id], affected_count: 1 });
const alert = (id, issues, extra = {}) => ({ alert_id: id, status: "open", effective_state: "critical", issues: issues.map(issue_id => ({ issue_id })), ...extra });

test("waiting alerts do not override two manual issues", () => {
  const workbench = { groups: [group("core"), group("listener"), group("mail", "waiting"), group("background", "automatic")], group_counts: { needs_user: 2, automatic: 1, waiting: 1, closed: 0 } };
  const result = fixture().attentionQueue(workbench, { alerts: [alert("a", ["core"]), alert("b", ["listener"]), alert("c", ["mail"])] });
  assert.equal(result.attention_total, 2);
  assert.deepEqual(Array.from(result.attention_rows, row => row.group_id).sort(), ["group-core", "group-listener"]);
  assert.equal(result.group_counts.needs_user, 2);
  assert.equal(result.attention_critical, true);
  assert.equal(workbench.groups[0].attention_alert, undefined);
});

test("several alerts and grouped occurrences do not duplicate one issue", () => {
  const result = fixture().attentionQueue({ groups: [group("x")] }, { alerts: [alert("a", ["x"], { group_primary_id: "a", group_member_ids: ["a", "b"] }), alert("b", ["x"], { group_primary_id: "a" }), alert("c", ["x"])] });
  assert.equal(result.attention_total, 1);
});

test("resolved items leave current attention without erasing their source records", () => {
  const workbench = { groups: [group("done", "closed", "closed"), group("auto", "automatic")] };
  const alerts = { alerts: [alert("old", ["done"], { status: "resolved" })] };
  assert.equal(fixture().attentionQueue(workbench, alerts).attention_total, 0);
  assert.equal(alerts.alerts.length, 1);
  assert.equal(workbench.groups.length, 2);
});

test("unlinked alerts remain visible and truncated input remains explicitly incomplete", () => {
  const result = fixture().attentionQueue({ groups: [] }, { alerts: [alert("missing", [])], has_more: true });
  assert.equal(result.attention_total, 1);
  assert.equal(result.attention_rows[0].kind, "alert");
  assert.equal(result.health.at(-1).available, false);
});

test("group membership preserves automatic classification and unaffected manual work", () => {
  const combined = { ...group("first", "automatic"), member_issue_ids: ["first", "second"], affected_count: 2 };
  const result = fixture().attentionQueue({ groups: [combined, group("manual")] }, { alerts: [alert("a", ["second"])] });
  assert.equal(result.attention_total, 1);
  assert.equal(result.affected_total, 1);
});

test("attention includes issues on later pages and computes the total before preview slicing", async () => {
  const calls = [];
  const workbench = fixture(async url => {
    calls.push(url);
    if (url.includes("offset=500")) return { groups: [group("last")], items: [], group_has_more: false, group_counts: { needs_user: 501 } };
    return { groups: Array.from({ length: 500 }, (_, i) => group(String(i))), items: [], group_has_more: true, group_next_offset: 500 };
  });
  const result = await workbench.fetchAttention(3, Promise.resolve({ alerts: [] }));
  assert.equal(result.attention_total, 501);
  assert.equal(result.groups.length, 3);
  assert.equal(result.group_has_more, true);
  assert.equal(result.attention_rows.some(row => row.group_id === "group-last"), true);
  assert.equal(calls.length, 2);
});

test("failed alert reads reject the count instead of displaying a false zero", async () => {
  await assert.rejects(fixture(async () => ({ groups: [], items: [] })).fetchAttention(3, Promise.reject(new Error("unavailable"))), /unavailable/);
});


test("automatic ledger recovery stays in system processing despite an open historical alert", () => {
  const recovery = {...group("ledger", "automatic"), error_code:"supervisor_runtime_error_pending"};
  const result = fixture().attentionQueue({groups:[recovery]}, {alerts:[alert("a",["ledger"])]});
  assert.equal(result.attention_total,0);
  assert.equal(result.groups[0].status,"open");
});
test("ledger recovery requiring a decision stays visible", () => {
  for (const attention of ["automatic","needs_user"]) {
    const recovery = {...group("ledger", attention), error_code:"supervisor_runtime_error_pending", ledger_recovery:{requires_user:true}};
    assert.equal(fixture().attentionQueue({groups:[recovery]}, {alerts:[alert("a",["ledger"])]}).attention_total,1);
  }
});
test("ledger rendering uses backend attempts and scheduled check without borrowing manual counts", () => {
  const context=vm.createContext({ui:{},api:()=>{},esc:String,formatTime:String});
  const code=fs.readFileSync(path.join(__dirname,"../decision-center/workbench.js"),"utf8").replace("return { listPath,", "return { recoverySummary, nextCheck, attemptLabel, listPath,");
  vm.runInContext(code+";globalThis.w=workbenchWorkspace;",context);
  const value={status:"open",observation:{item_attempt_count:0,ledger_recovery:{automatic_attempts:6,pending_incidents:2,missing_paths:["mark_delivery"],native_cycles:12,observation_seconds:660,next_check_at:"2099-01-01T00:00:00Z"}}};
  assert.equal(context.w.attemptLabel(value),"历史数据库尝试 6 次");
  assert.equal(context.w.nextCheck(value),"2099-01-01T00:00:00Z");
  assert.match(context.w.recoverySummary(value),/仍待核验 2 条历史异常/);
  assert.match(context.w.recoverySummary(value),/通知结果登记/);
});
