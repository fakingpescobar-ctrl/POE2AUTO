"""Тесты для EC2MobDetector (ExileCore2 TCP bridge).

Проверяет парсинг JSON → EntityData, логику детекции мобов,
hideout-определение, player vitals — без реального TCP-соединения.
"""
from unittest.mock import MagicMock, patch

import pytest

from entity_reader import EntityCategory, Rarity, EntityData
from ec2_detect import EC2MobDetector


# --- Helpers ---

def _raw_entity(eid=1, et="m", path="/Monsters/Test", hostile=1,
                gp=None, wp=None, life="__default__", rarity="normal"):
    """Создать raw Entity_c JSON dict (формат ShareData).

    life='__default__' → стандартный life; life=None → поле 'l' отсутствует.
    """
    default_life = [1000, 800, 0, 0, 0, 0, 500, 400, 0]
    raw = {
        "i": eid,
        "et": et,
        "p": path,
        "h": hostile,
        "gp": gp or [100, 200],
        "wp": wp or [100, 200, 0],
        "r": rarity,
        "ia": 1,
        "t": 1,
    }
    raw["l"] = default_life if life == "__default__" else life
    return raw


def _raw_player(gp=None, life=None):
    """Создать raw PlayerInfo_c JSON dict."""
    return {
        "gp": gp or [100, 200],
        "l": life or [5000, 4500, 0, 200, 150, 0, 3000, 2800, 0],
        "b": [],
        "db": [],
        "im": 0,
        "lv": 95,
    }


def _make_data(entities=None, player="__default__", area="MapWorlds/Test",
               game_state=20, area_hash=12345):
    """Создать полный GetDataObject JSON.

    player='__default__' → стандартный player_info; player=None → 'pi' отсутствует.
    """
    pi = _raw_player() if player == "__default__" else player
    return {
        "g_s": game_state,
        "area_raw_name": area,
        "ah": area_hash,
        "awake_entities": entities or [],
        "pi": pi,
    }


def _det_with_data(data):
    """Создать EC2MobDetector и заполнить его данными (без TCP)."""
    det = EC2MobDetector({"attack_range": 15.0})
    det._connected = True
    det.client = MagicMock()
    det.client.connected = True
    det.client.get_data.return_value = data
    det.update()
    return det


# --- Tests: Init ---

class TestInit:
    def test_defaults(self):
        det = EC2MobDetector()
        assert det.attack_range == 15.0
        assert not det._connected
        assert det._area_code == ""

    def test_custom_config(self):
        det = EC2MobDetector({"attack_range": 20.0})
        assert det.attack_range == 20.0

    def test_custom_host_port(self):
        det = EC2MobDetector({"ec2_host": "192.168.1.5", "ec2_port": 12345})
        assert det.client.host == "192.168.1.5"
        assert det.client.port == 12345


# --- Tests: Available ---

class TestAvailable:
    def test_not_connected(self):
        det = EC2MobDetector()
        assert det.available is False

    def test_connected_no_area(self):
        det = EC2MobDetector()
        det._connected = True
        det.client = MagicMock()
        det.client.connected = True
        det._area_code = ""
        assert det.available is False

    def test_connected_with_area(self):
        det = EC2MobDetector()
        det._connected = True
        det.client = MagicMock()
        det.client.connected = True
        det._area_code = "MapWorlds/Test"
        assert det.available is True


# --- Tests: Entity Parsing ---

