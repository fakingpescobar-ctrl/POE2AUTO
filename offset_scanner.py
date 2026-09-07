"""Auto-discovery of PoE2 memory offsets.

Strategies (tried in order, first success wins):
  1. AOB Chain - AOB -> GameState -> InGameState -> AreaInstance -> Player
  2. Area Brute-force - try InGameState.AREA_INSTANCE +/- 512
  3. Player Brute-force - try AreaInstance.LOCAL_PLAYER +/- 512
  4. Entity Map Brute-force - try AreaInstance.AWAKE_ENTITIES +/- 512
  5. Component Scan - resolve Life/Render/Positioned from component list
  6. Vital Scan - find VitalStruct (HP/Mana/ES) in Life component

Usage:
  python offset_scanner.py                         # full scan
  python offset_scanner.py --quick                  # chain only (fast)
  python offset_scanner.py --hp N [--mana N]       # value-scan with known HP

Adapted from Sikaka/POE2Radar (Research --hp / --find-entities / --aob).
"""
import struct
import logging
import sys
from typing import Optional, Dict, List, Tuple

import game_offsets as off
from game_memory import GameMemory

_log = logging.getLogger("poe2auto.scanner")


class ScanResult:
    """Результат сканирования: найденные оффсеты + уровень достоверности."""

    def __init__(self):
        # Цепочка указателей
        self.game_state: int = 0
        self.in_game_state: int = 0
        self.area_instance: int = 0
        self.player: int = 0

        # Найденные оффсеты
        self.area_instance_offset: int = 0       # InGameState → AreaInstance
        self.local_player_offset: int = 0        # AreaInstance → Player
        self.server_data_offset: int = 0         # AreaInstance → ServerData
        self.awake_entities_offset: int = 0      # AreaInstance → AwakeEntities
        self.sleeping_entities_offset: int = 0   # AreaInstance → SleepingEntities
        self.terrain_offset: int = 0             # AreaInstance → Terrain
        self.camera_offset: int = 0              # InGameState → Camera

        # Оффсеты компонентов
        self.health_offset: int = 0
        self.mana_offset: int = 0
        self.es_offset: int = 0
        self.world_position_offset: int = 0
        self.rarity_offset: int = 0
        self.reaction_offset: int = 0
        self.minimap_icon_completed_offset: int = 0

        # Метаданные
        self.metadata: str = ""
        self.entity_count: int = 0
        self.strategies_tried: List[str] = []
        self.strategy_success: str = ""
        self.confidence: float = 0.0  # 0.0–1.0

    def summary(self) -> str:
        lines = [f"=== Offset Scan Result ==="]
        lines.append(f"Strategy: {self.strategy_success} (confidence={self.confidence:.0%})")
        lines.append(f"Player: {self.metadata}")
        lines.append(f"Entities: {self.entity_count}")
        lines.append("")
        lines.append("Chain:")
        lines.append(f"  GameState          = 0x{self.game_state:X}")
        lines.append(f"  InGameState        = 0x{self.in_game_state:X}")
        lines.append(f"  AreaInstance       = 0x{self.area_instance:X}")
        lines.append(f"  Player             = 0x{self.player:X}")
        lines.append("")
        lines.append("Offsets (default -> discovered):")
        def fmt_off(name, default, discovered):
            if discovered and discovered != default:
                return f"  {name} = 0x{default:X} -> 0x{discovered:X}"
            return f"  {name} = 0x{default:X} (unchanged)"
        lines.append(fmt_off("InGameState.AREA_INSTANCE", off.InGameState.AREA_INSTANCE, self.area_instance_offset))
        if self.camera_offset:
            lines.append(fmt_off("InGameState.CAMERA", off.InGameState.CAMERA, self.camera_offset))
        lines.append(fmt_off("AreaInstance.LOCAL_PLAYER", off.AreaInstance.LOCAL_PLAYER, self.local_player_offset))
        lines.append(fmt_off("AreaInstance.AWAKE_ENTITIES", off.AreaInstance.AWAKE_ENTITIES, self.awake_entities_offset))
        lines.append("")
        if self.health_offset or self.mana_offset or self.es_offset:
            lines.append("Vital offsets:")
            lines.append(f"  Life.HEALTH   = 0x{self.health_offset:X}" if self.health_offset else "")
            lines.append(f"  Life.MANA     = 0x{self.mana_offset:X}" if self.mana_offset else "")
            lines.append(f"  Life.ES       = 0x{self.es_offset:X}" if self.es_offset else "")
            lines.append("")
        if self.rarity_offset:
            lines.append(f"ObjectMagicProperties.RARITY = 0x{self.rarity_offset:X}")
        if self.reaction_offset:
            lines.append(f"Positioned.REACTION = 0x{self.reaction_offset:X}")
        if self.minimap_icon_completed_offset:
            lines.append(f"MinimapIcon.CompletedState = 0x{self.minimap_icon_completed_offset:X}")
        if self.rarity_offset or self.reaction_offset or self.minimap_icon_completed_offset:
            lines.append("")
        lines.append(f"Strategies tried: {', '.join(self.strategies_tried)}")
        return "\n".join(lines)

    def offsets_changed(self) -> bool:
        """True если хотя бы один оффсет отличается от дефолта."""
        if self.area_instance_offset and self.area_instance_offset != off.InGameState.AREA_INSTANCE:
            return True
        if self.camera_offset and self.camera_offset != off.InGameState.CAMERA:
            return True
        if self.local_player_offset and self.local_player_offset != off.AreaInstance.LOCAL_PLAYER:
            return True
        if self.awake_entities_offset and self.awake_entities_offset != off.AreaInstance.AWAKE_ENTITIES:
            return True
        if self.health_offset and self.health_offset != off.Life.HEALTH:
            return True
        if self.mana_offset and self.mana_offset != off.Life.MANA:
            return True
        if self.es_offset and self.es_offset != off.Life.ENERGY_SHIELD:
            return True
        if self.rarity_offset and self.rarity_offset != off.ObjectMagicProperties.RARITY:
            return True
        if self.reaction_offset and self.reaction_offset != off.Positioned.REACTION:
            return True
        return False


