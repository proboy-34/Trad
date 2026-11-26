"""Strategy Pool - the pipeline every strategy goes through.

  NEW   passed the history tests (research + unseen validation) in the Lab
   |    -> from that moment it is SHADOW-TRADED on live market data
   v       (every new candle is data it has never seen = a real market test)
  LIVE  made money in the live market test -> trades the paper account
   |
   v
  RETIRED  lost money in the live market test (or in the paper account)

The pool is not limited to a few strategies: it keeps up to `pool.max_size`
candidates in testing at the same time, while the Lab keeps adding new ones.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from . import backtest
from .lab import STRATEGIES_FILE, load_active, save_active
from .log import get_logger
from .strategy import Strategy

log = get_logger("pool")

DEFAULTS = {
    "max_size": 300,          # strategies in testing at the same time
    "max_live": 10,           # strategies allowed to trade the paper account
    "min_live": 6,            # if fewer are proven, the best NEW ones trade on probation
    "promote_after_trades": 5,
    "promote_min_profit_factor": 1.1,
    "retire_after_trades": 6,
    "retire_if_profit_factor_below": 0.9,
    "retire_if_return_below_pct": -3.0,
    "max_per_family": 12,     # max strategies using the same indicators (keeps research broad)
}


def _score_validation(e: dict) -> float:
    v, f = e["validation"], e.get("final") or {}
    return min(v.get("sharpe", 0), e["research"].get("sharpe", 0)) + 0.25 * f.get("sharpe", 0)


def _final_ok(e: dict) -> bool:
    """Also positive on the final test period (history never used for choosing)."""
    f = e.get("final") or {}
    return f.get("return_pct", 0) > 0 and f.get("sharpe", 0) > 0


def _kind(e: dict) -> tuple:
    """The 'family' of a strategy = which indicators its buy rules use."""
    return tuple(sorted({c["feature"] for c in e["strategy"]["entry"]}))


class StrategyPool:
    def __init__(self, cfg: dict, account=None):
        self.cfg = cfg
        self.pc = {**DEFAULTS, **(cfg.get("pool") or {})}
        self.account = account
        self.lock = threading.RLock()
        d = load_active()
        self.data = {
            "pool": d.get("pool") or [dict(e, stage="new") for e in d.get("active", [])],
            "retired": d.get("retired", [])[-300:],
            "stats": {"tested_total": 0, "found_total": 0, "rounds": 0,
                      **(d.get("totals") or {})},
            "last_round": d.get("stats", {}),
            "updated": d.get("updated"),
        }

    # ------------------------------------------------------------ store
    def save(self):
        with self.lock:
            live = [e for e in self.data["pool"] if e.get("stage") == "live"]
            save_active({
                "updated": self.data["updated"] or time.time(),
                "active": live,                       # what the engine trades
                "pool": self.data["pool"],
                "retired": self.data["retired"][-300:],
                "totals": self.data["stats"],
                "stats": self.data["last_round"],
            })

    def entries(self, stage: str | None = None) -> list[dict]:
        with self.lock:
            return [e for e in self.data["pool"] if stage is None or e.get("stage") == stage]

    def seeds(self, n: int = 20, learn_stats: dict | None = None) -> list[dict]:
        """Best strategies to evolve NEW ideas from: winners of real (learning) trades and
        proven LIVE ones first, then the best history results."""
        learn_stats = learn_stats or {}

        def key(e):
            ls = learn_stats.get(e["strategy"]["id"]) or {}
            won = ls.get("trades", 0) >= 3 and ls.get("profit_factor", 0) >= 1.2
            return (won, e.get("stage") == "live", ls.get("profit_factor", 0) if won else 0,
                    _score_validation(e))
        with self.lock:
            return sorted(self.data["pool"], key=key, reverse=True)[:n]

    # -------------------------------------------------------------- add
    def add_round(self, result: dict) -> int:
        """Adds the strategies that passed a Lab round. Returns how many are new."""
        with self.lock:
            st = result["stats"]
            known = {e["strategy"]["id"] for e in self.data["pool"]}
            known |= {e["strategy"]["id"] for e in self.data["retired"]}
            family = {}
            for e in self.data["pool"]:
                family[_kind(e)] = family.get(_kind(e), 0) + 1
            new = []
            for e in result["passed"]:
                if e["strategy"]["id"] in known:
                    continue
                k = _kind(e)
                if family.get(k, 0) >= self.pc["max_per_family"]:
                    continue                      # enough of this family already
                family[k] = family.get(k, 0) + 1
                new.append(dict(e, stage="new", forward=None))
            self.data["pool"].extend(new)
            # too many? drop the weakest NEW ones (live ones are never dropped here)
            if len(self.data["pool"]) > self.pc["max_size"]:
                live = [e for e in self.data["pool"] if e["stage"] == "live"]
                rest = sorted([e for e in self.data["pool"] if e["stage"] != "live"],
                              key=_score_validation, reverse=True)
                self.data["pool"] = live + rest[: max(0, self.pc["max_size"] - len(live))]
            tot = self.data["stats"]
            tot["tested_total"] = tot.get("tested_total", 0) + st.get("tested", 0)
            tot["found_total"] = tot.get("found_total", 0) + len(new)
            tot["rounds"] = tot.get("rounds", 0) + 1
            self.data["last_round"] = {**st, "new_candidates": len(new), "time": time.time()}
            self.data["updated"] = time.time()
            self.save()
            return len(new)

    # ----------------------------------------------- live market test
    def forward_test(self, datasets: dict, probs: dict, timeframe: str):
        """Shadow-trades every strategy on the candles that appeared AFTER it
        was discovered (fees included, next-candle entries, worst-case fills)."""
        costs = backtest.Costs(self.cfg["paper"]["fee_pct"], self.cfg["paper"]["slippage_pct"],
                               self.cfg["risk"]["max_position_pct"] / 100)
        with self.lock:
            entries = list(self.data["pool"])
        for e in entries:
            s = Strategy.from_dict(e["strategy"])
            per = []
            for sym, feats in datasets.items():
                times = feats["time"].to_numpy()
                start = int(np.searchsorted(times, e["added"] * 1000, side="right"))
                if start >= len(feats) - 1:
                    continue
                r = backtest.run(s, feats, start, None, costs, (probs or {}).get(sym), timeframe)
                per.append(r.metrics)
            if not per:
                e["forward"] = {"trades": 0, "return_pct": 0.0, "profit_factor": 0.0,
                                "win_rate": 0.0, "max_dd_pct": 0.0,
                                "hours": round((time.time() - e["added"]) / 3600, 1)}
                continue
            pf_parts = [m for m in per if m["trades"]]
            e["forward"] = {
                "trades": int(sum(m["trades"] for m in per)),
                "return_pct": float(np.mean([m["return_pct"] for m in per])),
                "profit_factor": float(np.mean([min(m["profit_factor"], 5) for m in pf_parts]))
                if pf_parts else 0.0,
                "win_rate": float(np.mean([m["win_rate"] for m in pf_parts])) if pf_parts else 0.0,
                "max_dd_pct": float(max(m["max_dd_pct"] for m in per)),
                "hours": round((time.time() - e["added"]) / 3600, 1),
            }

    # ------------------------------------------- promote / retire
    def review(self, live_stats: dict | None = None, learn_stats: dict | None = None) -> list[str]:
        """Moves strategies between stages. Returns readable messages.
        Evidence: the live market test (shadow trades), the main paper account and the
        testing trades (real small paper trades)."""
        pc, msgs = self.pc, []
        live_stats = live_stats or {}
        learn_stats = learn_stats or {}
        with self.lock:
            keep = []
            for e in self.data["pool"]:
                f = e.get("forward") or {}
                acc = live_stats.get(e["strategy"]["id"])
                bad_forward = (f.get("trades", 0) >= pc["retire_after_trades"]
                               and (f.get("profit_factor", 0) < pc["retire_if_profit_factor_below"]
                                    or f.get("return_pct", 0) < pc["retire_if_return_below_pct"]))
                bad_account = (acc and acc["trades"] >= pc["retire_after_trades"]
                               and acc["profit_factor"] < pc["retire_if_profit_factor_below"])
                ls = learn_stats.get(e["strategy"]["id"])
                bad_learning = (ls and ls["trades"] >= pc["retire_after_trades"]
                                and ls["profit_factor"] < pc["retire_if_profit_factor_below"])
                if bad_forward or bad_account or bad_learning:
                    why = ("lost in the live market test" if bad_forward
                           else "lost in the paper account" if bad_account
                           else "lost in learning trades")
                    msgs.append(f"Retired {e['strategy']['id']}: {why} "
                                f"({f.get('trades', 0)} test trades, {f.get('return_pct', 0):+.2f}%).")
                    self.data["retired"].append(dict(e, stage="retired", retired=time.time(),
                                                     why=why))
                    continue
                keep.append(e)
            self.data["pool"] = keep

            def learn_ok(e):
                ls = learn_stats.get(e["strategy"]["id"])
                return bool(ls and ls["trades"] >= pc["promote_after_trades"] and ls["pnl"] > 0
                            and ls["profit_factor"] >= pc["promote_min_profit_factor"])

            def forward_ok(e):
                f = e.get("forward") or {}
                return (f.get("trades", 0) >= pc["promote_after_trades"] and f["return_pct"] > 0
                        and f["profit_factor"] >= pc["promote_min_profit_factor"])

            proven = [e for e in keep if forward_ok(e) or learn_ok(e)]
            proven.sort(key=lambda e: (learn_ok(e) and forward_ok(e),
                                       (learn_stats.get(e["strategy"]["id"]) or {}).get("profit_factor", 0),
                                       (e.get("forward") or {}).get("return_pct", 0)), reverse=True)
            live = proven[: pc["max_live"]]
            if len(live) < pc["min_live"]:      # not enough proven yet -> best new on probation
                # strategies already on probation keep their place (no pointless swapping);
                # only free places go to the best new strategies
                ids = {x["strategy"]["id"] for x in live}
                staying = [e for e in keep if e.get("stage") == "live" and e["strategy"]["id"] not in ids]
                staying.sort(key=_score_validation, reverse=True)
                live += staying[: pc["min_live"] - len(live)]
                ids = {x["strategy"]["id"] for x in live}
                # best new ones first - preferring those that ALSO stayed positive on the
                # final unseen test (less likely to have passed by luck)
                rest = sorted([e for e in keep if e["strategy"]["id"] not in ids],
                              key=lambda e: (_final_ok(e), _score_validation(e)), reverse=True)
                # prefer DIFFERENT kinds of strategies (not 3 copies of one idea)
                kinds = {_kind(x) for x in live}
                for e in rest:
                    if len(live) >= pc["min_live"]:
                        break
                    if _kind(e) not in kinds:
                        live.append(e)
                        kinds.add(_kind(e))
                ids = {x["strategy"]["id"] for x in live}
                live += [e for e in rest if e["strategy"]["id"] not in ids][: max(0, pc["min_live"] - len(live))]
            live_ids = {e["strategy"]["id"] for e in live}
            for e in keep:
                old = e.get("stage")
                if e["strategy"]["id"] in live_ids:
                    e["stage"] = "live"
                    e["probation"] = e["strategy"]["id"] not in {x["strategy"]["id"] for x in proven}
                    if old != "live":
                        ls = learn_stats.get(e["strategy"]["id"])
                        if e["probation"]:
                            why = " (probation - best new strategy)"
                        elif learn_ok(e):
                            why = (f" - won in learning trades ({ls['trades']} trades, profit factor "
                                   f"{ls['profit_factor']:.2f})")
                        else:
                            why = f" - made {e['forward']['return_pct']:+.2f}% in the live market test"
                        msgs.append(f"Promoted {e['strategy']['id']} to LIVE{why}.")
                else:
                    if old == "live":
                        msgs.append(f"{e['strategy']['id']} moved back to testing "
                                    "(a better strategy took its place).")
                    e["stage"] = "new"
            self.save()
        return msgs

    def counts(self) -> dict:
        with self.lock:
            pool = self.data["pool"]
            return {"testing": sum(e["stage"] == "new" for e in pool),
                    "live": sum(e["stage"] == "live" for e in pool),
                    "retired": len(self.data["retired"]),
                    **self.data["stats"]}
