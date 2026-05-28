#!/bin/bash
cd "$(dirname "$0")"

echo "Stopping any running Friday process..."
pkill -f "friday.py" 2>/dev/null || true
sleep 2

echo "Starting Friday via launchd..."
launchctl kickstart -k "gui/$(id -u)/com.friday.agent" 2>/dev/null || true
sleep 5

# If launchd didn't start it (throttled or broken state), start directly
if ! pgrep -f "friday.py" > /dev/null; then
    echo "launchd unavailable — starting directly..."
    nohup /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 friday.py >> logs/friday.log 2>&1 &
    sleep 4
fi

if pgrep -f "friday.py" > /dev/null; then
    echo "Friday is running (PID $(pgrep -f 'friday.py'))."
else
    echo "ERROR: Friday failed to start. Check logs/friday.log."
fi

echo "--- Recent log ---"
tail -5 logs/friday.log
