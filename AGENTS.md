# Проект: POE2AUTO (бот для Path of Exile 2)

## Расположение
`C:\Projects\POE2AUTO` — Python-бот для Path of Exile 2 с режимом чтения памяти через PoE2ReaderLib.dll (reader mode).

## Ключевые файлы
| Файл | Назначение |
|------|------------|
| `reader_detect.py` | Детект мобов через ReaderBridge (прямое чтение памяти) |
| `entity_reader.py` | Чтение сущностей из памяти PoE2 (EntityData NamedTuple) |
| `reader_bridge.py` | Python↔C# мост (pythonnet, один процесс) |
| `monster_db.py` | SQLite телеметрия мобов: кадры, kill events, сессии |
| `ec2_detect.py` | Детект через EC2 (альтернативный бэкенд) |
| `mem_detect.py` | Детект через прямое чтение памяти (альтернативный бэкенд) |
| `bot.py` | Главный бот: AttackWorker, цикл авто-атаки |
| `core_map.py` | Ядро маппинга: где бот, где препятствия, маршрут в обход (класс `CoreMap`) |
| `config.yaml` | Конфигурация бота |

## EntityData NamedTuple (entity_reader.py)
```python
class EntityData(NamedTuple):
    id, category, metadata, x, y, z, grid_x, grid_y
    hp_current, hp_max, hp_ratio, rarity, is_friendly, distance
    is_boss, is_alive, is_reachable, is_corpse
    confidence: float = 0.0       # 0..1 — уверенность в классификации
    dead_since: Optional[float] = None  # timestamp смерти (perf_counter)
```

## TargetStatus (reader_detect.py)
Многоуровневая классификация целей: DEAD → SPIRIT → LATENT → GHOST_STATIC → UNKNOWN → COMBAT.
Также: CORPSE (недавно мёртв, дроп ещё доступен), UNREACHABLE (другой Z-слой).

## Что реализовано (2026-08-01)

