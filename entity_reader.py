"""Чтение сущностей из памяти PoE2.

Ходит по.StdMap (AwakeEntities), для каждой сущности:
1. Читает EntityDetails → metadata path (имя)
2. Определяет тип: Monster / Npc / Player / Chest / Other
3. Через ComponentLookUp находит компонент Render → позиция
4. Через Life → текущее/максимальное HP
5. Через Positioned → реакция (дружественный/враждебный)
6. Через ObjectMagicProperties → редкость

Адаптировано из POE2GPS (Sikaka/POE2Radar, MIT).
"""
import logging
from collections import deque
from typing import Optional, Dict, List, NamedTuple
from enum import IntEnum

from game_memory import GameMemory
import game_offsets as off
from offset_discovery import OffsetDiscovery, VitalOffsets

_log = logging.getLogger("poe2auto.entities")


class EntityCategory(IntEnum):
    MONSTER = 0
    NPC = 1
    PLAYER = 2
    CHEST = 3
    TRANSITION = 4
    OTHER = 5


class Rarity(IntEnum):
    NORMAL = 0
    MAGIC = 1
    RARE = 2
    UNIQUE = 3


class EntityData(NamedTuple):
    id: int
    category: EntityCategory
    metadata: str
    x: float           # мировая координата X
    y: float           # мировая координата Z (вертикаль в PoE2)
    z: float           # мировая координата Y (глубина)
    grid_x: float      # сетевая координата
    grid_y: float      # сетевая координата
    hp_current: int
    hp_max: int
    hp_ratio: float    # 0..1
    rarity: Rarity
    is_friendly: bool
    distance: float    # расстояние от игрока (в сетевых координатах)
    is_boss: bool = False
    is_alive: bool = True


