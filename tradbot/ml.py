"""Machine learning model (LightGBM): "what is the chance price rises soon?"

Walk-forward training: to predict a period, the model is trained ONLY on
earlier data (with a gap so no future information leaks in). That way the
probabilities used in the Strategy Lab are honest, never "already seen".
"""
from __future__ import annotations

import pickle
import warnings

import numpy as np
import pandas as pd

from .config import DATA_DIR
from .features import FEATURE_NAMES, WARMUP_BARS
from .log import get_logger

log = get_logger("ml")

try:
    import lightgbm as lgb
    HAVE_LGB = True
except Exception:   # pragma: no cover - optional dependency
    HAVE_LGB = False

MODEL_DIR = DATA_DIR / "models"


def make_labels(feats: pd.DataFrame, horizon: int, target_pct: float) -> np.ndarray:
    """1 if the price `horizon` candles later is at least target_pct% higher."""
    c = feats["close"].to_numpy()
    fut = np.full(len(c), np.nan)
    fut[:-horizon] = c[horizon:] / c[:-horizon] - 1
    y = (fut >= target_pct / 100).astype(float)
    y[np.isnan(fut)] = np.nan
    return y


def _params(threads: int) -> dict:
    return dict(objective="binary", learning_rate=0.03, num_leaves=15, min_data_in_leaf=80,
                bagging_fraction=0.8, bagging_freq=1, feature_fraction=0.8, lambda_l2=1.0,
                num_threads=threads, verbose=-1, seed=7)


def _fit(X, y, threads):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ds = lgb.Dataset(X, label=y, free_raw_data=True)
        return lgb.train(_params(threads), ds, num_boost_round=200)


def _predict(model, X) -> np.ndarray:
    return np.asarray(model.predict(X), dtype=float)


def walk_forward_probs(feats: pd.DataFrame, horizon: int = 12, target_pct: float = 0.6,
                       folds: int = 6, threads: int = 2) -> np.ndarray:
    """Out-of-sample probability for every row (NaN for the first chunk)."""
    probs = np.full(len(feats), np.nan)
    if not HAVE_LGB:
        log.warning("LightGBM not installed - ML filter disabled")
        return probs
    X = feats[FEATURE_NAMES].to_numpy(dtype=float)
    y = make_labels(feats, horizon, target_pct)
    start = WARMUP_BARS
    edges = np.linspace(start, len(feats), folds + 2).astype(int)
    for k in range(1, len(edges) - 1):
        lo, hi = edges[k], edges[k + 1]
        train_end = lo - horizon          # gap: labels must not overlap the test chunk
        tr = slice(start, train_end)
        mask = ~np.isnan(y[tr])
        if mask.sum() < 500:
            continue
        model = _fit(X[tr][mask], y[tr][mask], threads)
        probs[lo:hi] = _predict(model, X[lo:hi])
    return probs


def train_final(feats: pd.DataFrame, symbol: str, horizon: int, target_pct: float,
                threads: int = 2):
    """Trains on all history and saves the model for live paper trading."""
    if not HAVE_LGB:
        return None
    X = feats[FEATURE_NAMES].to_numpy(dtype=float)
    y = make_labels(feats, horizon, target_pct)
    mask = ~np.isnan(y)
    mask[:WARMUP_BARS] = False
    model = _fit(X[mask], y[mask], threads)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    with open(MODEL_DIR / f"{symbol.replace('/', '')}.pkl", "wb") as f:
        pickle.dump(model, f)
    return model


def load_model(symbol: str):
    p = MODEL_DIR / f"{symbol.replace('/', '')}.pkl"
    if not p.exists() or not HAVE_LGB:
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        log.warning("Could not load ML model for %s: %s", symbol, e)
        return None


def predict_latest(model, feats: pd.DataFrame) -> np.ndarray:
    """Probabilities for every row (used live; only the last rows matter)."""
    if model is None:
        return np.full(len(feats), np.nan)
    X = feats[FEATURE_NAMES].to_numpy(dtype=float)
    return _predict(model, X)


def auc(y: np.ndarray, p: np.ndarray) -> float:
    """Area under ROC curve: 0.5 = coin flip, higher = model has some skill."""
    m = ~(np.isnan(y) | np.isnan(p))
    y, p = y[m], p[m]
    pos, neg = (y == 1).sum(), (y == 0).sum()
    if pos == 0 or neg == 0:
        return float("nan")
    ranks = pd.Series(p).rank().to_numpy()
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))
