#!/bin/zsh
[ "${SYNC_CENTER_ENABLE_ARCHIVED_CODE:-0}" = "1" ] || { echo "Archived integration is disabled" >&2; exit 78; }
: "${SYNC_CENTER_HOME:?Set an isolated absolute data root}"
set -eu
umask 077

root_dir="${SYNC_CENTER_HOME}"
bin_dir="$root_dir/bin"
scanner_source="$bin_dir/wechat_key_scanner.c"
helper_source="$bin_dir/wechat_keychain_helper.swift"
image_decoder_source="$bin_dir/wechat_image_decode.c"
scanner_binary="$bin_dir/wechat-key-scanner"
helper_binary="$bin_dir/wechat-keychain-helper"
image_decoder_binary="$bin_dir/wechat-image-decode"
entitlements="$bin_dir/wechat_key_scanner.entitlements.plist"

/usr/bin/clang -O2 -Wall -Wextra -Werror \
  -o "$scanner_binary" "$scanner_source" \
  -framework Security
/usr/bin/swiftc -O -o "$helper_binary" "$helper_source" \
  -framework Security
/usr/bin/clang -O2 -Wall -Wextra -Werror \
  -o "$image_decoder_binary" "$image_decoder_source"

/usr/bin/codesign --force --sign - --entitlements "$entitlements" "$scanner_binary"
/usr/bin/codesign --force --sign - "$helper_binary"
/usr/bin/codesign --force --sign - "$image_decoder_binary"
/bin/chmod 700 "$scanner_binary" "$helper_binary" "$image_decoder_binary"

"$scanner_binary" --self-test
"$helper_binary" self-test
"$image_decoder_binary" --self-test
/usr/bin/codesign --verify --strict "$scanner_binary"
/usr/bin/codesign --verify --strict "$helper_binary"
/usr/bin/codesign --verify --strict "$image_decoder_binary"

printf '%s\n' '{"verdict":"pass","tools_built":3,"wechat_app_modified":false}'
