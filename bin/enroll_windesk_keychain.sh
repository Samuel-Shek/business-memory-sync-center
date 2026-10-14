#!/bin/bash
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -u

# Native GUI enrollment is launched through a real local .app so macOS gives
# its standard NSWindow a foreground application lifecycle. `open -W` only
# waits for that application to close and does not propagate the helper's exit
# status. The visible form reports stored/cancelled state; acceptance must
# independently verify the Keychain item's non-secret metadata. No password or
# enrollment status is placed in argv, environment, a file or either output
# stream.
HELPER="${SYNC_CENTER_HOME}/bin/windesk-keychain-helper"
APP_BUNDLE="${SYNC_CENTER_HOME}/bin/WinDesk Keychain Enrollment.app"
APP_EXECUTABLE="${APP_BUNDLE}/Contents/MacOS/windesk-keychain-helper"

[ -f "$HELPER" ] && [ -x "$HELPER" ] || exit 70
[ "$(/usr/bin/stat -L -f '%u' "$HELPER" 2>/dev/null)" = "$(/usr/bin/id -u)" ] || exit 71
[ "$(/usr/bin/stat -L -f '%Lp' "$HELPER" 2>/dev/null)" = "700" ] || exit 72
[ -d "$APP_BUNDLE" ] && [ -x "$APP_EXECUTABLE" ] || exit 73
[ "$(/usr/bin/stat -f '%u' "$APP_EXECUTABLE" 2>/dev/null)" = "$(/usr/bin/id -u)" ] || exit 74
[ "$(/usr/bin/stat -f '%Lp' "$APP_EXECUTABLE" 2>/dev/null)" = "700" ] || exit 75
/usr/bin/cmp -s "$HELPER" "$APP_EXECUTABLE" || exit 76
[ "$(/usr/bin/stat -L -f '%i' "$HELPER" 2>/dev/null)" = "$(/usr/bin/stat -f '%i' "$APP_EXECUTABLE" 2>/dev/null)" ] || exit 77
/usr/bin/codesign --verify --strict "$HELPER" >/dev/null 2>&1 || exit 78
/usr/bin/codesign --verify --strict "$APP_EXECUTABLE" >/dev/null 2>&1 || exit 79

exec /usr/bin/open -W -n "$APP_BUNDLE" --args enroll >/dev/null 2>&1
