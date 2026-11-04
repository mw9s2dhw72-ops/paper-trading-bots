"""Live evolution for the paper-trading bots (runs inside paper_engine.py on a schedule).

Each evolution step (default daily at 00:30 UTC, just after the crypto daily bar closes):
  1. Score every living bot:  fitness = w * live_fitness + (1 - w) * recent_backtest_fitness
       recent_backtest_fitness = Sharpe - maxDD of the bot's genome over the most recent ROLLING_BARS daily bars
                                 (2 years, which includes every live day so far), same costs as the backtest.
       live_fitness            = Sharpe - maxDD of the bot's own daily paper equity since birth.
       w = 0 until the bot has MIN_LIVE_DAYS of live history, then ramps linearly to W_LIVE_MAX at W_LIVE_FULL_DAYS.
  2. Cull: the bottom CULL_FRAC of the population, counted only among bots with >= MIN_LIVE_DAYS live days
     (young bots cannot be killed on a tiny sample).  Killed bots are liquidated at the live price (fees charged)
     and moved to the graveyard with their final record.
  3. Rebirth: for each death, pick a parent from the top PARENT_FRAC (tournament of 3), optionally cross over with
     a same-strategy mate, then RE-OPTIMISE: try N_CANDIDATES mutations and keep the one with the best recent
     rolling-window backtest fitness.  The child starts with a fresh GBP 1,000 (see README for why).
  4. Re-training (every RETRAIN_EVERY_DAYS): every living bot tries N_RETRAIN mutations of its own genome on the
     recent window; if the best beats its current genome by RETRAIN_MARGIN, the bot adopts it (genome version +1,
     capital and record kept, change logged).
Population size stays constant.
"""
import json, math, time, datetime as dt, sqlite3
import numpy as np, pandas as pd

import strategies as S
import stops as SL

CFG = dict(
    EVOLVE_HOUR_UTC=0, EVOLVE_MINUTE_UTC=30,
    CULL_FRAC=0.20, PARENT_FRAC=0.20, CROSSOVER_P=0.3,
    MIN_LIVE_DAYS=3.0, W_LIVE_MAX=0.5, W_LIVE_FULL_DAYS=60.0,
    ROLLING_BARS=504, WARMUP_BARS=300,
    N_CANDIDATES=8, RETRAIN_EVERY_DAYS=7, N_RETRAIN=6, RETRAIN_MARGIN=0.15,
    MUT_RATE=0.4, MUT_SIGMA=0.12,
)
PREFIX = {"Supertrend": "ST", "Ichimoku Cloud": "ICH"}

def prefix(strategy):
    return PREFIX.get(strategy, "".join(w[0] for w in strategy.split() if w[0].isalpha()))


# ------------------------------------------------------------------ fitness helpers
def fit_from_returns(r):
    r = pd.Series(r).dropna()
    if len(r) < 2: return 0.0, {}
    eq = (1 + r).cumprod(); sd = r.std()
    sharpe = float(r.mean() / sd * math.sqrt(252)) if sd > 1e-12 else 0.0
    sharpe = max(-5.0, min(5.0, sharpe))
    dd = float(-(eq / eq.cummax() - 1).min())
    return sharpe - dd, dict(sharpe=sharpe, max_dd=dd, ret=float(eq.iloc[-1] - 1), days=len(r))

def recent_backtest(strategy, genome, hist, assets_cost):
    """Equal-weight portfolio of the genome over the most recent ROLLING_BARS daily bars of every asset."""
    n = CFG["ROLLING_BARS"] + CFG["WARMUP_BARS"]
    rets = {}
    for a, df in hist.items():
        df = df.iloc[-n:]
        d = dict(o=df.Open.values.astype(float), h=df.High.values.astype(float), l=df.Low.values.astype(float),
                 c=df.Close.values.astype(float), v=df.Volume.values.astype(float), pc=df.Close.values.astype(float),
                 cost=assets_cost[a])
        if (d["v"] > 0).mean() < 0.5: d["v"] = np.ones_like(d["v"])
        r = SL.run(strategy, d, genome)[0]          # includes the bot's stop-loss overlay (none = identical to plain strategy)
        rets[a] = pd.Series(np.nan_to_num(r), index=df.index).iloc[-CFG["ROLLING_BARS"]:]
    port = pd.DataFrame(rets).mean(axis=1)
    return fit_from_returns(port)