class OffsetScanner:
    """Авто-обнаружение всех оффсетов PoE2 через 5 стратегий."""

    # Максимальное отклонение для brute-force (байт)
    BRUTE_RANGE = 512
    BRUTE_STEP = 8

    def __init__(self):
        self._mem = GameMemory()
        self._result = ScanResult()

    def scan(self, hp_value: Optional[int] = None, mana_value: Optional[int] = None) -> ScanResult:
        """Запустить все стратегии по порядку.

        Args:
            hp_value: Известное HP игрока (для value-scan).
            mana_value: Известное Mana игрока (для value-scan).
        """
        if not self._mem.connect():
            return self._result

        # Стратегия 1: AOB Chain
        if self._strategy_aob_chain():
            self._result.strategies_tried.append("aob_chain")
            self._result.strategy_success = "AOB Chain"
            # Если цепь работает — всё хорошо, оффсеты в порядке
            self._result.confidence = 1.0
            self._result.area_instance_offset = off.InGameState.AREA_INSTANCE
            self._result.local_player_offset = off.AreaInstance.LOCAL_PLAYER
            self._scan_awake_entities()
            self._scan_components()
            return self._result

        # Стратегия 2: Area brute-force (InGameState → AreaInstance)
        if self._strategy_area_bruteforce():
            self._result.strategies_tried.append("area_bruteforce")
            self._result.confidence = 0.8

            # Если нашли AreaInstance — ищем LocalPlayer
            if self._local_player_heal():
                self._result.strategies_tried.append("player_heal")
                self._result.strategy_success = "Area Brute-force + Player Heal"
                self._scan_awake_entities()
                self._scan_components()
                # Пробуем entity map
                if not self._result.awake_entities_offset:
                    self._strategy_entity_map_scan()
                return self._result

            self._result.strategy_success = "Area Brute-force"
            return self._result

        # Стратегия 3: Value-scan (HP)
        if hp_value is not None:
            if self._strategy_value_scan(hp_value, mana_value):
                self._result.strategies_tried.append("value_scan")
                self._result.strategy_success = "HP Value-scan"
                self._result.confidence = 0.7
                self._scan_components()
                return self._result

        # Стратегия 4: Полный entity brute-force
        if self._strategy_entity_bruteforce():
            self._result.strategies_tried.append("entity_bruteforce")
            self._result.strategy_success = "Entity Brute-force"
            self._result.confidence = 0.6
            return self._result

        # Ничего не вышло
        self._result.strategies_tried.append("all_failed")
        self._result.confidence = 0.0
        self._disconnect()
        return self._result

    # ── Strategy 1: AOB Chain ──────────────────────────────────────────────

    def _strategy_aob_chain(self) -> bool:
        """Стандартная цепь: AOB → GameState → InGameState → AreaInstance → Player."""
        pattern, wildcards, disp_off, instr_len, desc = off.AOB_PATTERNS["game_states"]

        try:
            matches = self._mem.aob_scan(pattern, wildcards)
        except Exception:
            return False

        if not matches:
            return False

        for match_addr in matches:
            # RIP-relative resolution
            next_instr = match_addr + instr_len
            disp_data = self._mem.read_bytes(match_addr + disp_off, 4)
            if not disp_data:
                continue

            displacement = struct.unpack("<i", disp_data)[0]
            slot_addr = next_instr + displacement
            game_state = self._mem.read_pointer(slot_addr)
            if not game_state or game_state < 0x10000:
                continue

            # GameState → InGameState (пост-патч 2026-09-04: States[]-перебор)
            # КАЖДЫЙ кандидат валидируется по metadata (как C# ValidateIgs):
            # vec-deref может дать ненулевой МУСОР — тогда берётся States[i].
            in_game = 0
            _cands = []
            vec_ptr = self._mem.read_pointer(game_state + off.GameState.CURRENT_STATE_PTR)
            if vec_ptr:
                _cands.append(self._mem.read_pointer(vec_ptr))
            for _i in range(off.GameState.STATES_COUNT):
                _cands.append(self._mem.read_pointer(
                    game_state + off.GameState.STATES_ARRAY + _i * off.GameState.STATES_STRIDE))
            for _cand in _cands:
                if not _cand or _cand < 0x10000:
                    continue
                _ai = self._mem.read_pointer(_cand + off.InGameState.AREA_INSTANCE)
                if not _ai or _ai < 0x10000:
                    continue
                _pl = self._mem.read_pointer(_ai + off.AreaInstance.LOCAL_PLAYER)
                if not _pl or _pl < 0x10000:
                    continue
                _meta = self._read_metadata(_pl)
                if _meta and "Metadata/" in _meta:
                    in_game = _cand
                    break
            if not in_game:
                continue

            # InGameState → AreaInstance
            area_inst = self._mem.read_pointer(in_game + off.InGameState.AREA_INSTANCE)
            if not area_inst or area_inst < 0x10000:
                continue

            # Validate AreaInstance vtable
            area_vtable = self._mem.read_pointer(area_inst)
            if not area_vtable:
                continue
            if not (self._mem._base <= area_vtable <= self._mem._base + self._mem._size):
                continue

            # AreaInstance → Player
            player = self._mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
            if not player or player < 0x10000:
                continue

            player_vtable = self._mem.read_pointer(player)
            if not player_vtable:
                continue
            if not (self._mem._base <= player_vtable <= self._mem._base + self._mem._size):
                continue

            # Validate player metadata
            meta = self._read_metadata(player)
            if not meta or not ("Metadata/" in meta or "data/" in meta):
                continue

            # Found valid chain!
            self._result.game_state = game_state
            self._result.in_game_state = in_game
            self._result.area_instance = area_inst
            self._result.player = player
            self._result.metadata = meta
            return True

        return False

    # ── Strategy 2: Area Brute-force ───────────────────────────────────────

    def _strategy_area_bruteforce(self) -> bool:
        """Brute-force поиск AreaInstance в InGameState.

        Перебирает разные оффсеты от InGameState и ищет:
        - Указатель с vtable в диапазоне модуля
        - У которого по +0x0A0 читается валидная UTF-16 строка (area code)
        """
        # Сначала находим InGameState через AOB
        pattern, wildcards, disp_off, instr_len, desc = off.AOB_PATTERNS["game_states"]
        try:
            matches = self._mem.aob_scan(pattern, wildcards)
        except Exception:
            return False

        if not matches:
            return False

        for match_addr in matches:
            next_instr = match_addr + instr_len
            disp_data = self._mem.read_bytes(match_addr + disp_off, 4)
            if not disp_data:
                continue
            displacement = struct.unpack("<i", disp_data)[0]
            slot_addr = next_instr + displacement
            game_state = self._mem.read_pointer(slot_addr)
            if not game_state or game_state < 0x10000:
                continue

            # InGameState
            vec_ptr = self._mem.read_pointer(game_state + off.GameState.CURRENT_STATE_PTR)
            if not vec_ptr:
                continue
            in_game = self._mem.read_pointer(vec_ptr)
            if not in_game:
                continue

            self._result.game_state = game_state
            self._result.in_game_state = in_game

            # Перебираем оффсеты от InGameState (+0x200..+0x400)
            _log.info("Brute-forcing AreaInstance offset from InGameState=0x%X", in_game)
            for off_shift in range(self.BRUTE_RANGE * -1, self.BRUTE_RANGE + 1, self.BRUTE_STEP):
                area_off = off.InGameState.AREA_INSTANCE + off_shift
                if area_off < 0x100 or area_off > 0x1000:
                    continue

                area_inst = self._mem.read_pointer(in_game + area_off)
                if not area_inst or area_inst < 0x10000:
                    continue

                # Validate vtable
                area_vtable = self._mem.read_pointer(area_inst)
                if not area_vtable:
                    continue
                if not (self._mem._base <= area_vtable <= self._mem._base + self._mem._size):
                    continue

                # Validate area code at +0x0A0
                area_code = self._read_std_wstring(area_inst + off.AreaInstance.AREA_INFO)
                if area_code and len(area_code) >= 2 and area_code[0].isalpha():
                    _log.info("Area brute-force found AreaInstance at InGameState+0x%X (shift=+%d): area=%s",
                              area_off, off_shift, area_code)
                    self._result.area_instance = area_inst
                    self._result.area_instance_offset = area_off
                    return True

        return False

    def _local_player_heal(self) -> bool:
        """Поиск LocalPlayer внутри AreaInstance.

        Использует два подхода:
        a) Ищет указатель на сущность с валидной Metadata
        b) Сканирует std::map сущностей и ищет ту, у которой ComponentList содержит
           компонент PlayerComponent (уникальный для игрока)
        """
        if not self._result.area_instance:
            return False

        ai = self._result.area_instance

        # Approach (a): brute-force LocalPlayer offset
        _log.info("Brute-forcing LocalPlayer offset from AreaInstance=0x%X", ai)
        for off_shift in range(self.BRUTE_RANGE * -1, self.BRUTE_RANGE + 1, 8):
            lp_off = off.AreaInstance.LOCAL_PLAYER + off_shift
            if lp_off < 0x100 or lp_off > 0x2000:
                continue

            player = self._mem.read_pointer(ai + lp_off)
            if not player or player < 0x10000:
                continue

            player_vtable = self._mem.read_pointer(player)
            if not player_vtable:
                continue
            if not (self._mem._base <= player_vtable <= self._mem._base + self._mem._size):
                continue

            meta = self._read_metadata(player)
            if meta and ("Metadata/" in meta or "data/" in meta):
                _log.info("LocalPlayer found at AreaInstance+0x%X (shift=+%d): %s",
                          lp_off, off_shift, meta)
                self._result.player = player
                self._result.local_player_offset = lp_off
                self._result.metadata = meta
                return True

        # Approach (b): scan for entity maps, find player
        for off_shift in range(self.BRUTE_RANGE * -1, self.BRUTE_RANGE + 1, 8):
            map_off = off.AreaInstance.AWAKE_ENTITIES + off_shift
            if map_off < 0x200 or map_off > 0x2000:
                continue

            found = self._check_entity_map(ai, map_off)
            if found:
                player = self._find_player_in_entities(ai, map_off)
                if player:
                    _log.info("Player found via entity map at AreaInstance+0x%X", map_off)
                    self._result.player = player
                    self._result.awake_entities_offset = map_off
                    meta = self._read_metadata(player)
                    if meta:
                        self._result.metadata = meta
                    return True

        return False

    def _find_player_in_entities(self, area_instance: int, entities_off: int) -> Optional[int]:
        """Ищет игрока среди AwakeEntities по наличию PlayerComponent."""
        map_addr = area_instance + entities_off
        head_ptr = self._mem.read_pointer(map_addr)
        size = self._mem.read_uint(map_addr + 8)
        if not head_ptr or not size or size > 5000:
            return None

        root = self._mem.read_pointer(head_ptr + 8)
        if not root:
            return None

        queue = [root]
        visited = set()
        for _ in range(min(size * 2, 2000)):
            if not queue:
                break
            node = queue.pop(0)
            if node in visited or node == head_ptr:
                continue
            visited.add(node)

            node_data = self._mem.read_bytes(node, 48)
            if not node_data or len(node_data) < 48:
                continue

            left = struct.unpack("<Q", node_data[0:8])[0]
            right = struct.unpack("<Q", node_data[16:24])[0]
            is_nil = node_data[0x19]
            key_id = struct.unpack("<I", node_data[0x20:0x24])[0]
            entity_ptr = struct.unpack("<Q", node_data[0x28:0x30])[0]

            if left and left != head_ptr:
                queue.append(left)
            if right and right != head_ptr:
                queue.append(right)

            if is_nil or key_id >= off.VISUAL_ID_THRESHOLD or not entity_ptr:
                continue

            # Check metadata
            meta = self._read_metadata(entity_ptr)
            if meta and ("Metadata/" in meta or "data/" in meta):
                # Check for PlayerComponent
                comp_names = self._read_component_names(entity_ptr)
                if "Player" in comp_names:
                    return entity_ptr

        return None

    # ── Strategy 3: HP Value-scan ─────────────────────────────────────────

    def _strategy_value_scan(self, hp_value: int, mana_value: Optional[int] = None) -> bool:
        """Value-scan: ищем известное HP в памяти, потом back-walk до Life → Entity.

        Сначала пробует найти Life-компонент через AOB цепь (если AreaInstance
        уже найден). Иначе — сканирует кучу.
        """
        # Если AreaInstance уже есть — пробуем компонентный сканер сначала
        if self._result.area_instance:
            if self._local_player_heal():
                self._scan_components()
                if self._result.health_offset:
                    _log.info("Value-scan: found vitals via component scan (HP=0x%X)",
                              self._result.health_offset)
                    return True

        _log.info("Value-scanning for HP=%d (full heap scan)...", hp_value)
        hp_candidates = self._scan_for_value(hp_value)

        if not hp_candidates:
            _log.warning("HP value %d not found in memory", hp_value)
            return False

        _log.info("Found %d candidates for HP=%d", len(hp_candidates), hp_value)

        for addr in hp_candidates:
            vital_start = addr - off.VitalStruct.CURRENT
            if vital_start < 0x10000:
                continue

            for life_off in range(0x80, 0x400, 4):
                life_addr_candidate = vital_start - life_off
                if life_addr_candidate < 0x10000:
                    continue

                owner = self._mem.read_pointer(life_addr_candidate + off.Life.OWNER)
                if not owner or owner < 0x10000:
                    continue

                meta = self._read_metadata(owner)
                if not meta:
                    continue

                _log.info("Value-scan: found entity via Life+0x%X", life_off)
                self._result.health_offset = life_off

                if self._backwalk_to_area(owner):
                    return True

        return False

    def _scan_for_value(self, value: int, max_candidates: int = 200) -> List[int]:
        """Сканирует кучу PoE2 на 4-байтовое значение.

        Ищет в committed private memory (куча) — там живут экземпляры классов.
        """
        candidates = []

        mbi = ctypes.c_byte * ctypes.sizeof(MEMORY_BASIC_INFORMATION)
        mbi_size = ctypes.sizeof(MEMORY_BASIC_INFORMATION)

        addr = 0
        end = 0x7FFFFFFFFFFF

        while addr < end:
            info = MEMORY_BASIC_INFORMATION()
            got = kernel32.VirtualQueryEx(
                self._mem._handle,
                ctypes.c_void_p(addr),
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if got == 0:
                break

            # Сканируем только committed, private (куча) — не image/mapped
            if (info.State == MEM_COMMIT and
                    info.Type == MEM_PRIVATE and
                    info.Protect & 0x02):  # PAGE_READWRITE
                chunk = self._mem.read_bytes(info.BaseAddress, min(info.RegionSize, 0x10000))
                if chunk:
                    for i in range(0, len(chunk) - 4, 4):
                        val = struct.unpack("<i", chunk[i:i+4])[0]
                        if val == value:
                            candidates.append(info.BaseAddress + i)
                            if len(candidates) >= max_candidates:
                                return candidates

            addr = info.BaseAddress + info.RegionSize

        return candidates

    def _backwalk_to_area(self, entity_ptr: int) -> bool:
        """Идёт от entity назад до AreaInstance через цепочку указателей.

        Пробуем найти AreaInstance сканированием указателей, которые
        ссылаются на entity (т.е. parent объектов).
        """
        # Пробуем каждый известный оффсет AreaInstance
        ai = self._result.area_instance
        if not ai:
            # Если AreaInstance ещё не найден — пробуем найти через AOB
            if not self._strategy_aob_chain():
                return False
            ai = self._result.area_instance
            if not ai:
                return False

        # Проверяем что найденный Player это наша entity
        player = self._mem.read_pointer(ai + off.AreaInstance.LOCAL_PLAYER)
        if player == entity_ptr:
            self._result.player = player
            return True

        # Пробуем другие оффсеты в AreaInstance
        for off_shift in range(-256, 257, 8):
            lp_off = off.AreaInstance.LOCAL_PLAYER + off_shift
            if lp_off < 0x100 or lp_off > 0x2000:
                continue
            player = self._mem.read_pointer(ai + lp_off)
            if player == entity_ptr:
                self._result.player = player
                self._result.local_player_offset = lp_off
                self._result.metadata = self._read_metadata(player)
                return True

        return False

    # ── Strategy 4: Entity Brute-force ─────────────────────────────────────

    def _strategy_entity_bruteforce(self) -> bool:
        """Полный brute-force: найти AreaInstance, потом все остальное."""
        if not self._result.area_instance:
            # Пробуем Area brute-force сначала
            if not self._strategy_area_bruteforce():
                return False

        ai = self._result.area_instance
        if not ai:
            return False

        # Ищем LocalPlayer и entity map
        if not self._result.player:
            self._local_player_heal()

        if not self._result.awake_entities_offset:
            self._strategy_entity_map_scan()

        return self._result.player != 0

    # ── Entity Map Scan ─────────────────────────────────────────────────────

    def _strategy_entity_map_scan(self):
        """Поиск AwakeEntities std::map в AreaInstance."""
        if not self._result.area_instance:
            return

        ai = self._result.area_instance

        _log.info("Scanning for entity maps in AreaInstance=0x%X", ai)
        for off_shift in range(self.BRUTE_RANGE * -1, self.BRUTE_RANGE + 1, 8):
            map_off = off.AreaInstance.AWAKE_ENTITIES + off_shift
            if map_off < 0x200 or map_off > 0x2000:
                continue

            found = self._check_entity_map(ai, map_off)
            if found:
                _log.info("Entity map found at AreaInstance+0x%X (shift=+%d)", map_off, off_shift)
                self._result.awake_entities_offset = map_off

                # Также проверяем SleepingEntities (обычно на +0x10 от Awake)
                sleep_off = map_off + 0x10
                sleep_found = self._check_entity_map(ai, sleep_off)
                if sleep_found:
                    self._result.sleeping_entities_offset = sleep_off
                    _log.info("SleepingEntities found at AreaInstance+0x%X", sleep_off)

                break

    def _check_entity_map(self, base_addr: int, map_off: int) -> bool:
        """Проверить, является ли указатель по base+map_off std::map сущностей."""
        map_addr = base_addr + map_off
        head_ptr = self._mem.read_pointer(map_addr)
        size = self._mem.read_uint(map_addr + 8)

        if not head_ptr or not size or size > 5000 or head_ptr < 0x10000:
            return False

        std_map_head_rb = self._mem.read_pointer(head_ptr + 8)  # parent of head = root
        if not std_map_head_rb or std_map_head_rb < 0x10000:
            return False

        # Read root node and check if it's a valid entity
        node_data = self._mem.read_bytes(std_map_head_rb, 48)
        if not node_data or len(node_data) < 48:
            return False

        is_nil = node_data[0x19]
        if is_nil:
            return False

        key_id = struct.unpack("<I", node_data[0x20:0x24])[0]
        entity_ptr = struct.unpack("<Q", node_data[0x28:0x30])[0]

        if not entity_ptr or entity_ptr < 0x10000:
            return False
        if key_id >= off.VISUAL_ID_THRESHOLD:
            return False

        # Check entity vtable
        evtable = self._mem.read_pointer(entity_ptr)
        if not evtable:
            return False
        if not (self._mem._base <= evtable <= self._mem._base + self._mem._size):
            return False

        # Check entity metadata
        meta = self._read_metadata(entity_ptr)
        if meta and ("Metadata/" in meta or "data/" in meta):
            return True

        # Entity без metadata — проверяем через EntityDetails
        details = self._mem.read_pointer(entity_ptr + off.Entity.DETAILS_PTR)
        if not details:
            return False
        owner = self._mem.read_pointer(details + 0x00)
        if owner == entity_ptr:
            return True  # EntityDetails.Owner == Entity — valid

        return False

    # ── Component Scanning ─────────────────────────────────────────────────

    def _scan_components(self):
        """После нахождения Player сканирует компоненты для поиска оффсетов."""
        if not self._result.player:
            return

        player = self._result.player

        # Получаем список компонентов
        comps = self._resolve_components(player)

        # Life → VitalStruct offsets
        life_addr = comps.get("Life")
        if life_addr:
            _log.info("Life component at 0x%X, scanning for VitalStructs...", life_addr)
            self._scan_vital_offsets(life_addr)

        # Render → WorldPosition
        render_addr = comps.get("Render")
        if render_addr:
            _log.info("Render component at 0x%X, scanning for WorldPosition...", render_addr)
            self._scan_world_position(render_addr)

        # ObjectMagicProperties → Rarity
        omp_addr = comps.get("ObjectMagicProperties")
        if omp_addr:
            _log.info("ObjectMagicProperties at 0x%X, scanning for Rarity...", omp_addr)
            self._scan_rarity(omp_addr)

        # Positioned → Reaction
        pos_addr = comps.get("Positioned")
        if pos_addr:
            _log.info("Positioned at 0x%X, scanning for Reaction...", pos_addr)
            self._scan_reaction(pos_addr)

        # MinimapIcon → CompletedState
        mm_addr = comps.get("MinimapIcon")
        if mm_addr:
            _log.info("MinimapIcon at 0x%X, scanning for CompletedState...", mm_addr)
            self._scan_minimap_icon_completed(mm_addr)

    def _resolve_components(self, entity_ptr: int) -> Dict[str, int]:
        """Найти адреса всех компонентов через ComponentLookUp."""
        comps = {}

        details = self._mem.read_pointer(entity_ptr + off.Entity.DETAILS_PTR)
        if not details:
            return comps

        lookup = self._mem.read_pointer(details + off.EntityDetails.COMPONENT_LOOKUP)
        if not lookup:
            return comps

        comp_first, comp_count = self._mem.read_std_vector(
            entity_ptr + off.Entity.COMPONENT_LIST, elem_size=8,
        )
        if not comp_first or comp_count == 0:
            return comps

        bucket_ptr = self._mem.read_pointer(lookup + off.ComponentLookUp.BUCKET)
        if not bucket_ptr:
            return comps

        for i in range(64):
            entry_addr = bucket_ptr + i * off.ComponentLookUp.ENTRY_STRIDE
            entry_data = self._mem.read_bytes(entry_addr, off.ComponentLookUp.ENTRY_STRIDE)
            if not entry_data:
                break

            name_ptr = struct.unpack("<Q", entry_data[0:8])[0]
            index = struct.unpack("<i", entry_data[8:12])[0]

            if not name_ptr or index < 0:
                continue

            comp_name = self._mem.read_string_utf8(name_ptr)
            if not comp_name:
                continue

            short_name = comp_name.rsplit("/", 1)[-1] if "/" in comp_name else comp_name

            comp_addr_ptr = comp_first + index * 8
            comp_addr = self._mem.read_pointer(comp_addr_ptr)
            if comp_addr:
                comps[short_name] = comp_addr

        return comps

    def _scan_vital_offsets(self, life_addr: int):
        """Ищет VitalStruct (HP/Mana/ES) в Life компоненте.

        Собирает всех кандидатов, дедуплицирует (Max+Current),
        назначает по Max: HP > Mana > ES.
        """
        def _check_vital_at(base_off: int) -> Optional[Tuple[int, int, int]]:
            data = self._mem.read_bytes(life_addr + base_off, off.VitalStruct.SIZE)
            if not data:
                return None
            try:
                max_val = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX + 4])[0]
                current = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT + 4])[0]
                reserved_flat = struct.unpack("<i", data[off.VitalStruct.RESERVED_FLAT:off.VitalStruct.RESERVED_FLAT + 4])[0]
                regen = struct.unpack("<f", data[off.VitalStruct.REGEN:off.VitalStruct.REGEN + 4])[0]
            except Exception:
                return None

            if max_val < 50 or max_val > 10_000_000:
                return None
            if current < 0 or current > max_val:
                return None
            if reserved_flat < 0 or reserved_flat > max_val:
                return None
            if regen != regen or abs(regen) > 1e8:
                return None
            return (max_val, current, reserved_flat)

        known_bases = [off.Life.HEALTH, off.Life.MANA, off.Life.ENERGY_SHIELD]
        candidates = {}  # offset → (max, current, reserved_flat)

        # Сначала проверяем известные
        for base in known_bases:
            r = _check_vital_at(base)
            if r:
                candidates[base] = r

        # Сканируем ±0x40 от каждого известного
        for base in known_bases:
            for off_shift in range(-0x40, 0x41, 4):
                cand = base + off_shift
                if cand < 0x80 or cand > 0x400:
                    continue
                if cand in candidates:
                    continue
                r = _check_vital_at(cand)
                if r:
                    candidates[cand] = r

        if not candidates:
            _log.warning("No vital structs found in Life component")
            return

        # Дедупликация: если (max, current) уже есть — пропускаем
        seen = set()
        deduped = []
        for offs, (m, c, rf) in sorted(candidates.items(), key=lambda x: x[1][0], reverse=True):
            key = (m, c)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((offs, m, c))
            if len(deduped) >= 3:
                break

        if not deduped:
            return

        self._result.health_offset = deduped[0][0]
        if len(deduped) >= 2:
            self._result.mana_offset = deduped[1][0]
        if len(deduped) >= 3:
            self._result.es_offset = deduped[2][0]

        _log.info("Vital offsets: Health=0x%X (max=%d), Mana=0x%X (max=%d), ES=0x%X (max=%d)",
                  deduped[0][0], deduped[0][1],
                  deduped[1][0] if len(deduped) > 1 else 0, deduped[1][1] if len(deduped) > 1 else 0,
                  deduped[2][0] if len(deduped) > 2 else 0, deduped[2][1] if len(deduped) > 2 else 0)

    def _scan_world_position(self, render_addr: int):
        """Ищет WorldPosition (Vec3) в Render компоненте.

        Только дефолтный оффсет (0x138) — он стабильный.
        Не сканирует альтернативы, т.к. соседние Vec3 (0x13C) — false positives.
        """
        default = off.Render.WORLD_POSITION
        data = self._mem.read_bytes(render_addr + default, 12)
        if data:
            try:
                x, y, z = struct.unpack("<fff", data)
                valid = self._pos_looks_valid(x, y, z)
                if valid:
                    self._result.world_position_offset = default
                    _log.info("WorldPosition at Render+0x%X: (%.1f, %.1f, %.1f)", default, x, y, z)
                    return
                _log.warning("WorldPosition at default 0x%X failed validation (%.1f, %.1f, %.1f), keeping anyway",
                             default, x, y, z)
            except Exception:
                _log.warning("WorldPosition at default 0x%X: read failed, keeping anyway", default)

        # Всё равно используем дефолт — он проверен
        self._result.world_position_offset = default

    @staticmethod
    def _pos_looks_valid(x: float, y: float, z: float) -> bool:
        if abs(x) > 50000 or abs(z) > 50000:
            return False
        if y < -5000 or y > 50000:
            return False
        if abs(x) < 0.01 and abs(z) < 0.01 and abs(y) < 0.01:
            return False
        # Grid validation: Vec3(x, z, Y) → grid X = x/ratio, grid Y = z/ratio
        gx = x / off.WORLD_TO_GRID_RATIO
        gy = y / off.WORLD_TO_GRID_RATIO  # y is PoE2's Z (depth)
        if gx < -500 or gx > 5000 or gy < -500 or gy > 5000:
            return False
        return True

    def _scan_rarity(self, omp_addr: int):
        """Ищет Rarity (int: 0-3) в ObjectMagicProperties.

        Сравнивает несколько сущностей из AwakeEntities: если хотя бы у двух
        разные значения на одном оффсете — это Rarity.
        """
        default = off.ObjectMagicProperties.RARITY
        val = self._mem.read_int(omp_addr + default)
        if val is not None and 0 <= val <= 3:
            self._result.rarity_offset = default
            _log.info("Rarity at ObjectMagicProperties+0x%X (default): value=%d", default, val)
            return

        # Собираем OMP компоненты всех сущностей в EntityMap
        entity_omps = self._collect_entity_components("ObjectMagicProperties")
        if not entity_omps:
            return

        for offset in range(0x100, 0x200, 4):
            values_seen = set()
            for e_omp in entity_omps:
                v = self._mem.read_int(e_omp + offset)
                if v is not None and 0 <= v <= 3:
                    values_seen.add(v)
            if len(values_seen) >= 2:  # хотя бы 2 разных значения
                self._result.rarity_offset = offset
                _log.info("Rarity at ObjectMagicProperties+0x%X (multi-entity scan): values=%s",
                          offset, sorted(values_seen))
                return
            if len(values_seen) == 1 and list(values_seen)[0] == 0:
                pass  # все Normal — неинформативно

        _log.warning("Rarity offset not found by multi-entity scan, keeping default 0x%X", default)
        self._result.rarity_offset = default

    def _scan_reaction(self, pos_addr: int):
        """Ищет Reaction (byte: 0=hostile, 1=friendly) в Positioned компоненте."""
        default = off.Positioned.REACTION
        val = self._mem.read_uint(pos_addr + default, 1)
        if val is not None and (val & 0x7F) <= 1:
            self._result.reaction_offset = default
            _log.info("Reaction at Positioned+0x%X (default): value=0x%02X", default, val)
            return

        # Сканер через сущности: ищем offset где есть 0 и 1
        entity_pos = self._collect_entity_components("Positioned")
        if entity_pos:
            for offset in range(0x1D0, 0x1F0, 1):
                vals = set()
                for e_pos in entity_pos:
                    v = self._mem.read_uint(e_pos + offset, 1)
                    if v is not None:
                        vals.add(v & 0x7F)
                if {0, 1}.issubset(vals):
                    self._result.reaction_offset = offset
                    _log.info("Reaction at Positioned+0x%X (multi-entity scan)", offset)
                    return

        _log.warning("Reaction offset not found, keeping default 0x%X", default)
        self._result.reaction_offset = default

    def _scan_minimap_icon_completed(self, mm_addr: int):
        """Ищет CompletedState (byte: 0/1) в MinimapIcon компоненте."""
        # Ищем где есть и 0 и 1 в entity map
        entity_mm = self._collect_entity_components("MinimapIcon")
        if not entity_mm:
            return

        for offset in range(0x00, 0x40, 1):
            vals = set()
            for e_mm in entity_mm:
                v = self._mem.read_uint(e_mm + offset, 1)
                if v is not None:
                    vals.add(v)
            if {0, 1}.issubset(vals):
                self._result.minimap_icon_completed_offset = offset
                _log.info("MinimapIcon.CompletedState at MinimapIcon+0x%X (multi-entity scan)", offset)
                return

        _log.info("MinimapIcon.CompletedState: no single offset has both 0 and 1")

    def _collect_entity_components(self, component_short: str) -> List[int]:
        """Собирает адреса указанного компонента со всех сущностей в entity map."""
        addrs = []
        ai = self._result.area_instance
        if not ai:
            return addrs

        map_addr = ai + (self._result.awake_entities_offset or off.AreaInstance.AWAKE_ENTITIES)
        head_ptr = self._mem.read_pointer(map_addr)
        size = self._mem.read_uint(map_addr + 8)
        if not head_ptr or not size or size > 5000:
            return addrs

        root = self._mem.read_pointer(head_ptr + 8)
        if not root:
            return addrs

        queue = [root]
        visited = set()
        for _ in range(min(size * 2, 500)):
            if not queue:
                break
            node = queue.pop(0)
            if node in visited or node == head_ptr:
                continue
            visited.add(node)

            node_data = self._mem.read_bytes(node, 48)
            if not node_data or len(node_data) < 48:
                continue

            left = struct.unpack("<Q", node_data[0:8])[0]
            right = struct.unpack("<Q", node_data[16:24])[0]
            is_nil = node_data[0x19]
            key_id = struct.unpack("<I", node_data[0x20:0x24])[0]
            entity_ptr = struct.unpack("<Q", node_data[0x28:0x30])[0]

            if left and left != head_ptr:
                queue.append(left)
            if right and right != head_ptr:
                queue.append(right)

            if is_nil or key_id >= off.VISUAL_ID_THRESHOLD or not entity_ptr:
                continue

            comps = self._resolve_components(entity_ptr)
            addr = comps.get(component_short)
            if addr:
                addrs.append(addr)

        _log.debug("Collected %d %s components from %d entities", len(addrs), component_short, len(visited))
        return addrs

    # ── Scan AwakeEntities ──────────────────────────────────────────────────

    def _scan_awake_entities(self):
        """Проверяет AwakeEntities и считает количество сущностей."""
        if not self._result.area_instance:
            return

        ai = self._result.area_instance
        map_addr = ai + off.AreaInstance.AWAKE_ENTITIES
        head_ptr = self._mem.read_pointer(map_addr)
        size = self._mem.read_uint(map_addr + 8)

        if head_ptr and size and 0 < size < 5000:
            self._result.entity_count = size
            self._result.awake_entities_offset = off.AreaInstance.AWAKE_ENTITIES
            _log.info("AwakeEntities: %d entities at AreaInstance+0x%X", size, off.AreaInstance.AWAKE_ENTITIES)
        else:
            # Fallback: brute-force for entity maps
            self._strategy_entity_map_scan()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _read_metadata(self, entity_ptr: int) -> Optional[str]:
        """Прочитать metadata path сущности.

        ВАЖНО: name_ptr из details+0x08 — это УЖЕ указатель на wchar_t данные,
        НЕ структура std::wstring (deref давал мусор; фикс 05.09.2026).
        Копия C# ReadEntityPath: UTF-16 напрямую, печатный ASCII."""
        details = self._mem.read_pointer(entity_ptr + off.Entity.DETAILS_PTR)
        if not details:
            return None
        name_ptr = self._mem.read_pointer(details + off.EntityDetails.NAME)
        if not name_ptr or name_ptr < 0x100000000:
            return None
        buf = self._mem.read_bytes(name_ptr, 256)
        if not buf:
            return None
        out = []
        for i in range(0, min(len(buf), 512) - 1, 2):
            c = (buf[i] | (buf[i + 1] << 8)) & 0xFFFF
            if c == 0:
                break
            if 0x20 <= c <= 0x7E:
                out.append(chr(c))
            else:
                break
        return "".join(out) if out else None

    def _read_std_wstring(self, address: int) -> Optional[str]:
        """Прочитать std::wstring."""
        if not address:
            return None
        ptr = self._mem.read_pointer(address)
        if not ptr:
            return None
        if ptr > 0x10000 and ptr < 0x7FFFFFFFFFFF:
            return self._mem.read_string_utf16(ptr, max_len=256)
        return self._mem.read_string_utf16(address + 8, max_len=256)

    def _read_component_names(self, entity_ptr: int) -> set:
        """Прочитать имена всех компонентов сущности."""
        names = set()

        details = self._mem.read_pointer(entity_ptr + off.Entity.DETAILS_PTR)
        if not details:
            return names

        lookup = self._mem.read_pointer(details + off.EntityDetails.COMPONENT_LOOKUP)
        if not lookup:
            return names

        bucket_ptr = self._mem.read_pointer(lookup + off.ComponentLookUp.BUCKET)
        if not bucket_ptr:
            return names

        for i in range(64):
            entry_addr = bucket_ptr + i * off.ComponentLookUp.ENTRY_STRIDE
            entry_data = self._mem.read_bytes(entry_addr, off.ComponentLookUp.ENTRY_STRIDE)
            if not entry_data:
                break
            name_ptr = struct.unpack("<Q", entry_data[0:8])[0]
            if not name_ptr:
                continue
            comp_name = self._mem.read_string_utf8(name_ptr)
            if comp_name:
                short = comp_name.rsplit("/", 1)[-1] if "/" in comp_name else comp_name
                names.add(short)

        return names

    def _disconnect(self):
        try:
            self._mem.disconnect()
        except Exception:
            pass