class EntityReader:
    """Читает список сущностей из памяти PoE2."""

    # Метаданные для категоризации
    MONSTER_PREFIXES = ("/Monsters/", "/Critters/")
    NPC_KEYWORDS = ("/NPC/",)
    PLAYER_KEYWORDS = ("/Characters/",)
    CHEST_KEYWORDS = ("/Chests",)
    TRANSITION_KEYWORDS = ("Transition",)
    NON_COMBAT_KEYWORDS = ("Summoned", "/Daemon/", "Invisible", "MonsterMods", "TormentedSpirit")

    def __init__(self, mem: GameMemory):
        self._mem = mem
        self._component_cache: Dict[int, Dict[str, int]] = {}  # entity_addr → {name → comp_addr}
        self._name_cache: Dict[int, str] = {}  # entity_addr → metadata path
        self._id_cache: Dict[int, int] = {}  # entity_addr → last entity id (recycle guard)
        # Self-healing vital offsets (fallback)
        self._health_off = off.Life.HEALTH
        self._mana_off = off.Life.MANA
        self._es_off = off.Life.ENERGY_SHIELD
        self._vital_offsets_resolved = False
        # Offset discovery system
        self._discovery = OffsetDiscovery(mem, self)

    def _read_entity_map(self, map_addr: int, player_pos: Optional[tuple]) -> List[EntityData]:
        """Прочитать std::map сущностей (AwakeEntities или SleepingEntities)."""
        entities = []
        root_ptr = self._mem.read_pointer(map_addr)
        size = self._mem.read_uint(map_addr + 8)
        if not root_ptr or not size:
            return entities

        head_ptr = root_ptr
        root = self._mem.read_pointer(head_ptr + 0x08)
        if not root:
            return entities

        queue = deque([root])
        visited = set()
        max_iter = min(size * 2, 5000)

        for _ in range(max_iter):
            if not queue:
                break
            node = queue.popleft()
            if node in visited or node == head_ptr:
                continue
            visited.add(node)

            node_data = self._mem.read_bytes(node, 48)
            if node_data is None or len(node_data) < 48:
                continue

            import struct
            left = struct.unpack("<Q", node_data[0:8])[0]
            right = struct.unpack("<Q", node_data[16:24])[0]
            is_nil = node_data[0x19]
            key_id = struct.unpack("<I", node_data[0x20:0x24])[0]
            entity_ptr = struct.unpack("<Q", node_data[0x28:0x30])[0]

            if left and left != head_ptr:
                queue.append(left)
            if right and right != head_ptr:
                queue.append(right)

            if is_nil:
                continue
            if key_id >= off.VISUAL_ID_THRESHOLD:
                continue
            if not entity_ptr:
                continue

            ed = self._read_entity(entity_ptr, key_id, player_pos)
            if ed is not None:
                entities.append(ed)

        return entities

    def read_entities(self, area_instance: int, player_pos: Optional[tuple] = None) -> List[EntityData]:
        """Прочитать все активные + спящие сущности.
        Возвращает список EntityData с позициями и HP.
        """
        entities = []
        entities.extend(self._read_entity_map(
            area_instance + off.AreaInstance.AWAKE_ENTITIES, player_pos))
        entities.extend(self._read_entity_map(
            area_instance + off.AreaInstance.SLEEPING_ENTITIES, player_pos))
        return entities

    def find_area_instance(self, game_state: int) -> Optional[int]:
        """Найти AreaInstance из GameState."""
        # GameState → CurrentStatePtr (StdVector) → first element → InGameState
        vec_ptr = self._mem.read_pointer(game_state + off.GameState.CURRENT_STATE_PTR)
        if not vec_ptr:
            return None

        # StdVector: first element is the active state pointer
        in_game = self._mem.read_pointer(vec_ptr)
        if not in_game:
            return None

        # InGameState → AreaInstance
        area_inst = self._mem.read_pointer(in_game + off.InGameState.AREA_INSTANCE)
        return area_inst

    def read_area_info(self, area_instance: int) -> dict:
        """Прочитать информацию о текущей зоне."""
        info = {}

        # Area code (UTF-16)
        info_ptr = self._mem.read_pointer(area_instance + off.AreaInstance.AREA_INFO)
        if info_ptr:
            code = self._mem.read_std_wstring(info_ptr)
            info["code"] = code or ""

        # Area level
        level = self._mem.read_int(area_instance + off.AreaInstance.AREA_LEVEL)
        info["level"] = level or 0

        # Area hash
        h = self._mem.read_uint(area_instance + off.AreaInstance.AREA_HASH)
        info["hash"] = h or 0

        return info

    def read_local_player(self, area_instance: int) -> Optional[EntityData]:
        """Прочитать локального игрока."""
        player_ptr = self._mem.read_pointer(area_instance + off.AreaInstance.LOCAL_PLAYER)
        if not player_ptr:
            return None
        return self._read_entity(player_ptr, 0, None)

    def _read_entity(self, entity_ptr: int, entity_id: int,
                     player_pos: Optional[tuple]) -> Optional[EntityData]:
        """Прочитать одну сущность."""
        # Entity validity
        is_valid = self._mem.read_uint(entity_ptr + off.Entity.IS_VALID, 1)
        if is_valid is not None and (is_valid & 1):
            return None

        # Recycle guard: entity addresses get reused within an area.
        # If this address now carries a different id, evict stale caches.
        prev_id = self._id_cache.get(entity_ptr)
        if prev_id is not None and prev_id != entity_id:
            self._component_cache.pop(entity_ptr, None)
            self._name_cache.pop(entity_ptr, None)
        self._id_cache[entity_ptr] = entity_id

        # EntityDetails → metadata path
        metadata = self._get_metadata(entity_ptr)
        if not metadata:
            return None

        # Category
        category = self._categorize(metadata)

        # Components
        comps = self._resolve_components(entity_ptr)

        # Position (from Render component)
        x, y, z = 0.0, 0.0, 0.0
        render_addr = comps.get(off.COMPONENT_RENDER)
        if render_addr:
            pos = self._mem.read_vec3(render_addr + off.Render.WORLD_POSITION)
            if pos:
                x, y, z = pos

        # Grid coordinates
        grid_x = x / off.WORLD_TO_GRID_RATIO
        grid_y = z / off.WORLD_TO_GRID_RATIO  # Z = глубина = grid Y

        # HP (from Life component)
        hp_cur, hp_max = 0, 0
        hp_ratio = 0.0
        life_addr = comps.get(off.COMPONENT_LIFE)
        if life_addr:
            self._ensure_vital_offsets(life_addr)
            hp_cur, hp_max = self._read_vital(life_addr + self._health_off)
            if hp_max > 0:
                hp_ratio = hp_cur / hp_max

        # Rarity
        rarity = Rarity.NORMAL
        omp_addr = comps.get(off.COMPONENT_MAGIC_PROPS)
        if omp_addr:
            r = self._mem.read_int(omp_addr + off.ObjectMagicProperties.RARITY)
            if r is not None and 0 <= r <= 3:
                rarity = Rarity(r)

        # Friendly check
        is_friendly = False
        pos_addr = comps.get(off.COMPONENT_POSITIONED)
        if pos_addr:
            reaction = self._mem.read_uint(pos_addr + off.Positioned.REACTION, 1)
            if reaction is not None:
                is_friendly = (reaction & 0x7F) == 1

        # Boss check (Monster component)
        is_boss = False
        monster_addr = comps.get(off.COMPONENT_MONSTER)
        if monster_addr:
            boss_byte = self._mem.read_uint(monster_addr + off.MonsterComponent.IS_BOSS, 1)
            if boss_byte is not None:
                is_boss = boss_byte != 0

        # Distance from player
        distance = 0.0
        if player_pos:
            px, py = player_pos
            dx = grid_x - px
            dy = grid_y - py
            distance = (dx * dx + dy * dy) ** 0.5

        return EntityData(
            id=entity_id,
            category=category,
            metadata=metadata,
            x=x, y=y, z=z,
            grid_x=grid_x, grid_y=grid_y,
            hp_current=hp_cur,
            hp_max=hp_max,
            hp_ratio=hp_ratio,
            rarity=rarity,
            is_friendly=is_friendly,
            distance=distance,
            is_boss=is_boss,
        )

    def _get_metadata(self, entity_ptr: int) -> Optional[str]:
        """Получить metadata path сущности."""
        cached = self._name_cache.get(entity_ptr)
        if cached is not None:
            return cached

        details = self._mem.read_pointer(entity_ptr + off.Entity.DETAILS_PTR)
        if not details:
            return None

        name_ptr = self._mem.read_pointer(details + off.EntityDetails.NAME)
        if not name_ptr:
            return None

        name = self._mem.read_std_wstring(name_ptr)
        if name:
            self._name_cache[entity_ptr] = name
        return name

    def _resolve_components(self, entity_ptr: int) -> Dict[str, int]:
        """Найти адреса нужных компонентов через ComponentLookUp."""
        cached = self._component_cache.get(entity_ptr)
        if cached is not None:
            return cached

        comps = {}

        # EntityDetails → ComponentLookUp
        details = self._mem.read_pointer(entity_ptr + off.Entity.DETAILS_PTR)
        if not details:
            return comps

        lookup = self._mem.read_pointer(details + off.EntityDetails.COMPONENT_LOOKUP)
        if not lookup:
            return comps

        # Component list (StdVector)
        comp_first, comp_count = self._mem.read_std_vector(
            entity_ptr + off.Entity.COMPONENT_LIST, elem_size=8,
        )
        if not comp_first or comp_count == 0:
            return comps

        # ComponentLookUp bucket
        bucket_ptr = self._mem.read_pointer(lookup + off.ComponentLookUp.BUCKET)
        if not bucket_ptr:
            return comps

        # Read bucket entries: {NamePtr(8), Index(4), pad(4)} stride=0x10
        target_names = {
            off.COMPONENT_RENDER, off.COMPONENT_LIFE,
            off.COMPONENT_POSITIONED, off.COMPONENT_MAGIC_PROPS,
            off.COMPONENT_MONSTER, off.COMPONENT_TARGETABLE,
        }

        # Read first few entries (budget)
        max_entries = 256
        for i in range(max_entries):
            entry_addr = bucket_ptr + i * off.ComponentLookUp.ENTRY_STRIDE
            entry_data = self._mem.read_bytes(entry_addr, off.ComponentLookUp.ENTRY_STRIDE)
            if entry_data is None:
                break

            import struct
            name_ptr = struct.unpack("<Q", entry_data[0:8])[0]
            index = struct.unpack("<i", entry_data[8:12])[0]

            if not name_ptr or index < 0:
                continue

            # Read component name (UTF-8 in bucket entries)
            comp_name = self._mem.read_string_utf8(name_ptr)
            if not comp_name:
                continue

            # Check if it's one of our targets
            short_name = comp_name.rsplit("/", 1)[-1] if "/" in comp_name else comp_name
            if short_name in target_names:
                # Get component address from ComponentList
                comp_addr_ptr = comp_first + index * 8
                comp_addr = self._mem.read_pointer(comp_addr_ptr)
                if comp_addr:
                    comps[short_name] = comp_addr

            # Stop if we found all targets
            if len(comps) >= len(target_names):
                break

        self._component_cache[entity_ptr] = comps
        return comps

    def _read_vital(self, address: int) -> tuple:
        """Прочитать VitalStruct (current, max).
        Layout: ReservedFlat@0x10, ReservedFraction@0x14, Regen@0x28, Max@0x2C, Current@0x30."""
        data = self._mem.read_bytes(address, off.VitalStruct.SIZE)
        if data is None:
            return 0, 0
        import struct
        max_val = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX + 4])[0]
        current = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT + 4])[0]
        return max(0, current), max(0, max_val)

    def _read_vital_unreserved(self, address: int) -> tuple:
        """Прочитать VitalStruct с учётом reserved (auras).
        Возвращает (current, unreserved_max)."""
        data = self._mem.read_bytes(address, off.VitalStruct.SIZE)
        if data is None:
            return 0, 0
        import struct
        reserved_flat = struct.unpack("<i", data[off.VitalStruct.RESERVED_FLAT:off.VitalStruct.RESERVED_FLAT + 4])[0]
        reserved_frac = struct.unpack("<i", data[off.VitalStruct.RESERVED_FRACTION:off.VitalStruct.RESERVED_FRACTION + 4])[0]
        max_val = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX + 4])[0]
        current = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT + 4])[0]
        if max_val <= 0:
            return 0, 0
        # Reserved: ceiling(frac/10000 * max) + flat
        import math
        reserved = math.ceil(reserved_frac / 10000.0 * max_val) + reserved_flat
        unreserved = max(0, max_val - reserved)
        return max(0, current), unreserved

    def _vital_looks_valid(self, address: int) -> bool:
        """Проверить, что VitalStruct выглядит валидной."""
        data = self._mem.read_bytes(address, off.VitalStruct.SIZE)
        if data is None:
            return False
        import struct
        max_val = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX + 4])[0]
        current = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT + 4])[0]
        reserved_flat = struct.unpack("<i", data[off.VitalStruct.RESERVED_FLAT:off.VitalStruct.RESERVED_FLAT + 4])[0]
        if max_val <= 0 or max_val > 10_000_000:
            return False
        if current < -max_val or current > max_val + 1:
            return False
        if reserved_flat < 0 or reserved_flat > max_val:
            return False
        return True

    def _ptr(self, address: int) -> int:
        """Safe pointer read: возвращает 0 для невалидных/мусорных адресов.
        Отсекает адреса вне user-mode пространства x64 Windows."""
        p = self._mem.read_pointer(address)
        if not p:
            return 0
        u = p & 0xFFFFFFFFFFFFFFFF  # unsigned
        if u < 0x10000 or u > 0x7FFFFFFFFFFF:
            return 0
        return p

    def _ensure_vital_offsets(self, life_addr: int):
        """Self-healing: если Health offset сломался, сканирует Life компонент
        в диапазоне 0x80–0x400 и находит валидные VitalStruct."""
        if self._vital_offsets_resolved or life_addr == 0:
            return

        # Проверяем текущий offset
        if self._vital_looks_valid(life_addr + self._health_off):
            self._vital_offsets_resolved = True
            return

        _log.warning("Health offset 0x%X invalid on Life@0x%X — scanning for VitalStructs",
                     self._health_off, life_addr)

        # Сканируем диапазон 0x80–0x400, ищем валидные VitalStruct (шаг 0x34)
        found = []
        for scan_off in range(0x80, 0x401, 4):
            if self._vital_looks_valid(life_addr + scan_off):
                found.append(scan_off)
                if len(found) >= 4:
                    break
                scan_off += 0x34  # прыгаем через структуру
            else:
                scan_off += 4

        if not found:
            _log.error("No valid VitalStruct found in Life@0x%X range 0x80-0x400", life_addr)
            return

        self._health_off = found[0]
        if len(found) >= 2:
            self._mana_off = found[1]
        if len(found) >= 3:
            self._es_off = found[2]

        self._vital_offsets_resolved = True
        _log.warning("Auto-relocated vital offsets: Health=0x%X, Mana=0x%X, ES=0x%X",
                     self._health_off, self._mana_off, self._es_off)

    def _categorize(self, metadata: str) -> EntityCategory:
        """Определить категорию сущности по metadata path."""
        # NPCs — проверяем ПЕРВЫМ (они под /Monsters/NPC/)
        for kw in self.NPC_KEYWORDS:
            if kw in metadata:
                return EntityCategory.NPC

        # Non-combat monsters
        for mp in self.MONSTER_PREFIXES:
            if mp in metadata:
                for kw in self.NON_COMBAT_KEYWORDS:
                    if kw in metadata:
                        return EntityCategory.OTHER
                return EntityCategory.MONSTER

        # Players
        for kw in self.PLAYER_KEYWORDS:
            if kw in metadata:
                return EntityCategory.PLAYER

        # Chests
        for kw in self.CHEST_KEYWORDS:
            if kw in metadata:
                return EntityCategory.CHEST

        # Transitions
        for kw in self.TRANSITION_KEYWORDS:
            if kw in metadata:
                return EntityCategory.TRANSITION

        return EntityCategory.OTHER

    def clear_cache(self):
        """Очистить кэш компонентов (вызывать при смене зоны)."""
        self._component_cache.clear()
        self._name_cache.clear()
        self._id_cache.clear()
        # Не сбрасываем _vital_offsets_resolved — offset'ы валидны в пределах сборки игры

    def initialize_discovery(self, life_addr: int = 0):
        """Инициализировать систему обнаружения оффсетов.
        Вызывать один раз при старте или при смене зоны.
        """
        if life_addr:
            self._discovery.set_life_addr(life_addr)
        self._discovery.initialize(life_addr)
        # Обновить локальные оффсеты из discovery
        self._sync_vital_offsets()

    def validate_offsets(self, life_addr: int = 0) -> bool:
        """Runtime валидация оффсетов. Вызывать периодически.
        Возвращает True если оффсеты валидны.
        """
        if life_addr:
            self._discovery.set_life_addr(life_addr)
        result = self._discovery.validate(life_addr)
        self._sync_vital_offsets()
        return result

    def force_rescan(self, life_addr: int = 0):
        """Принудительный re-scan оффсетов (F5 или смена зоны)."""
        self._discovery.force_rescan(life_addr)
        self._sync_vital_offsets()

    def _sync_vital_offsets(self):
        """Синхронизировать локальные оффсеты с discovery."""
        vitals = self._discovery.get_vital_offsets()
        self._health_off = vitals.health
        self._mana_off = vitals.mana
        self._es_off = vitals.es
        self._vital_offsets_resolved = True

    def get_discovery_stats(self) -> dict:
        """Статистика discovery системы."""
        return self._discovery.stats.copy()
