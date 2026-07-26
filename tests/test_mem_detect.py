from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from entity_reader import EntityCategory, Rarity, EntityData
from mem_detect import MemoryMobDetector


def _hostile(id=1, x=5.0, y=5.0, dist=5.0, hp=100):
    return EntityData(
        id=id, category=EntityCategory.MONSTER, metadata="/Monsters/something",
        x=x, y=y, z=0.0, grid_x=x, grid_y=y,
        hp_current=hp, hp_max=100, hp_ratio=hp / 100,
        rarity=Rarity.NORMAL, is_friendly=False, distance=dist,
    )


def _friendly(id=100, dist=3.0):
    return EntityData(
        id=id, category=EntityCategory.PLAYER, metadata="/Characters/Player",
        x=0, y=0, z=0, grid_x=0, grid_y=0,
        hp_current=500, hp_max=500, hp_ratio=1.0,
        rarity=Rarity.NORMAL, is_friendly=True, distance=dist,
    )


def _chest(id=200, dist=8.0):
    return EntityData(
        id=id, category=EntityCategory.CHEST, metadata="/Chests/Something",
        x=0, y=0, z=0, grid_x=0, grid_y=0,
        hp_current=0, hp_max=0, hp_ratio=0.0,
        rarity=Rarity.NORMAL, is_friendly=False, distance=dist,
    )


def _dead_hostile(id=300, dist=2.0):
    return EntityData(
        id=id, category=EntityCategory.MONSTER, metadata="/Monsters/Dead",
        x=0, y=0, z=0, grid_x=0, grid_y=0,
        hp_current=0, hp_max=100, hp_ratio=0.0,
        rarity=Rarity.NORMAL, is_friendly=False, distance=dist,
    )


class TestInit:
    def test_defaults(self):
        det = MemoryMobDetector()
        assert det.attack_range == 15.0
        assert det.attack_range_sq == 225.0
        assert not det._connected
        assert det._area_instance == 0

    def test_custom_config(self):
        det = MemoryMobDetector({"attack_range": 20.0})
        assert det.attack_range == 20.0
        assert det.attack_range_sq == 400.0


class TestConnect:
    @patch("mem_detect.GameMemory")
    def test_connect_success(self, MockGM):
        mock_mem = MockGM.return_value
        mock_mem.connect.return_value = True
        det = MemoryMobDetector()
        det.mem = mock_mem
        result = det.connect()
        assert result is True
        assert det._connected is True

    @patch("mem_detect.GameMemory")
    def test_connect_failure(self, MockGM):
        mock_mem = MockGM.return_value
        mock_mem.connect.return_value = False
        det = MemoryMobDetector()
        det.mem = mock_mem
        result = det.connect()
        assert result is False
        assert det._connected is False

    @patch("mem_detect.GameMemory")
    def test_already_connected(self, MockGM):
        mock_mem = MockGM.return_value
        mock_mem.connected = True
        det = MemoryMobDetector()
        det.mem = mock_mem
        det._connected = True
        result = det.connect()
        assert result is True


class TestAvailable:
    def test_not_connected(self):
        det = MemoryMobDetector()
        assert det.available is False

    def test_connected_no_area(self):
        det = MemoryMobDetector()
        det._connected = True
        det._area_instance = 0
        assert det.available is False

    def test_connected_with_area(self):
        det = MemoryMobDetector()
        det._connected = True
        det._area_instance = 0x12345
        assert det.available is True


class TestDisconnect:
    def test_disconnect_clears_state(self):
        det = MemoryMobDetector()
        det._connected = True
        det._area_instance = 0x12345
        det._game_state = 0xABCDE
        det.disconnect()
        assert det._connected is False
        assert det._area_instance == 0
        assert det._game_state == 0


