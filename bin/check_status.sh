#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -u

bridge_root="${SYNC_CENTER_HOME}"
receiver_label="com.example.business-memory-bridge"
watcher_label="com.example.business-memory-folder-watcher"
mount_label="com.user.mountwindesk"
receiver_health="$bridge_root/state/health.json"
watcher_health="$bridge_root/state/folder_watcher_health.json"
mount_root="${SYNC_CENTER_HOME}/isolated-home/WinDesk"
source_root="$mount_root/解密文件/业务记忆 Win11"
database="$bridge_root/state/bridge.sqlite"

service_state() {
  local label="$1"
  local output state
  if ! output=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null); then
    print -r -- "not_loaded"
    return
  fi
  state=$(print -r -- "$output" | /usr/bin/awk '/^[[:space:]]*state = / { sub(/^[[:space:]]*state = /, ""); gsub(/[[:space:]]+/, "_"); print; exit }')
  print -r -- "${state:-loaded}"
}

health_fields() {
  local path="$1"
  if [[ ! -f "$path" || -L "$path" ]]; then
    print -r -- $'missing\tunknown\tunknown\tunknown'
    return
  fi
  /usr/bin/python3 - "$path" <<'PY' 2>/dev/null || print -r -- $'invalid\tunknown\tunknown\tunknown'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
values = [
    data.get("status", "unknown"),
    data.get("updated_at", "unknown"),
    data.get("last_event_id", "unknown"),
    data.get("source_write_sandbox", "unknown"),
]
print("\t".join(str(value) if value is not None else "unknown" for value in values))
PY
}

receiver_service=$(service_state "$receiver_label")
watcher_service=$(service_state "$watcher_label")
mount_service=$(service_state "$mount_label")

IFS=$'\t' read -r receiver_state receiver_updated receiver_event receiver_sandbox <<< "$(health_fields "$receiver_health")"
IFS=$'\t' read -r watcher_state watcher_updated watcher_event watcher_sandbox <<< "$(health_fields "$watcher_health")"

if /sbin/mount | /usr/bin/grep -Fq " on $mount_root (smbfs"; then
  mount_state="mounted"
  source_state=$(/usr/bin/python3 - "$source_root" <<'PY' 2>/dev/null
import subprocess
import sys

try:
    result = subprocess.run(
        ["/bin/ls", "-A", sys.argv[1]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    print("online" if result.returncode == 0 else "missing_or_unreadable")
except subprocess.TimeoutExpired:
    print("timeout")
PY
  )
  source_state="${source_state:-check_failed}"
else
  mount_state="not_mounted"
  source_state="offline_mount"
fi

if [[ -x /usr/local/bin/tailscale ]]; then
  tailscale_state=$(/usr/local/bin/tailscale status --json 2>/dev/null | /usr/bin/python3 -c \
    'import json,sys; d=json.load(sys.stdin); print("online" if d.get("Self",{}).get("Online") else "offline")' \
    2>/dev/null || print -r -- "unavailable")
else
  tailscale_state="unavailable"
fi

if [[ -f "$database" && ! -L "$database" ]]; then
  database_fields=$(/usr/bin/python3 - "$database" <<'PY' 2>/dev/null
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
connection.execute("PRAGMA query_only = ON")
connection.execute("PRAGMA busy_timeout = 5000")
tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
watch_tables = ("watch_sources", "watch_entries", "watch_versions", "source_events", "maintenance_state")
print("db=present")
print(f"db_user_version={connection.execute('PRAGMA user_version').fetchone()[0]}")
print(f"db_journal_mode={connection.execute('PRAGMA journal_mode').fetchone()[0]}")
print(f"db_quick_check={connection.execute('PRAGMA quick_check').fetchone()[0]}")
for table in ("events", "files") + watch_tables:
    count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if table in tables else "missing"
    print(f"db_{table}={count}")
print("db_watch_schema=" + ("present" if all(table in tables for table in watch_tables) else "missing"))
if "maintenance_state" in tables:
    maintenance = dict(connection.execute("SELECT key,value FROM maintenance_state"))
    dirty = maintenance.get("index_dirty_generation", "unknown")
    applied = maintenance.get("index_applied_generation", "unknown")
    refreshed = maintenance.get("index_last_refreshed_at", "unknown") or "none"
    error = maintenance.get("index_last_error", "unknown")
    try:
        index_state = "up_to_date" if int(dirty) == int(applied) and not error else "stale_or_failed"
    except (TypeError, ValueError):
        index_state = "not_initialized" if dirty == applied == "unknown" else "invalid"
    print(f"db_index_dirty_generation={dirty}")
    print(f"db_index_applied_generation={applied}")
    print(f"db_index_last_refreshed_at={refreshed}")
    print(f"db_index_last_error={error or 'none'}")
    print(f"db_index_state={index_state}")
else:
    print("db_index_dirty_generation=unknown")
    print("db_index_applied_generation=unknown")
    print("db_index_last_refreshed_at=unknown")
    print("db_index_last_error=unknown")
    print("db_index_state=not_available")
connection.close()
PY
  )
  [[ -n "$database_fields" ]] || database_fields=$'db=unreadable\ndb_user_version=unknown\ndb_quick_check=unknown\ndb_index_state=unknown'
else
  database_fields=$'db=missing\ndb_user_version=unknown\ndb_quick_check=unknown\ndb_watch_schema=missing\ndb_index_state=not_available'
fi

printf 'receiver_service=%s\nreceiver_health=%s\nreceiver_health_updated_at=%s\nreceiver_last_event_id=%s\n' \
  "$receiver_service" "$receiver_state" "$receiver_updated" "$receiver_event"
printf 'watcher_service=%s\nwatcher_health=%s\nwatcher_health_updated_at=%s\nwatcher_last_event_id=%s\nwatcher_source_write_sandbox=%s\n' \
  "$watcher_service" "$watcher_state" "$watcher_updated" "$watcher_event" "$watcher_sandbox"
printf 'mount_service=%s\nmount=%s\nsource=%s\ntailscale=%s\n' \
  "$mount_service" "$mount_state" "$source_state" "$tailscale_state"
print -r -- "$database_fields"
