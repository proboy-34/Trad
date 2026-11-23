@echo off
title TradBot installer
cd /d "%~dp0"
echo.
echo === TradBot installer ===
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed or not on PATH.
  echo Install Python 3.12 from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)
if not exist .venv (
  echo Creating private Python environment in .venv ...
  python -m venv .venv
  if errorlevel 1 ( echo Could not create .venv & pause & exit /b 1 )
)
echo Installing libraries (first time can take a few minutes) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 ( echo. & echo Install failed - check your internet and try again. & pause & exit /b 1 )
echo.
echo Checking everything ...
".venv\Scripts\python.exe" main.py check
echo.
echo Done! Double-click start.bat to run the bot.
pause
