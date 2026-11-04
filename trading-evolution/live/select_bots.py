"""Pick profitable bots (test-period return > 0 after costs) from the main and no-extinction runs,
then drop behavioural clones: a bot is skipped if its daily return series (2017-2026) has correlation
>= 0.98 with an already-kept bot of the same strategy (kept in order of training fitness)."""
import json, os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine import Market, run_bot
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
TH = 0.98
rows = []
for R, src in [("results", "main"), ("results_niches", "no-extinction")]:
    f = pd.read_csv(os.path.join(ROOT, R, "final_population.csv"))
    n_all = len(f); f = f[f.test_return > 0]
    print(src, "profitable", len(f), "of", n_all)
    for _, r in f.iterrows():
        rows.append(dict(src=src, orig_id=int(r.id), strategy=r.strategy, genome=json.loads(r.genome),
                         train_fit=float(r.train_fit), train_sharpe=float(r.train_sharpe),
                         test_sharpe=float(r.test_sharpe), test_return=float(r.test_return), test_dd=float(r.test_dd)))
df = pd.DataFrame(rows)
df["k"] = df.strategy + df.genome.map(lambda g: json.dumps(g, sort_keys=True))
df = df.drop_duplicates("k").sort_values("train_fit", ascending=False).reset_index(drop=True)
m = Market()
rets = {i: run_bot(m, r.strategy, r.genome)[0]["2017":] for i, r in df.iterrows()}
kept = []
for i in df.index:
    if all(np.corrcoef(rets[i], rets[k])[0, 1] < TH for k in kept if df.strategy[k] == df.strategy[i]):
        kept.append(i)
out = []
for n, i in enumerate(kept):
    r = df.loc[i]
    short = {"Supertrend": "ST", "Ichimoku Cloud": "ICH"}.get(r.strategy, "".join(w[0] for w in r.strategy.split() if w[0].isalpha()))
    out.append(dict(bot_id=f"{short}-{r.orig_id}", src=r.src, orig_id=int(r.orig_id), strategy=r.strategy, genome=r.genome,
                    backtest=dict(train_sharpe=r.train_sharpe, test_sharpe=r.test_sharpe, test_return=r.test_return, test_dd=r.test_dd)))
json.dump(out, open(os.path.join(HERE, "bots.json"), "w"), indent=1)
print("unique profitable", len(df), "kept after clone filter", len(out))
print(pd.Series([o["strategy"] for o in out]).value_counts().to_string())
