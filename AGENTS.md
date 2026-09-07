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
| `bot.py` | Тонкая обёртка → `bot/main_loop.py` |
| `bot/state.py` | BotState: общий контейнер состояния (~40 nonlocal → атрибуты) |
| `bot/hp.py` | HP geometry, locate HUD, flask execution |
| `bot/hotkeys.py` | Keyboard handler dispatch |
| `bot/reload.py` | Hot-reload config.yaml |
| `bot/overlay_sync.py` | Overlay state updates each frame |
| `bot/main_loop.py` | Init + main loop + cleanup |
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
> **Обновлено 17.08.2026:** После коммита `1d8e2e1` (fix(core): мана автофляги, grid_y из Y) — мана и grid_y УЖЕ ИСПРАВЛЕНЫ в коде. Дополнительно применены P1-фиксы self-healing (см. ниже). AGENTS.md и Full_Analise устарели.

1. **🟢 БД ~556 МБ** — периодический VACUUM не помешает (известный долг).
2. **🟢 Конфликт хоткеев** — attack=f2 в конфиге, map_run=f10.
3. **⚠️ Оффсеты** — `game_offsets.py` меняется с патчами PoE2, прогонять `fix_offsets.bat`.

## P1-фиксы self-healing (17.08.2026)
1. **offset_discovery.py** — раньше `_value_scan_vitals` НИКОГДА не переопределял Health (Ward/LowLife защита), а `_ensure_vital_offsets` был заблокирован флагом resolved → после сдвига патча HP мобов не чинился НИКЕМ. Теперь: если default Health НЕ валиден (патч сдвинул) — переопределяется первым кандидатом с максимальным Max. Если валиден — поведение прежнее (Ward/LowLife защита работает).
2. **mem_detect.py sanity-check** — раньше пачка >3 однотипных мобов с одинаковым HP (норма!) ложно отключала детект. Теперь: учитывается тип моба (metadata) + невалидные значения (hp_cur>hp_max, hp_max<=0, hp_max>10M), и перед отключением пробуется self-heal (validate_offsets + _ensure_vital_offsets, раз в 5с).
3. **game_memory.py** — `read_bytes` возвращает частично прочитанные байты при STATUS_PARTIAL_COPY (0x8000000D) вместо None (граница страниц).
4. **entity_reader.py** — `max_iter` поднят 5000 → 20000 (std::map дерево = size*2 узлов).

## Ward-пул здоровья: диагностика, НЕ фляги (17.08.2026, live)
**Что было:** в бою CV-орб показывал hp=16-21%, а автофляги молчали (flasks=0) — выглядело как баг.

**Разгадка (механика PoE2):** у Low-Life билда Life=1/1, вся защита в **Ward** (барьер: тает от урона, самовосстанавливается по таймеру; урон сперва съедает Ward/ES, потом Life). CV-орб читал Ward-бар (отсюда 16-21%), а `read_player_vitals` (память) честно читал Life=1/1=100%. **Фляга НЕ восстанавливает Ward** → молчание фляг — ПРАВИЛЬНОЕ поведение (лить бутылки бессмысленно, персонаж с Life=1 умрёт от первого удара по Life).

**Что сделано:**
- `game_offsets.py` — класс `Ward` (BASE=0x314, Current@+0x00, Max@+0x04 — НЕ стандартный VitalStruct! Верифицировано live: Life+0x314=2819(cur), Life+0x318=3801(max) совпало с UI).
- `reader_bridge.py` — `_read_player_vitals_full()`: читает HP+Mana+ES+Ward+**reserved** игрока; `life=[hp_max,hp_cur,mp_max,mp_cur,es_max,es_cur,ward_max,ward_cur,hp_res,mp_res]` — для диагностики. Reserved читается из того же буфера 0x98 бесплатно (`_reserved_from`: ceil(frac/10000*max)+flat).
- `read_player_vitals()` (reader_detect/mem_detect) — hp_pct = **доступный Life% = cur/(max−reserved)** (ES/Ward НЕ входят — фляги их не лечат; reserved вычитается — Low-Life держит почти весь Life под аурами). Для Low-Life (Life=1/1) hp=100% → фляги молчат; для Reserved-билдов (Life 508/1466, reserved=958) полный доступный пул = 100%, а не 35%. Тесты TestPlayerVitals документируют это.

**Live-верификация reserved (17.08.2026):** Life 508/1466, reserved=958 → доступно ровно 508 (как в UI). Mana 712/712, reserved=0. При уроне Life падает → hp_pct<1 → фляги жмутся при <порога.

## Мобы-шахтёры не атаковались: подстрока "Mine" в NON_COMBAT_KEYWORDS (17.08.2026, live)
**Симптом:** «мобы окружили, а бот пишет в зоне 0 мобов» — DrudgeMiners (DrudgePickaxeMiner/DrudgeMinerHammer, hostile=1, alive, hp=23118) игнорировались. ENT: hostile=0, все статусы dead/latent; при этом сырой C# ридер видел hostile=96 и живых мобов на d=11-31.

