"""Mobile-first dashboard for the paper-trading engine. Everything is served under a secret path token."""
import json, os, sqlite3, sys, time
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = open(os.path.join(HERE, "token.txt")).read().strip()
STATE = os.path.join(HERE, "state.json"); DB = os.path.join(HERE, "paper.db")
BENCH = "BUY&HOLD"
sys.path.insert(0, os.path.dirname(HERE))
import stops as SL
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def state():
    return json.load(open(STATE))

def con():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10); return c

def downsample(rows, n=240):
    if len(rows) <= n: return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    out[-1] = rows[-1]; return out

def check(tok):
    if tok != TOKEN: raise HTTPException(404)


@app.get("/{tok}/api/summary")
def summary(tok: str):
    check(tok); st = state(); now = time.time()
    sb = st["start_balance"]
    bots = []
    for bid, b in st["bots"].items():
        if bid == BENCH: continue
        pos = [a for a, s in b["sleeves"].items() if abs(s["units"]) > 1e-12]
        cap = b.get("start_capital", sb); eq = b.get("equity", cap)
        bots.append(dict(id=bid, strategy=b["strategy"], equity=eq, pnl=eq - cap, start_capital=cap,
                         pnl_pct=(eq / cap - 1) * 100, positions=pos, n_trades=b["n_trades"], fees=b["fees"],
                         born_ts=b.get("born_ts"), generation=b.get("generation", 0), parent=b.get("parent"),
                         age_days=(now - b.get("born_ts", st["started"])) / 86400,
                         bt_test_return=b["backtest"].get("test_return"), bt_test_sharpe=b["backtest"].get("test_sharpe")))
    bots.sort(key=lambda x: -x["pnl_pct"])   # fair: % return since birth
    bench = st["bots"][BENCH]
    c = con()
    ids = [b["id"] for b in bots]
    rows = c.execute("SELECT ts, MAX(CASE WHEN bot='FUND' THEN equity END), MAX(CASE WHEN bot=? THEN equity END) FROM equity WHERE bot IN ('FUND', ?) GROUP BY ts ORDER BY ts", (BENCH, BENCH)).fetchall()
    trades = c.execute("SELECT ts,bot,asset,side,units,price,notional,fee,target,reason FROM trades ORDER BY ts DESC LIMIT 60").fetchall()
    ntr = c.execute("SELECT COUNT(*) FROM trades WHERE bot!=?", (BENCH,)).fetchone()[0]
    ev = st.get("evolution", {})
    c.close()
    prices = {a: dict(p, age=now - p["ts"]) for a, p in st["prices"].items()}
    avg_eq = st.get("fund_equity", sum(b["equity"] for b in bots) / len(bots))
    return JSONResponse(dict(
        now=now, started=st["started"], last_poll=st["meta"].get("last_poll"), us_open=st["meta"].get("us_session_open"),
        signal_bar=st["signal_bar"], prices=prices, bots=bots, n_bots=len(bots), start_balance=sb,
        currency=st.get("currency", "USD"), fx=st.get("fx"),
        evolution=dict(generation=ev.get("generation", 0), next_ts=ev.get("next_ts"), last_ts=ev.get("last_ts"), cfg=ev.get("cfg"),
                       log=[{k: v for k, v in e.items() if k != "scores"} for e in ev.get("log", [])][-30:][::-1],
                       mix_history=ev.get("mix_history", []), n_dead=len(ev.get("graveyard", [])),
                       graveyard=ev.get("graveyard", [])[-30:][::-1]),
        combined=dict(avg_equity=avg_eq, total_equity=avg_eq * len(bots), total_start=sb * len(bots),
                      bench_equity=bench.get("equity", sb)),
        history=downsample([[r[0], r[1], r[2]] for r in rows]),
        trades=[dict(ts=t[0], bot=t[1], asset=t[2], side=t[3], units=t[4], price=t[5], notional=t[6], fee=t[7], target=t[8], reason=t[9]) for t in trades],
        n_trades=ntr))


