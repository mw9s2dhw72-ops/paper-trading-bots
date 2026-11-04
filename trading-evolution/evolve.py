"""Evolutionary tournament of 100 trading bots (5 per strategy x 20 strategies).

Each generation:
  1. every bot is evaluated on the SAME set of rotating random sub-windows of the training period
     (walk-forward-style rotation so bots can't overfit one fixed window);
  2. the worst KILL_FRAC are killed;
  3. survivors are kept, and the dead are replaced by mutated clones (optionally crossed over with a
     same-strategy survivor) of parents picked by tournament selection from the top half.
The held-out test period (2023-01 .. 2026-11) is NEVER used for selection; it is only logged.
"""
import json, os, time, argparse
import numpy as np
import pandas as pd
from strategies import STRATEGIES
from engine import Market, run_bot, metrics, fitness, TRAIN_START, TRAIN_END, TEST_START, TEST_END

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")


def random_genome(strategy, rng):
    spec = STRATEGIES[strategy][1]
    g = {}
    for k, (lo, hi, kind) in spec.items():
        if kind == "f":
            g[k] = float(rng.uniform(lo, hi))
        elif kind == "i":
            g[k] = int(rng.integers(lo, hi + 1))
        else:
            g[k] = int(rng.integers(lo, hi + 1))
    return g


def mutate(strategy, g, rng, rate=0.4, sigma=0.12):
    spec = STRATEGIES[strategy][1]
    g = dict(g)
    changed = False
    for k, (lo, hi, kind) in spec.items():
        if rng.random() > rate:
            continue
        changed = True
        if kind == "f":
            g[k] = float(np.clip(g[k] + rng.normal(0, sigma * (hi - lo)), lo, hi))
        elif kind == "i":
            step = max(1, int(round(abs(rng.normal(0, sigma * (hi - lo))))))
            g[k] = int(np.clip(g[k] + step * rng.choice([-1, 1]), lo, hi))
        else:
            if rng.random() < 0.25:
                g[k] = int(1 - g[k]) if hi == 1 else int(rng.integers(lo, hi + 1))
    if not changed:  # guarantee at least one change
        return mutate(strategy, g, rng, rate=1.0 / len(spec) + 0.01, sigma=sigma)
    return g


def crossover(strategy, g1, g2, rng):
    return {k: (g1[k] if rng.random() < 0.5 else g2[k]) for k in g1}


