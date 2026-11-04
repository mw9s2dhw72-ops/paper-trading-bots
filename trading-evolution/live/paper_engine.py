"""Live PAPER-TRADING engine. No real money, no broker/exchange accounts, no API keys, no orders.

* Signals use COMPLETED daily bars only (same as the backtest).  Crypto bars complete at 00:00 UTC
  (weekend-dated bars are skipped, matching the backtest's business-day calendar); US stock/ETF bars
  complete at the 16:00 ET close.  A new signal is executed at the next live price at which the asset is
  tradable (crypto: immediately; stocks: during the regular 09:30-16:00 ET session).
* Equity is marked to market with live prices every POLL seconds in the always-on loop, or once per
  scheduled run (see run_once.py) when this file's run_cycle() is called with once=True.
* Each bot (and the buy-and-hold benchmark) starts with GBP 1,000 split equally into 7 asset sleeves.
  Accounting currency is GBP: idle cash is held in GBP; buying an asset converts GBP->USD at the live GBP/USD rate,
  so positions carry USD/GBP currency exposure (as they would for a UK investor). USDT is treated as USD.
  GBP/USD: Yahoo GBPUSD=X 1-min (primary), Yahoo chart API, open.er-api.com daily rate (fallback), last known rate otherwise.
* Costs: same per-unit-turnover bps as the backtest (fee + slippage).
* Stop-losses (since 2026-11-04): per-bot stop rule in the genome (stop_type none/atr/chandelier, stop_mult), chosen by
  ../stop_study.py on TRAIN and confirmed on TEST.  The stop level is set at each entry fill (fill price -/+ mult x ATR from the
  latest completed daily bar), ratcheted at each new completed bar for chandelier stops, and checked against the live price
  every poll (stocks only in the regular session).  A hit is filled at the live price, logged with reason 'stop-loss', and the
  sleeve stays flat until the strategy's raw signal stops pointing that way (no re-entry on the same signal).
  Scheduled runs also compare the stop with the high/low of 1-minute bars since the previous run, so a stop that
  prints and recovers between runs is not missed. That path fills at the stop, or at the bar's open if it gapped through.
* Crypto prices and klines come from data-api.binance.vision (same Binance spot market as the backtest). api.binance.com
  returns HTTP 451 from US hosts, so Coinbase and Kraken are fallbacks. See market_data.py.
State persists in state.json (atomic writes) + a SQLite connection the caller owns (trades, equity snapshots).
"""
import json, os, sys, time, sqlite3, traceback, datetime as dt
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import strategies as S
import stops as SL
import evolve_live as EV
import market_data as MD

STATE = os.path.join(HERE, "state.json"); DB = os.path.join(HERE, "paper.db")
START_BALANCE = 1_000.0   # GBP
CURRENCY = "GBP"
FX_POLL = 60
POLL = 20                # seconds between price polls
STOCK_POLL = 60          # seconds between Yahoo polls
HIST_REFRESH = 600       # seconds between daily-history refreshes
SNAP_EVERY = 60          # seconds between equity snapshots
ET = ZoneInfo("America/New_York")
ASSETS = {  # name: (source, symbol, cost bps)
    "SPY": ("yahoo", "SPY", 10), "QQQ": ("yahoo", "QQQ", 10), "AAPL": ("yahoo", "AAPL", 10),
    "GLD": ("yahoo", "GLD", 10), "FXE": ("yahoo", "FXE", 10),
    "BTC": ("binance", "BTCUSDT", 20), "ETH": ("binance", "ETHUSDT", 20),
}
YAHOO = [a for a, v in ASSETS.items() if v[0] == "yahoo"]
BIN = [a for a, v in ASSETS.items() if v[0] == "binance"]
BENCH_ID = "BUY&HOLD"


