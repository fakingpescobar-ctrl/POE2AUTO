"""Интеграция чтения памяти PoE2 с системой авто-атаки POE2AUTO.

Заменяет CV-детект полосы HP моба на прямое чтение памяти:
- Открывает процесс PoE2 (read-only)
- Читает список сущностей из AreaInstance
- Находит враждебных мобов в радиусе атаки
- Предоставляет тот же интерфейс что и MobBarDetector

Адаптировано из POE2GPS (Sikaka/POE2Radar, MIT).
"""
import logging
import time
from typing import Optional, List

from game_memory import GameMemory
from entity_reader import EntityReader, EntityCategory, Rarity, EntityData
import game_offsets as off

_log = logging.getLogger("poe2auto.memdetect")


class MemoryMobDetector:
    """Детект мобов через чтение памяти игры.

    Альтернатива MobBarDetector (CV). Работает через PROCESS_VM_READ:
    - Не зависит от разрешения/UI Scale
    - Не зависит от цвета фона/освещения локации
    - Не ловит ложные срабатывания от декора/лавы
    - Работает даже когда полоса HP не видна на экране
    """

    def __init__(self, cfg: dict = None):
        cfg = cfg or {}
        self.mem = GameMemory()
        self.reader: Optional[EntityReader] = None

        # Параметры
        self.attack_range = cfg.get("attack_range", 15.0)  # сетевые единицы
        self.attack_range_sq = self.attack_range ** 2

        # Состояние
        self._connected = False
        self._area_instance = 0
        self._game_state = 0
        self._player_pos = (0.0, 0.0)
        self._area_code = ""
        self._last_entities: List[EntityData] = []
        self._last_update = 0.0
        self._update_interval = 0.05  # 50ms = 20Hz
        self._resolve_failures = 0
        self._max_resolve_failures = 3  # после 3 неудач — откат на CV
        self._last_resolve = 0.0
        self._hideout_cache = None  # (area_instance, result)
        self._hideout_cache_ts = 0.0

        # Статистика
        self.stats = {
            "total_entities": 0,
            "hostile_in_range": 0,
            "friendlies": 0,
            "chests": 0,
        }

    def connect(self) -> bool:
        """Подключиться к процессу PoE2."""
        if self._connected and self.mem.connected:
            return True

        if not self.mem.connect():
            _log.warning("Could not connect to PoE2 process")
            return False

        self.reader = EntityReader(self.mem)
        self._connected = True
        self._resolve_game_state()

        # Инициализация discovery системы
        if self._area_instance:
            player = self.mem.read_pointer(self._area_instance + off.AreaInstance.LOCAL_PLAYER)
            if player:
                comps = self.reader._resolve_components(player)
                life_addr = comps.get("Life")
                if life_addr:
                    self.reader.initialize_discovery(life_addr)
                    _log.info("Offset discovery initialized (Life=0x%X)", life_addr)

        return True

    @property
    def available(self) -> bool:
        """True если memory detection работает (найшёл AreaInstance)."""
        return self._connected and self._area_instance != 0

    def disconnect(self):
        """Отключиться от процесса."""
        self.mem.disconnect()
        self._connected = False
        self._area_instance = 0
        self._game_state = 0

    def update(self) -> bool:
        """Обновить данные сущностей. Вызывать каждый кадр.
        Возвращает True если данные обновлены.
        """
        now = time.perf_counter()
        if now - self._last_update < self._update_interval:
            return False
        self._last_update = now

        # Переподключение при потере
        if not self.mem.connected:
            self.connect()
            if not self.mem.connected:
                return False

        # Раз в 2 секунды — пересчитываем GameState → AreaInstance
        if now - getattr(self, '_last_resolve', 0) > 2.0:
            self._resolve_game_state()
            self._last_resolve = now

        if not self._area_instance:
            return False

        # Runtime валидация оффсетов (каждые 5 секунд)
        if self.reader:
            player_ptr = self.mem.read_pointer(self._area_instance + off.AreaInstance.LOCAL_PLAYER)
            if player_ptr:
                comps = self.reader._resolve_components(player_ptr)
                life_addr = comps.get("Life")
                if life_addr:
                    self.reader.validate_offsets(life_addr)

        # Читаем позицию игрока
        player = self.reader.read_local_player(self._area_instance)
        if player:
            self._player_pos = (player.grid_x, player.grid_y)

        # Читаем сущности
        entities = self.reader.read_entities(self._area_instance, self._player_pos)
        self._last_entities = entities

        # Статистика
        hostile = [e for e in entities
                   if e.category == EntityCategory.MONSTER
                   and not e.is_friendly
                   and e.hp_current > 0]
        in_range = [e for e in hostile if e.distance <= self.attack_range]
        self.stats["total_entities"] = len(entities)
        self.stats["hostile_in_range"] = len(in_range)
        self.stats["friendlies"] = sum(1 for e in entities if e.is_friendly)
        self.stats["chests"] = sum(1 for e in entities if e.category == EntityCategory.CHEST)

        return True

    def present(self, frame_bgr=None) -> bool:
        """Совместимый с MobBarDetector интерфейс.
        Возвращает True если есть враждебный моб в радиусе атаки.
        frame_bgr игнорируется (память точнее CV).
        """
        if not self._connected:
            self.connect()
        if not self._connected:
            return False

        self.update()

        # Есть враждебные мобы в радиусе?
        return self.stats["hostile_in_range"] > 0

    def in_hideout(self) -> bool:
        """Определить, находится ли игрок в убежище.
        Читает area code из памяти — точнее чем template matching.
        Кеширует результат на 2с чтобы не дёргать память каждый кадр.
        """
        if not self._area_instance:
            return False

        now = time.perf_counter()
        if (self._hideout_cache is not None
                and self._hideout_cache[0] == self._area_instance
                and now - self._hideout_cache_ts < 2.0):
            return self._hideout_cache[1]

        area_info = self.reader.read_area_info(self._area_instance)
        code = area_info.get("code", "")
        self._area_code = code

        result = False
        # Hideout area codes в PoE2
        hideout_codes = [
            "Hideout", "PlayerHideout", "GuildHideout",
            "DelveMine",  # подземелье — тоже безопасная зона
        ]
        for hc in hideout_codes:
            if hc.lower() in code.lower():
                result = True
                break

        if not result:
            # Town areas
            town_codes = ["_town", "Town"]
            for tc in town_codes:
                if tc.lower() in code.lower():
                    result = True
                    break

        self._hideout_cache = (self._area_instance, result)
        self._hideout_cache_ts = now
        return result

    def get_area_info(self) -> dict:
        """Информация о текущей зоне."""
        if not self._area_instance:
            return {}
        return self.reader.read_area_info(self._area_instance)

    def read_player_vitals(self) -> tuple:
        """Прочитать HP% и Mana% игрока из памяти.
        Возвращает (hp_pct, mana_pct) или (None, None)."""
        if not self._area_instance or not self.reader:
            return None, None
        player = self.reader.read_local_player(self._area_instance)
        if not player:
            return None, None

        import game_offsets as off
        import struct

        comps = self.reader._resolve_components(player)
        life_addr = comps.get(off.COMPONENT_LIFE)
        if not life_addr:
            return None, None

        # HP
        hp_pct = None
        data = self.reader._mem.read_bytes(life_addr + off.Life.HEALTH, off.VitalStruct.SIZE)
        if data:
            hp_max = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX+4])[0]
            hp_cur = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT+4])[0]
            if hp_max > 0:
                hp_pct = max(0.0, min(1.0, hp_cur / hp_max))

        # Mana
        mana_pct = None
        data = self.reader._mem.read_bytes(life_addr + off.Life.MANA, off.VitalStruct.SIZE)
        if data:
            mp_max = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX+4])[0]
            mp_cur = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT+4])[0]
            if mp_max > 0:
                mana_pct = max(0.0, min(1.0, mp_cur / mp_max))

        return hp_pct, mana_pct

    def get_nearest_hostile(self) -> Optional[EntityData]:
        """Ближайший враждебный моб в радиусе атаки."""
        hostiles = [e for e in self._last_entities
                    if e.category == EntityCategory.MONSTER
                    and not e.is_friendly
                    and e.hp_current > 0
                    and e.distance <= self.attack_range]
        if not hostiles:
            return None
        return min(hostiles, key=lambda e: e.distance)

    def get_all_hostiles(self) -> List[EntityData]:
        """Все враждебные мобы."""
        return [e for e in self._last_entities
                if e.category == EntityCategory.MONSTER
                and not e.is_friendly
                and e.hp_current > 0]

    def get_hostiles_in_range(self) -> List[EntityData]:
        """Враждебные мобы в радиусе атаки."""
        return [e for e in self._last_entities
                if e.category == EntityCategory.MONSTER
                and not e.is_friendly
                and e.hp_current > 0
                and e.distance <= self.attack_range]

    def get_best_target(self) -> Optional[EntityData]:
        """Лучшая цель для атаки. Приоритет:
        1. Боссы (is_boss) — ближайший
        2. Unique — ближайший
        3. Rare — ближайший
        4. Magic — ближайший
        5. Normal — ближайший
        В пределах одной редкости — по расстоянию (ближе = лучше)."""
        hostiles = self.get_hostiles_in_range()
        if not hostiles:
            return None

        # Разделяем по приоритету: босс > unique > rare > magic > normal
        def target_score(e: EntityData) -> tuple:
            rarity_order = {
                Rarity.UNIQUE: 0,
                Rarity.RARE: 1,
                Rarity.MAGIC: 2,
                Rarity.NORMAL: 3,
            }
            is_boss_penalty = 0 if e.is_boss else 1
            rarity_penalty = rarity_order.get(e.rarity, 4)
            return (is_boss_penalty, rarity_penalty, e.distance)

        return min(hostiles, key=target_score)

    def get_lowest_hp_target(self) -> Optional[EntityData]:
        """Цель с минимальным HP% в радиусе атаки (для добивания)."""
        hostiles = self.get_hostiles_in_range()
        if not hostiles:
            return None
        return min(hostiles, key=lambda e: e.hp_ratio if e.hp_max > 0 else 1.0)

    def get_chests(self) -> List[EntityData]:
        """Сундуки."""
        return [e for e in self._last_entities
                if e.category == EntityCategory.CHEST]

    def get_transitions(self, dedup_radius: float = 0.5) -> List[EntityData]:
        """Уникальные переходы (portals, doorways) с дедупликацией по позиции.
        Группирует переходы в пределах dedup_radius сетевых единиц,
        оставляя самый ближний к игроку в каждой группе.
        """
        transitions = [e for e in self._last_entities
                       if e.category == EntityCategory.TRANSITION]
        if not transitions:
            return []

        # Сортируем по расстоянию (ближайшие первые)
        transitions.sort(key=lambda e: e.distance)

        unique = []
        for t in transitions:
            # Проверяем, не попадает ли в существующую группу
            is_dup = False
            for i, u in enumerate(unique):
                dx = t.grid_x - u.grid_x
                dy = t.grid_y - u.grid_y
                if dx * dx + dy * dy <= dedup_radius * dedup_radius:
                    # Дубликат — уже есть переход в этой зоне
                    is_dup = True
                    break
            if not is_dup:
                unique.append(t)

        return unique

    # --- Internal ---

    def _resolve_game_state(self):
        """Найти GameState → AreaInstance через AOB scan."""
        if not self.mem.connected:
            return

        # AOB scan для GameStates
        pattern_info = off.AOB_PATTERNS.get("game_states")
        if not pattern_info:
            _log.error("No AOB pattern for game_states")
            return

        pattern, wildcards, disp_off, instr_len, desc = pattern_info

        try:
            matches = self.mem.aob_scan(pattern, wildcards)
        except Exception as e:
            _log.error("AOB scan error: %s", e)
            self._resolve_failures += 1
            return

        if not matches:
            self._resolve_failures += 1
            if self._resolve_failures >= self._max_resolve_failures:
                _log.warning("AOB scan failed %d times — memory detection unavailable "
                             "(offsets may need updating for this PoE2 version)",
                             self._resolve_failures)
            return

        # Try each AOB match and validate
        old_area = self._area_instance
        self._area_instance = 0
        for match_addr in matches:
            # Resolve RIP-relative displacement
            next_instr = match_addr + instr_len
            disp_data = self.mem.read_bytes(match_addr + disp_off, 4)
            if disp_data is None:
                continue

            import struct
            displacement = struct.unpack("<i", disp_data)[0]
            slot_addr = next_instr + displacement

            # Dereference slot → GameState
            game_state = self.mem.read_pointer(slot_addr)
            if not game_state or game_state < 0x10000:
                continue

            # GameState → InGameState → AreaInstance → Player
            area_inst = self.reader.find_area_instance(game_state)
            if not area_inst or area_inst < 0x10000:
                continue

            # Validate: AreaInstance must have a valid player entity
            player = self.mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
            if not player or player < 0x10000:
                continue

            # Validate: player metadata must start with "Metadata/" or "data/"
            meta = self.reader._get_metadata(player)
            if not meta or not ("Metadata/" in meta or "data/" in meta):
                continue

            # Found valid chain!
            self._game_state = game_state
            self._resolve_failures = 0

            if area_inst != old_area:
                _log.info("AreaInstance changed: 0x%X → 0x%X", self._area_instance, area_inst)
                self._area_instance = area_inst
                self.reader.clear_cache()

                # Re-scan offsets для новой зоны
                player = self.mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
                if player:
                    comps = self.reader._resolve_components(player)
                    life_addr = comps.get("Life")
                    if life_addr:
                        self.reader.force_rescan(life_addr)
                        _log.info("Offset re-scan triggered for new zone (Life=0x%X)", life_addr)

                # Read area info
                info = self.reader.read_area_info(area_inst)
                _log.info("Zone: %s level=%d hash=%d",
                          info.get("code", "?"), info.get("level", 0), info.get("hash", 0))
            return

        _log.warning("No valid GameState chain found in AOB matches")
        self._resolve_failures += 1