class TestPresent:
    def _det_with_reader(self, attack_range=15.0, entities=None):
        det = MemoryMobDetector({"attack_range": attack_range})
        det._connected = True
        det._area_instance = 0x12345
        det.mem = MagicMock()
        det.mem.connected = True
        det._last_update = 0.0
        det.reader = MagicMock()
        det.reader.read_local_player.return_value = MagicMock(grid_x=0, grid_y=0)
        det.reader.read_entities.return_value = entities or []
        # Не даём _resolve_game_state обнулить _area_instance
        det._resolve_game_state = MagicMock()
        return det

    def test_not_connected_returns_false(self):
        det = MemoryMobDetector()
        det.mem = MagicMock()
        det.mem.connected = False
        det.mem.connect.return_value = False
        assert det.present() is False

    def test_connected_no_hostiles(self):
        det = self._det_with_reader()
        assert det.present() is False

    def test_connected_with_hostiles(self):
        det = self._det_with_reader(entities=[_hostile(dist=5.0)])
        assert det.present() is True

    def test_hostile_out_of_range(self):
        det = self._det_with_reader(attack_range=10.0, entities=[_hostile(dist=50.0)])
        assert det.present() is False


class TestInHideout:
    def test_no_area_instance(self):
        det = MemoryMobDetector()
        det._area_instance = 0
        assert det.in_hideout() is False

    def test_hideout_code(self):
        det = MemoryMobDetector()
        det._area_instance = 0x12345
        det.reader = MagicMock()
        det.reader.read_area_info.return_value = {"code": "PlayerHideout"}
        assert det.in_hideout() is True

    def test_town_code(self):
        det = MemoryMobDetector()
        det._area_instance = 0x12345
        det.reader = MagicMock()
        det.reader.read_area_info.return_value = {"code": "2_HideoutTown"}
        assert det.in_hideout() is True

    def test_delve_mine(self):
        det = MemoryMobDetector()
        det._area_instance = 0x12345
        det.reader = MagicMock()
        det.reader.read_area_info.return_value = {"code": "DelveMine_Depth50"}
        assert det.in_hideout() is True

    def test_combat_zone(self):
        det = MemoryMobDetector()
        det._area_instance = 0x12345
        det.reader = MagicMock()
        det.reader.read_area_info.return_value = {"code": "1_MudFlats"}
        assert det.in_hideout() is False


class TestGetNearestHostile:
    def test_no_entities(self):
        det = MemoryMobDetector()
        det._last_entities = []
        assert det.get_nearest_hostile() is None

    def test_returns_closest(self):
        det = MemoryMobDetector({"attack_range": 15.0})
        far = _hostile(id=1, dist=10.0)
        close = _hostile(id=2, dist=3.0)
        det._last_entities = [far, close]
        result = det.get_nearest_hostile()
        assert result is not None
        assert result.id == 2

    def test_filters_dead(self):
        det = MemoryMobDetector({"attack_range": 15.0})
        det._last_entities = [_dead_hostile(dist=1.0), _hostile(id=2, dist=5.0)]
        result = det.get_nearest_hostile()
        assert result is not None
        assert result.id == 2

    def test_filters_friendly(self):
        det = MemoryMobDetector({"attack_range": 15.0})
        det._last_entities = [_friendly(dist=1.0), _hostile(id=2, dist=8.0)]
        result = det.get_nearest_hostile()
        assert result is not None
        assert result.id == 2


class TestGetAllHostiles:
    def test_filters_correctly(self):
        det = MemoryMobDetector({"attack_range": 15.0})
        det._last_entities = [
            _hostile(id=1, dist=5.0),
            _friendly(dist=1.0),
            _chest(dist=3.0),
            _dead_hostile(dist=2.0),
            _hostile(id=2, dist=50.0),
        ]
        result = det.get_all_hostiles()
        ids = [e.id for e in result]
        assert 1 in ids
        assert 2 in ids
        assert len(result) == 2


class TestGetChests:
    def test_filters_chests(self):
        det = MemoryMobDetector()
        det._last_entities = [_hostile(id=1), _chest(id=200), _chest(id=201)]
        result = det.get_chests()
        assert len(result) == 2
        assert all(e.category == EntityCategory.CHEST for e in result)


