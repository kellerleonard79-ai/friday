#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "Stopping Friday..."
pkill -f "friday.py" 2>/dev/null || true
sleep 2

echo "Starting Friday via launchd..."
launchctl kickstart -k "gui/$(id -u)/com.friday.agent" 2>/dev/null || {
    sleep 5
    launchctl kickstart -k "gui/$(id -u)/com.friday.agent"
}

sleep 4
echo "--- Recent log ---"
tail -6 logs/friday.log
