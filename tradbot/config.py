"""Loads config.yaml and the .env secrets file."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"          # downloaded candles, database, models
STATE_DB = DATA_DIR / "tradbot.db"

DEFAULTS: dict = {
    "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT"],
    "scan_seconds": 30,
    "timeframe": "1h",
    "history_days": 1095,
    "mode": "paper",
    "paper": {"starting_balance": 10000, "fee_pct": 0.1, "slippage_pct": 0.05},
    "risk": {
        "max_position_pct": 25,
        "max_open_positions": 3,
        "daily_loss_limit_pct": 5,
        "kill_switch_drawdown_pct": 20,
        "min_trade_usdt": 10,
    },
    "lab": {
        "population": 300,
        "generations": 4,
        "keep_top": 5,
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
    },
    "news": {"enabled": True, "refresh_minutes": 15, "block_below": -0.5,
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
    "dashboard": {"host": "127.0.0.1", "port": 8050},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    user = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, user)
    if cfg.get("mode") != "paper":
        raise SystemExit(
            "Only mode: paper is supported in this version. "
            "Real-money trading is not built yet (by design)."
        )
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
