# PoE2 Auto-Bot: Offset Discovery & Fix System

## Проблема
PoE2 патчи сдвигают оффсеты в AreaInstance. После патча 2026-07-16 все оффсеты
AreaInstance сдвинулись на +0x20 (32 байта). Основные изменения:
- LOCAL_PLAYER: 0x5A0 → 0x5C0
- AWAKE_ENTITIES: 0x6C0 → 0x6E0
- SERVER_DATA: 0x580 → 0x5A0
- SLEEPING_ENTITIES: 0x6D0 → 0x6F0
- TERRAIN_METADATA: 0x8A0 → 0x8C0

## Подход Sikaka/POE2Radar

### 1. AOB Pattern Discovery
Главный инструмент — `POE2Radar.Research` с флагами:
- `--aob` — сканирование .text секции для AOB паттернов
- `--hp N [--mana M]` — value-scan для Life компонента, back-walk до AreaInstance
- `--find-entities [--window N]` — brute-force поиск entity std::map
- `--find-terrain [--window N]` — поиск terrain структур
- `--dump <addr>` — hex dump для ручного анализа

AOB паттерны хранятся в `AobPatterns.cs`:
```csharp
// GameStates: cmp [rip+rel32], rbp
48 39 2D ?? ?? ?? ?? 0F 85 16 01 00 00
// IngameState: mov rax, [rip+rel32]
48 8B 05 ?? ?? ?? ?? 48 89 07 48
```

RIP-relative resolution: next_instr + displacement = адрес глобального слота.
Deref слота = GameState/InGameState heap object.

FieldOffsetPattern: ищет `mov reg, [base+disp32]` с известным base-регистром,
извлекает disp32 как оффсет поля структуры.

### 2. Value-Scan Back-walk
1. Сканирование кучи (private memory) на 4-байтовое значение HP
2. Каждый кандидат = Current в VitalStruct → VitalStruct start = addr - 0x30
3. Ищем Life component: Owner ptr @ vital_start - Life_off указывает на Entity
4. Entity → EntityDetails → Metadata — подтверждение
5. Back-walk: ищем указатель на Entity в AreaInstance

### 3. Entity Map Validation
std::map<EntityKey, EntityPtr>:
- +0x00: head ptr (sentinel node)
- +0x08: size (int)
- head+0x08: parent of head = root node
- Node: Left(0), Parent(8), Right(16), IsNil(0x19), KeyId(0x20), EntityPtr(0x28)
- Validation: vtable в диапазоне модуля + metadata path

## Что сделали мы

### Создан offset_scanner.py
5 стратегий в порядке приоритета:

1. **AOB Chain** — стандартная цепь с AOB → GameState → InGameState → AreaInstance → Player
2. **Area Brute-force** — перебор InGameState.AREA_INSTANCE ±512 с валидацией по vtable + area code
3. **Player Brute-force** — перебор AreaInstance.LOCAL_PLAYER ±512 с валидацией по Metadata
4. **Entity Map Scan** — поиск std::map в диапазоне AreaInstance
5. **HP Value-scan** — поиск int в куче, back-walk до Life → Entity → AreaInstance

Дополнительно: Component Scan — resolve всех компонентов, поиск VitalStruct в Life,
поиск Vec3 в Render, поиск Rarity (0-3) в ObjectMagicProperties.

### Создан fix_offsets.ps1 / .bat
- Запускает offset_scanner.py
- Парсит результат (изменения оффсетов)
- Авто-обновление game_offsets.py
- Включение use_memory: true в config.yaml
- Vital offsets — только info (нужен --hp N для точности)

### Интеграция в mem_detect.py
`_heal_offsets()` теперь:
- Tier 1: brute-force InGameState.AREA_INSTANCE ±256 + LOCAL_PLAYER ±256
- Tier 2: OffsetScanner.full_discovery() с entity map scan и component scan
- Runtime-обновление оффсетов в памяти (off.InGameState.AREA_INSTANCE = ...)

### Подтверждённые оффсеты (live, 2026-07-29)

```python
InGameState.AREA_INSTANCE = 0x290      # → AreaInstance
InGameState.CAMERA = 0x368              # → Camera (WorldToScreenMatrix @ +0x1A0)
InGameState.UI_ROOT = 0x2F0             # → UiElement tree

AreaInstance.AREA_INFO = 0x0A0          # StdWString: area code
AreaInstance.AREA_LEVEL = 0x0C4         # int
AreaInstance.AREA_HASH = 0x11C          # uint
AreaInstance.SERVER_DATA = 0x5A0        # → ServerData
AreaInstance.LOCAL_PLAYER = 0x5C0       # → Entity (player)
AreaInstance.AWAKE_ENTITIES = 0x6E0     # StdMap: live entities
AreaInstance.SLEEPING_ENTITIES = 0x6F0  # StdMap: sleeping entities
AreaInstance.TERRAIN_METADATA = 0x8C0   # → Terrain struct

Entity.DETAILS_PTR = 0x08              # → EntityDetails
Entity.COMPONENT_LIST = 0x10           # StdVector<IntPtr>
Entity.ID = 0x80                       # uint
Entity.IS_VALID = 0x84                 # byte (bit0 = invalid)

EntityDetails.NAME = 0x08              # StdWString: Metadata path
EntityDetails.COMPONENT_LOOKUP = 0x28  # → ComponentLookUp

Render.WORLD_POSITION = 0x138          # Vec3 {float x, z, y}
Life.HEALTH = 0x1B0                    # VitalStruct (Max@+0x2C, Current@+0x30)
Life.MANA = 0x208                      # VitalStruct
Life.ENERGY_SHIELD = 0x248             # VitalStruct
Positioned.REACTION = 0x1E0            # byte: (val & 0x7F) == 1 → friendly
ObjectMagicProperties.RARITY = 0x144   # int: 0=Normal, 1=Magic, 2=Rare, 3=Unique
```

### Файлы проекта

```
C:\Projects\POE2AUTO\
├── offset_scanner.py    # Движок авто-обнаружения (5 стратегий)
├── fix_offsets.ps1      # PowerShell обёртка
├── fix_offsets.bat      # Двойной клик
├── game_offsets.py      # Оффсеты (обновлены под патч)
├── config.yaml          # use_memory: true
├── mem_detect.py        # Интегрирован OffsetScanner
├── entity_reader.py     # Чтение сущностей (без изменений)
├── offset_discovery.py  # Старая система (value-scan vitals)
└── game_memory.py       # Работа с памятью (без изменений)
```

### Как пользоваться

```powershell
# Быстрая проверка (если бот не видит мобов после патча):
.\fix_offsets.ps1 -Quick

# Полный скан с value-scan (нужно знать текущее HP):
.\fix_offsets.ps1 -HP 32758

# Или просто двойной клик по fix_offsets.bat

# Автоматически при старте бота:
# Если AOB цепь сломалась — mem_detect.py сам дёргает OffsetScanner
```

### Известные ограничения
1. Vital offsets без `--hp N` ненадёжны — слишком много false positives
2. AOB паттерн для GameState стабильный, но может сдвинуться при крупном патче
3. Component LookUp использует имена компонентов (строки) — они стабильны
4. Metadata path теперь с префиксом "data/" вместо "Metadata/" (смена нейминга в патче)
