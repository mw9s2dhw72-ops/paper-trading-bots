# The 20 strategies

| # | Strategy | One-line description (as implemented) | Evolvable genes | Source |
|---|---|---|---|---|
| 1 | SMA Crossover | Long (or short) when fast SMA is above (below) slow SMA | fast, slow, short | https://insights.wisdomchain.com/top-50-algorithmic-trading-strategies-futures-forex-comprehensive-review/ |
| 2 | EMA Crossover | Same as SMA crossover with exponential averages | fast, slow, short | https://www.quantvps.com/blog/trading-bot-strategies |
| 3 | MACD | Long when MACD line is above its signal line | fast, slow, signal, short | https://www.quantvps.com/blog/trading-bot-strategies |
| 4 | Time-Series Momentum | Sign of trailing N-day return, volatility-targeted position size | lookback, vol_target, rebal, short | https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf |
| 5 | Turtle / Donchian Breakout | Enter on N-day high break, exit on M-day low or ATR stop | entry, exit, stop_atr, short | https://stratcraft.ai/strategies/ |
| 6 | Bollinger Mean Reversion | Buy close below lower band, exit at the middle band | period, k, short | https://stratcraft.ai/strategies/ |
| 7 | RSI Mean Reversion | Connors-style: buy oversold RSI (optionally above a trend MA), exit on RSI recovery | period, low, high, exit, trend_filter, trend, short | https://insights.wisdomchain.com/top-50-algorithmic-trading-strategies-futures-forex-comprehensive-review/ |
| 8 | Z-Score Mean Reversion | Fade large z-scores of log price vs rolling mean | lookback, entry_z, exit_z, short | https://www.quantvps.com/blog/trading-bot-strategies |
| 9 | Stochastic Oscillator | Buy when smoothed %K is oversold, exit when overbought | k_period, d_period, low, high, short | https://github.com/enderekici/trader212/blob/cda3fad1361ee69b8dc9ada3880979e94748226b/docs/glm.md |
| 10 | Keltner Channel Breakout | Buy close above EMA + m*ATR, exit below EMA | period, mult, short | https://cloudquant.github.io/backtrader/en/strategies-series/en/16-volatility.html |
| 11 | Supertrend | ATR trailing band that flips with trend; long when bullish | atr_period, mult, short | https://cloudquant.github.io/backtrader/en/strategies-series/en/16-volatility.html |
| 12 | Ichimoku Cloud | Long above the cloud with Tenkan > Kijun, exit below Kijun | tenkan, kijun, senkou, short | https://fatelessdev-autonome.mintlify.app/trading/sovereign-variant |
| 13 | VWAP Deviation | Rolling-VWAP deviation: revert-to-VWAP or trend-with-VWAP (gene picks mode) | window, threshold, mode, short | https://fatelessdev-autonome.mintlify.app/trading/sovereign-variant |
| 14 | Grid Trading | Spot grid around a moving anchor; hold more as price falls through grid levels | anchor, spacing, levels, short | https://www.quantvps.com/blog/trading-bot-strategies |
| 15 | DCA Bot (Safety Orders) | Base order + scaled safety orders on dips, take profit on average cost | base, dip, scale, tp, max_orders | https://www.quantvps.com/blog/trading-bot-strategies |
| 16 | Pairs / Stat Arb | Trade z-score of the log spread vs a correlated partner (SPY/QQQ, AAPL/QQQ, GLD/SLV, BTC/ETH, FXE/FXB) | lookback, entry_z, exit_z, hedge | https://www.daytrading.com/python-trading-strategies |
| 17 | Market Making (OHLCV proxy) | Quote bid/ask around the open +/- spread*ATR; filled if the day's range touches; inventory skew | spread, max_inv, skew | https://www.quantvps.com/blog/trading-bot-strategies |
| 18 | Volatility Breakout (Williams) | Buy stop at open + k * yesterday's range, exit at the close | k, trend_filter, trend, short | https://www.luxalgo.com/library/concept/volatility-breakout/ |
| 19 | Gap Scalping (intraday proxy) | Fade (or follow) large opening gaps, exit at the close | thresh, mode, short | https://www.quantvps.com/blog/trading-bot-strategies |
| 20 | ML Ridge Forecaster | Walk-forward ridge regression on lagged-return features predicts next-day return | lags, window, refit, ridge, thresh, short | https://www.quantvps.com/blog/trading-bot-strategies |

OHLCV approximations: market making (no order book, queue position or adverse-selection model; fills assumed when the daily range touches a quote),
gap scalping (a daily-bar stand-in for intraday scalping), cross-exchange/funding arbitrage are not reproducible on free OHLCV data and are represented by pairs/stat-arb.
