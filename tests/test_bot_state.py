"""Smoke tests for BotState — ensure all configs resolve correctly at init."""
import pytest
from unittest.mock import MagicMock, patch
import sys

# Mock heavy C-extension/FFI dependencies
sys.modules['game_memory'] = MagicMock()
sys.modules['probe_cast'] = MagicMock()
sys.modules['screen_capture'] = MagicMock()
sys.modules['virtual_xbox'] = MagicMock()
sys.modules['dualsense_bridge'] = MagicMock()
sys.modules['nav_worker'] = MagicMock()

from bot.state import BotState


def _make_config(**overrides) -> dict:
    """Minimal config that matches real config.yaml structure."""
    cfg = {
        "game": {"window_title": "Path of Exile 2"},
        "capture": {"backend": "dxcam"},
        "loot": {
            "colors": [[235, 0, 185]],
            "radius_frac": 0,
            "pickup_radius_px": 400,
            "center_offset_xy": [0, 0],
            "cooldown_ms": 100,
            "pickup_button": "a",
            "pickup_hold_ms": 80,
        },
        "hp": {
            "enabled": True,
            "threshold": 0.88,
            "cooldown_ms": 333,
            "flask_hold_ms": 120,
            "flask_button": "left_trigger",
            "confirm_frames": 1,
            "valid_val": 45,
            "fallback_red_frac_min": 0.04,
            "num_low_grace_s": 0.35,
            "orb": {"cx": 0.06, "cy": 0.87, "r": 0.08},
        },
        "hp_num": {
            "enabled": True,
            "auto_locate": True,
            "menu_grace_s": 0.5,
            "roi": {"x1": 0.05, "y1": 0.70, "x2": 0.12, "y2": 0.73},
            "max_hp": 5000,
        },
        "auto_attack": {
            "enabled": True,
            "use_memory": "reader",
            "attack_range": 55.0,
            "button": "right_bumper",
            "release_grace_s": 0.6,
        },
        "hotkeys": {
            "toggle": "f8",
            "quit": "f12",
            "diag": "f7",
            "snapshot": "f9",
            "overlay": "f6",
            "attack": "f2",
            "flask": "f3",
            "radial": "f4",
            "nav": "f1",
            "stash": "f11",
            "map_run": "f10",
        },
    }
    cfg.update(overrides)
    return cfg


class TestBotStateInit:
    """BotState must resolve ALL config flags at __init__ time."""

    def test_atk_enabled_from_config(self):
        """CRITICAL: atk_enabled must be True when config has enabled=True."""
        cfg = _make_config()
        s = BotState(cfg)
        assert s.atk_enabled is True, "atk_enabled must be True when auto_attack.enabled=True"

    def test_detect_mode_reader(self):
        """detect_mode must be 'reader' when use_memory='reader'."""
        cfg = _make_config()
        s = BotState(cfg)
        assert s.detect_mode == "reader"
        assert s.use_memory is True

    def test_hp_enabled_from_config(self):
        """hp_enabled must be True when hp.enabled=True."""
        cfg = _make_config()
        s = BotState(cfg)
        assert s.hp_enabled is True

    def test_hp_threshold_from_config(self):
        """hp_threshold must match config value."""
        cfg = _make_config()
        s = BotState(cfg)
        assert s.hp_threshold == 0.88

    def test_attack_disabled(self):
        """atk_enabled must be False when auto_attack.enabled=False."""
        cfg = _make_config(auto_attack={"enabled": False})
        s = BotState(cfg)
        assert s.atk_enabled is False
        # detect_mode still reflects use_memory setting — only atk_enabled gates execution
        assert s.detect_mode == "reader"
        assert s.use_memory is True

    def test_hp_disabled(self):
        """hp_enabled must be False when hp.enabled=False."""
        cfg = _make_config(hp={"enabled": False})
        s = BotState(cfg)
        assert s.hp_enabled is False

    def test_use_memory_cv(self):
        """detect_mode='cv' when use_memory=False."""
        cfg = _make_config(auto_attack={"enabled": True, "use_memory": False})
        s = BotState(cfg)
        assert s.detect_mode == "cv"
        assert s.use_memory is False

    def test_hotkeys_resolved(self):
        """All hotkeys must be resolved from config."""
        cfg = _make_config()
        s = BotState(cfg)
        assert s.k_toggle == "f8"
        assert s.k_quit == "f12"
        assert s.k_attack == "f2"
        assert s.k_flask == "f3"
        assert s.k_stash == "f11"
        assert s.k_map_run == "f10"
        assert s.k_reload == "f5"  # hardcoded

    def test_attack_range_resolved(self):
        """atk_grace must come from config."""
        cfg = _make_config()
        s = BotState(cfg)
        assert s.atk_grace == 0.6
