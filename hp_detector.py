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
        # центральная полоса с руной-механикой (доля радиуса) — исключаем из скана
        self.rune_frac = cfg.get("rune_frac", 0.0)
        # нижний обод орба + край панели задач — тёмные, подрезаем снизу (доля радиуса)
        self.bottom_trim_frac = cfg.get("bottom_trim_frac", 0.05)
        # сколько детектор читает при ПОЛНОМ HP (<1.0): верх орба — глянцевый блик,
        # красным не детектится. real_hp = measured / full_ratio. 1.0 = без коррекции.
        self.full_ratio = cfg.get("full_ratio", 1.0)
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
        ccx, ccy = cx - x1, cy - y1  # центр орба в координатах кропа

        # маска круга орба + исключение центральной полосы с руной-механикой:
        # символ по центру не красный и ломал бы скан узкой центральной полоски.
        yy, xx = np.ogrid[:H, :W]
        circle = (xx - ccx) ** 2 + (yy - ccy) ** 2 <= r * r
        rune_half = int(self.rune_frac * r)
        usable = (circle & (np.abs(xx - ccx) > rune_half)) if rune_half > 0 else circle

        redpx = (mask > 0) & usable
        row_usable = usable.sum(axis=1)              # ширина «рабочей» части орба в строке
        row_red = redpx.sum(axis=1)
        # доля красного в строке относительно ширины орба в этой строке (не всего кропа)
        row_fill = np.divide(row_red, np.maximum(1, row_usable), dtype=float)
        # строки со слишком узким сечением круга (самый верх/низ) ненадёжны — пропускаем
        valid_row = row_usable >= max(3, int(0.12 * r))
        filled = (row_fill > self.row_fill_frac) & valid_row

        # высота «жидкости» снизу вверх. Нижний обод орба + край панели задач — тёмные,
        # поэтому пропускаем нижние bottom_trim строк, чтобы они не оборвали скан.
        bottom_trim = int(self.bottom_trim_frac * r)
        gap_tol = max(1, int(self.gap_tol_frac * r))
        height = 0
        gap = 0
        for i in range(H - 1 - bottom_trim, -1, -1):
            if not valid_row[i]:
                continue                              # узкое сечение круга — не считаем разрывом
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

        # яркость залитой части (без руны): реальный HP — яркий красный, меню — тусклый
        fill_v = float(V[redpx].mean()) if redpx.any() else 0.0
        # доля красного в рабочей части орба: реальная просадка под эффектом ещё имеет
        # красную лужу (~0.08), а перекрытый меню/тёмный орб — почти ноль (~0.001).
        # Нужна fallback'у в bot.py, чтобы не лить флягу в меню после низкого HP.
        red_frac = float(redpx.sum() / max(1, int(usable.sum())))

        # гейты валидности: затемнённый/перекрытый орб помечаем как неизвестно (не пьём)
        if ratio is not None and (ratio < self.min_valid_ratio or fill_v < self.valid_val):
            ratio = None
            surface_y = None

        # коррекция под глянцевый верх орба: real_hp = measured / full_ratio
        if ratio is not None and self.full_ratio > 0:
            ratio = min(1.0, ratio / self.full_ratio)

        # tripwire: доля красного на геом. высоте, соответствующей реальному порогу
        thr_y = int((cy + r) - (self.threshold * self.full_ratio) * 2 * r)
        trip_below = None
        if self.tripwire:
            tr = thr_y - y1
            ph = max(1, int(0.05 * r))
            rows = [row_fill[i] for i in range(max(0, tr - ph), min(H, tr + ph + 1))
                    if valid_row[i]]
            if rows:
                trip_below = (sum(rows) / len(rows)) <= self.row_fill_frac

        return {"ratio": ratio, "surface_y": surface_y, "thr_y": thr_y,
                "trip_below": trip_below, "orb": (cx, cy, r), "fill_v": round(fill_v),
                "red_frac": round(red_frac, 4)}

    def is_low(self, frame_bgr):
        """True если HP достоверно ниже порога. Решение — по уровню жидкости (ratio).
        Когда орб не виден (ratio=None) — здесь НЕ решаем (вернём False): подстраховку
        на слепые кадры делает главный цикл с памятью последнего HP (см. bot.py),
        чтобы fallback ПРОДОЛЖАЛ питьё при просадке, но не НАЧИНАЛ его с полного HP."""
        m = self.measure(frame_bgr)
        if m["ratio"] is None:
            return False, m
        return (m["ratio"] < self.threshold), m
