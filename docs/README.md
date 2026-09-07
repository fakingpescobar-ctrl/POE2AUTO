# POE2AUTO — Документация проекта

> **Path of Exile 2 Auto-Bot** — Python-автоматизация для PoE2 с поддержкой DualSense/DualShock контроллера.
>
> **Последнее обновление:** 2026-09-07 | **Статус:** Активная разработка

---

## 📋 Оглавление

1. [Обзор проекта](#обзор-проекта)
2. [Режимы детекции](#режимы-детекции)
3. [Архитектура](#архитектура)
4. [Установка и зависимости](#установка-и-зависимости)
5. [Запуск](#запуск)
6. [Горячие клавиши](#горячие-клавиши)
7. [Структура модулей](#структура-модулей)
8. [Конфигурация](#конфигурация)
9. [Известные проблемы и решения](#известные-проблемы-и-решения)
10. [План улучшений](#план-улучшений)
11. [История изменений](#история-изменений)
12. [Структура файлов](#структура-файлов)
13. [Безопасность](#безопасность)

---

## Обзор проекта

POE2AUTO — полноценный бот для автоматизации Path of Exile 2:

- Управляет персонажем через **Sony DualSense / DualShock** (raw HID) → виртуальный Xbox 360 (ViGEmBus)
- Автоматически подбирает лут, атакует мобов, пьёт фляги
- Детекция мобов из **памяти игры** (C# DLL) — быстрее и точнее любой CV
- Ведёт **SQLite телеметрию** убийств мобов (kill cooldown, статистика)
- Click-through оверлей: статусы, HP-пулы (LIFE/ES/Ward), счётчики сущностей, причина fallback

**Ключевая философия:** игрок управляет персонажем как обычно, бот автоматизирует рутину (pickup, attack, flask).

---

## Режимы детекции

Ключ конфигурации: `auto_attack.use_memory`.

| Режим | Значение | Описание |
|---|---|---|
| **RDR** ⭐ | `reader` | **Рекомендуется.** C# `PoE2ReaderLib.dll` через pythonnet: ECS-дерево awake, Life-vtable, позиции, зоны, снаряды. 20 Гц обновление, кэши классификации. |
| MEM | `memory` | Legacy: OpenProcess + NtReadVirtualMemory, AOB-скан оффсетов, self-healing цепочки. |
| CV | `false` / `cv` | Fallback: детект полосы HP моба по цвету (`mob_bar.py` strict/narrow/low_hp_lock). |

### Авто-реконнект RDR (фикс 07.09.2026)

- Бота можно запускать **до** старта PoE2: читалка не откатывается насовсем, а ждёт процесс (проверка каждые ~2 с) и подключается сама, как только персонаж окажется в зоне.
- Пока читалка не готова — работает **CV fallback** (с проверкой hideout через `zone_detector`).
- Причина fallback показывается в оверлее **постоянно** (жёлтая строка уведомлений): `RDR: PoE2 не запущен…`, `RDR: OpenProcess failed (нет admin?)…`, `RDR: GSO AOB pattern не найден (патч сдвинул оффсеты? нужна пересборка DLL)…`.
- После подключения строка исчезает, режим возвращается на `Reader 📡 (mem✓)`.

**После патча PoE2** при появлении «GSO AOB pattern не найден» — **оффсеты сдвинулись**, нужно пересобрать DLL.

> ### 🔧 Инструмент poe2-scope (скачать и запустить!)
>
> **poe2-scope** — отдельный инструмент (не входит в этот репо): мастер AOB-скана оффсетов и пересборки `PoE2ReaderLib.dll`.
>
> ```bash
> git clone https://github.com/fakingpescobar-ctrl/poe2-scope.git
> cd poe2-scope
> poe2_scope.bat        # Electron-GUI мастер (первый запуск сам поставит зависимости)
> # или ядро из консоли:
> python poe2_scope/scope_main.py --auto --yes --tests
> ```
>
> Конвейер `check → find → verify → apply`. Запускай **при каждой смене оффсетов / после каждого патча PoE2**,
> пока не исчезнет жёлтая строка «GSO AOB pattern не найден».

---

## Архитектура

```
┌──────────────────────────────────────────────┐
│              bot.py → bot/ пакет              │
│  main_loop (главный цикл) · overlay_sync      │
│  reload (F5) · state · hotkeys · hp           │
└──────────────────────────────────────────────┘
         │            │              │
         ▼            ▼              ▼
┌──────────────┐ ┌────────────┐ ┌──────────────┐
│  Input Layer │ │  Detect    │ │ Auto Systems │
├──────────────┤ ├────────────┤ ├──────────────┤
│ virtual_xbox │ │reader_bridge││ loot_engine  │
│ dualsense_br │ │reader_detect││ auto_flask   │
│ XUSB mapping │ │mem_detect  │ │ hp_engine    │
│              │ │mob_bar(CV) │ │ monster_db   │
└──────────────┘ └────────────┘ └──────────────┘
                      │
              ┌───────┴────────┐
              ▼                ▼
       poe2-reader (C# DLL)   workers.py
       PoE2ReaderLib.dll      AttackWorker (гибрид memory+CV fallback)
```

### Слои

1. **Input** — обработка ввода геймпада (виртуальный Xbox через vgamepad/ViGEmBus)
2. **Detect** — детекция мобов/зон: RDR (C#), MEM (legacy), CV (fallback)
3. **Auto Systems** — подбор, фляги, атака, телеметрия, навигация
4. **Config** — YAML-конфигурация с типизированной валидацией
5. **UI** — tkinter click-through оверлей

---

## Установка и зависимости

### Системные требования

- **OS:** Windows 10/11 (ViGEmBus)
- **Python:** 3.9+ (Anaconda preferred)
- **Права:** Администратор (ViGEmBus + OpenProcess)
- **Игра:** Path of Exile 2 (Steam) запущена до входа в зону
- **.NET 8 SDK** — только для сборки DLL читалки

### Установка зависимостей

```bash
# Core gaming dependencies
pip install vgamepad hidapi pillow

# Computer vision
pip install opencv-python numpy dxcam mss

# Input handling
pip install pynput

# Configuration
pip install pyyaml

# C# bridge (для RDR режима)
pip install pythonnet>=3.0
```

### C# DLL — сборка (RDR режим)

```bash
cd poe2-reader
dotnet build lib/PoE2ReaderLib.csproj -c Release
# → lib/bin/Release/net8.0/PoE2ReaderLib.dll
```

**Или отдельным инструментом [poe2-scope](https://github.com/fakingpescobar-ctrl/poe2-scope)** (рекомендуется после каждого патча игры) — мастер AOB-скана и пересборки DLL: `poe2_scope.bat` (Electron GUI) → конвейер `check → find → verify → apply`.

---

## Запуск

### Стандартный

```batch
run_bot.bat
```

Лаунчер: проверка admin (`net session`) → elevate (RunAs) → `python bot.py` (Anaconda Python).

### Ручной

```bash
python bot.py
```

**Важно:** бота можно запускать **ДО** входа в игру PoE2 — RDR-читалка подключится сама, когда персонаж окажется в зоне. CV-fallback на это время подхватит атаку.

---

## Горячие клавиши

(из `config.yaml → hotkeys`)

| Клавиша | Функция | Описание |
|---|---|---|
| **F1** | Навигация | Авто-движение (map run / waypoint) |
| **F2** | Авто-атака | Вкл/выкл |
| **F3** | Авто-фляска | Вкл/выкл |
| **F4** | Радиус | Показать/скрыть круг атаки |
| **F6** | Оверлей | Показать/скрыть |
| **F7** | Диагностика | Дамп HP-данных |
| **F8** | Авто-подбор | Вкл/выкл |
| **F9** | Снимок | Скриншот-дамп |
| **F10** | Map run | Прогон карт |
| **F11** | Stash | Операции со сташем (Maps) |
| **F12** | Выход | Полный выход из бота |

---

## Структура модулей

### 🎮 Input Layer

**`virtual_xbox.py`** — виртуальный Xbox 360 (vgamepad/ViGEmBus), heartbeat 20 Гц, потокобезопасность.
**`dualsense_bridge.py`** — raw HID DualSense, маппинг Cross→A/Circle→B/Square→X/Triangle→Y, подавление триггеров при действиях бота.

### 🧠 Detect Layer

**Режим RDR (рекомендуется)**
- `reader_bridge.py` — Python ↔ C# pythonnet мост: OpenProcess, AOB-скан GSO, чтение ECS
- `reader_detect.py` — детекция мобов: parse+track за один проход, Life-vtable «живость», кэш классификации, снаряды

**Режим MEM (legacy)**
- `mem_detect.py` — self-healing цепочка AOB → GameState → AreaInstance → Player
- `offset_discovery.py` — AOB + value-scan + runtime validation оффсетов

**Режим CV (fallback)**
- `mob_bar.py` — детект HP-полосы (strict/narrow/low_hp_lock/bootstrap)
- `zone_detector.py` — hideout-детект по кадру

### ⚙️ Workers

**`workers.py`** — фоновые потоки:
- `LootWorker` — CV-подбор лута
- `AttackWorker` — **гибрид**: память готова → `update()/present()`; не готова → CV fallback c проверкой зоны

### 📊 Auto Systems

- `loot_engine.py` — логика подбора (радиус, приоритеты, кулдауны)
- `auto_flask.py` — правила фляг: HP%, мана, враги рядом, кулдауны
- `hp_engine.py` + `bot/hp.py` — OCR цифр HUD + fallback по орбу, дебаунс, слепота орба
- `monster_db.py` — SQLite телеметрия: batch-insert, фоновый writer, индексы

### 🔧 Остальное

- `config.py` / `config.yaml` — конфигурация и валидация
- `overlay.py` / `bot/overlay_sync.py` — оверлей и синк состояния
- `screen_capture.py` — dxcam + mss fallback
- `poe2-reader/` — C# исходники PoE2ReaderLib
- AOB-скан/пересборка DLL — отдельный инструмент [poe2-scope](https://github.com/fakingpescobar-ctrl/poe2-scope)

---

## Конфигурация

Ключевые секции `config.yaml`:

```yaml
loot:
  pickup_button: "a"
  pickup_hold_ms: 60
  cooldown_ms: 100
  pickup_radius_px: 400
  priority_mode: "distance"      # size | distance

hp:
  enabled: true
  threshold: 0.88
  cooldown_ms: 333
  flask_hold_ms: 120
  confirm_frames: 2
  fallback_red_frac_min: 0.04    # порог «слепого» орба

auto_attack:
  enabled: true
  use_memory: "reader"           # reader | memory | false/cv
  attack_range: 15.0
  button: "right_bumper"
  min_hp_percent: 0.1

hotkeys:
  toggle: f8    # подбор
  attack: f2
  flask: f3
  overlay: f6
  quit: f12
```

Типизированная валидация (`config.py`): schema-проверка типов, deep merge, graceful fallback на defaults.

---

## Известные проблемы и решения

### 🐛 Исправлено (кратко)

| Проблема | Решение |
|---|---|
| RDR не работал, если бот запущен до PoE2 — вечный откат на CV | **Авто-реконнект**: читалка ждёт процесс (2 с), CV временный fallback, причина в оверлее (07.09.2026) |
| F5 (reload) в reader-режиме пересоздавал MemoryMobDetector | reload.py выбирает класс по `detect_mode`, `zone_det` создаётся всегда (07.09.2026) |
| IndentationError bot.py:193 | Убран лишний отступ |
| reload_cfg() nonlocal scope | hp_engine добавлен в nonlocal |
| Flask right inversion (L2/R2) | Исправлен маппинг кнопок |
| Фляга поздно / ложные срабатывания | Fallback red-лужа, guard `last_good_ratio`, дебаунс |
| SQLite 1.1M строк тормозил | Составной индекс `idx_session_status_ts` (5000x) |
| Kill cooldown кэш блокировал цикл | TTL 0.1 → 2.0 c |
| «Стоп» в poe2-scope-ui не снимал состояние | taskkill /T /F + watchdog (05.09.2026) |

Полный журнал — в `ОБНОВЛЕНИЯ.md` и `ПРОБЛЕМЫ_И_РЕШЕНИЯ.md`.

---

## План улучшений

1. **Pathfinding (A*)** — проходимость terrain для движения к целям
2. **Entity persistence** — кэш NPC/сундуков между сессиями
3. **MinimapIcon** — определение waypoints/encounters по иконкам миникарты
4. **CV: bootstrap-калибровка** — авто-подбор порогов при первом запуске
5. **Predictive pickup** — пред-подбор по траектории движения

---

## История изменений

- **07.09.2026** — авто-реконнект RDR (CV fallback вместо вечного отката), причина fallback в оверлее, фикс reload для reader, корневой README
- **05.09.2026** — poe2-scope-ui: фикс «Стоп», сегментная шкала прогресса; разбивка `bot.py` → пакет `bot/`
- **27.07.2026** — 165 тестов, 7 коммитов, разборка монолита, HP-фиксы, SQLite-индексы

Детально — в `ОБНОВЛЕНИЯ.md`.

---

## Структура файлов

```
POE2AUTO/
├── bot.py                   # Точка входа
├── run_bot.bat              # Лаунчер с авто-элевацией
├── config.yaml              # Основная конфигурация
├── README.md                # Этот файл (корневой, на GitHub)
│
├── bot/                     # Пакет оркестратора
│   ├── main_loop.py         #   главный цикл
│   ├── overlay_sync.py      #   синк оверлея (живая причина fallback)
│   ├── reload.py            #   hot-reload F5
│   ├── state.py             #   BotState
│   ├── hotkeys.py / hp.py   #   клавиши / HP-движок
│   └── __main__.py
│
├── workers.py               # LootWorker + AttackWorker (гибрид)
├── reader_bridge.py         # Python ↔ C# мост
├── reader_detect.py         # RDR детектор
├── mem_detect.py            # MEM legacy детектор
├── mob_bar.py               # CV fallback
├── zone_detector.py         # Hideout-детект (CV)
├── dualsense_bridge.py      # Raw HID DualSense
├── virtual_xbox.py          # Виртуальный Xbox 360
│
├── poe2-reader/             # C# PoE2ReaderLib (Program.cs, csproj)
│                            # (AOB-скан/пересборка DLL → отдельный репо: fakingpescobar-ctrl/poe2-scope)
│
├── tests/                   # Юнит- + integration-тесты (170+)
├── docs/                    # Полная документация (README, ОБНОВЛЕНИЯ,
│                            #  ПРОБЛЕМЫ_И_РЕШЕНИЯ, УЛУЧШЕНИЯ, ANALYSIS)
└── assets/                  # Ресурсы оверлея
```

---

## Безопасность

- **Read-only память:** только `PROCESS_VM_READ`, ноль записи в процесс игры
- **Нет сети:** бот не делает внешних сетевых вызовов
- **Admin:** нужен только для ViGEmBus и OpenProcess
- **Валидация конфига:** schema-проверка предотвращает битые настройки

---

## 📞 Поддержка

- `docs/ОБНОВЛЕНИЯ.md` — журнал изменений
- `docs/ПРОБЛЕМЫ_И_РЕШЕНИЯ.md` — баг-журнал
- `docs/УЛУЧШЕНИЯ.md` — планы
- `docs/ANALYSIS.md`, `docs/DEPENDENCY_ANALYSIS.md` — анализ архитектуры/зависимостей

**Статус:** Активный | **Тесты:** pytest `tests/` (170+) | **Режимы:** RDR ⭐ / MEM / CV

---

*Документ обновлён: 2026-09-07 | Ранее: 2026-07-27 (v1.0)*