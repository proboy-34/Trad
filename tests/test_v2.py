"""Tests for the version 2 features."""
import io
import json
import sqlite3
import zipfile

import pytest

from tradbot import backtest, backup, data, installer, updater
from tradbot.config import DEFAULTS, _merge, load_config, validate
from tradbot.features import WARMUP_BARS, compute_features
from tradbot.learn import lesson_for
from tradbot.notify import Notifier
from tradbot.paper import PaperAccount
from tradbot.pool import StrategyPool
from tradbot.strategy import Condition, Strategy, StrategyGenerator, validate_strategy_dict


def flat_feats(n_extra=40):
    f = compute_features(data.synthetic_candles(WARMUP_BARS + n_extra, seed=4))
    f.loc[:, ["open", "high", "low", "close"]] = 100.0
    f["atr_pct"] = 0.01
    return f


# ------------------------------------------------------------ trailing stop
def test_trailing_stop_locks_in_profit():
    f = flat_feats()
    e = WARMUP_BARS + 1
    for k, px in enumerate([101, 102, 103, 104]):          # steady rise
        f.loc[e + 1 + k, ["open", "high", "low", "close"]] = [px - 0.5, px, px - 0.6, px]
    f.loc[e + 5, ["open", "high", "low", "close"]] = [103.5, 103.6, 99.0, 99.5]   # sharp drop
    base = dict(entry=[Condition("atr_pct", ">", 0)], stop_atr=3, take_atr=20, max_hold=30)
    costs = backtest.Costs(0.1, 0.05, 1)
    plain = backtest.run(Strategy(**base), f, WARMUP_BARS, None, costs, record_trades=True)
    trail = backtest.run(Strategy(**base, trail_atr=1.5), f, WARMUP_BARS, None, costs,
                         record_trades=True)
    t = trail.trades[0]
    assert t["reason"] == "trail"
    # stop = highest previous high (104) - 1.5 ATR of the fill price (100.05) -> profit locked
    assert t["exit"] == pytest.approx((104 - 1.5 * 0.01 * 100.05) * (1 - 0.0005))
    assert t["return_pct"] > 1.5
    assert plain.trades[0]["return_pct"] < t["return_pct"]


def test_trailing_stop_is_pessimistic_on_the_new_high_candle():
    """A new high only moves the stop from the NEXT candle (we can't know the order inside a candle)."""
    f = flat_feats()
    e = WARMUP_BARS + 1
    f.loc[e + 1, ["open", "high", "low", "close"]] = [100, 110, 97.5, 99]   # spike up AND down
    s = Strategy(entry=[Condition("atr_pct", ">", 0)], stop_atr=2.0, take_atr=50, max_hold=30,
                 trail_atr=1.0)
    t = backtest.run(s, f, WARMUP_BARS, None, backtest.Costs(0, 0, 1), record_trades=True).trades[0]
    assert t["exit"] == pytest.approx(99.0)       # trail from entry (100 - 1) - not from 110


def test_trailing_gene_roundtrip_and_ids_stay_stable():
    old = Strategy(entry=[Condition("rsi_14", "<", 30)])
    assert "trail_atr" not in json.dumps({k: v for k, v in old.to_dict().items() if v is not None})
    s = Strategy(entry=[Condition("rsi_14", "<", 30)], trail_atr=2.0)
    assert s.id != old.id and Strategy.from_dict(s.to_dict()).trail_atr == 2.0
    assert "trailing stop" in s.describe()["sell"]
    assert validate_strategy_dict({"entry": [{"feature": "rsi_14", "op": "<", "value": 30}],
                                   "trail_atr": 99}).trail_atr == 5.0
    gen = StrategyGenerator(compute_features(data.synthetic_candles(1500, seed=2)), seed=3)
    assert any(gen.random().trail_atr for _ in range(60))


def test_trailing_lesson():
    t = {"entry_price": 100, "exit_price": 102, "low_seen": 99, "high_seen": 104, "stop": 102,
         "take": 108, "reason": "trailing stop", "pnl": 2}
    assert lesson_for(t)[1] == ["trail_profit"]


# --------------------------------------------------------------- the pool
def entry(i, features=("rsi_14",), final=None):
    s = Strategy(entry=[Condition(f, "<", 20 + i) for f in features])
    return {"strategy": s.to_dict(), "description": {}, "research": {"sharpe": 1},
            "validation": {"sharpe": 1 + i / 100}, "final": final or {}, "added": 0}


