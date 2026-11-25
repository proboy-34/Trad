@echo off
rem ================================================================
rem  TradBot - ONE-CLICK START
rem  Double-click this file. It does everything by itself:
rem   1. installs Python (only if missing)
rem   2. installs the libraries (only the first time / after updates)
rem   3. starts the bot + opens the dashboard
rem   4. restarts the bot automatically if it ever crashes
rem   5. after a one-click update (dashboard Settings) it restarts with the new version
rem ================================================================
title TradBot starter
cd /d "%~dp0"
set "SELF=%~f0"
set "PYCMD="
set "FIRST=1"
set "TRADBOT_LAUNCHER=bat"
if /i "%~1"=="--autostart" set "FIRST=0"
if /i "%~1"=="--restart" set "FIRST=0"

echo.
echo  ==========================================================
echo     TradBot - AI crypto paper trading (fake money only)
echo  ==========================================================
echo.

rem ---- already running? then just open the dashboard -----------
curl -s -o nul -m 3 http://127.0.0.1:8050/api/state >nul 2>nul
if errorlevel 1 goto :not_running
echo  TradBot is already running.
echo.
echo  If you just UPDATED the bot files, it must restart to use the new version.
choice /c YN /n /t 15 /d N /m " Restart the bot now? Press Y or N (No in 15 s): "
if errorlevel 2 goto :just_open
echo  Stopping the running bot...
taskkill /f /t /fi "WINDOWTITLE eq TradBot - keep*" >nul 2>nul
timeout /t 4 >nul
curl -s -o nul -m 3 http://127.0.0.1:8050/api/state >nul 2>nul
if errorlevel 1 goto :not_running
echo  Could not stop it automatically. Close the black TradBot window yourself, then open this file again.
pause
exit /b 1
:just_open
echo  Opening the dashboard...
start "" http://127.0.0.1:8050
timeout /t 3 >nul
exit /b 0
:not_running
title TradBot - keep this window open (close it to stop the bot)

rem ---- step 1: Python -----------------------------------------------
if exist ".venv\Scripts\python.exe" goto :have_venv
echo  [1/3] Looking for Python...
call :find_python
if defined PYCMD goto :make_venv
echo  Python not found. Installing Python 3.12 automatically.
echo  (one time only, takes 2-5 minutes, no admin needed)
call :install_python
call :find_python
if not defined PYCMD goto :python_failed
:make_venv
echo  Creating the bot's private Python environment...
%PYCMD% -m venv .venv
if errorlevel 1 goto :venv_failed
:have_venv
set "VPY=%~dp0.venv\Scripts\python.exe"

rem ---- step 2: libraries (first time only; later updates are handled by the bot) ----
if exist ".venv\libs-v2.txt" goto :libs_ok
echo  [2/3] Installing libraries (one time only, 3-10 minutes)...
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q
"%VPY%" main.py install
if errorlevel 1 goto :pip_failed
echo ok> ".venv\libs-v2.txt"
"%VPY%" main.py check
:libs_ok

rem ---- ask once: start with Windows? --------------------------------
if exist ".venv\autostart-asked.txt" goto :run
echo.
choice /c YN /n /t 30 /d N /m " Start TradBot automatically every time Windows starts? Press Y or N (No in 30 s): "
if errorlevel 2 goto :autostart_done
call :enable_autostart
:autostart_done
echo asked> ".venv\autostart-asked.txt"

rem ---- step 3: run (and auto-restart if it crashes) ----------------
:run
echo.
echo  [3/3] Starting TradBot... dashboard: http://127.0.0.1:8050
echo  Keep this window open (you can minimize it). Close it to stop the bot.
echo.
if "%FIRST%"=="1" "%VPY%" main.py run
if not "%FIRST%"=="1" "%VPY%" main.py run --no-browser
set "CODE=%errorlevel%"
set "FIRST=0"
if "%CODE%"=="3" goto :restart_now
echo.
echo  The bot stopped. It restarts automatically in 20 seconds.
echo  (To stop completely, just close this window.)
timeout /t 20
goto :run

:restart_now
echo.
echo  Restarting TradBot (update / restore)...
if exist "TradBot.bat.new" (
  move /y "TradBot.bat.new" "%SELF%" >nul
  start "TradBot" "%SELF%" --restart
  exit
)
goto :run

rem ==================================================================
:find_python
set "PYCMD="
for %%V in (312 313 311 314) do (
  if not defined PYCMD if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set PYCMD="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
  if not defined PYCMD if exist "%ProgramFiles%\Python%%V\python.exe" set PYCMD="%ProgramFiles%\Python%%V\python.exe"
)
if defined PYCMD goto :eof
py -3 -c "import sys; assert (3,10) <= sys.version_info[:2] < (3,15)" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto :eof
python -c "import sys; assert (3,10) <= sys.version_info[:2] < (3,15)" >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
goto :eof

:install_python
where winget >nul 2>nul
if errorlevel 1 goto :install_python_download
echo  Installing Python with winget...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :find_python
if defined PYCMD goto :eof
:install_python_download
echo  Downloading Python from python.org...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile '%TEMP%\tradbot-python.exe'"
if not exist "%TEMP%\tradbot-python.exe" goto :eof
echo  Installing Python (please wait)...
"%TEMP%\tradbot-python.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1 InstallLauncherAllUsers=0
del "%TEMP%\tradbot-python.exe" >nul 2>nul
goto :eof

:enable_autostart
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
> "%STARTUP%\TradBot-autostart.bat" echo @echo off
>> "%STARTUP%\TradBot-autostart.bat" echo rem Starts TradBot minimized when Windows starts. Delete this file to turn it off.
>> "%STARTUP%\TradBot-autostart.bat" echo start "TradBot" /min "%SELF%" --autostart
echo  Done: TradBot will start by itself (minimized) when Windows starts.
echo  To turn this off: press Win+R, type shell:startup, delete TradBot-autostart.bat
goto :eof

rem ---- errors ----------------------------------------------------------
:python_failed
echo.
echo  Could not install Python automatically.
echo  Please install it yourself from https://www.python.org/downloads/
echo  and TICK "Add python.exe to PATH" during install. Then open this file again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:venv_failed
echo.
echo  Could not create the Python environment. Restart your PC and try again.
pause
exit /b 1

:pip_failed
echo.
echo  Installing libraries failed. Check your internet connection and open this file again.
echo  If it keeps failing, take a screenshot of this window and send it for help.
pause
exit /b 1
