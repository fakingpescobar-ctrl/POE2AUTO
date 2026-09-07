"""Integration tests for overlay_sync — sync_overlay correctly populates OverlayState.

Covers critical path: bot state → overlay state fields.
Prevents regression where overlay shows wrong attack mode, HP, or module status.
"""
import sys
from unittest.mock import MagicMock

# Mock heavy dependencies
sys.modules.setdefault('game_memory', MagicMock())
sys.modules.setdefault('probe_cast', MagicMock())
sys.modules.setdefault('screen_capture', MagicMock())
sys.modules.setdefault('virtual_xbox', MagicMock())
sys.modules.setdefault('dualsense_bridge', MagicMock())
sys.modules.setdefault('nav_worker', MagicMock())

from bot.state import BotState
from bot.overlay_sync import sync_overlay
from utils import Toggle


def _make_state() -> BotState:
    cfg = {
        "game": {"window_title": "PoE2"},
        "capture": {"backend": "dxcam"},
        "loot": {"colors": [[235, 0, 185]], "radius_frac": 0, "pickup_radius_px": 400,
                 "center_offset_xy": [0, 0], "cooldown_ms": 100, "pickup_button": "a",
                 "pickup_hold_ms": 80},
        "hp": {"enabled": False},
        "hp_num": {"enabled": False},
        "auto_attack": {"enabled": False},
        "hotkeys": {
            "toggle": "f8", "quit": "f12", "diag": "f7", "snapshot": "f9",
            "overlay": "f6", "attack": "f2", "flask": "f3", "radial": "f4",
            "nav": "f1", "stash": "f11", "map_run": "f10",
        },
    }
    s = BotState(cfg)
    # Ensure hp_engine exists (normally set up by main_loop init)
    s.rebuild_hp_engine()
    # Mock win for sync_overlay
    s.win = MagicMock()
    s.win.hwnd = 12345
    s.win.is_foreground.return_value = True
    return s


class TestSyncOverlayBasic:
    """Basic sync: auto_loot, attack_on, flask_on from toggle states."""

    def test_auto_loot_reflects_toggle(self):
        s = _make_state()
        s.auto = Toggle(default=True)
        sync_overlay(s)
        assert s.ov_state.auto_loot is True

    def test_auto_loot_off(self):
        s = _make_state()
        s.auto = Toggle(default=False)
        sync_overlay(s)
        assert s.ov_state.auto_loot is False

    def test_attack_on_reflects_toggle(self):
        s = _make_state()
        s.mob_det = MagicMock()
        s.attack = Toggle(default=True)
        sync_overlay(s)
        assert s.ov_state.attack_on is True

    def test_flask_on_reflects_toggle(self):
        s = _make_state()
        s.flask = Toggle(default=True)
        sync_overlay(s)
        assert s.ov_state.flask_on is True

    def test_diag_reflects_state(self):
        s = _make_state()
        s.diag.enabled = True
        sync_overlay(s)
        assert s.ov_state.diag is True


class TestSyncOverlayAttackMode:
    """Attack mode detection: CV, MEM, RDR, or empty."""

    def test_no_mob_det_mode_empty(self):
        s = _make_state()
        s.mob_det = None
        sync_overlay(s)
        assert s.ov_state.attack_mode == ""
        assert s.ov_state.attack_enabled is False

    def test_cv_mode(self):
        """MobBarDetector (not Memory/Reader) → CV."""
        s = _make_state()
        # MobBarDetector is not MemoryMobDetector or ReaderMobDetector
        mob = MagicMock(spec=["present", "last_match", "attack_range", "available"])
        mob.available = True
        mob.attack_range = 15.0
        s.mob_det = mob
        s.use_memory = False
        sync_overlay(s)
        assert s.ov_state.attack_mode == "CV"

    def test_rdr_mode(self):
        """ReaderMobDetector → RDR."""
        from reader_detect import ReaderMobDetector
        s = _make_state()
        mob = MagicMock(spec=ReaderMobDetector)
        mob.available = True
        mob.attack_range = 55.0
        mob._connected = True
        mob._player_hp = None
        mob.stats = {}
        mob.get_all_hostiles.return_value = []
        s.mob_det = mob
        s.use_memory = True
        s.atk_enabled = True
        s.atk_cfg = {"use_memory": True}
        sync_overlay(s)
        assert s.ov_state.attack_mode == "RDR"

    def test_mem_mode(self):
        """MemoryMobDetector → MEM."""
        from mem_detect import MemoryMobDetector
        s = _make_state()
        mob = MagicMock(spec=MemoryMobDetector)
        mob.available = True
        mob.attack_range = 15.0
        mob._connected = True
        mob._player_hp = None
        mob.stats = {}
        mob.get_all_hostiles.return_value = []
        s.mob_det = mob
        s.use_memory = True
        s.atk_enabled = True
        s.atk_cfg = {"use_memory": True}
        sync_overlay(s)
        assert s.ov_state.attack_mode == "MEM"