def log(*a):
    print(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ data
def binance_daily(sym):
    """Completed weekday daily bars. Vision (Binance spot) first; CSV/Coinbase if that host is blocked."""
    return MD.crypto_daily(sym, os.path.join(ROOT, "data"))


def yahoo_daily():
    return MD.yahoo_daily_history(os.path.join(ROOT, "data"))


def live_prices_binance():
    return MD.live_crypto_prices()


def live_prices_yahoo():
    prices, _bars = MD.yahoo_intraday(None)
    return prices


def live_fx():
    """GBP/USD (USD per 1 GBP)."""
    return MD.live_fx()


def us_session_open(prices):
    now = dt.datetime.now(ET)
    if now.weekday() >= 5: return False
    if not (dt.time(9, 30) <= now.time() < dt.time(16, 0)): return False
    # holiday guard: need a fresh regular-session print
    ts = [p["ts"] for a, p in prices.items() if a in YAHOO]
    return bool(ts) and time.time() - max(ts) < 15 * 60


# ------------------------------------------------------------------ signals
def target_position(strategy, df, genome, cost):
    """Run the backtest strategy on completed daily bars and return the latest target position."""
    cap = {}
    orig = S.pos_to_ret
    def grab(pos, c, cst):
        cap["pos"] = np.nan_to_num(pos)
        return orig(pos, c, cst)
    S.pos_to_ret = grab
    try:
        d = dict(o=df.Open.values.astype(float), h=df.High.values.astype(float), l=df.Low.values.astype(float),
                 c=df.Close.values.astype(float), v=df.Volume.values.astype(float), pc=df.Close.values.astype(float), cost=cost)
        if (d["v"] > 0).mean() < 0.5: d["v"] = np.ones_like(d["v"])
        S.STRATEGIES[strategy][0](d, genome)
    finally:
        S.pos_to_ret = orig
    return float(cap["pos"][-1])


# ------------------------------------------------------------------ persistence
def db():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS trades(ts REAL, bot TEXT, asset TEXT, side TEXT, units REAL, price REAL, notional REAL, fee REAL, target REAL, reason TEXT, fx REAL)")  # price USD; notional/fee GBP
    con.execute("CREATE TABLE IF NOT EXISTS equity(ts REAL, bot TEXT, equity REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS eq_bot_ts ON equity(bot, ts)")
    con.execute("CREATE TABLE IF NOT EXISTS prices(ts REAL, asset TEXT, price REAL)")
    return con


def save_state(st, path=None):
    path = path or STATE
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(st, f, default=float)
    os.replace(tmp, path)


def new_state(bots):
    sleeve = START_BALANCE / len(ASSETS)
    st = dict(started=time.time(), start_balance=START_BALANCE, currency=CURRENCY, bots={}, prices={}, signal_bar={}, meta={}, fx=None)
    for b in bots + [dict(bot_id=BENCH_ID, strategy="Buy & Hold (equal-weight 7 assets)", genome={}, backtest={})]:
        st["bots"][b["bot_id"]] = dict(strategy=b["strategy"], genome=b["genome"], backtest=b.get("backtest", {}),
                                       sleeves={a: dict(cash=sleeve, units=0.0, target=0.0, pending=None, last_bar=None, stop=None, stop_lock=0.0) for a in ASSETS},
                                       fees=0.0, n_trades=0, born_ts=st["started"], generation=0, parent=None, mate=None,
                                       genome_version=1, start_capital=START_BALANCE,
                                       lineage=[dict(ts=st["started"], event="seeded from backtest", src=b.get("src"), orig_id=b.get("orig_id"))])
    return st


def new_sleeves():
    sleeve = START_BALANCE / len(ASSETS)
    return {a: dict(cash=sleeve, units=0.0, target=0.0, pending=None, last_bar=None, stop=None, stop_lock=0.0) for a in ASSETS}


def liquidate(st, bid, prices, fx, con, now, dry_run=False):
    """Close every position of a culled bot at the live price (paper fill, fees charged). Returns final GBP equity."""
    b = st["bots"][bid]
    for a, s in b["sleeves"].items():
        if abs(s["units"]) < 1e-15 or a not in prices: continue
        pg = prices[a]["price"] / fx; cost = ASSETS[a][2] / 1e4
        notional = abs(s["units"] * pg); fee = notional * cost
        s["cash"] += s["units"] * pg - fee; b["fees"] += fee; b["n_trades"] += 1
        if not dry_run:
            con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (now, bid, a, "SELL" if s["units"] > 0 else "BUY", abs(s["units"]), prices[a]["price"], notional, fee, 0.0,
                         "culled by evolution: liquidated", fx))
        s["units"] = 0.0
    b["equity"] = sum(s["cash"] for s in b["sleeves"].values())
    return b["equity"]


