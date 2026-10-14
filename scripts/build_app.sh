#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
out="$repo/build/Business Memory Sync Center.app"
mkdir -p "$out/Contents/MacOS"
swiftc -parse-as-library "$repo/app-source/DingTalkSyncCenter.swift" -o "$out/Contents/MacOS/DingTalkSyncCenter" -framework AppKit -framework WebKit -framework UserNotifications
cp "$repo/app-source/Info.plist" "$out/Contents/Info.plist"
echo "Built locally; not installed or launched."
