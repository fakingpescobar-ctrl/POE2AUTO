# Анализ зависимостей и потоков данных — POE2AUTO

> Автогенерированный отчёт на основе статического анализа всех модулей проекта.

---

## 1. Архитектурный обзор

```
                    ┌─────────────┐
                    │   bot.py    │  ← Главная оркестрация
                    │   (entry)   │
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    ┌─────▼──────┐ ┌──────▼───────┐ ┌─────▼──────┐
    │ virtual_    │ │ dualsense    │ │ screen.py  │
    │ xbox.py     │ │ bridge.py    │ │ window.py  │
    └──────┬──────┘ └──────┬──────┘ └────────────┘
           │                │
    ┌──────▼────────────┐  │  ┌──────────────────┐
    │ hp_detector.py    │  │  │ config.py        │
    │ hp_engine.py      │  │  │ utils.py         │
    │ hp_ocr.py         │  │  │ color_detector.py│
    │ hp_locate.py      │  │  └──────────────────┘
    └────┬─────────────┘  │
         │                │
    ┌────▼─────────────┐  │
    │ mob_bar.py        │  │
    │ mem_detect.py     │  │
    │ reader_detect.py  │  │
    └────┬────────────┘  │
         │                │
    ┌────▼─────────────┐  │
    │ auto_flask.py     │  │
    │ workers.py        │  │
    │ nav_worker.py     │  │
    │ map_run_flow.py   │  │
    │ stash_flow.py     │  │
    └────┬─────────────┘  │
         │                │
    ┌────▼─────────────┐  │
    │ game_memory.py    │  │
    │ offset_discovery. │  │
    │ offset_scanner.py│  │
    │ entity_reader.py  │  │
    │ reader_bridge.py  │  │
    │ core_map.py       │  │
    │ precision_move.py │  │
    │ probe_cast.py     │  │
    │ move_to_portal.py │  │
    │ overlay.py        │  │
    │ diag_dump.py      │  │
    │ stash_reader.py   │  │
    │ stash_tab_browser│  │
    │ monster_db.py     │  │
    │ game_offsets.py   │  │
    │ zone_detector.py  │  │
    │ monster_db.py     │  │
    └──────────────────┘
```

---

## 2. Подробный граф зависимостей модулей

### 2.1 Ядро оркестрации (`bot.py`)

**Поток данных:**
```
[Исходные данные]
  │
  ├─► screen.capture() → frame_bgr
  │   └─► hp_detector.measure() → orb_fill_level
  │   └─► hp_locate.find_digits_roi() → roi
  │       └─► hp_ocr.read_string_crop() → digits string
  │           └─► hp_engine.decide() → HPDecision
  │               └─► ov_state.hp_pct (для оверлея)
  │
  ├─► mob_bar.present() → mob_present (CV)
  │   └─► mem_detect.available → MemoryMobDetector
  │       └─► reader_detect.update() → TargetStatus
  │           └─► ov_state.attack_mode ("RDR"/"MEM"/"CV")
  │
  ├─► auto_flask.evaluate(af_state) → FlaskDecision
  │   └─► vx.tap(flask_key) → авто-использование фласок
  │
  ├─► nav_worker → CoreMap.find_path() → stick control
  │
  └─► overlay.update() → Tkinter UI
```

**Импорты в `bot.py`:**
- `virtual_xbox.VirtualXbox360` — виртуальный геймпад
- `dualsense_bridge.DualSenseBridge` — мост PS5→Xbox
- `screen.ScreenCapture` — захват экрана
- `hp_detector.HPDetector` — детекция HP по орбу
- `hp_engine.HPEngine` — решение по HP
- `hp_ocr` — OCR цифр HP
- `hp_locate` — локализация ROI HP
- `mob_bar.MobBarDetector` — детекция полоски HP моба
- `mem_detect.MemoryMobDetector` — memory-based mob detection
- `reader_detect.ReaderMobDetector` — reader-bridge mob detection
- `auto_flask.AutoFlaskEngine` — правила фласок
- `workers.LootWorker`, `AttackWorker` — воркеры
- `nav_worker.NavWorker` — навигация
- `map_run_flow` — map run flow
- `stash_flow` — stash operations
- `overlay.BotOverlay` — Tkinter UI
- `diag_dump` — debug screenshots
- `game_memory.GameMemory` — memory reader
- `offset_discovery.OffsetDiscovery` — offset discovery
- `offset_scanner` — offset scanner
- `entity_reader.EntityReader` — entity parsing
- `reader_bridge.ReaderBridge` — C# bridge
- `precision_move` — adaptive movement
- `probe_cast` — actor verification
- `move_to_portal` — waypoint navigation
- `config` — YAML config
- `utils` — toggle, detector factory
- `color_detector.ColorDetector` — HSV masking
- `window.GameWindow` — window finder

