"""Оффсеты памяти PoE2 для чтения сущностей.

Адаптировано из POE2GPS (Sikaka/POE2Radar, MIT).
Оффсеты могут сдвигаться при патчах игры — проверяйте обновления.

Обновление 2026-07-16: патч сдвинул AreaInstance на +0x20 (32 байта).
LOCAL_PLAYER 0x5A0→0x5C0, AWAKE_ENTITIES 0x6C0→0x6E0.
Подтверждено: Metadata читается, Entity+0x08→EntityDetails работает.
"""

# AOB паттерны для поиска корневых указателей
# Формат: (pattern_bytes, wildcard_positions, disp_offset, instr_len, description)
AOB_PATTERNS = {
    # GameStates: cmp [rip+rel32], rbp
    "game_states": (
        b"\x48\x39\x2D\x00\x00\x00\x00\x0F\x85\x16\x01\x00\x00",
        {3, 4, 5, 6},  # wildcards (null bytes in pattern)
        3,   # displacement offset within instruction
        7,   # instruction length
        "GameStates global pointer",
    ),
}

# Цепочка оффсетов от GameState до нужных данных
# Каждый оффсет — смещение от начала структуры-владельца

class GameState:
    CURRENT_STATE_PTR = 0x08       # → InGameState (StdVector deref)
    STATES_ARRAY = 0x48            # States[12] array, stride 0x10

class InGameState:
    AREA_INSTANCE = 0x290          # → AreaInstanceData
    UI_ROOT = 0x2F0                # → UiRoot
    CAMERA = 0x368                 # → Camera

class AreaInstance:
    AREA_INFO = 0x0A0              # → AreaInfo (UTF-16 area code)
    AREA_LEVEL = 0x0C4             # int: area level
    AREA_HASH = 0x11C              # uint: instance hash
    SERVER_DATA = 0x5A0            # → ServerData (candidate, shifted +0x20)
    LOCAL_PLAYER = 0x5C0           # ✓ → Entity (local player, shifted +0x20)
    AWAKE_ENTITIES = 0x6E0         # ✓ StdMap: live entity map (shifted +0x20)
    SLEEPING_ENTITIES = 0x6F0      # ✓ StdMap: inactive entities (shifted +0x20)
    TERRAIN_METADATA = 0x8C0       # ✓ → TerrainStruct (shifted +0x20)

class Entity:
    DETAILS_PTR = 0x08             # → EntityDetails
    COMPONENT_LIST = 0x10          # StdVector<IntPtr> of components
    ID = 0x80                      # uint: entity id
    IS_VALID = 0x84               # byte: bit0 clear = valid

class EntityDetails:
    NAME = 0x08                    # StdWString: metadata path
    COMPONENT_LOOKUP = 0x28        # → ComponentLookUp

class ComponentLookUp:
    BUCKET = 0x28                  # StdBucket of (NamePtr, Index)
    ENTRY_STRIDE = 0x10            # {NamePtr(8), Index(4), pad(4)}

# Компоненты сущностей
class Render:
    WORLD_POSITION = 0x138         # Vector3 {float X, Y, Z}

class Life:
    """Life component. Owner @ +0x08 (back-pointer to entity).
    VitalStruct: ReservedFlat@0x10, ReservedFraction@0x14, Regen@0x28, Max@0x2C, Current@0x30.
    Patch 2026-07-29: Life internal layout shifted (0x1B0→0x1D8, 0x208→0x1DC/?, 0x248→0x1E8)."""
    OWNER = 0x008                  # back-pointer to entity
    HEALTH = 0x1B0                 # ✓ VitalStruct — Sikaka POE2Radar live offset
    MANA = 0x208                   # VitalStruct — Sikaka POE2Radar live offset
    ENERGY_SHIELD = 0x248          # VitalStruct — Sikaka POE2Radar live offset

class VitalStruct:
    """MSVC x64 VitalStruct — 0x34 bytes total.
    Layout: ReservedFlat(0x10), ReservedFraction(0x14), Regen(0x28), Max(0x2C), Current(0x30).
    ReservedFraction — parts per 10000 (e.g. 2023 = 20.23% reserved by auras).
    Validated live from GameHelper2 + POE2Radar."""
    OWNER = 0x00                   # back-pointer to Life component
    RESERVED_FLAT = 0x10           # int: flat reserved HP/mana
    RESERVED_FRACTION = 0x14       # int: reserved fraction × 10000
    REGEN = 0x28                   # float: regen per second
    MAX = 0x2C                     # ✓ int
    CURRENT = 0x30                 # ✓ int
    SIZE = 0x34

class Positioned:
    REACTION = 0x1E0               # byte: (Reaction & 0x7F) == 1 → friendly
    PRESENCE_AOE_SCALE = 0x2A0     # float

class ObjectMagicProperties:
    RARITY = 0x144                 # int: 0=Normal, 1=Magic, 2=Rare, 3=Unique
    MODS = 0x168                   # StdVector of mod elements
    MOD_ELEM_STRIDE = 0x20
    MOD_RECORD_PTR = 0x8
    MOD_ID_STRING = 0x0

class Camera:
    WORLD_TO_SCREEN_MATRIX = 0x1A0    # float[16], row-major 4x4
    ZOOM = 0x528                      # float, == 1.0

class MonsterComponent:
    IS_BOSS = 0x27                    # byte: 0=нет, 1=босс
    FLIP_ENABLED = 0x26
    DISABLE_DEFAULT_STATS = 0x24

class Targetable:
    ATTACKABLE = 0x17
    IS_TARGETABLE = 0x18
    FORCE_TARGET = 0x19

class Terrain:
    TOTAL_TILES = 0x18                # StdTuple2D<long> (tilesX, tilesY)
    TILE_DETAILS_PTR = 0x28           # StdVector of TileStructure (0x38 bytes)
    GRID_WALKABLE_DATA = 0xD0         # StdVector — nibble-packed walkable grid
    GRID_LANDSCAPE_DATA = 0xE8
    GRID_LAYER_3 = 0x100
    GRID_LAYER_4 = 0x118
    BYTES_PER_ROW = 0x130             # int — cellsPerRow = ×2
    TILE_GRID_CELLS = 23              # tile = 23×23 grid cells

# Визуальные ID выше этого порога — декорации
VISUAL_ID_THRESHOLD = 0x40000000

# Конвертация мировых координат в сеточные
WORLD_TO_GRID_RATIO = 250.0 / 23.0  # ≈ 10.8696

# Имена компонентов для поиска
COMPONENT_RENDER = "Render"
COMPONENT_LIFE = "Life"
COMPONENT_POSITIONED = "Positioned"
COMPONENT_MAGIC_PROPS = "ObjectMagicProperties"
COMPONENT_MINIMAP_ICON = "MinimapIcon"
COMPONENT_CHEST = "Chest"
COMPONENT_MONSTER = "Monster"
COMPONENT_TARGETABLE = "Targetable"

