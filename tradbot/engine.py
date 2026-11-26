"""The Engine - runs the whole bot by itself, 24/7.

 every 30 s : live scan - prices (tiny downloads), stop-loss / target / trailing stop,
              and how close every LIVE strategy is to buying (dashboard)
 every hour : when a candle closes -> exits, then new buy decisions
 non-stop   : research thread (see _research_loop): new strategies, live market
              test, promotion / retirement, ML + trade-filter retraining
 every day  : summary (+ AI coach) to the dashboard and your phone, automatic backup

One coin failing (delisted, bad data, timeout) never stops the others.
"""
from __future__ import annotations

import collections
import json
import shutil
import sys
import threading
import time
import traceback

import numpy as np

from . import __version__, backtest, backup, data, ml
from .brain import Brain
from .config import DATA_DIR
from .features import WARMUP_BARS, compute_features, fmt_value
from .lab import Lab, load_active
from .learn import learn, lesson_for, size_multiplier
from .log import get_logger
from .news import NewsWatcher
from .notify import Notifier
from .paper import PaperAccount, utc_day
from .pool import StrategyPool
from .risk import RiskManager
from .strategy import Strategy
from .tradefilter import TradeFilter

log = get_logger("engine")
BACKTEST_FILE = DATA_DIR / "backtest.json"
LIVE_BARS = 1000          # candles kept in memory per coin (enough for exact 200-candle averages)


def load_datasets(cfg: dict, download: bool = True, warn=None) -> dict:
    """Full price history + indicators per coin. Coins that fail are skipped, not fatal."""
    warn = warn or (lambda msg: log.warning(msg))
    out = {}
    for sym in cfg["symbols"]:
        try:
            df = (data.update_history(sym, cfg["timeframe"], cfg["history_days"]) if download
                  else data.load_cached(sym, cfg["timeframe"]))
        except data.DataError as e:
            df = data.load_cached(sym, cfg["timeframe"])
            warn(f"{sym}: could not download new candles ({e}) - using saved history")
        except Exception as e:
            warn(f"{sym}: skipped ({type(e).__name__}: {e})")
            continue
        if len(df) < WARMUP_BARS + 1000:
            warn(f"{sym}: not enough price history yet ({len(df)} candles) - skipped in research")
            continue
        out[sym] = compute_features(df)
    if not out:
        raise data.DataError("no price history available for any coin")
    return out


def run_backtest(cfg: dict, datasets: dict | None = None, progress=None,
                 probs: dict | None = None) -> dict:
    """Backtests the LIVE strategies over all history, saves data/backtest.json."""
    progress = progress or (lambda p, m: None)
    datasets = datasets or load_datasets(cfg)
    active = load_active().get("active", [])
    costs = backtest.Costs(cfg["paper"]["fee_pct"], cfg["paper"]["slippage_pct"],
                           cfg["risk"]["max_position_pct"] / 100)
    probs = dict(probs or {})
    if (not probs and cfg["ml"]["enabled"] and ml.HAVE_LGB
            and any(a["strategy"].get("ml_min_prob") for a in active)):
        progress(10, "Computing walk-forward ML probabilities...")
        for sym, f in datasets.items():
            probs[sym] = ml.walk_forward_probs(f, cfg["ml"]["horizon_bars"], cfg["ml"]["target_pct"],
                                               threads=cfg["ml"]["threads"])
    n = min(len(f) for f in datasets.values())          # align coins on their common recent part
    results = []
    for i, a in enumerate(active):
        s = Strategy.from_dict(a["strategy"])
        curves, per = [], {}
        for sym, f in datasets.items():
            r = backtest.run(s, f, WARMUP_BARS, None, costs, probs.get(sym), cfg["timeframe"],
                             record_trades=True)
            curves.append(r.equity[-(n - WARMUP_BARS):])
            per[sym] = {"metrics": r.metrics, "trades": r.trades[-300:]}
        results.append({"id": s.id, "description": s.describe(), "per_symbol": per,
                        "equity": _downsample(np.mean(curves, axis=0))})
        progress(20 + 70 * (i + 1) / max(1, len(active)), f"Backtested {s.id}")
    ref = min(datasets.values(), key=len)
    times = ref["time"].to_numpy()[WARMUP_BARS:]
    bh = np.mean([f["close"].to_numpy()[-len(times):] / f["close"].to_numpy()[-len(times)]
                  for f in datasets.values()], axis=0)
    out = {"updated": time.time(), "times": _downsample(times), "buy_hold": _downsample(bh),
           "strategies": results}
    BACKTEST_FILE.write_text(json.dumps(out, default=float), encoding="utf-8")
    progress(100, "Backtest done")
    return out


def _downsample(arr, n: int = 800):
    arr = np.asarray(arr)
    if len(arr) <= n:
        return arr.tolist()
    idx = np.linspace(0, len(arr) - 1, n).astype(int)
    return arr[idx].tolist()


def _fmt_price(v: float) -> str:
    return f"{v:,.2f}" if v >= 1 else f"{v:.6f}"


