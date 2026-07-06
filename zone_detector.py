"""Детект «мы в убежище» по заголовку панели заданий справа сверху.

В убежище панель заданий всегда озаглавлена «Убежище» (плашка + герб),
в любой другой зоне там название карты/зоны и список целей — визуально
и текстово другое. Раз в убежище хостильных мобов физически не бывает,
это надёжный категорический стоп-сигнал для авто-атаки — не нужно
гадать с порогами по каждой новой декорации сцены (факелы, алтари и т.п.).

Шаблон заголовка (zone_templates/hideout_label.png) вырезан один раз из
реального кадра в убежище. Матчим через cv2.matchTemplate (быстро,
детерминированно, без OCR/ML — тот же принцип, что и hp_ocr.py).
"""
import os

import cv2

_log_name = "autoloot.zone"

TPL_PATH = os.path.join(os.path.dirname(__file__), "zone_templates", "hideout_label.png")


class ZoneDetector:
    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.roi = cfg.get("roi", {"x1": 0.90, "y1": 0.24, "x2": 1.0, "y2": 0.32})
        self.match_min = cfg.get("match_min", 0.85)
        self._template = cv2.imread(TPL_PATH, cv2.IMREAD_GRAYSCALE)
        self.last_score = 0.0

    def in_hideout(self, frame_bgr):
        """True, если в кадре виден заголовок «Убежище» панели заданий."""
        if self._template is None or frame_bgr is None:
            return False

        fh, fw = frame_bgr.shape[:2]
        x1, y1 = int(self.roi["x1"] * fw), int(self.roi["y1"] * fh)
        x2, y2 = int(self.roi["x2"] * fw), int(self.roi["y2"] * fh)
        crop = frame_bgr[y1:y2, x1:x2]

        th, tw = self._template.shape[:2]
        if crop.shape[0] < th or crop.shape[1] < tw:
            return False

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(gray, self._template, cv2.TM_CCOEFF_NORMED)
        self.last_score = float(res.max())
        return self.last_score >= self.match_min
