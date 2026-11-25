"""Dashboard web server (opens in your browser at http://127.0.0.1:8050).

Uses Python's built-in server - nothing heavy to install, low memory.
Only your own PC can open it (host 127.0.0.1).
"""
from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import data
from .brain import Brain
from .config import save_env_value
from .engine import BACKTEST_FILE, Engine
from .lab import load_active
from .log import RECENT, get_logger

log = get_logger("dashboard")
WEB = Path(__file__).resolve().parent / "web"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class API:
    def __init__(self, engine: Engine):
        self.e = engine

    # ------------------------------------------------------------- GET
    def state(self, q):
        e = self.e
        acc = e.account.account()
        positions = []
        for p in e.account.positions():
            px = e.prices.get(p["symbol"], p["entry_price"])
            value = p["units"] * px * (1 - e.account.fee)
            positions.append({**{k: v for k, v in p.items() if k != "strategy_json"},
                              "price": px, "value": value, "pnl": value - p["cost"],
                              "pnl_pct": (value / p["cost"] - 1) * 100})
        summary = e.summary()
        peak = acc["peak_equity"] or 1
        return {
            "summary": summary, "account": acc, "positions": positions,
            "drawdown_pct": max(0.0, (1 - summary["equity"] / peak) * 100),
            "engine": e.state, "job": e.job, "brain": e.brain.status(),
            "research": e.research, "pool": e.pool.counts(),
            "radar": sorted(e.radar, key=lambda r: (r["status"] != "holding",
                                                    r["status"] != "signal",
                                                    -r["met"] / max(1, r["total"])))[:10],
            "activity": list(e.activity)[-80:][::-1], "scan_seconds": e.scan_sec,
            "news": e.news.status(), "filter": e.filter.report,
            "learning": {k: e.adjust.get(k) for k in ("insights", "mistakes", "coins", "strategies")},
            "risk": e.cfg["risk"], "symbols": e.cfg["symbols"], "timeframe": e.tf,
            "server_time": time.time(),
        }

    def equity(self, q):
        acc = self.e.account.account()
        return {"points": self.e.account.equity_history(), "start": acc["start_balance"]}

    def trades(self, q):
        return {"trades": self.e.account.trades(300)}

    def messages(self, q):
        return {"messages": self.e.account.messages(80)}

    def logs(self, q):
        return {"lines": list(RECENT)[-200:]}

    def strategies(self, q):
        stats = self.e.account.strategy_stats()
        order = {"live": 0, "new": 1}
        pool = sorted(self.e.pool.entries(), key=lambda x: (
            order.get(x.get("stage"), 2), -((x.get("forward") or {}).get("return_pct") or 0)))
        for a in pool:
            a["live"] = stats.get(a["strategy"]["id"])
        d = load_active()
        return {"pool": pool[:60], "retired": d.get("retired", [])[-30:][::-1],
                "counts": self.e.pool.counts(), "research": self.e.research,
                "last_round": d.get("stats", {})}

    def backtest(self, q):
        return _read_json(BACKTEST_FILE, {"strategies": []})

    def candles(self, q):
        sym = (q.get("symbol") or [self.e.cfg["symbols"][0]])[0]
        if sym not in self.e.cfg["symbols"]:
            return {"error": "unknown symbol"}
        if sym in self.e.recent:
            df = self.e.recent[sym][0]
        else:
            df = data.load_cached(sym, self.e.tf)
        df = df.tail(300)
        candles = [{"time": int(r.time // 1000), "open": r.open, "high": r.high,
                    "low": r.low, "close": r.close} for r in df.itertuples()]
        trades = [t for t in self.e.account.trades(300) if t["symbol"] == sym]
        opens = [p for p in self.e.account.positions() if p["symbol"] == sym]
        return {"symbol": sym, "candles": candles,
                "trades": [{"entry_time": t["entry_time"], "exit_time": t["exit_time"],
                            "pnl": t["pnl"], "reason": t["reason"]} for t in trades],
                "open": [{"entry_time": p["entry_time"], "stop": p["stop"], "take": p["take"],
                          "entry_price": p["entry_price"]} for p in opens]}

    # ------------------------------------------------------------ POST
    def post(self, action: str, body: dict):
        e = self.e
        if action == "research-toggle":
            e.research["paused"] = not e.research["paused"]
            e.act("Research " + ("paused" if e.research["paused"] else "resumed") + " by you.", "system")
            return {"ok": True, "paused": e.research["paused"]}
        if action == "backtest":
            return {"ok": e.run_backtest_job()}
        if action == "pause":
            e.account.update_account(paused=1)
            e.account.message("system", "Bot paused by you - no new trades.")
            return {"ok": True}
        if action == "resume":
            e.account.update_account(paused=0)
            e.account.message("system", "Bot resumed.")
            return {"ok": True}
        if action == "reset-kill":
            eq = e.account.equity(e.prices)
            e.account.update_account(kill_switch=0, peak_equity=eq)
            e.account.message("risk", "Kill switch reset by you.")
            return {"ok": True}
        if action == "reset-account":
            if e.account.positions() and not body.get("force"):
                return {"ok": False, "msg": "Close open trades first (or confirm again)."}
            e.account.reset()
            return {"ok": True}
        if action == "settings":
            provider = str(body.get("provider", "")).lower()
            if provider in ("gemini", "claude", "none"):
                save_env_value("BRAIN_PROVIDER", provider)
            for field, env in (("gemini_key", "GEMINI_API_KEY"), ("claude_key", "ANTHROPIC_API_KEY")):
                if body.get(field):
                    save_env_value(env, str(body[field]).strip())
            e.brain = Brain(e.cfg, on_message=e.brain.on_message)
            e.news.brain = e.brain
            e.brain.lessons = e.adjust.get("insights", [])
            return {"ok": True, "brain": e.brain.status()}
        if action == "test-ai":
            if not e.brain.available():
                return {"ok": False, "msg": "No AI key saved yet."}
            text = e.brain.ask("Reply with exactly: TradBot AI brain is working.")
            return {"ok": bool(text), "msg": text or e.brain.last_error}
        return {"ok": False, "msg": "unknown action"}


def password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "")


