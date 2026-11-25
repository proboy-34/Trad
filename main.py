"""TradBot - AI crypto paper-trading bot.

Commands (run inside the project folder):
  python main.py run        start the bot + dashboard (normal use)
  python main.py check      check that everything is installed and online
  python main.py lab        run the Strategy Lab once in the terminal
  python main.py backtest   backtest the active strategies in the terminal
  python main.py report     print the paper account summary
"""
from __future__ import annotations

import argparse
import sys
import time

if sys.version_info < (3, 10):
    sys.exit("TradBot needs Python 3.10 or newer (3.12 recommended). "
             "Download it from https://www.python.org/downloads/")


def _ensure_libraries():
    """Hosting panels sometimes don't run 'pip install'. If a library is missing,
    install everything from requirements.txt once, then continue."""
    import importlib.util
    needed = ["numpy", "pandas", "requests", "yaml", "lightgbm"]
    if all(importlib.util.find_spec(m) for m in needed):
        return
    import subprocess
    from pathlib import Path
    req = Path(__file__).resolve().parent / "requirements.txt"
    print("Some libraries are missing - installing them now (one time, a few minutes)...", flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                           "-r", str(req)])
    importlib.invalidate_caches()


def _progress(pct, msg):
    print(f"  [{pct:5.1f}%] {msg}", flush=True)


def cmd_check(cfg):
    ok = True
    print(f"Python {sys.version.split()[0]} ... OK")
    for mod in ("pandas", "numpy", "requests", "yaml", "lightgbm"):
        try:
            __import__(mod)
            print(f"library {mod} ... OK")
        except Exception as e:
            ok = False
            print(f"library {mod} ... MISSING ({e}). Delete the .venv folder and open TradBot.bat again.")
    from tradbot import data
    try:
        for s in cfg["symbols"]:
            print(f"Binance price {s} ... {data.fetch_price(s):,.2f} OK")
        print(f"Binance server used: {data._good_host}")
    except Exception as e:
        ok = False
        print(f"Binance ... FAILED: {e}")
    from tradbot.brain import Brain
    b = Brain(cfg)
    if b.provider == "none":
        print("AI brain ... turned off (provider: none)")
    elif not b.available():
        print(f"AI brain ... no {b.provider} API key yet (add it in the dashboard Settings page)")
    else:
        reply = b.ask("Reply with exactly: OK")
        print(f"AI brain ({b.provider}) ... {'OK' if reply else 'FAILED: ' + b.last_error}")
    print("\nAll good! Start the bot with:  python main.py run" if ok
          else "\nSome checks failed - see messages above.")


def cmd_lab(cfg):
    from tradbot.brain import Brain
    from tradbot.engine import load_datasets, run_backtest
    from tradbot.lab import Lab
    from tradbot.pool import StrategyPool
    print("Downloading / updating price history...")
    ds = load_datasets(cfg)
    brain = Brain(cfg)
    pool = StrategyPool(cfg)
    res = Lab(cfg, ds, brain=brain if brain.available() else None, progress=_progress).run(
        seeds=pool.seeds(20))
    new = pool.add_round(res)
    pool.forward_test(ds, res["probs"], cfg["timeframe"])
    for m in pool.review():
        print(" ", m)
    st, c = res["stats"], pool.counts()
    print(f"\nTested {st['tested']} strategies, {st['passed_validation']} passed the unseen-data test, "
          f"{new} new added to the pool.")
    print(f"Pool: {c['testing']} in live market test, {c['live']} LIVE, {c['retired']} retired.\n")
    for e in pool.entries("live"):
        f = e["final"]
        print(e["strategy"]["id"], "-", e["description"]["buy"])
        print(f"    final honest test: return {f['return_pct']:+.2f}% (buy & hold "
              f"{f['buy_hold_return_pct']:+.1f}%), max drop {f['max_dd_pct']:.1f}%, {f['trades']} trades\n")
    run_backtest(cfg, ds)


def cmd_backtest(cfg):
    from tradbot.engine import run_backtest
    res = run_backtest(cfg, progress=_progress)
    if not res["strategies"]:
        print("No active strategies yet. Run:  python main.py lab")
    for s in res["strategies"]:
        print(f"\n{s['id']}: {s['description']['buy']}")
        for sym, p in s["per_symbol"].items():
            m = p["metrics"]
            print(f"  {sym}: {m['trades']} trades, win {m['win_rate']:.0f}%, return {m['return_pct']:+.2f}% "
                  f"(buy & hold {m['buy_hold_return_pct']:+.1f}%), max drop {m['max_dd_pct']:.1f}% "
                  f"(buy & hold {m['buy_hold_max_dd_pct']:.0f}%), Sharpe {m['sharpe']:.2f}")


def cmd_report(cfg):
    from tradbot.paper import PaperAccount
    acc = PaperAccount(cfg)
    a = acc.account()
    trades = acc.trades(1000)
    print(f"Cash: ${a['cash']:,.2f}   (started with ${a['start_balance']:,.2f})")
    print(f"Open trades: {len(acc.positions())}   Finished trades: {len(trades)}")
    if trades:
        wins = sum(1 for t in trades if t["pnl"] > 0)
        print(f"Win rate: {100 * wins / len(trades):.0f}%   Total P/L: ${sum(t['pnl'] for t in trades):,.2f}")
    print(f"Kill switch: {'ON' if a['kill_switch'] else 'off'}   Paused: {'yes' if a['paused'] else 'no'}")


def cmd_run(cfg, open_browser=True):
    from tradbot.dashboard import serve
    from tradbot.engine import Engine
    engine = Engine(cfg)
    engine.start()
    try:
        serve(engine, open_browser=open_browser)
    finally:
        engine.stop()
        print("Bot stopped. Open trades are saved and will continue next start.")


def main():
    p = argparse.ArgumentParser(description="TradBot - AI crypto paper-trading bot")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "check", "lab", "backtest", "report"])
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    p.add_argument("--server", action="store_true",
                   help="hosting mode: dashboard reachable from the internet (needs DASHBOARD_PASSWORD)")
    args = p.parse_args()
    _ensure_libraries()

    from tradbot.config import load_config
    from tradbot.log import get_logger
    get_logger()
    cfg = load_config()
    t0 = time.time()
    if args.server:
        import os
        os.environ.setdefault("TRADBOT_HOST", "0.0.0.0")
        args.no_browser = True
    if args.command == "run":
        cmd_run(cfg, open_browser=not args.no_browser)
    else:
        {"check": cmd_check, "lab": cmd_lab, "backtest": cmd_backtest, "report": cmd_report}[args.command](cfg)
        print(f"\n(done in {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
