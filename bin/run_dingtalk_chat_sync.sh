#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -eu
umask 077

chmod 600 \
  '${SYNC_CENTER_HOME}/isolated-home/AI/LocalData/BUSINESS-NonObsidian.nosync/dingtalk-chat/logs/launchd.log' \
  2>/dev/null || true

export PATH="${SYNC_CENTER_HOME}/isolated-home/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
exec /usr/bin/python3 ${SYNC_CENTER_HOME}/bin/sync_dingtalk_chat.py --no-ledger
