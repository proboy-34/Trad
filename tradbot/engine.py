"""The Engine - runs the whole bot by itself, 24/7.

 every 30 s   : live scan - prices, stop-loss / take-profit, and how close
                every strategy is to buying (shown live on the dashboard)
 every hour   : when a candle closes -> exits, then new buy decisions
 every day    : AI daily report
 every 6 h    : Strategy Lab re-research + ML retrain (lab.auto_every_hours)
 first start  : downloads history and runs the Strategy Lab automatically
"""
from __future__ import annotations

import collections
import json
import threading
import time
import traceback

import numpy as np

from . import backtest, data, ml
from .brain import Brain
from .config import DATA_DIR
from .features import WARMUP_BARS, compute_features, fmt_value
from .lab import Lab, load_active
from .learn import learn, lesson_for, size_multiplier
from .news import NewsWatcher
from .pool import StrategyPool
from .log import get_logger
from .paper import PaperAccount, utc_day
from .risk import RiskManager
from .strategy import Strategy

log = get_logger("engine")
BACKTEST_FILE = DATA_DIR / "backtest.json"


def load_datasets(cfg: dict, download: bool = True) -> dict:
    out = {}
    for sym in cfg["symbols"]:
        df = (data.update_history(sym, cfg["timeframe"], cfg["history_days"]) if download
              else data.load_cached(sym, cfg["timeframe"]))
        if len(df) < WARMUP_BARS + 1000:
            raise RuntimeError(f"Not enough history for {sym} ({len(df)} candles)")
        out[sym] = compute_features(df)
    return out


