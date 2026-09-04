@echo off
REM ===========================================================================
REM Phase A - unattended monthly export. For Windows Task Scheduler.
REM
REM Reads auth.mode from scripts\config.json and handles all three unattended
REM modes from this one script - secret_env, certificate, and token_passthrough.
REM No copy of this file per mode; change config.json's auth.mode and this
REM script adapts. (interactive is refused below - it needs a browser, which
REM Task Scheduler cannot provide.)
REM
REM DO NOT SCHEDULE THIS UNTIL BOTH OF THESE COME BACK GREEN:
REM     python scripts\check_auth.py
REM     python scripts\check_auth.py --auth-mode <same mode as config.json>
REM The second one matters: interactive and unattended use DIFFERENT permission
REM types on the app registration, so one working proves nothing about the other.
REM
REM Scheduled task settings that matter:
REM   - Run whether user is logged on or not
REM   - Start in: the repo root (this script also cds there itself)
REM   - Trigger: monthly, day 2, early. Audit retention is short - commonly 30
REM     days on P1/P2 - so a missed month is gone permanently.
REM
REM CREDENTIAL GOTCHA (secret_env): Task Scheduler does NOT inherit environment
REM variables from your interactive shell. Setting $env:ENTRA_CLIENT_SECRET in
REM PowerShell does nothing for a scheduled run. Either make it persistent:
REM     setx ENTRA_CLIENT_SECRET "<secret>"        (user scope, survives reboot)
REM or switch config.json to "mode": "certificate" or "token_passthrough" - both
REM avoid a standing secret on this machine. token_passthrough also avoids a
REM plaintext private key on disk (see scripts\Get-GraphToken.ps1); its cost is
REM an extra PowerShell hop, handled automatically below.
REM
REM Best of all: run the export from Azure Automation with a managed identity,
REM which removes the stored credential from this machine altogether.
REM ===========================================================================

setlocal enabledelayedexpansion

REM Repo root is this script's parent directory.
cd /d "%~dp0.."

REM --- adjust if your interpreter lives elsewhere -----------------------------
set PYTHON=.venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python
REM ---------------------------------------------------------------------------

set LOGDIR=logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM Target the month that just ended, not the current one.
for /f %%i in ('%PYTHON% -c "from datetime import date;d=date.today().replace(day=1);import datetime;p=d-datetime.timedelta(days=1);print(f'{p.year:04d}-{p.month:02d}')"') do set MONTH=%%i

if "%MONTH%"=="" (
    echo Could not determine the target month. Is Python on PATH?
    exit /b 2
)

set LOG=%LOGDIR%\export-%MONTH%.log
echo ====================================================== >> "%LOG%"
echo Export run started %DATE% %TIME% for %MONTH% >> "%LOG%"

REM --- read auth.mode from config.json, once, so this script needs no editing
REM     when the mode changes -----------------------------------------------
for /f %%i in ('%PYTHON% -c "import json;print(json.load(open('scripts/config.json'))['auth']['mode'])" 2^>^>"%LOG%"') do set AUTH_MODE=%%i

if "%AUTH_MODE%"=="" (
    echo Could not read auth.mode from scripts\config.json - see %LOG% >> "%LOG%"
    echo Could not read auth.mode from scripts\config.json. See %LOG%
    exit /b 2
)

echo Auth mode: %AUTH_MODE% >> "%LOG%"

if "%AUTH_MODE%"=="interactive" (
    echo Config is set to interactive mode, which needs a browser. Task Scheduler >> "%LOG%"
    echo cannot supply one. Set auth.mode to secret_env, certificate, or >> "%LOG%"
    echo token_passthrough in scripts\config.json before scheduling this. >> "%LOG%"
    echo Interactive mode cannot be scheduled unattended. See %LOG%
    exit /b 2
)

REM --- token_passthrough needs a token minted fresh before every run; the
REM     other two modes read their own credential straight from config/env
REM     inside run_month.py, so no extra step is needed for them. Get-GraphToken.ps1
REM     reads client_id/tenant_id/thumbprint from scripts\config.json itself, so
REM     nothing needs extracting here. ---------------------------------------------
if "%AUTH_MODE%"=="token_passthrough" (
    REM MSAL.PS 4.x requires PowerShell 7+ (Core edition) - "powershell.exe" is ALWAYS
    REM Windows PowerShell 5.1 on Windows regardless of what's installed or what your
    REM terminal defaults to interactively, and MSAL.PS fails under 5.1 with a confusing
    REM cascade (Import-PowerShellDataFile missing, then type-not-found errors) rather
    REM than a clear "wrong PowerShell version" message. pwsh.exe is PowerShell 7+.
    where pwsh >nul 2>&1
    if errorlevel 1 (
        echo pwsh.exe - PowerShell 7+ - not found on PATH. MSAL.PS requires PowerShell 7+; >> "%LOG%"
        echo Windows PowerShell 5.1 is not sufficient, it will fail silently. >> "%LOG%"
        echo pwsh.exe not found. Install PowerShell 7+: https://aka.ms/powershell-release?tag=stable
        exit /b 2
    )

    set TOKEN_FILE=%TEMP%\graph_token_%RANDOM%.txt
    pwsh -NoProfile -ExecutionPolicy Bypass -File "scripts\Get-GraphToken.ps1" -Raw > "!TOKEN_FILE!" 2>>"%LOG%"
    set PSRC=!ERRORLEVEL!

    if !PSRC! NEQ 0 (
        echo Get-GraphToken.ps1 failed with exit !PSRC! - see %LOG% >> "%LOG%"
        echo Token acquisition FAILED for %MONTH%. See %LOG%
        del "!TOKEN_FILE!" 2>nul
        exit /b !PSRC!
    )

    REM set /p silently truncates long lines - JWT access tokens are 1000-2000+ chars,
    REM well past where that showed up as corruption. for /f has no such limit.
    for /f "usebackq delims=" %%T in ("!TOKEN_FILE!") do set "GRAPH_ACCESS_TOKEN=%%T"
    del "!TOKEN_FILE!" 2>nul

    if "!GRAPH_ACCESS_TOKEN!"=="" (
        echo Get-GraphToken.ps1 produced no token. >> "%LOG%"
        echo Token acquisition produced no token for %MONTH%. See %LOG%
        exit /b 3
    )
    echo Token acquired for this run, expires in under an hour. >> "%LOG%"
)

"%PYTHON%" scripts\run_month.py --month %MONTH% --export-only --auth-mode %AUTH_MODE% >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

REM Belt-and-suspenders: this env var only ever holds a short-lived token, but
REM there is no reason to let it outlive this process either.
set GRAPH_ACCESS_TOKEN=

if %RC% NEQ 0 (
    echo FAILED with exit code %RC% - see %LOG% >> "%LOG%"
    echo Export FAILED for %MONTH%. See %LOG%
    exit /b %RC%
)

echo Completed %DATE% %TIME% >> "%LOG%"
echo Export complete for %MONTH%. Files are in the month's input folder.
echo Next: open Cowork and ask Claude to run the monthly PIM review.
exit /b 0
