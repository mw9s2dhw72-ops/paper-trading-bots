"""Compact trade and equity history for the data branch.

Trades are append-only JSON lines so git diffs stay small and floats are not
rewritten. Equity is one snapshot per engine run (every bot, the benchmark and
the fund, sharing a timestamp). Snapshots older than 2 days are kept hourly
out to 30 days, then one per UTC day. That is enough for the dashboard charts
and for live Sharpe, which resamples equity to a daily last print.
"""
import json, os, sqlite3, time

TRADE_CAP = 8000


def connect(path=":memory:"):
    con = sqlite3.connect(path, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS trades(ts REAL, bot TEXT, asset TEXT, side TEXT, units REAL, price REAL, notional REAL, fee REAL, target REAL, reason TEXT, fx REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS equity(ts REAL, bot TEXT, equity REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS eq_bot_ts ON equity(bot, ts)")
    con.execute("CREATE TABLE IF NOT EXISTS prices(ts REAL, asset TEXT, price REAL)")
    return con


def _trade_obj(row):
    ts, bot, asset, side, units, price, notional, fee, target, reason, fx = row
    return dict(ts=ts, bot=bot, asset=asset, side=side, units=units, price=price,
                notional=notional, fee=fee, target=target, reason=reason, fx=fx)


def load(con, trades_path, equity_path):
    """Load committed history into `con`. Returns the number of trade lines."""
    n = 0
    if trades_path and os.path.exists(trades_path):
        with open(trades_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                t = json.loads(line)
                con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (t["ts"], t["bot"], t["asset"], t["side"], t["units"], t["price"],
                             t["notional"], t["fee"], t["target"], t.get("reason") or "", t.get("fx")))
                n += 1
    if equity_path and os.path.exists(equity_path):
        data = json.load(open(equity_path))
        rows = []
        for snap in data.get("snapshots", []):
            for bot, eq in snap["equity"].items():
                rows.append((snap["ts"], bot, eq))
        if rows:
            con.executemany("INSERT INTO equity VALUES (?,?,?)", rows)
    con.commit()
    return n


def compact_snapshots(snaps, now):
    """Keep full resolution for 2 days, hourly to 30 days, then daily. Always keep the newest."""
    if not snaps:
        return []
    snaps = sorted(snaps, key=lambda s: s["ts"])
    full_cut = now - 2 * 86400
    hour_cut = now - 30 * 86400
    kept, hours, days = [], {}, {}
    for s in snaps:
        ts = s["ts"]
        if ts >= full_cut:
            kept.append(s)
        elif ts >= hour_cut:
            hours[int(ts // 3600)] = s
        else:
            days[int(ts // 86400)] = s
    merged = {}
    for s in list(days.values()) + list(hours.values()) + kept:
        merged[s["ts"]] = s
    # never drop the latest point, even if bucketing would
    merged[snaps[-1]["ts"]] = snaps[-1]
    return [merged[k] for k in sorted(merged)]


def save(con, trades_path, equity_path, n_trades_loaded, now=None):
    """Append trades inserted during this run and rewrite the compacted equity file."""
    now = now or time.time()
    os.makedirs(os.path.dirname(os.path.abspath(trades_path)), exist_ok=True)
    rows = con.execute(
        "SELECT ts,bot,asset,side,units,price,notional,fee,target,reason,fx FROM trades ORDER BY rowid"
    ).fetchall()
    new = rows[n_trades_loaded:]
    if len(rows) > TRADE_CAP:
        # rewrite, dropping the oldest, once the audit log outgrows the cap
        keep = rows[-TRADE_CAP:]
        with open(trades_path, "w") as f:
            for row in keep:
                f.write(json.dumps(_trade_obj(row), separators=(",", ":")) + "\n")
    elif new:
        with open(trades_path, "a") as f:
            for row in new:
                f.write(json.dumps(_trade_obj(row), separators=(",", ":")) + "\n")
    elif not os.path.exists(trades_path):
        open(trades_path, "w").close()

    eq_rows = con.execute("SELECT ts, bot, equity FROM equity ORDER BY ts").fetchall()
    grouped = {}
    for ts, bot, eq in eq_rows:
        grouped.setdefault(ts, {})[bot] = eq
    snaps = compact_snapshots([{"ts": ts, "equity": eqs} for ts, eqs in grouped.items()], now)
    tmp = equity_path + ".tmp"
    with open(tmp, "w") as f:
        f.write('{"snapshots":[\n')
        for i, s in enumerate(snaps):
            f.write(json.dumps(s, separators=(",", ":")))
            f.write(",\n" if i < len(snaps) - 1 else "\n")
        f.write("]}\n")
    os.replace(tmp, equity_path)
    return len(new), len(snaps)