def run_backtest(cfg: dict, datasets: dict | None = None, progress=None) -> dict:
    """Backtests the active strategies over all history, saves data/backtest.json."""
    progress = progress or (lambda p, m: None)
    datasets = datasets or load_datasets(cfg)
    active = load_active().get("active", [])
    costs = backtest.Costs(cfg["paper"]["fee_pct"], cfg["paper"]["slippage_pct"],
                           cfg["risk"]["max_position_pct"] / 100)
    probs = {}
    if cfg["ml"]["enabled"] and ml.HAVE_LGB and any(a["strategy"].get("ml_min_prob") for a in active):
        progress(10, "Computing walk-forward ML probabilities...")
        for sym, f in datasets.items():
            probs[sym] = ml.walk_forward_probs(f, cfg["ml"]["horizon_bars"], cfg["ml"]["target_pct"],
                                               threads=cfg["ml"]["threads"])
    results = []
    for i, a in enumerate(active):
        s = Strategy.from_dict(a["strategy"])
        curves, per = [], {}
        for sym, f in datasets.items():
            r = backtest.run(s, f, WARMUP_BARS, None, costs, probs.get(sym), cfg["timeframe"],
                             record_trades=True)
            curves.append(r.equity)
            per[sym] = {"metrics": r.metrics, "trades": r.trades[-300:]}
        eq = np.mean(curves, axis=0)
        results.append({"id": s.id, "description": s.describe(), "per_symbol": per,
                        "equity": _downsample(eq)})
        progress(20 + 70 * (i + 1) / max(1, len(active)), f"Backtested {s.id}")
    any_df = next(iter(datasets.values()))
    times = any_df["time"].to_numpy()[WARMUP_BARS:]
    bh = np.mean([f["close"].to_numpy()[WARMUP_BARS:] / f["close"].to_numpy()[WARMUP_BARS]
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


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tf = cfg["timeframe"]
        self.tf_sec = data.TF_MS[self.tf] // 1000
        self.account = PaperAccount(cfg)
        self.risk = RiskManager(cfg, self.account)
        self.brain = Brain(cfg, on_message=lambda kind, text: self.account.message("ai-" + kind, text))
        self.models = {s: ml.load_model(s) for s in cfg["symbols"]}
        self.prices: dict[str, float] = {}
        self.recent: dict = {}             # latest candles+features per symbol (for the chart)
        self.last_candle: dict[str, int] = {}
        self.job = {"name": None, "pct": 0, "msg": "", "error": ""}
        self.state = {"running": False, "last_hourly": None, "next_hourly": None,
                      "last_error": "", "started": time.time()}
        self._stop = threading.Event()
        self._job_lock = threading.Lock()
        self._last_report_day = None
        self.pool = StrategyPool(cfg, self.account)
        self.news = NewsWatcher(cfg, self.brain)
        self._news_busy = False
        self.adjust: dict = {}
        self._relearn()
        self.research = {"state": "starting", "paused": False, "round": 0, "pct": 0,
                         "msg": "starting...", "last": {}}
        self.scan_sec = int(cfg.get("scan_seconds", 30))
        self.radar: list[dict] = []          # what every strategy sees right now
        self.activity = collections.deque(maxlen=300)   # live feed for the dashboard
        self.state["last_scan"] = None

    def _relearn(self):
        """Re-learns from ALL finished trades: trade sizes per coin/strategy and
        the most common mistakes (also given to the AI brain)."""
        self.adjust = learn(self.account.trades(500))
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

    def act(self, msg: str, kind: str = "scan"):
        """Adds a line to the live activity feed (memory only, not the log file)."""
        self.activity.append({"time": time.time(), "kind": kind, "msg": msg})

    # ------------------------------------------------------------ jobs
    def start_job(self, name: str, fn) -> bool:
        """Runs a long task (lab, backtest) in the background. One at a time."""
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

    # -------------------------------------------------- research worker
    def _research_loop(self):
        """Never stops: refresh data -> live market test of every strategy ->
        promote / retire -> invent + test a new round -> repeat."""
        rc = self.cfg["research"]
        use_ml = bool(self.cfg["ml"]["enabled"] and ml.HAVE_LGB)
        datasets, probs = None, {}
        last_refresh = last_train = 0.0
        while not self._stop.is_set():
            if self.research["paused"]:
                self.research.update(state="paused", msg="Research paused by you")
                self._stop.wait(3)
                continue
            try:
                if datasets is None or time.time() - last_refresh > rc["refresh_minutes"] * 60:
                    self.research.update(state="running", pct=2,
                                         msg="Downloading latest prices for all coins...")
                    datasets = load_datasets(self.cfg)
                    if use_ml:
                        for i, (sym, f) in enumerate(datasets.items()):
                            self.research.update(msg=f"ML model learning {sym} (walk-forward)...",
                                                 pct=5 + 40 * i / len(datasets))
                            probs[sym] = ml.walk_forward_probs(
                                f, self.cfg["ml"]["horizon_bars"], self.cfg["ml"]["target_pct"],
                                threads=self.cfg["ml"]["threads"])
                        if time.time() - last_train > 6 * 3600 or not all(self.models.values()):
                            for sym, f in datasets.items():
                                ml.train_final(f, sym, self.cfg["ml"]["horizon_bars"],
                                               self.cfg["ml"]["target_pct"], self.cfg["ml"]["threads"])
                            self.models = {s: ml.load_model(s) for s in self.cfg["symbols"]}
                            last_train = time.time()
                    last_refresh = time.time()
                    self.research.update(msg="Live market test of every strategy in the pool...")
                    self.pool.forward_test(datasets, probs, self.tf)
                    self._review_pool()
                    c = self.pool.counts()
                    self.act(f"Live market test updated: {c['testing']} strategies testing, "
                             f"{c['live']} live, {c['retired']} retired.", "lab")

                self.research["round"] += 1
                n = self.research["round"]
                ai = (self.brain.available() and rc["ai_ideas_every_rounds"] > 0
                      and n % rc["ai_ideas_every_rounds"] == 1)

                def progress(pct, msg, n=n):
                    self.research.update(state="running", pct=round(pct, 1),
                                         msg=f"Round {n}: {msg}")

                res = Lab(self.cfg, datasets, brain=self.brain if ai else None,
                          progress=progress).run(
                    probs=probs, seeds=self.pool.seeds(20),
                    population=rc["round_population"], generations=rc["round_generations"],
                    train_ml=False, ai_ideas=ai)
                new = self.pool.add_round(res)
                self._review_pool()
                st, c = res["stats"], self.pool.counts()
                self.research.update(last=st, pct=100,
                                     msg=f"Round {n} done: {st['tested']} tested, {new} new")
                self.act(f"Research round {n}: invented & tested {st['tested']} strategies"
                         + (f" (+{st['ai_ideas']} AI ideas)" if st.get("ai_ideas") else "")
                         + f" -> {st['passed_validation']} passed the unseen test, {new} NEW added to "
                         f"the live market test. Pool: {c['testing']} testing, {c['live']} live.", "lab")
                if new:
                    self.account.message("lab", f"Research round {n}: {new} new strategies "
                                                "entered the live market test.")
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
        for m in self.pool.review(self.account.strategy_stats()):
            self.account.message("lab", m)
            self.act(m, "lab")

    def run_backtest_job(self) -> bool:
        return self.start_job("Backtest", lambda p: run_backtest(self.cfg, progress=p))

    # ------------------------------------------------------------- loop
    def start(self):
        self.state["running"] = True
        threading.Thread(target=self._loop, daemon=True, name="engine").start()
        if self.cfg["research"].get("enabled", True):
            threading.Thread(target=self._research_loop, daemon=True, name="research").start()

    def stop(self):
        self._stop.set()
        self.state["running"] = False

    def _loop(self):
        log.info("Engine started (paper trading). Press Ctrl+C to stop.")
        if not self.pool.entries():
            self.account.message("system", "First start: researching strategies "
                                           "(the first ones go live in a few minutes).")
        next_hourly = 0.0
        self.act(f"Bot started. Scanning the market every {self.scan_sec} seconds.", "system")
        while not self._stop.is_set():
            now = time.time()
            try:
                if now >= next_hourly:
                    self.hourly_cycle()
                    next_hourly = (now // self.tf_sec + 1) * self.tf_sec + 20   # 20s after close
                    self.state["next_hourly"] = next_hourly
                self.scan()
                self._scheduled_tasks()
                self.state["last_error"] = ""
            except data.DataError as e:
                self.state["last_error"] = str(e)
                self.act(f"Internet/Binance problem: {e} - retrying.", "error")
                log.warning("%s (will retry)", e)
            except Exception as e:
                self.state["last_error"] = f"{type(e).__name__}: {e}"
                self.act(f"Error: {e} - retrying.", "error")
                log.error("Engine error: %s\n%s", e, traceback.format_exc())
            self._stop.wait(self.scan_sec)

    def _scheduled_tasks(self):
        t = time.gmtime()
        today = utc_day()
        if (self.cfg["brain"].get("daily_report") and t.tm_hour == 0 and t.tm_min >= 5
                and self._last_report_day != today and self.brain.available()):
            self._last_report_day = today
            self.brain.daily_report({**self.summary(), "lessons": self.adjust.get("insights"),
                                     "mistakes": self.adjust.get("mistakes"),
                                     "recent_trades": [
                                         {k: t.get(k) for k in ("symbol", "strategy_id", "return_pct",
                                                                "reason", "lesson")}
                                         for t in self.account.trades(15)],
                                     "news": {k: v["score"] for k, v in self.news.mood.items()}})

    # ----------------------------------------------------- fast checks
    def scan(self):
        """Every few seconds: live prices, stop-loss/take-profit checks, and a
        preview of how close every strategy is to buying. (Real buy decisions
        still happen only when a candle CLOSES - exactly like the backtest.)"""
        strategies = [Strategy.from_dict(a["strategy"]) for a in load_active().get("active", [])]
        positions = self.account.positions()
        radar = []
        for sym in self.cfg["symbols"]:
            df = data.fetch_recent(sym, self.tf, 300, include_open=True)
            if len(df) < WARMUP_BARS + 5:
                continue
            price = float(df["close"].iat[-1])
            self.prices[sym] = price
            feats = compute_features(df)
            row = feats.iloc[-1]
            prob = ml.predict_latest(self.models.get(sym), feats.tail(1))[-1]
            change = (price / float(df["open"].iat[-1]) - 1) * 100
            holding = {p["strategy_id"] for p in positions if p["symbol"] == sym}
            for st in strategies:
                chk = st.check(row, None if prob != prob else float(prob))
                status = ("holding" if st.id in holding else
                          "signal" if chk["signal"] else "waiting")
                radar.append({"symbol": sym, "price": price, "change_pct": change,
                              "strategy": st.id, "status": status, **chk})
        self.radar = radar
        for sym in {p["symbol"] for p in positions}:
            if sym in self.prices:
                self.account.update_extremes(sym, self.prices[sym])
        self.fast_check()
        self._news_check()
        self.state["last_scan"] = time.time()
        # one readable line for the activity feed
        if not strategies:
            self.act(f"Scanned {len(self.cfg['symbols'])} coins - no active strategies yet "
                     "(Strategy Lab will find some).")
        elif radar:
            best = max(radar, key=lambda r: (r["status"] == "signal", r["met"] / max(1, r["total"])))
            sig = [r for r in radar if r["status"] == "signal"]
            missing = next((r["rule"] for r in best["rules"] if not r["ok"]), "")
            line = (f"Scanned {len(self.cfg['symbols'])} coins x {len(strategies)} strategies. ")
            if sig:
                line += (f"{len(sig)} BUY signal(s) forming: "
                         + ", ".join(f"{r['symbol']} ({r['strategy']})" for r in sig[:3])
                         + " - confirms when this candle closes.")
            else:
                line += (f"Closest: {best['symbol']} {best['strategy']} "
                         f"{best['met']}/{best['total']} rules"
                         + (f", waiting for: {missing}" if missing else "") + ".")
            if positions:
                line += f" Watching stop-loss/target on {len(positions)} open trade(s)."
            self.act(line)

    def fast_check(self):
        """Stop-loss / take-profit using the latest live prices."""
        for p in self.account.positions():
            price = self.prices.get(p["symbol"]) or data.fetch_price(p["symbol"])
            if price <= p["stop"]:
                self._close(p, min(price, p["stop"]), "stop-loss")
            elif price >= p["take"]:
                self._close(p, price, "take-profit")

    # ---------------------------------------------------- hourly cycle
    def hourly_cycle(self):
        active = load_active().get("active", [])
        strategies = [Strategy.from_dict(a["strategy"]) for a in active]
        for sym in self.cfg["symbols"]:
            df = data.fetch_recent(sym, self.tf, 400)
            if len(df) < WARMUP_BARS + 5:
                log.warning("Not enough recent candles for %s", sym)
                continue
            feats = compute_features(df)
            probs = ml.predict_latest(self.models.get(sym), feats)
            self.recent[sym] = (feats, probs)
            price = data.fetch_price(sym)
            self.prices[sym] = price
            candle_t = int(feats["time"].iat[-1])
            self._exits(sym, feats, price)
            if self.last_candle.get(sym) != candle_t:
                self.last_candle[sym] = candle_t
                self._entries(sym, feats, probs, price, strategies)
        eq = self.account.equity(self.prices)
        self.risk.update(eq)
        self.account.record_equity(eq)
        self._monitor()
        self.state["last_hourly"] = time.time()
        self.act(f"Candle closed - decision time: checked {len(self.cfg['symbols'])} coins with "
                 f"{len(strategies)} strategies. Balance ${eq:,.2f}.", "decision")

    def _exits(self, sym, feats, price):
        now = time.time()
        for p in [p for p in self.account.positions() if p["symbol"] == sym]:
            # stop/take touched while we were not watching (e.g. PC was off)?
            # (only candles that started after the buy, so no pre-entry prices count)
            after = feats[feats["time"] / 1000 >= p["entry_time"] - 60]
            hit = None
            for row in after.itertuples():
                if row.low <= p["stop"]:
                    hit = (min(p["stop"], row.open), "stop-loss")
                    break
                if row.high >= p["take"]:
                    hit = (max(p["take"], row.open), "take-profit")
                    break
            if hit:
                self._close(p, hit[0], hit[1])
                continue
            s = Strategy.from_dict(json.loads(p["strategy_json"]))
            if s.exit and s.exit_signal(feats.tail(1)).any():
                self._close(p, price, "exit rule")
            elif now >= p["max_hold_until"]:
                self._close(p, price, "time limit")

    def _entries(self, sym, feats, probs, price, strategies):
        last = feats.tail(1)
        last_prob = probs[-1:] if probs is not None else None
        for s in strategies:
            if not s.entry_signal(last, last_prob).any():
                continue
            self.act(f"{sym} {s.id}: all buy rules met - BUY SIGNAL. Checking risk, news and AI...",
                     "signal")
            eq = self.account.equity(self.prices)
            ok, why, size = self.risk.can_open(eq, sym, s.id)
            if not ok:
                log.info("%s %s signal skipped: %s", sym, s.id, why)
                self.act(f"{sym} {s.id}: risk manager said NO - {why}.", "risk")
                continue
            blocked = self.news.blocks(sym)
            if blocked:
                self.account.message("trade", f"NEWS BLOCKED buy {sym} ({s.id}): {blocked}")
                self.act(f"{sym} {s.id}: news said NO - {blocked}.", "news")
                continue
            mult = size_multiplier(self.adjust, sym, s.id)
            if mult < 1:
                size *= mult
                self.act(f"{sym} {s.id}: learned from past losses - trade size cut to "
                         f"{int(mult * 100)}%.", "lesson")
            if size < self.cfg["risk"]["min_trade_usdt"]:
                continue
            atr = float(last["atr_pct"].iat[0])
            facts = {c.feature: fmt_value(c.feature, float(last[c.feature].iat[0])) for c in s.entry}
            ml_p = None if last_prob is None or np.isnan(last_prob[0]) else round(float(last_prob[0]), 3)
            note = f"{s.id}: " + "; ".join(f"{k}={v}" for k, v in facts.items())
            if ml_p is not None:
                note += f"; ML={ml_p:.0%}"
            if self.brain.available() and self.cfg["brain"].get("review_trades"):
                ctx = {"symbol": sym, "price": price, "strategy": s.id,
                       "rules": s.describe()["buy"], "indicator_values": facts, "ml_probability": ml_p,
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
            hold_until = time.time() + s.max_hold * self.tf_sec
            self.account.open_position(sym, s.to_dict(), price, size, stop, take, hold_until, note,
                                       news_score=self.news.score(sym))
            msg = (f"BUY {sym} ${size:,.0f} at {price:,.2f} ({s.id}). "
                   f"Stop {stop:,.2f}, target {take:,.2f}")
            log.info(msg)
            self.account.message("trade", msg)
            self.act(msg, "trade")

    def _close(self, p, price, reason):
        t = self.account.close_position(p["id"], price, reason)
        if t:
            msg = (f"SELL {t['symbol']} at {t['exit_price']:,.2f} ({reason}) "
                   f"{'PROFIT' if t['pnl'] > 0 else 'LOSS'} ${abs(t['pnl']):,.2f}")
            log.info(msg)
            self.account.message("trade", msg)
            self.act(msg, "trade")
            self.account.record_equity(self.account.equity(self.prices))
            lesson, tags = lesson_for(t)
            entry = t["entry_price"]
            mae = (min(t.get("low_seen") or entry, t["exit_price"]) / entry - 1) * 100
            mfe = (max(t.get("high_seen") or entry, t["exit_price"]) / entry - 1) * 100
            self.account.set_lesson(t["trade_id"], lesson, tags, mae, mfe)
            self.account.message("lesson", f"{t['symbol']} {t['strategy_id']}: {lesson}")
            self.act(f"Lesson - {t['symbol']} {t['strategy_id']}: {lesson}", "lesson")
            self._relearn()

    def _monitor(self):
        """Retire strategies that lose in the paper account (via the pool)."""
        self._review_pool()

    # ------------------------------------------------------------ views
    def summary(self) -> dict:
        acc = self.account.account()
        eq = self.account.equity(self.prices)
        trades = self.account.trades(500)
        today = [t for t in trades if utc_day(t["exit_time"]) == utc_day()]
        return {
            "equity": eq, "start_balance": acc["start_balance"],
            "total_return_pct": (eq / acc["start_balance"] - 1) * 100,
            "today_pnl_pct": (eq / acc["day_start_equity"] - 1) * 100 if acc["day_start_equity"] else 0,
            "open_positions": len(self.account.positions()),
            "trades_today": len(today), "total_trades": len(trades),
            "prices": self.prices, "kill_switch": bool(acc["kill_switch"]),
            "active_strategies": [a["strategy"]["id"] for a in load_active().get("active", [])],
        }
