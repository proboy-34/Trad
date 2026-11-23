# TradBot — AI crypto paper-trading bot

TradBot invents its own trading strategies, tests them honestly, and trades
them automatically with **fake money** (paper trading) on live Binance prices.
It has a dashboard that opens in your browser.

> **Safety first.** This version can only paper trade. There is no real-money
> code at all. Real money comes later, only after the bot proves itself
> (see [When can I use real money?](#when-can-i-use-real-money)).

---

## What it does (in simple words)

| Part | What it does |
|---|---|
| **Data collector** | Downloads 1-hour candles for BTC/USDT and ETH/USDT from Binance (public data, no account needed). |
| **Strategy Lab** | Invents ~300 strategy ideas per round, tests them on past data with fees, improves the best ones over 4 rounds (~1,000 strategies), then re-tests the winners on data they have **never seen**. Only strategies that make money there are used. Re-runs automatically every week. |
| **ML model** | A LightGBM model estimates "chance the price rises in the next 12 hours". Strategies can require it to agree. |
| **AI brain** (Gemini or Claude) | Suggests new strategy ideas, reviews every buy (it can **block** a trade, never force one) and writes a daily report. Optional. |
| **Risk manager** | Max 25% of the balance per trade, max 3 open trades, a stop-loss on every trade, a 5% daily loss stop, and a 20% kill switch. Nothing can skip these rules. |
| **Paper account** | Fake $10,000. Every trade pays realistic fees and slippage. Saved in a database, so nothing is lost if your PC turns off. |
| **Dashboard** | Balance, open trades, chart with buy/sell markers, strategies explained in plain English, backtest, AI messages and settings. |

---

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
  - **Overview:** balance, today's result, open trades, latest events.
  - **Strategies:** what each strategy does, in plain English, with honest test results.
  - **Trades:** every finished trade and why it was sold.
  - **AI Brain:** the AI's reviews and daily reports.
- If the PC turns off, just run `TradBot.bat` again (or it starts by itself if you chose auto-start). Open trades are remembered, and
  stop-losses that were hit while it was off are applied.

### Commands (optional, for the terminal)
Open the folder, click the address bar, type `cmd` and press Enter. Then:

```bash
.venv\Scripts\python main.py check
```
Checks the install, the Binance connection and the AI key.

```bash
.venv\Scripts\python main.py lab
```
Runs the Strategy Lab once and prints the results.

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

Only when **all** of these are true:

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
| `symbols` | BTC/USDT, ETH/USDT | Coins to trade |
| `risk.max_position_pct` | 25 | Max % of the balance per trade |
| `risk.daily_loss_limit_pct` | 5 | Stop for the day after this loss |
| `risk.kill_switch_drawdown_pct` | 20 | Stop everything after this drop from the peak |
| `lab.population` / `lab.generations` | 300 / 4 | Lab size (smaller = faster on a weak PC) |
| `ml.threads` | 2 | CPU threads for the ML model |
| `brain.provider` | gemini | `gemini`, `claude` or `none` |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Could not install Python automatically" | Install Python 3.12 from python.org and tick **"Add python.exe to PATH"**, then open `TradBot.bat` again. |
| "Installing libraries failed" | Check your internet and open `TradBot.bat` again. Python 3.10–3.14 is supported. |
| "Could not reach Binance" | Check your internet. The bot automatically tries Binance's backup data server. It retries every minute. |
| Dashboard doesn't open | Open <http://127.0.0.1:8050> yourself. Make sure the black TradBot window is still open. |
| "Port 8050 in use" | Another copy is running. Close it, or change `dashboard.port` in `config.yaml`. |
| "No strategy passed the tests" | Normal in some markets. The bot doesn't trade, which is the safe choice. The Lab retries weekly, or click **Run Strategy Lab now**. |
| Gemini "API key is not valid" | Copy the key again from aistudio.google.com, paste it in Settings, and click Save. |
| Gemini "limit reached" | The free tier is used up for now. The bot keeps trading without the AI and retries later. |
| Kill switch is ON | The balance fell 20% from its peak. Review the trades, then **Settings → Reset kill switch**. |
| PC is slow during the Lab | Lower `lab.population` (e.g. 150) and `ml.threads` (1) in `config.yaml`. |

Logs are saved in `data/bot.log`. If you need help, share that file (it contains no keys).

---

## 24/7 on a server (later)

Your PC must stay on for the bot to trade. For the 4–8 week paper test, a small server is
better. See **[docs/SERVER_SETUP.md](docs/SERVER_SETUP.md)** (Oracle Cloud free tier, or a
~$5/month VPS paid with USDT).

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
  lab.py             Strategy Lab (invent, test, evolve, validate, choose)
  ml.py              LightGBM model with walk-forward training
  brain.py           AI brain (Gemini / Claude)
  risk.py            risk manager
  paper.py           paper account database
  engine.py          runs everything automatically
  dashboard.py       dashboard web server
  web/               dashboard page (HTML/CSS/JS + chart library)
tests/               safety tests (python -m pytest)
data/                created automatically: prices, database, models, log
```

The chart library is [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)
(Apache 2.0, license in `tradbot/web/vendor/`).

---

*Not financial advice. Crypto is very risky. Never trade money you can't afford to lose.*
