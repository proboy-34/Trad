# TradBot — AI crypto paper-trading bot

TradBot invents its own trading strategies, tests them honestly, and trades
them automatically with **fake money** (paper trading) on live Binance prices.
It has a dashboard that opens in your browser.

**Version 2** — see [CHANGELOG.md](CHANGELOG.md) for what's new.

> **Safety first.** This version can only paper trade. There is no real-money
> code at all. Real money comes later, only after the bot proves itself
> (see [When can I use real money?](#when-can-i-use-real-money)).

---

## What it does (in simple words)

TradBot never stops working. Three things run at the same time:

| Part | What it does |
|---|---|
| **Live scan (every 30 s)** | Gets live prices for **BTC, ETH, SOL, BNB, XRP and DOGE**, checks stop-loss and target of open trades, and shows for every live strategy **how many of its buy rules are met right now** and what it is still waiting for. |
| **Non-stop research** | Round after round (about one per minute), it invents ~150 new strategies, evolves more from the best known ones, and tests them on 3 years of history with fees. Every strategy that also passes on history it never saw joins the **strategy pool**. |
| **Live market test** | Every strategy in the pool (up to 300) is shadow-traded on the **new candles that appeared after it was discovered**, which is a real test on data it couldn't have seen. Every hour the results are updated. |
| **Promotion / retirement** | Strategies that make money in the live market test are promoted to **LIVE** (up to 10) and trade the paper account. Strategies that lose are **retired**. Until enough are proven, the 6 best new ones trade on **probation** (at half size). |
| **Trading decisions** | When each hourly candle closes, LIVE strategies decide to buy or not (same rules as the tests, so no peeking). The **risk manager** limits every trade: max 25% of the balance, max 3 open trades, stop-loss always, 5% daily loss stop, 20% kill switch. |
| **ML model** | A LightGBM model estimates "chance the price rises in the next 12 hours". Strategies can require it to agree. |
| **AI brain** (Gemini or Claude) | Suggests strategy ideas every few research rounds, reviews every buy (it can **block** a trade, never force one) and writes a daily report. Optional. |
| **Dashboard** | Heartbeat, research counters, live view of what the bot sees, live activity feed, balance, trades, the whole strategy pipeline, backtest and settings. |

## How a trade works (and how it should make money)

1. **Research** finds strategies that made money on 3 years of history **and** on history they never saw.
2. **Live market test:** each one is shadow-traded on new candles. Only winners go **LIVE**.
3. **Signal:** when an hourly candle closes and **all** buy rules of a LIVE strategy are met, that's a buy signal.
4. **Checks:** the **learned trade filter** (when switched on), the **risk manager** (size, max trades,
   daily loss), the **news check** (very bad news on that coin or the whole market = no buy) and the
   **AI review** (may block) must all say yes. Strategies still on **probation** trade at **half size**.
5. **Buy** with a stop-loss and a target set at once. Trade size is reduced automatically for coins or
   strategies that have been losing (learned from the journal).
6. **Sell** at the target, the stop-loss, an exit rule or a time limit (checked every 30 seconds).
   Many strategies also use a **break-even stop**: once the price is up enough, the stop moves to the buy
   price, so a winning trade can't turn into a real loss. (The backtester tests this too, pessimistically.)
7. **Lesson:** the bot records the best and worst price during the trade and writes what happened
   (e.g. "was up 3% but reversed" or "bad entry timing"). These lessons change future trade sizes,
   go to the AI when it invents strategies, and feed the AI's daily coach report.

**Why this can make money:** not every trade wins. Strategies are chosen so the **wins are bigger than the losses**
(e.g. target = 4 × ATR, stop = 2 × ATR) and losses are cut fast. Over many trades, a small edge adds up,
while the risk rules keep any single mistake small. No bot can promise profit, which is why it paper trades first.

## The learned trade filter

A second ML model learns from thousands of trades that the pool's strategies made on data they were
not built on, and tries to recognise signals that usually fail. It is trained on older trades and
checked on newer ones it never saw; it **only switches on if it clearly improves those newer trades**.
If it can't, it stays OFF (that is the honest result, not a bug). It retrains every 6 hours as
more trades come in. Its status is shown on the Live page under "What the bot learned".

## Safety details

- **Max 2 new trades per hour:** crypto coins usually move together, so many buys at once would be the same bet.
- **Trailing stop / break-even:** many strategies move their stop-loss up as the price rises, locking in profit.
- **One bad coin can't stop the others:** if a coin has no data (delisted, timeout), the rest keep trading.
- **Typos can't crash it:** a broken `config.yaml` falls back to safe defaults and the dashboard tells you.
- **No double buys after a crash:** the bot remembers on disk which candle it already decided on.
- **Breadth test:** a strategy must make money on at least half of the coins on unseen data, not just
  one lucky coin.
- **Benchmark:** the Overview shows what simply holding the 6 coins would have made since the start,
  so you can see if the bot is really better.

## The Live page

Click **Live** in the sidebar. It is the bot's mission control:
- **The station map:** Research → Live market test → LIVE strategies → Scanning → Signals → Risk · News · AI → Trades → Lessons.
  A station glows when it is doing something right now, and the line to the next one starts flowing.
- **Timeline:** every step, newest on top (scans, research rounds, signals, checks, trades, lessons).
- **News mood:** a score per coin from −1 (bad) to +1 (good), plus the latest headlines.
- **AI coach:** the AI's daily review (mistakes and what to watch) and its latest trade reviews.
- **What the bot learned:** its win/loss record, most common mistake, and which coins or strategies now trade smaller.

## Phone alerts (Telegram, free, optional)

Get every buy and sell (with the reason), the daily summary and kill-switch alerts on your phone:
1. In Telegram open **@BotFather**, send `/newbot`, choose any name, copy the token.
2. Dashboard → **Settings → Phone alerts**: paste the token, click **Connect**.
3. Open your new bot in Telegram, press **Start**, then click **Connect** again. A test message arrives.

## Updates and backups (no more copying files)

- **Update:** download the newest ZIP from GitHub (Code → Download ZIP), then dashboard →
  **Settings → Install update from ZIP**. Your data, keys and settings are kept, the old code is saved
  in `data\backups`, and the bot restarts by itself with the new version.
- **Backups:** made automatically every day (last 7 kept in `data\backups`). **Download backup** gives you a
  file to keep on Google Drive / USB; **Restore backup** puts one back (the current data is saved first).

## Install and run on Windows: ONE file does everything

1. **Download TradBot:** while logged in to GitHub, open
   <https://github.com/proboy-34/Trad/archive/refs/heads/claude/gifted-ptolemy-4nosha.zip>
   and the ZIP downloads. Right-click it → **Extract All**. You can move the folder anywhere, e.g. `C:\TradBot`.
2. **Double-click `TradBot.bat`.** That's all. It automatically:
   - installs **Python** if your PC doesn't have it (no admin needed)
   - installs the libraries (first time only, 3–10 minutes)
   - asks once if TradBot should **start by itself when Windows starts** (press **Y** or **N**)
   - starts the bot and opens the dashboard at <http://127.0.0.1:8050>
   - **restarts the bot by itself** if it ever crashes
3. **First start:** the bot downloads 3 years of prices and runs the Strategy Lab
   (1–3 minutes, watch the progress bar). After that it decides and trades by itself every hour.

**Keep the black window open** (you can minimize it). Closing it stops the bot.
Double-clicking `TradBot.bat` again while the bot runs just opens the dashboard.

> If Windows shows "Windows protected your PC", click **More info → Run anyway**.
> This happens because the file is new. You can open `TradBot.bat` in Notepad to read exactly what it does.
>
> **Turn off auto-start:** press `Win + R`, type `shell:startup`, and delete `TradBot-autostart.bat`.

### Optional: turn on the free AI brain (Gemini)
1. Open <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key** and copy it.
3. In the dashboard, go to **Settings**, paste the key in "Gemini API key", click **Save**, then **Test AI**.

The key is stored only on your PC in a file called `.env`. Never share it or upload it.
The bot limits itself to 20 AI calls per hour to stay inside the free tier.

---

## Daily use

- Run `TradBot.bat` and leave it running (or let it auto-start with Windows). The bot works 24/7 while the window is open.
- Check the dashboard once a day (5 minutes):
  - **Overview:** balance vs just holding the coins, the real-money readiness checklist, what the bot sees, open trades.
  - **Live:** the timeline of everything the bot did, AI coach, news, lessons.
  - **Journal:** every finished trade: why it bought, why it sold, and the lesson.
  - **Strategies:** what each strategy does, in plain English, with honest test results.
- If the PC turns off, just run `TradBot.bat` again (or it starts by itself if you chose auto-start). Open trades are remembered, and
  stop-losses that were hit while it was off are applied.

### Commands (optional, for the terminal)
Open the folder, click the address bar, type `cmd` and press Enter. Then:

```bash
.venv\Scripts\python main.py check
```
Checks the install, the Binance connection and the AI key.

```bash
.venv\Scripts\python main.py install
```
Repairs / re-installs the libraries.

```bash
.venv\Scripts\python main.py lab
```
Runs one research round and prints the results.

```bash
.venv\Scripts\python main.py backtest
```
Backtests the active strategies and prints the results.

```bash
.venv\Scripts\python main.py report
```
Prints the paper account summary.

---

## How to read the results

Each strategy shows three test periods:

| Period | Meaning |
|---|---|
| **Research** | Old history where the strategy was found. It always looks good here. |
| **Validation** | Newer history it never saw. It had to **make money** and beat buy-and-hold here to be chosen. |
| **Final test** | The newest history, never used for choosing. **This is the honest number.** |

- **Sharpe:** profit compared to how bumpy the ride was (above 1 is good).
- **Max drop (drawdown):** the biggest fall from a peak.
- **ML AUC:** how good the ML model is (0.50 = coin flip; 0.55 = a small real edge).

### What the tests showed while building (November 2026)
- The 3 years of data contain a big rise (2024–Sep 2025) and then a big fall
  (Sep 2025–now: BTC −36%, ETH −51%).
- The Strategy Lab tested about 1,000 strategies per run and kept finding the same few
  strategy families, which suggests real patterns rather than luck.
- On the **final unseen test** (the last ~7 months, a falling market), the chosen
  strategies ended between **−1.8% and 0%**, while buy-and-hold lost **−21%**.
  So the bot protected money very well, but it **has not proven it can make a profit
  yet**. That is exactly why we paper trade first.

---

## When can I use real money?

The **Real-money readiness** card on the Overview checks this for you automatically. Only when **all** of these are true:

- [ ] At least **4–8 weeks** of paper trading, with **30+ finished trades**
- [ ] Paper profit is **positive after fees**
- [ ] It beat buy-and-hold over the same weeks
- [ ] The biggest drop stayed **under 20%**, and the kill switch never fired
- [ ] Paper results look similar to the backtest (if much worse → overfitting)
- [ ] No crashes or strange trades in the Log

Then real-money support gets built as a separate, careful step: start with a tiny amount
($50–100), use a Binance API key **with withdrawals disabled** and locked to your IP address.

---

## Settings (`config.yaml`)

Open `config.yaml` in Notepad. Every setting has a comment explaining it.
Restart the bot after changing it. The most useful ones:

| Setting | Default | Meaning |
|---|---|---|
| `symbols` | BTC, ETH, SOL, BNB, XRP, DOGE | Coins to trade |
| `eco_mode` | true | Low CPU priority, so your PC stays fast |
| `risk.max_new_per_candle` | 2 | Max new trades per hour |
| `notify.daily_hour` | 21 | Local time of the daily summary |
| `risk.max_position_pct` | 25 | Max % of the balance per trade |
| `risk.daily_loss_limit_pct` | 5 | Stop for the day after this loss |
| `risk.kill_switch_drawdown_pct` | 20 | Stop everything after this drop from the peak |
| `scan_seconds` | 30 | How often the live scan runs |
| `research.round_population` | 150 | New ideas per research round |
| `research.pause_seconds` | 20 | Break between research rounds (bigger = cooler PC) |
| `pool.max_live` | 10 | Max strategies trading the paper account |
| `ml.threads` | 2 | CPU threads for the ML model |
| `brain.provider` | gemini | `gemini`, `claude` or `none` |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Could not install Python automatically" | Install Python 3.12 from python.org and tick **"Add python.exe to PATH"**, then open `TradBot.bat` again. |
| "Installing libraries failed" | Check your internet and open `TradBot.bat` again. Python 3.10–3.14 is supported. If only the ML library fails, TradBot still runs without the ML model (Settings → System health shows it). |
| "Update not finished" banner | The running bot is older than the page. Settings → **Restart bot**, or close the black window and open `TradBot.bat`. |
| A yellow note "Setting fixed: …" | A value in `config.yaml` was impossible or had a typo; the bot used a safe value instead. Fix the file or download it again. |
| "Could not reach Binance" | Check your internet. The bot automatically tries Binance's backup data server. It retries every minute. |
| Dashboard doesn't open | Open <http://127.0.0.1:8050> yourself. Make sure the black TradBot window is still open. |
| "Port 8050 in use" | Another copy is running. Close it, or change `dashboard.port` in `config.yaml`. |
| No trades for a while | Normal: LIVE strategies only buy when all their rules are met. Watch **What the bot sees right now** to see how close each one is. |
| Gemini "API key is not valid" | Copy the key again from aistudio.google.com, paste it in Settings, and click Save. |
| Gemini "limit reached" | The free tier is used up for now. The bot keeps trading without the AI and retries later. |
| Kill switch is ON | The balance fell 20% from its peak. Review the trades, then **Settings → Reset kill switch**. |
| PC is slow | Research runs non-stop. In `config.yaml` raise `research.pause_seconds` (e.g. 120), lower `research.round_population` (e.g. 80) and `ml.threads` (1), or click **Pause research** on the Strategies page. |

Logs are saved in `data/bot.log`. If you need help, share that file (it contains no keys).

---

## 24/7 on a server (later)

Your PC must stay on for the bot to trade. For the 4–8 week paper test, a small server is
better. See **[docs/SERVER_SETUP.md](docs/SERVER_SETUP.md)**: a free bot-hosting panel (TradBot uses only
~170 MB RAM), Oracle Cloud free tier, or a ~$5/month VPS paid with USDT. On a server, start it with
`python main.py run --server` and set `DASHBOARD_PASSWORD`.

---

## Project structure

```
main.py              start here (commands)
config.yaml          all settings
TradBot.bat          Windows one-click: installs everything + runs the bot
tradbot/
  data.py            Binance price download (with backup server)
  features.py        indicators (RSI, MACD, ATR, moving averages ...)
  strategy.py        strategy rules + the strategy inventor
  backtest.py        honest backtester (next-candle entry, fees, worst-case fills)
  lab.py             one research round (invent, test, evolve, validate)
  pool.py            strategy pool: live market test, promotion, retirement
  news.py            crypto news reader + mood score (blocks buys on bad news)
  learn.py           lessons from every trade + learned trade sizes
  tradefilter.py     learned trade filter (switches on only if it helps)
  ml.py              LightGBM model with walk-forward training
  brain.py           AI brain (Gemini / Claude)
  risk.py            risk manager
  paper.py           paper account database
  engine.py          runs everything automatically
  notify.py          Telegram phone alerts
  backup.py          daily backups, download / restore
  updater.py         one-click update from a TradBot ZIP
  installer.py       installs libraries (optional ones can't block the start)
  dashboard.py       dashboard web server
  web/               dashboard page (HTML/CSS/JS + chart library)
tests/               safety tests (python -m pytest) - also run on Windows + Linux by GitHub
data/                created automatically: prices, database, models, log
```

The chart library is [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)
(Apache 2.0, license in `tradbot/web/vendor/`).

---

*Not financial advice. Crypto is very risky. Never trade money you can't afford to lose.*