# ── Windows API для Value-scan (VirtualQueryEx) ────────────────────────────
from game_memory import kernel32, MEMORY_BASIC_INFORMATION, MEM_COMMIT, MEM_PRIVATE


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="PoE2 Offset Scanner")
    parser.add_argument("--fix", action="store_true", help="Обновить game_offsets.py найденными оффсетами")
    parser.add_argument("--quick", action="store_true", help="Только цепь AOB (быстрый режим)")
    parser.add_argument("--hp", type=int, help="Известное HP игрока (для value-scan)")
    parser.add_argument("--mana", type=int, help="Известное Mana игрока (для value-scan)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Подробный лог")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    scanner = OffsetScanner()
    result = scanner.scan(hp_value=args.hp, mana_value=args.mana)

    print()
    print(result.summary())

    if args.fix and result.offsets_changed():
        print("\nOffset changes detected! Updating game_offsets.py...")
        changes = []
        import re
        with open("game_offsets.py", "r", encoding="utf-8") as f:
            src = f.read()

        # AreaInstance
        if result.area_instance_offset and result.area_instance_offset != off.InGameState.AREA_INSTANCE:
            src = re.sub(r'(class InGameState:.*?AREA_INSTANCE\s*=\s*)0x[0-9A-Fa-f]+',
                         r'\g<1>0x%X' % result.area_instance_offset, src, flags=re.DOTALL)
            changes.append("InGameState.AREA_INSTANCE")

        # Life
        if result.health_offset and result.health_offset != off.Life.HEALTH:
            src = re.sub(r'(class Life:.*?HEALTH\s*=\s*)0x[0-9A-Fa-f]+',
                         r'\g<1>0x%X' % result.health_offset, src, flags=re.DOTALL)
            changes.append("Life.HEALTH")
        if result.mana_offset and result.mana_offset != off.Life.MANA:
            src = re.sub(r'(class Life:.*?MANA\s*=\s*)0x[0-9A-Fa-f]+',
                         r'\g<1>0x%X' % result.mana_offset, src, flags=re.DOTALL)
            changes.append("Life.MANA")
        if result.es_offset and result.es_offset != off.Life.ENERGY_SHIELD:
            src = re.sub(r'(class Life:.*?ENERGY_SHIELD\s*=\s*)0x[0-9A-Fa-f]+',
                         r'\g<1>0x%X' % result.es_offset, src, flags=re.DOTALL)
            changes.append("Life.ENERGY_SHIELD")

        # WorldPosition
        if result.world_position_offset and result.world_position_offset != off.Render.WORLD_POSITION:
            src = re.sub(r'(class Render:.*?WORLD_POSITION\s*=\s*)0x[0-9A-Fa-f]+',
                         r'\g<1>0x%X' % result.world_position_offset, src, flags=re.DOTALL)
            changes.append("Render.WORLD_POSITION")

        # Rarity / Reaction
        if result.rarity_offset and result.rarity_offset != off.ObjectMagicProperties.RARITY:
            src = re.sub(r'(class ObjectMagicProperties:.*?RARITY\s*=\s*)0x[0-9A-Fa-f]+',
                         r'\g<1>0x%X' % result.rarity_offset, src, flags=re.DOTALL)
            changes.append("ObjectMagicProperties.RARITY")
        if result.reaction_offset and result.reaction_offset != off.Positioned.REACTION:
            src = re.sub(r'(class Positioned:.*?REACTION\s*=\s*)0x[0-9A-Fa-f]+',
                         r'\g<1>0x%X' % result.reaction_offset, src, flags=re.DOTALL)
            changes.append("Positioned.REACTION")

        with open("game_offsets.py", "w", encoding="utf-8") as f:
            f.write(src)

        print(f"Updated: {', '.join(changes)}")
        print("game_offsets.py saved. Re-run bot to apply changes.")

    return 0 if result.confidence > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
