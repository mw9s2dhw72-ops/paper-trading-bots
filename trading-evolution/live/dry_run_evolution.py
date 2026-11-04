"""Dry-run one live evolution step against the CURRENT live state without changing anything.
Usage: python dry_run_evolution.py [--ignore-min-days]   (writes logs/dry_run_<ts>.json)"""
import json, sys, time, sqlite3, os
import paper_engine as PE
import evolve_live as EV
ignore = "--ignore-min-days" in sys.argv
st = json.load(open(PE.STATE)); EV.init_evolution(st)
hist = {a: PE.binance_daily(PE.ASSETS[a][1]) for a in PE.BIN}; hist.update(PE.yahoo_daily())
con = sqlite3.connect(f"file:{PE.DB}?mode=ro", uri=True)
costs = {a: PE.ASSETS[a][2] / 1e4 for a in PE.ASSETS}
t = time.time()
ev, after = EV.evolve(st, hist, costs, con, st["prices"], st["fx"]["rate"], PE.liquidate, PE.new_sleeves,
                      dry_run=True, ignore_min_days=ignore, seed=123)
print(f"dry run ({'min-days guard OFF' if ignore else 'normal rules'}) took {time.time()-t:.1f}s; note: {ev['note']}")
print("top 5 by blended fitness:", [(s['id'], round(s['fitness'], 3), f"w_live={s['w_live']:.2f}") for s in ev["scores"][:5]])
print("would kill:", [(k["id"], k["fitness"]) for k in ev["killed"]])
for b in ev["born"]: print("would birth:", b["id"], "from", b["parent"], ("x " + b["mate"]) if b["mate"] else "", b["mutations"], "recent fit", b["recent_fitness"])
print("population after:", len([k for k in after["bots"] if k != "BUY&HOLD"]), "mix:", EV.mix(after))
os.makedirs("logs", exist_ok=True)
json.dump(ev, open(f"logs/dry_run_{int(t)}{'_nominguard' if ignore else ''}.json", "w"), indent=1, default=float)
