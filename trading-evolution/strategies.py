"""20 trading-bot strategies, each mapping (OHLCV, genome) -> daily net return series.

Conventions (no look-ahead):
  * Position strategies decide pos[t] using data up to and including close[t]; that
    position earns the close-to-close return of day t+1.  Costs = cost * |pos[t]-pos[t-1]|.
  * Intraday strategies (vol breakout, gap fade, market making) enter and exit within
    day t using only open[t] plus data up to close[t-1] for the decision.
"""
import numpy as np
import pandas as pd
from numba import njit

# ---------------------------------------------------------------- helpers

def sma(x, n):
    return pd.Series(x).rolling(int(n), min_periods=int(n)).mean().values

def ema(x, n):
    return pd.Series(x).ewm(span=int(n), adjust=False, min_periods=int(n)).mean().values

def rstd(x, n):
    return pd.Series(x).rolling(int(n), min_periods=int(n)).std().values

def rmax(x, n):
    return pd.Series(x).rolling(int(n), min_periods=int(n)).max().values

def rmin(x, n):
    return pd.Series(x).rolling(int(n), min_periods=int(n)).min().values

def atr(h, l, c, n):
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1.0 / int(n), adjust=False, min_periods=int(n)).mean().values

def rsi(c, n):
    d = np.diff(c, prepend=c[0])
    up = pd.Series(np.where(d > 0, d, 0.0)).ewm(alpha=1.0 / n, adjust=False, min_periods=int(n)).mean().values
    dn = pd.Series(np.where(d < 0, -d, 0.0)).ewm(alpha=1.0 / n, adjust=False, min_periods=int(n)).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = up / dn
    return 100 - 100 / (1 + rs)

def nz(x):
    return np.nan_to_num(x, nan=0.0)

@njit(cache=True)
def state_machine(el, xl, es, xs, allow_short):
    """Boolean entry/exit arrays -> position in {-1,0,1} (path dependent)."""
    n = el.shape[0]
    pos = np.zeros(n)
    p = 0.0
    for t in range(n):
        if p == 0.0:
            if el[t]:
                p = 1.0
            elif allow_short and es[t]:
                p = -1.0
        elif p > 0:
            if xl[t]:
                p = 0.0
                if allow_short and es[t]:
                    p = -1.0
        else:
            if xs[t]:
                p = 0.0
                if el[t]:
                    p = 1.0
        pos[t] = p
    return pos

@njit(cache=True)
def turtle_loop(c, hi_n, lo_n, hi_m, lo_m, a, stop_mult, allow_short):
    n = c.shape[0]
    pos = np.zeros(n)
    p = 0.0
    stop = 0.0
    for t in range(1, n):
        if np.isnan(hi_n[t - 1]) or np.isnan(a[t]):
            pos[t] = 0.0
            continue
        if p == 0.0:
            if c[t] > hi_n[t - 1]:
                p = 1.0; stop = c[t] - stop_mult * a[t]
            elif allow_short and c[t] < lo_n[t - 1]:
                p = -1.0; stop = c[t] + stop_mult * a[t]
        elif p > 0:
            if c[t] < lo_m[t - 1] or c[t] < stop:
                p = 0.0
        else:
            if c[t] > hi_m[t - 1] or c[t] > stop:
                p = 0.0
        pos[t] = p
    return pos

@njit(cache=True)
def grid_loop(c, anchor_src, spacing, levels, allow_short):
    n = c.shape[0]
    pos = np.zeros(n)
    anchor = np.nan
    for t in range(n):
        if np.isnan(anchor):
            if not np.isnan(anchor_src[t]):
                anchor = anchor_src[t]
            else:
                continue
        dev = (c[t] / anchor - 1.0) / spacing        # in grid steps
        if abs(dev) > levels + 1:                    # price left the grid -> re-centre
            anchor = c[t]
            dev = 0.0
        k = np.floor(dev + 0.5)
        if allow_short:
            p = -k / levels
            p = min(1.0, max(-1.0, p))
        else:
            p = 0.5 - k / (2.0 * levels)
            p = min(1.0, max(0.0, p))
        pos[t] = p
    return pos