def live_daily_returns(con, bot_id, born_ts):
    rows = con.execute("SELECT ts, equity FROM equity WHERE bot=? AND ts>=? ORDER BY ts", (bot_id, born_ts)).fetchall()
    if len(rows) < 2: return pd.Series(dtype=float)
    s = pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows], unit="s"))
    daily = s.resample("1D").last().dropna()
    first = pd.Series([rows[0][1]], index=[daily.index[0] - pd.Timedelta(days=1)])
    return pd.concat([first, daily]).pct_change().dropna()


# ------------------------------------------------------------------ genetic operators (same as the backtest)
def mutate(strategy, g, rng):
    spec = S.STRATEGIES[strategy][1]; g = dict(g); changes = {}
    keys = list(spec)
    for k in keys:
        if rng.random() > CFG["MUT_RATE"]: continue
        lo, hi, kind = spec[k]; old = g[k]
        if kind == "f":
            g[k] = float(np.clip(g[k] + rng.normal(0, CFG["MUT_SIGMA"] * (hi - lo)), lo, hi))
        elif kind == "i":
            step = max(1, int(round(abs(rng.normal(0, CFG["MUT_SIGMA"] * (hi - lo))))))
            g[k] = int(np.clip(g[k] + step * rng.choice([-1, 1]), lo, hi))
        elif rng.random() < 0.25:
            g[k] = int(1 - g[k]) if hi == 1 else int(rng.integers(lo, hi + 1))
        if g[k] != old: changes[k] = [old, g[k]]
    tries = 0
    while not changes and tries < 20:            # guarantee at least one real parameter change
        tries += 1
        k = keys[int(rng.integers(len(keys)))]; lo, hi, kind = spec[k]; old = g[k]
        if kind == "f": g[k] = float(np.clip(g[k] + rng.normal(0, CFG["MUT_SIGMA"] * (hi - lo)), lo, hi))
        elif kind == "i": g[k] = int(np.clip(g[k] + rng.choice([-1, 1]), lo, hi))
        else: g[k] = int(1 - g[k]) if hi == 1 else int(rng.integers(lo, hi + 1))
        if g[k] != old: changes[k] = [old, g[k]]
    # stop-loss genes: only bots that already carry a stop (accepted by the train/test stop study, or inherited) can
    # mutate it, so evolution never bolts a stop onto a strategy where it was shown to counteract the strategy.
    st_type, st_mult = SL.stop_cfg(g)
    if st_type != "none":
        lo, hi = SL.MULT_RANGE
        if rng.random() < CFG["MUT_RATE"]:
            nm = float(np.clip(round(st_mult + rng.normal(0, CFG["MUT_SIGMA"] * (hi - lo)), 2), lo, hi))
            if nm != st_mult: changes["stop_mult"] = [st_mult, nm]; g["stop_mult"] = nm
        if rng.random() < 0.05:
            nt = "chandelier" if st_type == "atr" else "atr"
            changes["stop_type"] = [st_type, nt]; g["stop_type"] = nt
    return g, changes

def crossover(g1, g2, rng):
    child = {k: (g1[k] if rng.random() < 0.5 else g2.get(k, g1[k])) for k in g1}
    src = g1 if rng.random() < 0.5 else g2           # stop genes travel together (type + multiplier)
    child["stop_type"] = src.get("stop_type", "none"); child["stop_mult"] = src.get("stop_mult", 0.0)
    return child

def optimise(strategy, base, hist, costs, rng, n):
    """Try n mutations of `base`, return the best (genome, changes, fitness, metrics) on the recent window."""
    best = None
    for _ in range(n):
        g, ch = mutate(strategy, base, rng)
        f, m = recent_backtest(strategy, g, hist, costs)
        if best is None or f > best[2]: best = (g, ch, f, m)
    return best


