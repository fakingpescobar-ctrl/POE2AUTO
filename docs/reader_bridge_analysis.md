# reader_bridge.py — Анализ

## Роль
Python-мост через pythonnet → C# PoE2ReaderLib.dll. Читает память PoE2 напрямую, один процесс, ноль TCP.

## Архитектура
```
PoE2 (PathOfExileSteam.exe) → ReadProcessMemory → PoE2ReaderLib.dll (C#) → pythonnet → ReaderBridge → bot.py
```

## Ключевые методы
- `initialize()` — находит PID, открывает процесс, AOB scan GSO, создаёт GameReader
- `get_game_state()` — 0=меню, 20=в игре
- `get_area_info()` — код/имя/хэш зоны
- `get_player_grid_pos()` — позиция в grid-координатах
- `read_entities_raw()` — список сущностей: addr, path, grid_pos, world_pos, entity_type, hostile, life [hp_max, hp_cur, ...]
- `_read_life_hp()` — HP/Mana + reserved (ауры)
- `_read_mob_meta()` — (life_addr, rarity, is_boss)

## Кэширование
- `_life_cache`: addr → life_addr, TTL 1.0 сек, очистка при >2048 записей
- `_mob_meta_cache`: addr → (life, rarity, is_boss), TTL 1.0 сек

## Классификация сущностей (по metadata path)
- Characters → Player (hostile=0)
- Monsters → m (hostile=1)
- Chests → c
- NPC → n
- Effects → fx
- Projectiles → p
- Terrain → t
- MiscellaneousObjects → mo

## Константы
- `DLL_PATH` — путь к PoE2ReaderLib.dll (переопределяется через POE2READER_DLL)
- `GAME_PROCESS = "PathOfExileSteam"`
- `GAME_MODULE = "PathOfExileSteam.exe"`
- `REINIT_INTERVAL_S = 2.0` — минимальный интервал между инициализациями

## Потенциальные проблемы
1. Требует admin-прав (OpenProcess с PROCESS_VM_READ)
2. Токсично для PoE2 — прямой доступ к памяти может триггерить античит
3. Нет обработки исключений при ResolveComponents
4. TTL кэша 1.0 сек — может запаздывать с обновлением HP
5. Нет rate-limiting на чтение памяти
6. Hardcoded offsets — могут сломаться при патче игры

## Зависимости
- pythonnet>=3.0 (pip install pythonnet)
- C# PoE2ReaderLib.dll (из poe2-reader/lib/bin/Release/net8.0/)
- game_offsets.py (Life, VitalStruct, Ward, ObjectMagicProperties, MonsterComponent)