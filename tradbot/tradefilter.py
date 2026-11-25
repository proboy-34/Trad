"""Learned trade filter - a second ML model that learns which BUY SIGNALS fail.

Training data: every trade that the strategies in the pool made on data they
were NOT built on (validation + final-test periods + the live market test).
That is thousands of trades, each with the market situation at the moment of
the signal (all indicators, the ML chance of rising, the strategy settings)
and the result (win or loss).

Honest switch-on rule: the filter is trained on OLDER trades and checked on
NEWER trades it never saw. It is only switched ON if, on those newer trades,
the trades it would keep are clearly better than all trades. Otherwise it
stays OFF and the bot trades without it.
"""
from __future__ import annotations

import json
import pickle
import time
import warnings

import numpy as np

from . import backtest, ml
from .config import DATA_DIR
from .features import FEATURE_NAMES, WARMUP_BARS
from .log import get_logger
from .strategy import Strategy

log = get_logger("filter")

MODEL_FILE = DATA_DIR / "models" / "trade_filter.pkl"
REPORT_FILE = DATA_DIR / "trade_filter.json"
EXTRA = ["stop_atr", "take_atr", "max_hold", "n_rules", "be_atr", "ml_prob"]
COLUMNS = FEATURE_NAMES + EXTRA


def _vector(row_values: np.ndarray, s: Strategy, ml_prob) -> np.ndarray:
    extra = [s.stop_atr, s.take_atr, s.max_hold, len(s.entry), s.be_atr or 0.0,
             np.nan if ml_prob is None else float(ml_prob)]
    return np.concatenate([np.asarray(row_values, dtype=float), np.asarray(extra, dtype=float)])


class TradeFilter:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.model = None
        self.report: dict = {"enabled": False, "status": "not trained yet"}
        try:
            if REPORT_FILE.exists():
                self.report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
            if self.report.get("enabled") and MODEL_FILE.exists():
                with open(MODEL_FILE, "rb") as f:
                    self.model = pickle.load(f)
        except Exception as e:
            log.warning("Trade filter could not be loaded: %s", e)
            self.report = {"enabled": False, "status": "could not load - will retrain"}
        if self.model is None:
            self.report["enabled"] = False

    @property
    def enabled(self) -> bool:
        return bool(self.report.get("enabled") and self.model is not None)

    # ---------------------------------------------------------- data
    def collect(self, entries: list[dict], datasets: dict, probs: dict, timeframe: str,
                split_frac: float = 0.6, max_strategies: int = 200):
        """Trades of pool strategies on unseen periods -> (X, y, returns, times)."""
        costs = backtest.Costs(self.cfg["paper"]["fee_pct"], self.cfg["paper"]["slippage_pct"],
                               self.cfg["risk"]["max_position_pct"] / 100)
        X, y, rets, times = [], [], [], []
        arrays = {sym: f[FEATURE_NAMES].to_numpy(dtype=float) for sym, f in datasets.items()}
        for e in entries[:max_strategies]:
            s = Strategy.from_dict(e["strategy"])
            for sym, f in datasets.items():
                start = WARMUP_BARS + int((len(f) - WARMUP_BARS) * split_frac)
                p = (probs or {}).get(sym)
                r = backtest.run(s, f, start, None, costs, p, timeframe, record_trades=True)
                for t in r.trades:
                    i = t["i"]
                    mp = None if p is None or np.isnan(p[i]) else p[i]
                    X.append(_vector(arrays[sym][i], s, mp))
                    y.append(1.0 if t["return_pct"] > 0 else 0.0)
                    rets.append(t["return_pct"])
                    times.append(t["entry_time"])
        if not X:
            return None
        order = np.argsort(times)
        return (np.array(X)[order], np.array(y)[order], np.array(rets)[order],
                np.array(times)[order])

    # --------------------------------------------------------- train
    def _fit(self, X, y):
        import lightgbm as lgb
        params = dict(objective="binary", learning_rate=0.05, num_leaves=15, min_data_in_leaf=40,
                      feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=2.0,
                      num_threads=self.cfg["ml"]["threads"], verbose=-1, seed=11)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=150)

    def train(self, data) -> dict:
        """Older 50% -> learn, next 20% -> pick the threshold, newest 30% -> honest test."""
        rep = {"updated": time.time(), "enabled": False}
        if not ml.HAVE_LGB:
            rep["status"] = "LightGBM not installed"
        elif data is None or len(data[0]) < 400:
            rep["status"] = f"collecting trades ({0 if data is None else len(data[0])} of 400 needed)"
        else:
            X, y, rets, _ = data
            n = len(y)
            a, b = int(n * 0.5), int(n * 0.7)
            m = self._fit(X[:a], y[:a])
            p_mid, p_test = m.predict(X[a:b]), m.predict(X[b:])
            best_thr, best_avg = None, rets[a:b].mean()
            for thr in (0.4, 0.45, 0.5, 0.55, 0.6):
                keep = p_mid >= thr
                if keep.mean() >= 0.4 and rets[a:b][keep].mean() > best_avg:
                    best_thr, best_avg = thr, rets[a:b][keep].mean()
            all_avg = float(rets[b:].mean())
            auc = ml.auc(y[b:], p_test)
            rep.update(trades=n, test_trades=n - b, auc=round(auc, 3) if auc == auc else None,
                       all_avg_pct=round(all_avg, 3), all_win_rate=round(float(y[b:].mean()) * 100, 1))
            if best_thr is None:
                rep["status"] = "no useful threshold found - filter OFF"
            else:
                keep = p_test >= best_thr
                kept_avg = float(rets[b:][keep].mean()) if keep.any() else 0.0
                rep.update(threshold=best_thr, kept_frac=round(float(keep.mean()), 3),
                           kept_avg_pct=round(kept_avg, 3),
                           kept_win_rate=round(float(y[b:][keep].mean()) * 100, 1) if keep.any() else 0)
                useful = (auc == auc and auc >= 0.53 and keep.mean() >= 0.3
                          and kept_avg > all_avg + 0.05 and kept_avg > 0)
                if useful:
                    self.model = self._fit(X, y)           # final model on all trades
                    MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
                    with open(MODEL_FILE, "wb") as f:
                        pickle.dump(self.model, f)
                    rep["enabled"] = True
                    rep["status"] = "ON - it improved results on newer trades it never saw"
                else:
                    rep["status"] = "OFF - it did not clearly improve newer trades"
        self.report = rep
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        return rep

    # ------------------------------------------------------------ use
    def check(self, feats_row, s: Strategy, ml_prob) -> tuple[bool, float | None]:
        """(allowed, chance of winning). Always allowed while the filter is OFF."""
        if not self.enabled:
            return True, None
        x = _vector(feats_row[FEATURE_NAMES].to_numpy(dtype=float), s, ml_prob)
        p = float(self.model.predict(x.reshape(1, -1))[0])
        return p >= self.report.get("threshold", 0.5), p
