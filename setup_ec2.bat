@echo off
REM ============================================================
REM  setup_ec2.bat — настройка ExileCore2 + ShareData для POE2AUTO
REM ============================================================
REM
REM  Что делает:
REM  1. Находит ExileCore2 (в poe_bot или по пути)
REM  2. Собирает ShareData плагин из исходников
REM  3. Копирует ShareData.dll в ExileCore2/Plugins/
REM  4. Проверяет готовность
REM
REM  Запуск: setup_ec2.bat
REM ============================================================

setlocal enabledelayedexpansion

echo.
echo ============================================================
echo   POE2AUTO — ExileCore2 + ShareData Setup
echo ============================================================
echo.

REM --- Шаг 1: Найти ExileCore2 ---
set "EC2_DIR="

REM Проверяем poe_bot
if exist "C:\Projects\poe_bot\0_guest\ExileCore2\Loader.exe" (
    set "EC2_DIR=C:\Projects\poe_bot\0_guest\ExileCore2"
    echo [1/4] ExileCore2 найден: !EC2_DIR!
    goto :found_ec2
)

REM Спрашиваем пользователя
set /p EC2_DIR="[1/4] Введи путь к ExileCore2 (например C:\ExileCore2): "
if not exist "!EC2_DIR!\Loader.exe" (
    echo ОШИБКА: Loader.exe не найден в "!EC2_DIR!"
    echo Проверь путь или установи ExileCore2 (см. docs\EC2_SETUP.md)
    pause
    exit /b 1
)

:found_ec2
echo.

REM --- Шаг 2: Собрать ShareData ---
set "SD_SRC=C:\Projects\poe_bot\0_guest\Plugins\Source\ShareData"

if not exist "!SD_SRC!\ShareData.csproj" (
    echo [2/4] Исходники ShareData не найдены: !SD_SRC!
    echo        Пропускаем сборку. Можно скопировать готовый ShareData.dll вручную.
    goto :check_dll
)

echo [2/4] Сборка ShareData плагина...
cd /d "!SD_SRC!"
dotnet build -c Release 2>nul
if errorlevel 1 (
    echo ОШИБКА сборки. Проверь что установлен .NET 8 SDK:
    echo   dotnet --version
    pause
    exit /b 1
)

REM --- Шаг 3: Копировать ShareData.dll ---
set "SD_DLL=!SD_SRC!\bin\Release\net8.0\ShareData.dll"
if not exist "!SD_DLL!" (
    REM Пробуем альтернативный путь (зависит от версии SDK)
    for /f "delims=" %%f in ('dir /b /s "!SD_SRC!\bin\*ShareData.dll" 2^>nul') do (
        set "SD_DLL=%%f"
    )
)

:check_dll
if not exist "!SD_DLL!" (
    echo [3/4] ShareData.dll не найден.
    echo        Собери плагин вручную: cd !SD_SRC! ^&^& dotnet build -c Release
    pause
    exit /b 1
)

echo [3/4] Копирование ShareData.dll в ExileCore2/Plugins/...
if not exist "!EC2_DIR!\Plugins" mkdir "!EC2_DIR!\Plugins"
copy /y "!SD_DLL!" "!EC2_DIR!\Plugins\ShareData.dll" >nul
echo       OK: !EC2_DIR!\Plugins\ShareData.dll

echo.
echo [4/4] Готово!
echo.
echo ============================================================
echo   Установка завершена!
echo ============================================================
echo.
echo  Дальнейшие шаги:
echo   1. Запусти PoE2
echo   2. Запусти: "!EC2_DIR!\Loader.exe"
echo   3. Зайди в игру персонажем
echo   4. Проверь: python ec2_test.py
echo   5. Запусти бота: python bot.py
echo.
echo  Config (config.yaml) уже настроен: use_memory: ec2
echo.
pause
