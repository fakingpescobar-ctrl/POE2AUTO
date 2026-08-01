"""Конфиг POE2AUTO: загрузка/сохранение config.yaml с дефолтами и валидацией."""
import copy
import logging
import os

import yaml

_log = logging.getLogger("poe2auto.config")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

DEFAULTS = {
    "game": {
        "window_title": "Path of Exile 2",
    },
    "capture": {
        "backend": "dxcam",
    },
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
        "radius_frac": 0,           # >0 → радиус = min(h,w)*frac (масштаб под разрешение); 0 → pickup_radius_px
        "center_offset_xy": [0, -40],
        "cooldown_ms": 100,
        "pickup_hold_ms": 60,
        "pickup_button": "a",
        "priority_mode": "size",
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
        "low_hp_hard_cap_frames": 120,
        "narrow_enabled": True,
        "narrow_min_w_frac": 0.02,
        "narrow_min_aspect": 4.0,
        "bootstrap_enabled": False,
        "bootstrap_min_w_frac": 0.02,
        "bootstrap_min_aspect": 1.8,
        "bootstrap_confirm_s": 0.25,
        "bootstrap_move_tol_px": 6,
        "dark_zone": {
            "enabled": True,
            "brightness_thr": 40.0,
            "min_aspect": 5.0,
        },
        "zone": {
            "roi": {"x1": 0.88, "y1": 0.10, "x2": 1.0, "y2": 0.32},
            "match_min": 0.85,
        },
    },
    "hotkeys": {
        "toggle": "f8",
        "diag": "f7",
        "snapshot": "f9",
        "overlay": "f6",
        "attack": "f2",
        "flask": "f3",
        "reload": "f5",
        "quit": "f12",
    },
}


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_SCHEMA = {
    "game.window_title": str,
    "capture.backend": str,
    "vision.marker_rgb": list,
    "vision.extra_colors": list,
    "vision.hue_tolerance": (int, float),
    "vision.sat_min": (int, float),
    "vision.val_min": (int, float),
    "vision.min_blob_area": (int, float),
    "vision.close_px": (int, float),
    "loot.pickup_radius_px": (int, float),
    "loot.radius_frac": (int, float),
    "loot.center_offset_xy": list,
    "loot.cooldown_ms": (int, float),
    "loot.pickup_hold_ms": (int, float),
    "loot.pickup_button": str,
    "loot.priority_mode": str,
    "hp.enabled": bool,
    "hp.orb": dict,
    "hp.threshold": (int, float),
    "hp.cooldown_ms": (int, float),
    "hp.flask_button": str,
    "hp.flask_hold_ms": (int, float),
    "hp.fill_mode": str,
    "hp.red_sat_min": (int, float),
    "hp.red_val_min": (int, float),
    "hp.row_fill_frac": (int, float),
    "hp.strip_frac": (int, float),
    "hp.gap_tol_frac": (int, float),
    "hp.rune_frac": (int, float),
    "hp.bottom_trim_frac": (int, float),
    "hp.full_ratio": (int, float),
    "hp.valid_val": (int, float),
    "hp.fallback_red_frac_min": (int, float),
    "hp.confirm_frames": (int, float),
    "hp.tripwire": bool,
    "hp_num.enabled": bool,
    "hp_num.auto_locate": bool,
    "hp_num.roi": dict,
    "hp_num.bin_thr": (int, float),
    "hp_num.match_min": (int, float),
    "hp_num.min_glyph_h": (int, float),
    "hp_num.max_hp": (int, float),
    "hp_num.menu_grace_s": (int, float),
    "auto_attack.enabled": bool,
    "auto_attack.use_memory": (bool, str),
    "auto_attack.attack_range": (int, float),
    "auto_attack.button": str,
    "auto_attack.roi": dict,
    "auto_attack.red_sat_min": (int, float),
    "auto_attack.red_val_min": (int, float),
    "auto_attack.min_run_frac": (int, float),
    "auto_attack.max_h_frac": (int, float),
    "auto_attack.min_aspect": (int, float),
    "auto_attack.min_fill": (int, float),
    "auto_attack.release_grace_s": (int, float),
    "auto_attack.low_hp_lock_timeout_s": (int, float),
    "auto_attack.low_hp_min_px": (int, float),
    "auto_attack.low_hp_hard_cap_s": (int, float),
    "auto_attack.low_hp_hard_cap_frames": int,
    "auto_attack.dark_zone.enabled": bool,
    "auto_attack.dark_zone.brightness_thr": (int, float),
    "auto_attack.dark_zone.min_aspect": (int, float),
    "auto_attack.narrow_enabled": bool,
    "auto_attack.narrow_min_w_frac": (int, float),
    "auto_attack.narrow_min_aspect": (int, float),
    "auto_attack.bootstrap_enabled": bool,
    "auto_attack.bootstrap_min_w_frac": (int, float),
    "auto_attack.bootstrap_min_aspect": (int, float),
    "auto_attack.bootstrap_confirm_s": (int, float),
    "auto_attack.bootstrap_move_tol_px": (int, float),
    "auto_attack.zone.roi": dict,
    "auto_attack.zone.match_min": (int, float),
    "hotkeys.toggle": str,
    "hotkeys.diag": str,
    "hotkeys.snapshot": str,
    "hotkeys.overlay": str,
    "hotkeys.attack": str,
    "hotkeys.flask": str,
    "hotkeys.reload": str,
    "hotkeys.quit": str,
}


def _get_nested(cfg, path):
    """cfg['a']['b']['c'] по строке 'a.b.c'."""
    keys = path.split(".")
    for k in keys:
        if not isinstance(cfg, dict):
            return None
        cfg = cfg.get(k)
    return cfg


def validate_config(cfg):
    """Проверить типы критичных полей. Логирует предупреждения, исправляет автоматически."""
    ok = True
    for path, expected in _SCHEMA.items():
        val = _get_nested(cfg, path)
        if val is None:
            continue
        if isinstance(expected, tuple):
            if not isinstance(val, expected):
                _log.warning("config: %s = %r (ожидалось %s)", path, val, expected)
                ok = False
        else:
            if not isinstance(val, expected):
                _log.warning("config: %s = %r (ожидалось %s)", path, val, expected)
                ok = False
    return ok


def load_config():
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})
    validate_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return CONFIG_PATH
