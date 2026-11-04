# Paper trading bots

**Paper trading only. Simulated money. No brokerage account, no exchange account, no API keys, no real orders.** Nothing here is financial advice. Backtests and a paper book both overstate what a real account would do (fills, slippage, data revisions, regime change).

35 strategy bots and a buy-and-hold benchmark each started with **£1,000**, split across seven sleeves: SPY, QQQ, AAPL, GLD, FXE, BTC and ETH. Idle cash is GBP. Buys convert GBP to USD at the live GBP/USD rate, so the book carries the currency move a UK investor would. Fees are 10 bps on stocks, ETFs and FXE, and 20 bps on crypto. Trades under 5p are skipped.

The research that produced the bots is in [`trading-evolution/`](trading-evolution/README.md) (engine, evolution, stop study, cached daily bars, result tables and charts).

## How a run works

GitHub Actions runs [`trading-evolution/live/run_once.py`](trading-evolution/live/run_once.py) on a schedule. Each run is short and stateless:

1. Load `state.json`, `trades.jsonl` and `equity.json` from the `data` branch.
2. Fetch latest prices and any new **completed** daily bars. Signals use the same strategy code as the backtest, and only on a finished bar (crypto at 00:00 UTC, US stocks and ETFs at 16:00 America/New_York). Weekend crypto bars are dropped, as in the backtest.
3. Fill pending paper orders at the live price while the asset is tradable. US names only trade 09:30–16:00 ET, and only if the quote is under 15 minutes old.
4. Check stop-losses. The live price is checked, and so is the high/low of 1-minute bars since the previous run, so a stop that prints and recovers between runs is still filled. The fill is the stop, or the bar's open if that bar gapped through. Stocks use regular-session bars only.
5. If 00:30 UTC has passed since the last evolution step, run that step (cull the bottom 20% once a bot has 3 live days, rebirth mutated clones of the top 20% with a fresh £1,000, weekly re-training). A missed 00:30 is caught by the next successful run. Several missed days become **one** step, not several culls, because the missing days have no extra live samples.
6. Mark equity to market in GBP and commit the state back to `data`.

A concurrency group stops two runs overlapping. If a push races, the run rebases and retries; it does not force-push.

The book was migrated from the always-on process at **2026-11-04 15:19 UTC** (78 paper trades, balances and open stops unchanged). That snapshot is `trading-evolution/live/migrated/`. The `data` branch starts from the same files.

## Schedule and dashboard

The workflow is [`.github/workflows/paper-trade.yml`](.github/workflows/paper-trade.yml). Cron is `*/5 * * * *`. GitHub often starts scheduled runs a few minutes late, and the Pages deploy adds a bit more, so the dashboard usually moves every **5–15 minutes**, not every 5 minutes on the dot.

Dashboard: <https://mw9s2dhw72-ops.github.io/paper-trading-bots/>

It is the same mobile layout as the previous app (overview, leaderboard, bot page with stop-loss card and lineage, trades, evolution). The banner says paper trading, simulated, no real money, in £. “Last updated” is in UK time. Add it to an iPhone Home Screen from Safari’s Share sheet (Add to Home Screen). A service worker keeps the last copy when the network is down. If that copy is more than 45 minutes old, the page says so in red and still shows the last figures.

Scheduled workflows run only from the default branch (`main`). GitHub turns a schedule off after **60 days with no repository activity**. The `data` branch commits should count as activity. If the schedule is paused anyway, open the Actions tab and re-enable “Paper trade”.

## Data sources

| Feed | Primary | If that fails |
| --- | --- | --- |
| BTC, ETH live price, daily bars, 1-minute bars | [data-api.binance.vision](https://data-api.binance.vision) (same Binance spot market as the backtest) | Coinbase Exchange public API, then Kraken |
| SPY, QQQ, AAPL, GLD, FXE | Yahoo Finance via yfinance (adjusted daily, 1-minute last) | Yahoo chart API, then the cached CSVs in `trading-evolution/data/` for daily history |
| GBP/USD | Yahoo `GBPUSD=X` 1-minute | Yahoo chart API, then [open.er-api.com](https://open.er-api.com) daily rate, then the last rate stored in state |

`api.binance.com` returns **HTTP 451** from US hosts, including GitHub-hosted runners, so the engine does not depend on it. The workflow’s “Verify price sources” step fails the job if vision, Coinbase and Kraken are all down.

Daily history prefers a fresh download so a new completed bar matches the old engine. The CSVs are the fallback and are the backtest’s series, so a Yahoo or Binance outage does not invent a different history.

## What is different from the always-on loop

The always-on loop (`python paper_engine.py` in `trading-evolution/live/`) polled crypto about every 20 seconds and Yahoo about every 60 seconds, and could only run while a machine was awake. On GitHub:

- Prices, fills and equity are updated once per workflow run, about every 5–15 minutes, instead of every 20–60 seconds.
- Stops are checked against the latest price **and** the 1-minute high/low since the previous run (lookback capped at 3 days). The always-on loop only saw the price at the moment it polled, and filled at that live price. A stop noticed only from a 1-minute bar fills at the stop (or at the open, if the bar gapped through).
- A run that starts just after 16:00 ET can still fill a stock stop from regular-session minute bars since the previous run. It will not use an after-hours quote.
- Equity is stored once per run. Points older than 2 days are kept hourly out to 30 days, then one per day. Live evolution only needs the daily last print.
- Evolution still happens at 00:30 UTC. If that run is late, the next run does the step once.

Strategy, genome, costs, the stop rules and the evolution rules are the modules from the always-on engine. They are not reimplemented.

## Pause or reset

**Pause.** Actions → Paper trade → ⋯ → Disable workflow. State stays on `data`. Re-enable the same workflow to continue. You can also run it once from “Run workflow” without waiting for the cron.

**Reset to the migrated snapshot** (2026-11-04 15:19 UTC, 78 trades). Actions → Paper trade → Run workflow → tick “Restore the 2026-11-04 15:19 UTC snapshot”. That copies `trading-evolution/live/migrated/` over the `data` branch files and then takes one step, so the book moves forward from that afternoon rather than sitting frozen.

## What you need to click

Pages is not on for this repository yet, and a scheduled workflow does not run until this change is on `main`.

1. Merge the pull request into `main`.
2. Settings → Pages → Build and deployment → Source: **GitHub Actions**. Save. (The workflow cannot turn this on by itself. Until it is set, the data branch still updates and the deploy job fails with a Pages error.)
3. If a run fails to push to `data` with a permissions error: Settings → Actions → General → Workflow permissions → **Read and write permissions**.
4. Optional: Actions → Paper trade → Run workflow, so the first update does not wait for the next cron slot.

After the first green run, the dashboard is <https://mw9s2dhw72-ops.github.io/paper-trading-bots/>.

## Layout

- `.github/workflows/paper-trade.yml` — schedule, data-branch commit, Pages deploy
- `trading-evolution/live/paper_engine.py` — signals, fills, stops, evolution hook (shared with the old loop)
- `trading-evolution/live/market_data.py` — price sources and fallbacks
- `trading-evolution/live/run_once.py` — one scheduled step
- `trading-evolution/live/migrated/` — seed state, trades and equity
- `site/` — the Pages app (`index.html` plus icons, manifest, service worker). `site/data/` is written on each run and is not committed
- `data` branch — `state.json`, `trades.jsonl`, `equity.json` (the live book)
