@echo off
title POE2AUTO - HP diag
cd /d "%~dp0"

set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

echo === POE2AUTO :: HP diagnostics (v=0 catcher, NO gamepad) ===
echo Catches the moment HP detection is lost and dumps screenshots to _debug\hp_events
echo F9 = manual snapshot   F12 = quit
echo This mode has NO joystick/flask. To diagnose while actually playing,
echo run run_bot.bat instead and press F7 (same dump, gamepad + autoflask work).
echo Do NOT run this together with the bot - dxcam conflict.
echo.
"%PY%" hp_diag.py
echo.
pause