def test_pool_limits_one_family(tmp_path, monkeypatch):
    import tradbot.lab as lab
    monkeypatch.setattr(lab, "STRATEGIES_FILE", tmp_path / "s.json")
    pool = StrategyPool(_merge(DEFAULTS, {"pool": {"max_per_family": 3}}))
    same = [entry(i) for i in range(6)]
    other = [entry(50, ("vol_z",))]
    assert pool.add_round({"passed": same + other, "stats": {"tested": 10}}) == 4
    fams = [tuple(c["feature"] for c in e["strategy"]["entry"]) for e in pool.entries()]
    assert fams.count(("rsi_14",)) == 3 and ("vol_z",) in fams


def test_probation_prefers_final_test_winners(tmp_path, monkeypatch):
    import tradbot.lab as lab
    monkeypatch.setattr(lab, "STRATEGIES_FILE", tmp_path / "s.json")
    pool = StrategyPool(_merge(DEFAULTS, {"pool": {"min_live": 1}}))
    lucky = entry(90, ("rsi_14",), final={"return_pct": -2, "sharpe": -1})        # best validation
    solid = entry(1, ("vol_z",), final={"return_pct": 1, "sharpe": 0.5})
    pool.data["pool"] = [dict(lucky, stage="new"), dict(solid, stage="new")]
    pool.review()
    assert [e["strategy"]["id"] for e in pool.entries("live")] == [solid["strategy"]["id"]]


# ---------------------------------------------------------- config safety
def test_broken_config_uses_safe_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("risk: [this is: not valid\n")
    cfg = load_config(p)
    assert cfg["risk"]["max_position_pct"] == 20 and cfg["_warnings"]


def test_impossible_values_are_corrected():
    cfg = _merge(DEFAULTS, {"risk": {"max_position_pct": 500, "kill_switch_drawdown_pct": "abc"},
                            "symbols": [], "timeframe": "7m", "scan_seconds": 1})
    warnings = validate(cfg)
    assert cfg["risk"]["max_position_pct"] == 100
    assert cfg["risk"]["kill_switch_drawdown_pct"] == 20
    assert cfg["symbols"] == DEFAULTS["symbols"] and cfg["timeframe"] == "1h"
    assert cfg["scan_seconds"] == 10 and len(warnings) == 5


# ------------------------------------------------------------- installer
def test_installer_reads_requirements_and_import_names():
    core = installer.read_requirements(installer.CORE_FILE)
    assert "PyYAML>=6.0" in core and all(" " not in r for r in core)
    assert installer.import_name("PyYAML>=6.0") == "yaml"
    assert installer.import_name("lightgbm>=4.3") == "lightgbm"


