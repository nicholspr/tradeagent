@echo off
rem Runs the full TradeAgent app locally at http://localhost:8000/ (analyses, approvals, notifications).
rem Safety: DRY_RUN is forced on (no order ever reaches eToro) and the scheduler is off
rem (nothing runs on its own, so it can't duplicate the VPS's scheduled analyses or spend credits).
rem Settings come from a git-ignored .env file (KEY=value per line).
cd /d "%~dp0"

set "DATA_DIR=%~dp0data"
set "ETORO_ENV=demo"
set "PUBLIC_BASE_URL=http://localhost:8000"
set "APP_SECRET=local-dev-secret"

if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do if not "%%B"=="" set "%%A=%%B"
)

rem Forced after .env so a local run can never place orders or run on a schedule.
set "DRY_RUN=true"
set "SCHEDULER=off"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment and installing everything ^(first run takes a few minutes^)...
    py -3.13 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto :fail
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 goto :fail
)

echo.
echo TradeAgent running locally: TEST MODE, scheduler off, %ETORO_ENV% account.
echo Open http://localhost:8000/   -   Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m uvicorn app.web:app --reload --port 8000
goto :eof

:fail
echo Setup failed. Check that Python 3.11+ and git are installed and on your PATH.
pause
