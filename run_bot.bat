@echo off
title POE2AUTO - bot
cd /d "%~dp0"

rem ViGEmBus requires admin rights - auto-elevate
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

echo === POE2AUTO :: bot ===
echo F8 = toggle auto-pickup   F12 = quit
echo Start this BEFORE entering PoE2.
echo.
"%PY%" bot.py
echo.
pause
