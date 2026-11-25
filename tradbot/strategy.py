"""A strategy = buy rules + exit rules, stored as simple JSON.

Example (in plain English):
  BUY when RSI(14) is below 30 AND price is above its 200-candle average
  SELL when the stop-loss (2 x ATR) or take-profit (4 x ATR) is hit,
       or after 48 candles at most.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .features import FEATURE_NAMES, FEATURES, fmt_value


@dataclass
class Condition:
    feature: str
    op: str          # "<" or ">"
    value: float

    def describe(self) -> str:
        label = FEATURES.get(self.feature, (self.feature, "num"))[0].split(" - ")[0]
        word = "below" if self.op == "<" else "above"
        return f"{label} is {word} {fmt_value(self.feature, self.value)}"


@dataclass
class Strategy:
    entry: list[Condition]
    stop_atr: float = 2.0          # stop-loss distance in ATRs
    take_atr: float = 4.0          # take-profit distance in ATRs
    max_hold: int = 48             # max candles to hold a trade
    exit: list[Condition] = field(default_factory=list)   # optional early exit
    ml_min_prob: float | None = None   # only buy if ML model agrees this much
    be_atr: float | None = None    # move stop to break-even after +be_atr x ATR (None = off)
    trail_atr: float | None = None  # trailing stop: follows the highest price at trail_atr x ATR
    origin: str = "random"         # random | evolved | ai
    id: str = ""

    def __post_init__(self):
        self.entry = [c if isinstance(c, Condition) else Condition(**c) for c in self.entry]
        self.exit = [c if isinstance(c, Condition) else Condition(**c) for c in self.exit]
        if not self.id:
            self.id = self.fingerprint()

    def fingerprint(self) -> str:
        d = self.to_dict()
        d.pop("id", None)
        d.pop("origin", None)
        for optional in ("be_atr", "trail_atr"):   # keeps ids of older strategies unchanged
            if d.get(optional) is None:
                d.pop(optional, None)
        raw = json.dumps(d, sort_keys=True)
        return "S" + hashlib.sha1(raw.encode()).hexdigest()[:7].upper()

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Strategy":
        keys = {"entry", "stop_atr", "take_atr", "max_hold", "exit", "ml_min_prob", "be_atr",
                "trail_atr", "origin", "id"}
        return Strategy(**{k: v for k, v in d.items() if k in keys})

    # ------------------------------------------------------------ signals
    def entry_signal(self, feats: pd.DataFrame, ml_prob: np.ndarray | None = None) -> np.ndarray:
        sig = np.ones(len(feats), dtype=bool)
        for c in self.entry:
            col = feats[c.feature].to_numpy()
            with np.errstate(invalid="ignore"):
                sig &= (col < c.value) if c.op == "<" else (col > c.value)
        if self.ml_min_prob is not None:
            if ml_prob is None:
                sig[:] = False
            else:
                with np.errstate(invalid="ignore"):
                    sig &= ml_prob >= self.ml_min_prob
        return sig

    def exit_signal(self, feats: pd.DataFrame) -> np.ndarray:
        if not self.exit:
            return np.zeros(len(feats), dtype=bool)
        sig = np.ones(len(feats), dtype=bool)
        for c in self.exit:
            col = feats[c.feature].to_numpy()
            with np.errstate(invalid="ignore"):
                sig &= (col < c.value) if c.op == "<" else (col > c.value)
        return sig

    def check(self, row, prob: float | None = None) -> dict:
        """Which buy rules are met on one candle (for the live dashboard)."""
        rules = []
        for c in self.entry:
            v = row[c.feature]
            ok = bool(v == v and ((v < c.value) if c.op == "<" else (v > c.value)))
            label = FEATURES.get(c.feature, (c.feature, ""))[0].split(" - ")[0].split(" (")[0]
            rules.append({"rule": f"{label} {'below' if c.op == '<' else 'above'} "
                                  f"{fmt_value(c.feature, c.value)}",
                          "now": fmt_value(c.feature, float(v)) if v == v else "n/a", "ok": ok})
        if self.ml_min_prob is not None:
            ok = prob is not None and prob == prob and prob >= self.ml_min_prob
            rules.append({"rule": f"ML chance of rising at least {self.ml_min_prob:.0%}",
                          "now": f"{prob:.0%}" if prob is not None and prob == prob else "n/a",
                          "ok": bool(ok)})
        met = sum(r["ok"] for r in rules)
        return {"rules": rules, "met": met, "total": len(rules), "signal": met == len(rules)}

    def describe(self) -> dict:
        buy = " AND ".join(c.describe() for c in self.entry)
        if self.ml_min_prob is not None:
            buy += f" AND the ML model says at least {self.ml_min_prob:.0%} chance of rising"
        sell = [f"stop-loss at {self.stop_atr:.1f} x ATR below entry",
                f"take-profit at {self.take_atr:.1f} x ATR above entry",
                f"or after {self.max_hold} candles"]
        if self.be_atr:
            sell.insert(1, f"stop moves to break-even once price is up {self.be_atr:.1f} x ATR")
        if self.trail_atr:
            sell.insert(1, f"trailing stop {self.trail_atr:.1f} x ATR below the highest price")
        if self.exit:
            sell.append("or early when " + " AND ".join(c.describe() for c in self.exit))
        return {"buy": "BUY when " + buy, "sell": "SELL at " + ", ".join(sell)}


# ---------------------------------------------------------------- generator
class StrategyGenerator:
    """Invents random strategies and mutates good ones (a genetic search).
    Thresholds come from the real distribution of each indicator, so every
    rule is sensible (e.g. RSI thresholds between 10 and 90)."""

    QUANTILES = [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
    BE_CHOICES = [None, None, 1.0, 1.5, 2.0]        # break-even stop: off, or after +x ATR
    TRAIL_CHOICES = [None, None, None, 1.5, 2.0, 3.0]   # trailing stop: off, or x ATR below the top

    def __init__(self, feats: pd.DataFrame, seed: int | None = None, use_ml: bool = False):
        self.rng = random.Random(seed)
        self.use_ml = use_ml
        self.levels = {}
        for name in FEATURE_NAMES:
            col = feats[name].dropna()
            if len(col) > 50:
                self.levels[name] = [float(x) for x in col.quantile(self.QUANTILES).to_numpy()]
        self.names = list(self.levels)

    def _condition(self) -> Condition:
        name = self.rng.choice(self.names)
        q = self.rng.randrange(len(self.QUANTILES))
        op = "<" if q < len(self.QUANTILES) // 2 else ">"
        if self.rng.random() < 0.2:
            op = ">" if op == "<" else "<"
        return Condition(name, op, round(self.levels[name][q], 6))

    def random(self) -> Strategy:
        n = self.rng.choice([1, 2, 2, 3])
        names_used, entry = set(), []
        while len(entry) < n:
            c = self._condition()
            if c.feature not in names_used:
                names_used.add(c.feature)
                entry.append(c)
        exits = [self._condition()] if self.rng.random() < 0.3 else []
        ml = None
        if self.use_ml and self.rng.random() < 0.4:
            ml = self.rng.choice([0.5, 0.55, 0.6, 0.65])
        return Strategy(
            entry=entry,
            stop_atr=self.rng.choice([1.0, 1.5, 2.0, 2.5, 3.0]),
            take_atr=self.rng.choice([1.5, 2.0, 3.0, 4.0, 6.0]),
            max_hold=self.rng.choice([12, 24, 48, 96]),
            exit=exits, ml_min_prob=ml, origin="random",
            be_atr=self.rng.choice(self.BE_CHOICES),
            trail_atr=self.rng.choice(self.TRAIL_CHOICES),
        )

    def mutate(self, s: Strategy) -> Strategy:
        d = json.loads(json.dumps(s.to_dict()))
        d.pop("id")
        r = self.rng.random()
        if r < 0.35 and d["entry"]:
            # nudge one threshold to a neighbouring level
            c = self.rng.choice(d["entry"])
            lv = self.levels.get(c["feature"])
            if lv:
                idx = min(range(len(lv)), key=lambda i: abs(lv[i] - c["value"]))
                idx = max(0, min(len(lv) - 1, idx + self.rng.choice([-1, 1])))
                c["value"] = round(lv[idx], 6)
        elif r < 0.5 and len(d["entry"]) < 4:
            c = self._condition()
            if c.feature not in {x["feature"] for x in d["entry"]}:
                d["entry"].append(asdict(c))
        elif r < 0.6 and len(d["entry"]) > 1:
            d["entry"].pop(self.rng.randrange(len(d["entry"])))
        elif r < 0.75:
            d["stop_atr"] = self.rng.choice([1.0, 1.5, 2.0, 2.5, 3.0])
        elif r < 0.9:
            d["take_atr"] = self.rng.choice([1.5, 2.0, 3.0, 4.0, 6.0])
        elif r < 0.94:
            d["max_hold"] = self.rng.choice([12, 24, 48, 96])
        elif r < 0.97:
            d["be_atr"] = self.rng.choice(self.BE_CHOICES)
        else:
            d["trail_atr"] = self.rng.choice(self.TRAIL_CHOICES)
        d["origin"] = "evolved"
        return Strategy.from_dict(d)

    def crossover(self, a: Strategy, b: Strategy) -> Strategy:
        pool = {c.feature: c for c in a.entry + b.entry}
        conds = list(pool.values())
        self.rng.shuffle(conds)
        conds = conds[: self.rng.choice([1, 2, 3])]
        pick = self.rng.choice([a, b])
        return Strategy(entry=[Condition(c.feature, c.op, c.value) for c in conds],
                        stop_atr=pick.stop_atr, take_atr=pick.take_atr,
                        max_hold=pick.max_hold, exit=list(pick.exit),
                        ml_min_prob=self.rng.choice([a.ml_min_prob, b.ml_min_prob]),
                        be_atr=self.rng.choice([a.be_atr, b.be_atr]),
                        trail_atr=self.rng.choice([a.trail_atr, b.trail_atr]), origin="evolved")


def validate_strategy_dict(d: dict) -> Strategy | None:
    """Checks a strategy coming from outside (e.g. the AI brain). Returns None if invalid."""
    try:
        entry = d.get("entry") or []
        if not 1 <= len(entry) <= 4:
            return None
        for c in entry + (d.get("exit") or []):
            if c.get("feature") not in FEATURES or c.get("op") not in ("<", ">"):
                return None
            c["value"] = float(c["value"])
        d["stop_atr"] = float(min(max(float(d.get("stop_atr", 2.0)), 0.5), 5.0))
        d["take_atr"] = float(min(max(float(d.get("take_atr", 4.0)), 0.5), 10.0))
        d["max_hold"] = int(min(max(int(d.get("max_hold", 48)), 2), 240))
        for key in ("be_atr", "trail_atr"):
            v = d.get(key)
            d[key] = None if v in (None, "", "null", 0) else float(min(max(float(v), 0.5), 5.0))
        mp = d.get("ml_min_prob")
        d["ml_min_prob"] = None if mp in (None, "", "null") else float(min(max(float(mp), 0.3), 0.9))
        d["origin"] = "ai"
        d.pop("id", None)
        return Strategy.from_dict(d)
    except (TypeError, ValueError, KeyError):
        return None
