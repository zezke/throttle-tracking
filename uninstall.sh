#!/bin/bash
# Stop and remove the LaunchDaemon. Collected data in ./data is kept.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./uninstall.sh" >&2
  exit 1
fi
PLIST="/Library/LaunchDaemons/local.throttle-logger.plist"
launchctl bootout system "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Removed local.throttle-logger (data in ./data kept)"
