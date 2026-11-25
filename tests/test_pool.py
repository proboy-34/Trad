"""Strategy pool: promotion to LIVE and retirement."""
import time

import pytest

import tradbot.lab as lab
from tradbot.config import DEFAULTS, _merge
from tradbot.pool import StrategyPool
from tradbot.strategy import Condition, Strategy


def entry(i, fwd=None, val_sharpe=1.0):
    s = Strategy(entry=[Condition("rsi_14", "<", 20 + i)])
    return {"strategy": s.to_dict(), "description": {}, "research": {"sharpe": val_sharpe},
            "validation": {"sharpe": val_sharpe}, "final": {}, "added": time.time() - 3600,
            "stage": "new", "forward": fwd}


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "STRATEGIES_FILE", tmp_path / "s.json")
    return StrategyPool(_merge(DEFAULTS, {"pool": {"min_live": 2, "max_live": 3}}))


def fwd(trades, ret, pf):
    return {"trades": trades, "return_pct": ret, "profit_factor": pf, "win_rate": 50, "max_dd_pct": 1}


def test_probation_when_nothing_is_proven(pool):
    pool.data["pool"] = [entry(i, val_sharpe=i) for i in range(5)]
    pool.review()
    live = pool.entries("live")
    assert len(live) == 2 and all(e["probation"] for e in live)
    assert {e["validation"]["sharpe"] for e in live} == {4, 3}      # best by validation


def test_proven_strategies_take_over_and_losers_retire(pool):
    pool.data["pool"] = [
        entry(1, fwd(8, 4.0, 1.8)),       # proven winner
        entry(2, fwd(6, 1.0, 1.2)),       # proven
        entry(3, fwd(2, 9.0, 3.0)),       # too few trades yet -> stays testing
        entry(4, fwd(7, -5.0, 0.5)),      # loser -> retired
        entry(5, fwd(0, 0, 0), val_sharpe=0.1),
    ]
    msgs = pool.review()
    live = pool.entries("live")
    assert len(live) == 2 and not any(e["probation"] for e in live)
    assert live[0]["forward"]["return_pct"] == 4.0
    assert len(pool.data["retired"]) == 1 and any("Retired" in m for m in msgs)
    assert len(pool.entries("new")) == 2


def test_paper_account_losses_also_retire(pool):
    e = entry(1, fwd(1, 0.5, 1.5))
    pool.data["pool"] = [e, entry(2)]
    pool.review({e["strategy"]["id"]: {"trades": 7, "profit_factor": 0.4}})
    assert e["strategy"]["id"] in {r["strategy"]["id"] for r in pool.data["retired"]}


def test_add_round_dedupes_and_caps(pool):
    pool.pc["max_size"] = 3
    r = {"passed": [entry(i) for i in range(5)], "stats": {"tested": 100}}
    assert pool.add_round(r) == 5
    assert len(pool.entries()) == 3
    assert pool.add_round(r) == 2      # the 3 kept ones are known; only the 2 dropped come back
    assert pool.counts()["tested_total"] >= 200


def test_probation_strategies_are_not_swapped_every_round(pool):
    pool.data["pool"] = [entry(i, val_sharpe=i) for i in range(3)]
    pool.review()
    first = {e["strategy"]["id"] for e in pool.entries("live")}
    pool.data["pool"] += [entry(10 + i, val_sharpe=50 + i) for i in range(3)]   # "better" new ones
    msgs = pool.review()
    assert {e["strategy"]["id"] for e in pool.entries("live")} == first
    assert not any("moved back" in m for m in msgs)
    # a strategy PROVEN in the live market test does take a place
    pool.data["pool"].append(entry(30, fwd(9, 3.0, 1.6)))
    pool.review()
    live = pool.entries("live")
    assert any(not e["probation"] for e in live) and len(live) == 2