### 1. Fix is_alive bug
- Разделены `is_alive` (чистый C# флаг Life+0x10) и `is_reachable` (is_alive + Z-фильтр)
- `is_reachable` используется для фильтрации хостилов в `get_all_hostiles` / `get_hostiles_in_range`

### 2. Expanded NON_COMBAT_KEYWORDS
Добавлены: Wisp, Spirit, Spectre, Phantom, Illusion, Duplicate, Clone, Shade, Wraith, Ghost, Undead, Skeleton, Rogue, TreasureGuard, Trap, Mine, Explosive, Blighted, Seething, Festering, Carrion

### 3. Corpse tracking (`is_corpse`, `_dead_track`, `corpse_timeout_s`)
- `_dead_track` dict: addr → death_timestamp (заполняется в `_update_static_track`)
- `_is_recently_dead()`: проверяет, был ли моб недавно мёртв (в пределах `corpse_timeout_s=30s`)
- `is_corpse` поле в EntityData, `CORPSE` в TargetStatus
- `_classify_target`: если is_alive=False и недавно мёртв → CORPSE (не атакуем)

### 4. Confidence scoring (#4)
- `confidence: float` (0.0–1.0) в EntityData
- `_compute_confidence()`: основан на HP данных, reachability, movement/hit history, death state
- `get_best_target()`: confidence ≥ 0.7 получает приоритет над low-confidence целями

### 5. dead_since timestamp (#5)
- `dead_since: Optional[float]` в EntityData (timestamp из `_dead_track`)
- Используется в `_classify_target` для death_grace_s проверки

### 6. death_grace_s (не атакуем мобов в анимации смерти)
- Config param `death_grace_s=2.0`
- Если `dead_since` set и в пределах grace → DEAD (даже если C# says alive)
- Защита от атаки по мобам, которые только что умерли

### 7. Kill cooldown через monster_db
- `MonsterDB.get_recently_killed_addrs(cooldown_s)`: возвращает set addr мобов, убитых ботом недавно
- Config param `kill_cooldown_s=5.0`
- `_classify_target`: если addr в recently_killed → DEAD (не атакуем, дроп ещё не поднят)
- Только активно когда `monster_db` включён

### 8. get_corpses_in_range(radius=5.0)
- Новый метод: возвращает трупы в радиусе для сбора лута
- Фильтрует по: category=MONSTER, is_corpse=True, dead_since в пределах corpse_timeout_s, distance ≤ radius

### 9. Cleanup _update_static_track
- Убран дублирующий цикл dead-tracking (были два одинаковых цикла)

## Конфигурация (config.yaml → reader_detect)
```yaml
reader_detect:
  attack_range: 15.0
  ignore_static_mobs: true
  static_grace_s: 2.0
  corpse_timeout_s: 30.0
  death_grace_s: 2.0       # не атаковать мобов, умерших <2с назад
  kill_cooldown_s: 5.0     # не атаковать мобов, убитых ботом <5с назад
  min_hp_threshold: 0
  attack_unknown_hp: true
  monster_db:
    enabled: true
    path: monster_telemetry.db
```

## Тесты
- `tests/test_reader_detect.py` — 49 тестов (было 24, добавлено 25)
- Покрывают: DEAD/SPIRIT/LATENT/GHOST_STATIC/UNKNOWN/COMBAT/CORPSE классификация, confidence scoring, dead_since, death_grace_s, kill cooldown, get_corpses_in_range, confidence-based target selection
- Запуск: `python -m pytest tests/test_reader_detect.py -v`

## Kill cooldown самоцикл — исправлен (2026-08-03)

**Симптом:** бот показывал `hostile=3 in_range=0 nearest=150`, а свежий процесс без db видел `hostile=12 in_range=5 nearest=11.2` в один кадр. Живые мобы с полным HP (`VaalBloodPriestFemale` 12281/12281) классифицировались `dead` минутами, бот их не бил.

**Причина:** `get_recently_killed_addrs` брал **любой** `status='dead'` за cooldown_s (5с) из monster_events. А `record_frame` пишет текущую классификацию каждый кадр → замкнутый цикл: моб однажды dead → в cooldown → DEAD → снова записан dead → вечный cooldown.

**Фикс:** `monster_db.py` — SQL требует EXISTS combat-строку в той же сессии ДО dead:
```sql
SELECT DISTINCT d.addr FROM monster_events d
WHERE d.session_id = ? AND d.status = 'dead' AND d.ts >= ?
AND EXISTS (SELECT 1 FROM monster_events c
            WHERE c.session_id = d.session_id AND c.addr = d.addr
              AND c.status = 'combat' AND c.ts >= ? AND c.ts <= d.ts)
```
Проверено: жрец стал combat, in_range=1, nearest=13.

**Сопутствующие правки:**
- `bot.py:416` — детектор получает **полный** `{**atk_cfg, "monster_db": cfg.get("monster_db", {})}` (раньше терялся z_tolerance=120).
- ENTD-диагностика в bot.py после ENT: `status_counts`, `fire_age`, `player_pos`, топ-6 ближайших монстров <70 grid (distance/status/hp/имя).
- **Дубль бота:** `start_bot_hidden.ps1` матчит процессы по `bot\.py` в CommandLine — не убивает бота, запущенного через `run_bot.bat`→cmd (пустая CommandLine). Если каша в bot.log — проверить `Get-CimInstance Win32_Process | Where CommandLine -match 'bot\.py'` и убить дубли вручную.

## Коммиты
- `2db8221` — Add confidence scoring and dead_since tracking to EntityData
- `860ac48` — Add death_grace_s, confidence-based target selection, get_corpses_in_range
- `75d9495` — Add kill cooldown using monster_db telemetry

## Core Mapping (ядро маппинга)

Отдельный пайплайн для бота: где бот, где препятствия, маршрут в обход.

**Файл:** `C:\Projects\POE2AUTO\core_map.py` — класс `CoreMap`.

### Координаты (ВАЖНО, правильный маппинг)
```python
grid_x = worldX / WORLD_TO_GRID_RATIO   # RATIO = 250/23 ≈ 10.8696
grid_y = worldY / WORLD_TO_GRID_RATIO   # ← из Y, НЕ из Z
```
**БАГ в `entity_reader.py`:** `grid_y = z / RATIO` — берёт высоту (Z), надо Y. Исправлять при работе с grid-координатами сущностей.

### Terrain (walkable grid)
- `AreaInstance.TERRAIN_METADATA = 0x8C0` (не разыменовывать, это база структуры)
- `Terrain.TOTAL_TILES = 0x18` → (tilesX, tilesY), в хайдауте 39×39
- `Terrain.GRID_WALKABLE_DATA = 0xD0` — StdVector, nibble-packed (2 клетки/байт)
- `Terrain.BYTES_PER_ROW = 0x130` — байт на строку (×2 = клеток на строку)
- `GameMemory.read_terrain_walkable(area)` → (w, h, bytearray 0/1)
- `GameMemory.is_walkable(td, gx, gy)`

### API CoreMap
- `load(gs)` — загрузить террейн зоны
- `world_to_grid(wx, wy)` / `grid_to_world(gx, gy)`
- `is_walkable(gx, gy)` / `is_walkable_world(wx, wy)`
- `find_path(start_w, goal_w, obstacles, diagonal=True)` — A*, обход стен и препятствий
- `collect_entity_obstacles(area, player_world, inflate=1)` — сущности зоны как препятствия
- `render_ascii(center_world, radius, obstacles, path)` — визуализация
- `move_along_path(path, player_render, max_time)` — идти по waypoint'ам через stick

### Проверено
- Путь обходит синтетическую стену (A*)
- ASCII-карта хайдаута показывает реальные стены (###), NPC и объекты (o)
- Игрок в хайдауте: world (3690.2, 3755.4) → grid (339, 345), walkable=1

---

# СВОДКА ДЛЯ АССИСТЕНТА (обновлено 16.08.2026)

> **ПОЛНЫЙ анализ проекта (архитектура, модули, БД, оффсеты, ввод, навигация, HP-стек, риски, рейтинг модулей): `Full_Analise_Project.md` в корне проекта.** Начинать с него, а не с чтения кода.

## Быстрый старт
- Запуск: `run_bot.bat` (admin для ViGEmBus), Python из `%USERPROFILE%\anaconda3` с pythonnet. **Обычный Python 3.14 НЕ подходит.**
- Три режима детекта мобов (`auto_attack.use_memory`): `reader` (⭐ C# PoE2ReaderLib.dll) / `memory` (legacy) / `false`/`cv` (fallback). Авто-откат при падении.
- Хоткеи (config.yaml): F1 nav, **F2 attack**, F3 flask, F4 radial, F5 reload (зашит), F6 overlay, F7 diag, F8 подбор, F9 snapshot, **F10 map_run**, F11 stash, F12 quit.

## Ключевые факты
- Главный цикл ~11.8 Гц (было 2.1 Гц), reader update 87 Гц, 1172–1328 сущностей/кадр.
- Классификация TargetStatus: DEAD→SPIRIT→LATENT→GHOST_STATIC→UNKNOWN→COMBAT; атака только COMBAT; `kill_cooldown_s=5.0` через MonsterDB (EXISTS-подзапрос combat→dead), `death_grace_s=2.0`, Z-tolerance 120.
- monster_db: WAL, dedup (10–20×), индекс `(session_id,status,ts)`, VACUUM 1/час, keep_days=3.
- Ввод: VirtualXbox360 (ViGEmBus, heartbeat 20 Гц) + DualSenseBridge (HID, suppress_triggers, реконнект после 5 ошибок).
- Навигация: CoreMap (RATIO=250/23, A*), NavWorker (nav_target.json), map run F10, stash F11.
- Тесты: `python -m pytest tests/` — 81/59/188+ passed в разных наборах.

## Известные баги (топ)
1. **CRITICAL: мана не читается** — `reader_detect.read_player_vitals()` (строка ~525) возвращает `(hp_pct, None)` → правило «Mana Low» авто-фляги мертво. MANA offset 0x208 уже есть в game_offsets.py — фикс дешёвый.
2. `entity_reader.py` grid_y берёт Z вместо Y (см. раздел Core Mapping выше).
3. БД ~556 МБ — периодический ручной VACUUM не помешает.
4. Конфликт хоткеев: attack=f2 в конфиге, но дефолт кода f10, и f10 занят map_run.
5. Оффсеты захардкожены — после каждого патча PoE2 прогонять `fix_offsets.bat` (последний сдвиг +0x20, 16.07.2026).
6. Монолит bot.py 1213 строк; устаревшие докстринги (read_entities_raw); мусор в корне (nul, ~100 разовых скриптов).

## Память
Полная сводка и список багов сохранены в supermemory (user scope, ключ поиска «POE2AUTO») — при необходимости делать recall.
