# Полный анализ poe2-reader (PoE2ReaderLib)

> Дата анализа: 2026-08-19
> Путь: `C:\Projects\POE2AUTO\poe2-reader`
> Язык: C# (.NET 8.0)

## 1. Архитектура библиотеки

Библиотека **PoE2ReaderLib** читывает память игры Path of Exile 2 через Win32 API и предоставляет высокоуровневый API для:
- Чтения позиции игрока
- Чтения матрицы камеры
- Чтения названий зон и area hash
- Обхода дерева сущностей (entity tree traversal)
- Чтения heightmap-тайлсета (terrain packed data)
- Поиска модулей и паттернов байтов (AOB scanning)

## 2. Компоненты

### 2.1 Native — Win32 P/Invoke
- `OpenProcess`, `ReadProcessMemory`, `CloseHandle`
- Константы `PROCESS_VM_READ`, `PROCESS_QUERY_INFORMATION`

### 2.2 MemoryReader — Low-level byte reader
- `ReadBytes`, `ReadU64`, `ReadU32`, `ReadU16`, `ReadByte`, `ReadFloat`, `ReadI32`

### 2.3 PatternScanner — AOB scanning
- `GetModule` — находит модуль по имени
- `FindGsoSlot` — ищет GSO слот по байтовому паттерну

### 2.4 GameReader — High-level game API
- `ReadGameState()` — 0 = not in game, 20 = in game
- `ReadPlayerPos()` — позиция игрока (x, y)
- `ReadMatrix()` — матрица камеры
- `GetScreenPos(gridX, gridY)` — screen coordinates для grid
- `ReadAreaStrings()` — название зоны (UTF-16)
- `ReadAreaHash()` — хеш текущей зоны

### 2.5 Entity Tree Traversal — Обход дерева
- Ин-ордер обход red-black tree (awake entities)
- `ReadEntities()` возвращает список: `(entity, path, x, y, z, alive)`
- Alive detection через Life component flag (0 = alive, 1 = dead corpse)
- Component resolution из lookup table

### 2.6 Terrain Reader — Heightmap
- `ReadTerrainPacked()` читает nibble-packed terrain данные
- Возвращает `(packed byte[], rows, cols)` — сжатый heightmap

## 3. Смещения (Offsets)

| Смещение | Значение | Описание |
|----------|----------|----------|
| `PlayerPosOffset` | `0x2D0` | Позиция игрока (Vector3) |
| `MatrixOffset` | `0x928` | Матрица камеры (WorldViewProjection) |
| `AreaStringsOffset` | `0x918` | Указатель на UTF-16 строки зон |
| `EntityTreeOffset` | `0x2E8` | Root-указатель на tree awake-сущностей |
| `EntityListOffset` | `0x18` | Смещение до списка child-элементов |
| `EntityPathOffset` | `0x0` | Смещение пути сущности |
| `EntityTransformOffset` | `0x10` | Смещение трансформации |
| `EntityLifeOffset` | `0x8` | Флаг жизни (0 = alive, 1 = dead) |
| `EntityComponentOffset` | `0x20` | Смещение таблицы компонентов |
| `TerrainPackedOffset` | `0x3A0` | Указатель на сплющенные тайлы |
| `TerrainWidthOffset` | `0x350` | Ширина тайлсета |
| `TerrainHeightOffset` | `0x354` | Высота тайлсета |

## 4. Риски и известные проблемы

1. **Хрупкость патчей** — все смещения захардкожены и привязаны к конкретной версии бинаря. Новый патч PoE2 сдвигает адреса, и сканер перестает работать.
2. **Red-black tree traversal** — ин-ордер обход предполагает определенную структуру node-элементов. Если игра изменит порядок или тип tree, логика сломается.
3. **UTF-16 AreaStrings** — зависит от кодирования названия зон. При смене кодирования может глючить.
4. **Packed terrain** — nibble-pack предполагает определенный формат данных. Если движок изменит compression, `ReadTerrainPacked` вернет мусор.
5. **Нет fallback на AOB scanning** — смещения захардкожены, нет graceful degradation при miss.

## 5. Рекомендации по улучшению

1. **Внедрить AOB-based discovery** — вместо жестких смещений искать паттерны байтов при старте, чтобы адаптироваться к патчам.
2. **Добавить version detection** — проверять build hash игры и подгружать соответствующие offsets.
3. **Валидация tree structure** — проверять, что node содержит expected fields перед traverse.
4. **Error handling** — обернуть P/Invoke вызовы в try/catch с fallback.
5. **Тестовая suite** — добавить unit-тесты для MemoryReader и PatternScanner.

## 6. Итоговая оценка

Библиотека **PoE2ReaderLib** — функциональный прототип для чтения памяти PoE2. Архитектурно она разделена на low-level memory access, high-level game API, и entity/terrain readers. Основной недостаток — хрупкость к патчам игры из-за hardcoded offsets. Рекомендованы AOB-based discovery и version detection для устойчивости.

**Статус:** анализ завершен, репорт сохранен.
