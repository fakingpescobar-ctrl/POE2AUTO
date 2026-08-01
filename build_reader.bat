@echo off
REM ============================================================
REM  build_reader.bat - сборка PoE2ReaderLib.dll (C# ридер памяти)
REM  для режима auto_attack.use_memory: reader (встроенный ридер).
REM
REM  Требуется: .NET 8 SDK (https://dotnet.microsoft.com/download)
REM  Результат: poe2-reader\lib\bin\Release\net8.0\PoE2ReaderLib.dll
REM  После сборки бот сам найдёт DLL (reader_bridge.py, относит. путь).
REM ============================================================
setlocal

set "ROOT=%~dp0"
set "LIBPROJ=%ROOT%poe2-reader\lib\PoE2ReaderLib.csproj"

echo [*] Сборка PoE2ReaderLib.dll ...

where dotnet >nul 2>nul
if errorlevel 1 (
    echo [ERROR] dotnet SDK не найден в PATH.
    echo         Установи .NET 8 SDK: https://dotnet.microsoft.com/download
    exit /b 1
)

dotnet build "%LIBPROJ%" -c Release
if errorlevel 1 (
    echo [ERROR] Сборка не удалась. Проверь ошибки выше.
    exit /b 1
)

echo.
echo [OK] DLL собрана:
echo     %ROOT%poe2-reader\lib\bin\Release\net8.0\PoE2ReaderLib.dll
echo.
echo Запусти бота с auto_attack.use_memory: reader
endlocal
