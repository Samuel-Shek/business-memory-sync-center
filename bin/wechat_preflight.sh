#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -eu
umask 077

bridge_root="${SYNC_CENTER_HOME}"
/usr/bin/python3 "$bridge_root/bin/enroll_wechat_keys.py" status --config "$bridge_root/config.json"
/usr/bin/python3 "$bridge_root/bin/collect_wechat_chat.py" --config "$bridge_root/config.json" --preflight-only
