#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
exec /usr/bin/python3 '${SYNC_CENTER_HOME}/bin/dingtalk_decision_center.py' --open
