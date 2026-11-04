"""Re-run the whole evolution with different random seeds to see whether the outcome is stable."""
import subprocess, sys, json, os
import pandas as pd
rows = []
for seed in range(1, 11):
    out = f"results_seeds/seed_{seed}"
    subprocess.run([sys.executable, "evolve.py", "--seed", str(seed), "--out", out], check=True, capture_output=True)
    f = pd.read_csv(os.path.join(out, "final_population.csv"))
    s = json.load(open(os.path.join(out, "summary.json")))
    top = f.iloc[0]
    mix = f.strategy.value_counts()
    rows.append(dict(seed=seed, winner=top.strategy, winner_genome=top.genome, dominant_share=int(mix.iloc[0]),
                     dominant=mix.index[0], n_strategies_left=len(mix),
                     top1_train_sharpe=top.train_sharpe, top1_test_sharpe=top.test_sharpe,
                     top1_test_return=top.test_return, top1_test_dd=top.test_dd,
                     ens_test_sharpe=s["benchmarks"]["Top-10 ensemble"]["test"]["sharpe"],
                     ens_test_return=s["benchmarks"]["Top-10 ensemble"]["test"]["total_return"]))
    print(rows[-1], flush=True)
pd.DataFrame(rows).to_csv("results/robustness_seeds.csv", index=False)