class TestSyncOverlayHpPct:
    """HP percentage sync."""

    def test_hp_pct_from_engine(self):
        s = _make_state()
        s.hp_engine.hp_pct = 0.75
        s.hp_engine.hp_src = "num"
        s.mob_det = None
        sync_overlay(s)
        assert s.ov_state.hp_pct == 0.75


class TestSyncOverlayModuleStatus:
    """Module status dots in overlay."""

    def test_modules_list_populated(self):
        """sync_overlay must populate ov_state.modules."""
        s = _make_state()
        s.auto_flask = MagicMock()
        s.auto_flask.enabled = False
        sync_overlay(s)
        assert isinstance(s.ov_state.modules, list)
        assert len(s.ov_state.modules) >= 5  # ReaderBridge, MobDetector, HpEngine, AutoAttack, AutoFlask, AutoLoot, ...

    def test_auto_loot_module(self):
        """AutoLoot module reflects toggle state."""
        s = _make_state()
        s.auto = Toggle(default=True)
        sync_overlay(s)
        loot_mod = [m for m in s.ov_state.modules if m[0] == "AutoLoot"]
        assert len(loot_mod) == 1
        assert loot_mod[0][1] is True  # status flag
        assert loot_mod[0][2] == "ON"

    def test_auto_loot_module_off(self):
        s = _make_state()
        s.auto = Toggle(default=False)
        sync_overlay(s)
        loot_mod = [m for m in s.ov_state.modules if m[0] == "AutoLoot"]
        assert loot_mod[0][1] is False
        assert loot_mod[0][2] == "OFF"


class TestSyncOverlayEntityStats:
    """Entity diagnostics when reader/memory connected."""

    def test_entities_zero_when_no_mob_det(self):
        s = _make_state()
        s.mob_det = None
        s.atk_enabled = True
        s.atk_cfg = {"use_memory": True}
        sync_overlay(s)
        assert s.ov_state.ent_total == 0
        assert s.ov_state.ent_hostile == 0

    def test_entities_populated_from_reader(self):
        """ReaderMobDetector → entity counts populated."""
        from reader_detect import ReaderMobDetector
        s = _make_state()
        mob = MagicMock(spec=ReaderMobDetector)
        mob.available = True
        mob.attack_range = 55.0
        mob._connected = True
        mob._player_hp = None
        mob.stats = {"total_entities": 42, "hostile_in_range": 3}

        hostiles = [MagicMock(distance=10.0), MagicMock(distance=20.0)]
        mob.get_all_hostiles.return_value = hostiles

        s.mob_det = mob
        s.use_memory = True
        s.atk_enabled = True
        s.atk_cfg = {"use_memory": True}
        sync_overlay(s)
        assert s.ov_state.ent_total == 42
        assert s.ov_state.ent_hostile == 2
        assert s.ov_state.nearest_dist == 10.0


class TestSyncOverlayModulesCompleteness:
    """All 9 module entries must always be present."""

    MODULE_NAMES = [
        "ReaderBridge", "MobDetector", "HpEngine", "AutoAttack",
        "AutoFlask", "AutoLoot", "CoreMap", "VirtualXbox", "DualSense",
    ]

    def test_all_modules_present(self):
        s = _make_state()
        sync_overlay(s)
        names = [m[0] for m in s.ov_state.modules]
        for expected in self.MODULE_NAMES:
            assert expected in names, f"Module {expected} missing from overlay"
