@echo off
title POE2AUTO - HP calibrate
cd /d "%~dp0"

set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

echo === POE2AUTO :: HP orb calibration ===
echo LMB = orb center   trackbars = radius/threshold/color   s = save   q = quit
echo.
"%PY%" calibrate_hp.py
echo.
pause
