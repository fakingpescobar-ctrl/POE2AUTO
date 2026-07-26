# pytest configuration for POE2AUTO tests
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import cv2
import numpy as np


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')


def _has_fixture(name):
    return os.path.exists(os.path.join(FIXTURES_DIR, name))


# Fixtures for test frames — skipIf файл отсутствует
@pytest.fixture(scope="session")
def hp_frame():
    """Full HP frame (100% HP) - from _debug/hp_frame.png"""
    path = os.path.join(FIXTURES_DIR, 'hp_frame.png')
    if os.path.exists(path):
        return cv2.imread(path)
    pytest.skip("fixture file hp_frame.png not found")


@pytest.fixture(scope="session")
def hp_vis():
    """HP visualization frame"""
    path = os.path.join(FIXTURES_DIR, 'hp_vis.png')
    if os.path.exists(path):
        return cv2.imread(path)
    pytest.skip("fixture file hp_vis.png not found")


@pytest.fixture(scope="session")
def ocr_roi():
    """OCR ROI frame"""
    path = os.path.join(FIXTURES_DIR, '_ocr_roi.png')
    if os.path.exists(path):
        return cv2.imread(path)
    pytest.skip("fixture file _ocr_roi.png not found")


@pytest.fixture(scope="session")
def ocr_roi_174():
    """OCR ROI frame with current=174, max=0019 (full HP)"""
    path = os.path.join(FIXTURES_DIR, '_ocr_roi_095202_174_0019_ok_full.png')
    if os.path.exists(path):
        return cv2.imread(path)
    pytest.skip("fixture file _ocr_roi_095202_174_0019_ok_full.png not found")


@pytest.fixture(scope="session")
def ocr_roi_940():
    """OCR ROI frame with current=940, max=0026"""
    path = os.path.join(FIXTURES_DIR, '_ocr_roi_095231_940_0026_ok_full.png')
    if os.path.exists(path):
        return cv2.imread(path)
    pytest.skip("fixture file _ocr_roi_095231_940_0026_ok_full.png not found")


@pytest.fixture(scope="session")
def hp_check_frame():
    """HP orb check frame"""
    path = os.path.join(FIXTURES_DIR, 'hp_check_frame.png')
    if os.path.exists(path):
        return cv2.imread(path)
    pytest.skip("fixture file hp_check_frame.png not found")


@pytest.fixture(scope="session")
def mob_bar_frame():
    """Mob health bar frame"""
    path = os.path.join(FIXTURES_DIR, '_barmask.png')
    if os.path.exists(path):
        return cv2.imread(path)
    pytest.skip("fixture file _barmask.png not found")


@pytest.fixture(scope="session")
def loot_frame():
    """Loot detection frame"""
    path = os.path.join(FIXTURES_DIR, '_top.png')
    if os.path.exists(path):
        return cv2.imread(path)
    pytest.skip("fixture file _top.png not found")


@pytest.fixture
def sample_config():
    """Sample config matching actual config.py DEFAULTS structure"""
    return {
        "game": {"window_title": "Path of Exile 2"},
        "capture": {"backend": "mss"},
        "vision": {
            "marker_rgb": [235, 0, 185],
            "extra_colors": [],
            "hue_tolerance": 8,
            "sat_min": 200,
            "val_min": 120,
            "min_blob_area": 200,
            "close_px": 3,
        },
        "loot": {
            "pickup_radius_px": 400,
            "center_offset_xy": [0, -40],
            "cooldown_ms": 100,
            "pickup_hold_ms": 60,
            "pickup_button": "a",
        },
        "hp": {
            "enabled": False,
            "orb": {"cx": 0.0732, "cy": 0.8712, "r": 0.0799},
            "threshold": 0.88,
            "cooldown_ms": 333,
            "flask_button": "left_trigger",
            "flask_hold_ms": 120,
            "fill_mode": "linear",
            "red_sat_min": 24,
            "red_val_min": 52,
            "row_fill_frac": 0.28,
            "strip_frac": 0.2,
            "gap_tol_frac": 0.1,
            "rune_frac": 0.38,
            "bottom_trim_frac": 0.05,
            "full_ratio": 0.78,
            "valid_val": 45,
            "fallback_red_frac_min": 0.04,
            "confirm_frames": 1,
            "tripwire": True,
        },
        "hp_num": {
            "enabled": True,
            "auto_locate": True,
            "roi": {"x1": 0.066, "y1": 0.700, "x2": 0.137, "y2": 0.735},
            "bin_thr": 130,
            "match_min": 0.55,
            "min_glyph_h": 10,
            "max_hp": 1371,
            "menu_grace_s": 0.5,
        },
        "auto_attack": {
            "enabled": True,
            "use_memory": True,
            "attack_range": 15.0,
            "button": "right_bumper",
            "roi": {"x1": 0.22, "y1": 0.02, "x2": 0.78, "y2": 0.12},
            "red_sat_min": 150,
            "red_val_min": 90,
            "min_run_frac": 0.10,
            "max_h_frac": 0.030,
            "min_aspect": 3.0,
            "min_fill": 0.45,
            "release_grace_s": 0.6,
            "low_hp_lock_timeout_s": 5.0,
            "low_hp_min_px": 6,
            "low_hp_hard_cap_s": 6.0,
            "narrow_enabled": True,
            "narrow_min_w_frac": 0.02,
            "narrow_min_aspect": 4.0,
            "bootstrap_enabled": False,
            "bootstrap_min_w_frac": 0.02,
            "bootstrap_min_aspect": 1.8,
            "bootstrap_confirm_s": 0.25,
            "bootstrap_move_tol_px": 6,
            "zone": {"roi": {"x1": 0.88, "y1": 0.10, "x2": 1.0, "y2": 0.32}, "match_min": 0.85},
        },
        "hotkeys": {
            "toggle": "f8", "diag": "f7", "snapshot": "f9",
            "overlay": "f6", "attack": "f2", "flask": "f3",
            "reload": "f5", "quit": "f12",
        },
    }


@pytest.fixture
def temp_debug_dir(tmp_path):
    """Temporary directory for debug outputs"""
    debug_dir = tmp_path / "_debug"
    debug_dir.mkdir()
    return str(debug_dir)
