#!/usr/bin/env bash
# Public-URL watchdog (v2). Every INTERVAL s:
#  - checks the public URL (hard timeouts); 2 consecutive failures (non-200 / timeout / "No tunnel found") => restart tunnel
#  - detects the box having been paused/hibernated (wall-clock jump) => restarts the tunnel immediately (registration is dead after a pause)
#  - watches tunnel.log for disconnect/error lines from tunn3l => restart
#  - calls ensure.sh so any missing component is restarted
# Restart back-off 30 -> 60 -> 120 s (cap), reset after a healthy check. Log: logs/watchdog.log (via supervise.sh)
cd "$(dirname "$0")"
INTERVAL=${INTERVAL:-30}; FAILS=0; BACKOFF=30; LAST_RESTART=0; PREV=$(date +%s)
TLOG=logs/tunnel.log; TPOS=$(stat -c %s "$TLOG" 2>/dev/null || echo 0)
log(){ echo "[$(date -u +%FT%TZ)] $*"; }
restart_tunnel(){  # $1 = reason; $2 = "force" to ignore back-off
  local now=$(date +%s)
  if [ "$2" != force ] && [ $((now-LAST_RESTART)) -lt $BACKOFF ]; then log "($1) - in back-off, $((BACKOFF-(now-LAST_RESTART)))s left"; return; fi
  log "restarting tunnel: $1"
  pkill -f "bin/tunn3l http"; sleep 2; pkill -9 -f "bin/tunn3l http"; pkill -CONT -f "bin/tunn3l http"
  LAST_RESTART=$(date +%s); BACKOFF=$(( BACKOFF*2 > 120 ? 120 : BACKOFF*2 )); FAILS=0
  TPOS=$(stat -c %s "$TLOG" 2>/dev/null || echo 0)
}
log "watchdog v2 start (interval ${INTERVAL}s, back-off cap 120s)"
while true; do
  NOW=$(date +%s); GAP=$((NOW-PREV)); PREV=$NOW
  timeout 20 ./ensure.sh
  if [ $GAP -gt $((INTERVAL+60)) ]; then
    log "box was paused/asleep for ~${GAP}s (clock jump)"; sleep 3; restart_tunnel "resume after pause" force; sleep 10; PREV=$(date +%s); continue
  fi
  # tunn3l's own output: new disconnect/error lines since last look
  SZ=$(stat -c %s "$TLOG" 2>/dev/null || echo 0); [ "$SZ" -lt "$TPOS" ] && TPOS=0
  if [ "$SZ" -gt "$TPOS" ]; then
    NEW=$(tail -c +$((TPOS+1)) "$TLOG" | grep -v '^\[' | grep -iE "disconnect|error|closed|lost|reconnect|fail" | head -1); TPOS=$SZ
    [ -n "$NEW" ] && { log "tunn3l reported: ${NEW:0:120}"; restart_tunnel "tunn3l reported a disconnect"; }
  fi
  URL=$(cat tunnel_url.txt 2>/dev/null)
  if [ -n "$URL" ]; then
    BODY=$(timeout 12 curl -s -m 8 -w $'\n%{http_code}' "${URL}api/summary" 2>/dev/null); CODE=${BODY##*$'\n'}; [ -z "$CODE" ] && CODE=000
    if [ "$CODE" = "200" ] && ! grep -q "No tunnel found" <<<"$BODY"; then
      [ $FAILS -gt 0 ] && log "OK again (HTTP 200) after $FAILS failure(s)"
      FAILS=0; BACKOFF=30
      [ $((NOW-${LAST_HB:-0})) -ge 600 ] && { log "heartbeat: public URL OK"; LAST_HB=$NOW; }
    else
      FAILS=$((FAILS+1)); SNIP=$(head -c 80 <<<"${BODY%$'\n'*}" | tr -d '\n')
      LOCAL=$(timeout 10 curl -s -o /dev/null -m 8 -w '%{http_code}' "http://127.0.0.1:8787/$(cat token.txt)/api/summary")
      log "check failed #$FAILS: public HTTP $CODE ${SNIP} | local HTTP $LOCAL"
      [ $FAILS -ge 2 ] && restart_tunnel "$FAILS consecutive public failures"
      sleep 10; continue   # re-check quickly after a failure
    fi
  fi
  sleep "$INTERVAL"
done