@njit(cache=True)
def dca_loop(c, valid, base, dip, scale, tp, max_orders):
    """3Commas-style DCA bot: base order, safety orders every `dip` drop, take-profit on avg cost."""
    n = c.shape[0]
    pos = np.zeros(n)
    p = 0.0; avg = 0.0; last = 0.0; k = 0
    size0 = base
    for t in range(n):
        if not valid[t]:
            continue
        if p == 0.0:
            p = size0; avg = c[t]; last = c[t]; k = 0
        else:
            if c[t] >= avg * (1 + tp):
                p = 0.0; k = 0                         # take profit; re-enter next bar
            elif k < max_orders and c[t] <= last * (1 - dip) and p < 1.0:
                add = min(size0 * scale ** (k + 1), 1.0 - p)
                avg = (avg * p + c[t] * add) / (p + add)
                p += add; last = c[t]; k += 1
        pos[t] = p
    return pos

@njit(cache=True)
def mm_loop(o, h, l, c, a_pct, spread_mult, max_inv, skew, fee):
    """OHLCV market-making proxy: quote bid/ask around the open; a quote is filled if the
    day's range touches it; inventory is marked to the close. Returns daily net return."""
    n = c.shape[0]
    ret = np.zeros(n)
    q = 0.0
    clip = 1.0 / max_inv
    for t in range(1, n):
        if np.isnan(a_pct[t - 1]) or c[t - 1] <= 0:
            continue
        r = q * (c[t] / c[t - 1] - 1.0)               # carry inventory overnight
        s = spread_mult * a_pct[t - 1]
        mid = o[t] * (1.0 - skew * q * s)             # skew quotes to shed inventory
        bid = mid * (1 - s); ask = mid * (1 + s)
        if l[t] <= bid and q < 1.0:
            sz = min(clip, 1.0 - q)
            r += sz * (c[t] / bid - 1.0) - fee * sz
            q += sz
        if h[t] >= ask and q > -1.0:
            sz = min(clip, q + 1.0)
            r += sz * (1.0 - c[t] / ask) - fee * sz
            q -= sz
        ret[t] = r
    return ret

def pos_to_ret(pos, c, cost):
    pos = nz(pos)
    r = np.zeros_like(c)
    r[1:] = c[1:] / c[:-1] - 1
    r = nz(r)
    held = np.zeros_like(pos); held[1:] = pos[:-1]
    turn = np.abs(np.diff(pos, prepend=0.0))
    net = held * r - cost * turn
    return net, turn

# ---------------------------------------------------------------- strategies
# Each returns (net_ret, turnover). d: dict of numpy arrays o,h,l,c,v,pc (partner close), cost

def s_sma_cross(d, g):
    f, s = int(g["fast"]), int(g["slow"])
    if f >= s: s = f + 5
    sig = np.where(sma(d["c"], f) > sma(d["c"], s), 1.0, -1.0 if g["short"] else 0.0)
    sig[np.isnan(sma(d["c"], s))] = 0
    return pos_to_ret(sig, d["c"], d["cost"])

def s_ema_cross(d, g):
    f, s = int(g["fast"]), int(g["slow"])
    if f >= s: s = f + 5
    ef, es_ = ema(d["c"], f), ema(d["c"], s)
    sig = np.where(ef > es_, 1.0, -1.0 if g["short"] else 0.0); sig[np.isnan(es_)] = 0
    return pos_to_ret(sig, d["c"], d["cost"])

def s_macd(d, g):
    f, s, sg = int(g["fast"]), int(g["slow"]), int(g["signal"])
    if f >= s: s = f + 5
    m = ema(d["c"], f) - ema(d["c"], s)
    sl = pd.Series(m).ewm(span=sg, adjust=False).mean().values
    sig = np.where(m > sl, 1.0, -1.0 if g["short"] else 0.0); sig[np.isnan(m)] = 0
    return pos_to_ret(sig, d["c"], d["cost"])