def fund_equity(st):
    """Average-bot 'fund' value: GBP 1,000 + (P&L of living bots + locked P&L of culled bots) / population.
    Continuous across evolution steps (a child's fresh GBP 1,000 doesn't create or destroy P&L)."""
    living = [b for bid, b in st["bots"].items() if bid != BENCH_ID]
    pnl = sum(b.get("equity", b.get("start_capital", START_BALANCE)) - b.get("start_capital", START_BALANCE) for b in living)
    pnl += sum(g["final_equity"] - g["start_capital"] for g in st.get("evolution", {}).get("graveyard", []))
    return START_BALANCE + pnl / max(1, len(living))


def equity(bot, prices, fx):
    """Bot equity in GBP: GBP cash + USD market value / GBPUSD."""
    e = 0.0
    for a, s in bot["sleeves"].items():
        p = prices.get(a, {}).get("price")
        e += s["cash"] + (s["units"] * p / fx if p else 0.0)
    return e


# ------------------------------------------------------------------ stop-loss overlay (live)
STOP_FILE = os.path.join(ROOT, "results_stops", "accepted_stops.json")

def migrate_stops(st, con):
    """Idempotent: give every genome stop_type/stop_mult (from the stop study for seeded bots, inherited/none otherwise)
    and every sleeve stop state. Never touches cash, units, trades or equity."""
    acc = json.load(open(STOP_FILE)) if os.path.exists(STOP_FILE) else {}
    for bid, b in st["bots"].items():
        if bid == BENCH_ID: continue
        g = b["genome"]
        if "stop_type" not in g:
            a = acc.get(bid)
            g["stop_type"] = a["stop_type"] if a else "none"; g["stop_mult"] = float(a["stop_mult"]) if a else 0.0
            if a: b["stop_study"] = {k: a[k] for k in ("verdict", "reason", "candidate", "no_stop", "with_candidate")}
            b.setdefault("lineage", []).append(dict(ts=time.time(), event="stop-loss rule set", stop=SL.describe(g)))
        b.setdefault("n_stopouts", 0)
        for a_, s in b["sleeves"].items():
            s.setdefault("stop", None); s.setdefault("stop_lock", 0.0)
            if abs(s["units"]) > 1e-15 and s["stop"] is None and SL.stop_cfg(g)[0] != "none":
                row = con.execute("SELECT price, ts FROM trades WHERE bot=? AND asset=? ORDER BY ts DESC LIMIT 1", (bid, a_)).fetchone()
                if row: s["stop"] = dict(dir=1.0 if s["units"] > 0 else -1.0, entry=row[0], entry_ts=row[1], extreme=row[0],
                                         level=None, bar=s.get("last_bar"), migrated=True)   # level set once history is loaded


def set_stop_on_fill(b, s, a, fill_px, hist, prev_units):
    """Called after a paper fill: new entry or flip -> fresh stop; flat -> clear; same direction resize -> keep."""
    d_new = float(np.sign(s["units"])) if abs(s["units"]) > 1e-15 else 0.0
    d_old = float(np.sign(prev_units)) if abs(prev_units) > 1e-15 else 0.0
    if d_new == 0.0: s["stop"] = None; return
    if d_new == d_old and s.get("stop"): return
    g = b["genome"]
    if SL.stop_cfg(g)[0] == "none" or a not in hist: s["stop"] = None; return
    atr_v = SL.atr_last(hist[a], g)
    s["stop"] = dict(dir=d_new, entry=fill_px, entry_ts=time.time(), extreme=fill_px, atr=atr_v,
                     level=SL.initial_level(d_new, fill_px, atr_v, g), bar=s.get("last_bar"))


