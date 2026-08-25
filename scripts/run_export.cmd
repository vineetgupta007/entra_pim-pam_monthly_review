@echo off
REM ===========================================================================
REM Phase A - unattended monthly export. For Windows Task Scheduler.
REM
REM DO NOT SCHEDULE THIS UNTIL BOTH OF THESE COME BACK GREEN:
REM     python scripts\check_auth.py
REM     python scripts\check_auth.py --auth-mode secret_env
REM The second one matters: interactive and unattended use DIFFERENT permission
REM types on the app registration, so one working proves nothing about the other.
REM
REM Scheduled task settings that matter:
REM   - Run whether user is logged on or not
REM   - Start in: the repo root (this script also cds there itself)
REM   - Trigger: monthly, day 2, early. Audit retention is short - commonly 30
REM     days on P1/P2 - so a missed month is gone permanently.
REM
REM CREDENTIAL GOTCHA: Task Scheduler does NOT inherit environment variables
REM from your interactive shell. Setting $env:ENTRA_CLIENT_SECRET in PowerShell
REM does nothing for a scheduled run. Either make it persistent:
REM     setx ENTRA_CLIENT_SECRET "<secret>"        (user scope, survives reboot)
REM or better, avoid the secret entirely by switching config.json to
REM     "mode": "certificate"
REM Best of all, run the export from Azure Automation with a managed identity,
REM which removes the stored credential from this machine altogether.
REM ===========================================================================

setlocal

REM Repo root is this script's parent directory.
cd /d "%~dp0.."

REM --- adjust if your interpreter lives elsewhere -----------------------------
set PYTHON=.venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python
REM ---------------------------------------------------------------------------

REM Target the month that just ended, not the current one.
for /f %%i in ('%PYTHON% -c "from datetime import date;d=date.today().replace(day=1);import datetime;p=d-datetime.timedelta(days=1);print(f'{p.year:04d}-{p.month:02d}')"') do set MONTH=%%i

if "%MONTH%"=="" (
    echo Could not determine the target month. Is Python on PATH?
    exit /b 2
)

set LOGDIR=logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set LOG=%LOGDIR%\export-%MONTH%.log

echo ====================================================== >> "%LOG%"
echo Export run started %DATE% %TIME% for %MONTH% >> "%LOG%"

"%PYTHON%" scripts\run_month.py --month %MONTH% --export-only --auth-mode secret_env >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

if %RC% NEQ 0 (
    echo FAILED with exit code %RC% - see %LOG% >> "%LOG%"
    echo Export FAILED for %MONTH%. See %LOG%
    exit /b %RC%
)

echo Completed %DATE% %TIME% >> "%LOG%"
echo Export complete for %MONTH%. Files are in the month's input folder.
echo Next: open Cowork and ask Claude to run the monthly PIM review.
exit /b 0
