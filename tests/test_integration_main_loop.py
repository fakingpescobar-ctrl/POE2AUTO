"""Integration tests for main_loop — _log_status and init sequence.

Covers critical path: state → log output + overlay state.
Prevents regression where _log_status crashes on missing fields or wrong types.
"""
import sys
import logging
from unittest.mock import MagicMock, patch, PropertyMock

# Mock heavy C-extension/FFI dependencies
sys.modules.setdefault('game_memory', MagicMock())
sys.modules.setdefault('probe_cast', MagicMock())
sys.modules.setdefault('screen_capture', MagicMock())
sys.modules.setdefault('virtual_xbox', MagicMock())
sys.modules.setdefault('dualsense_bridge', MagicMock())
sys.modules.setdefault('nav_worker', MagicMock())

from bot.state import BotState
from hp_engine import HPEngine, HPDecision
from utils import Toggle


def _make_state() -> BotState:
    cfg = {
        "game": {"window_title": "PoE2"},
        "capture": {"backend": "dxcam"},
        "loot": {"colors": [[235, 0, 185]], "radius_frac": 0, "pickup_radius_px": 400,
                 "center_offset_xy": [0, 0], "cooldown_ms": 100, "pickup_button": "a",
                 "pickup_hold_ms": 80},
        "hp": {"enabled": True, "threshold": 0.88, "cooldown_ms": 333, "flask_hold_ms": 120,
               "flask_button": "left_trigger", "confirm_frames": 1, "valid_val": 45,
               "fallback_red_frac_min": 0.04, "num_low_grace_s": 0.35,
               "orb": {"cx": 0.06, "cy": 0.87, "r": 0.08}},
        "hp_num": {"enabled": False, "auto_locate": False, "menu_grace_s": 0.5,
                   "roi": {"x1": 0.05, "y1": 0.70, "x2": 0.12, "y2": 0.73}, "max_hp": 5000},
        "auto_attack": {"enabled": True, "use_memory": "reader", "attack_range": 55.0,
                        "button": "right_bumper", "release_grace_s": 0.6},
        "hotkeys": {
            "toggle": "f8", "quit": "f12", "diag": "f7", "snapshot": "f9",
            "overlay": "f6", "attack": "f2", "flask": "f3", "radial": "f4",
            "nav": "f1", "stash": "f11", "map_run": "f10",
        },
    }
    return BotState(cfg)


