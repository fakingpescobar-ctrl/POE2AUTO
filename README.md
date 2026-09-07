# POE2AUTO — Path of Exile 2 Auto-Bot

> **Python-автоматизация для PoE2:** авто-подбор лута, авто-атака, авто-фляги.
> Управление через **Sony DualSense / DualShock** (raw HID) → виртуальный **Xbox 360 (ViGEmBus)**.
>
> **Последнее обновление README:** 2026-09-07 | **Статус:** Активная разработка | **Тесты:** 100+ (pytest)

---

## 📋 Оглавление

1. [Обзор проекта](#обзор-проекта)
2. [Возможности](#возможности)
3. [Режимы детекции мобов](#режимы-детекции-мобов)
4. [Архитектура](#архитектура)
5. [Ядро: как всё работает](#ядро-как-всё-работает)
6. [Граф зависимостей](#граф-зависимостей)
7. [Установка и зависимости](#установка-и-зависимости)
8. [Запуск](#запуск)
9. [Горячие клавиши](#горячие-клавиши)
10. [Структура модулей](#структура-модулей)
11. [Конфигурация](#конфигурация)
12. [Известные проблемы и решения](#известные-проблемы-и-решения)
13. [План улучшений](#план-улучшений)
14. [История изменений](#история-изменений)
15. [Структура файлов](#структура-файлов)
16. [Безопасность](#безопасность)
17. [Поддержка и документация](#поддержка-и-документация)

---

## Обзор проекта

POE2AUTO — полноценный бот для автоматизации рутины **Path of Exile 2**:

- Управляет персонажем через **Sony DualSense / DualShock** контроллер (raw HID → виртуальный Xbox 360 через ViGEmBus для стабильной работы с игрой)
- **Автоматически подбирает лут, атакует мобов, пьёт фляги**
- Детекция мобов — **из памяти игры** (C# DLL через pythonnet): быстрее и точнее любого CV
- Ведёт **SQLite телеметрию** убийств (monster_db): кэши убийств, kill cooldown, статистика
- Click-through оверлей: статусы режимов, HP-пулы (LIFE/ES/Ward), счётчики сущностей, **причина fallback**

**Ключевая философия:** игрок управляет персонажем как обычно, бот автоматизирует рутину — pickup, attack, flask.

---

## Возможности

| Область | Что умеет |
|---|---|
| 🎮 Управление | Sony DualSense raw HID → виртуальный Xbox (vgamepad/ViGEmBus), heartbeat 20 Гц |
| ⚔️ Авто-атака | По данным из **памяти игры** (RDR) или CV-fallback по HP-полосе; приоритет целей, минимальный HP персонажа |
| 💊 Авто-фляги | Правила: HP% (OCR цифр HUD + fallback «слепого» орба), мана, враги рядом; дебаунс, confirm_frames |
| 💰 Авто-подбор | Радиус, приоритеты (size/distance), кулдауны, центр-оффсет |
| 🧭 Навигация | Авто-движение по картам (map run), waypoint, порталы, сташ-операции (Maps) |
| 📈 Телеметрия | SQLite monster_db: batch-insert, фоновый writer, оптимизированные индексы |
| 🖥️ Оверлей | tkinter click-through: статусы, HP-пулы, причина fallback (жёлтая строка) |

---

## Режимы детекции мобов

Ключ конфигурации: `auto_attack.use_memory`.

| Режим | Значение | Описание |
|---|---|---|
| **RDR** ⭐ | `reader` | **Рекомендуется.** C# `PoE2ReaderLib.dll` (pythonnet): ECS-дерево awake, Life-vtable, позиции, зоны, снаряды. ~20 Гц обновление, кэш классификации. |
| **MEM** | `memory` | Legacy: OpenProcess + NtReadVirtualMemory, AOB-скан оффсетов, self-healing цепочка. |
| **CV** | `false` / `cv` | Fallback: детект HP-полосы моба по цвету (`mob_bar.py`, strict/narrow/low_hp_lock + bootstrap). |

### Авто-реконнект RDR (фикс 07.09.2026)

- Бот можно запускать **ДО** старта PoE2 — читалка не откатывается насовсем, а **ждёт процесс** (проверка каждые ~2 с) и подключается сама, как только персонаж окажется в зоне.
- Пока читалка не готова — работает **CV-fallback** (с проверкой hideout через `zone_detector`).
- Причина fallback показывается в оверлее **постоянно** (жёлтая строка уведомлений):

```
RDR: PoE2 не запущен (нет процесса PathOfExileSteam)
RDR: OpenProcess failed (бот запущен без admin?)
RDR: GSO AOB pattern не найден (патч сдвинул оффсеты? нужна пересборка DLL)
RDR: GSO не резолвится в игру (персонаж не в зоне?) — зайди в зону
```

- После подключения строка исчезает, режим возвращается на `Reader 📡 (mem✓)`.

**После патча PoE2** при появлении «GSO AOB pattern не найден» — **оффсеты сдвинулись**, нужно пересобрать DLL.

> ### 🔧 Инструмент poe2-scope (скачать и запустить!)
>
> **poe2-scope** — отдельный инструмент: мастер AOB-скана оффсетов и пересборки `PoE2ReaderLib.dll`.
> Он **не входит** в этот репозиторий — его надо **скачать** (оба репо клонируй **рядом**, в одну папку):
>
> ```bash
> git clone https://github.com/fakingpescobar-ctrl/POE2AUTO.git
> git clone https://github.com/fakingpescobar-ctrl/poe2-scope.git
> cd poe2-scope
> poe2_scope.bat        # Electron-GUI мастер (первый запуск сам поставит зависимости)
> # или ядро из консоли:
> python poe2_scope/scope_main.py --auto --yes --tests
> ```
>
> Инструмент сам найдёт клон бота рядом (или через переменную `POE2AUTO_PATH`).
> Конвейер `check → find → verify → apply` найдет актуальные оффсеты в живой игре,
> поправит `game_offsets.py` + `poe2-reader/Program.cs` и пересоберёт DLL.
> Запускай **при каждой смене оффсетов / после каждого патча PoE2**,
> пока в оверлее бота не исчезнет жёлтая строка «GSO AOB pattern не найден».

---

## Архитектура

```
┌────────────────────────────────────────────────┐
│            bot.py  →  bot/ пакет               │
│  main_loop (главный цикл) · overlay_sync       │
│  reload (F5) · state · hotkeys · hp            │
└────────────────────────────────────────────────┘
         │               │               │
         ▼               ▼               ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Input Layer │  │ Detect Layer │  │ Auto Systems │
├──────────────┤  ├──────────────┤  ├──────────────┤
│ virtual_xbox │  │reader_bridge │  │ loot_engine  │
│ dualsense_br │  │reader_detect │  │ auto_flask   │
│ win_mouse    │  │mem_detect    │  │ hp_engine    │
└──────────────┘  │mob_bar (CV)  │  │ monster_db   │
                  │zone_detector │  │ nav_worker   │
                  └──────────────┘  │ map_run_flow │
                                    │ stash_flow   │
                                    └──────────────┘
                         │                    │
              ┌──────────┴──────────┐   ┌─────┴─────┐
              ▼                     ▼   ▼           ▼
       poe2-reader (C#)         workers.py    overlay.py
       PoE2ReaderLib.dll   (фоновые потоки)   (tkinter)
```

### Слои архитектуры

| Слой | Модули | Ответственность |
|---|---|---|
| **Input** | `virtual_xbox.py`, `dualsense_bridge.py`, `win_mouse.py` | Ввод геймпада/мыши, виртуальный Xbox, подавление конфликтов |
| **Detect** | `reader_bridge.py`, `reader_detect.py`, `mem_detect.py`, `mob_bar.py`, `zone_detector.py` | Детекция мобов/зон: RDR → MEM → CV-fallback |
| **Auto Systems** | `loot_engine.py`, `auto_flask.py`, `hp_engine.py`, `monster_db.py`, `nav_worker.py`, `map_run_flow.py`, `stash_flow.py` | Подбор, фляги, атака, телеметрия, навигация, сташ |
| **Config** | `config.py`, `config.yaml` | YAML + типизированная валидация, deep merge |
| **UI** | `overlay.py`, `entity_overlay.py` | Click-through оверлей статусов и сущностей |
| **Telemetry** | `monster_db.py` | SQLite, batch-insert, индексы |

---

## Ядро: как всё работает

### Главный цикл (`bot/main_loop.py`)
1. Читает `config.yaml`, инициализирует связку: геймпад → виртуальный Xbox, детектор мобов (по `use_memory`), системы автоматизации.
2. Основной поток крутит цикл состояний: `scan → update → decide → act` с частотой ~20 Гц.
3. **Reader-режим:** `ReaderMobDetector.available` сама ретраит `connect()` каждые ~2 с, пока PoE2 не появится. Не готова? → `_atk_fallback_reason = "RDR: <причина> — CV fallback"`, атака временно через CV.
4. **Оверлей** синкается каждый кадр (`bot/overlay_sync.py`): статусы, HP-пулы, причина fallback.
5. **F5 (hot-reload)** — пересоздаёт конфиг и модули без рестарта (`bot/reload.py`; детектор выбирается по режиму — reader остаётся reader!).

### Фоновые потоки (`workers.py`)
| Поток | Цикл | Действие |
|---|---|---|
| **LootWorker** | ~10 Гц | CV-поиск лута → движение → подбор (`pickup_button`) с кулдаунами |
| **AttackWorker** | ~20 Гц | **Гибрид:** память готова → `update()/present()` из RDR; не готова → CV: `zone_detector.in_hideout(full_frame)` + `mob_bar.present(full_frame)` |

### Детекция из памяти (RDR)
```
Python (reader_bridge.py) ──pythonnet──▶ C# (PoE2ReaderLib.dll)
     │                                          │
     │  OpenProcess + AOB-скан GSO              │
     │  ECS-дерево awake, Life-vtable           │
     │  позиции, зоны, снаряды                  │
     ◀──────────────── parse + track ──────────│
                            │
                            ▼
              кэш классификации → AttackWorker
```

### Фляги (hp_engine + bot/hp.py)
OCR цифр HUD (`hp_ocr.py`/`hp_locate.py`) → если орб «слепнет» от эффектов (ratio=None) → **fallback red-лужа** по орбу с guard'ом `last_good_ratio` → дебаунс и `confirm_frames` против ложных срабатываний.

### Навигация (nav_worker + core_map)
`core_map.py` (карта/граф зоны) → `precision_move.py` (точное движение к точке) → `map_run_flow.py` (цикл прогона карт) → `move_to_portal.py` (возврат к порталу), сташ-флоу: `stash_reader.py` + `stash_tab_browser.py`.

---

## Граф зависимостей

```
                    ┌─────────────┐
                    │   bot.py    │  (т.входа → bot/ пакет)
                    └─────────────┘
                          │
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
┌───────────────┐ ┌──────────────┐  ┌──────────────┐
│ config.py     │ │bot/main_loop │  │bot/overlay   │
│ (yaml→validate) │ │bot/reload(F5) │  │_sync →ovlay │
└───────────────┘ └──────────────┘  └──────────────┘
        │                 │                  │
   ┌────┴────┐      ┌─────┴─────┐      ┌─────┴─────┐
   ▼         ▼      ▼           ▼      ▼           ▼
┌──────┐ ┌──────┐ ┌────────┐ ┌──────┐ ┌────────┐ ┌──────────────┐
│loot_ │ │auto_ │ │workers │ │mob_  │ │monster │ │dualSense_bridge
│engine│ │flask │ │(Loot/  │ │bar   │ │db      │ │  virtual_xbox
└──────┘ │hp_   │ │ Attack)│ │(CV)  │ └────────┘ └──────────────┘
         │engine│ └────────┘ └──────┘
         └──────┘      │
                       ▼
                ┌─────────────┐     ┌──────────────────────┐
                │reader_bridge│◀───▶│poe2-reader (C# DLL)  │
                │reader_detect│     │ PoE2ReaderLib        │
                │mem_detect   │     │ (built via dotnet /  │
                │zone_detector│     │  poe2-scope repo)   │
                └─────────────┘     └──────────────────────┘
```

**Внешние Python-зависимости:**
| Пакет | Роль |
|---|---|
| `vgamepad` (+ ViGEmBus) | Виртуальный Xbox 360 |
| `hidapi` | Raw HID DualSense |
| `pythonnet>=3.0` | Мост Python ↔ C# (RDR) |
| `opencv-python`, `numpy` | CV-детекция, обработка кадра |
| `dxcam`, `mss` | Захват экрана (dxcam основной, mss fallback) |
| `pynput` | Глобальные хоткеи F1–F12 |
| `pillow` | Изображения (HP-OCR, оверлей) |
| `pyyaml` | Конфигурация |

**C# (.NET 8 SDK)** — только для сборки DLL: `poe2-reader/lib/PoE2ReaderLib.csproj`.

---

## Установка и зависимости

### Системные требования
- **OS:** Windows 10/11 (ViGEmBus)
- **Python:** 3.9+ (Anaconda preferred)
- **Права:** Администратор (ViGEmBus + OpenProcess)
- **Игра:** Path of Exile 2 (Steam), запущена до входа в зону
- **C# bridge:** .NET 8 SDK — только для **сборки** DLL (не для запуска)

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

### Сборка C# DLL (RDR-режим)
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
Лаунчер автоматически: проверяет admin (`net session`) → elevate (RunAs) → `python bot.py` (Anaconda Python).

### Ручной
```bash
python bot.py
```

**Важно:** бот запускается **ДО** входа в игру PoE2 — читалка подключится сама, когда персонаж окажется в зоне (CV-fallback подхватит атаку до этого).

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
- **`virtual_xbox.py`** — виртуальный Xbox 360 (vgamepad/ViGEmBus): heartbeat-поток 20 Гц, потокобезопасность через `threading.Lock()`, XInput.
- **`dualsense_bridge.py`** — root HID DualSense: парсинг репортов, маппинг Cross→A/Circle→B/Square→X/Triangle→Y, подавление триггеров при действиях бота.
- **`win_mouse.py`** — эмуляция мыши для навигации/кликов.

### 🧠 Detect Layer
**Режим RDR (рекомендуется):**
- **`reader_bridge.py`** — Python ↔ C# (pythonnet) мост: OpenProcess, AOB-скан GSO, авто-реконнект (REINIT 2 с), `_last_err` с человекочитаемой причиной.
- **`reader_detect.py`** — детекция мобов: parse + track за один проход, Life-vtable «живость», кэш классификации, снаряды.

**Режим MEM (legacy):**
- **`mem_detect.py`** — OpenProcess + NtReadVirtualMemory, self-healing цепочка AOB → GameState → AreaInstance → Player.
- **`offset_discovery.py`** / **`offset_scanner.py`** — AOB + value-scan + runtime validation оффсетов; `game_offsets.py` — памятные оффсеты.

**Режим CV (fallback):**
- **`mob_bar.py`** — детект HP-полосы (strict/narrow/low_hp_lock/bootstrap).
- **`zone_detector.py`** — hideout-детект по кадру (CV).
- **`color_detector.py`** — HSV-маски цвета (лут).

### ⚙️ Workers
- **`workers.py`** — фоновые потоки:
  - `LootWorker` — CV-подбор лута (~10 Гц)
  - `AttackWorker` — **гибрид**: память готова → `update()/present()` (RDR); не готова → CV fallback через `zone_detector.in_hideout()` + `mob_bar.present(full_frame)`

### 📊 Auto Systems
- **`loot_engine.py`** — радиус, приоритеты (size/distance), кулдауны.
- **`auto_flask.py`** — FlaskRule: HP%, мана, враги рядом, кулдауны.
- **`hp_engine.py`** + **`bot/hp.py`** — OCR цифр HUD + fallback по орбу, дебаунс, слепота орба, confirm_frames.
- **`monster_db.py`** — SQLite: batch-insert (executemany), фоновый writer, индексы, kill cooldown.
- **`nav_worker.py`** / **`core_map.py`** / **`precision_move.py`** / **`map_run_flow.py`** / **`move_to_portal.py`** — навигация и прогон карт.
- **`stash_flow.py`** / **`stash_reader.py`** / **`stash_tab_browser.py`** — сташ-операции (Maps).
- **`probe_cast.py`** — исследовательские замеры (research).

### 🔧 Остальное
- **`config.py` / `config.yaml`** — YAML + типизированная валидация, deep merge, defaults.
- **`overlay.py`** / **`entity_overlay.py`** — tkinter click-through оверлей.
- **`bot/main_loop.py`**, **`bot/overlay_sync.py`** (живая причина fallback), **`bot/reload.py`** (F5), **`bot/state.py`**, **`bot/hotkeys.py`**, **`bot/hp.py`**.
- **`screen_capture.py`** / **`screen.py`** — dxcam + mss fallback; **`window.py`** — Win32 окно/фокус.
- **`calibrate_hp.py` / `calibrate_hp_digits.py` / `calibrate.py`** — калибровка HP-OCR.
- **`poe2-reader/`** — C# исходники PoE2ReaderLib; AOB-скан и пересборка DLL — отдельный инструмент **[poe2-scope](https://github.com/fakingpescobar-ctrl/poe2-scope)**.

---

## Конфигурация

Ключевые секции `config.yaml`:

```yaml
game:
  window_title: "Path of Exile 2"

capture:
  backend: dxcam              # dxcam | mss

vision:
  marker_rgb: [235, 0, 185]   # маркер-пинг на земле
  hue_tolerance: 8
  sat_min: 200
  val_min: 120
  min_blob_area: 200

loot:
  pickup_button: "a"
  pickup_hold_ms: 60
  cooldown_ms: 100
  pickup_radius_px: 400
  radius_frac: 0              # 0 = pickup_radius_px; >0 = min(h,w)*frac
  center_offset_xy: [0, -40]  # смещение центра подбора
  priority_mode: "distance"   # size | distance

hp:
  enabled: true
  threshold: 0.88             # порог срабатывания фляги
  cooldown_ms: 333
  flask_hold_ms: 120
  confirm_frames: 2
  fallback_red_frac_min: 0.04 # порог «слепого» орба

hp_num:                       # OCR цифр HUD
  ...

auto_attack:
  enabled: true
  use_memory: "reader"        # reader | memory | false/cv
  attack_range: 15.0
  button: "right_bumper"
  min_hp_percent: 0.1

auto_flask:
  ...

hotkeys:
  nav: f1
  attack: f2
  flask: f3
  radial: f4
  overlay: f6
  diag: f7
  toggle: f8
  snapshot: f9
  map_run: f10
  stash: f11
  quit: f12

radial:
  ...

monster_db:
  ...

nav:                          # параметры навигации
  ...

map_run:
  ...
```

**Валидация (`config.py`):** типизированная schema-проверка, deep merge нескольких источников, graceful fallback на defaults.

---

## Известные проблемы и решения

### 🔴 Критические (исправлены)
| Проблема | Решение | Статус |
|---|---|---|
| RDR не мог подключиться, если бот запущен до PoE2 — **вечный откат на CV** | Авто-реконнект (REINIT 2 с), CV как временный fallback, причина в оверлее (07.09.2026) | ✅ |
| F5 (reload) в reader-режиме пересоздавал `MemoryMobDetector` | reload.py выбирает класс по `detect_mode` (07.09.2026) | ✅ |
| IndentationError `bot.py:193` | Убран лишний отступ | ✅ |
| `reload_cfg()` nonlocal scope | `hp_engine` добавлен в nonlocal | ✅ |
| Flask right inversion (L2/R2) | Исправлен маппинг кнопок | ✅ |

### 🩹 HP Flask (журнал)
| Проблема | Причина / Решение | Статус |
|---|---|---|
| Фляга поздно, HP проседает до 70–78% | Орб «слепнет» от эффектов (ratio=None) → fallback red-лужа + снижение cooldown | ✅ |
| Fallback не срабатывал | `low_streak` оставался 0 при pass() → считать от итогового решения | ✅ |
| Ложные фляги на полном HP | Effects на орбе дают false negative → guard `last_good_ratio` | ✅ |

### ⚡ Performance
| Проблема | Решение | Статус |
|---|---|---|
| SQLite 1.1M строк тормозила выборки | Составной индекс `idx_session_status_ts` — **5000x ускорение** | ✅ |
| `_kill_cooldown_cache_ttl = 0.1` блокировал цикл | Поднят до 2.0 сек | ✅ |
| Mob detection frame optimization | Оптимизация кадра детекта | ✅ |

### 🛠 Инструменты (05.09.2026)
| Проблема | Решение | Статус |
|---|---|---|
| «Стоп» в poe2-scope-ui не снимал состояние | taskkill /T /F + watchdog | ✅ |

Полный журнал — в [docs/ОБНОВЛЕНИЯ.md](docs/ОБНОВЛЕНИЯ.md) и [docs/ПРОБЛЕМЫ_И_РЕШЕНИЯ.md](docs/ПРОБЛЕМЫ_И_РЕШЕНИЯ.md).

---

## План улучшений

1. **Pathfinding (A*)** — проходимость terrain для движения к целям
2. **Entity persistence** — кэш NPC/сундуков между сессиями
3. **MinimapIcon** — waypoints/encounters по иконкам миникарты
4. **CV: bootstrap-калибровка** — авто-подбор порогов при первом запуске
5. **Predictive pickup** — пред-подбор по траектории движения
6. **hp_locate / hp_ocr** — авто-локализация HUD, разрез слипшихся цифр, adaptive template
7. **mob_bar confidence scoring** — weighted score по цвету + позиции

---

## История изменений

### Сессия 07.09.2026 — RDR авто-реконнект
- **Фикс:** reader не откатывается насовсем при запуске до PoE2; CV — временный fallback; причина — в оверлее постоянно (живая, из `bridge._last_err`)
- **Фикс:** F5 в reader-режиме больше не создаёт `MemoryMobDetector`
- **Фикс:** `_last_err_logged` (лог причины однократно при смене)
- **Hybrid AttackWorker:** память готова → RDR; нет → CV с проверкой зоны
- Документация: корневой README, обновлённый [docs/README.md](docs/README.md), сессия в [docs/ОБНОВЛЕНИЯ.md](docs/ОБНОВЛЕНИЯ.md)
- Коммиты: `9b892d2`, `4ea49ef`, `1582f7a`, `9b4c10a`
- **Проверено живьём:** бот запущен до PoE2 → после входа в зону RDR подключился сам ✅

### Сессия 05.09.2026 — инструменты и рефакторинг
- `bot.py` разбит на пакет `bot/` (main_loop, overlay_sync, reload, state, hotkeys, hp)
- poe2-scope-ui: фикс «Стоп», сегментная шкала прогресса

### Сессия 27.07.2026 — стабилизация
- 7 коммитов, 165 тестов, 5 новых модулей, 24 пофикшенных бага
- HP-движок: fallback, дебаунс, защита от ложных фляг; SQLite-индексы (5000x); Performance fixes

---

## Структура файлов

```
POE2AUTO/
├── bot.py                    # Точка входа
├── run_bot.bat               # Лаунчер с авто-элевацией
├── config.yaml               # Основная конфигурация
├── config.py                 # YAML-загрузка + валидация
├── README.md                 # Этот файл
├── requirements.txt          # Зависимости
│
├── bot/                      # Пакет оркестратора
│   ├── main_loop.py          #   главный цикл
│   ├── overlay_sync.py       #   синк оверлея (живая причина fallback)
│   ├── reload.py             #   hot-reload F5
│   ├── state.py              #   BotState
│   ├── hotkeys.py            #   глобальные клавиши
│   ├── hp.py                 #   HP-движок
│   └── __main__.py
│
├── workers.py                # LootWorker + AttackWorker (гибрид)
├── reader_bridge.py          # Python ↔ C# мост
├── reader_detect.py          # RDR детектор
├── mem_detect.py             # MEM legacy детектор
├── mob_bar.py                # CV fallback
├── zone_detector.py          # Hideout-детект (CV)
├── dualsense_bridge.py       # raw HID DualSense
├── virtual_xbox.py           # виртуальный Xbox 360
├── loot_engine.py            # логика подбора
├── auto_flask.py             # авто-фляги
├── hp_engine.py / hp_ocr.py / hp_locate.py / hp_detector.py / hp_diag.py
├── monster_db.py             # SQLite телеметрия
├── nav_worker.py / core_map.py / precision_move.py
├── map_run_flow.py / move_to_portal.py / probe_cast.py
├── stash_flow.py / stash_reader.py / stash_tab_browser.py
├── screen_capture.py / screen.py / window.py / win_mouse.py
├── color_detector.py / entity_reader.py / entity_overlay.py
├── overlay.py                # tkinter click-through
├── game_memory.py / game_offsets.py / offset_discovery.py / offset_scanner.py
├── calibrate_hp.py / calibrate_hp_digits.py / calibrate.py
├── utils.py / diag_dump.py
│
├── poe2-reader/              # C# PoE2ReaderLib (Program.cs, lib/PoE2ReaderLib.csproj)
│                             # (AOB-скан/пересборка DLL → отдельный репо: fakingpescobar-ctrl/poe2-scope)
│
├── tests/                    # Юнит- и integration-тесты (22 файла, 400+ функций)
├── docs/                     # Полная документация (README, ОБНОВЛЕНИЯ,
│                             #  ПРОБЛЕМЫ_И_РЕШЕНИЯ, УЛУЧШЕНИЯ, ANALYSIS,
│                             #  DEPENDENCY_ANALYSIS)
├── assets/                   # Ресурсы оверлея
└── archive/                  # Архивные debug-модули
```

---

## Безопасность

- **Read-only память:** только `PROCESS_VM_READ`, ноль записи в процесс игры
- **Нет сети:** бот не делает внешних сетевых вызовов
- **Admin:** нужен только для ViGEmBus и OpenProcess
- **Валидация конфига:** schema-проверка предотвращает битые настройки
- **Lock-файлы:** защита от двойного запуска

---

## Поддержка и документация

| Файл | Содержание |
|---|---|
| [docs/README.md](docs/README.md) | Полная документация проекта |
| [docs/ОБНОВЛЕНИЯ.md](docs/ОБНОВЛЕНИЯ.md) | Журнал изменений (по сессиям) |
| [docs/ПРОБЛЕМЫ_И_РЕШЕНИЯ.md](docs/ПРОБЛЕМЫ_И_РЕШЕНИЯ.md) | Баг-журнал |
| [docs/УЛУЧШЕНИЯ.md](docs/УЛУЧШЕНИЯ.md) | План развития |
| [docs/ANALYSIS.md](docs/ANALYSIS.md) | Полный анализ проекта |
| [docs/DEPENDENCY_ANALYSIS.md](docs/DEPENDENCY_ANALYSIS.md) | Анализ зависимостей (545 строк) |

**Статус:** Активный | **Тесты:** pytest (22 файла, 100+ тестов) | **Модулей:** 50+ | **Режимы:** RDR ⭐ / MEM / CV

---

*README обновлён: 2026-09-07 | Ранее: 2026-07-27 (docs/README v1.0)*