@app.get("/{tok}/api/bot/{bid}")
def bot(tok: str, bid: str):
    check(tok); st = state()
    if bid not in st["bots"]: raise HTTPException(404)
    b = st["bots"][bid]; pr = st["prices"]
    sleeves = []
    for a, s in b["sleeves"].items():
        p = pr.get(a, {}).get("price")
        fx = (st.get("fx") or {}).get("rate", 1.0)
        val = s["units"] * p / fx if p else 0.0
        sp = s.get("stop") or {}
        lvl = sp.get("level") if abs(s["units"]) > 1e-15 else None
        sleeves.append(dict(asset=a, units=s["units"], price=p, value=val, cash=s["cash"],
                            equity=s["cash"] + val, target=s["target"], pending=s["pending"], bar=s["last_bar"],
                            stop_level=lvl, stop_entry=sp.get("entry") if lvl else None,
                            stop_dist_pct=((p / lvl - 1) * 100 * (1 if sp.get("dir", 1) > 0 else -1)) if (lvl and p) else None,
                            stop_lock=s.get("stop_lock", 0.0), last_stop=s.get("last_stop")))
    c = con()
    eq = c.execute("SELECT ts, equity FROM equity WHERE bot=? AND ts>=? ORDER BY ts", (bid, b.get("born_ts", 0) - 1)).fetchall()
    tr = c.execute("SELECT ts,asset,side,units,price,notional,fee,target,reason FROM trades WHERE bot=? ORDER BY ts DESC LIMIT 50", (bid,)).fetchall()
    c.close()
    ancestors, cur = [], b.get("parent")
    dead = {g["id"]: g for g in st.get("evolution", {}).get("graveyard", [])}
    while cur and len(ancestors) < 20:
        src = st["bots"].get(cur) or dead.get(cur)
        if not src: ancestors.append(dict(id=cur)); break
        ancestors.append(dict(id=cur, generation=src.get("generation", 0), alive=cur in st["bots"], strategy=src["strategy"]))
        cur = src.get("parent")
    children = [i for i, x in st["bots"].items() if x.get("parent") == bid] + [g["id"] for g in dead.values() if g.get("parent") == bid]
    return JSONResponse(dict(id=bid, strategy=b["strategy"], genome=b["genome"], backtest=b["backtest"], equity=b.get("equity"),
                             start_capital=b.get("start_capital", st["start_balance"]), born_ts=b.get("born_ts"), generation=b.get("generation", 0),
                             parent=b.get("parent"), mate=b.get("mate"), genome_version=b.get("genome_version", 1),
                             lineage=b.get("lineage", []), ancestors=ancestors, children=children, fx=st.get("fx"),
                             fees=b["fees"], n_trades=b["n_trades"], sleeves=sleeves, history=downsample([list(r) for r in eq]),
                             trades=[dict(ts=t[0], asset=t[1], side=t[2], units=t[3], price=t[4], notional=t[5], fee=t[6], target=t[7], reason=t[8]) for t in tr],
                             stop=dict(type=SL.stop_cfg(b["genome"])[0], mult=SL.stop_cfg(b["genome"])[1], rule=SL.describe(b["genome"]),
                                       n_stopouts=b.get("n_stopouts", 0), study=b.get("stop_study"))))


@app.get("/{tok}/manifest.webmanifest")
def manifest(tok: str):
    check(tok)
    return JSONResponse(dict(name="Paper Bots", short_name="Paper Bots", start_url=f"/{TOKEN}/", scope=f"/{TOKEN}/",
                             display="standalone", background_color="#0e1117", theme_color="#0e1117",
                             icons=[dict(src=f"/{TOKEN}/icon-192.png", sizes="192x192", type="image/png"),
                                    dict(src=f"/{TOKEN}/icon-512.png", sizes="512x512", type="image/png")]),
                        media_type="application/manifest+json")


@app.get("/{tok}/icon-{n}.png")
def icon(tok: str, n: int):
    check(tok)
    if n not in (180, 192, 512): raise HTTPException(404)
    return FileResponse(os.path.join(HERE, "static", f"static_icon_{n}.png"), media_type="image/png")


@app.get("/{tok}/sw.js")
def sw(tok: str):
    check(tok)
    return Response(open(os.path.join(HERE, "static", "sw.js")).read(), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/{tok}/")
def index(tok: str):
    check(tok)
    html = open(os.path.join(HERE, "index.html")).read().replace("__TOKEN__", TOKEN)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/{tok}")
def index2(tok: str):
    check(tok)
    return Response(status_code=307, headers={"Location": f"/{TOKEN}/"})