def is_local(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def make_handler(api: API):
    routes = {"state": api.state, "equity": api.equity, "trades": api.trades,
              "messages": api.messages, "log": api.logs, "strategies": api.strategies,
              "backtest": api.backtest, "candles": api.candles}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):   # keep the console clean
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _authorized(self) -> bool:
            """With DASHBOARD_PASSWORD set, the browser asks for it (any user name)."""
            pw = password()
            if not pw:
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    given = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)[-1]
                except Exception:
                    given = ""
                if hmac.compare_digest(given.encode(), pw.encode()):
                    return True
            body = b"Password required"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="TradBot", charset="UTF-8"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        def do_GET(self):
            if not self._authorized():
                return
            u = urlparse(self.path)
            if u.path.startswith("/api/"):
                fn = routes.get(u.path[5:])
                if not fn:
                    return self._json({"error": "not found"}, 404)
                try:
                    return self._json(fn(parse_qs(u.query)))
                except Exception as ex:
                    log.error("API %s failed: %s", u.path, ex)
                    return self._json({"error": str(ex)}, 500)
            rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            f = (WEB / rel).resolve()
            if not str(f).startswith(str(WEB)) or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, f.read_bytes(), ctype, cache="vendor" in rel)

        def do_POST(self):
            if not self._authorized():
                return
            u = urlparse(self.path)
            # only our own page can send commands (blocks other websites)
            if self.headers.get("X-TradBot") != "1" or not u.path.startswith("/api/"):
                return self._json({"error": "forbidden"}, 403)
            length = min(int(self.headers.get("Content-Length") or 0), 100_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                body = {}
            try:
                return self._json(api.post(u.path[5:], body))
            except Exception as ex:
                log.error("API %s failed: %s", u.path, ex)
                return self._json({"ok": False, "msg": str(ex)}, 500)

    return Handler


def serve(engine: Engine, open_browser: bool = True):
    cfg = dict(engine.cfg["dashboard"])
    # hosting services usually tell the app its port in an environment variable
    port_env = os.environ.get("PORT") or os.environ.get("SERVER_PORT")
    if port_env:
        cfg["port"] = int(port_env)
    if os.environ.get("TRADBOT_HOST"):
        cfg["host"] = os.environ["TRADBOT_HOST"]
    if not is_local(cfg["host"]) and not password():
        log.error("SAFETY: the dashboard would be open to the internet without a password. "
                  "Set DASHBOARD_PASSWORD (see docs/SERVER_SETUP.md). Using 127.0.0.1 instead.")
        cfg["host"] = "127.0.0.1"
    server = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), make_handler(API(engine)))
    server.daemon_threads = True
    url = f"http://{'127.0.0.1' if cfg['host'] in ('0.0.0.0', '') else cfg['host']}:{cfg['port']}"
    log.info("Dashboard ready: open %s in your browser", url)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
