# POE2AUTO — Полный анализ проекта

> **Дата:** 16.08.2026
> **Статус фактов:** проверены по исходному коду, ревью critic APPROVE
> **Назначение документа:** сэкономить время на повторный анализ — при работе с проектом начинать отсюда, а не с чтения всего кода.
> **Метод сбора:** чтение ядра (bot.py, reader_detect.py, monster_db.py, reader_bridge.py, core_map.py, nav_worker.py, game_memory.py, game_offsets.py, virtual_xbox.py, dualsense_bridge.py, entity_reader.py, config.yaml, main.py) + supermemory + тесты.
> **Важно:** оффсеты и структуры памяти меняются с патчами игры. Проверять `game_offsets.py` перед боем. Небольшое устаревание деталей конфига возможно — сверяться с `config.yaml`.

---

## Оглавление

1. [Общая картина](#1-общая-картина)
2. [Запуск и окружение](#2-запуск-и-окружение)
3. [Архитектура ядра (bot.py)](#3-архитектура-ядра-botpy)
4. [Главный цикл бота](#4-главный-цикл-бота)
5. [Слои и модули](#5-слои-и-модули)
6. [Классификация целей (reader_detect.py) — ядро боя](#6-классификация-целей-reader_detectpy--ядро-боя)
7. [База данных телеметрии (monster_db.py)](#7-база-данных-телеметрии-monster_dbpy)
8. [Чтение памяти: offsets и цепочки](#8-чтение-памяти-offsets-и-цепочки)
9. [Ввод/управление (ViGEmBus + DualSense)](#9-вводуправление-vigembus--dualsense)
10. [Навигация (CoreMap + NavWorker + MapRun)](#10-навигация-coremap--navworker--maprun)
11. [HP-стек и авто-фляги](#11-hp-стек-и-авто-фляги)
12. [Хоткеи и CLI](#12-хоткеи-и-cli)
13. [Известные баги и долги](#13-известные-баги-и-долги)
14. [Производительность](#14-производительность)
15. [Тесты](#15-тесты)
16. [История ключевых коммитов](#16-история-ключевых-коммитов)
17. [Рейтинг модулей по важности](#17-рейтинг-модулей-по-важности)
18. [Риски архитектуры (замечания ревью)](#18-риски-архитектуры-замечания-ревью)
19. [Диагностические скрипты (лаборатория)](#19-диагностические-скрипты-лаборатория)

---

## 1. Общая картина

**Что это:** Python-бот для Path of Exile 2 (PoE2). Читает память игры (read-only, без инъекций), видит всех мобов на текущей карте, самостоятельно атакует, подбирает лут и пьёт фляги. Управление персонажем — через **виртуальный Xbox 360 геймпад** (ViGEmBus), на который мостом пробрасываются стики/кнопки реального **DualSense** (PS5).

**Философия:** бот НЕ двигает персонажа сам в бою (движение — за игроком), но навигация до целей (map run / stash) есть как отдельная механика. Бот «добивает» автоматику: цели, лут, фляги, фарм карт.

**Три режима детекта мобов** (`config: auto_attack.use_memory`):

| Режим | Механика | Статус |
|---|---|---|
| `reader` ⭐ | C# ридер `PoE2ReaderLib.dll` через pythonnet, один процесс | **Recommended**, дефолт |
| `memory` | Старый external `PROCESS_VM_READ` + AOB-скан (mem_detect.py) | Легаси, ломается на патчах |
| `false`/`cv` | CV по скриншоту (полоса HP моба) | Fallback при недоступности reader/memory |

**Авто-откат:** если memory/reader падает — бот автоматически переключается на CV-детект (3 неудачные попытки → fallback), чтобы не остаться слепым.

**Масштаб:** ~2500 строк продакшена (ядро), ~170 файлов всего, ~100 диагностических скриптов.

---

## 2. Запуск и окружение

| Что | Как |
|---|---|
| Запуск | `run_bot.bat` (авто-элевация через `net session`, admin обязателен для ViGEmBus) |
| Скрытый запуск | `start_bot_hidden.ps1` |
| Python | `%USERPROFILE%\anaconda3` — **обязателен `pythonnet`**; обычный Python 3.14 НЕ подходит |
| Сборка C# ридера | `build_reader.bat` → `dotnet build lib\PoE2ReaderLib.csproj -c Release` (в `C:\Projects\poe2-reader`) |
| Обновление оффсетов | `fix_offsets.bat` → offset scanner (6 стратегий поиска) |
| Калибровка | `run_calibrate.bat` / `calibrate.bat` |
| Тесты | `python -m pytest tests/` |

**Структура C# части:** `poe2-reader/lib/bin/Release/net8.0/PoE2ReaderLib.dll` (путь переопределяется env `POE2READER_DLL`).

---

## 3. Архитектура ядра (bot.py)

**Монолит-оркестратор, 1213 строк.** Вся логика в `main()`:

```
bot.py
├── VirtualXbox360 (ViGEmBus)      — виртуальный геймпад, heartbeat 20Гц
│     └── DualSenseBridge (raw HID) — стики/кнопки/триггеры реального DualSense
├── LootWorker (daemon-поток)      — детект лута, queue(maxsize=1), кадры отбрасываются
├── AttackWorker (daemon-поток)    — детект мобов, queue(maxsize=1)
├── ReaderMobDetector (main thread, update 20Гц)  ← ЯДРО
│     ├── ReaderBridge (pythonnet → PoE2ReaderLib.dll)
│     └── MonsterDB (SQLite, writer-поток)
├── HP-стек: HPDetector (орб) + HPNumReader (цифры HUD) + HPEngine (решения)
├── AutoFlaskEngine (memory-правила HP%/Mana%/количество врагов)
├── CV-фляга (когда ридер недоступен): HPEngine + HPDetector + HPNumReader
├── Overlay (tkinter, отдельный поток, click-through)
├── NavWorker (daemon-поток, навигация по карте)
├── _stash_task (F11) / _map_run_task (F10) — одноразовые потоки с СОБСТВЕННЫМ GameMemory
└── pynput Listener (хоткеи F1–F12)
```

**Ключевые паттерны:**
- **Один захват кадра на итерацию.** dxcam перезаписывает буфер — все кропы (HP-орб, лут, мобы-CV) вырезаются из одного full-кадра, чтобы не рвать синхронность.
- **LootWorker/AttackWorker:** `queue(maxsize=1)` — если поток не успел обработать, кадр просто отбрасывается (не накапливаем лаг).
- **Одноразовые задачи (stash/map_run):** отдельные потоки с флагами `_stash_running`/`_map_run_running` + lock — защита от двойного запуска (F10 дважды).
- **Отдельный GameMemory** у stash/map_run (не используют reader_bridge) — потому что это legacy-путь чтения для сташа/карт.
- **reload_cfg (F5):** hot-reload конфига и пересборка геометрии HP без рестарта.
- **rebuild_hp_geometry / try_locate_hud:** авто-локализация HP-элементов HUD при смене разрешения/положения окна.

---

## 4. Главный цикл бота

Каждая итерация (~11.8 Гц после оптимизаций, было 2.1 Гц):

1. **Захват** одного полного кадра (`cap.grab(region)`)
2. **HP-кроп** (орб + цифры) → HPDetector/HPNumReader → HPEngine.decide() → если нужно → `vx.tap(фляга)`
3. **Лут-кроп** вокруг персонажа → submit в LootWorker → если лут в радиусе и кулдаун истёк → `vx.tap(A)`
4. **Attack submit** → AttackWorker: если есть COMBAT-цель в радиусе → `vx.press(R1)`, иначе `vx.release(R1)` с grace 0.6с
5. **Auto-flask (memory):** `read_player_vitals()` + `get_hostiles_in_range()` → правила фляг (см. раздел 11)
6. **Обновление оверлея** (статистика, текущая цель, статус), лог 1 раз/сек
7. `time.sleep(0.01)`

**Цифры (измерены после оптимизаций):**
- reader update: **11.5 мс ≈ 87 Гц**
- raw read: 1172–1328 сущностей за 35–44 мс
- полный цикл: **~84.9 мс ≈ 11.8 Гц** (до фиксов было ~476 мс ≈ 2.1 Гц)
- запрос kill-cooldown: 3.1 с → **0.6 мс** (после индекса)
- 5-минутный тест боя: 35+ мобов, босс, редкий моб — телеметрия собирается без потерь

---

## 5. Слои и модули

| Слой | Модуль (строк) | Назначение |
|---|---|---|
| **Память (low-level)** | `game_memory.py` (540) | OpenProcess + NtReadVirtualMemory, AOB-скан, read-only terrain walkable, world_to_screen |
| **Оффсеты** | `game_offsets.py` (136) | Все цепочки адресов: GameState→InGameState→AreaInstance→Entity→Life/VitalStruct; RATIO=250/23 |
| **C# мост** | `reader_bridge.py` (429) | pythonnet → PoE2ReaderLib.dll: init (PID→OpenProcess→GSO AOB), ReadEntities, vitals, кэши компонентов |
| **Entity** | `entity_reader.py` (560) | EntityData NamedTuple, обход AwakeEntities (StdMap), используется core_map/сташем |
| **Детект мобов (reader)** | `reader_detect.py` (904) ⭐ | ReaderMobDetector: классификация целей, треки, kill cooldown, телеметрия |
| **Детект мобов (legacy)** | `mem_detect.py` (25 KB) | MemoryMobDetector (external, откат на CV после 3 неудач) |
| **Телеметрия** | `monster_db.py` (625) | SQLite: кадры, kill events, сессии, CLI-аналитика |
| **Решения/бой** | `bot.py` AttackWorker | Держит R1 пока есть COMBAT-цель в радиусе |
| **Лут** | `loot_engine.py` + `color_detector.py` | HSV-маски маркеров лута, радиус/приоритет, tap(A) |
| **HP-стек** | `hp_detector.py`, `hp_ocr.py`, `hp_engine.py`, `hp_locate.py` | Орб + цифры HUD, state-machine решений, авто-локализация |
| **Фляги** | `auto_flask.py` (+ CV-фляга в bot.py) | Правила HP%/Mana%/враги |
| **Ввод** | `virtual_xbox.py` (171), `dualsense_bridge.py` (234) | ViGEmBus + HID-мост с реконнектом |
| **Навигация** | `core_map.py` (330), `nav_worker.py` (271), `map_run_flow.py`, `flow_recorder.py` | Terrain grid, A*, движение через стик, map run F10 |
| **Сташ** | `stash_reader.py`, `stash_flow.py`, `stash_tab_browser.py` | F11: открыть MAPS, посчитать карты по тирам |
| **CV-fallback** | `mob_bar.py`, `zone_detector.py`, `screen.py`, `window.py` | Полоса HP моба, hideout-детект, захват экрана, окно |
| **UI** | `overlay.py`, `entity_overlay.py` | Click-through оверлей (tkinter), отрисовка сущностей |
| **Research/offsets** | `offset_scanner.py`, `offset_discovery.py`, `research.py`, `sampler.py`, `fix_offsets.bat` | 6 стратегий поиска оффсетов |
| **Диагностика** | `_diag_*`, `_debug_*`, `_bench_*`, `probe_*`, `watch_*`, `dump_*`, `calibrate_*` | Интерактивный дамп памяти/боя |

---

## 6. Классификация целей (reader_detect.py) — ядро боя

**Главная фишка проекта.** Многоуровневая классификация **TargetStatus** вместо бинарного «жив/мёртв» — от самого «мёртвого» состояния к боеготовому:

```
DEAD (труп, Life+0x10) → CORPSE (умер <30с, есть дроп)
→ SPIRIT (дух/эффект) → UNREACHABLE (другой Z-слой)
→ LATENT (hp ≤ min_hp) → GHOST_STATIC (жив, но ни разу не двигался)
→ UNKNOWN (HP не прочитан) → COMBAT (АТАКОВАТЬ)
```

**Атакуются только COMBAT.** ATTACKABLE = frozenset({COMBAT}).

**Защита от мусорных целей:**
- `death_grace_s = 2.0` — не бьём в анимации смерти
- `kill_cooldown_s = 5.0` — не бьём недавно убитых (пока дроп не поднят), через MonsterDB (см. раздел 7). Без этого — «кулдаун-самоцикл»: мёртвый моб снова становится целью
- **static track** — «призраки» (спящие утопленники/декор/стационарные сундуки) отсекаются по `ever_moved` / `ever_hit`
- **Z-tolerance 120** — отсекает аурных демонов на других этажах, но пропускает наклонные арены (dz 27–37)
- **Снаряды** — `_player_fire_time`: вражеский снаряд приближается к игроку → игрок «в бою» → стоячий стрелок становится целью
- **confidence** — score для ранжирования целей в `get_best_target()` (босс > unique > rare > magic > normal)

**Парсинг (оптимизирован):**
- `_parse_and_track` — ОДИН проход по raw_entities (1172+ сущностей/кадр) с ведением треков (раньше было два прохода)
- `_finalize_tracks` — детект смерти (моб был, исчез из кадра + ever_moved → `_dead_track`), чистка устаревших треков, снаряды
- Классификация кэшируется в `_status_by_id` на кадр — не пересчитывается
- `_check_static` — фильтр призраков (двигался ли/был ли атакован)
- `_compute_confidence` — вычисление score уверенности
- `_HIDEOUT_CACHE_TTL = 3.0` — кэш hideout-статуса (в хайдауте не сканируем мобов)

**API детектора:** `present()` (есть ли hostile в радиусе), `get_nearest_hostile()`, `get_all_hostiles()`, `get_best_target()`.

**Телеметрия:** каждый кадр пишется снимок `db.record_frame(_area_code, db_rows)` (с dedup, см. раздел 7).

---

## 7. База данных телеметрии (monster_db.py)

**Файл:** `monster_telemetry.db` (в корне проекта; на 16.08.2026: основной файл **~556 МБ** + WAL **~5.7 МБ** + SHM ~0 МБ — растёт быстро, чистится).

**Таблица `monster_events`:**
`id, session_id, ts (REAL, Unix-epoch!), area, addr, name, category, rarity, hp_cur, hp_max, hp_pct, grid_x, grid_y, dist, status, hostile, is_boss`

**Индексы (5 шт.), ключевой:**
- `idx_session_status_ts (session_id, status, ts)` — спас kill-cooldown запрос с 3.1 с → 0.6 мс
- также по addr, name, area, ts

**Архитектура записи:**
- `record_frame()` кладёт данные в Queue (O(1)) → **daemon writer-поток** батчами по 50 (flush 1с), `executemany`
- **WAL mode** (`PRAGMA journal_mode=WAL`) + `synchronous=NORMAL` — быстро при частых записях
- **dedup** (`monster_db.dedup: true`): пишутся только ИЗМЕНИВШИЕСЯ мобы → 10–20× меньше строк (234 000 → 6 319 за 115 с боя)

**Очистка:** `_maybe_cleanup` раз в час: DELETE строк старше `keep_days` (дефолт 3) + VACUUM только если реально удалили (иначе VACUUM не дёргается зря).

**Как определяется убийство:**
- последний статус моба = `dead`, ИЛИ
- статус был `combat`, и моб исчез из кадра (обрыв трека = умер)

**Ключевой запрос:** `get_recently_killed_addrs(cooldown_s=...)` (monster_db.py:435, дефолт `cooldown_s=10.0`; реальный вызов из reader_detect.py:554 — со значением `kill_cooldown_s=5.0` из конфига). EXISTS-подзапрос: «был ли combat до dead в течение кулдауна». Используется для kill cooldown (не атаковать недавно убитых).

**CLI-аналитика:** `python monster_db.py --kills / --name <имя> / --addr <адрес> / ...` — уникальные имена, распределение статусов, TOP по HP, история по addr, сводка по сессиям.

**Отдельная `sessions.db`** — пустая (0 байт), НЕ используется (задел).

---

## 8. Чтение памяти: offsets и цепочки

### game_offsets.py (136 строк) — из POE2GPS (Sikaka/POE2Radar, MIT)

**Оффсеты ЗАХАРДКОЖЕНЫ в коде** (не из конфига). Обновление при патчах игры: `fix_offsets.bat` → offset scanner (6 стратегий поиска, см. `offset_scanner.py`/`offset_discovery.py`). Обязательно прогонять перед боем после обновления PoE2.

**Обновление 2026-07-16:** сдвиг AreaInstance +0x20 после патча игры:

| Символ | Оффсет |
|---|---|
| `GameState.CURRENT_STATE_PTR` | 0x08 |
| `GameState.STATES_ARRAY` | 0x48 |
| `InGameState.AREA_INSTANCE` | 0x290 |
| `InGameState.UI_ROOT` | 0x2F0 |
| `InGameState.CAMERA` | 0x368 |
| `AreaInstance.AREA_INFO` | 0x0A0 |
| `AreaInstance.AREA_LEVEL` | 0x0C4 |
| `AreaInstance.AREA_HASH` | 0x11C |
| `AreaInstance.SERVER_DATA` | 0x5A0 |
| `AreaInstance.LOCAL_PLAYER` | **0x5C0** |
| `AreaInstance.AWAKE_ENTITIES` | **0x6E0** |
| `AreaInstance.SLEEPING_ENTITIES` | **0x6F0** |
| `AreaInstance.TERRAIN_METADATA` | 0x8C0 |
| `Life.HEALTH` | 0x1B0 |
| `Life.MANA` | 0x208 |
| `Life.ENERGY_SHIELD` | 0x248 |
| `VitalStruct.MAX` | 0x2C |
| `VitalStruct.CURRENT` | 0x30 |
| `ObjectMagicProperties.RARITY` | 0x144 |
| `MonsterComponent.IS_BOSS` | 0x27 |
| `Terrain.GRID_WALKABLE_DATA` | 0xD0 |

- AOB-паттерн `game_states` для поиска базы GameState
- `WORLD_TO_GRID_RATIO = 250/23` (≈10.87 клеток на единицу мира)

### game_memory.py (540 строк)

- `PROCESS_VM_READ` 0x0010 + `PROCESS_QUERY_LIMITED_INFORMATION` 0x1000
- Чтение через **NtReadVirtualMemory** (быстрее ReadProcessMemory)
- `aob_scan` — поиск паттерна по 4 KB чанкам
- `read_terrain_walkable` — распаковка nibble-packed данных (2 клетки/байт; width = bytes_per_row × 2) → walkable-грид
- `world_to_screen` — через матрицу камеры
- Имена процессов: `PathOfExile.exe`, `PathOfExileSteam.exe`, `PathOfExile_x64.exe`

### reader_bridge.py (429 строк) — pythonnet → C#

- Загрузка `PoE2ReaderLib.dll` через pythonnet (coreclr), путь из env `POE2READER_DLL`
- Init: `GetProcessesByName(PathOfExileSteam)` → `OpenProcess` → `GetModule` → `FindGsoSlot` (AOB) → `GameReader`
- `read_entities_raw` → `ReadEntities` (сырые сущности кадра)
- `_read_life_hp`: `ResolveComponents` → `Life + 0x1B0` → `VitalStruct { Max@0x2C, Current@0x30 }`
- Кэши адресов компонентов (TTL 1с) — не резолвим каждый кадр
- **`read_player_vitals()` возвращает `(hp_pct, None)`** — мана НЕ читается (строка 525) → см. баг №1

### entity_reader.py (560 строк)

- `EntityCategory(IntEnum)`: MONSTER=0, NPC=1, PLAYER=2, CHEST=3, TRANSITION=4, OTHER=5
- `Rarity(IntEnum)`: NORMAL=0, MAGIC=1, RARE=2, UNIQUE=3
- `EntityData` NamedTuple: `id, category, metadata, x, y, z, grid_x, grid_y, hp_current, hp_max, hp_ratio, rarity, is_friendly, distance, is_boss, is_alive, is_reachable, is_corpse, confidence, dead_since`
  - x = мировая X, y = мировая Z (вертикаль!), z = мировая Y (глубина) — **склейка неоднозначная, см. баг №2**
- Обход StdMap (AwakeEntities): deque + visited, кэш компонентов (Life/Render/Positioned/ObjectMagicProperties)
- `NON_COMBAT_KEYWORDS` — фильтр не-боевых сущностей

---

## 9. Ввод/управление (ViGEmBus + DualSense)

### virtual_xbox.py (171 строк)

- `VirtualXbox360` на базе `vgamepad` (VX360Gamepad)
- **heartbeat 20 Гц** — держит устройство живым
- **Единый `_lock`** на все операции (heartbeat + мост + команды бота не дерутся)
- XUSB-маски кнопок: a=0x1000, b=0x2000, x=0x4000, y=0x8000, left_bumper=0x0100, right_bumper=0x0200, dpad 0x0001–0x0008, start=0x0010, back=0x0020, left_stick=0x0040, right_stick=0x0080, guide=0x0400
- API: `tap/press/release/set_left_stick/set_right_stick/reset_state`
- Требует **прав администратора** (установка ViGEmBus-драйвера)

### dualsense_bridge.py (234 строк)

- **raw HID** (не ViGEm): VID=0x054C, PID=0x0CE6
- `_ACTIVATION = bytes([0x02] + [0xFF]*47)` — перевод геймпада в HID-режим отчётов
- Раскладка репорта: стики b1–b4, триггеры b5–b6 (L2/R2), b8 = hat + Square/Cross/Circle/Triangle, b9 = L1/R1/L2btn/R2btn/Create/Options/L3/R3, b10 = PS/Touchpad/Mute
- `_HAT_TO_DPAD` — 8 направлений
- Парсинг кнопок по **фронту** (`_edge`): срабатывание на переходе 0→1, не по удержанию
- Маппинг: Cross→A, Circle→B, Square→X, Triangle→Y
- **`suppress_triggers`** — когда бот сам жмёт флягу (L2/R2), мост НЕ перетирает виртуальный ввод своим
- **Реконнект:** 5 ошибок чтения подряд → переподключение (3 попытки); при ошибке → `vx.reset_state()` (гасит залипшие стики в игре)

**Движение:** continuous stick — держит стик полным к цели, корректирует каждые 0.02 с, отпускает у цели/коллизии/таймаута.

---

## 10. Навигация (CoreMap + NavWorker + MapRun)

### core_map.py (330 строк)

- `CoreMap`: `RATIO = off.WORLD_TO_GRID_RATIO` (250/23)
- `load(gs)`: GameState → InGameState → AREA_INSTANCE → `read_terrain_walkable` (nibble) → грид проходимости
- Реальная карта: **898×897 клеток, ~9% проходимо**
- `find_path` — A* через heapq, obstacles dict `{(gx,gy): True}` (12 сущностей → 81 клетка препятствий)
- `_nearest_walkable` — если цель непроходима (в стене), ищем ближайшую проходимую
- `collect_entity_obstacles` — через EntityReader (живые сущности становятся препятствиями)
- `move_along_path` — через `precision_move` post() (HTTP-endpoint движения стиком)
- `render_ascii` — ASCII-рендер грида для отладки

### nav_worker.py (271 строк)

- `NavWorker` — daemon-поток; читает файл `nav_target.json`: `{"wx": ..., "wy": ..., "active": true}`
- `set_target(wx, wy)` / `clear_target()`
- Статусы: `idle | loading | planning | moving | reached | error`
- `_navigate`: find_player (компоненты → Render → ppos) → CoreMap → find_path → движение по waypoint через `vx.set_left_stick` с **адаптивной длительностью** импульса (0.015–0.15 с), скорость усредняется (0.6/0.4)

### map_run_flow.py (F10) — прогон карты one-shot

Последовательность (из bot.py `_map_run_task`):
1. Подойти к сташу (world `[3950.2, 4963.2]`), tap(A)
2. R1/L1 до вкладки MAPS
3. D-pad до нужного тира карты (цель: `target_tier: 16`)
4. Стик на waystone → tap(Y) → tap(B)
5. Map Device (world `[4266.3, 4266.3]`) → tap(A) → Atlas → голубая точка → tap(A)
6. Waystone → tap(X) → TRAVERSE → tap(A) → вход в портал

**Сташ (F11):** `_stash_task` в фоновом потоке: открыть сташ, перейти в MAPS, посчитать карты по тирам (`summarize_items` из stash_flow).

---

## 11. HP-стек и авто-фляги

### CV-часть (работает по скриншоту)

| Компонент | Назначение |
|---|---|
| `hp_detector.py` | Детект орба HP по цвету/геометрии, кэш геометрии 0.05 мс |
| `hp_ocr.py` | OCR цифр HP (векторизован, 8 µs/вызов) |
| `hp_locate.py` | Fallback-поиск орба при отсутствии конфига, кэш ≥2 с |
| `hp_engine.py` | State-machine решений (когда жать флягу) |

Параметры (config.yaml):
- орб: cx=0.0732, cy=0.8712, r=0.0799 (нормированные), threshold 0.88
- фляга: left_trigger, hold 120 мс, cooldown 333 мс, tripwire=true
- hp_num: enabled, auto_locate, roi {x1:0.066, y1:0.700, x2:0.137, y2:0.735}, bin_thr 130, match_min 0.55, min_glyph_h 10, max_hp 1371

### Memory-часть (auto_flask.py, когда ридер жив)

`AutoFlaskEngine` с правилами (`auto_flask.rules`):

| Правило | Условие | Кнопка |
|---|---|---|
| HP Critical | hp_pct < 0.30 | L2 |
| HP Low | hp_pct < 0.50 | L2 |
| Mana Low | mana_pct < 0.30 | R2 |
| Enemies + Low HP | hp_pct < 0.70 и ≥3 врага | L2 |

**⚠️ ВАЖНО:** правило «Mana Low» НЕ РАБОТАЕТ — см. баг №1.

---

## 12. Хоткеи и CLI

### Хоткеи (pynput, F1–F12)

Все привязки читаются из `config.yaml → hotkeys` (bot.py:519–530) через `cfg.get(key, дефолт)`, F5 зашит жёстко (bot.py:527). **Приоритет: config.yaml → дефолт кода.**

| Клавиша | Ключ конфига | config.yaml | Дефолт кода | Действие |
|---|---|---|---|---|
| F1 | `nav` | f1 | f1 | Навигация (NavWorker: resume/stop по nav_target.json) |
| F2 | `attack` | **f2** | f10 (bot.py:524) | Toggle авто-атаки (R1) |
| F3 | `flask` | f3 | f3 | Toggle авто-фляски (L2/R2) |
| F4 | `radial` | f4 | f4 | Показать/скрыть круг радиуса атаки |
| F5 | — (зашит) | — | f5 (bot.py:527) | Reload конфига (hot reload) |
| F6 | `overlay` | f6 | f6 | Toggle оверлея |
| F7 | `diag` | f7 | f7 | Диагностика HP (улики → `_debug/hp_events`) |
| F8 | `toggle` | f8 | f8 | Toggle авто-подбора лута |
| F9 | `snapshot` | f9 | f9 | Ручной снимок (manaul_shot) |
| F10 | `map_run` | **f10** | f10 (bot.py:530) | Map run (one-shot, с lock) |
| F11 | `stash` | f11 | f11 | Stash MAPS (one-shot, с lock) |
| F12 | `quit` | f12 | f12 | Выход (graceful, с очисткой bridge) |

**Примечание:** `attack` в config.yaml = **f2**, хотя дефолт кода `cfg.get("attack", "f10")` — реальная привязка берётся из конфига (f2). `map_run` тоже из конфига (f10).

### CLI
- `python monster_db.py --kills/--name/--addr/...` — аналитика телеметрии
- `python core_map.py` — отладка/рендер карты
- `run_calibrate.bat` — калибровка HP-стека

---

## 13. Известные баги и долги

1. **🟥 mana_pct всегда None.** `reader_detect.read_player_vitals()` (строка 525) возвращает `(hp_pct, None)`. Правило «Mana Low» в auto_flask **никогда не срабатывает**. Потенциальный фикс: MANA оффсет уже есть (`Life.MANA = 0x208` в game_offsets.py) — достаточно прочитать VitalStruct для маны так же, как для HP.
2. **🟥 `entity_reader.py` grid_y берёт Z вместо Y** (зафиксировано в AGENTS.md). core_map обходит это через world_to_grid.
3. **🟨 БД 570 МБ.** dedup сдерживает, но растёт быстро; VACUUM раз в час. Кандидат на ручную чистку при работе.
4. **🟨 Мусор в корне:** файл `nul` (51 байт, артефакт редиректа), `bot.log.*` ротация, `_fight_log.txt`, десятки разовых `_diag_*/_debug_*/probe_*/watch_*` скриптов.
5. **🟨 Устаревший докстринг:** `read_entities_raw` (reader_bridge.py:305) говорит «HP не читается», но код уже читает vitals.
6. **🟨 `_player_fire_time`** — был баг смешения time.time/perf_counter (возраст 1.7e9 с), исправлен; в bot.py:1154 есть комментарий-предупреждение — не ломать снова.
7. **🟨 `attack_unknown_hp: true`** — дефолт атакует мобов без прочитанного HP вслепую (осознанное решение, но важно помнить при тюне порогов).
8. **🟨 pythonnet-риск:** если coreclr/DLL не грузится — откат на CV есть, но сам pythonnet может ронять процесс без внятного сообщения.
9. **🟨 Монолит bot.py 1213 строк** — main-цикл, хоткеи, фляги, сташ, map run в одном файле; когда начнёт мешать — разносить.

---

## 14. Производительность

| Метрика | Было | Стало |
|---|---|---|
| Полный цикл бота | ~476 мс (2.1 Гц) | **~84.9 мс (11.8 Гц)** |
| Reader update | — | **11.5 мс (87 Гц)** |
| Kill-cooldown запрос | 3.1 с | **0.6 мс** (+50.6%) |
| Запись в БД (115 с боя) | 234 000 строк | **6 319** (dedup, 37× меньше) |
| HP OCR | — | 8 µs/вызов (векторизован) |
| HP геометрия | — | 0.05 мс (кэш) |
| HP locate | — | кэш ≥2 с |

---

## 15. Тесты

| Набор | Результат |
|---|---|
| reader_detect + monster_db | 81 passed (3 fail — UI-зависимые) |
| reader_detect suite | 59/59 passed |
| hp_ocr suite | 188/190 passed |
| отдельный suite | 61/61 passed |

Запуск: `python -m pytest tests/`

---

## 16. История ключевых коммитов

| Коммит | Что сделано |
|---|---|
| `1e9867a` | Reader parsing optimization (single-pass), DB VACUUM, fire-age fix (perf_counter), ENT-logger кэш `_status_by_id` |
| `ec2f362` | HP location fix (+ тесты) |
| `0d07629` | HP-оптимизации: кэш геометрии, векторизованный OCR |

---

## 17. Рейтинг модулей по важности

| # | Модуль | Строк | Роль |
|---|---|---|---|
| 1 | `bot.py` | 1213 | Оркестратор всего |
| 2 | `reader_detect.py` | 904 | Классификация целей, ядро боя |
| 3 | `monster_db.py` | 625 | Телеметрия + kill cooldown |
| 4 | `entity_reader.py` | 560 | EntityData, обход памяти |
| 5 | `game_memory.py` | 540 | Low-level чтение памяти |
| 6 | `reader_bridge.py` | 429 | C# мост (PoE2ReaderLib) |
| 7 | `core_map.py` | 330 | Навигация / A* |
| 8 | `nav_worker.py` | 271 | Поток навигации |
| 9 | `dualsense_bridge.py` | 234 | HID-мост DualSense |
| 10 | `virtual_xbox.py` | 171 | Виртуальный геймпад |

---

## 18. Риски архитектуры (замечания ревью)

Ревью critic: **APPROVE** с тремя замечаниями по продакшн-рискам:

1. **pythonnet + PoE2ReaderLib.dll — хрупкая связка.** Если нет .NET runtime / DLL не грузится / подпись заблокирована — бот может упасть без внятного сообщения. Откат на CV есть, но сам pythonnet может ронять процесс. → Желательны явные проверки в run_bot.bat (проверка .NET, пробная загрузка clr) и try/except вокруг init.
2. **ViGEmBus: админ-права ≠ гарантия работы.** Нужен корректно установленный и подписанный драйвер; при конфликте устройств (реальный DualSense + эмулируемый Xbox) HID-события могут смешиваться. Heartbeat есть, но проверки живости устройства нет. → Желателен тест устройства при старте (`test_device`) и реконнект при dead.
3. **SQLite: WAL уже включён** (замечание про journal_mode=DELETE неактуально — код использует WAL + synchronous=NORMAL). Реальный риск: потеря последнего батча (≤50 событий) при краше в момент flush — для телеметрии некритично.

---

## 19. Диагностические скрипты (лаборатория)

В корне ~100 файлов `_diag_*`, `_debug_*`, `_bench_*`, `probe_*`, `watch_*`, `dump_*`, `calibrate_*` — интерактивный дамп памяти/боя, написанные в процессе отладки. Многие — разовые. Примеры использования:
- `_diag_*` — диагностика боя/ридера/телеметрии
- `_debug_*` — точечная отладка (классификация, кэши, фляги)
- `_bench_*` — замеры производительности (update, OCR, запись БД)
- `probe_*` — зонды памяти/оффсетов
- `watch_*` — слежение за значениями в реальном времени
- `calibrate_*` — калибровка HP-орба/окна

**При работе с проектом: не полагаться на эти скрипты как на API — это разовые инструменты отладки. Реальная логика — в модулях из разделов 3–11.**

---

*Конец документа. При обновлении кода/патчей — актуализировать разделы 8 (оффсеты), 13 (баги), 16 (коммиты).*
