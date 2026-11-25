"""Safety tests: run with  python -m pytest -q"""
import json

import numpy as np
import pandas as pd
import pytest

from tradbot import backtest, data, ml
from tradbot.config import DEFAULTS, _merge
from tradbot.features import FEATURE_NAMES, WARMUP_BARS, compute_features
from tradbot.lab import Lab, passes_unseen_test
from tradbot.paper import PaperAccount
from tradbot.risk import RiskManager
from tradbot.strategy import Condition, Strategy, StrategyGenerator, validate_strategy_dict


@pytest.fixture(scope="module")
def feats():
    return compute_features(data.synthetic_candles(6000, seed=3))


def cfg(**over):
    return _merge(DEFAULTS, over)


# ----------------------------------------------------------- no peeking
def test_features_do_not_use_future_data():
    """Changing FUTURE candles must not change PAST feature values."""
    df = data.synthetic_candles(1500, seed=1)
    a = compute_features(df)
    df2 = df.copy()
    df2.loc[1000:, ["open", "high", "low", "close"]] *= 1.5
    df2.loc[1000:, "volume"] *= 3
    b = compute_features(df2)
    pd.testing.assert_frame_equal(a.iloc[:1000][FEATURE_NAMES], b.iloc[:1000][FEATURE_NAMES])


def test_backtest_does_not_use_future_data(feats):
    s = Strategy(entry=[Condition("rsi_14", "<", 40)], stop_atr=2, take_atr=3, max_hold=24)
    full = backtest.run(s, feats, WARMUP_BARS, 4000, record_trades=True)
    changed = feats.copy()
    changed.loc[4000:, ["open", "high", "low", "close"]] *= 0.5
    again = backtest.run(s, changed, WARMUP_BARS, 4000, record_trades=True)
    assert full.trades == again.trades


def test_walk_forward_ml_does_not_peek(feats):
    if not ml.HAVE_LGB:
        pytest.skip("lightgbm not installed")
    p1 = ml.walk_forward_probs(feats, folds=3, threads=1)
    changed = feats.copy()
    cut = len(feats) - 500
    changed.loc[cut:, "close"] *= 2
    p2 = ml.walk_forward_probs(changed, folds=3, threads=1)
    # rows well before the last chunk were predicted by models that never saw the change
    first_chunk_end = WARMUP_BARS + (len(feats) - WARMUP_BARS) * 2 // 5
    np.testing.assert_allclose(p1[:first_chunk_end], p2[:first_chunk_end], equal_nan=True)


# ------------------------------------------------------------ backtest
def test_entry_is_next_candle_open_and_fees_are_paid():
    df = data.synthetic_candles(600, seed=2)
    f = compute_features(df)
    s = Strategy(entry=[Condition("rsi_14", ">", -1)], stop_atr=50, take_atr=50, max_hold=5)
    r = backtest.run(s, f, WARMUP_BARS, None, backtest.Costs(0.1, 0.05, 1.0), record_trades=True)
    t = r.trades[0]
    i = f.index[f["time"] == t["entry_time"]][0]
    assert i == WARMUP_BARS + 1                       # signal at WARMUP, buy at next candle
    assert t["entry"] == pytest.approx(f["open"].iat[i] * 1.0005)
    # flat price would still lose: 2 fees + 2 slippages
    flat = f.copy()
    for c in ("open", "high", "low", "close"):
        flat[c] = 100.0
    r2 = backtest.run(s, flat, WARMUP_BARS, None, backtest.Costs(0.1, 0.05, 1.0))
    assert r2.metrics["avg_trade_pct"] == pytest.approx(-0.3, abs=0.01)


def test_stop_beats_take_in_same_candle():
    """If one candle touches both stop and target we assume the WORSE case."""
    n = WARMUP_BARS + 20
    base = data.synthetic_candles(n, seed=4)
    f = compute_features(base)
    f.loc[:, ["open", "high", "low", "close"]] = 100.0
    f["atr_pct"] = 0.01
    f.loc[WARMUP_BARS + 2, "high"] = 120.0
    f.loc[WARMUP_BARS + 2, "low"] = 80.0
    s = Strategy(entry=[Condition("atr_pct", ">", 0)], stop_atr=1, take_atr=1, max_hold=10)
    r = backtest.run(s, f, WARMUP_BARS, WARMUP_BARS + 15, backtest.Costs(0, 0, 1), record_trades=True)
    assert r.trades[0]["reason"] == "stop"


