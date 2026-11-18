#!/bin/bash
# Turns autopilot ON (or repairs it): runs mac/run-tick.sh every 15 minutes, starting now, and again after every login/reboot.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.clipmachine.autopilot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$ROOT/mac/run-tick.sh</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "AUTOPILOT ON: runs every 15 minutes. Log: $ROOT/bot.log"
echo "(Keep the Mac awake: System Settings > Battery/Energy > prevent automatic sleeping when the display is off.)"
