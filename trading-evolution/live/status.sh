#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "URL: $(cat tunnel_url.txt 2>/dev/null)"
ps -eo pid,etime,cmd | grep -E "paper_engine|uvicorn dashboard|tunn3l http|watchdog.sh" | grep -v grep | grep -v supervise
echo "--- engine"; tail -n 2 logs/engine.log
echo "--- watchdog"; tail -n 3 logs/watchdog.log 2>/dev/null; tail -n 2 logs/ensure.log 2>/dev/null