**Причина:** `EntityReader.NON_COMBAT_KEYWORDS` содержал голые подстроки `"Mine"` и `"Carrion"` (для мин/ловушек и падальщиков) → `"Mine" in "DrudgeMiners"` = True, `"Carrion" in "GullCarrion"` = True → категория моба становилась OTHER → отсеивался ещё в `_parse_entity` (reader_detect.py:814), даже не доходя до классификации HP. Свежий инстанс детектора (без такого фильтра) видел combat=17-24 — расхождение с ботом.

**Фикс:** `entity_reader.py` — `"Mine"` → `"/Mine"`, `"Carrion"` → `"/Carrion"` (матчатся только как сегмент пути, не внутри слова). Проверено live: 0 живых мобов с HP отсекается, детектор видит combat.

## Спавнеры-энкаунтеры (руны/монолиты) попадали в атаку (17.08.2026, live)
**Симптом:** у монолитов бот считал их «2 монстра» в радиусе — `RuneEncounterController@79` (hp=12131/12131, тип 'm', alive=True) классифицировался COMBAT и атаковался, хотя это НЕ моб: это спавнер рун, монстры появятся только ПОСЛЕ активации. То же `DoodadDaemonShardPack1` (шип-пак, hp=9783/9783) — декорация.

**Фикс:** `entity_reader.py` — в `NON_COMBAT_KEYWORDS` добавлены `"RuneEncounterController"`, `"DoodadDaemonShardPack"` → категория OTHER, в hostiles не попадают. Проверено live: 3 контроллера отсеяны, hostiles=1 (настоящий CorruptedEliteToothy).

**Важно (механика счётчика игры):** на картах с боссом-змеёй `GiantSkeletonSnakeBossMap` ридер видит 67 отдельных сущностей (сегменты/куски одного босса, все с полным HP 2681034), а игра считает его 1 монстром → расхождение «игра 33 vs ридер 68» НЕ баг. Мобы в «спящих» пачках (MonkeyJungle hp=0/49751 при alive=True) — реально hp=0 в памяти (латентные, ещё не проявлены) — ридер их не атакует, что правильно. Оффсеты HP верифицированы live: Player 508/1466, босс 2681034/2681034.

## Оверлей: ресурсы (Ж/Щ/Ward) + always-on-top (17.08.2026)
- `overlay.py` — две строки ресурсов как в HUD: **LIFE** (фактический HP-пул `hp_cur`) и **E.SHIELD** (текущий энергощит `es_cur`), значения из памяти. Цвет LIFE по hp_pct; E.SHIELD зелёный. (было «Ресурсы: Ж/Щ/W cur/max»)
- Оверлей **больше не прячется при потере фокуса игры** (раньше `if not s.foreground: withdraw()`) — всегда поверх игры (topmost). Скрытие только хоткеем. Radial-круг тоже не зависит от фокуса.

## Актуальные файлы после 1d8e2e1
- `reader_bridge.py` — читает мана одним куском 0x98 байт (HP+Mana)
- `reader_detect.py` — возвращает `(hp_pct, mana_pct)` вместо `(hp_pct, None)`
- `mem_detect.py` — использует self-healing offsets из discovery + sanity-check по типам мобов
- `entity_reader.py` — grid_y из Y (ИСПРАВЛЕНО)

## Память
Полная сводка и список багов сохранены в supermemory (user scope, ключ поиска «POE2AUTO») — при необходимости делать recall.

---

## Модульная рефакторинг (20.08.2026)

### Структура `bot/` пакета
Раньше `bot.py` был монолитом (~1100 строк, ~40 nonlocal переменных, одна функция `main()`). Разбит на модули:

```
bot.py              — тонкая обёртка: _setup_logging() + import main_loop.main()
bot_monolith.py     — оригинальный бэкап (на случай отката)
bot/
  __init__.py       — пустой (lazy imports)
  __main__.py       — `python -m bot` entry point
  state.py          — BotState(dataclass): общий контейнер состояния
  hp.py             — rebuild_hp_geometry, startup_locate, try_locate_hud, do_cv_flask, do_memory_flask
  hotkeys.py        — on_press(key, s): dispatch по хоткеям
  reload.py         — reload_cfg(s): hot-reload config.yaml
  overlay_sync.py   — sync_overlay(s): обновление ov_state каждый кадр
  main_loop.py      — main(): init + главный цикл + cleanup
```

### BotState (bot/state.py)
- Заменяет ~40 nonlocal переменных из `main()` на атрибуты
- Включает методы: `resolve_hp_config()`, `rebuild_hp_engine()`, `resolve_attack_config()`, `compute_loot_region()`, `init_auto_flask()`, `stash_task()`, `trigger_stash()`, `map_run_task()`, `trigger_map_run()`
- Паттерн: `main_loop.py` создаёт `BotState(cfg)`, передаёт во все функции как `s`

### Запуск
```bash
python bot.py              # через обёртку
python -m bot              # через __main__.py
```

### Тесты
81 тестов в `tests/` — все проходят. Старые 15 фейлов (test_mem_detect, test_offset_discovery) не затронуты.
