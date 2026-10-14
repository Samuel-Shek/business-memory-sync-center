#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -eu
umask 077

chmod 600 \
  '${SYNC_CENTER_HOME}/isolated-home/AI/LocalData/BUSINESS-NonObsidian.nosync/dingtalk-chat/logs/hourly-launchd.out.log' \
  '${SYNC_CENTER_HOME}/isolated-home/AI/LocalData/BUSINESS-NonObsidian.nosync/dingtalk-chat/logs/hourly-launchd.err.log' \
  2>/dev/null || true

export PATH="${SYNC_CENTER_HOME}/isolated-home/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export BUSINESS_DINGTALK_MEMORY_HOURLY="1"
exec '${SYNC_CENTER_HOME}/bin/BUSINESS Memory Receiver.app/Contents/MacOS/business-memory-receiver-launcher'
