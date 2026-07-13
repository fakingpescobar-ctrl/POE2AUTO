"""Конфиг POE2AUTO: загрузка/сохранение config.yaml с дефолтами."""
import copy
import os

import yaml

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

DEFAULTS = {
    "game": {
        "window_title": "Path of Exile 2",
    },
    "capture": {
        "backend": "dxcam",
    },
    "vision": {
        "marker_rgb": [255, 0, 200],   # розовый маркер лут-фильтра
        "extra_colors": [],            # дополнительные цвета (мульти-маркер)
        "hue_tolerance": 8,
        "sat_min": 120,
        "val_min": 120,
        "min_blob_area": 120,
        "close_px": 3,
    },
    "loot": {
        "pickup_radius_px": 250,
        "center_offset_xy": [0, -40],  # центр персонажа = центр кадра + offset
        "cooldown_ms": 100,            # пауза между нажатиями pickup (быстрый подбор)
        "pickup_hold_ms": 60,          # сколько держать кнопку A
        "pickup_button": "a",          # кнопка подбора на Xbox-раскладке (Cross на DualSense)
    },
    "hp": {
        "enabled": False,              # включается калибратором (calibrate_hp.bat)
        "orb": {"cx": 0.06, "cy": 0.90, "r": 0.085},  # центр/радиус орба (доли кадра)
        "threshold": 0.55,             # пить флягу, когда HP ниже этой доли
        "cooldown_ms": 3000,           # пауза между флягами (≈ длительность фляги)
        "flask_button": "left_trigger",  # L2 на DualSense
        "flask_hold_ms": 120,          # длительность нажатия L2
        "fill_mode": "linear",         # linear (по высоте, интуитивно) | area (по площади круга)
        "red_sat_min": 60,             # порог насыщенности красного
        "red_val_min": 25,             # порог яркости красного (орб тёмный)
        "row_fill_frac": 0.35,         # доля красного в строке = «залито»
        "strip_frac": 0.30,            # (устар.) ширина центральной полоски — больше не используется в скане
        "gap_tol_frac": 0.10,          # допуск разрыва жидкости (доля радиуса)
        "rune_frac": 0.0,              # ширина центральной полосы с руной-механикой (доля радиуса), исключается из скана
        "bottom_trim_frac": 0.05,      # подрезка тёмного нижнего обода/таскбара (доля радиуса)
        "full_ratio": 1.0,             # сколько читается при полном HP (<1 из-за глянцевого верха): real = measured/full_ratio
        "tripwire": True,              # подстраховка патчем на высоте порога
        "confirm_frames": 2,           # сколько кадров подряд HP<порога перед флягой (анти-мерцание)
        "min_valid_ratio": 0.10,       # чтение ниже = орб затемнён/перекрыт → не реальный HP, не пьём
        "valid_val": 90,               # мин. яркость (V) залитой части; тусклый орб в меню отсекаем
    },
    "hotkeys": {
        "toggle": "f8",                # вкл/выкл авто-подбор
        "diag": "f7",                  # вкл/выкл диагностику HP
        "snapshot": "f9",              # ручной снимок-улика
        "overlay": "f6",               # показать/скрыть оверлей
        "flask": "f3",                 # вкл/выкл авто-фляску HP
        "quit": "f12",                 # выход
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


def load_config():
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return CONFIG_PATH
