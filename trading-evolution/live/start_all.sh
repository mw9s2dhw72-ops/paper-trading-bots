#!/usr/bin/env bash
# Start (or restart) engine, dashboard and tunnel as detached auto-restarting background processes.
cd "$(dirname "$0")"; mkdir -p logs
PY=../.venv/bin/python
./stop_all.sh >/dev/null 2>&1
setsid nohup ./supervise.sh engine $PY paper_engine.py >/dev/null 2>&1 < /dev/null &
setsid nohup ./supervise.sh web $PY -m uvicorn dashboard:app --host 127.0.0.1 --port 8787 --log-level warning >/dev/null 2>&1 < /dev/null &
setsid nohup ./supervise.sh tunnel ./tunnel.sh >/dev/null 2>&1 < /dev/null &
setsid nohup ./supervise.sh watchdog ./watchdog.sh >/dev/null 2>&1 < /dev/null &
sleep 1; echo started; ps -eo pid,cmd | grep -E "supervise.sh" | grep -v grep
