"""TradBot - AI crypto paper-trading bot.

Commands (run inside the project folder):
  python main.py run        start the bot + dashboard (normal use)
  python main.py check      check that everything is installed and online
  python main.py install    install / repair the libraries
  python main.py lab        run one research round in the terminal
  python main.py backtest   backtest the LIVE strategies in the terminal
  python main.py report     print the paper account summary
"""
from __future__ import annotations

import argparse
import os
import sys
import time

if sys.version_info < (3, 10):
    sys.exit("TradBot needs Python 3.10 or newer (3.12 recommended). "
             "Download it from https://www.python.org/downloads/")

RESTART_CODE = 3


def _progress(pct, msg):
    print(f"  [{pct:5.1f}%] {msg}", flush=True)


def _eco_mode():
    """Lower CPU priority: the bot gives way whenever you use your PC."""
    try:
        if sys.platform == "win32":
            import ctypes
            below_normal = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),
                                                    below_normal)
        else:
            os.nice(5)
    except Exception:
        pass


def cmd_check(cfg):
    ok = True
    print(f"Python {sys.version.split()[0]} ... OK")
    for mod in ("pandas", "numpy", "requests", "yaml"):
        try:
            __import__(mod)
            print(f"library {mod} ... OK")
        except Exception as e:
            ok = False
            print(f"library {mod} ... MISSING ({e}). Run:  python main.py install")
    try:
        __import__("lightgbm")
        print("library lightgbm (ML model) ... OK")
    except Exception:
        print("library lightgbm (ML model) ... not available - TradBot runs without the ML model")
    from tradbot import data
    try:
        for s in cfg["symbols"]:
            print(f"Binance price {s} ... {data.fetch_price(s):,.4g} OK")
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
    for w in cfg.get("_warnings", []):
        print("config warning:", w)
    print("\nAll good! Start the bot with:  python main.py run" if ok
          else "\nSome checks failed - see messages above.")
    return 0 if ok else 1


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
    run_backtest(cfg, ds, probs=res["probs"])
    return 0


def cmd_backtest(cfg):
    from tradbot.engine import run_backtest
    res = run_backtest(cfg, progress=_progress)
    if not res["strategies"]:
        print("No LIVE strategies yet. Start the bot (research finds them) or run:  python main.py lab")
    for s in res["strategies"]:
        print(f"\n{s['id']}: {s['description']['buy']}")
        for sym, p in s["per_symbol"].items():
            m = p["metrics"]
            print(f"  {sym}: {m['trades']} trades, win {m['win_rate']:.0f}%, return {m['return_pct']:+.2f}% "
                  f"(buy & hold {m['buy_hold_return_pct']:+.1f}%), max drop {m['max_dd_pct']:.1f}% "
                  f"(buy & hold {m['buy_hold_max_dd_pct']:.0f}%), Sharpe {m['sharpe']:.2f}")
    return 0


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
    return 0


def cmd_run(cfg, open_browser=True):
    from tradbot import backup
    from tradbot.dashboard import serve
    from tradbot.engine import Engine
    if backup.apply_pending():
        print("A backup was restored.")
    if cfg.get("eco_mode", True):
        _eco_mode()
    engine = Engine(cfg)
    engine.start()
    import signal

    def _stop_now(*_):            # servers stop apps with SIGTERM: stop cleanly
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _stop_now)
    except (ValueError, AttributeError):
        pass
    code = None
    try:
        code = serve(engine, open_browser=open_browser)
    finally:
        engine.stop()
        print("Bot stopped. Open trades are saved and will continue next start.", flush=True)
    return code or 0


def main() -> int:
    p = argparse.ArgumentParser(description="TradBot - AI crypto paper-trading bot")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "check", "install", "lab", "backtest", "report"])
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    p.add_argument("--server", action="store_true",
                   help="hosting mode: dashboard reachable from the internet (needs DASHBOARD_PASSWORD)")
    args = p.parse_args()

    from tradbot import installer
    if args.command == "install":
        try:
            res = installer.install(force=True)
        except RuntimeError as e:
            print(e)
            return 1
        if res["failed"]:
            print("Not installed (optional):", ", ".join(res["failed"]))
        print("Libraries OK.")
        return 0
    try:
        installer.ensure()
    except RuntimeError as e:
        print(e)
        return 1

    from tradbot.config import load_config
    from tradbot.log import get_logger
    get_logger()
    cfg = load_config()
    if args.server:
        os.environ.setdefault("TRADBOT_HOST", "0.0.0.0")
        args.no_browser = True
    if args.command == "run":
        code = cmd_run(cfg, open_browser=not args.no_browser)
        if code == RESTART_CODE and os.environ.get("TRADBOT_LAUNCHER") != "bat":
            # not started by TradBot.bat (e.g. on a server): restart ourselves
            print("Restarting TradBot...", flush=True)
            argv = [a for a in sys.argv[1:] if a != "--no-browser"] + ["--no-browser"]
            os.execv(sys.executable, [sys.executable, os.path.abspath(sys.argv[0]), *argv])
        return code
    t0 = time.time()
    code = {"check": cmd_check, "lab": cmd_lab, "backtest": cmd_backtest,
            "report": cmd_report}[args.command](cfg)
    print(f"\n(done in {time.time() - t0:.0f}s)")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
