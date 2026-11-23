"""The Engine - runs the whole bot by itself, 24/7.

 every minute : check stop-loss / take-profit of open trades (live price)
 every hour   : when a candle closes -> exits, then new buy decisions
 every day    : AI daily report
 every week   : Strategy Lab re-research + ML retrain (auto_run_weekday)
 first start  : downloads history and runs the Strategy Lab automatically
"""
from __future__ import annotations

import json
import threading
import time
import traceback

import numpy as np

from . import backtest, data, ml
from .brain import Brain
from .config import DATA_DIR
from .features import WARMUP_BARS, compute_features, fmt_value
from .lab import Lab, load_active, save_active
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
        self._last_lab_check_day = None

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

    def run_lab_job(self) -> bool:
        def work(progress):
            progress(1, "Downloading latest price history...")
            datasets = load_datasets(self.cfg)
            brain = self.brain if self.brain.available() else None
            res = Lab(self.cfg, datasets, brain=brain, progress=progress).run()
            self.models = {s: ml.load_model(s) for s in self.cfg["symbols"]}
            st = res["stats"]
            self.account.message(
                "lab", f"Strategy Lab tested {st['tested']} strategies: {st['passed_validation']} "
                       f"passed the unseen-data test, {st['chosen']} now paper trading.")
            run_backtest(self.cfg, datasets)
        return self.start_job("Strategy Lab", work)

    def run_backtest_job(self) -> bool:
        return self.start_job("Backtest", lambda p: run_backtest(self.cfg, progress=p))

    # ------------------------------------------------------------- loop
    def start(self):
        self.state["running"] = True
        threading.Thread(target=self._loop, daemon=True, name="engine").start()

    def stop(self):
        self._stop.set()
        self.state["running"] = False

    def _loop(self):
        log.info("Engine started (paper trading). Press Ctrl+C to stop.")
        if not load_active().get("active") and not load_active().get("updated"):
            log.info("First start: running the Strategy Lab to find strategies...")
            self.account.message("system", "First start: researching strategies (takes a few minutes).")
            self.run_lab_job()
        next_hourly = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if now >= next_hourly:
                    self.hourly_cycle()
                    next_hourly = (now // self.tf_sec + 1) * self.tf_sec + 20   # 20s after close
                    self.state["next_hourly"] = next_hourly
                else:
                    self.fast_check()
                self._scheduled_tasks()
                self.state["last_error"] = ""
            except data.DataError as e:
                self.state["last_error"] = str(e)
                log.warning("%s (will retry in 1 minute)", e)
            except Exception as e:
                self.state["last_error"] = f"{type(e).__name__}: {e}"
                log.error("Engine error: %s\n%s", e, traceback.format_exc())
            self._stop.wait(60)

    def _scheduled_tasks(self):
        t = time.gmtime()
        today = utc_day()
        lc = self.cfg["lab"]
        if (t.tm_wday == lc["auto_run_weekday"] and t.tm_hour == lc["auto_run_hour_utc"]
                and self._last_lab_check_day != today):
            self._last_lab_check_day = today
            updated = load_active().get("updated") or 0
            if time.time() - updated > 5 * 86400:
                log.info("Weekly Strategy Lab re-research starting")
                self.run_lab_job()
        if (self.cfg["brain"].get("daily_report") and t.tm_hour == 0 and t.tm_min >= 5
                and self._last_report_day != today and self.brain.available()):
            self._last_report_day = today
            self.brain.daily_report(self.summary())

    # ----------------------------------------------------- fast checks
    def fast_check(self):
        """Every minute: stop-loss / take-profit using the live price."""
        for p in self.account.positions():
            price = data.fetch_price(p["symbol"])
            self.prices[p["symbol"]] = price
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
            eq = self.account.equity(self.prices)
            ok, why, size = self.risk.can_open(eq, sym, s.id)
            if not ok:
                log.info("%s %s signal skipped: %s", sym, s.id, why)
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
                    continue
                if reason:
                    note += f"; AI: {reason}"
            stop = price * (1 - s.stop_atr * atr)
            take = price * (1 + s.take_atr * atr)
            hold_until = time.time() + s.max_hold * self.tf_sec
            self.account.open_position(sym, s.to_dict(), price, size, stop, take, hold_until, note)
            msg = (f"BUY {sym} ${size:,.0f} at {price:,.2f} ({s.id}). "
                   f"Stop {stop:,.2f}, target {take:,.2f}")
            log.info(msg)
            self.account.message("trade", msg)

    def _close(self, p, price, reason):
        t = self.account.close_position(p["id"], price, reason)
        if t:
            msg = (f"SELL {t['symbol']} at {t['exit_price']:,.2f} ({reason}) "
                   f"{'PROFIT' if t['pnl'] > 0 else 'LOSS'} ${abs(t['pnl']):,.2f}")
            log.info(msg)
            self.account.message("trade", msg)
            self.account.record_equity(self.account.equity(self.prices))

    def _monitor(self):
        """Retire strategies that fail in live paper trading."""
        mc = self.cfg["monitor"]
        data_ = load_active()
        stats = self.account.strategy_stats()
        keep = []
        for a in data_.get("active", []):
            st = stats.get(a["strategy"]["id"])
            if (st and st["trades"] >= mc["retire_after_trades"]
                    and st["profit_factor"] < mc["retire_if_profit_factor_below"]):
                self.account.message("lab", f"Retired strategy {a['strategy']['id']}: profit factor "
                                            f"{st['profit_factor']:.2f} after {st['trades']} paper trades.")
                continue
            keep.append(a)
        if len(keep) != len(data_.get("active", [])):
            data_["active"] = keep
            save_active(data_)

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
