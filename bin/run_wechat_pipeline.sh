#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -u
umask 077

bridge_root="${SYNC_CENTER_HOME}"
config_path="$bridge_root/config.json"
collector="$bridge_root/bin/collect_wechat_chat.py"
media_worker="$bridge_root/bin/process_wechat_media.py"

/usr/bin/python3 "$collector" --config "$config_path"
collect_exit=$?

if [[ $collect_exit -eq 0 || $collect_exit -eq 4 ]]; then
  /usr/bin/python3 "$media_worker" --config "$config_path" --limit 100
  media_exit=$?
else
  media_exit=0
fi

if [[ $collect_exit -ne 0 ]]; then
  exit $collect_exit
fi
exit $media_exit
