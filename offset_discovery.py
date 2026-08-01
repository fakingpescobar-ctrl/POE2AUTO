"""Автоматическое обнаружение и валидация оффсетов PoE2.

Три подхода:
1. АОБ-паттерны — сканирование .text секции для уникальных инструкций
2. Value-scan — поиск структур по известным значениям (HP/Mana/ES)
3. Runtime валидация — проверка оффсетов каждый тик, auto-recovery

Использование:
    discovery = OffsetDiscovery(mem, reader)
    discovery.initialize()      # AOB scan + value scan
    discovery.validate()        # runtime check + re-scan если нужно
    offsets = discovery.get_vital_offsets()  # текущие Health/Mana/ES
"""
import struct
import logging
import time
from typing import Optional, Dict, Tuple, NamedTuple

import game_offsets as off

_log = logging.getLogger("poe2auto.offsets")


class VitalOffsets(NamedTuple):
    health: int
    mana: int
    es: int


class OffsetDiscovery:
    """Автоматическое обнаружение оффсетов через AOB, value-scan и runtime validation."""

    # AOB паттерны для дополнительных корневых указателей
    # Формат: (pattern, wildcards, disp_offset, instr_len, description)
    EXTRA_AOB_PATTERNS = {
        # InGameState::GetCamera — lea rcx, [rip+disp32] (Camera ptr)
        # Встречается в функциях рендера, уникальный контекст рядом с Camera+0x1A0
        "camera_ptr": (
            b"\x48\x8B\x8F\x00\x00\x00\x00\x48\x8B\x05",
            {2, 3, 4, 5},
            3, 7,
            "Camera pointer from InGameState",
        ),
        # AreaInstance::GetLocalPlayer — mov rax, [rcx+LOCAL_PLAYER]
        # Уникальная последовательность с проверкой валидности
        "local_player": (
            b"\x48\x8B\x81\x00\x00\x00\x00\x48\x85\xC0\x74",
            {3, 4, 5, 6},
            3, 7,
            "LocalPlayer from AreaInstance",
        ),
    }

    # Диапазоны для value-scan VitalStruct
    VITAL_SCAN_MIN = 0x80
    VITAL_SCAN_MAX = 0x400
    VITAL_SCAN_STEP = 4

    # Валидные диапазоны значений
    MIN_VALID_HP = 1
    MAX_VALID_HP = 10_000_000
    MIN_VALID_MANA = 1
    MAX_VALID_MANA = 10_000_000

    def __init__(self, mem, reader=None):
        self._mem = mem
        self._reader = reader

        # Текущие оффсеты (начинаем с defaults)
        self._health_off = off.Life.HEALTH
        self._mana_off = off.Life.MANA
        self._es_off = off.Life.ENERGY_SHIELD

        # Кэш
        self._life_addr = 0
        self._last_validate = 0.0
        self._validate_interval = 5.0  # каждые 5 секунд
        self._consecutive_failures = 0
        self._max_failures = 3

        # Статистика
        self.stats = {
            "aob_matches": 0,
            "value_scan_matches": 0,
            "validations_passed": 0,
            "validations_failed": 0,
            "auto_recoveries": 0,
        }

    def initialize(self, life_addr: int = 0) -> bool:
        """Инициализация: AOB scan + value scan.
        life_addr — адрес Life компонента (если уже известен).
        """
        if life_addr:
            self._life_addr = life_addr

        # 1. AOB scan для дополнительных паттернов
        self._aob_scan_extra()

        # 2. Value scan для VitalStruct (если есть Life адрес)
        if self._life_addr:
            self._value_scan_vitals(self._life_addr)

        return True

    def validate(self, life_addr: int = 0) -> bool:
        """Runtime валидация: проверяет что текущие оффсеты валидны.
        Если нет — запускает re-scovery.
        Возвращает True если оффсеты валидны.
        """
        now = time.perf_counter()
        if now - self._last_validate < self._validate_interval:
            return True  # ещё не время проверять
        self._last_validate = now

        if life_addr:
            self._life_addr = life_addr

        if not self._life_addr:
            return False

        # Проверяем текущие оффсеты
        if self._vital_looks_valid(self._life_addr + self._health_off):
            self._consecutive_failures = 0
            self.stats["validations_passed"] += 1
            return True

        # Оффсеты невалидны — запускаем re-scovery
        self._consecutive_failures += 1
        self.stats["validations_failed"] += 1
        _log.warning("Vital offsets invalid at Life+0x%X (failure %d/%d) — re-scanning",
                     self._health_off, self._consecutive_failures, self._max_failures)

        if self._consecutive_failures >= self._max_failures:
            # Полный re-scan
            if self._value_scan_vitals(self._life_addr):
                self.stats["auto_recoveries"] += 1
                _log.info("Auto-recovered vital offsets: Health=0x%X, Mana=0x%X, ES=0x%X",
                          self._health_off, self._mana_off, self._es_off)
                self._consecutive_failures = 0
                return True
            else:
                _log.error("Re-scovery failed — using fallback offsets")
                return False

        return False

    def get_vital_offsets(self) -> VitalOffsets:
        """Текущие оффсеты VitalStruct."""
        return VitalOffsets(self._health_off, self._mana_off, self._es_off)

    def get_life_addr(self) -> int:
        """Текущий Life component address."""
        return self._life_addr

    def set_life_addr(self, addr: int):
        """Установить Life component address (вызывать при смене зоны)."""
        if addr != self._life_addr:
            self._life_addr = addr
            # При смене зоны — сбрасываем валидацию (новые оффсеты могут отличаться)
            self._last_validate = 0.0
            self._consecutive_failures = 0

    def force_rescan(self, life_addr: int = 0):
        """Принудительный re-scan (вызывать при смене зоны или F5)."""
        if life_addr:
            self._life_addr = life_addr
        self._last_validate = 0.0
        self._consecutive_failures = 0
        if self._life_addr:
            self._value_scan_vitals(self._life_addr)

    # --- AOB Scan ---

    def _aob_scan_extra(self):
        """AOB scan для дополнительных паттернов."""
        for name, (pattern, wildcards, disp_off, instr_len, desc) in self.EXTRA_AOB_PATTERNS.items():
            try:
                matches = self._mem.aob_scan(pattern, wildcards)
                if matches:
                    self.stats["aob_matches"] += len(matches)
                    _log.info("AOB '%s': %d matches (%s)", name, len(matches), desc)
            except Exception as e:
                _log.debug("AOB '%s' scan error: %s", name, e)

    # --- Value Scan ---

    def _value_scan_vitals(self, life_addr: int) -> bool:
        """Value-scan: ищем валидные VitalStruct в Life компоненте.
        Возвращает True если нашли хотя бы Health.
        """
        candidates = []

        for offset in range(self.VITAL_SCAN_MIN, self.VITAL_SCAN_MAX + 1, self.VITAL_SCAN_STEP):
            data = self._mem.read_bytes(life_addr + offset, off.VitalStruct.SIZE)
            if not data:
                continue

            max_val = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX + 4])[0]
            current = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT + 4])[0]
            reserved_flat = struct.unpack("<i", data[off.VitalStruct.RESERVED_FLAT:off.VitalStruct.RESERVED_FLAT + 4])[0]
            reserved_frac = struct.unpack("<i", data[off.VitalStruct.RESERVED_FRACTION:off.VitalStruct.RESERVED_FRACTION + 4])[0]

            # Фильтр валидности
            if not self._vital_looks_valid_at(max_val, current, reserved_flat):
                continue

            # Проверяем что это не дубликат (相邻 структуры с тем же Max)
            # и не перекрытие (Max одного == Current другого или наоборот)
            is_dup = False
            for prev_off, prev_max, prev_cur in candidates:
                if abs(offset - prev_off) < off.VitalStruct.SIZE:
                    if max_val == prev_max:
                        is_dup = True
                        break
                    # Overlap detection: if this struct's max matches prev's current
                    # or this struct's current matches prev's max, they overlap
                    if max_val == prev_cur or current == prev_max:
                        is_dup = True
                        break
            if is_dup:
                continue

            candidates.append((offset, max_val, current))
            self.stats["value_scan_matches"] += 1

            if len(candidates) >= 3:
                break

        if not candidates:
            _log.warning("No valid VitalStruct found in Life+0x%X range 0x%X-0x%X",
                         life_addr, self.VITAL_SCAN_MIN, self.VITAL_SCAN_MAX)
            return False

        # Health — используем default (off.Life.HEALTH), не переопределяем value-scan'ом
        # value-scan может найти неверный VitalStruct для Ward/LowLife билдов
        rest = [c for c in candidates if c[0] != off.Life.HEALTH]
        rest.sort(key=lambda x: x[1], reverse=True)

        if len(rest) >= 1:
            self._mana_off = rest[0][0]
        if len(rest) >= 2:
            self._es_off = rest[1][0]

        _log.info("Value-scan found %d VitalStructs: Health=0x%X (default), Mana=0x%X (%d), ES=0x%X (%d)",
                  len(candidates),
                  off.Life.HEALTH,
                  self._mana_off, rest[0][1] if len(rest) > 0 else 0,
                  self._es_off, rest[1][1] if len(rest) > 1 else 0)

        return True

    # --- Validation ---

    def _vital_looks_valid(self, address: int) -> bool:
        """Проверить что VitalStruct по адресу выглядит валидной."""
        data = self._mem.read_bytes(address, off.VitalStruct.SIZE)
        if not data:
            return False
        max_val = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX + 4])[0]
        current = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT + 4])[0]
        reserved_flat = struct.unpack("<i", data[off.VitalStruct.RESERVED_FLAT:off.VitalStruct.RESERVED_FLAT + 4])[0]
        return self._vital_looks_valid_at(max_val, current, reserved_flat)

    def _vital_looks_valid_at(self, max_val: int, current: int, reserved_flat: int) -> bool:
        """Проверить что значения VitalStruct выглядят валидными."""
        if max_val <= 0 or max_val > self.MAX_VALID_HP:
            return False
        if current < -max_val or current > max_val + 1:
            return False
        if reserved_flat < 0 or reserved_flat > max_val:
            return False
        return True

    def validate_chain(self, game_state: int) -> bool:
        """Валидация цепочки GameState → InGameState → AreaInstance → Player."""
        if not self._reader:
            return False

        # GameState → InGameState
        vec_ptr = self._mem.read_pointer(game_state + off.GameState.CURRENT_STATE_PTR)
        if not vec_ptr:
            return False

        in_game = self._mem.read_pointer(vec_ptr)
        if not in_game:
            return False

        # InGameState → AreaInstance
        area_inst = self._mem.read_pointer(in_game + off.InGameState.AREA_INSTANCE)
        if not area_inst or area_inst < 0x10000:
            return False

        # AreaInstance → LocalPlayer
        player = self._mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
        if not player or player < 0x10000:
            return False

        # Validate player metadata
        meta = self._reader._get_metadata(player)
        if not meta or not ("Metadata/" in meta or "data/" in meta):
            return False

        return True

    def discover_all_offsets(self, game_state: int) -> Dict[str, int]:
        """Полное обнаружение всех оффсетов.
        Возвращает словарь {name: offset_value}.
        """
        result = {}

        # 1. Валидация цепочки
        if not self.validate_chain(game_state):
            _log.warning("Chain validation failed for GameState 0x%X", game_state)
            return result

        # 2. AreaInstance
        vec_ptr = self._mem.read_pointer(game_state + off.GameState.CURRENT_STATE_PTR)
        in_game = self._mem.read_pointer(vec_ptr)
        area_inst = self._mem.read_pointer(in_game + off.InGameState.AREA_INSTANCE)
        player = self._mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)

        result["game_state"] = game_state
        result["in_game_state"] = in_game
        result["area_instance"] = area_inst
        result["local_player"] = player

        # 3. Life component
        if self._reader:
            comps = self._reader._resolve_components(player)
            life_addr = comps.get("Life")
            if life_addr:
                result["life_component"] = life_addr
                self._life_addr = life_addr

                # 4. Vital offsets
                self._value_scan_vitals(life_addr)
                vitals = self.get_vital_offsets()
                result["health_offset"] = vitals.health
                result["mana_offset"] = vitals.mana
                result["es_offset"] = vitals.es

        return result


