#!/bin/bash
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -euo pipefail
umask 077

BASE_DIR="${SYNC_CENTER_HOME}/bin"
SOURCE_FILE="${BASE_DIR}/windesk_keychain_helper.swift"
OUTPUT_FILE="${BASE_DIR}/windesk-keychain-helper"
INFO_PLIST_SOURCE="${BASE_DIR}/windesk-keychain-helper.Info.plist"
APP_BUNDLE="${BASE_DIR}/WinDesk Keychain Enrollment.app"
APP_EXECUTABLE="${APP_BUNDLE}/Contents/MacOS/windesk-keychain-helper"

if [ "${1:-}" != "" ] && [ "${1:-}" != "--replace" ]; then
    exit 64
fi
if [ -e "$OUTPUT_FILE" ] && [ "${1:-}" != "--replace" ]; then
    # Rebuilding changes the Keychain ACL's trusted binary identity. Require an
    # explicit replace, followed by a fresh GUI enrollment.
    exit 65
fi

# Rotation note: this helper is ad-hoc signed, so its Keychain trusted-app
# identity is its exact code hash, not merely the identifier below. A changed
# build must not be assumed able to update an item protected for the prior
# hash. Keep the old binary available only for an explicit, user-observed
# rotation, or remove the old item through Keychain Access before running the
# new binary's GUI enrollment. Never export the old secret or create a
# plaintext migration backup.

SWIFTC="$(/usr/bin/xcrun --find swiftc)"
SDK_PATH="$(/usr/bin/xcrun --sdk macosx --show-sdk-path)"
task_tmp="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/windesk-keychain-build.XXXXXX")"
trap '/bin/rm -rf "$task_tmp"' EXIT

staged_app="${task_tmp}/WinDesk Keychain Enrollment.app"
candidate="${staged_app}/Contents/MacOS/windesk-keychain-helper"
/bin/mkdir -p "${staged_app}/Contents/MacOS"
/bin/cp "$INFO_PLIST_SOURCE" "${staged_app}/Contents/Info.plist"
"$SWIFTC" \
    -sdk "$SDK_PATH" \
    -suppress-warnings \
    -parse-as-library \
    -O \
    -whole-module-optimization \
    -framework AppKit \
    -framework Security \
    "$SOURCE_FILE" \
    -o "$candidate"

# Sign the native app as one coherent code object. Both GUI enrollment and
# unattended mounting execute this exact Contents/MacOS file, so they share a
# single valid designated requirement and Keychain trusted-app identity.
/bin/chmod 700 "$staged_app" "${staged_app}/Contents" "${staged_app}/Contents/MacOS"
/bin/chmod 600 "${staged_app}/Contents/Info.plist"
/bin/chmod 700 "$candidate"
/usr/bin/plutil -lint "${staged_app}/Contents/Info.plist" >/dev/null
/usr/bin/codesign \
    --force \
    --sign - \
    --identifier com.business.memory-bridge.windesk-keychain-helper \
    "$staged_app" >/dev/null 2>&1
/usr/bin/codesign --verify --strict --deep "$staged_app" >/dev/null 2>&1
/usr/bin/codesign --verify --strict "$candidate" >/dev/null 2>&1

# This mode is offline: it checks only constants, argv construction, prompt
# detection and a synthetic PTY exchange. It never opens Keychain or SMB. Its
# two output streams must stay empty so no future regression reaches a log.
selftest_stdout="${task_tmp}/selftest.stdout"
selftest_stderr="${task_tmp}/selftest.stderr"
: > "$selftest_stdout"
: > "$selftest_stderr"
/bin/chmod 600 "$selftest_stdout" "$selftest_stderr"
"$candidate" self-test >"$selftest_stdout" 2>"$selftest_stderr"
[ ! -s "$selftest_stdout" ] && [ ! -s "$selftest_stderr" ]

# Swap the generated bundle without deleting the prior copy first. If the new
# move fails, restore the old bundle from this already-private temp directory.
old_app="${task_tmp}/previous-enrollment.app"
old_helper="${task_tmp}/previous-helper"
if [ -e "$APP_BUNDLE" ]; then
    /bin/mv "$APP_BUNDLE" "$old_app"
fi
if [ -e "$OUTPUT_FILE" ] || [ -L "$OUTPUT_FILE" ]; then
    /bin/mv "$OUTPUT_FILE" "$old_helper"
fi
if ! /bin/mv "$staged_app" "$APP_BUNDLE"; then
    if [ -e "$old_app" ]; then /bin/mv "$old_app" "$APP_BUNDLE"; fi
    if [ -e "$old_helper" ] || [ -L "$old_helper" ]; then /bin/mv "$old_helper" "$OUTPUT_FILE"; fi
    exit 66
fi

# Compatibility entrypoint: resolve to the app executable instead of keeping a
# second signed copy that could drift from the ACL-owning binary.
staged_link="${task_tmp}/windesk-keychain-helper-link"
/bin/ln -s "$APP_EXECUTABLE" "$staged_link"
if ! /bin/mv "$staged_link" "$OUTPUT_FILE"; then
    /bin/mv "$APP_BUNDLE" "$staged_app"
    if [ -e "$old_app" ]; then /bin/mv "$old_app" "$APP_BUNDLE"; fi
    if [ -e "$old_helper" ] || [ -L "$old_helper" ]; then /bin/mv "$old_helper" "$OUTPUT_FILE"; fi
    exit 67
fi

/usr/bin/cmp -s "$OUTPUT_FILE" "$APP_EXECUTABLE"
[ "$(/usr/bin/stat -L -f '%i' "$OUTPUT_FILE")" = "$(/usr/bin/stat -f '%i' "$APP_EXECUTABLE")" ]
/usr/bin/codesign --verify --strict --deep "$APP_BUNDLE" >/dev/null 2>&1
/usr/bin/codesign --verify --strict "$OUTPUT_FILE" >/dev/null 2>&1
/usr/bin/codesign --verify --strict "$APP_EXECUTABLE" >/dev/null 2>&1