def s_tsmom(d, g):
    lb = int(g["lookback"])
    c = d["c"]
    mom = c / pd.Series(c).shift(lb).values - 1
    r = np.diff(c, prepend=c[0]) / c
    vol = pd.Series(r).rolling(60, min_periods=60).std().values * np.sqrt(252)
    lev = np.clip(g["vol_target"] / np.where(vol > 0, vol, np.nan), 0, 2.0)
    direction = np.where(mom > 0, 1.0, -1.0 if g["short"] else 0.0)
    pos = nz(direction * lev)
    # rebalance only every `rebal` days to limit turnover
    rb = int(g["rebal"]); pos = pd.Series(pos).iloc[::rb].reindex(range(len(pos))).ffill().values
    return pos_to_ret(pos, c, d["cost"])

def s_turtle(d, g):
    c, h, l = d["c"], d["h"], d["l"]
    N, M = int(g["entry"]), int(g["exit"])
    a = atr(h, l, c, 20)
    pos = turtle_loop(c, rmax(h, N), rmin(l, N), rmax(h, M), rmin(l, M), a, g["stop_atr"], bool(g["short"]))
    return pos_to_ret(pos, c, d["cost"])

def s_bollinger(d, g):
    c = d["c"]; n = int(g["period"]); k = g["k"]
    m, sd = sma(c, n), rstd(c, n)
    up, lo = m + k * sd, m - k * sd
    pos = state_machine(nz(c < lo).astype(np.bool_), nz(c >= m).astype(np.bool_),
                        nz(c > up).astype(np.bool_), nz(c <= m).astype(np.bool_), bool(g["short"]))
    return pos_to_ret(pos, c, d["cost"])

def s_rsi(d, g):
    c = d["c"]; x = rsi(c, int(g["period"]))
    trend = c > sma(c, int(g["trend"])) if g["trend_filter"] else np.ones_like(c, bool)
    el = nz((x < g["low"]) & trend).astype(np.bool_)
    xl = nz(x > g["exit"]).astype(np.bool_)
    es = nz((x > g["high"]) & ~trend if g["trend_filter"] else x > g["high"]).astype(np.bool_)
    xs = nz(x < 100 - g["exit"]).astype(np.bool_)
    pos = state_machine(el, xl, es, xs, bool(g["short"]))
    return pos_to_ret(pos, c, d["cost"])

def s_zscore(d, g):
    c = d["c"]; n = int(g["lookback"])
    lc = np.log(c); z = (lc - sma(lc, n)) / rstd(lc, n)
    pos = state_machine(nz(z < -g["entry_z"]).astype(np.bool_), nz(z > -g["exit_z"]).astype(np.bool_),
                        nz(z > g["entry_z"]).astype(np.bool_), nz(z < g["exit_z"]).astype(np.bool_), bool(g["short"]))
    return pos_to_ret(pos, c, d["cost"])

def s_stochastic(d, g):
    c, h, l = d["c"], d["h"], d["l"]; n = int(g["k_period"])
    hh, ll = rmax(h, n), rmin(l, n)
    k = 100 * (c - ll) / np.where(hh - ll > 0, hh - ll, np.nan)
    kd = sma(nz(k) + 0, int(g["d_period"]))
    pos = state_machine(nz(kd < g["low"]).astype(np.bool_), nz(kd > g["high"]).astype(np.bool_),
                        nz(kd > g["high"]).astype(np.bool_), nz(kd < g["low"]).astype(np.bool_), bool(g["short"]))
    return pos_to_ret(pos, c, d["cost"])

def s_keltner(d, g):
    c, h, l = d["c"], d["h"], d["l"]; n = int(g["period"])
    mid = ema(c, n); a = atr(h, l, c, n)
    up, lo = mid + g["mult"] * a, mid - g["mult"] * a
    pos = state_machine(nz(c > up).astype(np.bool_), nz(c < mid).astype(np.bool_),
                        nz(c < lo).astype(np.bool_), nz(c > mid).astype(np.bool_), bool(g["short"]))
    return pos_to_ret(pos, c, d["cost"])

@njit(cache=True)
def _supertrend(h, l, c, a, mult):
    n = c.shape[0]; dirn = np.zeros(n); fu = np.zeros(n); fl = np.zeros(n)
    for t in range(n):
        if np.isnan(a[t]):
            continue
        mid = (h[t] + l[t]) / 2
        bu, bl = mid + mult * a[t], mid - mult * a[t]
        if t == 0 or dirn[t - 1] == 0:
            fu[t], fl[t], dirn[t] = bu, bl, 1.0
            continue
        fu[t] = bu if (bu < fu[t - 1] or c[t - 1] > fu[t - 1]) else fu[t - 1]
        fl[t] = bl if (bl > fl[t - 1] or c[t - 1] < fl[t - 1]) else fl[t - 1]
        if dirn[t - 1] > 0:
            dirn[t] = -1.0 if c[t] < fl[t] else 1.0
        else:
            dirn[t] = 1.0 if c[t] > fu[t] else -1.0
    return dirn

