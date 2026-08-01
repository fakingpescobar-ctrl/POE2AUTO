# Reader-режим (встроенный C# ридер памяти)

`auto_attack.use_memory: reader` — бот читает память PoE2 через встроенную
C#-сборку `PoE2ReaderLib.dll`, которая загружается **в тот же процесс** Python
через [pythonnet](https://pythonnet.github.io/). Никакого отдельного процесса,
никакого TCP — один процесс, прямой вызов методов.

> ⚠️ Чтение памяти нарушает ToS GGG и несёт риск бана. Только для личных
> экспериментов. Не пишет в память игры — только `PROCESS_VM_READ`.

---

## Архитектура

```
PoE2 (PathOfExileSteam.exe)
    ▲
    │ ReadProcessMemory (PROCESS_VM_READ)
PoE2ReaderLib.dll (C#: Native, MemoryReader, PatternScanner, GameReader)
    ▲
    │ pythonnet (coreclr runtime, загружается один раз)
ReaderBridge (reader_bridge.py, Python)
    ▲
ReaderMobDetector (reader_detect.py) → bot.py
```

Исходники C# лежат **внутри проекта**, в подпапке `poe2-reader/`:

```
POE2AUTO/
├── poe2-reader/                 ← C# ридер (часть проекта)
│   ├── Program.cs               ← единый исходник (Native, GameReader, ...)
│   ├── PoE2Reader.csproj        ← EXE-сборка (отдельный TCP-сервер, опц.)
│   └── lib/
│       ├── PoE2ReaderLib.csproj ← DLL-сборка (грузится pythonnet'ом)
│       └── bin/Release/net8.0/  ← собранная DLL (в .gitignore, собирается)
│           └── PoE2ReaderLib.dll
├── reader_bridge.py             ← Python-обёртка над DLL
├── reader_detect.py             ← детектор мобов на базе ReaderBridge
└── build_reader.bat             ← пересборка DLL
```

`PoE2ReaderLib.csproj` подключает корневой `Program.cs` через
`<Compile Include="..\Program.cs" />` и флаг `READER_LIB` — тот же исходник
компилится и как EXE (TCP-сервер), и как LIB (для pythonnet).

---

## Требования

| Компонент | Зачем | Установка |
|-----------|-------|-----------|
| **.NET 8 SDK** | Сборка DLL из исходников | <https://dotnet.microsoft.com/download> |
| **pythonnet ≥ 3.0** | Загрузка .NET DLL в Python | `pip install pythonnet` (уже в requirements.txt) |
| **ViGEmBus** | Виртуальный геймпад | драйвер, как и для любого режима |
| **Запуск от администратора** | `PROCESS_VM_READ` в PoE2 | `run_bot.bat` от админа |

---

## Сборка DLL

DLL не хранится в git — собирается из исходников одним кликом:

```bat
build_reader.bat
```

Или вручную:

```powershell
dotnet build .\poe2-reader\lib\PoE2ReaderLib.csproj -c Release
```

Результат: `poe2-reader\lib\bin\Release\net8.0\PoE2ReaderLib.dll`.
`reader_bridge.py` находит её по относительному пути автоматически.

Путь можно переопределить переменной окружения `POE2READER_DLL` (например,
если DLL лежит вне проекта).

---

## Когда пересобирать DLL

- После правок `poe2-reader/Program.cs` (логика ридера).
- После обновления PoE2, если изменились оффсеты в `Off`-классе — поправить
  оффсеты в `Program.cs` (или `game_offsets.py`) и пересобрать.
- При ошибке в логе бота «PoE2ReaderLib.dll не найдена / не загружена».

Сборка занимает ~1.5 секунды, безопасна (перезаписывает только DLL).

---

## Сравнение режимов детекции

| Режим (`use_memory`) | Бэкенд | Плюсы | Минусы |
|----------------------|--------|-------|--------|
| **`reader`** ⭐ | C# DLL в процессе | 1 процесс, без TCP, точные HP/поз. | Нужен .NET SDK для сборки DLL; зависит от оффсетов |
| `ec2` | ExileCore2 + TCP | Не ломается на патчах (оффсеты — сообщество) | Нужен Loader.exe + инъекция в PoE2 |
| `memory` | external RPM | Не нужен C# | Ломается на патчах; оффсеты править в коде |
| `false` / `cv` | OpenCV по скриншоту | Работает везде, без памяти | Менее точный (полоса HP, а не точные поз.) |

При недоступности выбранного режима бот **автоматически откатывается на CV**
(см. `bot.py`, `detect_mode`).
