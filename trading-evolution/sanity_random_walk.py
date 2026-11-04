"""Look-ahead sanity check: run every strategy on synthetic random-walk OHLCV (no predictability).
Mean Sharpe should be ~0 or negative (costs). A strongly positive value would indicate look-ahead bias."""
import numpy as np
from strategies import STRATEGIES
from evolve import random_genome
from engine import metrics

rng = np.random.default_rng(1)
def synth(n=2500, steps=40, vol=0.02):
    # overnight gap + intraday path that STARTS at the open (martingale throughout)
    z = rng.normal(0, vol / np.sqrt(steps), size=(n, steps))
    gap = rng.normal(0, vol * 0.3, n)
    lp = np.cumsum(np.c_[gap, z], axis=1).ravel()  # per day: gap step then intraday steps
    lp = (np.log(100) + np.cumsum(np.c_[gap, z].ravel())).reshape(n, steps + 1)
    path = np.exp(lp)
    o = path[:, 0]; c = path[:, -1]; h = path.max(1); l = path.min(1)
    return dict(o=o, h=h, l=l, c=c, v=rng.uniform(1e6, 2e6, n), cost=0.001)

for s, (fn, _) in STRATEGIES.items():
    sh = []
    for k in range(40):
        d = synth(); d2 = synth(); d["pc"] = d2["c"]
        r, _ = fn(d, random_genome(s, rng))
        sh.append(metrics(r[300:])["sharpe"])
    print(f"{s:32s} mean Sharpe {np.mean(sh):+.2f}  (sd {np.std(sh):.2f})")
