#!/usr/bin/env bash
# Usage: supervise.sh NAME CMD...   -> restarts CMD forever (5 s back-off), logs to logs/NAME.log
NAME=$1; shift
cd "$(dirname "$0")"; mkdir -p logs
echo $$ > "logs/$NAME.supervisor.pid"
while true; do
  echo "[$(date -u +%FT%TZ)] starting $NAME" >> "logs/$NAME.log"
  "$@" >> "logs/$NAME.log" 2>&1
  rc=$?
  echo "[$(date -u +%FT%TZ)] $NAME exited with code $rc - restarting in 5s" >> "logs/$NAME.log"
  sleep 5
done
