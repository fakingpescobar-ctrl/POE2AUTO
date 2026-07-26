"""Общие утилиты: Toggle (потокобезопасный флаг), build_detector."""
from __future__ import annotations

import threading
from typing import Any, Dict, List

from color_detector import ColorDetector


class Toggle:
    """Потокобезопасный флаг авто-режима."""
    def __init__(self, default: bool = False) -> None:
        self._on: bool = default
        self._lock = threading.Lock()

    def flip(self) -> bool:
        with self._lock:
            self._on = not self._on
            return self._on

    @property
    def on(self) -> bool:
        with self._lock:
            return self._on


def build_detector(cfg: Dict[str, Any]) -> ColorDetector:
    """Построить детектор цвета лута из секции vision конфига."""
    v = cfg["vision"]
    colors: List[List[int]] = [list(v["marker_rgb"])] + [list(c) for c in v.get("extra_colors", []) or []]
    return ColorDetector(
        colors,
        hue_tol=v.get("hue_tolerance", 8),
        sat_min=v.get("sat_min", 120),
        val_min=v.get("val_min", 120),
        min_blob_area=v.get("min_blob_area", 200),
        close_px=v.get("close_px", 3),
    )