class TestUpdate:
    def test_throttling(self):
        det = MemoryMobDetector()
        det._connected = True
        det._area_instance = 0x12345
        det.mem = MagicMock()
        det.mem.connected = True
        det._last_update = 999999.0  # far future — will throttle
        result = det.update()
        assert result is False

    def test_reconnect_on_disconnect(self):
        det = MemoryMobDetector()
        det._connected = True
        det._area_instance = 0x12345
        det.mem = MagicMock()
        det.mem.connected = False
        det.mem.connect.return_value = False
        det._last_update = 0.0
        result = det.update()
        assert result is False

    def test_no_area_instance(self):
        det = MemoryMobDetector()
        det._connected = True
        det._area_instance = 0
        det.mem = MagicMock()
        det.mem.connected = True
        det._last_update = 0.0
        result = det.update()
        assert result is False


def _transition(id=1, x=5.0, y=5.0, dist=10.0, metadata="/Metadata/Objects/Transition"):
    return EntityData(
        id=id, category=EntityCategory.TRANSITION, metadata=metadata,
        x=x, y=y, z=0.0, grid_x=x, grid_y=y,
        hp_current=0, hp_max=0, hp_ratio=0.0,
        rarity=Rarity.NORMAL, is_friendly=False, distance=dist,
    )


class TestGetTransitions:
    def test_empty(self):
        det = MemoryMobDetector()
        det._last_entities = []
        assert det.get_transitions() == []

    def test_no_transitions(self):
        det = MemoryMobDetector()
        det._last_entities = [_hostile(id=1), _chest(id=200)]
        assert det.get_transitions() == []

    def test_single_transition(self):
        det = MemoryMobDetector()
        det._last_entities = [_transition(id=1, x=10.0, y=10.0)]
        result = det.get_transitions()
        assert len(result) == 1
        assert result[0].id == 1

    def test_two_far_apart(self):
        det = MemoryMobDetector()
        det._last_entities = [
            _transition(id=1, x=10.0, y=10.0, dist=5.0),
            _transition(id=2, x=50.0, y=50.0, dist=50.0),
        ]
        result = det.get_transitions()
        assert len(result) == 2

    def test_two_close_together(self):
        det = MemoryMobDetector()
        det._last_entities = [
            _transition(id=1, x=10.0, y=10.0, dist=5.0),
            _transition(id=2, x=10.1, y=10.1, dist=5.1),  # within 0.5 radius
        ]
        result = det.get_transitions()
        assert len(result) == 1
        assert result[0].id == 1  # closest to player

    def test_exact_same_position(self):
        det = MemoryMobDetector()
        det._last_entities = [
            _transition(id=1, x=10.0, y=10.0, dist=5.0),
            _transition(id=2, x=10.0, y=10.0, dist=6.0),
        ]
        result = det.get_transitions()
        assert len(result) == 1
        assert result[0].id == 1

    def test_three_in_clusters(self):
        det = MemoryMobDetector()
        det._last_entities = [
            _transition(id=1, x=10.0, y=10.0, dist=5.0),
            _transition(id=2, x=10.1, y=10.1, dist=5.1),
            _transition(id=3, x=50.0, y=50.0, dist=50.0),
        ]
        result = det.get_transitions()
        assert len(result) == 2

    def test_sorted_by_distance(self):
        det = MemoryMobDetector()
        det._last_entities = [
            _transition(id=1, x=50.0, y=50.0, dist=50.0),
            _transition(id=2, x=10.0, y=10.0, dist=5.0),
            _transition(id=3, x=30.0, y=30.0, dist=30.0),
        ]
        result = det.get_transitions()
        assert len(result) == 3
        assert result[0].id == 2
        assert result[1].id == 3
        assert result[2].id == 1

    def test_custom_radius(self):
        det = MemoryMobDetector()
        det._last_entities = [
            _transition(id=1, x=10.0, y=10.0, dist=5.0),
            _transition(id=2, x=11.0, y=11.0, dist=6.0),
        ]
        # Default radius 0.5 — different positions
        result = det.get_transitions(dedup_radius=0.5)
        assert len(result) == 2
        # Custom radius 2.0 — same group
        result = det.get_transitions(dedup_radius=2.0)
        assert len(result) == 1