def update_stop_on_bar(b, s, a, df):
    st_ = s.get("stop")
    if not st_ or abs(s["units"]) < 1e-15: return
    g = b["genome"]; bar = str(df.index[-1].date())
    atr_v = SL.atr_last(df, g)
    if st_.get("level") is None:              # migrated open position: initialise from the latest completed bar
        st_["atr"] = atr_v; st_["level"] = SL.initial_level(st_["dir"], st_["entry"], atr_v, g); st_["bar"] = bar; return
    if bar == st_.get("bar"): return
    hi, lo = float(df.High.iloc[-1]), float(df.Low.iloc[-1])
    st_["extreme"] = max(st_["extreme"], hi) if st_["dir"] > 0 else min(st_["extreme"], lo)
    st_["atr"] = atr_v; st_["level"] = SL.ratchet(st_["dir"], st_["level"], st_["extreme"], atr_v, g); st_["bar"] = bar


def stop_fill(direction, level, live_price, bars):
    """Price a stop would have filled at.

    `bars` is [(ts, open, high, low, close), ...] since the previous run, oldest first.
    The first 1-minute bar that trades through the level fills at the level, or at that
    bar's open when the bar gapped through (same rule as the backtest overlay). If no
    bar hit it but the live price is through the level, fill at the live price — that
    is what the always-on loop does on the poll that first sees the cross.
    """
    for _ts, o, h, l, _c in bars or []:
        if direction > 0 and l <= level:
            return float(o if o <= level else level)
        if direction < 0 and h >= level:
            return float(o if o >= level else level)
    if live_price is None:
        return None
    if direction > 0 and live_price <= level:
        return float(live_price)
    if direction < 0 and live_price >= level:
        return float(live_price)
    return None


def check_stops(st, pr, fx, session, con, t0, bars=None):
    """`bars` is None in the always-on loop (live price only, identical to the original check).

    A scheduled run passes 1-minute bars since the previous poll. Stock bars are regular-session
    only, so a run that lands just after the close can still fill a stop that printed before 16:00 ET.
    A live quote older than 15 minutes never triggers a stop by itself.
    """
    for bid, b in st["bots"].items():
        if bid == BENCH_ID: continue
        for a, s in b["sleeves"].items():
            sp = s.get("stop")
            if not sp or sp.get("level") is None or abs(s["units"]) < 1e-15 or a not in pr: continue
            asset_bars = None if bars is None else (bars.get(a) or [])
            if bars is None:
                if a in YAHOO and not session: continue
                if t0 - pr[a].get("ts", 0) > 15 * 60: continue          # never trigger on a stale price
                p = pr[a]["price"]
                if not ((sp["dir"] > 0 and p <= sp["level"]) or (sp["dir"] < 0 and p >= sp["level"])): continue
                fill = p
                via = ""
            else:
                live = None
                if not (a in YAHOO and not session) and t0 - pr[a].get("ts", 0) <= 15 * 60:
                    live = pr[a]["price"]
                if a in YAHOO and not session and not asset_bars: continue
                if live is None and not asset_bars: continue
                fill = stop_fill(sp["dir"], sp["level"], live, asset_bars)
                if fill is None: continue
                via = "" if (live is not None and fill == live) else " via 1m range since last run"
                p = fill
            pg = p / fx; cost = ASSETS[a][2] / 1e4
            notional = abs(s["units"] * pg); fee = notional * cost
            side = "SELL" if s["units"] > 0 else "BUY"
            con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (t0, bid, a, side, abs(s["units"]), p, notional, fee, 0.0,
                         f"stop-loss: {SL.stop_cfg(b['genome'])[0]} stop {sp['level']:.4g} hit (entry {sp['entry']:.4g}){via}", fx))
            s["cash"] += s["units"] * pg - fee; s["units"] = 0.0
            b["fees"] += fee; b["n_trades"] += 1; b["n_stopouts"] = b.get("n_stopouts", 0) + 1
            s["stop_lock"] = sp["dir"]; s["target"] = 0.0
            if s["pending"] is not None and np.sign(s["pending"]) == sp["dir"]: s["pending"] = None
            s["last_stop"] = dict(ts=t0, price=p, level=sp["level"], entry=sp["entry"], dir=sp["dir"])
            s["stop"] = None
            log(f"STOP-LOSS {bid} {a} {side} @ {p:.4f} (level {sp['level']:.4f}, entry {sp['entry']:.4f}){via}")


