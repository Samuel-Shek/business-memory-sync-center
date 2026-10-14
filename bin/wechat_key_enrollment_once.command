#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -u
umask 077

printf '%s\n' '微信“示例企业”一次性密钥登记'
printf '%s\n' '当前版本使用 Apple 自带调试器，不再要求输入管理员密码。'
printf '%s\n\n' '本次使用微信启动时捕获；微信会正常退出并自动重启一次。'

${SYNC_CENTER_HOME}/bin/enroll_wechat_keys.sh enroll-startup
exit_code=$?

printf '\n%s\n' '登记命令已经结束。'
if [ "$exit_code" -eq 0 ]; then
  printf '%s\n' '请把上方非秘密状态回执发给 Codex；可能是完整登记，也可能仍缺少少量分片。'
else
  printf '%s\n' '登记未成功，所有采集与 AI 任务继续保持暂停。'
fi
printf '%s\n' '当前不会启用采集或 AI 后台任务。'
printf '%s\n' '回到 Codex 回复：启动捕获已结束'
printf '\n%s' '按回车关闭此窗口……'
IFS= read -r _reply
exit "$exit_code"
