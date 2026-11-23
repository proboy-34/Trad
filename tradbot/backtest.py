"""Backtester: "what would have happened if this strategy traded in the past?"

Honest-testing rules built in:
  * Signal is decided on a CLOSED candle; the buy happens at the NEXT candle's open.
  * Fees + slippage are paid on every buy and every sell.
  * If stop-loss and take-profit are both touched in the same candle, we
    assume the WORSE outcome (stop-loss).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .strategy import Strategy

BARS_PER_YEAR = {"15m": 35040, "30m": 17520, "1h": 8760, "2h": 4380, "4h": 2190, "1d": 365}


@dataclass
class Costs:
    fee_pct: float = 0.1
    slippage_pct: float = 0.05
    position_frac: float = 0.25

    @property
    def fee(self):
        return self.fee_pct / 100

    @property
    def slip(self):
        return self.slippage_pct / 100


@dataclass
class Result:
    trades: list = field(default_factory=list)
    equity: np.ndarray | None = None
    metrics: dict = field(default_factory=dict)


def metrics_from_equity(equity: np.ndarray, bars_per_year: int) -> dict:
    if equity is None or len(equity) < 2:
        return {"return_pct": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0}
    rets = np.diff(equity) / equity[:-1]
    peak = np.maximum.accumulate(equity)
    dd = (equity / peak - 1).min()
    sd = rets.std()
    sharpe = float(rets.mean() / sd * np.sqrt(bars_per_year)) if sd > 0 else 0.0
    return {
        "return_pct": float((equity[-1] / equity[0] - 1) * 100),
        "max_dd_pct": float(-dd * 100),
        "sharpe": sharpe,
    }


def buy_and_hold(close: np.ndarray, bars_per_year: int) -> dict:
    return metrics_from_equity(close / close[0], bars_per_year)


def run(strategy: Strategy, feats: pd.DataFrame, start: int = 0, end: int | None = None,
        costs: Costs | None = None, ml_prob: np.ndarray | None = None,
        timeframe: str = "1h", record_trades: bool = False) -> Result:
    """Backtests one strategy on rows [start, end) of the feature table."""
    costs = costs or Costs()
    n_all = len(feats)
    end = n_all if end is None else min(end, n_all)
    o = feats["open"].to_numpy()
    h = feats["high"].to_numpy()
    l = feats["low"].to_numpy()
    c = feats["close"].to_numpy()
    atr = feats["atr_pct"].to_numpy()
    entry_sig = strategy.entry_signal(feats, ml_prob)
    exit_sig = strategy.exit_signal(feats)
    fee, slip, frac = costs.fee, costs.slip, costs.position_frac

    equity = np.empty(end - start)
    eq = 1.0
    trades = []
    pnl_list = []
    i = start
    filled_to = start   # equity[] is filled up to (not including) this row
    sig_idx = np.flatnonzero(entry_sig[start:end - 1]) + start

    ptr = 0
    while ptr < len(sig_idx):
        i = sig_idx[ptr]
        if i < filled_to - 1 or not np.isfinite(atr[i]) or atr[i] <= 0:
            ptr += 1
            continue
        e = i + 1                                   # entry candle
        entry_px = o[e] * (1 + slip)
        stop_px = entry_px * (1 - strategy.stop_atr * atr[i])
        take_px = entry_px * (1 + strategy.take_atr * atr[i])
        last = min(i + strategy.max_hold, end - 1)  # last candle we may hold
        seg = slice(e, last + 1)
        hit_stop = l[seg] <= stop_px
        hit_take = h[seg] >= take_px
        hit_exit = exit_sig[seg].copy()
        hit_exit[-1] = False                        # exit signal needs a next candle
        first = lambda m: int(np.argmax(m)) if m.any() else 10**9
        js, jt, jx = first(hit_stop), first(hit_take), first(hit_exit)
        k = min(js, jt, jx)
        if k == 10**9:                              # time exit at close of `last`
            x = last
            exit_raw, reason = c[x], "time"
        else:
            x = e + k
            if js == k:                             # stop first (or same candle as take)
                exit_raw, reason = min(stop_px, o[x]) if x > e else stop_px, "stop"
            elif jt == k:
                exit_raw, reason = max(take_px, o[x]) if x > e else take_px, "take"
            else:                                   # signal exit -> next open
                x = x + 1
                exit_raw, reason = o[x], "signal"
        exit_px = exit_raw * (1 - slip)

        # equity before the trade stays flat
        equity[filled_to - start:e - start] = eq
        alloc = eq * frac
        units = alloc * (1 - fee) / entry_px
        cash = eq - alloc
        if x > e:
            equity[e - start:x - start] = cash + units * c[e:x]
        eq = cash + units * exit_px * (1 - fee)
        equity[x - start] = eq
        filled_to = x + 1
        trade_ret = (exit_px * (1 - fee)) / (entry_px / (1 - fee)) - 1
        pnl_list.append(trade_ret)
        if record_trades:
            trades.append({
                "entry_time": int(feats["time"].iat[e]), "exit_time": int(feats["time"].iat[x]),
                "entry": float(entry_px), "exit": float(exit_px),
                "return_pct": float(trade_ret * 100), "reason": reason,
            })
        ptr = int(np.searchsorted(sig_idx, x))    # next signal at/after exit candle
    equity[filled_to - start:] = eq

    bpy = BARS_PER_YEAR.get(timeframe, 8760)
    m = metrics_from_equity(equity, bpy)
    pnl = np.array(pnl_list)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    m.update({
        "trades": int(len(pnl)),
        "win_rate": float(len(wins) / len(pnl) * 100) if len(pnl) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else (99.0 if len(wins) else 0.0),
        "avg_trade_pct": float(pnl.mean() * 100) if len(pnl) else 0.0,
    })
    bh = buy_and_hold(c[start:end], bpy)
    m["buy_hold_return_pct"] = bh["return_pct"]
    m["buy_hold_max_dd_pct"] = bh["max_dd_pct"]
    m["buy_hold_sharpe"] = bh["sharpe"]
    return Result(trades=trades, equity=equity, metrics=m)