def test_optional_failure_does_not_block(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(installer, "has_module", lambda name: name != "lightgbm")
    calls = []
    monkeypatch.setattr(installer, "_pip", lambda *a: calls.append(a) or "lightgbm" not in a[-1])
    res = installer.install(force=True, out=lambda *_: None)
    assert any("lightgbm" in r for r in res["failed"])
    assert not installer.plan()["needed"]          # failed optional is not retried every start


# ---------------------------------------------------------------- backups
def make_db(path):
    acc = PaperAccount(DEFAULTS, db_path=path)
    acc.update_account(cash=1234.5)
    return acc


def test_backup_restore_roundtrip(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_db(src / "tradbot.db").db.close()
    (src / "strategies.json").write_text('{"pool": [1]}')
    (src / "models").mkdir()
    (src / "models" / "BTCUSDT.pkl").write_bytes(b"m")
    blob = backup.build_zip(src, src / "tradbot.db")
    dst = tmp_path / "dst"
    dst.mkdir()
    acc = make_db(dst / "tradbot.db")
    acc.update_account(cash=1.0)
    acc.db.close()                  # like the real start-up: restore happens before the DB is opened
    backup.stage_restore(blob, dst)
    assert backup.apply_pending(dst, dst / "tradbot.db")
    con = sqlite3.connect(str(dst / "tradbot.db"))
    assert con.execute("SELECT cash FROM account").fetchone()[0] == 1234.5
    con.close()
    assert (dst / "strategies.json").read_text() == '{"pool": [1]}'
    assert list((dst / "backups").glob("before-restore-*.zip"))          # undo is possible


def test_restore_rejects_bad_files(tmp_path):
    with pytest.raises(ValueError):
        backup.stage_restore(b"not a zip", tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(ValueError):
        backup.stage_restore(buf.getvalue(), tmp_path)


def test_daily_backup_keeps_last_days(tmp_path):
    make_db(tmp_path / "tradbot.db").db.close()
    out = tmp_path / "backups"
    out.mkdir()
    for d in range(1, 10):
        (out / f"tradbot-2020-01-{d:02d}.zip").write_bytes(b"x")
    backup.daily(3, tmp_path, tmp_path / "tradbot.db")
    assert len(list(out.glob("tradbot-????-??-??.zip"))) == 3


# ---------------------------------------------------------------- updater
def update_zip(version="9.9", extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        root = "Trad-branch/"
        z.writestr(root + "main.py", "print('new')")
        z.writestr(root + "tradbot/__init__.py", f'__version__ = "{version}"\n')
        z.writestr(root + "tradbot/engine.py", "# new engine")
        z.writestr(root + "config.yaml", "symbols: [BTC/USDT]\n")
        z.writestr(root + "TradBot.bat", "@echo new")
        z.writestr(root + "data/tradbot.db", "SHOULD NOT BE WRITTEN")
        z.writestr(root + ".env", "SECRET=stolen")
        for name, content in (extra or {}).items():
            z.writestr(root + name, content)
    return buf.getvalue()


def test_update_replaces_code_only(tmp_path):
    root = tmp_path
    (root / "tradbot").mkdir()
    (root / "tradbot" / "__init__.py").write_text('__version__ = "2.0"\n')
    (root / "tradbot" / "old_module.py").write_text("# removed in new version")
    (root / "config.yaml").write_text("symbols: [ETH/USDT]  # my settings\n")
    (root / "TradBot.bat").write_text("@echo old")
    (root / ".env").write_text("GEMINI_API_KEY=mine")
    (root / "data").mkdir()
    (root / "data" / "tradbot.db").write_text("my data")
    res = updater.apply(update_zip(), root)
    assert res["to"] == "9.9"
    assert (root / "main.py").read_text() == "print('new')"
    assert (root / "tradbot" / "engine.py").read_text() == "# new engine"
    assert not (root / "tradbot" / "old_module.py").exists()
    assert (root / ".env").read_text() == "GEMINI_API_KEY=mine"            # secrets untouched
    assert (root / "data" / "tradbot.db").read_text() == "my data"         # data untouched
    assert "my settings" in (root / "config.yaml.bak").read_text() and res["config_backup"]
    assert (root / "TradBot.bat").read_text() == "@echo old"               # swapped on restart
    assert (root / "TradBot.bat.new").read_text() == "@echo new"
    assert list((root / "data" / "backups").glob("code-*.zip"))           # undo is possible


def test_update_rejects_other_zips_and_path_tricks(tmp_path):
    with pytest.raises(ValueError):
        updater.apply(b"junk", tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/readme.txt", "not tradbot")
    with pytest.raises(ValueError):
        updater.apply(buf.getvalue(), tmp_path)
    (tmp_path / "tradbot").mkdir()
    updater.apply(update_zip(extra={"../evil.py": "x"}), tmp_path / "")
    assert not (tmp_path.parent / "evil.py").exists()


# -------------------------------------------------------------- telegram
class Resp:
    def __init__(self, payload, code=200):
        self.status_code = code
        self._p = payload
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._p


def test_telegram_connect_finds_chat(monkeypatch):
    import tradbot.notify as nmod
    saved = {}
    monkeypatch.setattr(nmod, "save_env_value", lambda k, v: (saved.__setitem__(k, v),
                                                               monkeypatch.setenv(k, v) if v else None))
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    sent = []

    def post(url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        if method == "getMe":
            return Resp({"ok": True, "result": {"username": "my_trad_bot"}})
        if method == "getUpdates":
            return Resp({"ok": True, "result": [{"message": {"chat": {"id": 777, "type": "private"}}}]})
        sent.append(json)
        return Resp({"ok": True, "result": {}})

    monkeypatch.setattr(nmod.requests, "post", post)
    n = Notifier(DEFAULTS)
    ok, msg = n.connect("123:ABC")
    assert ok and saved["TELEGRAM_CHAT_ID"] == "777" and sent[0]["chat_id"] == 777
    assert n.enabled()
    assert not n.send("lesson text", "lessons")          # lessons are off by default
