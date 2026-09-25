#!/bin/bash
# Install throttle_logger.py as a root LaunchDaemon (starts at boot, survives logout/reboot).
# Any extra arguments are passed to the logger, e.g.:
#   sudo ./install.sh --start 08:30 --end 17:30 --days-of-week mon-fri --period-days 14
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./install.sh [logger options]" >&2
  exit 1
fi

LABEL="local.throttle-logger"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
OWNER="${SUDO_USER:-$(stat -f %Su "$DIR")}"

mkdir -p "$DIR/data"
chown "$OWNER" "$DIR/data"

ARGS="<string>/usr/bin/python3</string><string>$DIR/throttle_logger.py</string><string>--db</string><string>$DIR/data/throttle.db</string>"
for a in "$@"; do ARGS="$ARGS<string>$a</string>"; done

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>$ARGS</array>
  <key>RunAtLoad</key><true/>
  <!-- restart after a crash, but not once the tracking period has ended -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>Nice</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DIR/data/logger.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/logger.log</string>
</dict>
</plist>
PLIST

chown root:wheel "$PLIST"
chmod 644 "$PLIST"
launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
echo "Installed and started $LABEL"
echo "Log: $DIR/data/logger.log   DB: $DIR/data/throttle.db"
