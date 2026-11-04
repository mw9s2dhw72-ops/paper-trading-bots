# Trading-bot evolution (paper trading / backtest only)

100 bots (5 per strategy x 20 strategies, see `STRATEGIES.md`) trade 7 assets on real daily data, then evolve:
the worst 25% are killed each generation and replaced by mutated clones (30% with same-strategy crossover) of
tournament-selected parents from the top half. No real money, broker or exchange account is involved.

## Data (free)
* Yahoo Finance via `yfinance` (adjusted): SPY, QQQ, AAPL, GLD, FXE (euro ETF = forex proxy), plus partners SLV, FXB
* Binance public spot klines API: BTCUSDT, ETHUSDT (from 2017-08-17)
* Daily bars 2016-01-04 .. 2026-11-03, aligned to a business-day calendar (crypto weekend bars dropped; their move rolls into Monday)
* Yahoo's raw FX bars (EURUSD=X) were tried and rejected: Close often equals Open, which corrupted intraday strategies.

## Costs
Per unit of turnover: 10 bps for stocks/ETFs/FX ETF (5 fee + 5 slippage), 20 bps for crypto (10 + 10). Intraday strategies pay
entry + exit. Market-making fills pay half (maker). No borrow cost for shorts, no interest on cash.

## Fitness and anti-overfitting
* A bot trades all 7 assets; its portfolio is the equal-weight average of per-asset returns.
* Fitness = annualised Sharpe - 1.0 x max drawdown.
* Training period 2017-01-01 .. 2022-12-31. Each generation, all bots are scored on the same 3 random 2-year sub-windows
  of the training period (rotating windows), so a bot can't specialise on one fixed stretch.
* Held-out test period 2023-01-01 .. 2026-11-03 is never used for selection; it is only logged.
* Final ranking uses full-period training fitness.
* `sanity_random_walk.py` runs every strategy on synthetic random walks: mean Sharpe should be ~0 (no look-ahead).

## Run
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install numpy pandas matplotlib yfinance requests numba
python data_loader.py                 # download + cache data into data/
python sanity_random_walk.py          # look-ahead check
python evolve.py                      # main run (seed 42, 40 generations) -> results/
python evolve.py --protect-niches --out results_niches   # variant: no extinction
python robustness.py                  # 10 more seeds -> results_seeds/, results/robustness_seeds.csv
python make_charts.py                 # charts/ + results/report_digest.json
```
Options: `--generations`, `--kill-frac`, `--windows`, `--window-years`, `--seed`.

## Outputs
* `results/final_population.csv` - final 100 bots ranked, genome, train/test metrics
* `results/top10_bots.csv`, `results/strategy_champions_niche_run.csv`
* `results/generation_log.csv` - every bot in every generation (fitness, train/test metrics, genome, parent)
* `results/strategy_share.csv`, `results/generation_summary.csv`, `results/summary.json` (benchmarks)
* `results/test_equity_curves.csv`, `results/train_equity_curves.csv`, `results/robustness_seeds.csv`
* `charts/*.png` - fitness per generation, strategy share, test equity vs buy-and-hold, train vs test scatter, per-strategy champions

Not financial advice. Backtests overstate live results (fills, slippage, data snooping, regime change).
