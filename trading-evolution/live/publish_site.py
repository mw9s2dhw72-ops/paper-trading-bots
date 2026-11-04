"""Write the static JSON the GitHub Pages app reads.

The shape matches the FastAPI handlers in dashboard.py so the existing page
can keep its markup and only change the fetch URLs.
"""
import json, os, time
import stops as SL

BENCH = "BUY&HOLD"


def _dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"), default=float)
    os.replace(tmp, path)


def downsample(rows, n=240):
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    out[-1] = rows[-1]
    return out


def _summary(st, con):
    now = time.time()
    sb = st["start_balance"]
    bots = []
    for bid, b in st["bots"].items():
        if bid == BENCH:
            continue
        pos = [a for a, s in b["sleeves"].items() if abs(s["units"]) > 1e-12]
        cap = b.get("start_capital", sb)
        eq = b.get("equity", cap)
        bots.append(dict(id=bid, strategy=b["strategy"], equity=eq, pnl=eq - cap, start_capital=cap,
                         pnl_pct=(eq / cap - 1) * 100, positions=pos, n_trades=b["n_trades"], fees=b["fees"],
                         born_ts=b.get("born_ts"), generation=b.get("generation", 0), parent=b.get("parent"),
                         age_days=(now - b.get("born_ts", st["started"])) / 86400,
                         bt_test_return=b["backtest"].get("test_return"), bt_test_sharpe=b["backtest"].get("test_sharpe")))
    bots.sort(key=lambda x: -x["pnl_pct"])
    bench = st["bots"][BENCH]
    rows = con.execute(
        "SELECT ts, MAX(CASE WHEN bot='FUND' THEN equity END), MAX(CASE WHEN bot=? THEN equity END) "
        "FROM equity WHERE bot IN ('FUND', ?) GROUP BY ts ORDER BY ts",
        (BENCH, BENCH)).fetchall()
    trades = con.execute(
        "SELECT ts,bot,asset,side,units,price,notional,fee,target,reason FROM trades ORDER BY ts DESC LIMIT 60"
    ).fetchall()
    ntr = con.execute("SELECT COUNT(*) FROM trades WHERE bot!=?", (BENCH,)).fetchone()[0]
    ev = st.get("evolution", {})
    prices = {a: dict(p, age=now - p["ts"]) for a, p in st["prices"].items()}
    avg_eq = st.get("fund_equity", sum(b["equity"] for b in bots) / len(bots))
    return dict(
        now=now, started=st["started"], last_poll=st["meta"].get("last_poll"), us_open=st["meta"].get("us_session_open"),
        signal_bar=st["signal_bar"], prices=prices, bots=bots, n_bots=len(bots), start_balance=sb,
        currency=st.get("currency", "GBP"), fx=st.get("fx"),
        evolution=dict(generation=ev.get("generation", 0), next_ts=ev.get("next_ts"), last_ts=ev.get("last_ts"), cfg=ev.get("cfg"),
                       log=[{k: v for k, v in e.items() if k != "scores"} for e in ev.get("log", [])][-30:][::-1],
                       mix_history=ev.get("mix_history", []), n_dead=len(ev.get("graveyard", [])),
                       graveyard=ev.get("graveyard", [])[-30:][::-1]),
        combined=dict(avg_equity=avg_eq, total_equity=avg_eq * len(bots), total_start=sb * len(bots),
                      bench_equity=bench.get("equity", sb)),
        history=downsample([[r[0], r[1], r[2]] for r in rows]),
        trades=[dict(ts=t[0], bot=t[1], asset=t[2], side=t[3], units=t[4], price=t[5], notional=t[6], fee=t[7], target=t[8], reason=t[9]) for t in trades],
        n_trades=ntr)


def _bot(st, con, bid):
    b = st["bots"][bid]
    pr = st["prices"]
    fxr = (st.get("fx") or {}).get("rate", 1.0)
    sleeves = []
    for a, s in b["sleeves"].items():
        p = pr.get(a, {}).get("price")
        val = s["units"] * p / fxr if p else 0.0
        sp = s.get("stop") or {}
        lvl = sp.get("level") if abs(s["units"]) > 1e-15 else None
        sleeves.append(dict(asset=a, units=s["units"], price=p, value=val, cash=s["cash"],
                            equity=s["cash"] + val, target=s["target"], pending=s["pending"], bar=s["last_bar"],
                            stop_level=lvl, stop_entry=sp.get("entry") if lvl else None,
                            stop_dist_pct=((p / lvl - 1) * 100 * (1 if sp.get("dir", 1) > 0 else -1)) if (lvl and p) else None,
                            stop_lock=s.get("stop_lock", 0.0), last_stop=s.get("last_stop")))
    eq = con.execute("SELECT ts, equity FROM equity WHERE bot=? AND ts>=? ORDER BY ts",
                     (bid, b.get("born_ts", 0) - 1)).fetchall()
    tr = con.execute(
        "SELECT ts,asset,side,units,price,notional,fee,target,reason FROM trades WHERE bot=? ORDER BY ts DESC LIMIT 50",
        (bid,)).fetchall()
    ancestors, cur = [], b.get("parent")
    dead = {g["id"]: g for g in st.get("evolution", {}).get("graveyard", [])}
    while cur and len(ancestors) < 20:
        src = st["bots"].get(cur) or dead.get(cur)
        if not src:
            ancestors.append(dict(id=cur))
            break
        ancestors.append(dict(id=cur, generation=src.get("generation", 0), alive=cur in st["bots"], strategy=src["strategy"]))
        cur = src.get("parent")
    children = [i for i, x in st["bots"].items() if x.get("parent") == bid] + [g["id"] for g in dead.values() if g.get("parent") == bid]
    return dict(id=bid, strategy=b["strategy"], genome=b["genome"], backtest=b["backtest"], equity=b.get("equity"),
                start_capital=b.get("start_capital", st["start_balance"]), born_ts=b.get("born_ts"), generation=b.get("generation", 0),
                parent=b.get("parent"), mate=b.get("mate"), genome_version=b.get("genome_version", 1),
                lineage=b.get("lineage", []), ancestors=ancestors, children=children, fx=st.get("fx"),
                fees=b["fees"], n_trades=b["n_trades"], sleeves=sleeves, history=downsample([list(r) for r in eq]),
                trades=[dict(ts=t[0], asset=t[1], side=t[2], units=t[3], price=t[4], notional=t[5], fee=t[6], target=t[7], reason=t[8]) for t in tr],
                stop=dict(type=SL.stop_cfg(b["genome"])[0], mult=SL.stop_cfg(b["genome"])[1], rule=SL.describe(b["genome"]),
                          n_stopouts=b.get("n_stopouts", 0), study=b.get("stop_study")))


def publish(st, con, site_dir):
    data = os.path.join(site_dir, "data")
    bots_dir = os.path.join(data, "bots")
    os.makedirs(bots_dir, exist_ok=True)
    # drop bot files from a previous population so culled ids disappear
    keep = set()
    for bid in st["bots"]:
        if bid == BENCH:
            continue
        safe = bid.replace("/", "_")
        _dump(os.path.join(bots_dir, safe + ".json"), _bot(st, con, bid))
        keep.add(safe + ".json")
    for name in os.listdir(bots_dir):
        if name not in keep and name.endswith(".json"):
            os.remove(os.path.join(bots_dir, name))
    summary = _summary(st, con)
    _dump(os.path.join(data, "summary.json"), summary)
    return summary
