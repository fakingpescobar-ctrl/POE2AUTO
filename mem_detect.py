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
        self._player_hp = None
        self._area_code = ""
        self._last_entities: List[EntityData] = []
        self._last_update = 0.0
        self._update_interval = 0.05  # 50ms = 20Hz
        self._resolve_failures = 0
        self._max_resolve_failures = 3  # после 3 неудач — откат на CV
        self._last_resolve = 0.0
        self._last_heal_attempt = 0.0  # guard для self-heal попыток (раз в 5с)
        self._igs_ptr = 0  # кеш AOB-цепочки
        self._hideout_cache = None  # (area_instance, result)
        self._hideout_cache_ts = 0.0

        # Совместимость с MobBarDetector (allows `if det.last_match`)
        self.last_match = None

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

        # Переподключение при потере процесса.
        # ВАЖНО: OpenProcess хендл НЕ закрывается при смерти PoE2 — mem.connected
        # остаётся True, а read'ы тихо фейлятся (None). Поэтому кроме connected
        # проверяем живость процесса через check_alive(), иначе reconnect
        # после перезапуска игры никогда не произойдёт.
        if not self.mem.connected or not self.mem.check_alive():
            if self.mem.connected:
                _log.warning("PoE2 процесс мёртв — переподключение...")
                self.disconnect()
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

        # Читаем позицию и HP игрока
        player = self.reader.read_local_player(self._area_instance)
        if player:
            self._player_pos = (player.grid_x, player.grid_y)
            self._player_hp = (player.hp_current, player.hp_max)

        # Читаем сущности
        entities = self.reader.read_entities(self._area_instance, self._player_pos)
        self._last_entities = entities

        # Статистика
        monsters_alive = [e for e in entities
                          if e.category == EntityCategory.MONSTER
                          and not e.is_friendly
                          and e.hp_current > 0]

        # Sanity: проверка на сломанный Vital offset.
        # Одинаковый HP у ОДНОТИПНЫХ мобов — НОРМА (пачка зомби). Подозрительно:
        # (а) невалидные значения (hp_cur > hp_max, hp_max <= 0, hp_max > 10M),
        # (б) одинаковый HP у мобов РАЗНЫХ типов, покрывающий >80% всех живых.
        hp_counts = {}
        for e in monsters_alive:
            key = (e.hp_current, e.hp_max)
            hp_counts[key] = hp_counts.get(key, 0) + 1
        most_common_hp = max(hp_counts, key=hp_counts.get) if hp_counts else None
        most_common_count = hp_counts.get(most_common_hp, 0) if most_common_hp else 0

        invalid_values = any(e.hp_max <= 0 or e.hp_current > e.hp_max
                             or e.hp_max > 10_000_000 for e in monsters_alive)
        hp_broken = invalid_values
        if len(monsters_alive) > 3 and most_common_count > len(monsters_alive) * 0.8:
            # Сколько разных типов мобов в самой частой группе HP
            group_types = set()
            for e in monsters_alive:
                if (e.hp_current, e.hp_max) == most_common_hp:
                    group_types.add(e.metadata.split("/")[-1] if e.metadata else "?")
            hp_broken = hp_broken or len(group_types) > 1

        # Single-type спавн (одни зомби): тип не различает. Сильный признак
        # сломанного оффсета — hp_max мобов == hp_max ИГРОКА (все читают одну
        # структуру). У живых мобов пулы почти всегда отличаются от игрока.
        if (not hp_broken and most_common_hp and self._player_hp
                and isinstance(self._player_hp[1], (int, float))
                and self._player_hp[1] > 0
                and most_common_count >= 5
                and most_common_hp[1] == self._player_hp[1]):
            hp_broken = True
            _log.debug("hp_broken: %d mobs share player hp_max=%d (offset points to player vitals)",
                       most_common_count, self._player_hp[1])

        if hp_broken:
            # Сначала пробуем self-heal (пере-скан оффсетов) — отключаем только
            # если после этого HP всё ещё подозрителен.
            now_h = time.monotonic()
            if self.reader and now_h - self._last_heal_attempt > 5.0:
                self._last_heal_attempt = now_h
                player_ptr = self.mem.read_pointer(
                    self._area_instance + off.AreaInstance.LOCAL_PLAYER)
                if player_ptr:
                    comps = self.reader._resolve_components(player_ptr)
                    life_addr = comps.get("Life")
                    if life_addr:
                        self.reader.validate_offsets(life_addr)
                        self.reader._vital_offsets_resolved = False
                        self.reader._ensure_vital_offsets(life_addr)
            hostile = []
            _log.warning("HP offset похоже сломан: %d/%d монстров имеют hp=%s — отключаю memory детект",
                         most_common_count, len(monsters_alive), most_common_hp)
        else:
            hostile = monsters_alive
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

        Возвращает (hp_pct, mana_pct) или (None, None).

        hp_pct = ТОЛЬКО доступный Life% = cur / (max − reserved): reserved —
        HP, зарезервированный аурами (Low-Life билды: max=1466, reserved≈958 →
        доступно ~508). По max считать нельзя — полный доступный HP выглядел
        бы как «низкое здоровье».

        ES/Ward НЕ входят: фляги их НЕ восстанавливают (Ward самовосстанавливается
        по таймеру, урон сперва съедает ES/Ward, потом Life). Для Low-Life
        (Life=1/1, защита = Ward) hp_pct всегда 100% → фляги молчат.
        Аналогично ReaderMobDetector.read_player_vitals."""
        if not self._area_instance or not self.reader:
            return None, None

        import game_offsets as off

        player_ptr = self.reader._mem.read_pointer(
            self._area_instance + off.AreaInstance.LOCAL_PLAYER
        )
        if not player_ptr:
            return None, None
        comps = self.reader._resolve_components(player_ptr)
        life_addr = comps.get(off.COMPONENT_LIFE)
        if not life_addr:
            return None, None

        # HP — self-healing offset из discovery; unreserved = max − reserved
        health_off = self.reader._health_off if hasattr(self.reader, '_health_off') else off.Life.HEALTH
        hp_cur, hp_avail = self.reader._read_vital_unreserved(life_addr + health_off)
        hp_pct = None
        if hp_avail > 0:
            hp_pct = max(0.0, min(1.0, hp_cur / hp_avail))

        # Mana — self-healing offset из discovery; unreserved = max − reserved
        mana_off = self.reader._mana_off if hasattr(self.reader, '_mana_off') else off.Life.MANA
        mp_cur, mp_avail = self.reader._read_vital_unreserved(life_addr + mana_off)
        mana_pct = None
        if mp_avail > 0:
            mana_pct = max(0.0, min(1.0, mp_cur / mp_avail))

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
        """Найти AreaInstance. Кеширует AOB-цепочку — сканирует один раз."""
        if not self.mem.connected:
            return

        import struct

        # Если кеш есть — резолвим AreaInstance без AOB
        if self._igs_ptr:
            igs = self._igs_ptr
            area_inst = self.mem.read_pointer(igs + off.InGameState.AREA_INSTANCE)
            if area_inst and area_inst > 0x10000:
                vtable = self.mem.read_pointer(area_inst)
                if vtable and self.mem._base <= vtable <= self.mem._base + self.mem._size:
                    player = self.mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
                    if player and player > 0x10000:
                        if area_inst != self._area_instance:
                            _log.info("AreaInstance changed: 0x%X → 0x%X",
                                      self._area_instance, area_inst)
                            self._area_instance = area_inst
                            self.reader.clear_cache()
                            self._on_zone_change(area_inst)
                        self._resolve_failures = 0
                        return

            # Кеш протух — сброс
            _log.info("Cached chain stale, re-scanning AOB...")
            self._igs_ptr = 0

        # AOB scan (один раз)
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
                _log.warning("AOB scan failed %d times — memory detection unavailable",
                             self._resolve_failures)
            return

        old_area = self._area_instance
        for match_addr in matches:
            next_instr = match_addr + instr_len
            disp_data = self.mem.read_bytes(match_addr + disp_off, 4)
            if disp_data is None:
                continue
            displacement = struct.unpack("<i", disp_data)[0]
            slot_addr = next_instr + displacement
            game_state = self.mem.read_pointer(slot_addr)
            if not game_state or game_state < 0x10000:
                continue
            # InGameState: States[]-перебор (пост-патч 2026-09-04).
            # vec-deref может дать ненулевой МУСОР — каждый кандидат валидируется.
            _cands = []
            _v = self.mem.read_pointer(game_state + 0x08)
            if _v:
                _cands.append(self.mem.read_pointer(_v))
            for _i in range(off.GameState.STATES_COUNT):
                _cands.append(self.mem.read_pointer(
                    game_state + off.GameState.STATES_ARRAY + _i * off.GameState.STATES_STRIDE))
            igs = 0
            area_inst = 0
            for _c in _cands:
                if not _c or _c < 0x10000:
                    continue
                _ai = self.mem.read_pointer(_c + off.InGameState.AREA_INSTANCE)
                if not _ai or _ai < 0x10000:
                    continue
                _vt = self.mem.read_pointer(_ai)
                if not _vt or not (self.mem._base <= _vt <= self.mem._base + self.mem._size):
                    continue
                _pl = self.mem.read_pointer(_ai + off.AreaInstance.LOCAL_PLAYER)
                if not _pl or _pl < 0x10000:
                    continue
                # Метадата игрока должна быть "Metadata/" — иначе мусорный кандидат
                # (States[0] в некоторых зонах даёт area с валидным vtable, но пустой path)
                _d = self.mem.read_pointer(_pl + off.Entity.DETAILS_PTR)
                _sp = self.mem.read_pointer(_d + off.EntityDetails.NAME) if _d else 0
                _buf = self.mem.read_bytes(_sp, 256) if _sp and _sp > 0x100000000 else None
                _meta = ""
                if _buf:
                    _chars = []
                    for _j in range(0, min(len(_buf), 512) - 1, 2):
                        _cc = (_buf[_j] | (_buf[_j + 1] << 8)) & 0xFFFF
                        if _cc == 0:
                            break
                        if 0x20 <= _cc <= 0x7E:
                            _chars.append(chr(_cc))
                        else:
                            break
                    _meta = "".join(_chars)
                if not _meta.startswith("Metadata/"):
                    continue
                igs = _c
                area_inst = _ai
                break
            if not igs or not area_inst:
                continue
            vtable = self.mem.read_pointer(area_inst)
            if not vtable or not (self.mem._base <= vtable <= self.mem._base + self.mem._size):
                continue
            player = self.mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
            if not player or player < 0x10000:
                continue
            player_vtable = self.mem.read_pointer(player)
            if not player_vtable or not (self.mem._base <= player_vtable <= self.mem._base + self.mem._size):
                continue

            # Валидная цепочка — кешируем
            self._igs_ptr = igs
            self._game_state = game_state
            self._resolve_failures = 0

            if area_inst != old_area:
                _log.info("AreaInstance changed: 0x%X → 0x%X", self._area_instance, area_inst)
                self._area_instance = area_inst
                self.reader.clear_cache()
                self._on_zone_change(area_inst)
            return

        _log.warning("No valid GameState chain found in AOB matches (%d matches tried)",
                      len(matches))
        self._resolve_failures += 1

        # Self-healing
        if self._resolve_failures <= self._max_resolve_failures:
            healed = self._heal_offsets(self._game_state, self._area_instance)
            if healed:
                _log.info("Self-heal: AreaInstance found via heuristic")
                return

        if self._resolve_failures >= self._max_resolve_failures:
            _log.warning("AOB scan failed %d times — memory detection unavailable",
                         self._resolve_failures)

    def _on_zone_change(self, area_inst: int):
        """Re-scan offsets при смене зоны."""
        if not self.reader:
            return
        player = self.mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
        if player:
            comps = self.reader._resolve_components(player)
            life_addr = comps.get("Life")
            if life_addr:
                self.reader.force_rescan(life_addr)
                _log.info("Offset re-scan triggered for new zone (Life=0x%X)", life_addr)
        info = self.reader.read_area_info(area_inst)
        _log.info("Zone: %s level=%d hash=%d",
                  info.get("code", "?"), info.get("level", 0), info.get("hash", 0))

    def _heal_offsets(self, game_state: int, old_area: int) -> bool:
        """Self-healing: brute-force key offsets around defaults when static chain fails.

        Tries three tiers:
          1. Wide brute-force InGameState.AREA_INSTANCE ±256 + AreaInstance.LOCAL_PLAYER ±256
          2. OffsetScanner (full discovery engine)
          3. Area brute-force with area code validation
        """
        _log.info("Self-healing AOB chain: trying tier-1 brute-force...")

        # Tier 1: wide brute-force
        for area_off_shift in range(-256, 257, 8):
            area_off = off.InGameState.AREA_INSTANCE + area_off_shift
            if area_off < 0 or area_off > 0xFFFF:
                continue
            in_game = self.mem.read_pointer(game_state + 0x08)
            if not isinstance(in_game, int) or not in_game:
                continue
            area_inst = self.mem.read_pointer(in_game + area_off)
            if not isinstance(area_inst, int) or not area_inst or area_inst < 0x10000:
                continue

            # Validate area code at +0x0A0 (faster than checking every player offset)
            code_ptr = self.mem.read_pointer(area_inst + off.AreaInstance.AREA_INFO)
            if not code_ptr:
                continue
            code = self.mem.read_std_wstring(code_ptr)
            if not code or len(code) < 2 or not code[0].isalpha():
                continue

            for lp_off_shift in range(-256, 257, 8):
                lp_off = off.AreaInstance.LOCAL_PLAYER + lp_off_shift
                if lp_off < 0 or lp_off > 0xFFFF:
                    continue
                player = self.mem.read_pointer(area_inst + lp_off)
                if not isinstance(player, int) or not player or player < 0x10000:
                    continue

                meta = self.reader._get_metadata(player)
                if meta and ("Metadata/" in meta or "data/" in meta):
                    _log.info(
                        "Tier-1 SUCCESS: InGameState.AREA_INSTANCE=0x%X (shift=+%d), "
                        "AreaInstance.LOCAL_PLAYER=0x%X (shift=+%d) area=%s",
                        area_off, area_off_shift, lp_off, lp_off_shift, code
                    )
                    return self._apply_healed_offsets(
                        game_state, area_inst, area_off, lp_off, old_area, player
                    )

        # Tier 2: OffsetScanner full discovery
        _log.info("Tier-1 failed. Trying tier-2: OffsetScanner...")
        try:
            from offset_scanner import OffsetScanner
            scanner = OffsetScanner()
            result = scanner.scan()
            if result.confidence > 0 and result.area_instance:
                self._game_state = result.game_state
                self._area_instance = result.area_instance
                # Update runtime offsets
                if result.area_instance_offset:
                    off.InGameState.AREA_INSTANCE = result.area_instance_offset
                if result.local_player_offset:
                    off.AreaInstance.LOCAL_PLAYER = result.local_player_offset
                if result.awake_entities_offset:
                    off.AreaInstance.AWAKE_ENTITIES = result.awake_entities_offset
                if result.health_offset:
                    off.Life.HEALTH = result.health_offset
                if result.mana_offset:
                    off.Life.MANA = result.mana_offset
                if result.es_offset:
                    off.Life.ENERGY_SHIELD = result.es_offset

                self._resolve_failures = 0
                player = self.mem.read_pointer(
                    result.area_instance + off.AreaInstance.LOCAL_PLAYER
                )
                if player and result.area_instance != old_area:
                    self.reader.clear_cache()
                    comps = self.reader._resolve_components(player)
                    life_addr = comps.get("Life")
                    if life_addr:
                        self.reader.force_rescan(life_addr)
                _log.info("OffsetScanner healed all offsets")
                return True
        except Exception as e:
            _log.warning("OffsetScanner error: %s", e)

        _log.warning("Self-heal failed: no valid offset combination found")
        return False

    def _apply_healed_offsets(self, game_state, area_inst, area_off, lp_off,
                               old_area, player) -> bool:
        """Apply healed offsets and re-init."""
        old_ai = off.InGameState.AREA_INSTANCE
        old_lp = off.AreaInstance.LOCAL_PLAYER
        off.InGameState.AREA_INSTANCE = area_off
        off.AreaInstance.LOCAL_PLAYER = lp_off

        self._game_state = game_state
        self._area_instance = area_inst
        self._resolve_failures = 0
        if area_inst != old_area:
            self.reader.clear_cache()
            comps = self.reader._resolve_components(player)
            life_addr = comps.get("Life")
            if life_addr:
                self.reader.force_rescan(life_addr)

        _log.info("Offsets applied: AREA_INSTANCE 0x%X→0x%X, LOCAL_PLAYER 0x%X→0x%X",
                  old_ai, area_off, old_lp, lp_off)
        return True
