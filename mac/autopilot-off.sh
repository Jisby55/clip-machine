#!/bin/bash
launchctl bootout "gui/$(id -u)/com.clipmachine.autopilot" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.clipmachine.autopilot.plist"
echo "AUTOPILOT OFF"
