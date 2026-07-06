"""Детект полосы HP моба (рара/уника) вверху по центру экрана — для авто-атаки.

Когда наводишься/в бою с монстром, PoE2 рисует вверху по центру его HP-бар:
красная полоса на тёмной подложке + имя. Ловим её как красную связную компоненту
ФОРМЫ ПОЛОСЫ — тонкий широкий сплошной прямоугольник (w ≫ h, высокий fill).
Красные пятна сцены (камень/лава/факелы в красно-оранжевой локации) — это почти
квадратные бесформенные блобы (aspect ≈ 1, большая высота) и отсекаются формой.
Бар виден (даже частично) = моб жив → бот держит R1. Бар пропал = цели нет → отпускает.
"""
import cv2
import numpy as np


class MobBarDetector:
    def __init__(self, cfg):
        self.roi = cfg.get("roi", {"x1": 0.36, "y1": 0.02, "x2": 0.64, "y2": 0.12})
        self.sat_min = cfg.get("red_sat_min", 150)
        self.val_min = cfg.get("red_val_min", 90)
        # признаки ПОЛОСЫ (а не пятна сцены): длинная, тонкая, сплошная
        self.min_w_frac = cfg.get("min_run_frac", 0.018)   # мин. ширина (доля кадра)
        self.max_h_frac = cfg.get("max_h_frac", 0.030)     # макс. высота (доля кадра)
        self.min_aspect = cfg.get("min_aspect", 3.0)       # w/h — полоса вытянута
        self.min_fill = cfg.get("min_fill", 0.45)          # area/(w*h) — полоса сплошная

        # диагностика последнего вызова present() — для дампа улик при ложном срабатывании
        self.last_roi_px = None    # (x1, y1, x2, y2) в координатах кадра
        self.last_mask = None      # маска красного внутри ROI
        self.last_match = None     # dict с bbox/aspect/fill компоненты, что дала True

    def present(self, frame_bgr):
        """True, если в ROI есть красная компонента ФОРМЫ полосы HP (моб жив)."""
        fh, fw = frame_bgr.shape[:2]
        x1, y1 = int(self.roi["x1"] * fw), int(self.roi["y1"] * fh)
        x2, y2 = int(self.roi["x2"] * fw), int(self.roi["y2"] * fh)
        self.last_roi_px = (x1, y1, x2, y2)
        sub = frame_bgr[y1:y2, x1:x2]
        if sub.size == 0:
            self.last_mask, self.last_match = None, None
            return False
        hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
        mask = cv2.bitwise_or(
            cv2.inRange(hsv, (0, self.sat_min, self.val_min), (10, 255, 255)),
            cv2.inRange(hsv, (170, self.sat_min, self.val_min), (179, 255, 255)))
        self.last_mask = mask
        min_w = max(8, int(self.min_w_frac * fw))
        max_h = max(4, int(self.max_h_frac * fh))
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            if (w >= min_w and h <= max_h
                    and w / max(1, h) >= self.min_aspect
                    and area / max(1, w * h) >= self.min_fill):
                self.last_match = {"x": x, "y": y, "w": w, "h": h, "area": area,
                                    "aspect": w / max(1, h), "fill": area / max(1, w * h)}
                return True
        self.last_match = None
        return False
