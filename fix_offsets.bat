@echo off
title POE2 Offset Fix Tool
cd /d "C:\Projects\POE2AUTO"
powershell -NoProfile -ExecutionPolicy Bypass -File "fix_offsets.ps1" %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo FAILED - see errors above
    pause
) else (
    echo.
    echo Done. Offsets are up to date.
    timeout /t 5 /nobreak >nul
)
