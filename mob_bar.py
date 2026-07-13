"""Детект полосы HP моба (рара/уника) вверху по центру экрана — для авто-атаки.

Когда наводишься/в бою с монстром, PoE2 рисует вверху по центру его HP-бар:
красная полоса на тёмной подложке + имя. Ловим её как красную связную компоненту
ФОРМЫ ПОЛОСЫ — тонкий широкий сплошной прямоугольник (w ≫ h, высокий fill).
Красные пятна сцены (камень/лава/факелы в красно-оранжевой локации) — это почти
квадратные бесформенные блобы (aspect ≈ 1, большая высота) и отсекаются формой.
Бар виден (даже частично) = моб жив → бот держит R1. Бар пропал = цели нет → отпускает.

Когда HP моба почти на нуле, от полосы остаётся крохотный огрызок красного у
левого края рамки (бар тает СПРАВА налево) — он уже не проходит фильтры формы
(слишком узкий/квадратный, легко спутать с декором). Чтобы не отпускать R1
прямо перед добиванием, детектор «залипает»: как только рамка бара была
уверенно опознана по форме, её координаты (x, y, w, h) запоминаются, и пока
внутри НИХ остаётся хоть немного красного — считаем бар всё ещё живым, даже
если остаток сам по себе форму полосы не держит.
"""
import time

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
        self.edge_margin_px = cfg.get("edge_margin_px", 2)  # компонента у самого края ROI = обрезанный артефакт декора, не бар
        # «добивание» почти мёртвого моба: рамку последнего уверенного матча
        # держим ещё столько секунд без подтверждений, ищем в ней хотя бы
        # столько красных пикселей
        self.low_hp_lock_timeout_s = cfg.get("low_hp_lock_timeout_s", 2.5)
        self.low_hp_min_px = cfg.get("low_hp_min_px", 12)

        # диагностика последнего вызова present() — для дампа улик при ложном срабатывании
        self.last_roi_px = None    # (x1, y1, x2, y2) в координатах кадра
        self.last_mask = None      # маска красного внутри ROI
        self.last_match = None     # dict с bbox/aspect/fill компоненты, что дала True

        self._lock = None      # (x, y, w, h) рамки последнего уверенного матча, координаты ROI-local
        self._lock_ts = 0.0    # когда рамка последний раз подтверждалась (строго или добиванием)

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
        roi_w = sub.shape[1]
        margin = self.edge_margin_px
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        now = time.perf_counter()
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            # компонента, упирающаяся в левый/правый край ROI — обрезанный кусок
            # декора сцены (факел/жаровня), а не настоящий бар (тот весь помещается
            # в ROI, т.к. рисуется по центру над мобом)
            if x <= margin or (x + w) >= (roi_w - margin):
                continue
            if (w >= min_w and h <= max_h
                    and w / max(1, h) >= self.min_aspect
                    and area / max(1, w * h) >= self.min_fill):
                self.last_match = {"x": x, "y": y, "w": w, "h": h, "area": area,
                                    "aspect": w / max(1, h), "fill": area / max(1, w * h)}
                self._lock = (x, y, w, h)
                self._lock_ts = now
                return True

        # строгая форма не нашлась — проверяем «добивание» по запомненной рамке:
        # бар тает справа налево, остаток красного всегда у ЛЕВОГО края рамки,
        # значит он лежит внутри уже подтверждённого (x, y, w, h)
        if self._lock is not None and now - self._lock_ts <= self.low_hp_lock_timeout_s:
            lx, ly, lw, lh = self._lock
            band = mask[max(0, ly - 2):ly + lh + 2, max(0, lx):lx + lw]
            red_px = int(cv2.countNonZero(band)) if band.size else 0
            if red_px >= self.low_hp_min_px:
                self.last_match = {"x": lx, "y": ly, "w": lw, "h": lh, "area": red_px,
                                    "aspect": lw / max(1, lh), "fill": red_px / max(1, lw * lh),
                                    "low_hp_lock": True}
                self._lock_ts = now
                return True

        self.last_match = None
        return False
