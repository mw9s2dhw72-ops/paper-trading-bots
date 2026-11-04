"""Produce PNG charts + a compact JSON digest from the results folders."""
import json, os
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

R = "results"; C = "charts"; os.makedirs(C, exist_ok=True)
gs = pd.read_csv(f"{R}/generation_summary.csv")
sh = pd.read_csv(f"{R}/strategy_share.csv").set_index("gen")
eq = pd.read_csv(f"{R}/test_equity_curves.csv", index_col=0, parse_dates=True)
fin = pd.read_csv(f"{R}/final_population.csv")
log = pd.read_csv(f"{R}/generation_log.csv")
summ = json.load(open(f"{R}/summary.json"))

# 1 fitness per generation
fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(gs.gen, gs.best_fit, label="best (rotating windows)")
ax.plot(gs.gen, gs.mean_fit, label="mean (rotating windows)")
ax.plot(gs.gen, gs.mean_train_fit, label="mean, full train 2017-2022")
ax.plot(gs.gen, gs.mean_test_fit, "--", label="mean, held-out test 2023-2026 (not used for selection)")
ax.set_xlabel("generation"); ax.set_ylabel("fitness = Sharpe - maxDD"); ax.legend(); ax.grid(alpha=.3)
ax.set_title("Fitness per generation"); fig.tight_layout(); fig.savefig(f"{C}/fitness_per_generation.png", dpi=120)

# 2 strategy share
fig, ax = plt.subplots(figsize=(12, 6))
cols = sh.columns[sh.sum() > 0]
ax.stackplot(sh.index, sh[cols].T.values, labels=cols, colors=plt.cm.tab20(np.linspace(0, 1, len(cols))))
ax.set_xlabel("generation"); ax.set_ylabel("bots"); ax.set_ylim(0, 100); ax.set_xlim(0, sh.index.max())
ax.legend(loc="center left", bbox_to_anchor=(1, .5), fontsize=8); ax.set_title("Strategy share of the 100-bot population")
fig.tight_layout(); fig.savefig(f"{C}/strategy_share.png", dpi=120)

# 3 test equity
fig, ax = plt.subplots(figsize=(11, 5.5))
for c in eq.columns:
    ax.plot(eq.index, eq[c], label=c, lw=2 if ("ensemble" in c or "Buy" in c) else 1, ls="--" if "Buy" in c else "-")
ax.set_yscale("log"); ax.set_ylabel("growth of $1 (log)"); ax.legend(fontsize=8); ax.grid(alpha=.3)
ax.set_title("Held-out test 2023-01-01 to 2026-11-03: evolved bots vs buy-and-hold (after costs)")
fig.tight_layout(); fig.savefig(f"{C}/test_equity_vs_buy_hold.png", dpi=120)

# 4 overfitting scatter gen 0 vs final
fig, ax = plt.subplots(figsize=(7, 6))
g0 = log[log.gen == 0]
ax.scatter(g0.train_sharpe, g0.test_sharpe, s=12, alpha=.6, label="generation 0 (random)")
ax.scatter(fin.train_sharpe, fin.test_sharpe, s=12, alpha=.6, label="final generation")
lim = [-2, 2.2]; ax.plot(lim, lim, "k:", lw=1); ax.set_xlabel("train Sharpe 2017-2022"); ax.set_ylabel("test Sharpe 2023-2026")
ax.legend(); ax.grid(alpha=.3); ax.set_title("Train vs test Sharpe per bot"); fig.tight_layout(); fig.savefig(f"{C}/train_vs_test_scatter.png", dpi=120)

# 5 niche run per-strategy champions
nf = pd.read_csv("results_niches/final_population.csv")
champ = nf.sort_values("train_fit", ascending=False).groupby("strategy").head(1).sort_values("test_sharpe")
fig, ax = plt.subplots(figsize=(10, 7)); y = np.arange(len(champ))
ax.barh(y - .2, champ.train_sharpe, .4, label="train Sharpe"); ax.barh(y + .2, champ.test_sharpe, .4, label="test Sharpe")
ax.set_yticks(y); ax.set_yticklabels(champ.strategy, fontsize=8); ax.legend(); ax.grid(alpha=.3, axis="x")
ax.set_title("Niche-protected run: evolved champion of each strategy"); fig.tight_layout(); fig.savefig(f"{C}/strategy_champions_train_vs_test.png", dpi=120)
print("charts written")

# ---- digest for the report
dig = {}
dig["gen_summary"] = gs.drop(columns="windows").round(3).to_dict("records")
dig["share"] = sh.reset_index().to_dict("records")
dig["extinction_gen"] = {s: (int(sh.index[sh[s] == 0].min()) if (sh[s] == 0).any() else None) for s in sh.columns}
dig["top10"] = fin.head(10)[["rank", "id", "strategy", "born", "parent", "genome", "train_sharpe", "train_cagr", "train_dd", "train_return",
                             "test_sharpe", "test_cagr", "test_dd", "test_return"]].round(4).to_dict("records")
dig["benchmarks"] = summ["benchmarks"]
wk = eq.resample("W-FRI").last()
dig["equity_weekly"] = {"dates": [d.strftime("%Y-%m-%d") for d in wk.index], **{c: wk[c].round(4).tolist() for c in wk.columns}}
gg = g0.groupby("strategy")
dig["gen0_by_strategy"] = pd.DataFrame({"best_train_sharpe": gg.train_sharpe.max(), "median_train_sharpe": gg.train_sharpe.median(),
                                        "median_test_sharpe": gg.test_sharpe.median(), "best_train_fit": gg.train_fit.max()}).round(3).reset_index().to_dict("records")
dig["niche_champions"] = champ[["strategy", "genome", "born", "train_sharpe", "train_cagr", "train_dd", "test_sharpe", "test_cagr", "test_dd", "test_return"]].round(4).to_dict("records")
dig["seeds"] = pd.read_csv("results/robustness_seeds.csv").round(4).to_dict("records")
dig["corr_train_test_final"] = float(fin[["train_sharpe", "test_sharpe"]].corr().iloc[0, 1])
dig["corr_train_test_gen0"] = float(g0[["train_sharpe", "test_sharpe"]].corr().iloc[0, 1])
json.dump(dig, open(f"{R}/report_digest.json", "w"), indent=1, default=float)
