"""Backtest engine: run a bot (strategy + genome) over all assets -> equal-weight portfolio returns."""
import numpy as np
import pandas as pd
from data_loader import load_all, ASSETS, COST_BPS
from strategies import STRATEGIES

TRAIN_START, TRAIN_END = "2017-01-01", "2022-12-31"
TEST_START, TEST_END = "2023-01-01", "2026-11-03"
ANN = 252


class Market:
    def __init__(self):
        self.raw = load_all()
        self.index = next(iter(self.raw.values())).index
        self.assets = list(ASSETS)
        self.inputs = {}
        for t in self.assets:
            cls, partner = ASSETS[t]
            df, pdf = self.raw[t], self.raw[partner]
            valid = df["Close"].notna() & pdf["Close"].notna()
            i0 = int(np.argmax(valid.values))
            sl = slice(i0, None)
            self.inputs[t] = dict(
                o=df["Open"].values[sl].astype(float), h=df["High"].values[sl].astype(float),
                l=df["Low"].values[sl].astype(float), c=df["Close"].values[sl].astype(float),
                v=df["Volume"].values[sl].astype(float), pc=pdf["Close"].values[sl].astype(float),
                cost=COST_BPS[cls] / 1e4, i0=i0)
        # availability mask (asset tradable on that date)
        self.avail = pd.DataFrame({t: self.raw[t]["Close"].notna() & self.raw[ASSETS[t][1]]["Close"].notna()
                                   for t in self.assets}, index=self.index)

    def asset_returns(self):
        return pd.DataFrame({t: self.raw[t]["Close"].pct_change(fill_method=None) for t in self.assets}, index=self.index)


def run_bot(market, strategy, genome):
    """Return (portfolio daily net returns Series, per-asset returns DataFrame, per-asset turnover DataFrame)."""
    fn = STRATEGIES[strategy][0]
    n = len(market.index)
    rets, turns = {}, {}
    for t in market.assets:
        d = market.inputs[t]
        r, tr = fn(d, genome)
        full_r = np.zeros(n); full_t = np.zeros(n)
        full_r[d["i0"]:] = np.nan_to_num(r); full_t[d["i0"]:] = np.nan_to_num(tr)
        rets[t], turns[t] = full_r, full_t
    R = pd.DataFrame(rets, index=market.index)
    T = pd.DataFrame(turns, index=market.index)
    w = market.avail.astype(float)
    port = (R * w).sum(axis=1) / w.sum(axis=1).replace(0, np.nan)
    return port.fillna(0.0), R, T


def metrics(r):
    r = pd.Series(r).dropna()
    if len(r) < 20:
        return dict(total_return=0, cagr=0, sharpe=0, max_dd=0, vol=0)
    eq = (1 + r).cumprod()
    yrs = len(r) / ANN
    total = eq.iloc[-1] - 1
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if eq.iloc[-1] > 0 else -1
    sd = r.std()
    sharpe = r.mean() / sd * np.sqrt(ANN) if sd > 1e-12 else 0.0
    dd = (eq / eq.cummax() - 1).min()
    return dict(total_return=float(total), cagr=float(cagr), sharpe=float(sharpe), max_dd=float(-dd), vol=float(sd * np.sqrt(ANN)))


DD_PENALTY = 1.0
def fitness(r):
    """Risk-adjusted fitness: annualised Sharpe minus a drawdown penalty (maxDD as a fraction)."""
    m = metrics(r)
    return m["sharpe"] - DD_PENALTY * m["max_dd"], m
