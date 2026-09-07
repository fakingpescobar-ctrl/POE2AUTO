# POE2AUTO — Полная архитектура проекта

> **ВАЖНО:** Читай этот файл ПЕРЕД любой работой с проектом. Он содержит связи между модулями, конфиги, известные проблемы и паттерны.

---

## Обзор

POE2AUTO — это автобот для Path of Exile 2 на Python 3.14. Основной цикл работает на ~100 Hz, спавнит потоки для LootWorker, AttackWorker, overlay и Flask.

**Режим атаки** (`auto_attack.use_memory` в config.yaml):
- `reader` — C# PoE2ReaderLib.dll через pythonnet (рекомендуется)
- `memory` — PROCESS_VM_READ + AOB scan (устаревший)
- `false`/`cv` — CV-only fallback

**Каскад детекции:** reader → memory → cv (после удаления EC2)

---

## Структура файлов

### Ядро (bot.py + workers.py)
| Файл | Описание |
|------|----------|
| `bot.py` | Главный скрипт: инициализация, основной цикл ~11.8ms, AttackWorker, LootWorker |
| `workers.py` | Потоки: LootWorker (лутинг), AttackWorker (атака с CV/memory/reader) |
| `main.py` | Точка входа (1.8KB) |
| `config.py` | Загрузка/сохранение config.yaml |
| `config.yaml` | Основной конфиг (21KB) |

### Ввод (Input Layer)
| Файл | Описание |
|------|----------|
| `virtual_xbox.py` | Virtual Xbox 360 через ViGEmBus + vgamepad (heartbeat 20ms) |
| `dualsense_bridge.py` | DualSense HID (suppress_triggers, 5 retry) |
| `joy_daemon.py` | Joy daemon |

### HP Detection
| Файл | Описание |
|------|----------|
| `hp_detector.py` | Детекция HP по орбу (CV) |
| `hp_engine.py` | Движок HP: low/high/unknown/clip, confirm_frames, menu_grace |
| `hp_ocr.py` | OCR цифр HP через калиброванные шаблоны |
| `hp_locate.py` | Автопоиск ROI для цифр HP |
| `hp_diag.py` | Диагностика HP |

### Mob Detection
| Файл | Описание |
|------|----------|
| `mob_bar.py` | CV-детекция мобов по HP-барам |
| `mem_detect.py` | Прямое чтение памяти (PROCESS_VM_READ) |
| `reader_detect.py` | Детекция через C# reader (рекомендуется) |
| `entity_reader.py` | Парсинг EntityData из PoE2 |

### Auto Systems
| Файл | Описание |
|------|----------|
| `auto_flask.py` | Авто-фласк: FlaskRule, FlaskState, FlaskDecision, default_rules |
| `zone_detector.py` | Детекция зон |

### Навигация
| Файл | Описание |
|------|----------|
| `core_map.py` | A* навигация: CoreMap, grid coordinates, walkability |
| `nav_worker.py` | NavWorker: навигация через nav_target.json |
| `map_run_flow.py` | Запуск карт (F10) |
| `stash_flow.py` | Стэш (F11) |
| `stash_reader.py` | Чтение стэша |
| `stash_tab_browser.py` | Браузер табов стэша |
| `move_to_portal.py` | Движение к порталу |
| `precision_move.py` | Точное движение |

### Память
| Файл | Описание |
|------|----------|
| `game_memory.py` | Чтение памяти игры (PROCESS_VM_READ, STATUS_PARTIAL_COPY) |
| `game_offsets.py` | Оффсеты (Health, Mana, ES, Ward, reserved) |
| `monster_db.py` | SQLite телеметрия: мобы, kill events, cooldowns |
| `offset_discovery.py` | Автопоиск оффсетов |
| `offset_scanner.py` | Сканер оффсетов (51KB!) |

### Визуал
| Файл | Описание |
|------|----------|
| `overlay.py` | Overlay: HP/Mana/ES/Ward, always-on-top |
| `entity_overlay.py` | Overlay сущностей |

### Утилиты
| Файл | Описание |
|------|----------|
| `screen_capture.py` | Захват экрана (dxcam) |
| `screen.py` | Экран |
| `window.py` | Поиск окна PoE2 |
| `color_detector.py` | Детекция цвета |
| `utils.py` | Toggle, build_detector, build_loot_engine |
| `diag_dump.py` | Диагностика |
| `research.py` | Исследование |

---

## Ключевые классы

### EntityData (entity_reader.py)
```python
class EntityData(NamedTuple):
    id, category, metadata, x, y, z, grid_x, grid_y
    hp_current, hp_max, hp_ratio, rarity, is_friendly, distance
    is_boss, is_alive, is_reachable, is_corpse
    confidence: float = 0.0       # 0..1 - достоверность идентификации
    dead_since: Optional[float] = None  # timestamp смерти (perf_counter)
```

### TargetStatus (reader_detect.py)
Порядок приоритетов: DEAD → SPIRIT → LATENT → GHOST_STATIC → UNKNOWN → COMBAT.
Также: CORPSE (мертвый моб), UNREACHABLE (другой Z-уровень).

