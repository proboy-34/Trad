"""Indicators ("the bot's eyes").

Every feature at row i only uses candles up to and including candle i,
so there is no peeking into the future. All features are "normalized"
(percentages / 0-100 scales), so the same rule works at $30k or $90k.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# name -> (plain-English label, display kind)
#   kind "pct"   : value is a fraction, shown as a percentage
#   kind "num"   : plain number
FEATURES: dict[str, tuple[str, str]] = {
    "rsi_7": ("RSI(7) - short-term overbought/oversold, 0-100", "num"),
    "rsi_14": ("RSI(14) - overbought/oversold, 0-100", "num"),
    "rsi_28": ("RSI(28) - slow overbought/oversold, 0-100", "num"),
    "stoch_14": ("Stochastic(14) - where price is in its 14-candle range, 0-100", "num"),
    "ret_1": ("price change over the last 1 candle", "pct"),
    "ret_4": ("price change over the last 4 candles", "pct"),
    "ret_12": ("price change over the last 12 candles", "pct"),
    "ret_24": ("price change over the last 24 candles", "pct"),
    "ret_72": ("price change over the last 72 candles", "pct"),
    "dist_ema_20": ("distance of price from its 20-candle average", "pct"),
    "dist_ema_50": ("distance of price from its 50-candle average", "pct"),
    "dist_ema_200": ("distance of price from its 200-candle average (long trend)", "pct"),
    "ema_20_50": ("20-candle average vs 50-candle average (trend direction)", "pct"),
    "ema_50_200": ("50-candle average vs 200-candle average (big trend direction)", "pct"),
    "macd_hist": ("MACD histogram (momentum), relative to price", "pct"),
    "atr_pct": ("ATR(14) - average candle size (volatility)", "pct"),
    "bb_pos": ("position inside Bollinger Bands (0 = lower band, 1 = upper band)", "num"),
    "bb_width": ("Bollinger Band width (how wide/quiet the market is)", "pct"),
    "vol_z": ("volume vs normal (z-score, above 2 = unusually high)", "num"),
    "range_pos_48": ("position inside the last 48-candle high/low range (0-1)", "num"),
    "volatility_24": ("volatility of the last 24 candles", "pct"),
}
FEATURE_NAMES = list(FEATURES)


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.fillna(100.0).where(loss.notna())


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """df has columns time, open, high, low, close, volume. Returns a new frame
    with the candles plus every feature column."""
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    out = df[["time", "open", "high", "low", "close", "volume"]].copy()

    for n in (7, 14, 28):
        out[f"rsi_{n}"] = _rsi(c, n)
    lo14, hi14 = l.rolling(14).min(), h.rolling(14).max()
    out["stoch_14"] = 100 * (c - lo14) / (hi14 - lo14).replace(0, np.nan)

    for n in (1, 4, 12, 24, 72):
        out[f"ret_{n}"] = c.pct_change(n)

    e20, e50, e200 = _ema(c, 20), _ema(c, 50), _ema(c, 200)
    out["dist_ema_20"] = c / e20 - 1
    out["dist_ema_50"] = c / e50 - 1
    out["dist_ema_200"] = c / e200 - 1
    out["ema_20_50"] = e20 / e50 - 1
    out["ema_50_200"] = e50 / e200 - 1

    macd = _ema(c, 12) - _ema(c, 26)
    out["macd_hist"] = (macd - _ema(macd, 9)) / c

    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["atr_pct"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / c

    ma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
    upper, lower = ma20 + 2 * sd20, ma20 - 2 * sd20
    out["bb_pos"] = (c - lower) / (upper - lower).replace(0, np.nan)
    out["bb_width"] = (upper - lower) / ma20

    lv = np.log1p(v)
    out["vol_z"] = (lv - lv.rolling(72).mean()) / lv.rolling(72).std().replace(0, np.nan)

    lo48, hi48 = l.rolling(48).min(), h.rolling(48).max()
    out["range_pos_48"] = (c - lo48) / (hi48 - lo48).replace(0, np.nan)
    out["volatility_24"] = c.pct_change().rolling(24).std()

    out = out.replace([np.inf, -np.inf], np.nan)
    return out


WARMUP_BARS = 210   # rows needed before every feature has a value


def fmt_value(name: str, value: float) -> str:
    kind = FEATURES.get(name, ("", "num"))[1]
    if kind == "pct":
        return f"{value * 100:+.2f}%"
    return f"{value:.2f}"
