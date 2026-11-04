# Live paper-trading of the profitable evolved bots

The book now runs as a scheduled GitHub Actions step (`run_once.py`). See the repository README for the schedule, the dashboard and how to pause or reset. The modules in this directory are what that step imports. `migrated/` is the 2026-11-04 15:19 UTC snapshot. The old always-on loop (`paper_engine.py` with no arguments) is still here; it is not what GitHub runs.

PAPER TRADING ONLY: live public prices, virtual money, no broker/exchange accounts, no API keys, no real orders.

* `select_bots.py`: picks bots with positive held-out test return from `results/` and `results_niches/` (190 unique genomes),
  then drops behavioural clones (daily-return correlation >= 0.98 with a better-ranked bot of the same strategy) -> `bots.json` (35 bots).
* `paper_engine.py`: the engine. Each bot gets $10,000 split into 7 asset sleeves. Signals come from completed daily bars, using
  the same strategy code as the backtest. Fills happen at the live price, with the same fee and slippage as the backtest.
  Positions are marked to market every 20 s. State lives in `state.json` and history in `paper.db`.
* `dashboard.py` + `index.html`: mobile web app (PWA manifest + icons), served at `/<token>/` on 127.0.0.1:8787.
* `tunnel.sh`: public HTTPS via tunn3l.sh with a fixed subdomain (`subdomain.txt`). The URL is written to `tunnel_url.txt`.
* `start_all.sh` / `stop_all.sh` / `status.sh`: run everything as detached, auto-restarting processes (`supervise.sh`). Logs are in `logs/`.

Reset the experiment: `./stop_all.sh && rm -f state.json paper.db* && ./start_all.sh`

## Currency: GBP (since 2026-11-04 13:18 UK reset)
Every bot and the buy-and-hold benchmark start with GBP 1,000. The account currency is GBP. Idle cash is held in GBP,
and buys convert GBP to USD at the live GBP/USD rate, so positions carry the USD/GBP currency move, as a UK investor's would.
The rate comes from Yahoo `GBPUSD=X` 1-minute data, refreshed every 60 s, with open.er-api.com's daily rate as a fallback and the
last known rate otherwise. USDT is treated as USD. Fees are percentages, so they're charged on the GBP notional. Units are fractional,
and trades under 5p are skipped. Old USD versions: `paper_engine_usd_backup.py`, `index_usd_backup.html`.

## Live evolution (`evolve_live.py`, runs inside the engine)
* Schedule: daily at 00:30 UTC (= 00:30 UK in winter, 01:30 in summer), set by `CFG` in `evolve_live.py`.
* Fitness = w x live(Sharpe - maxDD of the bot's daily paper equity since birth) + (1 - w) x backtest(Sharpe - maxDD over the
  latest 504 daily bars, which include all live days). w = 0 below 3 live days, then ramps up to 0.5 at 60 days.
* Cull: the bottom 20% (7 of 35), counted only among bots with 3 or more live days. Culled bots are liquidated (with fees) and moved to the graveyard.
* Rebirth: the parent comes from a tournament among the top 20%, with a 30% chance of same-strategy crossover. The best of 8 mutations
  on the recent window becomes the child. Children start with a fresh GBP 1,000 and are ranked on % since birth.
* Retraining: every 7 days, each bot adopts its best of 6 mutations if it beats the current genome by 0.15 fitness on the recent window.
* Fund view: GBP 1,000 + (P&L of living bots + locked P&L of culled bots) / 35. It stays continuous across culls.
* `python dry_run_evolution.py [--ignore-min-days]` simulates one step on the live state without changing anything.

## Keeping the phone URL up
* `watchdog.sh` (v2, supervised; started by start_all.sh, stopped by stop_all.sh, shown in status.sh) every 30 s:
  - fetches `<public URL>/api/summary` (curl -m 8 inside `timeout 12`); after a failure it re-checks in 10 s;
    2 consecutive failures (non-200 / timeout / "No tunnel found") => kills `bin/tunn3l http`, supervisor re-registers in ~5 s;
  - detects that the box was paused/hibernated (wall-clock jump > 90 s) and restarts the tunnel immediately;
  - restarts on disconnect/error lines printed by tunn3l; runs `ensure.sh` each loop so any missing supervisor is started;
  - back-off 30 -> 60 -> 120 s (cap), reset on success; heartbeat line every 10 min. Log: logs/watchdog.log.
* `ensure.sh` - idempotent: starts only missing supervisors (never restarts healthy ones). Run it after a box reboot.
* The page installs a service worker: if the box/tunnel is unreachable the app still opens with the last data and an
  "Offline copy from HH:MM" banner, and refreshes itself when the server is back.
* Known limit: the box is frozen when no agent is active (seen 13:45-14:33 GMT on 4 Nov 2026: equity snapshots stop, tunnel
  registration dies). Nothing on the box can run while it is frozen; there is no cron/systemd/sudo here for @reboot either.
