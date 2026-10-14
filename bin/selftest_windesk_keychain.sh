#!/bin/bash
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -euo pipefail
umask 077

BASE_DIR="${SYNC_CENTER_HOME}/bin"
SOURCE_FILE="${BASE_DIR}/windesk_keychain_helper.swift"
BUILD_FILE="${BASE_DIR}/build_windesk_keychain_helper.sh"
ENROLL_FILE="${BASE_DIR}/enroll_windesk_keychain.sh"
INFO_PLIST_SOURCE="${BASE_DIR}/windesk-keychain-helper.Info.plist"
MOUNT_FILE="${SYNC_CENTER_HOME}/isolated-home/bin/mount-windesk-keychain.sh"
INSTALLED_HELPER="${BASE_DIR}/windesk-keychain-helper"
APP_BUNDLE="${BASE_DIR}/WinDesk Keychain Enrollment.app"
APP_INFO_PLIST="${APP_BUNDLE}/Contents/Info.plist"
APP_EXECUTABLE="${APP_BUNDLE}/Contents/MacOS/windesk-keychain-helper"

files=("$SOURCE_FILE" "$BUILD_FILE" "$ENROLL_FILE" "$INFO_PLIST_SOURCE" "$MOUNT_FILE")
for file in "${files[@]}"; do
    [ -f "$file" ] || { printf 'FAIL missing file: %s\n' "$file" >&2; exit 1; }
done

# Static no-secret boundary. These patterns would put a password in a command
# argument, shell variable/environment, security(1) stdout, or SMB URL.
if /usr/bin/grep -En \
    'security[[:space:]]+(find-generic-password|add-generic-password)|find-generic-password[[:space:]].*-w|SMB_PASS|PASSWORD=|setenv\(|putenv\(|//[^/@[:space:]]+:[^/@[:space:]]+@' \
    "${files[@]}"; then
    printf 'FAIL forbidden credential transport pattern found\n' >&2
    exit 1
fi

# Fixed identifiers and fail-closed exit contract must remain visible in source.
/usr/bin/grep -Fq 'com.business.memory-bridge.windesk-smb' "$SOURCE_FILE"
/usr/bin/grep -Fq 'static let account = "Example User"' "$SOURCE_FILE"
/usr/bin/grep -Fq 'case keychainLocked = 20' "$SOURCE_FILE"
/usr/bin/grep -Fq 'case keychainItemMissing = 21' "$SOURCE_FILE"
/usr/bin/grep -Fq 'case keychainInteractionDenied = 22' "$SOURCE_FILE"
/usr/bin/grep -Fq 'kSecUseAuthenticationUIFail' "$SOURCE_FILE"
/usr/bin/grep -Fq 'installTerminationSignalHandlers()' "$SOURCE_FILE"
/usr/bin/grep -Fq '@main' "$SOURCE_FILE"
/usr/bin/grep -Fq 'class NativeGUIAppDelegate: NSObject, NSApplicationDelegate' "$SOURCE_FILE"
/usr/bin/grep -Fq 'runNativeGUIApplication(.uiSelfTest)' "$SOURCE_FILE"
/usr/bin/grep -Fq 'case "ui-self-test":' "$SOURCE_FILE"
/usr/bin/grep -Fq 'let firstField = NSSecureTextField()' "$SOURCE_FILE"
/usr/bin/grep -Fq 'let secondField = NSSecureTextField()' "$SOURCE_FILE"
/usr/bin/grep -Fq 'window.makeKeyAndOrderFront(nil)' "$SOURCE_FILE"
if /usr/bin/grep -Eq 'NSAlert|runModal|abortModal' "$SOURCE_FILE"; then
    printf 'FAIL modal alert path found; enrollment must live in a standard visible NSWindow\n' >&2
    exit 1
fi
ui_self_test_body="$(/usr/bin/sed -n '/private func makeSelfTestContent/,/private func makeEnrollmentContent/p' "$SOURCE_FILE")"
if /usr/bin/grep -Eq 'storeSecret|loginKeychain|preflightSecret|fetchSecret|mount\(' <<< "$ui_self_test_body"; then
    printf 'FAIL UI self-test body can reach Keychain or mount code\n' >&2
    exit 1
fi
/usr/bin/grep -Fq 'waitpid(processID, &status, WNOHANG)' "$SOURCE_FILE"
if /usr/bin/grep -Fq 'waitpid(processID, &status, 0)' "$SOURCE_FILE"; then
    printf 'FAIL unbounded child wait found\n' >&2
    exit 1
fi
/usr/bin/grep -Fq 'com.business.memory-bridge.windesk-keychain-helper' "$BUILD_FILE"
/usr/bin/grep -Fq 'exec /usr/bin/open -W -n "$APP_BUNDLE" --args enroll' "$ENROLL_FILE"
/usr/bin/grep -Fq 'windesk_mount_manager.py' "$MOUNT_FILE"
/usr/bin/python3 "${BASE_DIR}/windesk_mount_manager.py" --self-test
/usr/bin/python3 "${BASE_DIR}/selftest_windesk_mount_manager.py"

[ -L "$INSTALLED_HELPER" ]
[ "$(/usr/bin/readlink "$INSTALLED_HELPER")" = "$APP_EXECUTABLE" ]
[ -x "$INSTALLED_HELPER" ]
[ "$(/usr/bin/stat -L -f '%u' "$INSTALLED_HELPER")" = "$(/usr/bin/id -u)" ]
[ "$(/usr/bin/stat -L -f '%Lp' "$INSTALLED_HELPER")" = "700" ]
codesign_info="$(/usr/bin/codesign -dv --verbose=4 "$INSTALLED_HELPER" 2>&1)"
/usr/bin/grep -Fq 'Identifier=com.business.memory-bridge.windesk-keychain-helper' <<< "$codesign_info"