def s_supertrend(d, g):
    c, h, l = d["c"], d["h"], d["l"]
    dirn = _supertrend(h, l, c, atr(h, l, c, int(g["atr_period"])), g["mult"])
    pos = np.where(dirn > 0, 1.0, np.where(dirn < 0, -1.0 if g["short"] else 0.0, 0.0))
    return pos_to_ret(pos, c, d["cost"])

def s_ichimoku(d, g):
    c, h, l = d["c"], d["h"], d["l"]
    tk, kj, sb = int(g["tenkan"]), int(g["kijun"]), int(g["senkou"])
    ten = (rmax(h, tk) + rmin(l, tk)) / 2; kij = (rmax(h, kj) + rmin(l, kj)) / 2
    sa = pd.Series((ten + kij) / 2).shift(kj).values
    sbv = pd.Series((rmax(h, sb) + rmin(l, sb)) / 2).shift(kj).values
    top, bot = np.fmax(sa, sbv), np.fmin(sa, sbv)
    long_ = (c > top) & (ten > kij); short_ = (c < bot) & (ten < kij)
    pos = state_machine(nz(long_).astype(np.bool_), nz(c < kij).astype(np.bool_),
                        nz(short_).astype(np.bool_), nz(c > kij).astype(np.bool_), bool(g["short"]))
    return pos_to_ret(pos, c, d["cost"])

def s_vwap(d, g):
    c, h, l, v = d["c"], d["h"], d["l"], d["v"]; n = int(g["window"])
    tp = (h + l + c) / 3
    vw = pd.Series(tp * v).rolling(n, min_periods=n).sum().values / pd.Series(v).rolling(n, min_periods=n).sum().values
    dev = c / vw - 1
    th = g["threshold"]
    if g["mode"] == 0:   # mean reversion to VWAP
        pos = state_machine(nz(dev < -th).astype(np.bool_), nz(dev >= 0).astype(np.bool_),
                            nz(dev > th).astype(np.bool_), nz(dev <= 0).astype(np.bool_), bool(g["short"]))
    else:                # trend: above VWAP by threshold
        pos = state_machine(nz(dev > th).astype(np.bool_), nz(dev < 0).astype(np.bool_),
                            nz(dev < -th).astype(np.bool_), nz(dev > 0).astype(np.bool_), bool(g["short"]))
    return pos_to_ret(pos, c, d["cost"])

def s_grid(d, g):
    c = d["c"]
    pos = grid_loop(c, sma(c, int(g["anchor"])), g["spacing"], int(g["levels"]), bool(g["short"]))
    return pos_to_ret(pos, c, d["cost"])

def s_dca(d, g):
    c = d["c"]; valid = ~np.isnan(c)
    pos = dca_loop(np.nan_to_num(c, nan=1.0), valid, g["base"], g["dip"], g["scale"], g["tp"], int(g["max_orders"]))
    return pos_to_ret(pos, c, d["cost"])

def s_pairs(d, g):
    c, pc = d["c"], d["pc"]; n = int(g["lookback"])
    spr = np.log(c) - g["hedge"] * np.log(pc)
    z = (spr - sma(spr, n)) / rstd(spr, n)
    pos = state_machine(nz(z < -g["entry_z"]).astype(np.bool_), nz(z > -g["exit_z"]).astype(np.bool_),
                        nz(z > g["entry_z"]).astype(np.bool_), nz(z < g["exit_z"]).astype(np.bool_), True)
    ra = nz(np.r_[0, c[1:] / c[:-1] - 1]); rb = nz(np.r_[0, pc[1:] / pc[:-1] - 1])
    held = np.r_[0, pos[:-1]]
    turn = np.abs(np.diff(pos, prepend=0.0))
    net = held * (ra - g["hedge"] * rb) - d["cost"] * turn * (1 + g["hedge"])
    return net, turn

