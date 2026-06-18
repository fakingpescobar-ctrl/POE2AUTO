"""Детекция HP по орбу: метод «уровня жидкости» (fill-line scan) + tripwire-подстраховка.

Орб PoE2 заполняется снизу вверх красным. Вместо подсчёта доли красных пикселей
(шумит от текстуры/эффектов) ищем ГРАНИЦУ красное/тёмное — поверхность жидкости —
и переводим её высоту в HP% с учётом круглой формы орба.

tripwire: дополнительный патч на высоте порога. Если основной скан промахнётся
(орб перекрыт эффектом), патч даёт быстрый бинарный ответ «ниже порога или нет».
"""
import math

import cv2
import numpy as np


def _orb_px(orb, fw, fh):
    """Орб из долей кадра → пиксели (cx, cy, r). Радиус масштабируется по высоте кадра."""
    cx = int(orb["cx"] * fw)
    cy = int(orb["cy"] * fh)
    r = int(orb["r"] * fh)
    return cx, cy, r


def _red_mask(crop_bgr, sat_min, val_min):
    """Маска красного (две зоны HSV — красный оборачивается через 0/179)."""
    hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, sat_min, val_min), (15, 255, 255))
    m2 = cv2.inRange(hsv, (160, sat_min, val_min), (179, 255, 255))
    return cv2.bitwise_or(m1, m2)


def _area_ratio(u):
    """Доля площади круга ниже горизонтали на нормированной высоте u∈[-1,1].
    u=-1 (поверхность у верха) → 1.0 (полный); u=1 (у низа) → 0.0 (пустой)."""
    u = max(-1.0, min(1.0, u))
    return 1.0 - (math.acos(-u) + u * math.sqrt(max(0.0, 1 - u * u))) / math.pi


class HPDetector:
    def __init__(self, cfg):
        self.orb = cfg["orb"]
        self.fill_mode = cfg.get("fill_mode", "area")       # area | linear
        self.sat_min = cfg.get("red_sat_min", 60)
        self.val_min = cfg.get("red_val_min", 25)
        self.row_fill_frac = cfg.get("row_fill_frac", 0.35)
        self.strip_frac = cfg.get("strip_frac", 0.30)
        self.gap_tol_frac = cfg.get("gap_tol_frac", 0.10)
        self.tripwire = cfg.get("tripwire", True)
        self.threshold = cfg.get("threshold", 0.55)
        # мёртвая зона: чтение ниже = орб затемнён/перекрыт (меню), а не реальный HP
        self.min_valid_ratio = cfg.get("min_valid_ratio", 0.10)
        # мин. яркость залитой части (V): тусклый орб в меню отсекаем
        self.valid_val = cfg.get("valid_val", 90)

    def measure(self, frame_bgr):
        """Вернуть dict: ratio (0..1 или None), surface_y, thr_y, trip_below, orb(cx,cy,r)."""
        fh, fw = frame_bgr.shape[:2]
        cx, cy, r = _orb_px(self.orb, fw, fh)
        if r < 4:
            return {"ratio": None, "surface_y": None, "thr_y": None,
                    "trip_below": None, "orb": (cx, cy, r)}

        x1, y1 = max(0, cx - r), max(0, cy - r)
        x2, y2 = min(fw, cx + r), min(fh, cy + r)
        crop = frame_bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return {"ratio": None, "surface_y": None, "thr_y": None,
                    "trip_below": None, "orb": (cx, cy, r)}

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        m1 = cv2.inRange(hsv, (0, self.sat_min, self.val_min), (15, 255, 255))
        m2 = cv2.inRange(hsv, (160, self.sat_min, self.val_min), (179, 255, 255))
        mask = cv2.bitwise_or(m1, m2)
        V = hsv[:, :, 2]
        H, W = mask.shape
        ccx = cx - x1  # центр орба в координатах кропа
        sw = max(2, int(self.strip_frac * r))
        sx1, sx2 = max(0, ccx - sw), min(W, ccx + sw)
        strip = mask[:, sx1:sx2]
        strip_len = max(1, sx2 - sx1)

        # доля красного по строкам
        row_red = (strip > 0).sum(axis=1) / strip_len
        filled = row_red > self.row_fill_frac

        # высота «жидкости» снизу вверх с допуском разрывов
        gap_tol = max(1, int(self.gap_tol_frac * r))
        height = 0
        gap = 0
        for i in range(H - 1, -1, -1):
            if filled[i]:
                height = (H - 1 - i) + 1
                gap = 0
            else:
                gap += 1
                if gap > gap_tol:
                    break

        if height <= 0:
            # низ орба не красный → орб не виден (меню/карта/эффект), а НЕ 0% HP.
            # ratio=None означает «неизвестно» — фляга в этом случае не жмётся.
            ratio = None
            surface_y = None
        else:
            y_s = H - height                      # поверхность в кропе
            surface_y = y1 + y_s
            u = (y_s - r) / float(r)              # нормированная высота поверхности
            ratio = _area_ratio(u) if self.fill_mode == "area" else height / float(2 * r)

        # яркость залитой части: реальный HP светится ярким красным,
        # затемнённый орб в меню — тусклый. Считаем среднюю V красных пикселей полоски.
        fill_v = 0.0
        if height > 0:
            sm = strip > 0
            if sm.any():
                fill_v = float(V[:, sx1:sx2][sm].mean())

        # мёртвая зона снизу (~7% «дно») и проверка яркости —
        # затемнённый/перекрытый орб помечаем как неизвестно (фляга не жмётся)
        if ratio is not None and (ratio < self.min_valid_ratio or fill_v < self.valid_val):
            ratio = None
            surface_y = None

        # tripwire: патч на линейной высоте порога
        thr_y = int((cy + r) - self.threshold * 2 * r)
        trip_below = None
        if self.tripwire:
            ph = max(2, int(0.05 * r))
            ty1, ty2 = max(0, thr_y - ph), min(fh, thr_y + ph)
            tx1, tx2 = max(0, cx - sw), min(fw, cx + sw)
            patch = frame_bgr[ty1:ty2, tx1:tx2]
            if patch.size > 0:
                pm = _red_mask(patch, self.sat_min, self.val_min)
                trip_below = (pm > 0).mean() <= self.row_fill_frac

        return {"ratio": ratio, "surface_y": surface_y, "thr_y": thr_y,
                "trip_below": trip_below, "orb": (cx, cy, r), "fill_v": round(fill_v)}

    def is_low(self, frame_bgr):
        """True если HP достоверно ниже порога. Когда орб не виден (ratio=None) —
        НЕ пьём (иначе ложные фляги в меню/на карте)."""
        m = self.measure(frame_bgr)
        if m["ratio"] is None:
            return False, m
        below_fill = m["ratio"] < self.threshold
        below_trip = bool(m["trip_below"])
        return (below_fill or below_trip), m