class TestEntityParsing:
    def test_single_monster(self):
        data = _make_data(entities=[
            _raw_entity(eid=1, et="m", hostile=1,
                        life=[1000, 800, 0, 0, 0, 0, 500, 400, 0]),
        ])
        det = _det_with_data(data)
        assert len(det._last_entities) == 1
        e = det._last_entities[0]
        assert e.category == EntityCategory.MONSTER
        assert e.hp_current == 800
        assert e.hp_max == 1000
        assert e.hp_ratio == 0.8
        assert not e.is_friendly
        assert e.rarity == Rarity.NORMAL

    def test_rarity_mapping(self):
        for rarity_str, expected in [
            ("normal", Rarity.NORMAL),
            ("magic", Rarity.MAGIC),
            ("rare", Rarity.RARE),
            ("unique", Rarity.UNIQUE),
        ]:
            data = _make_data(entities=[
                _raw_entity(eid=1, rarity=rarity_str),
            ])
            det = _det_with_data(data)
            assert det._last_entities[0].rarity == expected

    def test_entity_type_mapping(self):
        cases = [
            ("m", EntityCategory.MONSTER),
            ("p", EntityCategory.PLAYER),
            ("c", EntityCategory.CHEST),
            ("t", EntityCategory.TRANSITION),
            ("n", EntityCategory.NPC),
            ("o", EntityCategory.OTHER),
        ]
        for et, expected_cat in cases:
            data = _make_data(entities=[
                _raw_entity(eid=1, et=et),
            ])
            det = _det_with_data(data)
            assert det._last_entities[0].category == expected_cat

    def test_player_position_used_for_distance(self):
        data = _make_data(
            player=_raw_player(gp=[100, 200]),
            entities=[
                _raw_entity(eid=1, gp=[110, 200]),  # distance = 10
            ],
        )
        det = _det_with_data(data)
        e = det._last_entities[0]
        assert e.distance == pytest.approx(10.0, abs=0.01)

    def test_grid_position_parsed(self):
        data = _make_data(entities=[
            _raw_entity(eid=1, gp=[150, 250]),
        ])
        det = _det_with_data(data)
        e = det._last_entities[0]
        assert e.grid_x == 150.0
        assert e.grid_y == 250.0

    def test_world_position_parsed(self):
        data = _make_data(entities=[
            _raw_entity(eid=1, wp=[1.5, 2.5, 3.5]),
        ])
        det = _det_with_data(data)
        e = det._last_entities[0]
        assert e.x == 1.5
        assert e.y == 2.5
        assert e.z == 3.5

    def test_unique_monster_is_boss(self):
        data = _make_data(entities=[
            _raw_entity(eid=1, et="m", rarity="unique"),
        ])
        det = _det_with_data(data)
        assert det._last_entities[0].is_boss is True

    def test_friendly_entity(self):
        data = _make_data(entities=[
            _raw_entity(eid=1, et="m", hostile=0),
        ])
        det = _det_with_data(data)
        assert det._last_entities[0].is_friendly is True

    def test_no_life_component(self):
        data = _make_data(entities=[
            _raw_entity(eid=1, life=None),
        ])
        det = _det_with_data(data)
        e = det._last_entities[0]
        assert e.hp_current == 0
        assert e.hp_max == 0
        assert e.hp_ratio == 0.0

    def test_skip_non_dict(self):
        data = _make_data(entities=[
            "not_a_dict",
            _raw_entity(eid=1),
            123,
            None,
        ])
        det = _det_with_data(data)
        assert len(det._last_entities) == 1

    def test_path_fallback_categorization(self):
        """Если 'et' пуст, категория определяется по path."""
        data = _make_data(entities=[
            _raw_entity(eid=1, et="", path="/Monsters/Test"),
            _raw_entity(eid=2, et="", path="/Characters/Player"),
            _raw_entity(eid=3, et="", path="/Chests/Gold"),
        ])
        det = _det_with_data(data)
        cats = [e.category for e in det._last_entities]
        assert EntityCategory.MONSTER in cats
        assert EntityCategory.PLAYER in cats
        assert EntityCategory.CHEST in cats


# --- Tests: Mob Queries ---

