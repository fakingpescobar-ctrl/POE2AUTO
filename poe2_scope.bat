@echo off
setlocal
title poe2_scope
cd /d C:\Projects\POE2AUTO\poe2-scope-ui

if not exist "node_modules\electron\dist\electron.exe" goto :install

:run
start "" "node_modules\electron\dist\electron.exe" "C:\Projects\POE2AUTO\poe2-scope-ui"
exit /b 0

:install
echo Installing Electron (first run, ~1-2 min)...
call npm install --no-audit --no-fund
if errorlevel 1 goto :err
if not exist "node_modules\electron\dist\electron.exe" (
  call npm install-scripts approve electron
  call npm rebuild electron
)
if not exist "node_modules\electron\dist\electron.exe" goto :err
goto :run

:err
echo.
echo [poe2_scope] ERROR - install failed, see output above.
echo.
pause
exit /b 1