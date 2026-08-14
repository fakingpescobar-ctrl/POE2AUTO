"""Детекция цвета(ов)-маркера: HSV-маски вокруг заданных RGB → центры пятен (цели).

Поддерживает один цвет [r,g,b] или несколько [[r,g,b], ...] — маски объединяются.
Красные оттенки оборачиваются по кругу HSV (H≈0 и H≈179 — оба красный).
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import cv2
import numpy as np


def rgb_to_hsv_bounds(marker_rgb, hue_tol=8, sat_min=120, val_min=120):
    """RGB маркера → список (low, high) границ в HSV (учёт wrap-around для красного)."""
    r, g, b = marker_rgb
    px = np.uint8([[[b, g, r]]])  # OpenCV ждёт BGR
    h = int(cv2.cvtColor(px, cv2.COLOR_BGR2HSV)[0][0][0])
    lo, hi = h - hue_tol, h + hue_tol
    if lo < 0:
        return [
            (np.array([0, sat_min, val_min], np.uint8), np.array([hi, 255, 255], np.uint8)),
            (np.array([179 + lo, sat_min, val_min], np.uint8), np.array([179, 255, 255], np.uint8)),
        ]
    if hi > 179:
        return [
            (np.array([lo, sat_min, val_min], np.uint8), np.array([179, 255, 255], np.uint8)),
            (np.array([0, sat_min, val_min], np.uint8), np.array([hi - 179, 255, 255], np.uint8)),
        ]
    return [(np.array([lo, sat_min, val_min], np.uint8), np.array([hi, 255, 255], np.uint8))]


def normalize_colors(markers):
    """[r,g,b] → [[r,g,b]]; [[r,g,b],...] остаётся списком."""
    if not markers:
        return []
    if isinstance(markers[0], (list, tuple)):
        return [list(c) for c in markers]
    return [list(markers)]


class ColorDetector:
    def __init__(self, markers, hue_tol=8, sat_min=120, val_min=120,
                 min_blob_area=120, close_px=3):
        self.colors = normalize_colors(markers)
        self.bounds = [rgb_to_hsv_bounds(c, hue_tol, sat_min, val_min) for c in self.colors]
        self.min_blob_area = min_blob_area
        self._open_kernel = np.ones((3, 3), np.uint8)
        self._close_kernel = np.ones((close_px, close_px), np.uint8) if close_px > 0 else None

    def detect(self, frame_bgr):
        """Возвращает (points, mask), points = [(cx, cy, area), ...] по убыванию площади."""
        if not self.bounds or frame_bgr is None or frame_bgr.size == 0:
            h, w = frame_bgr.shape[:2] if frame_bgr is not None and frame_bgr.size else (0, 0)
            return [], np.zeros((h, w), np.uint8)
        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        mask: Optional[np.ndarray] = None
        for ranges in self.bounds:
            for low, high in ranges:
                m = cv2.inRange(hsv, low, high)
                mask = m if mask is None else cv2.bitwise_or(mask, m)
        if mask is None:
            mask = np.zeros(frame_bgr.shape[:2], np.uint8)
        if self._close_kernel is not None:
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._close_kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._open_kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        points: List[Tuple[int, int, float]] = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_blob_area:
                continue
            mom = cv2.moments(c)
            if mom["m00"] == 0:
                continue
            cx = int(mom["m10"] / mom["m00"])
            cy = int(mom["m01"] / mom["m00"])
            points.append((cx, cy, area))

        points.sort(key=lambda p: p[2], reverse=True)
        return points, mask