class TestMobQueries:
    def test_present_no_mobs(self):
        det = _det_with_data(_make_data(entities=[]))
        assert det.present() is False

    def test_present_with_hostile_in_range(self):
        data = _make_data(
            player=_raw_player(gp=[100, 200]),
            entities=[
                _raw_entity(eid=1, hostile=1, gp=[105, 200]),  # dist=5
            ],
        )
        det = _det_with_data(data)
        assert det.present() is True

    def test_present_with_hostile_out_of_range(self):
        data = _make_data(
            player=_raw_player(gp=[100, 200]),
            entities=[
                _raw_entity(eid=1, hostile=1, gp=[200, 200]),  # dist=100
            ],
        )
        det = EC2MobDetector({"attack_range": 15.0})
        det._connected = True
        det.client = MagicMock()
        det.client.connected = True
        det.client.get_data.return_value = data
        det.update()
        assert det.stats["hostile_in_range"] == 0

    def test_get_nearest_hostile(self):
        data = _make_data(
            player=_raw_player(gp=[100, 200]),
            entities=[
                _raw_entity(eid=1, hostile=1, gp=[110, 200]),  # dist=10
                _raw_entity(eid=2, hostile=1, gp=[105, 200]),  # dist=5
                _raw_entity(eid=3, hostile=1, gp=[120, 200]),  # dist=20
            ],
        )
        det = _det_with_data(data)
        nearest = det.get_nearest_hostile()
        assert nearest is not None
        assert nearest.id == 2

    def test_get_all_hostiles_excludes_dead(self):
        data = _make_data(
            player=_raw_player(gp=[100, 200]),
            entities=[
                _raw_entity(eid=1, hostile=1, life=[1000, 500, 0, 0, 0, 0, 0, 0, 0]),
                _raw_entity(eid=2, hostile=1, life=[1000, 0, 0, 0, 0, 0, 0, 0, 0]),  # dead
            ],
        )
        det = _det_with_data(data)
        all_h = det.get_all_hostiles()
        assert len(all_h) == 1
        assert all_h[0].id == 1

    def test_get_best_target_priority(self):
        data = _make_data(
            player=_raw_player(gp=[100, 200]),
            entities=[
                _raw_entity(eid=1, hostile=1, gp=[105, 200], rarity="normal"),
                _raw_entity(eid=2, hostile=1, gp=[108, 200], rarity="rare"),
                _raw_entity(eid=3, hostile=1, gp=[110, 200], rarity="unique"),
            ],
        )
        det = _det_with_data(data)
        best = det.get_best_target()
        assert best.id == 3  # unique has highest priority


# --- Tests: Hideout ---

class TestHideout:
    def test_hideout_area(self):
        det = _det_with_data(_make_data(area="Hideout"))
        assert det.in_hideout() is True

    def test_player_hideout(self):
        det = _det_with_data(_make_data(area="PlayerHideout"))
        assert det.in_hideout() is True

    def test_map_area_not_hideout(self):
        det = _det_with_data(_make_data(area="MapWorlds/Test"))
        assert det.in_hideout() is False

    def test_town_area(self):
        det = _det_with_data(_make_data(area="Act1_town"))
        assert det.in_hideout() is True


# --- Tests: Player Vitals ---

class TestPlayerVitals:
    def test_read_vitals_normal(self):
        det = _det_with_data(_make_data(
            player=_raw_player(life=[5000, 4500, 0, 200, 150, 0, 3000, 2800, 0])
        ))
        hp, mana = det.read_player_vitals()
        assert hp == pytest.approx(0.9, abs=0.001)
        assert mana == pytest.approx(2800 / 3000, abs=0.001)

    def test_read_vitals_zero_max(self):
        det = _det_with_data(_make_data(
            player=_raw_player(life=[0, 0, 0, 0, 0, 0, 0, 0, 0])
        ))
        hp, mana = det.read_player_vitals()
        assert hp is None
        assert mana is None

    def test_read_vitals_no_player(self):
        det = _det_with_data(_make_data(player=None))
        hp, mana = det.read_player_vitals()
        assert hp is None
        assert mana is None


# --- Tests: Connect / Disconnect ---

class TestConnect:
    @patch("ec2_detect.EC2Client")
    def test_connect_success(self, MockClient):
        mock_client = MockClient.return_value
        mock_client.connect.return_value = True
        mock_client.get_data.return_value = _make_data()
        det = EC2MobDetector()
        det.client = mock_client
        assert det.connect() is True
        assert det._connected is True

    @patch("ec2_detect.EC2Client")
    def test_connect_failure(self, MockClient):
        mock_client = MockClient.return_value
        mock_client.connect.return_value = False
        det = EC2MobDetector()
        det.client = mock_client
        assert det.connect() is False
        assert det._connected is False

    def test_disconnect(self):
        det = EC2MobDetector()
        det.client = MagicMock()
        det._connected = True
        det._area_code = "test"
        det.disconnect()
        assert det._connected is False
        assert det._area_code == ""
