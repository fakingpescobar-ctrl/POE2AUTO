@echo off
REM ============================================================
REM  run_ec2_lite.bat — запуск POE2AUTO Lite (EC2 + auto-attack)
REM ============================================================
REM
REM  Требования:
REM   1. ExileCore2 Loader.exe запущен (инжектился в PoE2)
REM   2. Плагин ShareData активен (TCP :50006)
REM   3. DualSense подключён по USB
REM
REM  Что делает:
REM   - Поднимает виртуальный геймпад (anti-AFK)
REM   - Читает мобов через ExileCore2
REM   - Жмёт R1 когда моб в радиусе
REM   - Жмёт флягу при низком HP/Mana
REM
REM  Хоткеи: F8=атака  F3=фляска  F12=выход
REM ============================================================

cd /d "%~dp0"

echo.
echo ============================================================
echo   POE2AUTO Lite — ExileCore2 + Auto-Attack
echo ============================================================
echo.
echo   Перед запуском убедись:
echo     1. PoE2 запущен
echo     2. ExileCore2 Loader.exe запущен
echo     3. Плагин ShareData активен
echo     4. DualSense подключён по USB
echo.
echo   Хоткеи: F8=атака  F3=фляска  F12=выход
echo.
echo ============================================================
echo.

python bot_ec2_lite.py

if errorlevel 1 (
    echo.
    echo ОШИБКА! См. лог выше.
    pause
)