# ------------------------------------------------------------------ one pass (always-on loop and the scheduled runner)
def _fill_pending(st, pr, fx, session, con, t0, hist):
    """Paper fills at the live price. Identical to the always-on loop body."""
    for bid, b in st["bots"].items():
        for a, s in b["sleeves"].items():
            if s["pending"] is None or a not in pr: continue
            if a in YAHOO and not session: continue
            if bid == BENCH_ID and s["units"] > 0: s["pending"] = None; continue
            p = pr[a]["price"]; cost = ASSETS[a][2] / 1e4
            pg = p / fx                                   # asset price in GBP right now
            sleeve_eq = s["cash"] + s["units"] * pg       # GBP
            want = s["pending"] * sleeve_eq / pg          # fractional units allowed
            du = want - s["units"]
            if abs(du * pg) > 0.05:                       # ignore trades under 5p
                fee = abs(du * pg) * cost                 # % fee + slippage, in GBP
                prev_units = s["units"]
                s["cash"] -= du * pg + fee; s["units"] = want
                if bid != BENCH_ID: set_stop_on_fill(b, s, a, p, hist, prev_units)
                b["fees"] += fee; b["n_trades"] += 1
                con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (t0, bid, a, "BUY" if du > 0 else "SELL", abs(du), p, abs(du * pg), fee, s["pending"],
                             f"signal on bar {s['last_bar']}", fx))
            s["target"] = s["pending"]; s["pending"] = None


def _apply_signals(st, hist):
    """Refresh targets when a new completed daily bar is present. Same rules as the original loop."""
    for a, df in hist.items():
        if df is None or len(df) == 0: continue
        bar = str(pd.Timestamp(df.index[-1]).date())
        for bid, b in st["bots"].items():               # migrated open positions get their stop level
            s = b["sleeves"][a]
            if bid != BENCH_ID and s.get("stop") and s["stop"].get("level") is None: update_stop_on_bar(b, s, a, df)
        if st["signal_bar"].get(a) == bar: continue
        cost = ASSETS[a][2] / 1e4
        for bid, b in st["bots"].items():
            tgt = 1.0 if bid == BENCH_ID else target_position(b["strategy"], df, b["genome"], cost)
            s = b["sleeves"][a]
            if s.get("stop_lock"):                    # stopped out: wait for a fresh signal
                if np.sign(tgt) == s["stop_lock"]: tgt = 0.0
                else: s["stop_lock"] = 0.0
            if bid != BENCH_ID: update_stop_on_bar(b, s, a, df)
            if s["last_bar"] is None or abs(tgt - s["target"]) > 0.02:
                s["pending"] = tgt
            s["last_bar"] = bar
        st["signal_bar"][a] = bar
        log(f"new completed bar {a} {bar}: signals refreshed")


