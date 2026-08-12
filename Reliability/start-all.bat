@echo off
:: ============================================================
:: Starts one independent, monitored server process per entry
:: below. Each one runs in its own window via its own copy of
:: watchdog.bat, so they can't affect each other.
::
:: Mirrors the SERVERS list in Dashboard/server/app.py - if you're
:: already running the dashboard, copy the host/port/paths you used
:: there so the two configs describe the same servers.
:: ============================================================

set WATCHDOG=%~dp0watchdog.bat

:: ---- EDIT: one line per server ----
:: call "%WATCHDOG%" <id> <exe path> <launch args> <working dir>

start "watchdog-srv1" cmd /k call "%WATCHDOG%" srv1 "D:\ServerOnlyTSE\Bin\DedicatedServer_Custom.exe DefaultCoop"

start "watchdog-srv2" cmd /k call "%WATCHDOG%" srv2 "D:\ServerOnlyTSE\Bin\DedicatedServer_Custom.exe Server2"

start "watchdog-srv3" cmd /k call "%WATCHDOG%" srv3 "D:\ServerOnlyTSE\Bin\DedicatedServer_Custom.exe RocketJump"

:: Add more servers the same way.
:: ------------------------------------
