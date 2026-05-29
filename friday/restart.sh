#!/bin/bash
cd "$(dirname "$0")"

echo "Stopping any running Friday process..."
pkill -f "friday.py" 2>/dev/null || true
pkill -f "friday_watchdog" 2>/dev/null || true
sleep 2

echo "Starting Friday watchdog..."
nohup bash -c '
    cd "$(dirname "$0")"
    FAILURES=0
    while true; do
        START=$(date +%s)
        /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 friday.py
        EXIT=$?
        ELAPSED=$(( $(date +%s) - START ))
        if [ $ELAPSED -lt 10 ]; then
            FAILURES=$((FAILURES + 1))
        else
            FAILURES=0
        fi
        if [ $FAILURES -ge 5 ]; then
            echo "$(date): Friday crashed 5 times in <10s each. Giving up." >> logs/friday.log
            exit 1
        fi
        echo "$(date): Friday stopped (exit $EXIT, uptime ${ELAPSED}s). Restarting in 5s..." >> logs/friday.log
        sleep 5
    done
' -- "$(pwd)" >> logs/friday.log 2>&1 &

sleep 6

if pgrep -f "friday.py" > /dev/null; then
    echo "Friday is running (PID $(pgrep -f 'friday.py'))."
else
    echo "ERROR: Friday failed to start. Check logs/friday.log."
fi

echo "--- Recent log ---"
tail -5 logs/friday.log