def run_cycle(st, con, hist, timers, once=False):
    """One price / signal / stop / fill / equity pass.

    once=True is a stateless scheduled run: refresh every feed, and check stops against
    1-minute bars since the previous run as well as the latest price. The always-on loop
    passes once=False and keeps the original 20s / 60s throttles and live-price stops.
    `timers` keys: yahoo, hist, snap, fx (unix seconds, mutated).
    """
    t0 = time.time()
    since = (st.get("meta") or {}).get("last_poll")
    pr = st["prices"]
    minute_bars = {} if once else None
    try:
        pr.update(live_prices_binance())
    except Exception as e:
        log("crypto price error", e)
    if once or t0 - timers.get("yahoo", 0) > STOCK_POLL:
        try:
            got, ybars = MD.yahoo_intraday(since if once else None)
            pr.update(got)
            if once: minute_bars.update(ybars)
            timers["yahoo"] = t0
        except Exception as e:
            log("yahoo price error", e)
    if once and since:
        try:
            minute_bars.update(MD.crypto_minute_bars(since))
        except Exception as e:
            log("crypto 1m range error", e)
    if once or t0 - timers.get("fx", 0) > FX_POLL or not st.get("fx"):
        try:
            st["fx"] = live_fx(); timers["fx"] = t0
        except Exception as e:
            log("fx error (keeping last rate)", e)
    if not st.get("fx"):
        raise RuntimeError("no GBP/USD rate yet")
    fx = st["fx"]["rate"]
    session = us_session_open(pr)
    st.setdefault("meta", {}).update(us_session_open=session, last_poll=t0)
    # ---- daily history + new signals
    if once or t0 - timers.get("hist", 0) > HIST_REFRESH or not hist:
        try:
            for a in BIN: hist[a] = binance_daily(ASSETS[a][1])
            hist.update(yahoo_daily()); timers["hist"] = t0
        except Exception as e:
            log("history error", e)
        _apply_signals(st, hist)
    # ---- stop-loss checks (before new fills)
    check_stops(st, pr, fx, session, con, t0, bars=minute_bars)
    _fill_pending(st, pr, fx, session, con, t0, hist)
    # ---- equity snapshots
    for b in st["bots"].values(): b["equity"] = equity(b, pr, fx)
    st["fund_equity"] = fund_equity(st)
    # ---- evolution at 00:30 UTC; a delayed run still fires once next_ts is in the past
    ev = st["evolution"]
    if t0 >= ev["next_ts"] and len(hist) == len(ASSETS):
        try:
            costs = {a: ASSETS[a][2] / 1e4 for a in ASSETS}
            event, _ = EV.evolve(st, hist, costs, con, pr, fx, liquidate, new_sleeves, now=t0)
            log(f"evolution step: gen {ev['generation']} killed {[k['id'] for k in event['killed']]} "
                f"born {[b['id'] for b in event['born']]} retrained {[r['id'] for r in event['retrained']]} {event['note'] or ''}")
            if event["changed"]:
                st["signal_bar"] = {}; timers["hist"] = 0   # children / retrained bots need fresh targets
                if once and hist:
                    # the always-on loop fills these on its next 20s poll; a one-shot run has no next poll
                    _apply_signals(st, hist)
                    _fill_pending(st, pr, fx, session, con, t0, hist)
            for b in st["bots"].values(): b["equity"] = equity(b, pr, fx)
            st["fund_equity"] = fund_equity(st)
        except Exception:
            log("evolution error\n" + traceback.format_exc())
            ev["next_ts"] = EV.next_evolution_ts(t0)
    if once or t0 - timers.get("snap", 0) > SNAP_EVERY:
        con.executemany("INSERT INTO equity VALUES (?,?,?)", [(t0, bid, b["equity"]) for bid, b in st["bots"].items()]
                        + [(t0, "FUND", st["fund_equity"])])
        con.executemany("INSERT INTO prices VALUES (?,?,?)", [(t0, a, v["price"]) for a, v in pr.items()])
        timers["snap"] = t0
    con.commit()
    save_state(st)
    return hist


def main():
    bots = json.load(open(os.path.join(HERE, "bots.json")))
    st = json.load(open(STATE)) if os.path.exists(STATE) else new_state(bots)
    EV.init_evolution(st)
    con = db()
    migrate_stops(st, con); save_state(st)
    log(f"engine start: {len(st['bots'])-1} bots + benchmark, state {'resumed' if os.path.exists(STATE) else 'new'}")
    hist, timers = {}, {"yahoo": 0, "hist": 0, "snap": 0, "fx": 0}
    while True:
        t0 = time.time()
        try:
            hist = run_cycle(st, con, hist, timers, once=False)
        except Exception:
            log("loop error\n" + traceback.format_exc())
        time.sleep(max(1, POLL - (time.time() - t0)))


if __name__ == "__main__":
    main()
