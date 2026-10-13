@echo off
REM ============================================================
REM  MT5 Short-TF Screener - one-click start
REM
REM  Double-click this to start the dashboard + its watchdog.
REM  Uses the project-local virtual environment in .\.venv
REM  (created from requirements.txt). No console window is shown.
REM
REM  If .venv is missing, this will rebuild it from requirements.txt.
REM  Open http://127.0.0.1:8765/ in your browser after a few seconds.
REM ============================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\pythonw.exe"

REM --- First run: build the local venv and install dependencies ---
if not exist "%PY%" (
    echo .
    echo [start_app] Local venv not found - creating .\.venv and installing
    echo [start_app] requirements.txt (one-time step, may take a minute)...
    echo.
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --no-input --disable-pip-version-check -q -r requirements.txt
    echo.
)

REM --- Guard: a watchdog should already be the only thing running ---
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*dashboard*' -and $_.CommandLine -like '*NewTrading*' } | ForEach-Object { Write-Host ('[start_app] already running: ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"
echo.

REM --- Launch the watchdog (it starts + guards dashboard.py) ---
start "" "%PY%" "dashboard_watchdog.py"
echo [start_app] Dashboard watchdog launched. Opening in a moment...
echo [start_app] Watch the page at:  http://127.0.0.1:8765/
timeout /t 3 >nul
start "" "http://127.0.0.1:8765/"
endlocal
