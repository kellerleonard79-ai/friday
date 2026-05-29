#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Mutex — prevents two concurrent restarts from racing
LOCKDIR="/tmp/friday_restart.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo "Another restart already in progress — skipping."
    exit 0
fi
trap "rmdir '$LOCKDIR' 2>/dev/null" EXIT

PIDFILE="logs/watchdog.pid"

echo "Stopping any running Friday watchdog..."
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID"
        echo "Killed watchdog PID $OLD_PID."
    fi
    rm -f "$PIDFILE"
fi

echo "Stopping any running Friday process..."
pkill -f "friday.py" 2>/dev/null || true
sleep 2

echo "Starting Friday watchdog..."
(
    FAILURES=0
    while true; do
        START=$(date +%s)
        /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 "$SCRIPT_DIR/friday.py"
        EXIT=$?
        ELAPSED=$(( $(date +%s) - START ))
        if [ $ELAPSED -lt 10 ]; then
            FAILURES=$((FAILURES + 1))
        else
            FAILURES=0
        fi
        if [ $FAILURES -ge 5 ]; then
            echo "$(date): Friday crashed 5 times in <10s each. Giving up." >> "$SCRIPT_DIR/logs/friday.log"
            exit 1
        fi
        echo "$(date): Friday stopped (exit $EXIT, uptime ${ELAPSED}s). Restarting in 5s..." >> "$SCRIPT_DIR/logs/friday.log"
        sleep 5
    done
) >> "$SCRIPT_DIR/logs/friday.log" 2>&1 &

WATCHDOG_PID=$!
echo $WATCHDOG_PID > "$PIDFILE"
echo "Watchdog started (PID $WATCHDOG_PID)."

sleep 6

if pgrep -f "friday.py" > /dev/null; then
    echo "Friday is running (PID $(pgrep -f 'friday.py'))."
else
    echo "ERROR: Friday failed to start. Check logs/friday.log."
fi

echo "--- Recent log ---"
tail -5 "$SCRIPT_DIR/logs/friday.log"