def key(strategy, g):
    return strategy + "|" + json.dumps({k: round(v, 6) if isinstance(v, float) else v for k, v in sorted(g.items())})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generations", type=int, default=40)
    ap.add_argument("--per-strategy", type=int, default=5)
    ap.add_argument("--kill-frac", type=float, default=0.25)
    ap.add_argument("--windows", type=int, default=3, help="random sub-windows per generation")
    ap.add_argument("--window-years", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--protect-niches", action="store_true",
                    help="variant: each strategy's best bot is never killed (no extinction)")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    RES = os.path.join(HERE, args.out)
    os.makedirs(RES, exist_ok=True)

    market = Market()
    idx = market.index
    train_idx = idx[(idx >= TRAIN_START) & (idx <= TRAIN_END)]
    wlen = int(args.window_years * 252)

    cache = {}
    def series(bot):
        k = key(bot["strategy"], bot["genome"])
        if k not in cache:
            cache[k] = run_bot(market, bot["strategy"], bot["genome"])[0]
        return cache[k]

    # ---- initial population: 5 random bots per strategy
    pop, next_id = [], 0
    for s in STRATEGIES:
        for _ in range(args.per_strategy):
            pop.append(dict(id=next_id, strategy=s, genome=random_genome(s, rng), parent=None, born=0))
            next_id += 1
    N = len(pop)
    n_kill = int(round(args.kill_frac * N))

    log_rows, share_rows, gen_rows = [], [], []
    t0 = time.time()
    for gen in range(args.generations + 1):
        # rotating windows for this generation (same for every bot)
        starts = rng.integers(0, len(train_idx) - wlen, size=args.windows)
        windows = [(train_idx[s], train_idx[s + wlen - 1]) for s in starts]
        for b in pop:
            r = series(b)
            fits = [fitness(r[a:z])[0] for a, z in windows]
            b["fit"] = float(np.mean(fits))
            ftrain, mtrain = fitness(r[TRAIN_START:TRAIN_END])
            ftest, mtest = fitness(r[TEST_START:TEST_END])
            b.update(train_fit=ftrain, test_fit=ftest, train_sharpe=mtrain["sharpe"], test_sharpe=mtest["sharpe"],
                     train_cagr=mtrain["cagr"], test_cagr=mtest["cagr"], train_dd=mtrain["max_dd"], test_dd=mtest["max_dd"],
                     test_return=mtest["total_return"], train_return=mtrain["total_return"])
            log_rows.append(dict(gen=gen, id=b["id"], strategy=b["strategy"], parent=b["parent"], born=b["born"],
                                 window_fit=b["fit"], train_fit=ftrain, test_fit=ftest,
                                 train_sharpe=mtrain["sharpe"], test_sharpe=mtest["sharpe"],
                                 train_cagr=mtrain["cagr"], test_cagr=mtest["cagr"],
                                 train_dd=mtrain["max_dd"], test_dd=mtest["max_dd"], genome=json.dumps(b["genome"])))
        pop.sort(key=lambda b: b["fit"], reverse=True)
        counts = pd.Series([b["strategy"] for b in pop]).value_counts()
        share_rows.append({"gen": gen, **{s: int(counts.get(s, 0)) for s in STRATEGIES}})
        fits = np.array([b["fit"] for b in pop])
        gen_rows.append(dict(gen=gen, best_fit=fits.max(), mean_fit=fits.mean(), median_fit=float(np.median(fits)),
                             mean_train_fit=np.mean([b["train_fit"] for b in pop]),
                             mean_test_fit=np.mean([b["test_fit"] for b in pop]),
                             top10_train_sharpe=np.mean([b["train_sharpe"] for b in pop[:10]]),
                             top10_test_sharpe=np.mean([b["test_sharpe"] for b in pop[:10]]),
                             n_strategies=int((counts > 0).sum()), unique_genomes=len({key(b["strategy"], b["genome"]) for b in pop}),
                             windows=";".join(f"{a.date()}..{z.date()}" for a, z in windows)))
        print(f"gen {gen:2d} best {fits.max():.3f} mean {fits.mean():.3f} strategies {(counts>0).sum():2d} "
              f"top={pop[0]['strategy']} ({time.time()-t0:.0f}s)", flush=True)
        if gen == args.generations:
            break
        # ---- selection: kill bottom n_kill, refill with mutated clones of the best
        survivors = pop[: N - n_kill]
        if args.protect_niches:
            alive = {b["strategy"] for b in survivors}
            for b in pop[N - n_kill:]:
                if b["strategy"] not in alive:          # last-of-its-kind gets a reprieve
                    survivors.append(b); alive.add(b["strategy"])
        top_half = pop[: N // 2]
        children = []
        existing = {key(b["strategy"], b["genome"]) for b in survivors}
        for _ in range(N - len(survivors)):
            cand = [top_half[i] for i in rng.choice(len(top_half), size=3, replace=False)]
            parent = max(cand, key=lambda b: b["fit"])
            g = dict(parent["genome"])
            mates = [b for b in survivors if b["strategy"] == parent["strategy"] and b["id"] != parent["id"]]
            if mates and rng.random() < 0.3:
                g = crossover(parent["strategy"], g, mates[rng.integers(len(mates))]["genome"], rng)
            g = mutate(parent["strategy"], g, rng)
            tries = 0
            while key(parent["strategy"], g) in existing and tries < 20:   # no exact duplicate clones
                g = mutate(parent["strategy"], g, rng); tries += 1
            existing.add(key(parent["strategy"], g))
            children.append(dict(id=next_id, strategy=parent["strategy"], genome=g, parent=parent["id"], born=gen + 1))
            next_id += 1
        pop = survivors + children

    # ---- final ranking on the FULL training period (still no test data used)
    for b in pop:
        b["final_rank_fit"] = b["train_fit"]
    pop.sort(key=lambda b: b["train_fit"], reverse=True)
    final = pd.DataFrame([{k: (json.dumps(v) if k == "genome" else v) for k, v in b.items()} for b in pop])
    final.insert(0, "rank", range(1, len(final) + 1))
    final.to_csv(os.path.join(RES, "final_population.csv"), index=False)
    pd.DataFrame(log_rows).to_csv(os.path.join(RES, "generation_log.csv"), index=False)
    pd.DataFrame(share_rows).to_csv(os.path.join(RES, "strategy_share.csv"), index=False)
    pd.DataFrame(gen_rows).to_csv(os.path.join(RES, "generation_summary.csv"), index=False)

    # ---- equity curves on held-out test for top bots + benchmarks
    ar = market.asset_returns().fillna(0.0)
    w = market.avail.astype(float)
    ew_bh = (ar * w).sum(axis=1) / w.sum(axis=1)
    eq = {}
    for b in pop[:5]:
        eq[f"#{b['id']} {b['strategy']}"] = series(b)
    top10_ens = pd.concat([series(b) for b in pop[:10]], axis=1).mean(axis=1)
    eq["Top-10 ensemble"] = top10_ens
    eq["Buy&Hold equal-weight (7 assets)"] = ew_bh
    eq["Buy&Hold SPY"] = ar["SPY"]
    eq["Buy&Hold BTC"] = ar["BTCUSDT"]
    E = pd.DataFrame(eq)
    E_test = (1 + E[TEST_START:TEST_END]).cumprod()
    E_test.to_csv(os.path.join(RES, "test_equity_curves.csv"))
    E_train = (1 + E[TRAIN_START:TRAIN_END]).cumprod()
    E_train.to_csv(os.path.join(RES, "train_equity_curves.csv"))
    bench = {c: {"train": metrics(E[c][TRAIN_START:TRAIN_END]), "test": metrics(E[c][TEST_START:TEST_END])} for c in E.columns}
    json.dump(dict(args=vars(args), train=[TRAIN_START, TRAIN_END], test=[TEST_START, TEST_END], benchmarks=bench,
                   runtime_s=time.time() - t0), open(os.path.join(RES, "summary.json"), "w"), indent=2, default=float)
    print("done", time.time() - t0)


if __name__ == "__main__":
    main()