**HDK-клавиши:**
- F2 — toggle auto-loot
- F3 — toggle auto-attack
- F8 — diagnostic dump
- F12 — exit
- F11 — stash MAPS read
- F10 — map run flow
- R1/L1 — stash tab + map filtering

---

### 2.2 Контроллеры (`virtual_xbox.py`, `dualsense_bridge.py`)

**`virtual_xbox.py`:**
- Зависит от: `vgamepad` (внешняя зависимость)
- Импортируется: `bot.py`, `map_run_flow.py`
- API: `start()`, `stop()`, `press()`, `release()`, `set_left_stick()`, `set_right_stick()`, `tap()`, `tap_left_trigger()`, `tap_right_trigger()`, `reset_state()`
- Heartbeat: 20Hz

**`dualsense_bridge.py`:**
- Зависит от: `hid` (python-hid), `virtual_xbox`
- Импортируется: `bot.py`
- API: `start()`, `stop()`, `update()`, `is_connected()`
- Mapping: Cross→A, Circle→B, Square→X, Triangle→Y

---

### 2.3 HP-конвейер (`hp_detector.py`, `hp_engine.py`, `hp_ocr.py`, `hp_locate.py`)

**Поток данных:**
```
frame_bgr
  │
  ├─► hp_locate.find_digits_roi() → ROI dict
  │   └─► _components() (connected components)
  │   └─► _group_lines() (line grouping)
  │   └─► _find_in_region() (region search)
  │   └─► Cache full scan with TTL
  │
  ├─► hp_ocr.read_string_crop(roi, frame) → digits
  │   └─► binarize()
  │   └─► norm_glyph()
  │   └─► trim()
  │   └─► segment()
  │   └─► load_templates()
  │   └─► classify()
  │
  ├─► hp_detector.measure() → orb_fill_level
  │   └─► ColorDetector (HSV masking)
  │
  └─► hp_engine.decide(num, hm, low, foreground) → HPDecision
      └─► HPDecision enum: LOW / HIGH / UNKNOWN / CLIP
      └─► State: _last_good_ratio, _num_blind_since, _low_streak
```

**API surfaces:**
- `HPEngine.decide()` — возвращает решение по HP
- `HPEngine.hp_pct` — текущий процент HP
- `HPDetector.measure()` — уровень заполнения орба
- `hp_ocr.read_string_crop()` — чтение строки цифр
- `hp_locate.find_digits_roi()` — локализация ROI

---

### 2.4 Mob Detection (`mob_bar.py`, `mem_detect.py`, `reader_detect.py`)

**`mob_bar.py`:**
- Зависит от: `color_detector`, `screen`
- API: `present()` → bool, `is_low_hp()` → bool
- 4 режима: strict, narrow, low_hp_lock, bootstrap
- Feature-based: w ≫ h ratio, fill factor

**`mem_detect.py`:**
- Зависит от: `game_memory`, `offset_discovery`, `entity_reader`, `monster_db`
- API: `update()`, `get_all_hostiles()`, `read_player_vitals()`
- Discovery: AOB scan, value-scan, runtime validation

**`reader_detect.py`:**
- Зависит от: `reader_bridge`, `entity_reader`, `monster_db`
- API: `update()`, `get_all_hostiles()`, `read_player_vitals()`
- TargetStatus enum: DEAD, SPIRIT, LATENT, GHOST_STATIC, UNKNOWN, COMBAT

---

