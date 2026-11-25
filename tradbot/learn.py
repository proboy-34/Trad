"""Self-learning from the bot's own trades.

After every trade the bot writes a LESSON: what happened and what it means
(e.g. "bought into a falling move" or "was up 3% but gave it all back").
From all lessons it learns:
  * position size per coin / per strategy (losing ones trade smaller)
  * which mistakes repeat most (shown on the dashboard, sent to the AI coach)
"""
from __future__ import annotations

from collections import Counter

LABELS = {
    "win": "target hit (plan worked)",
    "gave_back": "was in profit, then reversed to the stop",
    "bad_entry": "fell right after buying (bad entry timing)",
    "stopped": "normal loss, stop-loss limited the damage",
    "slow_win": "small win after the time limit",
    "slow_loss": "idea didn't play out in time",
    "exit_rule": "closed early by the exit rule",
    "news_negative": "news mood was negative when buying",
    "protected": "break-even stop saved a loss",
    "trail_profit": "trailing stop locked in profit",
    "trail_loss": "trailing stop too tight",
}


def lesson_for(t: dict) -> tuple[str, list[str]]:
    """t: a closed trade with entry_price, exit_price, pnl, reason, stop, take,
    low_seen, high_seen (and optionally news_score)."""
    entry = t["entry_price"]
    low = min(t.get("low_seen") or entry, t["exit_price"])
    high = max(t.get("high_seen") or entry, t["exit_price"])
    mae = (low / entry - 1) * 100          # worst point during the trade (%)
    mfe = (high / entry - 1) * 100         # best point during the trade (%)
    take_pct = (t["take"] / entry - 1) * 100 if t.get("take") else 0
    stop_pct = (1 - t["stop"] / entry) * 100 if t.get("stop") else 0
    reason = t["reason"]
    tags: list[str] = []
    if reason == "take-profit":
        tags.append("win")
        text = f"Plan worked: price reached the target (+{mfe:.2f}%)."
    elif reason == "stop-loss":
        if take_pct and mfe >= 0.5 * take_pct:
            tags.append("gave_back")
            text = (f"Was up +{mfe:.2f}% (more than half way to the target) but reversed and hit "
                    "the stop. Lesson: profits can vanish - this strategy's target may be too far.")
        elif stop_pct and mfe < 0.3 * stop_pct:
            tags.append("bad_entry")
            text = (f"Price fell almost right after buying (best point only +{mfe:.2f}%). "
                    "Lesson: the signal bought into a falling move - bad entry timing.")
        else:
            tags.append("stopped")
            text = f"Normal loss: the idea didn't work, the stop-loss limited the damage to {mae:.2f}%."
    elif reason == "trailing stop":
        if t["pnl"] > 0:
            tags.append("trail_profit")
            text = (f"Locked in profit: the trailing stop followed the price up (best +{mfe:.2f}%) "
                    "and sold when it turned down.")
        else:
            tags.append("trail_loss")
            text = (f"The trailing stop sold after a small rise (best +{mfe:.2f}%) - "
                    "this strategy's trailing stop may be too tight.")
    elif reason == "break-even":
        tags.append("protected")
        text = (f"Protected: was up +{mfe:.2f}%, came back, and the break-even stop closed it "
                "at about zero instead of a loss.")
    elif reason == "time limit":
        if t["pnl"] > 0:
            tags.append("slow_win")
            text = f"Small win after the time limit (best point +{mfe:.2f}%)."
        else:
            tags.append("slow_loss")
            text = (f"The idea didn't play out in time (best +{mfe:.2f}%, worst {mae:.2f}%). "
                    "Lesson: the move this strategy expects did not come.")
    else:
        tags.append("exit_rule")
        text = f"Closed early by the strategy's exit rule ({'profit' if t['pnl'] > 0 else 'loss'})."
    news = t.get("news_score")
    if news is not None and news < -0.2:
        tags.append("news_negative")
        text += " Note: news mood was negative when it bought."
    return text, tags


def _pf(rows) -> float:
    gains = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    losses = -sum(r["pnl"] for r in rows if r["pnl"] <= 0)
    return gains / losses if losses else (99.0 if gains else 0.0)


def _mult(rows) -> float:
    if len(rows) < 4:
        return 1.0                    # not enough evidence yet
    pf = _pf(rows)
    return 1.0 if pf >= 1.0 else 0.75 if pf >= 0.7 else 0.5


def learn(trades: list[dict], last_n: int = 12) -> dict:
    """trades: newest first. Returns size multipliers + readable insights."""
    by_coin, by_strat = {}, {}
    for t in trades:
        by_coin.setdefault(t["symbol"], []).append(t)
        by_strat.setdefault(t["strategy_id"], []).append(t)
    coins = {k: {"mult": _mult(v[:last_n]), "trades": len(v[:last_n]), "pf": round(_pf(v[:last_n]), 2)}
             for k, v in by_coin.items()}
    strats = {k: {"mult": _mult(v[:last_n]), "trades": len(v[:last_n]), "pf": round(_pf(v[:last_n]), 2)}
              for k, v in by_strat.items()}
    recent = trades[:50]
    losses = [t for t in recent if t["pnl"] <= 0]
    mistakes = Counter(tag for t in losses for tag in (t.get("tags") or "").split(",") if tag)
    insights = []
    if recent:
        wins = sum(t["pnl"] > 0 for t in recent)
        insights.append(f"Last {len(recent)} trades: {wins} wins, {len(recent) - wins} losses, "
                        f"profit factor {_pf(recent):.2f}.")
    if mistakes:
        tag, n = mistakes.most_common(1)[0]
        insights.append(f"Most common mistake: {LABELS.get(tag, tag)} ({n} of {len(losses)} losses).")
    for k, v in coins.items():
        if v["mult"] < 1:
            insights.append(f"{k} keeps losing (profit factor {v['pf']}), so trades on it are now "
                            f"{int(v['mult'] * 100)}% size.")
    for k, v in strats.items():
        if v["mult"] < 1:
            insights.append(f"Strategy {k} is losing (profit factor {v['pf']}), so its trades are now "
                            f"{int(v['mult'] * 100)}% size.")
    if not insights:
        insights.append("No finished trades yet - lessons appear after the first trades close.")
    return {"coins": coins, "strategies": strats, "mistakes": dict(mistakes), "insights": insights,
            "labels": LABELS}


def size_multiplier(adj: dict, symbol: str, strategy_id: str) -> float:
    return min(adj.get("coins", {}).get(symbol, {}).get("mult", 1.0),
               adj.get("strategies", {}).get(strategy_id, {}).get("mult", 1.0))
