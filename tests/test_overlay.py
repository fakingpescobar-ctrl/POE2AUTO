"""Tests for overlay.py — OverlayState and _hp_color helper."""
import time
import pytest

from overlay import OverlayState, _hp_color, C_DIM, C_ON, C_ACCENT, C_WARN


class TestHpColor:
    """Test HP percentage to color mapping."""

    def test_none_returns_dim(self):
        assert _hp_color(None) == C_DIM

    def test_full_hp_returns_green(self):
        assert _hp_color(1.0) == C_ON
        assert _hp_color(0.95) == C_ON
        assert _hp_color(0.88) == C_ON

    def test_medium_hp_returns_accent(self):
        assert _hp_color(0.87) == C_ACCENT
        assert _hp_color(0.7) == C_ACCENT
        assert _hp_color(0.5) == C_ACCENT

    def test_low_hp_returns_red(self):
        assert _hp_color(0.49) == C_WARN
        assert _hp_color(0.3) == C_WARN
        assert _hp_color(0.0) == C_WARN

    def test_boundary_088(self):
        """Exactly 0.88 should be green (>= 0.88)."""
        assert _hp_color(0.88) == C_ON

    def test_boundary_05(self):
        """Exactly 0.5 should be accent (>= 0.5)."""
        assert _hp_color(0.5) == C_ACCENT


class TestOverlayState:
    """Test OverlayState defaults and attribute access."""

    def test_defaults(self):
        state = OverlayState()
        assert state.auto_loot is False
        assert state.attack_enabled is False
        assert state.attack_on is False
        assert state.attacking is False
        assert state.flask_enabled is False
        assert state.flask_on is True
        assert state.diag is False
        assert state.hp_pct is None
        assert state.hp_cur is None
        assert state.hp_max is None
        assert state.es_cur is None
        assert state.es_max is None
        assert state.ward_cur is None
        assert state.ward_max is None
        assert state.flasks == 0
        assert state.presses == 0
        assert state.fps == 0.0
        assert state.foreground is True
        assert state.locate_state == "off"
        assert state.locate_info == ""
        assert state.notification == ""
        assert state.notification_until == 0.0
        assert state.debug_hp == ""
        assert state.debug_mob == ""
        assert state.debug_zone == ""
        assert state.memory_status == "off"
        assert state.attack_mode == ""
        assert state.atk_fallback == ""
        assert state.modules == []
        assert state.mem_connected is False
        assert state.mem_reading is False
        assert state.ent_total == 0
        assert state.ent_hostile == 0
        assert state.ent_in_range == 0
        assert state.nearest_dist is None
        assert state.radial_visible is False
        assert state.attack_range == 0.0
        assert state.radial_scale == 20.0
        assert state.radial_center == (0, 0)

    def test_mutate_attributes(self):
        """State is mutable — bot writes to it directly."""
        state = OverlayState()
        state.auto_loot = True
        state.hp_pct = 0.65
        state.hp_cur = 1200
        state.hp_max = 1500
        state.es_cur = 300
        state.es_max = 500
        state.fps = 60.0
        state.modules = [("reader", True, "ok"), ("attack", False, "no mem")]
        state.nearest_dist = 5.3

        assert state.auto_loot is True
        assert state.hp_pct == 0.65
        assert state.hp_cur == 1200
        assert state.hp_max == 1500
        assert state.es_cur == 300
        assert state.es_max == 500
        assert state.fps == 60.0
        assert len(state.modules) == 2
        assert state.nearest_dist == 5.3

    def test_notification_lifecycle(self):
        """Notification has text and expiry time."""
        state = OverlayState()
        state.notification = "Map complete!"
        state.notification_until = time.time() + 5.0
        assert state.notification == "Map complete!"
        assert state.notification_until > time.time()

    def test_radial_center_tuple(self):
        state = OverlayState()
        state.radial_center = (100, 200)
        assert state.radial_center == (100, 200)

    def test_modules_list_mutation(self):
        """Modules list can be built incrementally."""
        state = OverlayState()
        state.modules.append(("core_map", True, "loaded"))
        state.modules.append(("attack_worker", True, "running"))
        state.modules.append(("flask", False, "disabled"))
        assert len(state.modules) == 3
        assert state.modules[0] == ("core_map", True, "loaded")
        assert state.modules[2][1] is False
