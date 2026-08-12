@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: Multi-server watchdog
::
:: Relaunches a dedicated server whenever it exits, for any reason.
:: One instance of this script supervises ONE server; start-all.bat
:: launches one independent copy per server, so a problem with one
:: server (or its watchdog window) can never affect another.
::
:: Called as:  watchdog.bat <id> <exe> <args> <workdir>
:: You normally won't call this directly - edit start-all.bat instead.
:: ============================================================

set SRV_ID=%~1
set SERVER_EXE=%~2
set SERVER_ARGS=%~3
set SERVER_DIR=%~4
set RESTART_DELAY_SEC=5

if "%SRV_ID%"=="" (
    echo Usage: watchdog.bat ^<id^> ^<exe^> ^<args^> ^<workdir^>
    echo This is normally launched by start-all.bat, not run directly.
    exit /b 1
)

set LOGFILE=%~dp0watchdog-%SRV_ID%.log

:: Optional: paste a Discord webhook URL to get pinged on every
:: unexpected restart, for every server. Leave blank to disable.
set DISCORD_WEBHOOK=

:: Optional: dashboard integration. Leave DASHBOARD_URL blank to skip -
:: the watchdog works fine standalone with nothing running on the other
:: end. See README section "Dashboard integration" for what this needs
:: on the app.py side.
set DASHBOARD_URL=
set DASHBOARD_ADMIN_TOKEN=

cd /d "%SERVER_DIR%"

:loop
echo [%date% %time%] [%SRV_ID%] Starting server... >> "%LOGFILE%"

"%SERVER_EXE%" %SERVER_ARGS%
set EXITCODE=%ERRORLEVEL%

echo [%date% %time%] [%SRV_ID%] Server exited (code %EXITCODE%). Restarting in %RESTART_DELAY_SEC%s... >> "%LOGFILE%"

if not "%DISCORD_WEBHOOK%"=="" (
    powershell -NoProfile -Command ^
        "try { Invoke-RestMethod -Uri '%DISCORD_WEBHOOK%' -Method Post -ContentType 'application/json' -Body (@{content=('[%SRV_ID%] Server exited (code %EXITCODE%) and is being restarted automatically.')} | ConvertTo-Json) } catch {}"
)

if not "%DASHBOARD_URL%"=="" (
    powershell -NoProfile -Command ^
        "try { Invoke-RestMethod -Uri '%DASHBOARD_URL%/api/admin/watchdog-report' -Method Post -Headers @{Authorization=('Bearer ' + '%DASHBOARD_ADMIN_TOKEN%')} -ContentType 'application/json' -Body (@{server='%SRV_ID%'; exit_code=%EXITCODE%} | ConvertTo-Json) } catch {}"
)

timeout /t %RESTART_DELAY_SEC% /nobreak > nul
goto loop