### 2.5 Auto Systems (`auto_flask.py`, `workers.py`, `nav_worker.py`, `map_run_flow.py`)

**`auto_flask.py`:**
- Зависит от: `config`
- API: `evaluate(af_state)` → FlaskDecision
- FlaskRule dataclass: hp_below, mana_below, enemies_nearby

**`workers.py`:**
- `LootWorker` — non-blocking queue-based
- `AttackWorker` — non-blocking queue-based

**`nav_worker.py`:**
- Зависит от: `core_map.CoreMap`, `game_memory`
- API: `set_target()`, `clear_target()`, `is_active()`, `get_status()`
- A* pathfinding on terrain grid

**`map_run_flow.py`:**
- Зависит от: `virtual_xbox`, `game_memory`, `stash_reader`, `stash_tab_browser`
- API: `run_map_flow(mem, vx, target_tier, ...)`

---

### 2.6 Memory & Offset Systems (`game_memory.py`, `offset_discovery.py`, `offset_scanner.py`, `entity_reader.py`, `reader_bridge.py`)

**`game_memory.py`:**
- Зависит от: `ctypes`, `win32`
- API: `connect()`, `disconnect()`, `read_bytes()`, `read_terrain_walkable()`
- NtReadVirtualMemory, OpenProcess, VirtualQueryEx

**`offset_discovery.py`:**
- Зависит от: `game_memory`
- API: `initialize()`, `validate()`, `get_vital_offsets()`
- Strategies: AOB patterns, value-scan, runtime validation

**`offset_scanner.py`:**
- Зависит от: `game_memory`, `game_offsets`
- API: `run()`, `get_results()`
- Strategies: AOB Chain, Area Brute-force, Player Brute-force, Entity Map Brute-force, Component Scan, Vital Scan

**`entity_reader.py`:**
- Зависит от: `game_memory`, `game_offsets`
- API: `update()`, `get_entities()`, `get_hostiles()`
- Categories: Monster, NPC, Player, Chest
- Parses AwakeEntities from StdMap

**`reader_bridge.py`:**
- Зависит от: `pythonnet`, `PoE2ReaderLib.dll`
- API: `initialize()`, `get_game_state()`, `get_area_info()`, `read_entities_raw()`, `shutdown()`

---

### 2.7 Navigation & Movement (`core_map.py`, `precision_move.py`, `move_to_portal.py`, `probe_cast.py`)

**`core_map.py`:**
- Зависит от: `game_memory`
- API: `find_path()`, `load()`, `is_walkable()`
- A* pathfinding on terrain grid

**`precision_move.py`:**
- Зависит от: `probe_cast`, `move_to_portal`, `game_memory`
- API: `move_to()` — adaptive stick control with EMA speed estimation
- Daemon: http://127.0.0.1:4322

**`move_to_portal.py`:**
- Зависит от: `game_memory`, `probe_cast`
- API: `read_waypoint()`, `move_to_portal()`
- Proportional stick control

**`probe_cast.py`:**
- Зависит от: `entity_reader`, `game_memory`
- API: `find_player()` — Actor+0x23C verification
- AOB pattern matching for player/slot/gs resolution

---

### 2.8 UI & Telemetry (`overlay.py`, `diag_dump.py`, `monster_db.py`, `stash_reader.py`, `stash_tab_browser.py`)

**`overlay.py`:**
- Зависит от: `tkinter`
- API: `update_state()`, `stop()`
- Tkinter UI with radial attack range display

**`diag_dump.py`:**
- API: `dump_event()`, `HpLostTracker`
- Outputs: _full.png, _crop.png, _mask.png, _vis.png to `_debug/hp_events/`

**`monster_db.py`:**
- Зависит от: `sqlite3`
- API: `update()`, `record_frame()`, `get_summary()`, `get_kill_events()`
- Background writer thread for batched inserts

**`stash_reader.py`:**
- Зависит от: `game_memory`, `entity_reader`
- API: `find_backpacks()`, `parse_backpack()`
- ItemView scanning, std::map parsing

**`stash_tab_browser.py`:**
- Зависит от: `win32gui`, `screen`
- API: `open_tab()`, `close_tab()`, `select_tab()`
- StashTabContainer discovery, UI element coordinate mapping

