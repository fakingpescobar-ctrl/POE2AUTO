@echo off
title POE2AUTO - calibrate HP digits
cd /d "%~dp0"

rem ViGEmBus (virtual gamepad) requires admin rights - auto-elevate
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

echo === POE2AUTO :: calibrate HP digits ===
echo CLOSE run_bot.bat first (gamepad/capture conflict).
echo Gamepad stays alive here so you are not kicked from the game.
echo Type what you see in the Health line; q to quit.
echo.
"%PY%" calibrate_hp_digits.py
echo.
pause
