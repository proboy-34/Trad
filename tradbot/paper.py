"""Paper-trading account (fake money) stored in a SQLite database file.

Everything is saved immediately, so if the PC turns off the bot continues
exactly where it stopped (open trades included).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from .config import STATE_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cash REAL, start_balance REAL, peak_equity REAL,
  day TEXT, day_start_equity REAL,
  kill_switch INTEGER DEFAULT 0, paused INTEGER DEFAULT 0, created REAL
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT, strategy_id TEXT, strategy_json TEXT,
  units REAL, entry_price REAL, entry_time REAL, cost REAL,
  stop REAL, take REAL, max_hold_until REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT, strategy_id TEXT, entry_time REAL, exit_time REAL,
  entry_price REAL, exit_price REAL, units REAL, cost REAL, proceeds REAL,
  pnl REAL, return_pct REAL, reason TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS equity (time REAL PRIMARY KEY, equity REAL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, kind TEXT, text TEXT
);
"""


def utc_day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))


class PaperAccount:
    def __init__(self, cfg: dict, db_path=None):
        self.cfg = cfg
        self.fee = cfg["paper"]["fee_pct"] / 100
        self.slip = cfg["paper"]["slippage_pct"] / 100
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path or STATE_DB), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            self._migrate()
            if not self.db.execute("SELECT 1 FROM account").fetchone():
                self._create(cfg["paper"]["starting_balance"])

    # columns added in later versions (old databases are upgraded in place)
    NEW_COLUMNS = {
        "positions": {"low_seen": "REAL", "high_seen": "REAL", "news_score": "REAL",
                      "be_trigger": "REAL", "be_price": "REAL", "be_done": "INTEGER",
                      "trail_dist": "REAL", "stop0": "REAL"},
        "equity": {"hold": "REAL"},
        "trades": {"stop": "REAL", "take": "REAL", "mae_pct": "REAL", "mfe_pct": "REAL",
                   "lesson": "TEXT", "tags": "TEXT"},
    }

    def _migrate(self):
        for table, cols in self.NEW_COLUMNS.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, kind in cols.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.db.commit()

    def _create(self, balance: float):
        self.db.execute(
            "INSERT INTO account (id, cash, start_balance, peak_equity, day, day_start_equity, created)"
            " VALUES (1, ?, ?, ?, ?, ?, ?)",
            (balance, balance, balance, utc_day(), balance, time.time()))
        self.db.commit()

    def reset(self):
        with self.lock:
            for t in ("account", "positions", "trades", "equity", "kv"):
                self.db.execute(f"DELETE FROM {t}")
            self._create(self.cfg["paper"]["starting_balance"])
            self.message("system", "Paper account was reset to the starting balance.")

    # ------------------------------------------------------------ reads
    def account(self) -> dict:
        with self.lock:
            return dict(self.db.execute("SELECT * FROM account").fetchone())

    def positions(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM positions ORDER BY id")]

    def trades(self, limit: int = 200) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))]

    def equity_history(self, limit: int = 3000) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT time, equity, hold FROM equity ORDER BY time DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def messages(self, limit: int = 50) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))]

    def equity(self, prices: dict[str, float]) -> float:
        acc = self.account()
        value = acc["cash"]
        for p in self.positions():
            px = prices.get(p["symbol"], p["entry_price"])
            value += p["units"] * px * (1 - self.fee)
        return value

    def strategy_stats(self) -> dict[str, dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT strategy_id, COUNT(*) n, SUM(pnl) pnl,"
                " SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) gains,"
                " SUM(CASE WHEN pnl <= 0 THEN -pnl ELSE 0 END) losses,"
                " SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins"
                " FROM trades GROUP BY strategy_id").fetchall()
        out = {}
        for r in rows:
            pf = (r["gains"] / r["losses"]) if r["losses"] else (99.0 if r["gains"] else 0.0)
            out[r["strategy_id"]] = {"trades": r["n"], "pnl": r["pnl"], "profit_factor": pf,
                                     "win_rate": 100 * r["wins"] / r["n"] if r["n"] else 0}
        return out

    # ----------------------------------------------------------- writes
    def message(self, kind: str, text: str):
        with self.lock:
            self.db.execute("INSERT INTO messages (time, kind, text) VALUES (?, ?, ?)",
                            (time.time(), kind, text))
            self.db.execute("DELETE FROM messages WHERE id < (SELECT MAX(id) - 1000 FROM messages)")
            self.db.commit()

    def update_account(self, **fields):
        if not fields:
            return
        with self.lock:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE account SET {cols} WHERE id = 1", tuple(fields.values()))
            self.db.commit()

    def record_equity(self, value: float, ts: float | None = None, hold: float | None = None):
        """Balance history; `hold` = what the balance would be by just holding the coins."""
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO equity (time, equity, hold) VALUES (?, ?, ?)",
                            (ts or time.time(), value, hold))
            self.db.commit()

    def update_extremes(self, symbol: str, price: float, low: float | None = None,
                        high: float | None = None):
        """Remembers the lowest/highest price seen during each open trade (for lessons)."""
        lo, hi = min(price, low or price), max(price, high or price)
        with self.lock:
            self.db.execute("UPDATE positions SET low_seen = MIN(COALESCE(low_seen, ?), ?),"
                            " high_seen = MAX(COALESCE(high_seen, ?), ?) WHERE symbol = ?",
                            (lo, lo, hi, hi, symbol))
            self.db.commit()

    def move_stop_to_break_even(self, pos_id: int) -> float | None:
        """Moves the stop up to the break-even price (never down). Returns new stop."""
        with self.lock:
            row = self.db.execute("SELECT stop, be_price FROM positions WHERE id = ? AND"
                                  " be_done = 0 AND be_price IS NOT NULL", (pos_id,)).fetchone()
            if not row:
                return None
            new = max(row["stop"], row["be_price"])
            self.db.execute("UPDATE positions SET stop = ?, be_done = 1 WHERE id = ?", (new, pos_id))
            self.db.commit()
            return new

    def raise_stop(self, pos_id: int, new_stop: float) -> float | None:
        """Moves a stop-loss UP (trailing stop). Never moves it down. Returns the new stop."""
        with self.lock:
            cur = self.db.execute("UPDATE positions SET stop = ? WHERE id = ? AND stop < ?",
                                  (new_stop, pos_id, new_stop))
            self.db.commit()
            return new_stop if cur.rowcount else None

    def get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return json.loads(row["v"]) if row else default

    def put(self, key: str, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, json.dumps(value)))
            self.db.commit()

    def set_lesson(self, trade_id: int, lesson: str, tags: list[str], mae: float, mfe: float):
        with self.lock:
            self.db.execute("UPDATE trades SET lesson = ?, tags = ?, mae_pct = ?, mfe_pct = ?"
                            " WHERE id = ?", (lesson, ",".join(tags), mae, mfe, trade_id))
            self.db.commit()

    def open_position(self, symbol: str, strategy: dict, price: float, usdt: float,
                      stop: float, take: float, max_hold_until: float, note: str = "",
                      news_score: float | None = None, be_trigger: float | None = None,
                      trail_dist: float | None = None) -> dict:
        """Fake market BUY: pays slippage + fee, exactly like the backtester."""
        with self.lock:
            acc = self.account()
            usdt = min(usdt, acc["cash"])
            fill = price * (1 + self.slip)
            units = usdt * (1 - self.fee) / fill
            cur = self.db.execute(
                "INSERT INTO positions (symbol, strategy_id, strategy_json, units, entry_price,"
                " entry_time, cost, stop, take, max_hold_until, note, low_seen, high_seen, news_score,"
                " be_trigger, be_price, be_done, trail_dist, stop0)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, strategy["id"], json.dumps(strategy), units, fill, time.time(), usdt,
                 stop, take, max_hold_until, note, fill, fill, news_score,
                 be_trigger, fill * (1 + 2 * self.fee + 2 * self.slip) if be_trigger else None, 0,
                 trail_dist, stop))
            self.db.execute("UPDATE account SET cash = cash - ? WHERE id = 1", (usdt,))
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM positions WHERE id = ?",
                                        (cur.lastrowid,)).fetchone())

    def close_position(self, pos_id: int, price: float, reason: str) -> dict | None:
        """Fake market SELL: pays slippage + fee."""
        with self.lock:
            row = self.db.execute("SELECT * FROM positions WHERE id = ?", (pos_id,)).fetchone()
            if not row:
                return None
            p = dict(row)
            fill = price * (1 - self.slip)
            proceeds = p["units"] * fill * (1 - self.fee)
            pnl = proceeds - p["cost"]
            cur = self.db.execute(
                "INSERT INTO trades (symbol, strategy_id, entry_time, exit_time, entry_price,"
                " exit_price, units, cost, proceeds, pnl, return_pct, reason, note, stop, take)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["symbol"], p["strategy_id"], p["entry_time"], time.time(), p["entry_price"], fill,
                 p["units"], p["cost"], proceeds, pnl, 100 * pnl / p["cost"], reason, p["note"],
                 p["stop"], p["take"]))
            trade_id = cur.lastrowid
            self.db.execute("DELETE FROM positions WHERE id = ?", (pos_id,))
            self.db.execute("UPDATE account SET cash = cash + ? WHERE id = 1", (proceeds,))
            self.db.commit()
            return {**p, "trade_id": trade_id, "exit_price": fill, "pnl": pnl, "reason": reason}