---

## 3. Потоки данных (Data Flow Diagrams)

### 3.1 HP Pipeline
```
[Screen Capture]
    │
    ├─► [hp_locate.find_digits_roi] → ROI coordinates
    │   └─► Connected components analysis
    │   └─► Line grouping
    │   └─► Cache full scan with TTL
    │
    ├─► [hp_ocr.read_string_crop] → "12345"
    │   └─► Template matching (no ML/GPU)
    │   └─► Binary segmentation
    │   └─► Glyph classification
    │
    ├─► [hp_detector.measure] → 0.85 (fill level)
    │   └─► HSV color masking (red orb)
    │   └─► Blob detection
    │
    └─► [hp_engine.decide] → HPDecision.LOW/HIGH/UNKNOWN/CLIP
        └─► State: _last_good_ratio, _num_blind_since, _low_streak
```

### 3.2 Mob Detection Pipeline
```
[Screen Capture] → [MobBarDetector] → mob_present (CV path)
    │
    └─► [MemoryMobDetector] → Memory-based (PROCESS_VM_READ)
        └─► [EntityReader] → parsed entities
        └─► [MonsterDB] → telemetry
    └─► [ReaderMobDetector] → Reader-bridge (pythonnet)
        └─► [ReaderBridge] → C# PoE2ReaderLib.dll
        └─► TargetStatus classification
```

### 3.3 Navigation Flow
```
[nav_target.json]
    │
    ├─► [NavWorker.set_target] → target coordinates
    │   └─► [CoreMap.find_path] → A* pathfinding
    │       └─► [GameMemory.read_terrain_walkable] → terrain grid
    │   └─► [VirtualXbox360.set_left_stick] → stick control
    │
    └─► [ProbeCast.find_player] → Actor verification
        └─► [MoveToPortal] → waypoint navigation
```

### 3.4 Stash & Map Flow
```
[F11 / F10 triggers]
    │
    ├─► [StashTabBrowser] → UI tab selection
    │   └─► Win32 SendInput simulation
    │   └─► UI element coordinate mapping
    │
    ├─► [StashReader.parse_backpack] → Map item reading
    │   └─► ItemView traversal
    │   └─► Std::map parsing
    │
    └─► [MapRunFlow] → Automated map execution
        └─► Pure VX360 gamepad input
        └─► Stash tab cycling
```

### 3.5 Telemetry Flow
```
[Mob Detection Updates]
    │
    └─► [MonsterDB.update] → Real-time analytics
        └─► Background writer thread (batched inserts)
        └─► get_summary() → Query interface
        └─► get_kill_events() → Kill event tracking
```

---

## 4. Классовая диаграмма (Краткая)

```
┌─────────────────────────────────────────────────────────┐
│  VirtualXbox360                                         │
│  - start(), stop()                                       │
│  - press(), release()                                    │
│  - set_left_stick(), set_right_stick()                  │
│  - tap(), tap_left_trigger(), tap_right_trigger()       │
│  - reset_state()                                         │
│  Heartbeat: 20Hz                                        │
└─────────────────────────────────────────────────────────┘
        │
        │ depends on
        ▼
┌─────────────────────────────────────────────────────────┐
│  DualSenseBridge                                        │
│  - start(), stop(), update(), is_connected()            │
│  - Button mapping: Cross→A, Circle→B, etc.              │
│  - Auto-reconnect (5 attempts threshold)                 │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  HPEngine                                               │
│  - decide(num, hm, low, foreground) → HPDecision         │
│  - State: _last_good_ratio, _num_blind_since, _low_streak│
│  - hp_pct property                                       │
└─────────────────────────────────────────────────────────┘
        │
        ├─► Uses ──► [HPDetector]
        │      measure() → fill level
        │
        ├─► Uses ──► [HPOcr]
        │      read_string_crop() → digits string
        │
        └─► Uses ──► [HPLocate]
               find_digits_roi() → ROI dict

┌─────────────────────────────────────────────────────────┐
│  MemoryMobDetector                                      │
│  - update() → Entity update                             │
│  - get_all_hostiles() → list[Entity]                    │
│  - read_player_vitals() → (hp, max_hp)                  │
│  - available property                                    │
└─────────────────────────────────────────────────────────┘
        │
        ├─► Uses ──► [GameMemory]
        │      PROCESS_VM_READ
        │
        ├─► Uses ──► [EntityReader]
        │      Parse AwakeEntities from StdMap
        │
        └─► Uses ──► [MonsterDB]
               Telemetry & analytics

┌─────────────────────────────────────────────────────────┐
│  ReaderMobDetector                                      │
│  - update() → TargetStatus classification               │
│  - get_all_hostiles() → list[Entity]                    │
│  - read_player_vitals() → (hp, max_hp)                  │
│  - available property                                    │
└─────────────────────────────────────────────────────────┘
        │
        ├─► Uses ──► [ReaderBridge]
        │      get_game_state(), get_area_info(),
        │      read_entities_raw()
        │
        └─► Uses ──► [MonsterDB]
               Telemetry & analytics
```