# ------------------------------------------------------------------ schedule
def next_evolution_ts(after_ts):
    t = dt.datetime.fromtimestamp(after_ts, dt.timezone.utc)
    cand = t.replace(hour=CFG["EVOLVE_HOUR_UTC"], minute=CFG["EVOLVE_MINUTE_UTC"], second=0, microsecond=0)
    if cand <= t: cand += dt.timedelta(days=1)
    return cand.timestamp()

def init_evolution(st):
    if "evolution" not in st:
        st["evolution"] = dict(generation=0, last_ts=None, next_ts=next_evolution_ts(time.time()), last_retrain_ts=st["started"],
                               log=[], mix_history=[], graveyard=[], cfg=CFG, seq=0)
        st["evolution"]["mix_history"].append(dict(ts=time.time(), gen=0, mix=mix(st)))
    for bid, b in st["bots"].items():
        b.setdefault("born_ts", st["started"]); b.setdefault("generation", 0); b.setdefault("parent", None)
        b.setdefault("mate", None); b.setdefault("genome_version", 1); b.setdefault("lineage", [])
        b.setdefault("start_capital", st["start_balance"])

def mix(st):
    m = {}
    for bid, b in st["bots"].items():
        if bid == "BUY&HOLD": continue
        m[b["strategy"]] = m.get(b["strategy"], 0) + 1
    return m


