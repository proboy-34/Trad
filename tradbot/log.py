"""Logging to console, to data/bot.log, and to a memory buffer for the dashboard."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import sys

from .config import DATA_DIR

RECENT = collections.deque(maxlen=400)   # last log lines for the dashboard


class _MemoryHandler(logging.Handler):
    def emit(self, record):
        try:
            RECENT.append({
                "time": record.created,
                "level": record.levelname,
                "msg": record.getMessage(),
            })
        except Exception:
            pass


_configured = False


def get_logger(name: str = "tradbot") -> logging.Logger:
    global _configured
    if not _configured:
        _configured = True
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger("tradbot")
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        # Windows consoles can choke on unusual characters - never crash on logging
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        filelog = logging.handlers.RotatingFileHandler(
            DATA_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        filelog.setFormatter(fmt)
        root.addHandler(console)
        root.addHandler(filelog)
        root.addHandler(_MemoryHandler())
    return logging.getLogger(name if name.startswith("tradbot") else f"tradbot.{name}")
