"""Checks that don't need the saved portfolio: stop fills, history compaction, and that a crypto price source answers.

GitHub-hosted runners are in the US, where api.binance.com usually returns HTTP 451.
The price check fails the job if vision, Coinbase and Kraken are all unreachable.
"""
import os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import history_store as HS
import market_data as MD
import paper_engine as PE


def test_stop_fill():
    level = 100.0
    # wick through the level, open still above: fill at the stop
    fill = PE.stop_fill(1.0, level, live_price=105.0, bars=[(1, 102.0, 106.0, 99.0, 104.0)])
    assert abs(fill - 100.0) < 1e-9, fill
    # gap through on the open: fill at the open
    fill = PE.stop_fill(1.0, level, live_price=105.0, bars=[(1, 98.0, 99.0, 97.0, 98.5)])
    assert abs(fill - 98.0) < 1e-9, fill
    # no bar hit, live price through: fill at the live price (always-on behaviour)
    fill = PE.stop_fill(1.0, level, live_price=99.5, bars=[(1, 110.0, 112.0, 101.0, 111.0)])
    assert abs(fill - 99.5) < 1e-9, fill
    # nothing hit
    assert PE.stop_fill(1.0, level, live_price=105.0, bars=[(1, 110.0, 112.0, 101.0, 111.0)]) is None
    # short mirror: high trades through, open below the level
    fill = PE.stop_fill(-1.0, level, live_price=90.0, bars=[(1, 95.0, 101.0, 94.0, 96.0)])
    assert abs(fill - 100.0) < 1e-9, fill
    print("stop_fill ok")


def test_check_stops_uses_session_bars_after_close():
    def sleeve(px_stop=True):
        return {"units": 1.0, "cash": 0.0, "target": 1.0, "pending": None, "stop_lock": 0.0,
                "stop": {"dir": 1.0, "level": 100.0, "entry": 110.0}}
    def st():
        return {"bots": {"B": {"genome": {"stop_type": "atr", "stop_mult": 2.0},
                               "sleeves": {"SPY": sleeve()}, "fees": 0.0, "n_trades": 0, "n_stopouts": 0}}}
    now = time.time()
    pr = {"SPY": {"price": 105.0, "ts": now}}
    # market now closed, but a regular-session minute bar since the last run traded through 100
    s = st(); con = HS.connect()
    PE.check_stops(s, pr, 1.3, False, con, now, bars={"SPY": [(now - 120, 102.0, 106.0, 99.0, 104.0)]})
    assert s["bots"]["B"]["sleeves"]["SPY"]["units"] == 0.0
    price, reason = con.execute("SELECT price, reason FROM trades").fetchone()
    assert abs(price - 100.0) < 1e-9, price
    assert reason.startswith("stop-loss"), reason
    # same quote with no bars and the session closed: the always-on rule does not fire
    s = st(); con = HS.connect()
    PE.check_stops(s, {"SPY": {"price": 90.0, "ts": now}}, 1.3, False, con, now, bars=None)
    assert s["bots"]["B"]["sleeves"]["SPY"]["units"] == 1.0
    assert con.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    # fresh live price through the stop during the session, no bars: fill at the live price
    s = st(); con = HS.connect()
    PE.check_stops(s, {"SPY": {"price": 90.0, "ts": now}}, 1.3, True, con, now, bars=None)
    assert s["bots"]["B"]["sleeves"]["SPY"]["units"] == 0.0
    assert abs(con.execute("SELECT price FROM trades").fetchone()[0] - 90.0) < 1e-9
    # stale quote never fires on its own
    s = st(); con = HS.connect()
    PE.check_stops(s, {"SPY": {"price": 90.0, "ts": now - 3600}}, 1.3, True, con, now, bars=None)
    assert s["bots"]["B"]["sleeves"]["SPY"]["units"] == 1.0
    print("check_stops ok")


def test_compact():
    now = 1_700_000_000
    snaps = [
        {"ts": now - 40 * 86400, "equity": {"A": 1}},
        {"ts": now - 40 * 86400 + 100, "equity": {"A": 2}},
        {"ts": now - 10 * 86400, "equity": {"A": 3}},
        {"ts": now - 10 * 86400 + 10, "equity": {"A": 4}},
        {"ts": now - 3600, "equity": {"A": 5}},
    ]
    out = HS.compact_snapshots(snaps, now)
    assert [s["equity"]["A"] for s in out] == [2, 4, 5], out
    print("compact ok")


def test_crypto_source():
    px = MD.live_crypto_prices()
    for asset, lo in (("BTC", 1000), ("ETH", 50)):
        assert px[asset]["price"] > lo, px[asset]
        assert px[asset]["src"], px[asset]
    df = MD.crypto_daily("BTCUSDT", os.path.join(ROOT, "data"))
    assert len(df) > 1000, len(df)
    last = df.index[-1]
    assert last.day_of_week < 5, last
    print(f"crypto ok BTC {px['BTC']['price']:.2f} via {px['BTC']['src']}; ETH {px['ETH']['price']:.2f} via {px['ETH']['src']}; daily through {last.date()}")


if __name__ == "__main__":
    test_stop_fill()
    test_check_stops_uses_session_bars_after_close()
    test_compact()
    test_crypto_source()
    print("all scheduled checks passed")
