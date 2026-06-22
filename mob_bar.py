"""Детект полосы HP моба (рара/уника) вверху по центру экрана — для авто-атаки.

Когда наводишься/в бою с монстром, PoE2 рисует вверху по центру его HP-бар:
красная полоса на тёмной подложке + имя. Ловим её по сигнатуре «длинный
НЕПРЕРЫВНЫЙ горизонтальный красный отрезок» в ROI верх-центра — у факелов/лавы
и иконки рарника такого ровного длинного отрезка нет. Бар виден (даже частично)
= моб жив → бот держит R1 (авто-атака). Бар пропал = цели нет → отпускает.
"""
import cv2
import numpy as np


class MobBarDetector:
    def __init__(self, cfg):
        self.roi = cfg.get("roi", {"x1": 0.36, "y1": 0.02, "x2": 0.64, "y2": 0.12})
        self.sat_min = cfg.get("red_sat_min", 120)
        self.val_min = cfg.get("red_val_min", 90)
        # мин. длина непрерывного красного отрезка (доля ширины кадра), чтобы
        # считать это полосой HP, а не бликом/факелом/иконкой
        self.min_run_frac = cfg.get("min_run_frac", 0.018)

    def present(self, frame_bgr):
        """True, если в ROI есть горизонтальная красная полоса (моб жив)."""
        fh, fw = frame_bgr.shape[:2]
        x1, y1 = int(self.roi["x1"] * fw), int(self.roi["y1"] * fh)
        x2, y2 = int(self.roi["x2"] * fw), int(self.roi["y2"] * fh)
        sub = frame_bgr[y1:y2, x1:x2]
        if sub.size == 0:
            return False
        hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
        mask = cv2.bitwise_or(
            cv2.inRange(hsv, (0, self.sat_min, self.val_min), (10, 255, 255)),
            cv2.inRange(hsv, (170, self.sat_min, self.val_min), (179, 255, 255)))
        need = max(1, int(self.min_run_frac * fw))
        # горизонтальная эрозия линией длины need: пиксель выживает, только если
        # слева-направо подряд идёт >= need красных → значит есть длинный отрезок
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (need, 1))
        return bool(cv2.erode(mask, kernel).any())
