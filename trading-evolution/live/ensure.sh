#!/usr/bin/env bash
# Idempotent "make sure everything is running": starts only the supervisors that are missing. Safe to call any time.
cd "$(dirname "$0")"; mkdir -p logs
PY=../.venv/bin/python
start(){ setsid nohup ./supervise.sh "$@" >/dev/null 2>&1 < /dev/null & echo "[$(date -u +%FT%TZ)] ensure: started missing supervisor $1" >> logs/ensure.log; }
alive(){ pgrep -f "^bash ./supervise.sh $1 " >/dev/null; }
alive engine   || start engine $PY paper_engine.py
alive web      || start web $PY -m uvicorn dashboard:app --host 127.0.0.1 --port 8787 --log-level warning
alive tunnel   || start tunnel ./tunnel.sh
alive watchdog || start watchdog ./watchdog.sh
exit 0
