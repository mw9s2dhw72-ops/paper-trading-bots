"""Stop-loss overlays for position-based bots (shared by the backtest and the live paper engine).

Conventions match strategies.pos_to_ret: raw pos[t] is decided at close t and held over day t+1.
A stop level for day t+1 is known at close t.  On day t+1 the stop triggers if the bar's low (long) /
high (short) crosses the level; the fill is the stop level, or the OPEN if the bar gapped through it.
Fees (same bps as entries/exits) are charged on the stop exit.  After a stop-out the sleeve stays flat
until the strategy's raw signal stops pointing the stopped direction (goes flat or flips): no immediate
re-entry on the same signal.

Stop families (params live in the genome as stop_type / stop_mult):
  none        - no overlay (identical to pos_to_ret)
  atr         - fixed initial stop: entry -/+ mult * ATR(20) at entry (Turtle "2N"-style), never moves
  chandelier  - trailing: max(previous level, highest high since entry - mult * ATR(22)) for longs
                (mirror for shorts), ratcheted once per completed daily bar (Le Beau chandelier exit)
"""
import numpy as np
from numba import njit
import strategies as S

STOP_TYPES = {"none": 0, "atr": 1, "chandelier": 2}
ATR_PERIOD = {"atr": 20, "chandelier": 22}
MULT_RANGE = (1.5, 6.0)
LABEL = {"none": "No stop-loss", "atr": "Fixed ATR stop", "chandelier": "Chandelier trailing stop"}


def stop_cfg(genome):
    t = genome.get("stop_type", "none") or "none"
    m = float(genome.get("stop_mult", 0.0) or 0.0)
    if t not in STOP_TYPES or m <= 0: t, m = "none", 0.0
    return t, m


def describe(genome):
    t, m = stop_cfg(genome)
    if t == "none": return "No stop-loss (the strategy's own exit only)"
    if t == "atr": return f"Fixed stop {m:g}x ATR(20) from entry"
    return f"Chandelier trailing stop: highest high since entry - {m:g}x ATR(22)"


def raw_positions(strategy, d, genome):
    """Run a strategy and capture its raw target-position array (strategies call S.pos_to_ret by global name)."""
    cap = {}
    orig = S.pos_to_ret
    def grab(pos, c, cst):
        cap["pos"] = np.nan_to_num(np.asarray(pos, dtype=float))
        return orig(pos, c, cst)
    S.pos_to_ret = grab
    try:
        out = S.STRATEGIES[strategy][0](d, genome)
    finally:
        S.pos_to_ret = orig
    return cap.get("pos"), out


