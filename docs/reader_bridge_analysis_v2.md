# Анализ reader_bridge.py

## Роль
Мост между Python (bot.py) и C# PoE2ReaderLib.dll. Через pythonnet загружает .NET runtime и вызывает методы C# для чтения памяти PoE2.

## Архитектура
```
PoE2 (PathOfExileSteam.exe)
    ▲
    │ ReadProcessMemory (PROCESS_VM_READ)
PoE2ReaderLib.dll (C#)
    ▲
    │ pythonnet (clr / coreclr runtime)
ReaderBridge (этот файл, Python)
    ▲
    │
bot.py → ReaderMobDetector
```

## Ключевые компоненты

### 1. Инициализация
- `_find_pid()` — находит PID PathOfExileSteam
- `initialize()` — открывает процесс, сканирует AOB паттерн GSO, создаёт GameReader
- Защита от частых перезапусков: `REINIT_INTERVAL_S = 2.0`
- Требует админ-права для `OpenProcess`

### 2. Кэширование
- `_life_cache` — addr → life_addr (результат ResolveComponents)
- `_mob_meta_cache` — addr → (life, rarity, is_boss)
- TTL = 1.0 секунда, очистка при 2048 записях

### 3. Чтение данных
- `get_game_state()` — 0 = меню, 20 = в игре
- `get_area_info()` — код, имя, хэш зоны
- `get_player_grid_pos()` — позиция в grid-координатах
- `read_entities_raw()` — сырой список сущностей с HP, позицией, типом
- `_read_life_hp()` — HP/Mana с reserved (ауры)
- `_read_player_vitals_full()` — все пулы: HP, Mana, ES, Ward, reserved

### 4. Классификация
- Characters → Player
- Monsters → m (hostile)
- Chests → c
- NPC → n
- Effects → fx
- Projectiles → p
- Terrain → t
- MiscellaneousObjects → mo

## Потенциальные проблемы
1. Требует admin-прав (OpenProcess с PROCESS_VM_READ)
2. Токсично для PoE2 — прямой доступ к памяти через pythonnet может триггерить античит
3. Нет обработки исключений при ResolveComponents — просто return (0, 0, 0, 0, 0, 0)
4. TTL кэша 1.0 сек — может запаздывать с обновлением HP моба
5. Нет rate-limiting на чтение памяти — может нагружать систему
6. Hardcoded offsets (_LifeOff, _VitalOff, _WardOff) — могут сломаться при патче игры

## Рекомендации
- Добавить мониторинг FPS/нагрузку от чтения памяти
- Увеличить TTL кэша до 2-3 сек для стабильности
- Добавить graceful degradation при ошибках ResolveComponents
- Рассмотреть замену pythonnet на более лёгкий мост (IPC/TCP)