# ------------------------------------------------------------ strategy
def test_strategy_roundtrip_and_ai_validation(feats):
    g = StrategyGenerator(feats, seed=1)
    for _ in range(20):
        s = g.random()
        s2 = Strategy.from_dict(json.loads(json.dumps(s.to_dict())))
        assert s2.id == s.id
        assert "BUY when" in s.describe()["buy"]
    assert validate_strategy_dict({"entry": [{"feature": "hack", "op": "<", "value": 1}]}) is None
    ok = validate_strategy_dict({"entry": [{"feature": "rsi_14", "op": "<", "value": "30"}],
                                 "stop_atr": 99, "max_hold": 5000})
    assert ok and ok.stop_atr == 5.0 and ok.max_hold == 240 and ok.origin == "ai"


def test_unseen_test_rules():
    good = {"trades": 20, "sharpe": 0.8, "profit_factor": 1.3, "return_pct": 2.0,
            "buy_hold_return_pct": -10, "max_dd_pct": 5, "buy_hold_max_dd_pct": 30}
    assert passes_unseen_test(good, 10, 20)
    assert not passes_unseen_test({**good, "trades": 3}, 10, 20)
    assert not passes_unseen_test({**good, "return_pct": -15, "sharpe": -1}, 10, 20)
    assert not passes_unseen_test({**good, "max_dd_pct": 35}, 10, 20)
    assert not passes_unseen_test({**good, "return_pct": -0.5}, 10, 20)   # small loss = fail


def test_lab_round_and_pool_end_to_end(tmp_path, monkeypatch):
    import tradbot.lab as lab
    from tradbot.pool import StrategyPool
    monkeypatch.setattr(lab, "STRATEGIES_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ml, "MODEL_DIR", tmp_path / "models")
    c = cfg(lab={"population": 40, "generations": 2}, ml={"enabled": False})
    ds = {"A/USDT": compute_features(data.synthetic_candles(5000, seed=5)),
          "B/USDT": compute_features(data.synthetic_candles(5000, seed=6, start_price=2000))}
    res = Lab(c, ds, seed=1).run()
    assert res["stats"]["tested"] >= 40
    pool = StrategyPool(c)
    fake = res["passed"] or [{"strategy": Strategy(entry=[Condition("rsi_14", "<", 40)]).to_dict(),
                              "description": {}, "research": {"sharpe": 1}, "validation": {"sharpe": 1},
                              "final": {}, "added": 0}]
    pool.add_round({"passed": fake, "stats": res["stats"]})
    pool.forward_test(ds, {}, "1h")
    pool.review()
    saved = json.loads((tmp_path / "s.json").read_text())
    assert saved["pool"] and saved["active"]          # something is live (probation)
    assert all(e["stage"] == "live" for e in saved["active"])


# ---------------------------------------------------------- paper+risk
def test_paper_account_money_math(tmp_path):
    acc = PaperAccount(cfg(), db_path=tmp_path / "t.db")
    s = {"id": "SX"}
    p = acc.open_position("BTC/USDT", s, 100.0, 1000.0, 90, 120, 1e12)
    assert acc.account()["cash"] == pytest.approx(9000)
    t = acc.close_position(p["id"], 100.0, "test")
    # 0.1% fee + 0.05% slippage on each side
    assert t["pnl"] == pytest.approx(1000 * (0.999 / 1.0005) * 0.9995 * 0.999 - 1000, rel=1e-9)
    assert acc.account()["cash"] == pytest.approx(9000 + 1000 + t["pnl"])
    assert acc.positions() == []


def test_risk_rules(tmp_path):
    c = cfg()
    acc = PaperAccount(c, db_path=tmp_path / "r.db")
    risk = RiskManager(c, acc)
    risk.update(10000)
    ok, why, size = risk.can_open(10000, "BTC/USDT", "S1")
    assert ok and size == pytest.approx(2500)            # 25% max per trade
    acc.open_position("BTC/USDT", {"id": "S1"}, 100, size, 90, 110, 1e12)
    ok, why, _ = risk.can_open(10000, "BTC/USDT", "S1")
    assert not ok and "already" in why                   # no doubling up
    ok, why, _ = risk.can_open(9400, "ETH/USDT", "S2")
    assert not ok and "daily" in why                     # -6% today -> stop
    risk.update(7900)                                    # -21% from peak
    assert acc.account()["kill_switch"] == 1
    ok, why, _ = risk.can_open(10000, "ETH/USDT", "S2")
    assert not ok and "kill" in why


def test_paper_only_mode():
    from tradbot import config as config_mod
    import yaml
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.yaml"
        p.write_text(yaml.safe_dump({"mode": "live"}))
        with pytest.raises(SystemExit):
            config_mod.load_config(p)