@njit(cache=True)
def _overlay(pos, o, h, l, c, a, cost, stype, mult):
    n = c.shape[0]
    net = np.zeros(n); eff = np.zeros(n); stopped = np.zeros(n); level_out = np.full(n, np.nan)
    trade_ret = np.zeros(n); trade_end = np.zeros(n, np.bool_)   # compounded sleeve return of each trade, at its exit index
    e_prev = 0.0; lock = 0.0; level = np.nan; ext = np.nan; acc = 1.0; in_trade = False
    for t in range(n):
        r = c[t] / c[t - 1] - 1.0 if t > 0 else 0.0
        hit = False; fill = 0.0
        # ---- stop check during day t on the position held from close t-1
        if t > 0 and e_prev != 0.0 and stype > 0 and not np.isnan(level):
            if e_prev > 0:
                if o[t] <= level: hit = True; fill = o[t]
                elif l[t] <= level: hit = True; fill = level
            else:
                if o[t] >= level: hit = True; fill = o[t]
                elif h[t] >= level: hit = True; fill = level
        p = pos[t]
        if hit:
            day = e_prev * (fill / c[t - 1] - 1.0) - cost * abs(e_prev)
            stopped[t] = 1.0
            lock = 1.0 if e_prev > 0 else -1.0
            e_prev_sign = lock
            acc *= (1.0 + day); trade_ret[t] = acc - 1.0; trade_end[t] = True; acc = 1.0; in_trade = False
            # same close: allowed to enter only if the raw signal no longer points the stopped way
            if np.sign(p) != lock:
                lock = 0.0
                e = p
            else:
                e = 0.0
            day -= cost * abs(e)
            net[t] = day
            level = np.nan
            if e != 0.0:
                in_trade = True; acc = 1.0 - cost * abs(e)
                ext = h[t] if e > 0 else l[t]
                if stype > 0 and not np.isnan(a[t]):
                    level = c[t] - mult * a[t] if e > 0 else c[t] + mult * a[t]
            eff[t] = e; level_out[t] = level; e_prev = e
            continue
        # ---- normal day: follow the raw signal unless locked out after a stop
        if lock != 0.0:
            if np.sign(p) == lock: p = 0.0
            else: lock = 0.0
        e = p
        day = e_prev * r - cost * abs(e - e_prev)
        net[t] = day
        new_entry = e != 0.0 and np.sign(e) != np.sign(e_prev)
        if in_trade:
            if e == 0.0 or new_entry:       # trade closed (flat or flip) at this close
                acc *= (1.0 + day); trade_ret[t] = acc - 1.0; trade_end[t] = True; acc = 1.0; in_trade = False
                level = np.nan
            else:
                acc *= (1.0 + day)
        if new_entry:
            in_trade = True; acc = 1.0 - cost * abs(e)
            ext = h[t] if e > 0 else l[t]
            if stype > 0 and not np.isnan(a[t]):
                level = c[t] - mult * a[t] if e > 0 else c[t] + mult * a[t]
            else:
                level = np.nan
        elif e != 0.0 and stype == 2 and not np.isnan(a[t]):
            if e > 0:
                ext = max(ext, h[t]); cand = ext - mult * a[t]
                level = cand if np.isnan(level) else max(level, cand)
            else:
                ext = min(ext, l[t]); cand = ext + mult * a[t]
                level = cand if np.isnan(level) else min(level, cand)
        elif e != 0.0 and stype == 1 and np.isnan(level) and not np.isnan(a[t]):
            level = c[t] - mult * a[t] if e > 0 else c[t] + mult * a[t]
        eff[t] = e; level_out[t] = level if e != 0.0 else np.nan; e_prev = e
    return net, eff, stopped, level_out, trade_ret, trade_end


def apply(pos, d, genome):
    stype_s, mult = stop_cfg(genome)
    stype = STOP_TYPES[stype_s]
    a = S.atr(d["h"], d["l"], d["c"], ATR_PERIOD.get(stype_s, 20))
    return _overlay(np.nan_to_num(pos).astype(float), d["o"].astype(float), d["h"].astype(float), d["l"].astype(float),
                    d["c"].astype(float), np.asarray(a, dtype=float), float(d["cost"]), stype, mult)


def run(strategy, d, genome):
    """(net daily returns, effective position, stopped flags, stop levels, trade returns, trade-end mask)."""
    pos, _ = raw_positions(strategy, d, genome)
    return apply(pos, d, genome)


def initial_level(direction, entry_px, atr_val, genome):
    t, m = stop_cfg(genome)
    if t == "none" or not np.isfinite(atr_val): return None
    return entry_px - m * atr_val if direction > 0 else entry_px + m * atr_val


def ratchet(direction, level, extreme, atr_val, genome):
    """Chandelier update at a completed bar; fixed ATR stops never move."""
    t, m = stop_cfg(genome)
    if t != "chandelier" or level is None or not np.isfinite(atr_val): return level
    cand = extreme - m * atr_val if direction > 0 else extreme + m * atr_val
    return max(level, cand) if direction > 0 else min(level, cand)


def atr_last(df, genome):
    t, _ = stop_cfg(genome)
    a = S.atr(df.High.values.astype(float), df.Low.values.astype(float), df.Close.values.astype(float), ATR_PERIOD.get(t, 20))
    return float(a[-1])