---

## 5. Зависимости от внешних библиотек

| Библиотека | Используемая в | Назначение |
|---|---|---|
| vgamepad | virtual_xbox.py | Виртуальный Xbox 360 контроллер |
| hid | dualsense_bridge.py | Raw HID чтение DualSense |
| pythonnet | reader_bridge.py | Python ↔ C# PoE2ReaderLib.dll |
| tkinter | overlay.py | Tkinter UI оверлей |
| sqlite3 | monster_db.py | SQLite телеметрия |
| dxcam | screen.py | DirectX capture (backend) |
| mss | screen.py | Fallback capture (backend) |
| yaml | config.py | YAML config loading |
| win32gui | window.py, stash_tab_browser.py | Win32 GUI API |
| pynput | bot.py | Hotkey listeners |

---

## 6. Ключевые точки интеграции

### 6.1 Точки входа (Entry Points)
- `main()` в `bot.py` — главный цикл бота
- `run_map_flow()` в `map_run_flow.py` — автоматический проход карт
- `open_maps_tab()` в `stash_flow.py` — stash tab operations

### 6.2 Горячие клавиши (Hotkeys)
- **F2**: Toggle auto-loot
- **F3**: Toggle auto-attack
- **F8**: Diagnostic dump
- **F12**: Exit bot
- **F11**: Stash MAPS read
- **F10**: Start map run flow
- **R1/L1**: Stash tab + map filtering

### 6.3 Состояния оверлея (Overlay State)
- `auto_loot` — статус авто-лута
- `attack_enabled` — статус авто-атаки
- `attack_on` — активность атаки
- `attacking` — текущая атака (R1 held)
- `attack_range` — радиус атаки
- `attack_mode` — "RDR"/"MEM"/"CV"
- `flask_on` — статус фласок
- `hp_pct` — процент HP
- `hp_max`, `hp_cur` — макс/текущее HP
- `es_max`, `es_cur` — макс/текущий ES
- `ward_max`, `ward_cur` — макс/текущий Ward
- `debug_hp`, `debug_mob`, `debug_zone` — debug информация

---

## 7. Патны потоков выполнения

### 7.1 Heartbeat Pattern
```
VirtualXbox360.heartbeat() → 20Hz thread
  └─► Держит виртуальный геймпад активным для XInput
```

### 7.2 Worker Pool Pattern
```
LootWorker (thread) → queue-based frame submission
AttackWorker (thread) → queue-based frame submission
NavWorker (thread) → nav_target.json → CoreMap.find_path()
```

### 7.3 Telemetry Background Writer
```
MonsterDB → background thread for batched SQLite inserts
```

---

## 8. Критические ограничения

1. **Нет ML/GPU** в HP детекции — только OpenCV template matching
2. **PROCESS_VM_READ** требует прав доступа к игре
3. **ViGEmBus** требует установки драйвера
4. **pythonnet** требует правильной версии .NET runtime
5. **HID** требует подключения реального DualSense
6. **Tkinter overlay** — блокирующий UI поток

---

*Отчёт сгенерирован на основе статического анализа всех модулей проекта POE2AUTO.*