#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -eu
umask 077

mode="${1:-status}"
case "$mode" in
  enroll|enroll-memory|enroll-startup|verify|status) ;;
  *) printf '%s\n' '用法：enroll_wechat_keys.sh [enroll|enroll-memory|enroll-startup|verify|status]' >&2; exit 2 ;;
esac

exec /usr/bin/python3 \
  ${SYNC_CENTER_HOME}/bin/enroll_wechat_keys.py \
  "$mode"