class TestLogStatusNoCrash:
    """_log_status must never crash regardless of state combination."""

    def _call_log_status(self, s, **overrides):
        """Import and call _log_status with a mocked win."""
        from bot.main_loop import _log_status
        s.win = MagicMock()
        s.win.hwnd = 12345
        s.win.is_foreground.return_value = True
        points = overrides.get("points", [])
        in_radius = overrides.get("in_radius", [])
        now = overrides.get("now", 100.0)
        full = overrides.get("full", MagicMock())
        _log_status(s, points, in_radius, now, full)

    def test_basic_no_crash(self):
        """Basic state — no mob_det, no HP reading."""
        s = _make_state()
        s.iters = 10
        s.last_status = 90.0
        s.presses = 5
        s.flasks = 2
        s.mob_det = None
        s.hp_engine = HPEngine()
        s.nav_worker = None
        s.auto_flask = MagicMock()
        s.auto_flask.enabled = False
        self._call_log_status(s, now=100.0)

    def test_hp_pct_active(self):
        """HP engine has active reading."""
        s = _make_state()
        s.iters = 10
        s.last_status = 90.0
        s.presses = 0
        s.flasks = 0
        s.mob_det = None
        s.hp_engine = HPEngine()
        s.hp_engine.hp_pct = 0.75
        s.hp_engine.hp_src = "num"
        s.nav_worker = None
        s.auto_flask = MagicMock()
        s.auto_flask.enabled = False
        self._call_log_status(s, now=100.0)

    def test_hp_pct_none(self):
        """HP engine has no reading."""
        s = _make_state()
        s.iters = 10
        s.last_status = 90.0
        s.presses = 0
        s.flasks = 0
        s.mob_det = None
        s.hp_engine = HPEngine()
        s.hp_engine.hp_pct = None
        s.nav_worker = None
        s.auto_flask = MagicMock()
        s.auto_flask.enabled = False
        self._call_log_status(s, now=100.0)

    def test_attack_mode_cv(self):
        """CV mob detector with attack on."""
        s = _make_state()
        s.iters = 10
        s.last_status = 90.0
        s.presses = 0
        s.flasks = 0
        mob = MagicMock(spec=["present", "last_match", "attack_range", "available"])
        mob.last_match = {"bootstrap": True}
        mob.attack_range = 15.0
        mob.available = True
        s.mob_det = mob
        s.use_memory = False
        s.attack = Toggle(default=True)
        s.r1_held = True
        s.hp_engine = HPEngine()
        s.nav_worker = None
        s.auto_flask = MagicMock()
        s.auto_flask.enabled = False
        self._call_log_status(s, now=100.0)

    def test_attack_mode_memory(self):
        """Memory mob detector — ent diagnostics."""
        from mem_detect import MemoryMobDetector
        s = _make_state()
        s.iters = 10
        s.last_status = 90.0
        s.presses = 0
        s.flasks = 0
        mob = MagicMock(spec=MemoryMobDetector)
        mob.attack_range = 15.0
        mob.available = True
        mob.read_player_vitals.return_value = (0.80, 0.50)
        mob.get_all_hostiles.return_value = []
        mob.stats = {"total_entities": 5, "hostile_in_range": 1}
        s.mob_det = mob
        s.use_memory = True
        s.attack = Toggle(default=False)
        s.r1_held = False
        s.hp_engine = HPEngine()
        s.nav_worker = None
        s.auto_flask = MagicMock()
        s.auto_flask.enabled = False
        self._call_log_status(s, now=100.0)

    def test_diag_enabled(self):
        """Debug overlay enabled → ov_state.debug_* populated."""
        from mem_detect import MemoryMobDetector
        s = _make_state()
        s.iters = 10
        s.last_status = 90.0
        s.presses = 0
        s.flasks = 0
        mob = MagicMock(spec=MemoryMobDetector)
        mob.attack_range = 15.0
        mob.available = True
        mob.read_player_vitals.return_value = (0.60, None)
        mob.get_all_hostiles.return_value = []
        mob.stats = {"total_entities": 3, "hostile_in_range": 2}
        s.mob_det = mob
        s.use_memory = True
        s.diag.enabled = True
        s.attack = Toggle(default=False)
        s.r1_held = False
        s.hp_engine = HPEngine()
        s.hp_engine.hp_pct = 0.60
        s.hp_engine.hp_src = "mem"
        s.nav_worker = None
        s.auto_flask = MagicMock()
        s.auto_flask.enabled = False
        self._call_log_status(s, now=100.0)
        assert s.ov_state.debug_hp is not None
        assert s.ov_state.debug_mob is not None

    def test_in_radius_populated(self):
        """in_radius list passed correctly."""
        s = _make_state()
        s.iters = 10
        s.last_status = 90.0
        s.presses = 0
        s.flasks = 0
        s.mob_det = None
        s.hp_engine = HPEngine()
        s.nav_worker = None
        s.auto_flask = MagicMock()
        s.auto_flask.enabled = False
        self._call_log_status(s, now=100.0, in_radius=[(1, 2), (3, 4)])


class TestLogStatusLogOutput:
    """Verify _log_status actually produces log output."""

    def test_log_contains_key_fields(self, caplog):
        """Log line must contain targets, auto, hp, streak, flasks."""
        s = _make_state()
        s.iters = 10
        s.last_status = 90.0
        s.presses = 5
        s.flasks = 3
        s.mob_det = None
        s.hp_engine = HPEngine()
        s.hp_engine.hp_pct = 0.80
        s.hp_engine.hp_src = "num"
        s.nav_worker = None
        s.auto_flask = MagicMock()
        s.auto_flask.enabled = False
        s.auto = Toggle(default=True)
        s.win = MagicMock()
        s.win.hwnd = 12345
        s.win.is_foreground.return_value = True

        from bot.main_loop import _log_status
        with caplog.at_level(logging.INFO, logger="poe2auto.bot"):
            _log_status(s, points=[(100, 200), (300, 400)], in_radius=[(100, 200)],
                        now=100.0, full=MagicMock())

        # Find the main status line
        status_lines = [r.message for r in caplog.records if "targets=" in r.message]
        assert len(status_lines) >= 1
        line = status_lines[0]
        assert "targets=2" in line
        assert "in_radius=1" in line
        assert "presses=5" in line
        assert "80%" in line
        assert "num" in line
