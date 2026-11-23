@echo off
title TradBot (close this window to stop the bot)
cd /d "%~dp0"
if not exist .venv (
  echo Please run install.bat first.
  pause
  exit /b 1
)
echo Starting TradBot... the dashboard opens in your browser.
echo Keep this window open. Close it (or press Ctrl+C) to stop the bot.
".venv\Scripts\python.exe" main.py run
pause