def s_market_making(d, g):
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    a_pct = atr(h, l, c, 14) / c
    ret = mm_loop(o, h, l, c, a_pct, g["spread"], int(g["max_inv"]), g["skew"], d["cost"] * 0.5)
    return ret, np.ones_like(c) * 0.0

def s_vol_breakout(d, g):
    """Larry Williams volatility breakout: buy if price rises k*(prev range) above the open, exit at close."""
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    rng = np.r_[np.nan, (h - l)[:-1]]
    trig = o + g["k"] * rng
    trend = c > sma(c, int(g["trend"])); trend = np.r_[False, trend[:-1]]
    if not g["trend_filter"]: trend = np.ones_like(c, bool)
    hit = (h >= trig) & trend & ~np.isnan(rng)
    entry = np.maximum(trig, o)
    ret = np.where(hit, c / entry - 1 - 2 * d["cost"], 0.0)
    if g["short"]:
        trig_s = o - g["k"] * rng
        hit_s = (l <= trig_s) & ~np.isnan(rng) & ~hit
        ret = ret + np.where(hit_s, 1 - c / np.minimum(trig_s, o) - 2 * d["cost"], 0.0)
    return nz(ret), nz(hit.astype(float))

def s_gap_scalp(d, g):
    """Daily-bar scalping proxy: trade the opening gap (fade or follow) and exit at the close."""
    o, c = d["o"], d["c"]
    gap = o / np.r_[np.nan, c[:-1]] - 1
    sd = pd.Series(gap).rolling(60, min_periods=20).std().shift(1).values
    sig = np.where(gap > g["thresh"] * sd, -1.0, np.where(gap < -g["thresh"] * sd, 1.0, 0.0))
    if g["mode"] == 1: sig = -sig            # follow instead of fade
    if not g["short"]: sig = np.maximum(sig, 0)
    ret = sig * (c / o - 1) - 2 * d["cost"] * np.abs(sig)
    return nz(ret), np.abs(sig)

def s_ml_ridge(d, g):
    """Walk-forward ridge regression on lagged-return features (re-fit every `refit` days on trailing window)."""
    c = d["c"]; r = nz(np.r_[0, c[1:] / c[:-1] - 1])
    L = int(g["lags"]); W = int(g["window"]); RF = int(g["refit"]); lam = g["ridge"]
    feats = [pd.Series(r).shift(i).values for i in range(L)]
    feats += [pd.Series(c).pct_change(20).values, pd.Series(r).rolling(20).std().values,
              (c / sma(c, 50) - 1)]
    X = np.column_stack(feats); y = np.r_[r[1:], np.nan]  # target: next-day return
    n = len(c); pred = np.full(n, np.nan)
    good = ~np.isnan(X).any(1) & ~np.isnan(c)
    t = W + 60
    while t < n:
        lo = t - W
        mtr = good[lo:t - 1].copy()
        Xt, yt = X[lo:t - 1][mtr], y[lo:t - 1][mtr]  # y[t-2] uses r[t-1]: known at t-1
        if len(yt) > 50:
            mu, sdv = Xt.mean(0), Xt.std(0) + 1e-12
            Z = (Xt - mu) / sdv
            beta = np.linalg.solve(Z.T @ Z + lam * len(yt) * np.eye(Z.shape[1]), Z.T @ (yt - yt.mean()))
            hi = min(n, t + RF)
            Zp = (X[t:hi] - mu) / sdv
            pred[t:hi] = Zp @ beta + yt.mean()
            ystd = yt.std()
            pred[t:hi] /= ystd
        t += RF
    pos = np.where(pred > g["thresh"], 1.0, np.where(pred < -g["thresh"], -1.0 if g["short"] else 0.0, 0.0))
    pos[np.isnan(pred)] = 0
    return pos_to_ret(pos, c, d["cost"])

