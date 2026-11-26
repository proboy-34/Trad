"""Learning account: many small real paper trades from strategies still being tested."""
import time

import pytest

import tradbot.engine as eng
import tradbot.lab as lab
import tradbot.paper as paper
from tradbot import data
from tradbot.config import DEFAULTS, _merge
from tradbot.pool import StrategyPool
from tradbot.strategy import Condition, Strategy


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "STATE_DB", tmp_path / "main.db")
    monkeypatch.setattr(eng, "DATA_DIR", tmp_path)
    monkeypatch.setattr(lab, "STRATEGIES_FILE", tmp_path / "s.json")
    cfg = _merge(DEFAULTS, {"symbols": ["AAA/USDT", "BBB/USDT"], "research": {"enabled": False},
                            "ml": {"enabled": False}, "news": {"enabled": False},
                            "brain": {"provider": "none"},
                            "learning": {"max_new_per_candle": 3, "trade_usdt": 200}})
    # a pool of 8 testing strategies that all want to buy right now
    pool = StrategyPool(cfg)
    passed = [{"strategy": Strategy(entry=[Condition("atr_pct", ">", -1 - i)]).to_dict(),
               "description": {}, "research": {"sharpe": 1}, "validation": {"sharpe": 1},
               "final": {}, "added": time.time()} for i in range(8)]
    pool.add_round({"passed": passed, "stats": {"tested": 8}})
    e = eng.Engine(cfg)
    e.pool.data["pool"] = pool.data["pool"]
    for i, sym in enumerate(cfg["symbols"]):
        df = data.synthetic_candles(600, seed=i)
        e.candles[sym] = df
        e.prices[sym] = float(df["close"].iat[-1])
    return e


def test_learning_trades_open_with_limits(engine):
    engine.hourly_cycle()
    pos = engine.learn_acc.positions()
    assert len(pos) == 3                                   # max 3 new per candle
    assert all(p["cost"] == pytest.approx(200) for p in pos)
    assert engine.account.positions() == []                # main account is separate
    assert engine.learning_summary()["today"] == 3
    # same candle again (e.g. restart): no double buys
    engine.hourly_cycle()
    assert len(engine.learn_acc.positions()) == 3


def test_learning_trades_close_and_teach(engine):
    engine.hourly_cycle()
    p = engine.learn_acc.positions()[0]
    engine.prices[p["symbol"]] = p["stop"] * 0.99          # price falls through the stop
    engine.fast_check()
    t = [t for t in engine.learn_acc.trades() if t["strategy_id"] == p["strategy_id"]][0]
    assert t["reason"] == "stop-loss" and t["lesson"]
    assert engine.account.trades() == []                   # main account untouched
    assert any(a["kind"] == "learn" and "Lesson" in a["msg"] for a in engine.activity)


def test_learning_results_promote_and_retire(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "STRATEGIES_FILE", tmp_path / "s.json")
    pool = StrategyPool(_merge(DEFAULTS, {"pool": {"min_live": 0}}))
    good, bad = (Strategy(entry=[Condition("rsi_14", "<", v)]) for v in (30, 40))
    pool.data["pool"] = [{"strategy": s.to_dict(), "description": {}, "research": {"sharpe": 1},
                          "validation": {"sharpe": 1}, "final": {}, "added": 0, "stage": "new"}
                         for s in (good, bad)]
    msgs = pool.review({}, {good.id: {"trades": 6, "pnl": 40.0, "profit_factor": 2.0},
                            bad.id: {"trades": 7, "pnl": -30.0, "profit_factor": 0.4}})
    assert [e["strategy"]["id"] for e in pool.entries("live")] == [good.id]
    assert any("won in learning trades" in m for m in msgs)
    assert any("lost in learning trades" in m for m in msgs)
