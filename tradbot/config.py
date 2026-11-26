"""Loads config.yaml and the .env secrets file.

Future-proof rules:
  * every setting has a safe default here, so an old or incomplete config.yaml works
  * a broken config.yaml (typo) never crashes the bot - it uses the defaults and
    shows a warning on the dashboard
  * impossible values (e.g. 500% per trade) are corrected to safe limits
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"          # downloaded candles, database, models, backups
STATE_DB = DATA_DIR / "tradbot.db"
TIMEFRAMES = ("15m", "30m", "1h", "2h", "4h", "1d")

DEFAULTS: dict = {
    "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT"],
    "scan_seconds": 30,
    "timeframe": "1h",
    "history_days": 1095,
    "mode": "paper",
    "eco_mode": True,             # lower CPU priority so your PC stays fast
    "paper": {"starting_balance": 10000, "fee_pct": 0.1, "slippage_pct": 0.05},
    "risk": {
        "max_position_pct": 20,
        "max_open_positions": 5,
        "max_new_per_candle": 3,  # coins move together: don't open many trades at once
        "daily_loss_limit_pct": 5,
        "kill_switch_drawdown_pct": 20,
        "min_trade_usdt": 10,
    },
    "lab": {
        "population": 300,
        "generations": 4,
        "min_trades": 30,
        "max_drawdown_pct": 20,
        "split": [0.6, 0.2, 0.2],
        "batch_size": 50,
    },
    "research": {
        "enabled": True,
        "round_population": 150,
        "round_generations": 2,
        "pause_seconds": 20,
        "refresh_minutes": 60,
        "ai_ideas_every_rounds": 10,
        "filter_every_hours": 6,
    },
    "pool": {
        "max_size": 300,
        "max_live": 10,
        "min_live": 6,
        "promote_after_trades": 5,
        "promote_min_profit_factor": 1.1,
        "retire_after_trades": 6,
        "retire_if_profit_factor_below": 0.9,
        "retire_if_return_below_pct": -3.0,
        "max_per_family": 12,
    },
    "news": {"enabled": True, "refresh_minutes": 30, "block_below": -0.5,
             "market_block_below": -0.6},
    "ml": {"enabled": True, "horizon_bars": 12, "target_pct": 0.6, "threads": 2},
    "brain": {
        "provider": "gemini",
        "gemini_model": "gemini-flash-latest",
        "claude_model": "claude-opus-5-5",
        "review_trades": True,
        "suggest_strategies": True,
        "daily_report": True,
        "use_news": True,
        "max_calls_per_hour": 20,
    },
    "learning": {                 # many small TESTING trades (same account as everything else)
        "enabled": True,
        "daily_target": 10,       # at least this many testing trades per day
        "daily_max": 40,
        "trade_usdt": 200,        # size of each testing trade
        "max_open": 20,
        "max_new_per_candle": 6,
    },
    "notify": {"trades": True, "lessons": False, "daily": True, "alerts": True,
               "daily_hour": 21, "testing_trades": False},     # local time of the daily summary
    "backup": {"keep_days": 7},
    "dashboard": {"host": "127.0.0.1", "port": 8050},
}

# (path, minimum, maximum): impossible values are pulled back into these limits
LIMITS = [
    (("scan_seconds",), 10, 3600),
    (("history_days",), 120, 3650),
    (("paper", "starting_balance"), 10, 1e12),
    (("paper", "fee_pct"), 0, 5),
    (("paper", "slippage_pct"), 0, 5),
    (("risk", "max_position_pct"), 1, 100),
    (("risk", "max_open_positions"), 1, 20),
    (("risk", "max_new_per_candle"), 1, 20),
    (("risk", "daily_loss_limit_pct"), 0.5, 50),
    (("risk", "kill_switch_drawdown_pct"), 1, 90),
    (("risk", "min_trade_usdt"), 1, 1e9),
    (("research", "round_population"), 10, 5000),
    (("research", "round_generations"), 1, 20),
    (("research", "pause_seconds"), 0, 86400),
    (("research", "refresh_minutes"), 5, 1440),
    (("pool", "max_size"), 10, 5000),
    (("pool", "max_live"), 1, 100),
    (("pool", "min_live"), 0, 100),
    (("ml", "threads"), 1, 64),
    (("news", "refresh_minutes"), 5, 1440),
    (("backup", "keep_days"), 1, 365),
    (("learning", "daily_target"), 0, 500),
    (("learning", "daily_max"), 1, 1000),
    (("learning", "trade_usdt"), 10, 1e9),
    (("learning", "max_open"), 1, 500),
    (("learning", "max_new_per_candle"), 1, 100),
    (("notify", "daily_hour"), 0, 23),
    (("dashboard", "port"), 1024, 65535),
]


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _get(cfg, path):
    for k in path:
        cfg = cfg[k]
    return cfg


def _set(cfg, path, value):
    for k in path[:-1]:
        cfg = cfg[k]
    cfg[path[-1]] = value


def validate(cfg: dict) -> list[str]:
    """Fixes impossible settings in place. Returns readable warnings."""
    warnings = []
    for path, lo, hi in LIMITS:
        name = ".".join(path)
        default = _get(DEFAULTS, path)
        try:
            value = float(_get(cfg, path))
        except (TypeError, ValueError, KeyError):
            _set(cfg, path, default)
            warnings.append(f"{name} was not a number - using {default}")
            continue
        fixed = min(max(value, lo), hi)
        if fixed != value:
            warnings.append(f"{name} = {value:g} is outside {lo:g}-{hi:g} - using {fixed:g}")
        _set(cfg, path, int(fixed) if isinstance(default, int) else fixed)

    syms = cfg.get("symbols")
    clean = []
    if isinstance(syms, list):
        for s in syms:
            s = str(s).strip().upper()
            if "/" in s and s not in clean:
                clean.append(s)
    if not clean:
        clean = list(DEFAULTS["symbols"])
        warnings.append("symbols list was empty or invalid - using the default coins")
    cfg["symbols"] = clean
    if cfg.get("timeframe") not in TIMEFRAMES:
        warnings.append(f"timeframe {cfg.get('timeframe')!r} is not supported - using 1h")
        cfg["timeframe"] = "1h"
    return warnings


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    user, warnings = {}, []
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            if not isinstance(user, dict):
                raise ValueError("the file is not a list of settings")
        except Exception as e:
            user = {}
            warnings.append(f"config.yaml could not be read ({str(e).splitlines()[0]}) - "
                            "using safe default settings. Fix the file or download it again.")
    cfg = _merge(DEFAULTS, user)
    if cfg.get("mode") != "paper":
        raise SystemExit(
            "Only mode: paper is supported in this version. "
            "Real-money trading is not built yet (by design)."
        )
    warnings += validate(cfg)
    cfg["_warnings"] = warnings
    for w in warnings:
        print("CONFIG WARNING:", w, flush=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_env()
    return cfg


# ---------------------------------------------------------------- secrets
def load_env() -> dict:
    """Reads KEY=VALUE lines from .env into os.environ (without overriding)."""
    values = read_env_file()
    for k, v in values.items():
        os.environ.setdefault(k, v)
    return values


def read_env_file() -> dict:
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def save_env_value(key: str, value: str) -> None:
    values = read_env_file()
    if value:
        values[key] = value
    else:
        values.pop(key, None)
    lines = ["# TradBot secrets - never share this file or upload it anywhere"]
    lines += [f"{k}={v}" for k, v in values.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)