ACT_KIND = {"trade": "trade", "lesson": "lesson", "lab": "lab", "risk": "risk",
            "system": "system", "error": "error"}


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tf = cfg["timeframe"]
        self.tf_sec = data.TF_MS[self.tf] // 1000
        self.account = PaperAccount(cfg)                                # main paper account
        self.learn_acc = PaperAccount(cfg, db_path=DATA_DIR / "learning.db")  # learning trades
        self.risk = RiskManager(cfg, self.account)
        self.notifier = Notifier(cfg)
        self.brain = Brain(cfg, on_message=self._on_ai)
        self.models = {s: ml.load_model(s) for s in cfg["symbols"]}
        self.prices: dict[str, float] = {}
        self.candles: dict = {}             # latest candles per coin (incl. the forming one)
        self.recent: dict = {}              # closed candles + features per coin (chart)
        self.coin_errors: dict[str, str] = {}
        self.job = {"name": None, "pct": 0, "msg": "", "error": ""}
        self.state = {"running": False, "last_hourly": None, "next_hourly": None,
                      "last_error": "", "started": time.time(), "last_scan": None}
        self.exit_code = None               # set by the dashboard to restart (updates, restores)
        self._stop = threading.Event()
        self._job_lock = threading.Lock()
        self._news_busy = False
        self._scan_sig = None
        self._offline_since = None
        self._last_daily = self.account.get("last_daily_summary")
        self._last_backup_day = None
        self._probation: set[str] = set()
        self._new_this_candle = 0
        self._learn_new = 0
        self._learn_cands: list[Strategy] = []
        self.pool = StrategyPool(cfg, self.account)
        self.news = NewsWatcher(cfg, self.brain)
        self.filter = TradeFilter(cfg)
        self.adjust: dict = {}
        self._relearn()
        self.research = {"state": "starting", "paused": False, "round": 0, "pct": 0,
                         "msg": "starting...", "last": {}}
        self.scan_sec = int(cfg.get("scan_seconds", 30))
        self.radar: list[dict] = []
        self.activity = collections.deque(maxlen=300)
        self._seed_activity()

    # ------------------------------------------------------------ helpers
    def act(self, msg: str, kind: str = "scan"):
        """A line for the live timeline (memory only; important events are also saved)."""
        self.activity.append({"time": time.time(), "kind": kind, "msg": msg})

    def _seed_activity(self):
        """After a restart, the timeline starts with the last saved important events."""
        for m in reversed(self.account.messages(40)):
            kind = "ai" if m["kind"].startswith("ai") else ACT_KIND.get(m["kind"], "system")
            self.activity.append({"time": m["time"], "kind": kind, "msg": m["text"][:400]})

    def _on_ai(self, kind: str, text: str):
        self.account.message("ai-" + kind, text)
        self.act(("AI coach: " if kind == "report" else "AI: ") + text[:400], "ai")
        if kind == "report":
            self.notifier.send("AI coach:\n" + text, "daily")

    def _relearn(self):
        """Re-learns from ALL finished trades: trade size per coin/strategy and the most
        common mistakes (also given to the AI brain)."""
        trades = self.account.trades(500) + self.learn_acc.trades(500)
        trades.sort(key=lambda t: t["exit_time"], reverse=True)
        self.adjust = learn(trades)
        self.brain.lessons = self.adjust["insights"]

    def _news_check(self):
        if self._news_busy or not self.news.due():
            return
        self._news_busy = True

        def work():
            try:
                for m in self.news.refresh():
                    self.act(m, "news")
            except Exception as e:
                log.warning("News check failed: %s", e)
            finally:
                self._news_busy = False
        threading.Thread(target=work, daemon=True, name="news").start()

    # --------------------------------------------------------------- jobs
    def start_job(self, name: str, fn) -> bool:
        """Runs a long task (backtest) in the background. One at a time."""
        if not self._job_lock.acquire(blocking=False):
            return False

        def progress(pct, msg):
            self.job.update(pct=round(pct, 1), msg=msg)

        def runner():
            self.job.update(name=name, pct=0, msg="starting...", error="")
            try:
                fn(progress)
            except Exception as e:
                self.job["error"] = f"{type(e).__name__}: {e}"
                log.error("%s failed: %s\n%s", name, e, traceback.format_exc())
                self.account.message("error", f"{name} failed: {e}")
            finally:
                self.job["name"] = None
                self._job_lock.release()

        threading.Thread(target=runner, daemon=True, name=name).start()
        return True

    def run_backtest_job(self) -> bool:
        return self.start_job("Backtest", lambda p: run_backtest(self.cfg, progress=p))

    # ---------------------------------------------------- research worker
    def _research_loop(self):
        """Never stops: refresh data -> live market test of every strategy -> promote /
        retire -> invent + test a new round -> short break -> repeat."""
        rc = self.cfg["research"]
        use_ml = bool(self.cfg["ml"]["enabled"] and ml.HAVE_LGB)
        datasets, probs = None, {}
        last_refresh = last_train = 0.0
        last_filter = self.filter.report.get("updated") or 0.0
        while not self._stop.is_set():
            if self.research["paused"]:
                self.research.update(state="paused", msg="Research paused by you")
                self._stop.wait(3)
                continue
            try:
                if datasets is None or time.time() - last_refresh > rc["refresh_minutes"] * 60:
                    self.research.update(state="running", pct=2,
                                         msg="Downloading the newest candles for all coins...")
                    datasets = load_datasets(self.cfg, warn=lambda m: self.act(m, "error"))
                    probs = {}
                    if use_ml:
                        for i, (sym, f) in enumerate(datasets.items()):
                            self.research.update(msg=f"ML model learning {sym} (walk-forward)...",
                                                 pct=5 + 40 * i / len(datasets))
                            probs[sym] = ml.walk_forward_probs(
                                f, self.cfg["ml"]["horizon_bars"], self.cfg["ml"]["target_pct"],
                                threads=self.cfg["ml"]["threads"])
                        if time.time() - last_train > 6 * 3600 or not all(
                                self.models.get(s) for s in datasets):
                            for sym, f in datasets.items():
                                ml.train_final(f, sym, self.cfg["ml"]["horizon_bars"],
                                               self.cfg["ml"]["target_pct"], self.cfg["ml"]["threads"])
                            self.models = {s: ml.load_model(s) for s in self.cfg["symbols"]}
                            last_train = time.time()
                    last_refresh = time.time()
                    self.research.update(msg="Live market test of every strategy in the pool...")
                    self.pool.forward_test(datasets, probs, self.tf)
                    self._review_pool()
                    if time.time() - last_filter > rc.get("filter_every_hours", 6) * 3600:
                        self.research.update(msg="Trade filter learning from past trades...")
                        rows = self.filter.collect(self.pool.entries(), datasets, probs, self.tf,
                                                   self.cfg["lab"]["split"][0])
                        rep = self.filter.train(rows)
                        last_filter = time.time()
                        self.act(f"Trade filter retrained on {rep.get('trades', 0)} trades: "
                                 f"{rep['status']}.", "filter")
                    try:
                        run_backtest(self.cfg, datasets, probs=probs)   # keeps the Backtest page fresh
                    except Exception as e:
                        log.warning("Backtest refresh skipped: %s", e)
                    c = self.pool.counts()
                    self.act(f"Live market test updated: {c['testing']} testing, {c['live']} LIVE, "
                             f"{c['retired']} retired.", "lab")

                self.research["round"] += 1
                n = self.research["round"]
                ai = (self.brain.available() and rc["ai_ideas_every_rounds"] > 0
                      and n % rc["ai_ideas_every_rounds"] == 1)

                def progress(pct, msg, n=n):
                    self.research.update(state="running", pct=round(pct, 1), msg=f"Round {n}: {msg}")

                res = Lab(self.cfg, datasets, brain=self.brain if ai else None,
                          progress=progress).run(
                    probs=probs, seeds=self.pool.seeds(20, self.learn_acc.strategy_stats()),
                    population=rc["round_population"], generations=rc["round_generations"],
                    train_ml=False, ai_ideas=ai)
                new = self.pool.add_round(res)
                self._review_pool()
                st, c = res["stats"], self.pool.counts()
                self.research.update(last=st, pct=100,
                                     msg=f"Round {n} done: {st['tested']} tested, {new} new")
                self.act(f"Research round {n}: tested {st['tested']} strategies"
                         + (f" (+{st['ai_ideas']} AI ideas)" if st.get("ai_ideas") else "")
                         + f", {st['passed_validation']} passed the unseen test, {new} new in the "
                         f"live market test. Pool: {c['testing']} testing, {c['live']} LIVE.", "lab")
            except data.DataError as e:
                self.research.update(state="waiting", msg=f"No internet / Binance: {e}")
                self._stop.wait(60)
                continue
            except Exception as e:
                self.research.update(state="error", msg=f"{type(e).__name__}: {e}")
                log.error("Research error: %s\n%s", e, traceback.format_exc())
                self._stop.wait(60)
                continue
            self.research.update(state="waiting",
                                 msg=f"Short break ({rc['pause_seconds']}s) so your PC stays fast...")
            self._stop.wait(rc["pause_seconds"])

    def _review_pool(self):
        for m in self.pool.review(self.account.strategy_stats(), self.learn_acc.strategy_stats()):
            self.account.message("lab", m)       # promotions / retirements are worth keeping
            self.act(m, "lab")

    # --------------------------------------------------------------- loop
    def start(self):
        self.state["running"] = True
        threading.Thread(target=self._loop, daemon=True, name="engine").start()
        if self.cfg["research"].get("enabled", True):
            threading.Thread(target=self._research_loop, daemon=True, name="research").start()

    def stop(self):
        self._stop.set()
        self.state["running"] = False

    def _loop(self):
        log.info("Engine %s started (paper trading). Press Ctrl+C to stop.", __version__)
        if not self.pool.entries():
            self.account.message("system", "First start: researching strategies "
                                           "(the first ones go live in a few minutes).")
        self.act(f"Bot {__version__} started. Scanning the market every {self.scan_sec} seconds.",
                 "system")
        next_hourly = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                self.scan()
                if now >= next_hourly:
                    self.hourly_cycle()
                    next_hourly = (now // self.tf_sec + 1) * self.tf_sec + 20   # 20 s after close
                    self.state["next_hourly"] = next_hourly
                self._scheduled_tasks()
                self.state["last_error"] = ""
                self._back_online()
            except data.DataError as e:
                self.state["last_error"] = str(e)
                if self._offline_since is None:
                    self._offline_since = now
                    self.act(f"Internet/Binance problem: {e} - retrying.", "error")
                log.warning("%s (will retry)", e)
            except Exception as e:
                self.state["last_error"] = f"{type(e).__name__}: {e}"
                self.act(f"Error: {e} - retrying.", "error")
                log.error("Engine error: %s\n%s", e, traceback.format_exc())
            self._stop.wait(self.scan_sec)

    def _back_online(self):
        if self._offline_since is not None:
            minutes = (time.time() - self._offline_since) / 60
            self._offline_since = None
            if minutes >= 15:
                msg = (f"Connection is back after {minutes:.0f} minutes offline. Stop-losses and "
                       "targets that were hit meanwhile were applied using the candles.")
                self.act(msg, "system")
                self.notifier.send("TradBot: " + msg, "alerts")

    def _scheduled_tasks(self):
        today = utc_day()
        if self._last_backup_day != today:
            self._last_backup_day = today
            try:
                backup.daily(self.cfg["backup"]["keep_days"])
            except Exception as e:
                log.warning("Daily backup failed: %s", e)
        lt = time.localtime()
        day_key = time.strftime("%Y-%m-%d", lt)
        if lt.tm_hour >= int(self.cfg["notify"].get("daily_hour", 21)) and self._last_daily != day_key:
            self._last_daily = day_key
            self.account.put("last_daily_summary", day_key)
            self.daily_summary()

    def daily_summary(self):
        sm, rd = self.summary(), self.readiness()
        hold = sm["hold_return_pct"]
        text = (f"TradBot daily summary (paper money)\n"
                f"Balance: ${sm['equity']:,.2f} ({sm['total_return_pct']:+.2f}% total"
                + (f", just holding the coins: {hold:+.2f}%" if hold is not None else "") + ")\n"
                f"Today: {sm['today_pnl_pct']:+.2f}% · trades closed today: {sm['trades_today']} · "
                f"open: {sm['open_positions']}\n"
                f"Research: {self.pool.counts()['tested_total']:,} strategies tested, "
                f"{len(sm['active_strategies'])} LIVE\n"
                f"Learning trades today: {sm['learning']['today']} (target {sm['learning']['target']}), "
                f"learning account {sm['learning']['pnl']:+,.2f} USDT over {sm['learning']['trades']} trades\n"
                f"Real-money readiness: {rd['passed']}/{rd['total']} checks")
        self.act(text.replace("\n", " · "), "system")
        self.notifier.send(text, "daily")
        if self.cfg["brain"].get("daily_report") and self.brain.available():
            threading.Thread(target=self.brain.daily_report, daemon=True, args=({
                **sm, "readiness": rd, "lessons": self.adjust.get("insights"),
                "mistakes": self.adjust.get("mistakes"),
                "recent_trades": [{k: t.get(k) for k in ("symbol", "strategy_id", "return_pct",
                                                        "reason", "lesson")}
                                  for t in self.account.trades(15)],
                "news": {k: v["score"] for k, v in self.news.mood.items()}},)).start()

    # --------------------------------------------------------- live scan
    def _live_strategies(self):
        active = load_active().get("active", [])
        self._probation = {a["strategy"]["id"] for a in active if a.get("probation")}
        return [Strategy.from_dict(a["strategy"]) for a in active]

    def scan(self):
        """Every few seconds: live prices (tiny downloads), stop / target / trailing checks,
        and a preview of how close every LIVE strategy is to buying. Buys themselves are
        decided only when a candle CLOSES - exactly like the backtest."""
        strategies = self._live_strategies()
        positions = self.account.positions()
        radar, ok_coins = [], 0
        for sym in self.cfg["symbols"]:
            try:
                df = data.refresh_recent(self.candles.get(sym), sym, self.tf, LIVE_BARS)
            except data.DataError as e:
                self.coin_errors[sym] = str(e)
                continue
            except Exception as e:          # bad data for one coin must not stop the rest
                self.coin_errors[sym] = f"{type(e).__name__}: {e}"
                continue
            if len(df) < WARMUP_BARS + 5:
                self.coin_errors[sym] = f"only {len(df)} candles available"
                continue
            self.candles[sym] = df
            self.coin_errors.pop(sym, None)
            ok_coins += 1
            price = float(df["close"].iat[-1])
            self.prices[sym] = price
            if not strategies:
                continue
            feats = compute_features(df)
            row = feats.iloc[-1]
            prob = ml.predict_latest(self.models.get(sym), feats.tail(1))[-1]
            change = (price / float(df["open"].iat[-1]) - 1) * 100
            holding = {p["strategy_id"] for p in positions if p["symbol"] == sym}
            for st in strategies:
                chk = st.check(row, None if prob != prob else float(prob))
                status = ("holding" if st.id in holding else "signal" if chk["signal"] else "waiting")
                radar.append({"symbol": sym, "price": price, "change_pct": change,
                              "strategy": st.id, "status": status, **chk})
        if ok_coins == 0:
            raise data.DataError("could not get prices for any coin")
        self.radar = radar
        bench = self.account.get("bench_start") or {}
        missing = {k: v for k, v in self.prices.items() if k not in bench}
        if missing:
            self.account.put("bench_start", {**bench, **missing})
        self.fast_check()
        self._news_check()
        self.state["last_scan"] = time.time()
        self._scan_line(strategies, radar, positions)

    def _scan_line(self, strategies, radar, positions):
        """Writes a scan line to the timeline only when something CHANGED (no spam)."""
        if not strategies:
            sig, line = ("none",), (f"Scanning {len(self.prices)} coins - no LIVE strategies yet "
                                    "(research is finding some).")
        elif radar:
            best = max(radar, key=lambda r: (r["status"] == "signal", r["met"] / max(1, r["total"])))
            signals = sorted(f"{r['symbol']} ({r['strategy']})" for r in radar if r["status"] == "signal")
            missing = next((r["rule"] for r in best["rules"] if not r["ok"]), "")
            sig = (tuple(signals), best["symbol"], best["strategy"], best["met"], len(positions))
            if signals:
                line = (f"{len(signals)} BUY signal(s) forming: {', '.join(signals[:3])} - "
                        "confirmed only when this candle closes.")
            else:
                line = (f"Closest to a buy: {best['symbol']} {best['strategy']} "
                        f"({best['met']}/{best['total']} rules)"
                        + (f", waiting for: {missing}" if missing else "") + ".")
            if positions:
                line += f" Watching {len(positions)} open trade(s)."
        else:
            return
        if sig != self._scan_sig:
            self._scan_sig = sig
            self.act(line)

    # ----------------------------------------------------- stops & exits
    def _update_stops(self, p: dict, high: float, acc: PaperAccount | None = None) -> dict:
        """Break-even and trailing stop: the stop-loss can only move UP."""
        acc = acc or self.account
        main = acc is self.account
        if p.get("be_trigger") and not p.get("be_done") and high >= p["be_trigger"]:
            new = acc.move_stop_to_break_even(p["id"])
            if new:
                p = {**p, "stop": new, "be_done": 1}
                if main:
                    self.act(f"{p['symbol']} {p['strategy_id']}: price is up enough - stop-loss moved "
                             f"to break-even ({_fmt_price(new)}). This trade can no longer turn into "
                             "a real loss (except a sudden gap).", "trade")
        if p.get("trail_dist"):
            top = max(high, p.get("high_seen") or high)
            candidate = top - p["trail_dist"]
            if candidate > p["stop"]:
                first_time = p["stop"] <= (p.get("stop0") or p["stop"]) + 1e-12
                if acc.raise_stop(p["id"], candidate):
                    if first_time and main:
                        self.act(f"{p['symbol']} {p['strategy_id']}: trailing stop is following the "
                                 f"price up - stop now {_fmt_price(candidate)}.", "trade")
                    p = {**p, "stop": candidate}
        return p

    @staticmethod
    def _stop_reason(p: dict) -> str:
        stop0 = p.get("stop0")
        if stop0 is None:                  # trade opened by an older version
            return "break-even" if p.get("be_done") else "stop-loss"
        if p["stop"] <= stop0 * (1 + 1e-9):
            return "stop-loss"
        if p.get("be_done") and p.get("be_price") and abs(p["stop"] - p["be_price"]) <= p["stop"] * 1e-9:
            return "break-even"
        return "trailing stop"

    def fast_check(self):
        """Stops / targets with the latest live prices (main AND learning trades)."""
        for acc in (self.account, self.learn_acc):
            for p in acc.positions():
                price = self.prices.get(p["symbol"])
                if price is None or p["symbol"] in self.coin_errors:
                    continue                  # no fresh price this round - check next round
                acc.update_extremes(p["symbol"], price)
                p = self._update_stops(p, price, acc)
                if price <= p["stop"]:
                    self._close(p, min(price, p["stop"]), self._stop_reason(p), acc)
                elif price >= p["take"]:
                    self._close(p, price, "take-profit", acc)

    # ------------------------------------------------------- hourly cycle
    def hourly_cycle(self):
        strategies = self._live_strategies()
        self._new_this_candle = 0
        self._learn_new = 0
        self._learn_cands = self._learning_candidates(strategies)
        for sym in self.cfg["symbols"]:
            df = self.candles.get(sym)
            if df is None or sym in self.coin_errors:
                continue
            try:
                closed = data.drop_unclosed(df, self.tf)
                if len(closed) < WARMUP_BARS + 5:
                    continue
                feats = compute_features(closed)
                probs = ml.predict_latest(self.models.get(sym), feats.tail(1))
                self.recent[sym] = (feats, probs)
                price = self.prices[sym]
                candle_t = int(feats["time"].iat[-1])
                self._exits(sym, feats, price)
                if self.account.get("decided:" + sym) != candle_t:
                    self.account.put("decided:" + sym, candle_t)   # survives crashes/restarts
                    self._entries(sym, feats, probs, price, strategies)
                    self._learning_entries(sym, feats, probs, price)
            except Exception as e:
                log.error("Hourly check failed for %s: %s\n%s", sym, e, traceback.format_exc())
                self.act(f"{sym}: hourly check failed ({e}) - the other coins continue.", "error")
        eq = self.account.equity(self.prices)
        r = self.risk.update(eq)
        if r.get("kill_switch_fired"):
            msg = (f"KILL SWITCH ON: the balance fell {r['drawdown_pct']:.1f}% from its peak. "
                   "No new trades until you reset it in Settings.")
            self.act(msg, "risk")
            self.notifier.send("TradBot: " + msg, "alerts")
        hold = self.summary()["hold_return_pct"]
        start = self.account.account()["start_balance"]
        self.account.record_equity(eq, hold=None if hold is None else start * (1 + hold / 100))
        self._review_pool()
        self.state["last_hourly"] = time.time()
        self.act(f"Candle closed - decision time: checked {len(self.recent)} coins with "
                 f"{len(strategies)} LIVE strategies. Balance ${eq:,.2f}.", "decision")

    def _exits(self, sym, feats, price):
        for acc in (self.account, self.learn_acc):
            self._exits_for(acc, sym, feats, price)

    def _exits_for(self, acc, sym, feats, price):
        now = time.time()
        for p in [p for p in acc.positions() if p["symbol"] == sym]:
            # stop / target touched while we were not watching (e.g. PC was off)?
            after = feats[feats["time"] / 1000 >= p["entry_time"] - 60]
            hit = None
            for row in after.itertuples():
                if row.low <= p["stop"]:
                    hit = (min(p["stop"], row.open), self._stop_reason(p))
                    break
                if row.high >= p["take"]:
                    hit = (max(p["take"], row.open), "take-profit")
                    break
                acc.update_extremes(sym, row.close, row.low, row.high)
                p = self._update_stops({**p, "high_seen": max(p.get("high_seen") or 0, row.high)},
                                       row.high, acc)  # takes effect from the next candle
            if hit:
                self._close(p, hit[0], hit[1], acc)
                continue
            s = Strategy.from_dict(json.loads(p["strategy_json"]))
            if s.exit and s.exit_signal(feats.tail(1)).any():
                self._close(p, price, "exit rule", acc)
            elif now >= p["max_hold_until"]:
                self._close(p, price, "time limit", acc)

    def _entries(self, sym, feats, probs, price, strategies):
        last = feats.tail(1)
        last_prob = probs[-1:] if probs is not None else None
        for s in strategies:
            if not s.entry_signal(last, last_prob).any():
                continue
            self.act(f"{sym} {s.id}: all buy rules met - BUY SIGNAL. Checking filter, risk, news "
                     "and AI...", "signal")
            if self._new_this_candle >= self.cfg["risk"]["max_new_per_candle"]:
                self.act(f"{sym} {s.id}: skipped - already {self._new_this_candle} new trades this "
                         "candle (coins move together, so more would be the same bet).", "risk")
                continue
            ml_now = None if last_prob is None or np.isnan(last_prob[0]) else float(last_prob[0])
            ok_f, win_p = self.filter.check(last.iloc[0], s, ml_now)
            if not ok_f:
                self.act(f"{sym} {s.id}: trade filter said NO - signals like this won only "
                         f"{win_p:.0%} of the time in past trades.", "filter")
                continue
            eq = self.account.equity(self.prices)
            ok, why, size = self.risk.can_open(eq, sym, s.id)
            if not ok:
                self.act(f"{sym} {s.id}: risk manager said NO - {why}.", "risk")
                continue
            blocked = self.news.blocks(sym)
            if blocked:
                self.account.message("trade", f"NEWS BLOCKED buy {sym} ({s.id}): {blocked}")
                self.act(f"{sym} {s.id}: news said NO - {blocked}.", "news")
                continue
            if s.id in self._probation:
                size *= 0.5
                self.act(f"{sym} {s.id}: strategy is on probation (not proven in the live market "
                         "test yet) - half size.", "risk")
            mult = size_multiplier(self.adjust, sym, s.id)
            if mult < 1:
                size *= mult
                self.act(f"{sym} {s.id}: learned from past losses - trade size cut to "
                         f"{int(mult * 100)}%.", "lesson")
            if size < self.cfg["risk"]["min_trade_usdt"]:
                continue
            atr = float(last["atr_pct"].iat[0])
            facts = {c.feature: fmt_value(c.feature, float(last[c.feature].iat[0])) for c in s.entry}
            note = "Bought because: " + "; ".join(c.describe() for c in s.entry)
            note += " (now: " + ", ".join(f"{k} {v}" for k, v in facts.items()) + ")"
            if ml_now is not None:
                note += f"; ML chance of rising {ml_now:.0%}"
            if win_p is not None:
                note += f"; filter win chance {win_p:.0%}"
            if self.brain.available() and self.cfg["brain"].get("review_trades"):
                ctx = {"symbol": sym, "price": price, "strategy": s.id,
                       "rules": s.describe()["buy"], "indicator_values": facts,
                       "ml_probability": None if ml_now is None else round(ml_now, 3),
                       "change_24h": fmt_value("ret_24", float(last["ret_24"].iat[0])),
                       "change_72h": fmt_value("ret_72", float(last["ret_72"].iat[0])),
                       "volatility_atr": fmt_value("atr_pct", atr)}
                approve, reason = self.brain.review_trade(ctx)
                if not approve:
                    self.account.message("trade", f"AI BLOCKED buy {sym} ({s.id}): {reason}")
                    self.act(f"{sym} {s.id}: AI blocked the buy - {reason}", "ai")
                    continue
                if reason:
                    note += f"; AI: {reason}"
            stop = price * (1 - s.stop_atr * atr)
            take = price * (1 + s.take_atr * atr)
            self.account.open_position(
                sym, s.to_dict(), price, size, stop, take, time.time() + s.max_hold * self.tf_sec,
                note, news_score=self.news.score(sym),
                be_trigger=price * (1 + s.be_atr * atr) if s.be_atr else None,
                trail_dist=s.trail_atr * atr * price if s.trail_atr else None)
            self._new_this_candle += 1
            msg = (f"BUY {sym} ${size:,.0f} at {_fmt_price(price)} ({s.id}). "
                   f"Stop {_fmt_price(stop)}, target {_fmt_price(take)}")
            log.info(msg)
            self.account.message("trade", msg)
            self.act(msg, "trade")
            self.notifier.send(f"TradBot paper trade\nBUY {sym} ${size:,.0f} at {_fmt_price(price)}\n"
                               f"Stop {_fmt_price(stop)} · target {_fmt_price(take)}\n{note}", "trades")

    def _close(self, p, price, reason, acc: PaperAccount | None = None):
        acc = acc or self.account
        main = acc is self.account
        t = acc.close_position(p["id"], price, reason)
        if not t:
            return
        msg = (("" if main else "[learning] ")
               + f"SELL {t['symbol']} at {_fmt_price(t['exit_price'])} ({reason}) "
               f"{'PROFIT' if t['pnl'] > 0 else 'LOSS'} ${abs(t['pnl']):,.2f}")
        log.info(msg)
        lesson, tags = lesson_for(t)
        entry = t["entry_price"]
        mae = (min(t.get("low_seen") or entry, t["exit_price"]) / entry - 1) * 100
        mfe = (max(t.get("high_seen") or entry, t["exit_price"]) / entry - 1) * 100
        acc.set_lesson(t["trade_id"], lesson, tags, mae, mfe)
        if main:
            self.account.message("trade", msg)
            self.act(msg, "trade")
            self.account.record_equity(self.account.equity(self.prices))
            self.account.message("lesson", f"{t['symbol']} {t['strategy_id']}: {lesson}")
            self.act(f"Lesson - {t['symbol']} {t['strategy_id']}: {lesson}", "lesson")
            self.notifier.send(f"TradBot paper trade\n{msg}\nLesson: {lesson}", "trades")
        else:
            self.act(f"{msg}. Lesson: {lesson}", "learn")
        self._relearn()

    # --------------------------------------------------- learning trades
    def _learning_opened_today(self) -> int:
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        opened = sum(1 for p in self.learn_acc.positions() if p["entry_time"] >= midnight)
        return opened + sum(1 for t in self.learn_acc.trades(500) if t["entry_time"] >= midnight)

    def _learning_candidates(self, live: list[Strategy]) -> list[Strategy]:
        """Strategies still being tested, least-tested first (so every strategy gets real
        trades), then the best history results. LIVE ones trade in the main account."""
        live_ids = {s.id for s in live}
        stats = self.learn_acc.strategy_stats()
        pool = [e for e in self.pool.entries() if e["strategy"]["id"] not in live_ids]
        pool.sort(key=lambda e: ((stats.get(e["strategy"]["id"]) or {}).get("trades", 0),
                                 -((e.get("validation") or {}).get("sharpe", 0))))
        return [Strategy.from_dict(e["strategy"]) for e in pool]

    def _learning_entries(self, sym, feats, probs, price):
        """Small paper trades in the separate LEARNING account, from strategies that are still
        being tested - at least `daily_target` per day, so the bot learns from real trades."""
        lc = self.cfg["learning"]
        if not lc.get("enabled") or not self._learn_cands:
            return
        if self._learning_opened_today() >= lc["daily_max"]:
            return
        if self.news.blocks(sym):
            return
        positions = self.learn_acc.positions()
        held = {(p["strategy_id"], p["symbol"]) for p in positions}
        last = feats.tail(1)
        last_prob = probs[-1:] if probs is not None else None
        for s in self._learn_cands:
            if (self._learn_new >= lc["max_new_per_candle"]
                    or len(positions) + self._learn_new >= lc["max_open"]):
                return
            if (s.id, sym) in held or not s.entry_signal(last, last_prob).any():
                continue
            size = min(lc["trade_usdt"], self.learn_acc.account()["cash"])
            if size < 10:
                return
            atr = float(last["atr_pct"].iat[0])
            stop, take = price * (1 - s.stop_atr * atr), price * (1 + s.take_atr * atr)
            note = ("Learning trade (strategy still being tested). Bought because: "
                    + "; ".join(c.describe() for c in s.entry))
            self.learn_acc.open_position(
                sym, s.to_dict(), price, size, stop, take, time.time() + s.max_hold * self.tf_sec,
                note, news_score=self.news.score(sym),
                be_trigger=price * (1 + s.be_atr * atr) if s.be_atr else None,
                trail_dist=s.trail_atr * atr * price if s.trail_atr else None)
            held.add((s.id, sym))
            self._learn_new += 1
            self.act(f"[learning] BUY {sym} ${size:,.0f} at {_fmt_price(price)} ({s.id}) - "
                     "testing this strategy with a real paper trade.", "learn")

    def learning_summary(self) -> dict:
        lc = self.cfg["learning"]
        trades = self.learn_acc.trades(5000)
        wins = sum(t["pnl"] > 0 for t in trades)
        acc = self.learn_acc.account()
        return {"enabled": bool(lc.get("enabled")), "today": self._learning_opened_today(),
                "target": lc["daily_target"], "open": len(self.learn_acc.positions()),
                "trades": len(trades), "pnl": sum(t["pnl"] for t in trades),
                "win_rate": 100 * wins / len(trades) if trades else 0,
                "equity": self.learn_acc.equity(self.prices), "start": acc["start_balance"],
                "candidates": len(self._learn_cands)}

    # -------------------------------------------------------------- views
    def summary(self) -> dict:
        acc = self.account.account()
        eq = self.account.equity(self.prices)
        trades = self.account.trades(1000)
        today = [t for t in trades if utc_day(t["exit_time"]) == utc_day()]
        bench = self.account.get("bench_start") or {}
        rets = [self.prices[k] / v - 1 for k, v in bench.items() if k in self.prices and v]
        return {
            "hold_return_pct": (sum(rets) / len(rets) * 100) if rets else None,
            "equity": eq, "start_balance": acc["start_balance"],
            "total_return_pct": (eq / acc["start_balance"] - 1) * 100,
            "today_pnl_pct": (eq / acc["day_start_equity"] - 1) * 100 if acc["day_start_equity"] else 0,
            "open_positions": len(self.account.positions()),
            "trades_today": len(today), "total_trades": len(trades),
            "prices": self.prices, "kill_switch": bool(acc["kill_switch"]),
            "active_strategies": [a["strategy"]["id"] for a in load_active().get("active", [])],
            "learning": self.learning_summary(),
        }

    def readiness(self) -> dict:
        """The checklist that must be passed before real money is even considered."""
        acc = self.account.account()
        sm = self.summary()
        trades = self.account.trades(5000)
        days = (time.time() - (acc.get("created") or time.time())) / 86400
        gains = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        losses = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
        pf = gains / losses if losses else (99.0 if gains else 0.0)
        values = [p["equity"] for p in self.account.equity_history(100000)] + [sm["equity"]]
        peak, max_dd = values[0], 0.0
        for v in values:
            peak = max(peak, v)
            max_dd = max(max_dd, (1 - v / peak) * 100 if peak else 0)
        ret, hold = sm["total_return_pct"], sm["hold_return_pct"]
        checks = [
            ("4+ weeks of paper trading", days >= 28, f"{days:.0f} of 28 days"),
            ("30+ finished trades", len(trades) >= 30, f"{len(trades)} of 30"),
            ("Profit after fees", len(trades) > 0 and ret > 0, f"{ret:+.2f}%"),
            ("Beats just holding the coins", hold is not None and len(trades) > 0 and ret > hold,
             f"bot {ret:+.2f}% vs holding {hold:+.2f}%" if hold is not None else "measuring..."),
            ("Wins bigger than losses (profit factor 1.2+)", len(trades) >= 10 and pf >= 1.2,
             f"{pf:.2f}" if trades else "no trades yet"),
            ("Biggest drop under 20%", max_dd < 20, f"{max_dd:.1f}%"),
            ("Kill switch never fired", not self.account.get("kill_fired"),
             "never" if not self.account.get("kill_fired") else "it fired"),
        ]
        rows = [{"check": c, "ok": bool(ok), "value": v} for c, ok, v in checks]
        passed = sum(r["ok"] for r in rows)
        return {"checks": rows, "passed": passed, "total": len(rows), "ready": passed == len(rows)}

    def health(self) -> dict:
        from . import installer
        state = {}
        try:
            state = json.loads(installer.STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        size = sum(p.stat().st_size for p in DATA_DIR.rglob("*") if p.is_file())
        return {
            "version": __version__, "python": sys.version.split()[0],
            "uptime_h": round((time.time() - self.state["started"]) / 3600, 1),
            "binance_server": data._good_host, "coin_errors": self.coin_errors,
            "last_scan": self.state["last_scan"], "research": self.research["state"],
            "ml": ml.HAVE_LGB, "ai": self.brain.status(), "news_error": self.news.error,
            "telegram": self.notifier.status(), "eco_mode": bool(self.cfg.get("eco_mode")),
            "data_mb": round(size / 1e6, 1),
            "disk_free_gb": round(shutil.disk_usage(DATA_DIR).free / 1e9, 1),
            "last_backup": backup.last_backup_time(),
            "config_warnings": self.cfg.get("_warnings", []),
            "missing_optional": state.get("failed", []),
        }