class ChainValidator:
    """Валидация цепочки GameState → Player с auto-recovery."""

    def __init__(self, mem, reader):
        self._mem = mem
        self._reader = reader
        self._game_state = 0
        self._area_instance = 0
        self._last_validate = 0.0
        self._validate_interval = 2.0

    def validate(self) -> Tuple[bool, int, int]:
        """Валидация цепочки. Возвращает (is_valid, game_state, area_instance)."""
        now = time.perf_counter()
        if now - self._last_validate < self._validate_interval:
            return (self._game_state != 0, self._game_state, self._area_instance)
        self._last_validate = now

        if not self._game_state:
            return (False, 0, 0)

        # Проверяем что AreaInstance всё ещё валиден
        vec_ptr = self._mem.read_pointer(self._game_state + off.GameState.CURRENT_STATE_PTR)
        if not vec_ptr:
            return (False, 0, 0)

        in_game = self._mem.read_pointer(vec_ptr)
        if not in_game:
            return (False, 0, 0)

        area_inst = self._mem.read_pointer(in_game + off.InGameState.AREA_INSTANCE)
        if not area_inst or area_inst < 0x10000:
            return (False, 0, 0)

        # Проверяем что Player всё ещё валиден
        player = self._mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
        if not player or player < 0x10000:
            return (False, 0, 0)

        meta = self._reader._get_metadata(player)
        if not meta or not ("Metadata/" in meta or "data/" in meta):
            return (False, 0, 0)

        self._area_instance = area_inst
        return (True, self._game_state, area_inst)

    def set_game_state(self, game_state: int):
        """Установить GameState (при первом AOB find)."""
        self._game_state = game_state

    def invalidate(self):
        """Сбросить кэш (при смене зоны)."""
        self._last_validate = 0.0
