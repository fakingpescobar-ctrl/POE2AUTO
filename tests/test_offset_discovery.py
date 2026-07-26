"""Tests for offset_discovery.py — AOB, value-scan, runtime validation."""
import struct
from unittest.mock import MagicMock, patch, call
import pytest

from offset_discovery import OffsetDiscovery, VitalOffsets, ChainValidator
import game_offsets as off


def _make_vital_bytes(max_val: int, current: int, reserved_flat: int = 0, reserved_frac: int = 0) -> bytes:
    """Build a minimal VitalStruct-like byte buffer."""
    buf = bytearray(off.VitalStruct.SIZE)
    struct.pack_into("<i", buf, off.VitalStruct.RESERVED_FLAT, reserved_flat)
    struct.pack_into("<i", buf, off.VitalStruct.RESERVED_FRACTION, reserved_frac)
    struct.pack_into("<i", buf, off.VitalStruct.MAX, max_val)
    struct.pack_into("<i", buf, off.VitalStruct.CURRENT, current)
    return bytes(buf)


class TestVitalOffsets:
    def test_tuple_access(self):
        v = VitalOffsets(0x1B0, 0x208, 0x248)
        assert v.health == 0x1B0
        assert v.mana == 0x208
        assert v.es == 0x248


class TestOffsetDiscoveryInit:
    def test_defaults(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        assert d._health_off == off.Life.HEALTH
        assert d._mana_off == off.Life.MANA
        assert d._es_off == off.Life.ENERGY_SHIELD
        assert d._life_addr == 0
        assert d._consecutive_failures == 0

    def test_set_life_addr(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        d.set_life_addr(0x12345)
        assert d._life_addr == 0x12345

    def test_get_vital_offsets(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        v = d.get_vital_offsets()
        assert isinstance(v, VitalOffsets)
        assert v.health == off.Life.HEALTH


class TestValueScan:
    def test_finds_three_vitals(self):
        mem = MagicMock()
        life_addr = 0x1000

        # Build full memory region for scan range (0x80..0x400+SIZE)
        # Fill with clearly invalid data (Max=0), then plant valid VitalStructs
        # Space them far apart (> VitalStruct.SIZE=0x34) to avoid overlap
        region_size = 0x401 + off.VitalStruct.SIZE
        region = bytearray(region_size)
        # All Max fields are 0 (invalid) by default

        # Plant Health at +0x100 (Max=5000) — well separated from others
        struct.pack_into("<i", region, 0x100 + off.VitalStruct.MAX, 5000)
        struct.pack_into("<i", region, 0x100 + off.VitalStruct.CURRENT, 4500)
        # Plant Mana at +0x200 (Max=1000)
        struct.pack_into("<i", region, 0x200 + off.VitalStruct.MAX, 1000)
        struct.pack_into("<i", region, 0x200 + off.VitalStruct.CURRENT, 800)
        # Plant ES at +0x300 (Max=500)
        struct.pack_into("<i", region, 0x300 + off.VitalStruct.MAX, 500)
        struct.pack_into("<i", region, 0x300 + off.VitalStruct.CURRENT, 300)

        def read_bytes(addr, size):
            offset = addr - life_addr
            if 0 <= offset < len(region) and offset + size <= len(region):
                return bytes(region[offset:offset + size])
            return None

        mem.read_bytes = MagicMock(side_effect=read_bytes)

        d = OffsetDiscovery(mem)
        result = d._value_scan_vitals(life_addr)
        assert result is True

        vitals = d.get_vital_offsets()
        # Health is the largest (5000) → sorted first
        assert vitals.health == 0x100
        # Mana (1000) and ES (500) follow
        assert vitals.mana == 0x200
        assert vitals.es == 0x300

    def test_no_valid_structs(self):
        mem = MagicMock()
        # All reads return None
        mem.read_bytes = MagicMock(return_value=None)

        d = OffsetDiscovery(mem)
        result = d._value_scan_vitals(0x1000)
        assert result is False

    def test_filters_invalid_max(self):
        mem = MagicMock()
        life_addr = 0x1000

        # Only one valid struct at +0x1B0
        valid_struct = _make_vital_bytes(3000, 2500)

        def read_bytes(addr, size):
            offset = addr - life_addr
            if offset == 0x1B0:
                return valid_struct[:size]
            return None

        mem.read_bytes = MagicMock(side_effect=read_bytes)

        d = OffsetDiscovery(mem)
        result = d._value_scan_vitals(life_addr)
        assert result is True
        assert d._health_off == 0x1B0


class TestValidation:
    def test_valid_vital(self):
        mem = MagicMock()
        valid_data = _make_vital_bytes(5000, 4500)
        mem.read_bytes = MagicMock(return_value=valid_data)

        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid(0x1000) is True

    def test_invalid_max_zero(self):
        mem = MagicMock()
        invalid_data = _make_vital_bytes(0, 0)
        mem.read_bytes = MagicMock(return_value=invalid_data)

        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid(0x1000) is False

    def test_invalid_max_negative(self):
        mem = MagicMock()
        invalid_data = _make_vital_bytes(-100, 50)
        mem.read_bytes = MagicMock(return_value=invalid_data)

        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid(0x1000) is False

    def test_invalid_current_exceeds_max(self):
        mem = MagicMock()
        # current > max + 1
        invalid_data = _make_vital_bytes(100, 102)
        mem.read_bytes = MagicMock(return_value=invalid_data)

        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid(0x1000) is False

    def test_valid_with_reserved(self):
        mem = MagicMock()
        valid_data = _make_vital_bytes(5000, 4500, reserved_flat=200, reserved_frac=500)
        mem.read_bytes = MagicMock(return_value=valid_data)

        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid(0x1000) is True

    def test_invalid_reserved_exceeds_max(self):
        mem = MagicMock()
        invalid_data = _make_vital_bytes(100, 50, reserved_flat=150)
        mem.read_bytes = MagicMock(return_value=invalid_data)

        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid(0x1000) is False


class TestVitalLooksValidAt:
    def test_valid(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid_at(5000, 4500, 0) is True

    def test_zero_max(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid_at(0, 0, 0) is False

    def test_huge_max(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid_at(99_999_999, 50, 0) is False

    def test_negative_current(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid_at(100, -101, 0) is False

    def test_current_exceeds_max_plus_one(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid_at(100, 102, 0) is False

    def test_negative_reserved(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        assert d._vital_looks_valid_at(100, 50, -10) is False


class TestInitialize:
    def test_initialize_calls_aob_and_value_scan(self):
        mem = MagicMock()
        mem.aob_scan = MagicMock(return_value=[])
        mem.read_bytes = MagicMock(return_value=None)

        d = OffsetDiscovery(mem)
        result = d.initialize(0x1000)
        assert result is True
        mem.aob_scan.assert_called()


class TestValidate:
    def test_validate_skips_if_not_time(self):
        mem = MagicMock()
        d = OffsetDiscovery(mem)
        d._last_validate = 999999.0  # far future
        d._life_addr = 0x1000

        # Should skip validation
        result = d.validate(0x1000)
        assert result is True  # returns True when skipping

    def test_validate_checks_vital(self):
        mem = MagicMock()
        valid_data = _make_vital_bytes(5000, 4500)
        mem.read_bytes = MagicMock(return_value=valid_data)

        d = OffsetDiscovery(mem)
        d._life_addr = 0x1000
        d._last_validate = 0.0  # force validation

        result = d.validate(0x1000)
        assert result is True
        assert d.stats["validations_passed"] == 1

    def test_validate_triggers_rescan_on_failure(self):
        mem = MagicMock()
        # First call: invalid (triggers validation failure)
        # After rescan: valid
        call_count = [0]

        def read_bytes(addr, size):
            call_count[0] += 1
            if call_count[0] <= 1:
                # First read: invalid
                return _make_vital_bytes(0, 0)
            else:
                # Subsequent reads: valid
                return _make_vital_bytes(3000, 2500)

        mem.read_bytes = MagicMock(side_effect=read_bytes)
        mem.aob_scan = MagicMock(return_value=[])

        d = OffsetDiscovery(mem)
        d._life_addr = 0x1000
        d._last_validate = 0.0
        d._max_failures = 1  # trigger immediately

        result = d.validate(0x1000)
        # After rescan, should find valid offsets
        assert d.stats["validations_failed"] >= 1


class TestForceRescan:
    def test_force_rescan_resets_counters(self):
        mem = MagicMock()
        valid_data = _make_vital_bytes(5000, 4500)
        mem.read_bytes = MagicMock(return_value=valid_data)
        mem.aob_scan = MagicMock(return_value=[])

        d = OffsetDiscovery(mem)
        d._consecutive_failures = 5
        d._last_validate = 999999.0

        d.force_rescan(0x1000)
        assert d._consecutive_failures == 0
        assert d._last_validate == 0.0


class TestChainValidator:
    def test_valid_chain(self):
        mem = MagicMock()
        reader = MagicMock()
        reader._get_metadata = MagicMock(return_value="Metadata/Player")

        def fake_read_pointer(addr):
            if addr == 0x7FFE0000 + off.GameState.CURRENT_STATE_PTR:
                return 0x7FFE1000  # vec_ptr
            if addr == 0x7FFE1000:
                return 0x7FFE2000  # in_game
            if addr == 0x7FFE2000 + off.InGameState.AREA_INSTANCE:
                return 0x7FFE3000  # area_inst
            if addr == 0x7FFE3000 + off.AreaInstance.LOCAL_PLAYER:
                return 0x7FFE4000  # player
            return 0

        mem.read_pointer = MagicMock(side_effect=fake_read_pointer)

        cv = ChainValidator(mem, reader)
        cv.set_game_state(0x7FFE0000)
        cv._last_validate = 0.0

        is_valid, gs, ai = cv.validate()
        assert is_valid is True
        assert gs == 0x7FFE0000
        assert ai == 0x7FFE3000

    def test_invalid_chain_no_player(self):
        mem = MagicMock()
        reader = MagicMock()
        reader._get_metadata = MagicMock(return_value=None)

        def fake_read_pointer(addr):
            if addr == 0x7FFE0000 + off.GameState.CURRENT_STATE_PTR:
                return 0x7FFE1000
            if addr == 0x7FFE1000:
                return 0x7FFE2000
            if addr == 0x7FFE2000 + off.InGameState.AREA_INSTANCE:
                return 0x7FFE3000
            if addr == 0x7FFE3000 + off.AreaInstance.LOCAL_PLAYER:
                return 0x7FFE4000
            return 0

        mem.read_pointer = MagicMock(side_effect=fake_read_pointer)

        cv = ChainValidator(mem, reader)
        cv.set_game_state(0x7FFE0000)
        cv._last_validate = 0.0

        is_valid, gs, ai = cv.validate()
        assert is_valid is False

    def test_invalidate(self):
        mem = MagicMock()
        reader = MagicMock()
        cv = ChainValidator(mem, reader)
        cv._last_validate = 999999.0
        cv.invalidate()
        assert cv._last_validate == 0.0


class TestDiscoverAllOffsets:
    def test_discover_returns_dict(self):
        mem = MagicMock()
        reader = MagicMock()
        reader._resolve_components = MagicMock(return_value={"Life": 0x7FFE5000})
        reader._get_metadata = MagicMock(return_value="Metadata/Player")

        def fake_read_pointer(addr):
            if addr == 0x7FFE0000 + off.GameState.CURRENT_STATE_PTR:
                return 0x7FFE1000
            if addr == 0x7FFE1000:
                return 0x7FFE2000
            if addr == 0x7FFE2000 + off.InGameState.AREA_INSTANCE:
                return 0x7FFE3000
            if addr == 0x7FFE3000 + off.AreaInstance.LOCAL_PLAYER:
                return 0x7FFE4000
            return 0

        mem.read_pointer = MagicMock(side_effect=fake_read_pointer)

        def read_bytes(addr, size):
            return _make_vital_bytes(5000, 4500)

        mem.read_bytes = MagicMock(side_effect=read_bytes)

        d = OffsetDiscovery(mem, reader)
        result = d.discover_all_offsets(0x7FFE0000)

        assert "game_state" in result
        assert "area_instance" in result
        assert "local_player" in result
        assert "life_component" in result
