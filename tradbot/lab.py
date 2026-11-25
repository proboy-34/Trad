"""Strategy Lab - the bot's research team.

 1. INVENT    hundreds of random strategies (+ ideas from the AI brain)
 2. TEST      each one on the "research" part of history (with fees)
 3. EVOLVE    mutate / combine the best ones, test again (several rounds)
 4. VALIDATE  re-test the best on "validation" history they have never seen
 5. PICK      the top few that passed, preferring different ideas
 6. FINAL     report their score on a last unseen "final test" period
              (never used for choosing - this is the honest number)
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd

from . import backtest, ml
from .config import DATA_DIR
from .features import WARMUP_BARS
from .log import get_logger
from .strategy import Strategy, StrategyGenerator

log = get_logger("lab")

STRATEGIES_FILE = DATA_DIR / "strategies.json"


def load_active() -> dict:
    if STRATEGIES_FILE.exists():
        try:
            return json.loads(STRATEGIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"updated": None, "active": [], "stats": {}}


def save_active(data: dict) -> None:
    tmp = STRATEGIES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STRATEGIES_FILE)


def _splits(n: int, split) -> tuple[int, int, int]:
    usable = n - WARMUP_BARS
    a = WARMUP_BARS + int(usable * split[0])
    b = a + int(usable * split[1])
    return a, b, n


class Lab:
    def __init__(self, cfg: dict, datasets: dict[str, pd.DataFrame], brain=None, progress=None,
                 seed: int | None = None):
        self.cfg = cfg
        self.lc = cfg["lab"]
        self.datasets = datasets
        self.brain = brain
        self.progress = progress or (lambda pct, msg: None)
        self.seed = seed
        self.tf = cfg["timeframe"]
        self.costs = backtest.Costs(cfg["paper"]["fee_pct"], cfg["paper"]["slippage_pct"],
                                    cfg["risk"]["max_position_pct"] / 100)
        self.splits = {s: _splits(len(df), self.lc["split"]) for s, df in datasets.items()}
        self.probs: dict[str, np.ndarray | None] = {s: None for s in datasets}
        self.cache: dict[tuple, dict] = {}
        self.tested = 0

    # ------------------------------------------------------------ evaluate
    def _eval(self, s: Strategy, period: str) -> dict:
        key = (s.id, period)
        if key in self.cache:
            return self.cache[key]
        per_symbol = {}
        chunk_sharpes: list[float] = []
        for sym, feats in self.datasets.items():
            a, b, n = self.splits[sym]
            lo, hi = {"research": (WARMUP_BARS, a), "validation": (a, b), "final": (b, n)}[period]
            r = backtest.run(s, feats, lo, hi, self.costs, self.probs[sym], self.tf)
            per_symbol[sym] = r.metrics
            if period == "research":
                # also score 3 separate windows: a good strategy works in
                # rising, falling AND sideways markets, not just one of them
                bpy = backtest.BARS_PER_YEAR.get(self.tf, 8760)
                edges = np.linspace(0, len(r.equity), 4).astype(int)
                chunk_sharpes.extend(
                    backtest.metrics_from_equity(r.equity[edges[j]:edges[j + 1]], bpy)["sharpe"]
                    for j in range(3))
        ms = list(per_symbol.values())
        agg = {
            "trades": int(sum(m["trades"] for m in ms)),
            "return_pct": float(np.mean([m["return_pct"] for m in ms])),
            "max_dd_pct": float(max(m["max_dd_pct"] for m in ms)),
            "sharpe": float(np.mean([m["sharpe"] for m in ms])),
            "low_sharpe": float(np.percentile([m["sharpe"] for m in ms], 25)),
            "win_rate": float(np.mean([m["win_rate"] for m in ms])),
            "profit_factor": float(np.mean([min(m["profit_factor"], 5) for m in ms])),
            "buy_hold_return_pct": float(np.mean([m["buy_hold_return_pct"] for m in ms])),
            "buy_hold_sharpe": float(np.mean([m["buy_hold_sharpe"] for m in ms])),
            "buy_hold_max_dd_pct": float(max(m["buy_hold_max_dd_pct"] for m in ms)),
            "low_window_sharpe": float(np.percentile(chunk_sharpes, 25)) if chunk_sharpes else 0.0,
            "per_symbol": per_symbol,
        }
        self.cache[key] = agg
        if period == "research":
            self.tested += 1
        return agg

    def _fitness(self, m: dict, min_trades: int) -> float:
        if m["trades"] < min_trades or m["max_dd_pct"] > self.lc["max_drawdown_pct"]:
            return -99.0
        if m["profit_factor"] < 1.0:
            return -50.0 + m["profit_factor"]
        # reward risk-adjusted profit that works on (nearly) EVERY coin and in
        # EVERY market window, not just one lucky period
        return 0.4 * m["sharpe"] + 0.3 * m["low_sharpe"] + 0.3 * m["low_window_sharpe"]

    def _min_trades(self, period: str) -> int:
        base = self.lc["min_trades"]
        frac = {"research": self.lc["split"][0], "validation": self.lc["split"][1],
                "final": self.lc["split"][2]}[period]
        return max(5, int(base * frac / self.lc["split"][0]))

    # ---------------------------------------------------------------- run
    def run(self, probs: dict | None = None, seeds: list[dict] | None = None,
            population: int | None = None, generations: int | None = None,
            train_ml: bool = True, ai_ideas: bool = True) -> dict:
        """One research round. Returns every strategy that passed the
        unseen-data test (near-copies removed). The StrategyPool decides
        what happens to them next (live market test, promotion, retirement)."""
        t0 = time.time()
        use_ml = bool(self.cfg["ml"]["enabled"] and ml.HAVE_LGB)
        ml_report = {}
        if use_ml:
            if probs:
                self.probs.update(probs)
            else:
                self.progress(2, "Training ML model (walk-forward, no peeking)...")
                for sym, feats in self.datasets.items():
                    self.probs[sym] = ml.walk_forward_probs(
                        feats, self.cfg["ml"]["horizon_bars"], self.cfg["ml"]["target_pct"],
                        threads=self.cfg["ml"]["threads"])
            for sym, feats in self.datasets.items():
                y = ml.make_labels(feats, self.cfg["ml"]["horizon_bars"], self.cfg["ml"]["target_pct"])
                ml_report[sym] = {"auc": ml.auc(y, self.probs[sym])}

        # generator uses indicator levels from the RESEARCH period only
        research_parts = [df.iloc[WARMUP_BARS:self.splits[s][0]] for s, df in self.datasets.items()]
        gen = StrategyGenerator(pd.concat(research_parts), seed=self.seed, use_ml=use_ml)
        pop_size = int(population or self.lc["population"])
        gens = max(1, int(generations or self.lc["generations"]))

        # new random ideas + the best known strategies (to evolve new ones from them)
        seed_list = [Strategy.from_dict(x["strategy"]) for x in (seeds or [])]
        pop: list[Strategy] = [gen.random() for _ in range(pop_size)] + seed_list
        ai_count = 0
        if ai_ideas and self.brain is not None and self.cfg["brain"].get("suggest_strategies"):
            self.progress(5, "Asking the AI brain for strategy ideas...")
            try:
                ideas = self.brain.suggest_strategies(seeds or [], gen.levels)
                pop += ideas
                ai_count = len(ideas)
            except Exception as e:
                log.warning("AI brain ideas skipped: %s", e)

        min_tr = self._min_trades("research")
        batch = max(10, int(self.lc["batch_size"]))
        scored: dict[str, tuple[float, Strategy]] = {}
        for g in range(gens):
            for k in range(0, len(pop), batch):
                for s in pop[k:k + batch]:
                    if s.id not in scored:
                        scored[s.id] = (self._fitness(self._eval(s, "research"), min_tr), s)
                pct = 8 + 70 * (g + min(1.0, (k + batch) / len(pop))) / gens
                self.progress(pct, f"Generation {g + 1}/{gens}: tested {self.tested} strategies")
            ranked = sorted(scored.values(), key=lambda x: -x[0])
            elite = [s for f, s in ranked[: max(5, len(ranked) // 10)] if f > -50]
            if g == gens - 1 or not elite:
                break
            pop = []
            while len(pop) < pop_size:
                r = gen.rng.random()
                if r < 0.6:
                    pop.append(gen.mutate(gen.rng.choice(elite)))
                elif r < 0.85 and len(elite) > 1:
                    a, b = gen.rng.sample(elite, 2)
                    pop.append(gen.crossover(a, b))
                else:
                    pop.append(gen.random())

        # ---------------------------------------------------- validation
        self.progress(82, "Re-testing the best on unseen data...")
        ranked = sorted(scored.values(), key=lambda x: -x[0])
        candidates = [s for f, s in ranked[:60] if f > 0]
        val_min = self._min_trades("validation")
        passed = []
        for s in candidates:
            v = self._eval(s, "validation")
            if passes_unseen_test(v, val_min, self.lc["max_drawdown_pct"]):
                rs = self._eval(s, "research")
                passed.append((min(rs["sharpe"], v["sharpe"]), s))
        passed.sort(key=lambda x: -x[0])

        # drop near-copies (strategies that buy at almost the same moments)
        kept: list[Strategy] = []
        signals = {}
        ref_sym = next(iter(self.datasets))
        ref = self.datasets[ref_sym]
        for _, s in passed:
            sig = s.entry_signal(ref, self.probs[ref_sym])
            if any(((sig & signals[o.id]).sum() / max(1, (sig | signals[o.id]).sum())) > 0.7
                   for o in kept):
                continue
            kept.append(s)
            signals[s.id] = sig

        self.progress(92, "Final honest test...")
        entries = [{
            "strategy": s.to_dict(),
            "description": s.describe(),
            "research": _strip(self._eval(s, "research")),
            "validation": _strip(self._eval(s, "validation")),
            "final": _strip(self._eval(s, "final")),
            "added": time.time(),
        } for s in kept]
        if use_ml and train_ml:
            for sym, feats in self.datasets.items():
                ml.train_final(feats, sym, self.cfg["ml"]["horizon_bars"],
                               self.cfg["ml"]["target_pct"], self.cfg["ml"]["threads"])

        stats = {
            "tested": self.tested, "ai_ideas": ai_count,
            "passed_research": len(candidates), "passed_validation": len(passed),
            "new_candidates": len(entries), "seconds": round(time.time() - t0, 1),
            "ml": ml_report,
        }
        self.progress(100, f"Round done: tested {self.tested}, {len(passed)} passed")
        log.info("Research round finished in %.0fs: tested %d, passed unseen test %d",
                 stats["seconds"], self.tested, len(passed))
        return {"passed": entries, "stats": stats, "probs": self.probs}


def passes_unseen_test(v: dict, min_trades: int, max_dd: float) -> bool:
    """Rules a strategy must pass on data it has never seen."""
    per = v.get("per_symbol") or {}
    if per and sum(m["return_pct"] > 0 for m in per.values()) < len(per) / 2:
        return False                                             # must win on half the coins+
    return (v["trades"] >= min_trades
            and v["return_pct"] > 0                              # made money
            and v["sharpe"] > 0                                  # ...also risk-adjusted
            and v["profit_factor"] >= 1.05                       # wins bigger than losses
            and v["return_pct"] > v["buy_hold_return_pct"]       # beat buy & hold on profit
            and v["max_dd_pct"] <= min(max_dd, v["buy_hold_max_dd_pct"]))   # and on risk


def _strip(m: dict) -> dict:
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()
            if k != "per_symbol"} | {"per_symbol": {
                s: {k: round(v, 3) if isinstance(v, float) else v for k, v in pm.items()}
                for s, pm in m.get("per_symbol", {}).items()}}
