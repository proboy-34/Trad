"""Risk Manager - the "boss" that can say NO to any trade.

Rules (from config.yaml -> risk):
  * max % of balance per trade
  * max number of open trades
  * daily loss limit  -> no new trades until the next UTC day
  * kill switch       -> after a big drop from the peak, ALL new trading stops
                         until you reset it yourself in the dashboard
"""
from __future__ import annotations

from .paper import PaperAccount, utc_day


class RiskManager:
    def __init__(self, cfg: dict, account: PaperAccount):
        self.r = cfg["risk"]
        self.acc = account

    def update(self, equity: float) -> dict:
        """Call every check: tracks the day's start, the peak and the kill switch."""
        a = self.acc.account()
        changes = {}
        today = utc_day()
        if a["day"] != today:
            changes.update(day=today, day_start_equity=equity)
            a["day_start_equity"] = equity
        if equity > a["peak_equity"]:
            changes["peak_equity"] = equity
            a["peak_equity"] = equity
        dd = (1 - equity / a["peak_equity"]) * 100 if a["peak_equity"] else 0
        if dd >= self.r["kill_switch_drawdown_pct"] and not a["kill_switch"]:
            changes["kill_switch"] = 1
            self.acc.message("risk", f"KILL SWITCH ON: balance fell {dd:.1f}% from its peak. "
                                     "All new trades stopped. Review, then reset in Settings.")
        self.acc.update_account(**changes)
        day_pnl = (equity / a["day_start_equity"] - 1) * 100 if a["day_start_equity"] else 0
        return {"drawdown_pct": dd, "day_pnl_pct": day_pnl}

    def can_open(self, equity: float, symbol: str, strategy_id: str) -> tuple[bool, str, float]:
        """Returns (allowed, reason, trade size in USDT)."""
        a = self.acc.account()
        if a["paused"]:
            return False, "bot is paused", 0.0
        if a["kill_switch"]:
            return False, "kill switch is ON", 0.0
        day_pnl = (equity / a["day_start_equity"] - 1) * 100 if a["day_start_equity"] else 0
        if day_pnl <= -self.r["daily_loss_limit_pct"]:
            return False, f"daily loss limit reached ({day_pnl:.1f}%)", 0.0
        positions = self.acc.positions()
        if len(positions) >= self.r["max_open_positions"]:
            return False, "max open trades reached", 0.0
        if any(p["symbol"] == symbol and p["strategy_id"] == strategy_id for p in positions):
            return False, "this strategy already holds this coin", 0.0
        size = min(equity * self.r["max_position_pct"] / 100, a["cash"])
        if size < self.r["min_trade_usdt"]:
            return False, "not enough free balance", 0.0
        return True, "ok", size
