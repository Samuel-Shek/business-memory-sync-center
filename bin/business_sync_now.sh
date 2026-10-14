#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
#
# Win11 memory sync entry point.  Two modes:
#
#   (no args)   Launchpad button: re-mount if needed, force the watcher to scan
#               right now, wait for the files to land, then merge them into
#               curated memory notes, reporting every step by notification.
#   --hourly    Unattended agent: only run the semantic merge over whatever the
#               always-on watcher has already brought in.  Stays silent unless
#               something was written or something failed, so an hourly job
#               never turns into hourly noise.
#
# Neither mode scans the SMB share itself, so this needs no network-volume
# entitlement -- it only signals the already-authorised watcher.
set -u
umask 077

BRIDGE="${SYNC_CENTER_HOME}"
MOUNT_POINT="${SYNC_CENTER_HOME}/isolated-home/WinDesk"
MOUNT_SCRIPT="${SYNC_CENTER_HOME}/isolated-home/bin/mount-windesk-keychain.sh"
TRIGGER="$BRIDGE/state/force_scan.trigger"
LOG="$BRIDGE/logs/sync_now.log"
PY="/usr/bin/python3"

MODE="button"
[[ "${1:-}" == "--hourly" ]] && MODE="hourly"

mkdir -p "$BRIDGE/logs"

note() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') [$MODE] $*" >> "$LOG"; }

notify() {
  local title="$1" message="$2"
  /usr/bin/osascript -e "display notification \"${message//\"/\\\"}\" with title \"${title//\"/\\\"}\"" >/dev/null 2>&1
}

started_at=$(date +%s)
moved="-1"
note "=== 开始 ==="

if [[ "$MODE" == "button" ]]; then
  notify "业务记忆 · 同步中" "正在催 Win11 立即同步…"

  # 1) The share must be mounted before a scan is worth requesting.
  if ! /sbin/mount | /usr/bin/grep -Fq " on $MOUNT_POINT (smbfs"; then
    note "共享盘未挂载，尝试重连"
    notify "业务记忆 · 同步中" "共享盘掉线，正在重连…"
    [[ -x "$MOUNT_SCRIPT" ]] && "$MOUNT_SCRIPT" >> "$LOG" 2>&1
    if ! /sbin/mount | /usr/bin/grep -Fq " on $MOUNT_POINT (smbfs"; then
      note "重连失败，终止"
      notify "业务记忆 · 同步失败" "Win11 共享盘连不上，请确认那台电脑开着且在同一网络。"
      exit 1
    fi
    note "重连成功"
  fi

  # 2) Trigger the watcher and wait for the transport layer to settle.
  transport=$("$PY" - "$BRIDGE" "$TRIGGER" <<'PYEOF'
import json, pathlib, sqlite3, sys, time

bridge = pathlib.Path(sys.argv[1])
trigger = pathlib.Path(sys.argv[2])
health = bridge / "state" / "folder_watcher_health.json"
inbox = bridge / "watch_inbox"
db = bridge / "state" / "bridge.sqlite"

def event_count():
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.execute("PRAGMA query_only = ON")
        n = con.execute(
            "SELECT COUNT(*) FROM source_events WHERE source_id='win11_business_memory'"
        ).fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return -1

def health_stamp():
    try:
        return json.loads(health.read_text())
    except Exception:
        return {}

before = event_count()
baseline = health_stamp().get("updated_at")
trigger.touch()

# Phase A: confirm the forced full reconcile actually ran.
full_seen = False
deadline = time.time() + 45
while time.time() < deadline:
    data = health_stamp()
    if data.get("full_scan") and data.get("updated_at") != baseline:
        full_seen = True
        break
    if data.get("status") == "offline":
        break
    time.sleep(0.4)

# Phase B: let the receiver drain whatever that scan queued.
quiet = 0
deadline = time.time() + 120
while time.time() < deadline:
    pending = len(list(inbox.iterdir())) if inbox.is_dir() else 0
    if pending == 0:
        quiet += 1
        if quiet >= 4:
            break
    else:
        quiet = 0
    time.sleep(1.0)

after = event_count()
print(json.dumps({
    "moved": max(0, after - before) if before >= 0 and after >= 0 else -1,
    "full_scan_observed": full_seen,
    "watcher_status": health_stamp().get("status", "unknown"),
}, ensure_ascii=False))
PYEOF
)
  moved=$("$PY" -c "import json,sys;print(json.loads(sys.argv[1]).get('moved',-1))" "$transport" 2>/dev/null || echo "-1")
  watcher_status=$("$PY" -c "import json,sys;print(json.loads(sys.argv[1]).get('watcher_status','unknown'))" "$transport" 2>/dev/null || echo "unknown")
  note "搬运阶段: $transport"

  if [[ "$watcher_status" == "offline" ]]; then
    notify "业务记忆 · 同步失败" "监听器报告 Win11 来源离线，未能同步。"
    exit 1
  fi
  notify "业务记忆 · 同步中" "文件已就位，正在整理成记忆…"
fi

# 3) Turn whatever has landed into curated memory notes.
merge=$("$PY" "$BRIDGE/bin/win11_memory_automation.py" 2>> "$LOG")
merge_rc=$?
note "整理阶段(rc=$merge_rc): $merge"

elapsed=$(( $(date +%s) - started_at ))

summary=$("$PY" - "$merge" "$merge_rc" "$moved" "$elapsed" "$MODE" <<'PYEOF'
import json, sys
raw, rc, moved, elapsed, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
try:
    data = json.loads(raw)
except Exception:
    data = {}

notes = data.get("new_notes", 0)
pending = data.get("pending_events")

if rc != 0:
    code = data.get("code", "未知错误")
    if mode == "hourly":
        print(f"FAIL\t每小时整理失败（{code}）。文件仍在本机，记忆未更新。")
    else:
        print(f"FAIL\t{'搬入 ' + moved + ' 个变化' if moved not in ('-1','') else '搬运结果未知'}；"
              f"整理失败（{code}），文件已到本机但未整理成记忆。")
elif data.get("status") == "skipped":
    print("QUIET\t整理任务正忙，本次跳过。" if mode == "hourly"
          else "OK\t另一个整理任务正在跑，本次跳过，稍后会自动补上。")
elif mode == "hourly":
    if notes:
        tail = f"，仍有 {pending} 条待整理" if pending else ""
        print(f"OK\t自动整理新增 {notes} 条记忆{tail}。")
    else:
        print("QUIET\t无新内容需要整理。")
else:
    moved_txt = f"搬入 {moved} 个变化" if moved not in ("-1", "") else "搬运结果未知"
    tail = f"，仍有 {pending} 条待整理" if pending else "，已全部整理完"
    print(f"OK\t{moved_txt}；新增 {notes} 条记忆{tail}。用时 {elapsed} 秒。")
PYEOF
)

verdict="${summary%%$'\t'*}"
message="${summary#*$'\t'}"
note "结论: $verdict / $message"

case "$verdict" in
  FAIL)  notify "业务记忆 · 同步未完成" "$message"; note "=== 结束（失败）==="; exit 1 ;;
  QUIET) note "=== 结束（无需通知）===" ; exit 0 ;;
  *)     notify "业务记忆 · 同步完成" "$message"; note "=== 结束（成功）==="; exit 0 ;;
esac