# The visible enrollment app must execute the exact same signed file as mount
# mode. The compatibility helper resolves to this exact executable; byte,
# inode and designated-requirement equality prevent a silent ACL split.
[ -d "$APP_BUNDLE" ]
[ -f "$APP_INFO_PLIST" ]
[ -x "$APP_EXECUTABLE" ]
[ "$(/usr/bin/stat -f '%u' "$APP_EXECUTABLE")" = "$(/usr/bin/id -u)" ]
[ "$(/usr/bin/stat -f '%Lp' "$APP_EXECUTABLE")" = "700" ]
/usr/bin/plutil -lint "$APP_INFO_PLIST" >/dev/null
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP_INFO_PLIST")" = "windesk-keychain-helper" ]
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundlePackageType' "$APP_INFO_PLIST")" = "APPL" ]
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP_INFO_PLIST")" = "com.business.memory-bridge.windesk-keychain-helper" ]
/usr/bin/cmp -s "$INSTALLED_HELPER" "$APP_EXECUTABLE"
[ "$(/usr/bin/stat -L -f '%i' "$INSTALLED_HELPER")" = "$(/usr/bin/stat -f '%i' "$APP_EXECUTABLE")" ]
/usr/bin/codesign --verify --strict --deep "$APP_BUNDLE" >/dev/null 2>&1
/usr/bin/codesign --verify --strict "$APP_EXECUTABLE" >/dev/null 2>&1
app_codesign_info="$(/usr/bin/codesign -dv --verbose=4 "$APP_EXECUTABLE" 2>&1)"
/usr/bin/grep -Fq 'Identifier=com.business.memory-bridge.windesk-keychain-helper' <<< "$app_codesign_info"
[ "$(/usr/bin/codesign -d -r- "$INSTALLED_HELPER" 2>&1 | /usr/bin/sed -n 's/^# designated => //p')" = "$(/usr/bin/codesign -d -r- "$APP_EXECUTABLE" 2>&1 | /usr/bin/sed -n 's/^# designated => //p')" ]

task_tmp="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/windesk-keychain-test.XXXXXX")"
trap '/bin/rm -rf "$task_tmp"' EXIT
candidate="${task_tmp}/windesk-keychain-helper"
SWIFTC="$(/usr/bin/xcrun --find swiftc)"
SDK_PATH="$(/usr/bin/xcrun --sdk macosx --show-sdk-path)"

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
/bin/chmod 700 "$candidate"

# The compiled self-test validates the exact mount argv template and runs a
# synthetic PTY prompt/response. It has no Keychain or network code path. Keep
# stdout/stderr separate, mode 0600, and prove both remain completely empty.
selftest_stdout="${task_tmp}/selftest.stdout"
selftest_stderr="${task_tmp}/selftest.stderr"
: > "$selftest_stdout"
: > "$selftest_stderr"
/bin/chmod 600 "$selftest_stdout" "$selftest_stderr"
[ "$(/usr/bin/stat -f '%Lp' "$selftest_stdout")" = "600" ]
[ "$(/usr/bin/stat -f '%Lp' "$selftest_stderr")" = "600" ]
"$candidate" self-test >"$selftest_stdout" 2>"$selftest_stderr"

# Run the installed binary too: testing only a fresh candidate could let a
# stale installed helper pass acceptance.
"$INSTALLED_HELPER" self-test >>"$selftest_stdout" 2>>"$selftest_stderr"
"$APP_EXECUTABLE" self-test >>"$selftest_stdout" 2>>"$selftest_stderr"
# Exercise the exact LaunchServices route used for GUI enrollment, but select
# the strictly offline self-test command so this never opens Keychain or SMB.
/usr/bin/open -W -n "$APP_BUNDLE" --args self-test >>"$selftest_stdout" 2>>"$selftest_stderr"
"$MOUNT_FILE" --self-test >>"$selftest_stdout" 2>>"$selftest_stderr"

if [ -s "$selftest_stdout" ] || [ -s "$selftest_stderr" ]; then
    printf 'FAIL compiled helper emitted runtime output\n' >&2
    exit 1
fi
if /usr/bin/grep -aFq 'pty-ok' "$selftest_stdout" "$selftest_stderr"; then
    printf 'FAIL synthetic PTY marker escaped to an output stream\n' >&2
    exit 1
fi

printf 'PASS source contains no forbidden secret transport\n'
printf 'PASS installed helper has stable signed identity and mode 0700\n'
printf 'PASS mount argv template contains user/server/share/path only\n'
printf 'PASS synthetic PTY response leaves 0600 stdout/stderr captures empty\n'
printf 'PASS synthetic result mapping covers Keychain locked/missing/interaction failures\n'
printf 'PASS prompt and mount deadlines exercise bounded TERM/KILL child recovery\n'
printf 'PASS wrapper requires exact smbfs user/server/share identity at WinDesk\n'
printf 'PASS native enrollment app and mount helper resolve to one valid signed executable\n'
printf 'PASS LaunchServices can start and wait for the app through its offline self-test route\n'
printf 'PASS UI self-test uses the standard NSWindow path with no Keychain or mount call\n'
