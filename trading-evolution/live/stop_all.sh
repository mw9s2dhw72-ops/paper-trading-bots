#!/usr/bin/env bash
cd "$(dirname "$0")"
for n in watchdog engine web tunnel; do
  f=logs/$n.supervisor.pid; [ -f $f ] && kill -- -$(ps -o pgid= $(cat $f) | tr -d ' ') 2>/dev/null; rm -f $f
done
pkill -f "watchdog.sh" 2>/dev/null; pkill -f "bin/tunn3l http" 2>/dev/null; pkill -f "bin/cloudflared" 2>/dev/null; pkill -f "paper_engine.py" 2>/dev/null; pkill -f "uvicorn dashboard:app" 2>/dev/null
echo stopped