# ------------------------------------------------------------------ the evolution step
def evolve(st, hist, costs, con, prices, fx, liquidate, new_sleeves, now=None, dry_run=False, ignore_min_days=False, seed=None):
    """Mutates `st` in place unless dry_run (then works on a copy). Returns the event dict."""
    import copy
    now = now or time.time()
    if dry_run: st = copy.deepcopy(st)
    ev = st["evolution"]; rng = np.random.default_rng(seed if seed is not None else int(now))
    gen = ev["generation"] + 1
    living = {bid: b for bid, b in st["bots"].items() if bid != "BUY&HOLD"}
    # 1. score
    scores = []
    for bid, b in living.items():
        bt_f, bt_m = recent_backtest(b["strategy"], b["genome"], hist, costs)
        days = (now - b["born_ts"]) / 86400
        lr = live_daily_returns(con, bid, b["born_ts"])
        live_f, live_m = fit_from_returns(lr) if len(lr) >= 2 else (0.0, {})
        w = 0.0 if days < CFG["MIN_LIVE_DAYS"] else min(CFG["W_LIVE_MAX"], CFG["W_LIVE_MAX"] * days / CFG["W_LIVE_FULL_DAYS"])
        pct = (b.get("equity", b["start_capital"]) / b["start_capital"] - 1) * 100
        scores.append(dict(id=bid, strategy=b["strategy"], fitness=w * live_f + (1 - w) * bt_f, bt_fitness=bt_f, live_fitness=live_f,
                           w_live=w, live_days=days, pct_since_birth=pct, eligible=ignore_min_days or days >= CFG["MIN_LIVE_DAYS"],
                           bt_sharpe=bt_m.get("sharpe"), bt_dd=bt_m.get("max_dd")))
    scores.sort(key=lambda x: -x["fitness"])
    n = len(scores); n_cull = int(round(CFG["CULL_FRAC"] * n))
    eligible = [s for s in scores if s["eligible"]]
    killed = sorted(eligible, key=lambda x: x["fitness"])[:min(n_cull, max(0, len(eligible) - 1))] if eligible else []
    killed_ids = {k["id"] for k in killed}
    parents = [s for s in scores if s["id"] not in killed_ids][:max(2, int(round(CFG["PARENT_FRAC"] * n)))]
    event = dict(gen=gen, ts=now, dry_run=dry_run, scores=scores, killed=[], born=[], retrained=[],
                 note=None if eligible else f"no bot has {CFG['MIN_LIVE_DAYS']:g}+ live days yet: nobody culled")
    # 2. cull
    for k in killed:
        b = st["bots"][k["id"]]
        final_eq = liquidate(st, k["id"], prices, fx, con, now, dry_run)
        st["evolution"]["graveyard"].append(dict(id=k["id"], strategy=b["strategy"], genome=b["genome"], born_ts=b["born_ts"], died_ts=now,
                                                 generation=b["generation"], parent=b["parent"], final_equity=final_eq,
                                                 start_capital=b["start_capital"], pct=(final_eq / b["start_capital"] - 1) * 100,
                                                 fitness=k["fitness"], died_in_gen=gen))
        event["killed"].append(dict(id=k["id"], strategy=b["strategy"], fitness=round(k["fitness"], 3),
                                    pct_since_birth=round((final_eq / b["start_capital"] - 1) * 100, 3), live_days=round(k["live_days"], 2)))
        del st["bots"][k["id"]]
    # 3. rebirth with re-optimisation on the recent window
    for i in range(len(killed)):
        cand = [parents[j] for j in rng.choice(len(parents), size=min(3, len(parents)), replace=False)]
        par = max(cand, key=lambda x: x["fitness"]); pb = st["bots"][par["id"]]
        base = dict(pb["genome"]); mate = None
        mates = [p for p in parents if p["strategy"] == par["strategy"] and p["id"] != par["id"]]
        if mates and rng.random() < CFG["CROSSOVER_P"]:
            mate = mates[int(rng.integers(len(mates)))]["id"]
            base = crossover(base, st["bots"][mate]["genome"], rng)
        g, ch, f, m = optimise(par["strategy"], base, hist, costs, rng, CFG["N_CANDIDATES"])
        ev["seq"] += 1
        cid = f"{prefix(par['strategy'])}-g{gen}.{ev['seq']}"
        st["bots"][cid] = dict(strategy=par["strategy"], genome=g, backtest=dict(recent_fitness=f, recent_sharpe=m.get("sharpe")),
                               sleeves=new_sleeves(), fees=0.0, n_trades=0, equity=st["start_balance"],
                               born_ts=now, generation=gen, parent=par["id"], mate=mate, genome_version=1,
                               start_capital=st["start_balance"],
                               lineage=[dict(ts=now, event="born", parent=par["id"], mate=mate, mutations=ch)])
        event["born"].append(dict(id=cid, parent=par["id"], mate=mate, strategy=par["strategy"], mutations=ch,
                                  recent_fitness=round(f, 3), parent_fitness=round(par["fitness"], 3)))
    # 4. periodic in-place re-training
    if (now - ev["last_retrain_ts"]) / 86400 >= CFG["RETRAIN_EVERY_DAYS"]:
        for s in scores:
            if s["id"] in killed_ids or s["id"] not in st["bots"]: continue
            b = st["bots"][s["id"]]
            g, ch, f, m = optimise(b["strategy"], b["genome"], hist, costs, rng, CFG["N_RETRAIN"])
            if f > s["bt_fitness"] + CFG["RETRAIN_MARGIN"]:
                b["genome"] = g; b["genome_version"] += 1
                b["lineage"].append(dict(ts=now, event=f"retrained v{b['genome_version']}", mutations=ch,
                                         old_fitness=round(s["bt_fitness"], 3), new_fitness=round(f, 3)))
                event["retrained"].append(dict(id=s["id"], mutations=ch, old_fitness=round(s["bt_fitness"], 3), new_fitness=round(f, 3)))
        ev["last_retrain_ts"] = now
    changed = bool(event["killed"] or event["retrained"])
    if not changed:                      # nothing happened: keep the generation number, log the check
        gen = ev["generation"]; event["gen"] = gen
    ev["generation"] = gen; ev["last_ts"] = now; ev["next_ts"] = next_evolution_ts(now)
    if changed: ev["mix_history"].append(dict(ts=now, gen=gen, mix=mix(st)))
    event["changed"] = changed
    slim = dict(event); slim["scores"] = [dict(id=s["id"], fitness=round(s["fitness"], 3), w_live=round(s["w_live"], 2),
                                              bt_fitness=round(s["bt_fitness"], 3)) for s in scores]
    ev["log"].append(slim); ev["log"] = ev["log"][-200:]
    return event, st