# ---------------------------------------------------------------- registry
# param spec: name -> (low, high, kind)  kind: 'i' int, 'f' float, 'b' bool(0/1), 'c' category(int)
S = "b"
STRATEGIES = {
    "SMA Crossover": (s_sma_cross, {"fast": (5, 60, "i"), "slow": (30, 250, "i"), "short": (0, 1, S)}),
    "EMA Crossover": (s_ema_cross, {"fast": (3, 50, "i"), "slow": (20, 200, "i"), "short": (0, 1, S)}),
    "MACD": (s_macd, {"fast": (5, 20, "i"), "slow": (20, 60, "i"), "signal": (3, 20, "i"), "short": (0, 1, S)}),
    "Time-Series Momentum": (s_tsmom, {"lookback": (20, 260, "i"), "vol_target": (0.05, 0.4, "f"), "rebal": (1, 21, "i"), "short": (0, 1, S)}),
    "Turtle / Donchian Breakout": (s_turtle, {"entry": (10, 100, "i"), "exit": (5, 50, "i"), "stop_atr": (1.0, 5.0, "f"), "short": (0, 1, S)}),
    "Bollinger Mean Reversion": (s_bollinger, {"period": (10, 60, "i"), "k": (1.0, 3.0, "f"), "short": (0, 1, S)}),
    "RSI Mean Reversion": (s_rsi, {"period": (2, 21, "i"), "low": (5, 40, "f"), "high": (60, 95, "f"), "exit": (40, 80, "f"), "trend_filter": (0, 1, S), "trend": (50, 250, "i"), "short": (0, 1, S)}),
    "Z-Score Mean Reversion": (s_zscore, {"lookback": (10, 100, "i"), "entry_z": (1.0, 3.0, "f"), "exit_z": (-0.5, 0.8, "f"), "short": (0, 1, S)}),
    "Stochastic Oscillator": (s_stochastic, {"k_period": (5, 30, "i"), "d_period": (1, 7, "i"), "low": (5, 35, "f"), "high": (65, 95, "f"), "short": (0, 1, S)}),
    "Keltner Channel Breakout": (s_keltner, {"period": (10, 60, "i"), "mult": (0.5, 3.0, "f"), "short": (0, 1, S)}),
    "Supertrend": (s_supertrend, {"atr_period": (5, 40, "i"), "mult": (1.0, 5.0, "f"), "short": (0, 1, S)}),
    "Ichimoku Cloud": (s_ichimoku, {"tenkan": (5, 20, "i"), "kijun": (20, 60, "i"), "senkou": (40, 120, "i"), "short": (0, 1, S)}),
    "VWAP Deviation": (s_vwap, {"window": (5, 60, "i"), "threshold": (0.005, 0.08, "f"), "mode": (0, 1, "c"), "short": (0, 1, S)}),
    "Grid Trading": (s_grid, {"anchor": (10, 120, "i"), "spacing": (0.01, 0.08, "f"), "levels": (2, 10, "i"), "short": (0, 1, S)}),
    "DCA Bot (Safety Orders)": (s_dca, {"base": (0.1, 0.5, "f"), "dip": (0.02, 0.15, "f"), "scale": (1.0, 2.0, "f"), "tp": (0.02, 0.3, "f"), "max_orders": (1, 6, "i")}),
    "Pairs / Stat Arb": (s_pairs, {"lookback": (20, 120, "i"), "entry_z": (1.0, 3.0, "f"), "exit_z": (-0.5, 0.8, "f"), "hedge": (0.5, 1.5, "f")}),
    "Market Making (OHLCV proxy)": (s_market_making, {"spread": (0.1, 1.5, "f"), "max_inv": (2, 10, "i"), "skew": (0.0, 2.0, "f")}),
    "Volatility Breakout (Williams)": (s_vol_breakout, {"k": (0.2, 1.2, "f"), "trend_filter": (0, 1, S), "trend": (20, 200, "i"), "short": (0, 1, S)}),
    "Gap Scalping (intraday proxy)": (s_gap_scalp, {"thresh": (0.3, 3.0, "f"), "mode": (0, 1, "c"), "short": (0, 1, S)}),
    "ML Ridge Forecaster": (s_ml_ridge, {"lags": (2, 10, "i"), "window": (250, 1000, "i"), "refit": (10, 63, "i"), "ridge": (0.001, 1.0, "f"), "thresh": (0.0, 0.3, "f"), "short": (0, 1, S)}),
}
assert len(STRATEGIES) == 20
