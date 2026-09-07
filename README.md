# POE2AUTO

**Path of Exile 2 Auto-Bot** — Python-автоматизация для PoE2: авто-подбор лута, авто-атака, авто-фляги. Управление через **DualSense / DualShock** контроллер через виртуальный Xbox 360 (ViGEmBus).

> **Философия:** игрок управляет персонажем как обычно, бот автоматизирует рутину — pickup, attack, flask.

---

## Возможности

- 🎮 Поддержка Sony DualSense (raw HID) → виртуальный Xbox 360 через `vgamepad`
- ⚔️ Авто-атака мобов по данным из **памяти игры** (C# DLL, pythonnet) — без распознавания изображения
- 💊 Авто-фляги: HP (OCR цифр HUD + fallback по орбу), мана, shield — с дебаунсом и «слепотой» орба
- 💰 Авто-подбор лута с радиусом, приоритетами и кулдаунами
- 🧭 Авто-движение по картам (map run) и сташ-операции (Maps)
- 📈 SQLite телеметрия мобов (`monster_db`) — кэши убийств, kill cooldown, статистика
- 🖥️ Click-through оверлей: статус режимов, HP-пулы (LIFE/ES/Ward), счётчики сущностей, причина fallback

## Режимы детекции мобов (`config.yaml → auto_attack.use_memory`)

| Режим | Значение | Описание |
|---|---|---|
| **RDR** ⭐ | `reader` | **Рекомендуется.** C# `PoE2ReaderLib.dll` (pythonnet): ECS-дерево, Life-vtable, зоны, снаряды. Быстро и точно. |
| MEM | `memory` | Legacy-режим: OpenProcess + NtReadVirtualMemory, AOB-скан оффсетов. |
| CV | `false` / `cv` | Fallback: детекция полосы HP моба по цвету (`mob_bar.py`). |

**Авто-реконнект RDR:** если бот запущен до старта PoE2 — читалка не сдаётся, а ждёт процесс (проверка каждые ~2 с) и переключается сама, как только войдёшь в игру/зону. Пока читалка не готова — работает CV-fallback. Причина отображается в оверлее постоянно (например: `RDR: PoE2 не запущен…` / `RDR: GSO AOB pattern не найден — нужна пересборка DLL`).

**После патча PoE2** (если в оверлее/логе появилось «GSO AOB pattern не найден»):

```
poe2_scope.bat    # мастер скана оффсетов и пересборки DLL (poe2-scope-ui / poe2_scope)
```

## Системные требования

- **OS:** Windows 10/11, права администратора (ViGEmBus + OpenProcess)
- **Python:** 3.9+ (Anaconda preferred)
- **Игра:** Path of Exile 2 (Steam), загружена до входа в зону
- **C# bridge:** .NET 8 SDK (только для сборки DLL, не для запуска)

## Установка

```bash
# Core gaming
pip install vgamepad hidapi pillow pynput pyyaml

# Computer vision + capture
pip install opencv-python numpy dxcam mss

# C# reader bridge
pip install pythonnet>=3.0
```

Сборка DLL читалки (один раз, или после каждого патча игры):

```bash
cd poe2-reader
dotnet build lib/PoE2ReaderLib.csproj -c Release
# → lib/bin/Release/net8.0/PoE2ReaderLib.dll
```

(альтернатива: `poe2_scope.bat` — мастер со сканом актуальных оффсетов)

## Запуск

```batch
run_bot.bat
```

Лаунчер проверяет admin, elevate, запускает `python bot.py`. **Важно:** запускать бота **ДО** входа в игру PoE2 — читалка сама подключится, когда персонаж окажется в зоне.

## Горячие клавиши

| Клавиша | Функция |
|---|---|
| **F2** | Авто-атака вкл/выкл |
| **F3** | Авто-фляска вкл/выкл |
| **F6** | Оверлей показать/скрыть |
| **F8** | Авто-подбор вкл/выкл |
| **F9** | Авто-движение по карте |
| **F12** | Полный выход |

## Структура

```
bot/                    # Пакет оркестратора: main_loop, overlay_sync, reload, state, hotkeys, hp
bot.py                  # Точка входа
workers.py              # Фоновые потоки: loot, attack (гибрид memory + CV fallback)
reader_bridge.py        # Python ↔ C# мост (pythonnet)
reader_detect.py        # Детекция мобов через память (RDR)
mem_detect.py           # Legacy memory-детектор (MEM)
mob_bar.py              # CV детекция HP-полосы (fallback)
zone_detector.py        # Определение hideout/зоны (CV)
dualsense_bridge.py     # Raw HID DualSense → Xbox
virtual_xbox.py         # Виртуальный Xbox 360 (vgamepad/ViGEmBus)
poe2-reader/            # C# библиотека чтения памяти (PoE2ReaderLib)
poe2-scope-ui/          # Electron-мастер скана оффсетов и пересборки DLL
poe2_scope/             # Python-движок скана (chain AOB → оффсеты)
config.yaml             # Основная конфигурация
tests/                  # Юнит- и integration-тесты (pytest)
docs/                   # Полная документация, журналы багов, планы
```

## Тесты

```bash
python -m pytest tests/ -q
```

## Безопасность

- Только чтение памяти: `PROCESS_VM_READ`, ноль записи в процесс игры
- Бот не выходит в интернет (нет внешних сетевых вызовов)
- Права администратора нужны только для ViGEmBus и OpenProcess

## Документация

- `docs/README.md` — полная документация проекта
- `docs/ОБНОВЛЕНИЯ.md` — журнал изменений
- `docs/ПРОБЛЕМЫ_И_РЕШЕНИЯ.md` — баг-журнал
- `docs/УЛУЧШЕНИЯ.md` — план развития
- `docs/ANALYSIS.md`, `docs/DEPENDENCY_ANALYSIS.md` — анализ архитектуры и зависимостей