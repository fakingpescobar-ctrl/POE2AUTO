@echo off
title POE2AUTO - calibrate
cd /d "%~dp0"

set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

echo === POE2AUTO :: calibration wizard ===
echo LMB=pick color  a=+color  x=clear  Shift+LMB/ijkl=center  d=debug  s=save  q=quit
echo.
"%PY%" calibrate.py
echo.
pause
