# TradBot changelog

The newest version is at the top. The dashboard shows the newest section once after an update.

## 2.0 - bigger, safer, easier
- Trailing stop: strategies can now follow the price up and lock in profit (tested honestly in the backtester too)
- Real-money readiness checklist on the Overview (7 checks - all must be green for weeks)
- Balance chart now shows "just holding the coins" next to the bot, so you see if it is really better
- Phone alerts through Telegram (free): every buy and sell with the reason, daily summary, kill-switch alerts
- One-click updates: Settings -> Install update from ZIP. Data, keys and settings are kept, the bot restarts by itself
- Automatic daily backups, plus Download / Restore backup buttons
- Trade journal: every trade shows why it was bought, why it was sold and the lesson
- Internet saver: the live scan downloads ~99% less data; unchanged news is not downloaded again
- Eco mode: the bot runs at low CPU priority, so your PC stays fast
- Safer: max 2 new trades per hour, broader research (no flood of near-copies), probation prefers strategies that also passed the final test
- Robust: one coin with bad data no longer stops the others; a typo in config.yaml can't crash the bot; optional libraries can't block the start
- Cleaner: the AI Brain tab is now the AI coach on the Live page; no more repeated scan lines in the timeline; Hugging Face-only code removed
- System health panel in Settings (internet, coins, AI, news, alerts, disk, backups)

## 1.6
- Version check between the dashboard and the running bot; 6 probation strategies

## 1.5
- Non-stop research, strategy pool with live market test, promotion and retirement, 6 coins
