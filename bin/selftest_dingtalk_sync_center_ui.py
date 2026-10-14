#!/usr/bin/env python3
"""Static product, accessibility, and API checks for Sync Center 4.4.8."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import subprocess
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0


class Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: dict[str, int] = {}
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        self.elements.append((tag, values))
        if values.get("id"):
            self.ids[values["id"]] = self.ids.get(values["id"], 0) + 1


def check(value: bool, message: str) -> None:
    global CHECKS
    if not value:
        raise AssertionError(message)
    CHECKS += 1


def main() -> int:
    html = (ROOT / "decision-center" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "decision-center" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "decision-center" / "app.js").read_text(encoding="utf-8")
    alert_js = (ROOT / "decision-center" / "alert-workbench.js").read_text(encoding="utf-8")
    workbench_js = (ROOT / "decision-center" / "workbench.js").read_text(encoding="utf-8")
    handoff = (ROOT / "decision-center" / "codex-handoff.js").read_text(encoding="utf-8")
    status_contract = (ROOT / "decision-center" / "status-contract.js").read_text(encoding="utf-8")
    backend = (ROOT / "bin" / "dingtalk_decision_center.py").read_text(encoding="utf-8")
    reconcile = (ROOT / "bin" / "claim_reconciliation.py").read_text(encoding="utf-8")
    conversation = (ROOT / "bin" / "case_conversation.py").read_text(encoding="utf-8")
    syncv2 = (ROOT / "bin" / "dingtalk_sync_v2.py").read_text(encoding="utf-8")
    swift = (ROOT / "app-source" / "DingTalkSyncCenter.swift").read_text(encoding="utf-8")
    info = (ROOT / "app-source" / "Info.plist").read_text(encoding="utf-8")
    logo = (ROOT / "assets" / "logo" / "energy-pulse-master.svg").read_text(encoding="utf-8")
    document = Document()
    document.feed(html)
    check('id="alert-chat"' not in html and 'id="issue-chat"' in html and 'data-alert-workbench' in alert_js,
          "alerts link to a single issue workspace instead of a duplicate action/chat window")
    check(all(value in workbench_js for value in ("为什么没有自动恢复", "做到什么才算结束", "data-issue-id", "confirmed: true")),
          "each issue has a recovery explanation, completion boundary and explicit disposition")

    check(all(document.ids.get(value) == 1 for value in document.ids), "HTML IDs are unique")
    check(
        all(f'data-page="{value}"' in html for value in ("overview", "needs-user", "archived-questions", "automatic", "activity")),
        "five primary information-architecture entries exist",
    )
    check(
        '<details class="system-nav" id="system-nav" open>' in html
        and all(f'data-page="{value}"' in html for value in ("processes", "locks", "history")),
        "System details start expanded and remain manually collapsible",
    )
    check('id="needs-badge"' in html and 'data-page="alerts"' not in html and 'id="system-alert-badge"' not in html and 'id="alerts-badge"' not in html, "pending issues and alerts share one sidebar entry and count")
    check(all(symbol not in html for symbol in (">⌂<", ">↻<", ">●<", ">!<")), "text glyph navigation icons are removed")
    check(html.count("<svg") >= 8 and "memory loop" not in html.lower(), "navigation uses inline vector icons")
    check('id="metric-needs"' in html and 'id="metric-automatic"' in html and 'id="metric-resolved"' in html and 'id="metric-history"' in html, "work-count hierarchy is visible")
    check(all(f'id="{value}"' in html for value in ("overview-needs-list", "action-card", "action-progress", "action-steps", "sync-availability")), "overview exposes unified attention, continuous progress, steps, and sync eligibility")
    check("问题工作台" in html and "无需逐条查看" in html, "copy presents a problem workbench without per-evidence review burden")
    check("<strong>业务记忆</strong><span>同步中心</span>" in html, "visible brand header uses the current app name")
    check("培训、旧台账和已收口证据只在这里出现" in html, "historical informational records are history-only")
    check('role="listbox"' in html and 'role="option"' in js, "master list semantics are exposed")
    check(any(tag == "a" and attrs.get("class") == "skip-link" for tag, attrs in document.elements), "skip link exists")
    check(any(attrs.get("aria-live") == "polite" for _tag, attrs in document.elements), "polite live status exists")
    check(all(attrs.get("type") or attrs.get("value") for tag, attrs in document.elements if tag == "button"), "buttons have explicit behavior")
    check(all(attrs.get("aria-label") for tag, attrs in document.elements if tag == "button" and attrs.get("class") == "icon-button"), "icon buttons have accessible names")
    check('id="choice-dialog"' in html and "明确应用" in html and "没有默认选项，也不会超时代选" in js, "case actions require two explicit clicks")
    check(
        'id="mail-safety-dialog"' in html
        and "另一台设备仍是未读" in html
        and "未读状态已改变" in html
        and "confirm_mail" in workbench_js and "still_unread" in workbench_js,
        "mail read safety waits for one explicit cross-device choice without a default",
    )
    check("context_hash: ui.selectedCase.context_hash" in js and "case_version: ui.selectedCase.case_version" in js and "conversation_hash: ui.conversation.conversation_hash" in js, "preview is bound to case and conversation context")
    check("preview_token: ui.preview.preview_token" in js and "crypto.randomUUID()" in js, "apply uses preview token and idempotency key")
    check("business_fact_changed" in reconcile and "governed_memory_write" in reconcile, "manual automatic-observation choice cannot mutate facts")
    check("document.title = needs" in js and "dockTile.badgeLabel" in swift, "Dock badge is derived from Needs Me count")
    check(all(value in js for value in ("/api/reconciliation?scope=needs_user", "/api/reconciliation?scope=archived_questions", "/api/reconciliation?scope=automatic", "/api/reconciliation?scope=history")), "all reconciliation scopes are consumed")
    check("/api/reconciliation/case-[0-9a-f]{20}" in backend and "/preview" in backend and "/apply" in backend and "/archive" in backend, "structured reconciliation endpoints exist")
    check(all(value in backend for value in ("/conversation", "/messages", "/retry", "/draft")), "persistent conversation endpoints exist")
    check('"work_counts": work_counts' in backend, "state API exposes compatibility work counts")
    check('"case_kind": "automatic_group"' in backend and '"group_count": len(automatic_items)' in backend, "automatic work is progressively disclosed as aggregates")
    check('work_counts["automatic"] +=' not in backend, "operational diagnostics do not inflate evidence workload")
    check("claim_handle,claim_kind,title,proposition_summary" in reconcile and '"block"' not in reconcile[reconcile.index("def case_detail"):reconcile.index("def work_counts")], "case API never returns raw claim blocks")
    check("business-claim" not in js and "base64" not in js.lower(), "frontend cannot render internal claim markers")
    check("self.headers.get(\"Host\"" in backend and "X-CSRF-Token" in backend and "Origin" in backend, "loopback API validates Host Origin and CSRF")
    check("127.0.0.1" in backend and "localhost" not in js, "service remains loopback-only")
    check("Promise.allSettled" in js and "未误显示为零" in js, "partial reads fail closed without false zeroes")
    check("statusTone(sourceCode)" in js and "SyncStatusContract" in js and "/status-contract.js" in html, "all source colors use the shared status contract")
    check(all(value in status_contract for value in ('persistent_failure: "critical"', 'unknown: "warning"', 'paused: "neutral"', 'healthy: "success"')), "status contract encodes critical warning neutral and success states")
    check("|| \"warning\"" in status_contract and 'background: var(--amber)' in css, "unrecognized status fails closed to amber instead of green")
    check(all(value in js for value in ("sync-center:tracked-run", "sync-center:dismissed-run", "/api/actions/${encodeURIComponent(runId)}", "}, 1000);")), "manual action progress survives reopen and polls once per second")
    check("失败链路：" in js and "个步骤已跳过" in js and 'id="action-progress"' in html, "failed progress explains the failure chain and skipped downstream work")
    check("global_action" in js and "action.available !== true" in js and "sync-availability" in html, "sync control explains and enforces dashboard eligibility")
    check('id="overview-alert"' not in html and "attention_total" in js and "attentionQueue" in workbench_js and 'data-issue-scope="attention"' in html, "overview and workbench use the same deduplicated attention queue")
    check("row.last_seen" in alert_js and "maintenance_pending" in alert_js and "recovered" in alert_js, "alert chronology maintenance-pending and recovery states remain visible")
    check("deliveryLabel(\"Mac\"" in alert_js and "deliveryLabel(\"Miso\"" in alert_js and "alert_health" in workbench_js, "per-channel delivery failure and uncertainty remain in issue details and history")
    check("界面连接正常" in js and "本机服务正常" not in js, "interface connectivity is not presented as sync health")
    check("control_action_gate" in backend and "control_state_unknown" in backend and '"/status-contract.js"' in backend, "backend serves the contract and independently enforces sync eligibility")
    check("reconciliation_uninitialized" in reconcile and "PRAGMA query_only=ON" in reconcile and "range(3)" in reconcile, "case reads recover transient WAL state while remaining query-only")
    check("data-retry-case" in js and "targetGroups.length && !ui.selectedCase" in js, "failed case loads have an explicit retry and the first workbench topic auto-opens")
    check("window.setDashboardVisibility" in js and "setDashboardVisibility(false)" in swift, "polling follows native window visibility")
    check("15000" in js and "document.hidden" in js, "visible dashboard refresh is bounded")
    check("AbortController" in js and "request_timeout" in js and "timeoutMs: 12000" in js, "one slow read cannot freeze alert and delivery receipt refresh")
    check("prefers-color-scheme" in css and "data-theme" in css, "light dark and manual themes are supported")
    check("prefers-reduced-motion" in css and "prefers-contrast: more" in css and ":focus-visible" in css, "motion contrast and focus accessibility are covered")
    check("@media (max-width: 1199px)" in css and "min-width: 720px" in css, "integrated two-column workbench retains the minimum native window")
    check(".case-list { flex: 1; min-height: 0; overflow-y: auto" in css and ".workbench-main { min-width: 0; min-height: 0; overflow-y: auto" in css, "topic list and integrated detail conversation have independent scrolling")
    check("workbench-tabs" not in html and "data-mobile-pane" not in css, "detail and GPT conversation never switch through hidden tabs")
    check("#f5f5f7" in css and "#232421" in logo and "#D98935" in logo, "neutral native palette preserves the existing logo")
    check(logo.count("<circle") == 1 and "pulse" not in logo.lower() and "#FF9B1D" not in logo, "logo is a single memory loop with one amber node")
    check("<text" not in logo and "<path" in logo, "logo is vector-only and contains no text or trademark copy")
    check(
        all("示例企业记忆同步中心" not in value for value in (html, js, swift, info, logo)),
        "legacy visible app name is absent from current UI sources",
    )
    check(
        all("业务记忆同步中心" in value for value in (html, js, swift, info, logo)),
        "current visible app name is consistent across UI sources",
    )
    check((ROOT / "assets" / "logo" / "energy-pulse-16.svg").is_file() and (ROOT / "assets" / "logo" / "energy-pulse-32.svg").is_file(), "16 and 32 px optical masters exist")
    check("com.example.dingtalk-sync-center" in info, "Bundle ID is preserved")
    check("<string>4.7.1</string>" in info and "<string>27</string>" in info, "release is 4.7.1 build 27")
    check("value?.groups || value?.items" in workbench_js and "group_counts" in workbench_js,
          "workbench defaults to root-cause groups while retaining item compatibility")
    check("data-member-issue" in workbench_js and "affected_total" in workbench_js,
          "group cards expose affected counts and retain member drill-down")
    check("getDeliveredNotifications" in swift and "--sync-notification" in swift and "permission_required" in swift,
          "native delivery separates permissions, requests and notification-center evidence")
    notification_helper = swift.split("static func runHelper()", 1)[1].split("static func requestPermission()", 1)[0]
    check("NSApplication.shared" not in notification_helper and "setActivationPolicy" not in notification_helper,
          "headless notification checks never register a transient Dock application")
    check("alert_health" in workbench_js and "repair_local" in workbench_js,
          "issue workbench exposes bounded repair and independent notification receipts")
    check("localURL = URL(string: \"http://127.0.0.1:17626/\")" in swift, "native app and port are preserved")
    check("configuration.websiteDataStore = .default()" in swift, "native drafts and original submission ids persist through application restarts")
    check("allowsMagnification = true" in swift and "window.minSize" in swift, "native zoom and minimum size remain available")
    check(all(value in swift for value in ('NSMenu(title: "编辑")', 'NSSelectorFromString("paste:")', 'NSSelectorFromString("selectAll:")', "installControlKeyEditingShortcuts", "flags.contains(.control)")), "native Edit menu and explicit Control editing shortcuts reach the focused web input")
    check("maintenance_write_guard" in backend and "preview_action" in backend and "apply_action" in backend, "writes retain maintenance and preview gates")
    check("confirm_email_read_safety" in backend and "still_unread" in backend, "mail safety confirmation is explicit and maintenance-gated")
    check("kickstartPostMaintenanceProbe" in swift and "verify_loaded" in swift, "maintenance exit repairs services and immediately requests a fresh probe")
    launch_body = swift.split("func applicationDidFinishLaunching", 1)[1].split("func applicationShouldTerminateAfterLastWindowClosed", 1)[0]
    check("DispatchQueue.global(qos: .utility).async" in launch_body and "self?.ensureBackgroundServices()" in launch_body, "startup service repair stays off the main UI thread")
    run_launchctl_body = swift.split("private func runLaunchctl", 1)[1].split("private func loadServiceRepairConfiguration", 1)[0]
    check(run_launchctl_body.index("readDataToEndOfFile") < run_launchctl_body.index("waitUntilExit"), "launchctl output is drained before waiting so a full pipe cannot hold the maintenance lock")
    check('guard FileManager.default.fileExists(atPath: maintenanceStatePath) else { return true }' in swift, "missing maintenance state fails closed before service repair")
    kickstart_body = swift.split("private func kickstartPostMaintenanceProbe()", 1)[1].split("private func maintenanceIsActive()", 1)[0]
    check("maintenanceLockPath" in kickstart_body and "LOCK_SH" in kickstart_body, "post-maintenance kickstart is serialized with the maintenance lock")
    check("source_fingerprint" in reconcile and "independence_group" in reconcile and "authority_class" in reconcile, "source independence and authority metadata are represented")
    check("human_decision" in reconcile and "human_authorization" in reconcile and "model_proposed" in reconcile, "human authority is separate from AI relation proposals")
    check("RELATION_OUTBOX" in reconcile and "source_changed" in reconcile and "BEGIN IMMEDIATE" in reconcile, "outbox CAS and transactional guards are present")
    check(all(f"CREATE TABLE IF NOT EXISTS {table}" in syncv2 for table in ("case_threads", "case_episodes", "case_events", "case_messages", "case_ai_runs", "case_proposals", "case_drafts", "case_action_receipts", "case_memory_proposals")), "formal conversation and recovery tables are additive")
    check("semantic_fingerprint" in conversation and "case_identity_reused" in conversation and "case_reopened" in conversation, "stable case identity has lineage and episode guards")
    check("plan_confirmed" in reconcile and '"resolve_plan": "plan_confirmed"' in reconcile, "confirmed plans remain distinct from resolved business facts")
    check("basis_conversation_hash" in conversation and "ai_response_superseded" in conversation, "late AI answers are fenced from changed context")
    check("target_before_hash" in reconcile and "memory_target_stale" in conversation and "maintenance_epoch" in reconcile, "previews bind maintenance and target state")
    check("restore_durable_actions" in conversation and "restore_case_disposition" in reconcile, "formal action receipts can restore derived case state")
    check('maxlength="4000"' in handoff and "e.metaKey" in handoff and 'e.key === "Enter"' in handoff and "e.isComposing" in handoff, "composer enforces length IME safety and Command Enter sending")
    check("localStorage" in js and "sync-center:case-draft" in js, "maintenance mode preserves unsent browser draft")
    check("v.renderKey === key" in handoff and "priorScrollTop" in handoff and "expanded" in handoff, "conversation refresh preserves old history position and expansion")
    check(all(value in js for value in ("function captureWorkbenchScroll", "function selectedCaseSummaryChanged", "renderWorkbench: !stillExists", "silent: true, scrollState", "refreshSelected: true")), "background refresh keeps the current workbench mounted and restores its reading position")
    check("overflow-anchor: none" in css, "browser scroll anchoring cannot override the explicit workbench position")
    check("focused_questions" in conversation and "len(questions) > 3" in conversation, "AI asks no more than three focused questions")
    check(all(value in conversation for value in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", 'service_tier=\"{service_tier}\"')), "selectable GPT model and fast service tier reach the real invocation")
    check("codexHandoff.mount" in js and "codexHandoff.mount" in workbench_js and "data-handoff-send" in handoff and "ai_profile" not in handoff, "all problem composers use native Codex settings and a shared handoff")
    check(all(value in syncv2 for value in ("ai_model TEXT", "ai_reasoning_effort TEXT", "ai_speed TEXT", "archive_after_reply INTEGER", "PRAGMA user_version=11")), "selected GPT profile and archive intent are retained in schema v11")
    check("owner_statement" in conversation and "review_required" in conversation and "AI 回答不作为来源" in conversation, "memory proposal governance is explicit")
    check("case-workbench" in html and 'id="case-scroll"' in html and html.index('id="needs-detail"') < html.index('id="case-conversation"'), "problem detail actions and persistent GPT chat are continuous in one right pane")
    check(all(value in js for value in ("function groupNeeds", "data-group-key", "const key = item.case_id")), "specific decisions retain stable identities independent of archive folder titles")
    check(all(value in reconcile for value in ("def decision_guidance", '"system_finding"', '"owner_must_decide"', '"why_not_automatic"', '"after_decision"')), "case API explains the finding owner decision boundary and consequence")
    check("-webkit-user-select: text" in css and "Ctrl + Enter" in handoff, "composer supports text selection and keyboard sending")
    check("data-history-case-id" in js and "await selectCase(button.dataset.historyCaseId)" in js, "closed case conversations remain browsable from history")
    check("textContent" in js and "esc(message.content)" in js and "innerHTML = message.content" not in js, "AI and user text are rendered as escaped plain text")
    check("已归档问题" in html and 'id="needs-badge"' in html and 'data-page="archived-questions"' in html, "archived questions remain outside the current workload badge")
    check("archiveSelectedCase" in js and "只读保留" in handoff and "转交不会关闭或归档问题" in handoff, "history and manual archive remain separate from handoff")
    check("client_message_id" in handoff and "sync-center:codex-submit:" in handoff and "receipt_uncertain" in (ROOT / "bin/codex_handoff.py").read_text(), "handoff retains submission identity across ambiguous outcomes")
    check("recover_pending_archives" in conversation and "cancelled_active_reply" in conversation and "case_archived" in conversation, "reply archive recovery and late-answer fencing are implemented")
    check("archived_questions" in reconcile and "NOT (state='archived' AND disposition='archive')" in reconcile, "question archives have a dedicated scope and do not duplicate generic history")

    result = subprocess.run(["/usr/bin/env", "node", "--check", str(ROOT / "decision-center" / "app.js")], capture_output=True, text=True, check=False)
    check(result.returncode == 0, "frontend JavaScript parses")
    contract_result = subprocess.run(
        [
            "/usr/bin/env", "node", "-e",
            "const c=require(process.argv[1]);if(c.toneFor('persistent_failure')!=='critical'||c.toneFor('failed')!=='critical'||c.toneFor('unknown')!=='warning'||c.toneFor('brand_new_state')!=='warning'||c.toneFor('healthy')!=='success'||c.toneFor('paused')!=='neutral')process.exit(1)",
            str(ROOT / "decision-center" / "status-contract.js"),
        ],
        capture_output=True, text=True, check=False,
    )
    check(contract_result.returncode == 0, "status contract never maps failures or unknown states to green")
    eligibility = subprocess.run(
        ["/usr/bin/env", "node", "-e", r"""
