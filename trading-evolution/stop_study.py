"""Backtest each live bot with no stop vs a coarse grid of stop overlays.
Choose on TRAIN only, confirm on TEST.  Output: results_stops/stop_study.json + .csv"""
import json, os, sys
import numpy as np, pandas as pd
from engine import Market, metrics, TRAIN_START, TRAIN_END, TEST_START, TEST_END
import stops

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results_stops"); os.makedirs(OUT, exist_ok=True)
GRID = [("atr", 2.0), ("atr", 3.0), ("atr", 4.0), ("chandelier", 3.0), ("chandelier", 4.0), ("chandelier", 5.0)]
SHARPE_TOL, RET_TOL, MIN_IMPROVE = 0.05, 0.05, 0.005   # Sharpe drop <= 0.05, return <= 5% relative lower, risk better by >= 0.5pp


def run_port(market, strategy, genome):
    n = len(market.index); R = {}; TR = {}; NS = 0
    for t in market.assets:
        d = market.inputs[t]
        net, eff, stopped, lvl, tret, tend = stops.run(strategy, d, genome)
        full = np.zeros(n); full[d["i0"]:] = net; R[t] = full
        idx = market.index[d["i0"]:]
        TR[t] = pd.Series(tret[tend], index=idx[tend])
        R[t + "_stops"] = pd.Series(stopped, index=idx)
    Rdf = pd.DataFrame({t: R[t] for t in market.assets}, index=market.index)
    w = market.avail.astype(float)
    port = ((Rdf * w).sum(axis=1) / w.sum(axis=1).replace(0, np.nan)).fillna(0.0)
    return port, TR, {t: R[t + "_stops"] for t in market.assets}


def period_stats(port, TR, ST, a, b):
    m = metrics(port[a:b])
    trades = pd.concat([s[a:b] for s in TR.values()])
    m["worst_trade"] = float(trades.min()) if len(trades) else 0.0
    m["n_trades"] = int(len(trades))
    m["n_stopouts"] = int(sum(s[a:b].sum() for s in ST.values()))
    m["fitness"] = m["sharpe"] - m["max_dd"]
    return m


def passes(base, cand):
    ok_sharpe = cand["sharpe"] >= base["sharpe"] - SHARPE_TOL
    ok_ret = cand["total_return"] >= base["total_return"] - RET_TOL * abs(base["total_return"])
    better_dd = cand["max_dd"] <= base["max_dd"] - MIN_IMPROVE
    better_wt = cand["worst_trade"] >= base["worst_trade"] + MIN_IMPROVE
    return bool(ok_sharpe and ok_ret and (better_dd or better_wt)), dict(ok_sharpe=bool(ok_sharpe), ok_ret=bool(ok_ret),
                                                                          better_dd=bool(better_dd), better_worst_trade=bool(better_wt))


def main():
    market = Market()
    bots = json.load(open(os.path.join(HERE, "live", "bots.json")))
    rows = []
    for b in bots:
        g0 = {k: v for k, v in b["genome"].items() if k not in ("stop_type", "stop_mult")}
        res = {}
        for st_type, mult in [("none", 0.0)] + GRID:
            g = dict(g0, stop_type=st_type, stop_mult=mult)
            port, TR, ST = run_port(market, b["strategy"], g)
            res[(st_type, mult)] = dict(train=period_stats(port, TR, ST, TRAIN_START, TRAIN_END),
                                        test=period_stats(port, TR, ST, TEST_START, TEST_END))
        base = res[("none", 0.0)]
        elig = []
        for k in GRID:
            ok, why = passes(base["train"], res[k]["train"])
            if ok: elig.append(k)
        choice = max(elig, key=lambda k: (res[k]["train"]["fitness"], k[1])) if elig else None
        if choice is None:
            verdict, reason = "no stop", "no candidate passed on TRAIN (each one cut Sharpe/return or didn't reduce risk)"
            test_ok, test_why = None, None
        else:
            test_ok, test_why = passes(base["test"], res[choice]["test"])
            if test_ok:
                verdict, reason = "stop accepted", "passed on TRAIN and confirmed on TEST"
            else:
                fails = [k for k, v in test_why.items() if not v and k.startswith("ok_")]
                if not fails: fails = ["no risk improvement on test"]
                verdict, reason = "no stop", "chosen on TRAIN but failed on TEST: " + ", ".join(fails)
        # best-on-test info only (NOT used for the decision): did ANY candidate pass on test?
        any_test = [f"{k[0]} {k[1]:g}" for k in GRID if passes(base["test"], res[k]["test"])[0]]
        rows.append(dict(bot_id=b["bot_id"], strategy=b["strategy"], verdict=verdict, reason=reason,
                         choice=None if choice is None else dict(stop_type=choice[0], stop_mult=choice[1]),
                         train_eligible=[f"{k[0]} {k[1]:g}" for k in elig], test_check=test_why, any_pass_on_test_info_only=any_test,
                         base=base, chosen=None if choice is None else res[choice],
                         grid={f"{k[0]} {k[1]:g}": v for k, v in res.items()}))
        print(f"{b['bot_id']:9s} {b['strategy'][:26]:26s} {verdict:13s} {choice} | {reason}", flush=True)
    json.dump(dict(grid=GRID, rule=dict(sharpe_tol=SHARPE_TOL, ret_rel_tol=RET_TOL, min_improve=MIN_IMPROVE),
                   train=[TRAIN_START, TRAIN_END], test=[TEST_START, TEST_END], bots=rows), open(os.path.join(OUT, "stop_study.json"), "w"), indent=1)
    flat = []
    for r in rows:
        for per in ("train", "test"):
            for lab, m in (("no stop", r["base"][per]), ("with stop", (r["chosen"] or {}).get(per))):
                if m is None: continue
                flat.append(dict(bot=r["bot_id"], strategy=r["strategy"], verdict=r["verdict"],
                                 stop=None if not r["choice"] else f"{r['choice']['stop_type']} {r['choice']['stop_mult']:g}",
                                 period=per, version=lab, sharpe=m["sharpe"], total_return=m["total_return"], max_dd=m["max_dd"],
                                 worst_trade=m["worst_trade"], n_trades=m["n_trades"], n_stopouts=m["n_stopouts"]))
    pd.DataFrame(flat).to_csv(os.path.join(OUT, "stop_study.csv"), index=False)


if __name__ == "__main__":
    main()
