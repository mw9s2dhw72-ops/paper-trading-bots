"""One stateless paper-trading step.

Loads the saved state and trade/equity history, polls prices, applies any new
completed daily bars, fills pending paper orders, checks stops (including the
1-minute range since the previous run), runs evolution when 00:30 UTC is due,
and writes state, history and the static dashboard JSON.

No broker, no API keys, no real orders. Money is simulated GBP.
"""
import argparse, json, os, shutil, sys, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import history_store as HS
import paper_engine as PE
import evolve_live as EV
import publish_site as PUB

SEED = os.path.join(HERE, "migrated")


def seed_into(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    for name in ("state.json", "trades.jsonl", "equity.json"):
        src = os.path.join(SEED, name)
        if not os.path.exists(src):
            raise SystemExit(f"missing seed file {src}")
        shutil.copy(src, os.path.join(data_dir, name))
        print(f"seeded {name} from the 2026-11-04 snapshot")


def _pos(st):
    """cash and units per sleeve, so a run can show whether balances carried over."""
    out = {}
    for bid, b in st["bots"].items():
        out[bid] = {a: (s["cash"], s["units"]) for a, s in b["sleeves"].items()}
    return out


def main():
    ap = argparse.ArgumentParser(description="Run one simulated paper-trading step")
    ap.add_argument("--data-dir", required=True, help="directory with state.json, trades.jsonl, equity.json")
    ap.add_argument("--site", default=os.path.join(REPO, "site"), help="static site root to write data/*.json into")
    ap.add_argument("--reset", action="store_true", help="restore the migrated 2026-11-04 snapshot before the step")
    args = ap.parse_args()
    data = os.path.abspath(args.data_dir)
    os.makedirs(data, exist_ok=True)
    state_path = os.path.join(data, "state.json")
    trades_path = os.path.join(data, "trades.jsonl")
    equity_path = os.path.join(data, "equity.json")
    if args.reset or not os.path.exists(state_path):
        seed_into(data)

    st = json.load(open(state_path))
    n_bots = len(st["bots"]) - (1 if "BUY&HOLD" in st["bots"] else 0)
    trades_before_state = sum(b.get("n_trades", 0) for b in st["bots"].values())
    pos_before = _pos(st)
    fund_before = st.get("fund_equity")
    PE.STATE = state_path
    EV.init_evolution(st)
    con = HS.connect()
    n_loaded = HS.load(con, trades_path, equity_path)
    n_sql = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    PE.migrate_stops(st, con)
    print(f"loaded {n_bots} bots, state trades {trades_before_state}, history trades {n_sql}, fund {fund_before}")
    try:
        PE.run_cycle(st, con, {}, {"yahoo": 0, "hist": 0, "snap": 0, "fx": 0}, once=True)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
    new_trades, n_snaps = HS.save(con, trades_path, equity_path, n_loaded)
    summary = PUB.publish(st, con, os.path.abspath(args.site))
    trades_after = sum(b.get("n_trades", 0) for b in st["bots"].values())
    pos_after = _pos(st)
    moved = []
    for bid, sleeves in pos_before.items():
        if bid not in pos_after:
            moved.append(bid + " culled")
            continue
        for a, (cash, units) in sleeves.items():
            cash2, units2 = pos_after[bid][a]
            if abs(cash - cash2) > 1e-6 or abs(units - units2) > 1e-10:
                moved.append(f"{bid} {a}")
    for bid in pos_after:
        if bid not in pos_before:
            moved.append(bid + " born")
    srcs = {a: (st["prices"].get(a) or {}).get("src") for a in PE.ASSETS}
    print("--- paper step ---")
    print(f"trades {trades_before_state} -> {trades_after}  (appended {new_trades}, sql {n_sql} -> {con.execute('SELECT COUNT(*) FROM trades').fetchone()[0]})")
    print(f"fund £{fund_before} -> £{st.get('fund_equity')}")
    print(f"equity snapshots kept: {n_snaps}")
    print(f"US session open: {st['meta'].get('us_session_open')}")
    print(f"signal bars: {st.get('signal_bar')}")
    print(f"GBP/USD {st['fx']['rate']:.5f} ({st['fx']['src']})")
    for a, src in srcs.items():
        p = st["prices"].get(a) or {}
        print(f"  {a} {p.get('price')}  {src}")
    print(f"sleeves whose cash or units changed: {moved or 'none'}")
    print(f"dashboard bots: {summary['n_bots']}  trades shown: {summary['n_trades']}")
    if summary["n_bots"] != n_bots and not moved:
        raise SystemExit("bot count changed without a recorded birth or cull")


if __name__ == "__main__":
    main()