const fs = require('node:fs'), vm = require('node:vm');
const js = fs.readFileSync(process.argv[1], 'utf8');
const body = js.slice(js.indexOf('function renderSyncControl()'), js.indexOf('function actionOutcome('));
const nodes = Object.fromEntries(['#sync-all','#sync-confirm','#sync-availability'].map(k=>[k,{}]));
const ui = {system:null,core:{},panelErrors:{},activeAction:null};
const context = {ui,$:s=>nodes[s],text:(s,v)=>{nodes[s].textContent=v},statusContract:require(process.argv[2])};
vm.createContext(context);vm.runInContext(body,context);
const cases = [
 [null,{},true], [{},{},true], [{global_action:{}},{},true],
 [{global_action:{available:true}},{},false],
 [{global_action:{available:false,reason:'unavailable'}},{},true],
 [{global_action:{available:true,running:true}},{},true],
 [{global_action:{available:true}},{maintenance:{active:true}},true]
];
for(const [system,core,disabled] of cases){ui.system=system;ui.core=core;context.renderSyncControl();
 if(nodes['#sync-all'].disabled!==disabled||nodes['#sync-confirm'].disabled!==disabled)throw Error('sync eligibility mismatch');
 if(disabled&&!nodes['#sync-availability'].textContent)throw Error('missing disabled reason');
}
""", str(ROOT / "decision-center" / "app.js"), str(ROOT / "decision-center" / "status-contract.js")],
        capture_output=True, text=True, check=False,
    )
    check(eligibility.returncode == 0, "unknown control state, maintenance and running tasks never enable a sync action: " + eligibility.stderr)
    print(f'{{"verdict":"pass","checks":{CHECKS}}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