### HPEngine (hp_engine.py)
Состояния: low / high / unknown / clip. Решает: HPDecision (flask/no_flask).

### CoreMap (core_map.py)
```python
grid_x = worldX / WORLD_TO_GRID_RATIO   # RATIO = 250/23 ≈ 10.8696
grid_y = worldY / WORLD_TO_GRID_RATIO   # НЕ Y, а Z!
```
- `load(gs)` — загрузка тайлов
- `find_path(start_w, goal_w, obstacles, diagonal=True)` — A*
- `collect_entity_obstacles(area, player_world, inflate=1)` — сущности как препятствия
- `move_along_path(path, player_render, max_time)` — движение через stick

### MonsterDB (monster_db.py)
- WAL mode, dedup (10-20?), индексы `(session_id, status, ts)`
- VACUUM раз в сутки, keep_days=3
- `get_recently_killed_addrs(cooldown_s)` — SQL с EXISTS (combat→dead)

---

## Конфигурация (config.yaml — ключевые секции)

```yaml
game:
  window_title: Path of Exile 2

capture:
  backend: dxcam

auto_attack:
  enabled: true
  use_memory: reader          # reader | memory | false
  attack_range: 55.0          # в grid-координатах
  z_tolerance: 120.0
  ignore_static_mobs: true
  static_grace_s: 2.0
  corpse_timeout_s: 30.0
  death_grace_s: 2.0
  kill_cooldown_s: 5.0

auto_flask:
  enabled: true
  flask_button: left_trigger  # или right_trigger

hp:
  enabled: true
  threshold: 0.88
  flask_button: left_trigger
  flask_hold_ms: 120

loot:
  pickup_radius_px: 400
  pickup_button: a
  hold_ms: 60
  cooldown_ms: 100
```

### Горячие клавиши
F1 nav, **F2 attack**, F3 flask, F4 radial, F5 reload, F6 overlay, F7 diag, F8 toggle, F9 snapshot, **F10 map_run**, F11 stash, F12 quit.

---

## Связи между модулями

```
bot.py (главный цикл ~11.8ms)
├── VirtualXbox360 (ввод через ViGEmBus)
├── DualSenseBridge (DualSense HID)
├── ScreenCapture (dxcam)
├── GameWindow (поиск окна PoE2)
├── HPDetector / HPEngine / HPNumReader (HP)
├── LootWorker (поток)
│   ├── MobBarDetector (CV)
│   └── LootEngine
├── AttackWorker (поток)
│   ├── ReaderMobDetector (reader) ← EntityReader ← ReaderBridge (pythonnet)
│   ├── MemoryMobDetector (memory) ← GameMemory ← offsets
│   └── MobBarDetector (CV fallback)
├── AutoFlaskEngine
├── Overlay
├── CoreMap / NavWorker (навигация)
├── MonsterDB (телеметрия)
└── GameMemory (чтение памяти)
    └── game_offsets.py (Health, Mana, ES, Ward, reserved)
```

---

## Известные проблемы и паттерны

### 1. Kill cooldown deadlock (исправлено)
**Проблема:** `get_recently_killed_addrs` брал ЛЮБОЙ `status='dead'` → deadlock.
**Решение:** SQL с EXISTS: нужен combat перед dead.

### 2. "Mine" / "Carrion" в NON_COMBAT_KEYWORDS
**Проблема:** `"Mine" in "DrudgeMiners"` = True → мобы фильтровались.
**Решение:** `"/Mine"`, `"/Carrion"` (только целые слова).

### 3. RuneEncounterController
**Проблема:** Декоративные объекты детектились как враги.
**Решение:** Добавить в NON_COMBAT_KEYWORDS.

### 4. Ward vs Life (Low-Life build)
**Проблема:** CV видит Ward (16-21%), но read_player_vitals видит Life=1/1=100%.
**Решение:** Читаем Ward отдельно (BASE=0x314), hp_pct = (cur - reserved) / max.

### 5. grid_y ≠ worldY
**Важно:** `grid_y = z / RATIO` (НЕ worldY!). Это поправлено в commit 1d8e2e1.

### 6. start_bot_hidden.ps1
**Важно:** CommandLine содержит `bot.py`, НЕ `run_bot.bat`. Проверяй дубли через `Get-CimInstance Win32_Process | Where CommandLine -match 'bot\.py'`.

---

## Производительность

- Основной цикл: ~11.8ms (базовый), reader update: 87ms
- Сущностей за кадр: 1172-1328
- monster_db: WAL, VACUUM раз в сутки
- Overlay: always-on-top, отключается при потере фокуса

---

## Тесты

```bash
python -m pytest tests/test_reader_detect.py -v  # 49 тестов
python -m pytest tests/                           # 81/59/188+ тестов
```

---

## Запуск

```bash
run_bot.bat          # Обычный запуск (нужен admin для ViGEmBus)
start_bot_hidden.ps1 # Тихий запуск
```

**Python 3.14 + pythonnet** (обязательно).

---

*Последнее обновление: 17.08.2026*
*Проект: C:\Projects\POE2AUTO*
