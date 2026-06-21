"""Чтение HP из цифр HUD PoE2 (current/max) — надёжнее уровня жидкости орба.

HUD показывает «Здоровье 1 640/1 640» белым по тёмному. Шрифт и позиция
фиксированы, поэтому вместо тяжёлого OCR используем template matching:
  ROI → бинаризация (белый текст) → сегментация глифов (connected components)
  → классификация каждого глифа по шаблону (0-9 и '/') → парс current/max.

Шаблоны (hp_digits/*.png) строит calibrate_hp_digits.py один раз. Если кадр
не читается (касающиеся цифры/перекрытие/неизвестный глиф) — read() вернёт
None, и вызывающий код откатывается на детекцию по орбу.

Метод детерминирован и быстр (<1 мс на кадр): никакого ML/GPU.
"""
import os

import cv2
import numpy as np

TPL_DIR = os.path.join(os.path.dirname(__file__), "hp_digits")
GLYPH_H, GLYPH_W = 28, 18           # нормированный размер глифа для сравнения
LABELS = [str(d) for d in range(10)] + ["slash"]   # имена файлов шаблонов


def roi_px(roi, fw, fh):
    """ROI из долей кадра → пиксели (x1, y1, x2, y2)."""
    return (int(roi["x1"] * fw), int(roi["y1"] * fh),
            int(roi["x2"] * fw), int(roi["y2"] * fh))


def binarize(gray, thr):
    """Серый ROI → бинарь: белые цифры = 255, тёмный фон = 0."""
    _, b = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
    return b


def norm_glyph(glyph_bin):
    """Глиф любого размера → фиксированный GLYPH_H×GLYPH_W (для сравнения шаблонов)."""
    return cv2.resize(glyph_bin, (GLYPH_W, GLYPH_H), interpolation=cv2.INTER_NEAREST)


def segment(binary, min_h=10, max_h_frac=1.0, min_w=2):
    """Бинарь ROI → список (x, glyph_bin), глифы слева направо.

    Каждая связная белая компонента = глиф (цифра или '/'). Слишком мелкие
    (шум) и слишком высокие (рамки/артефакты) отсекаем. Возвращаем нормированные
    кропы глифов, отсортированные по X.
    """
    h = binary.shape[0]
    n, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    glyphs = []
    for i in range(1, n):                       # 0 — фон
        x, y, w, hh, area = stats[i]
        if hh < min_h or hh > h * max_h_frac or w < min_w:
            continue
        if area < 0.10 * w * hh:                # почти пустой бокс — шум
            continue
        crop = binary[y:y + hh, x:x + w]
        glyphs.append((x, norm_glyph(crop)))
    glyphs.sort(key=lambda g: g[0])
    return glyphs


def load_templates(tpl_dir=TPL_DIR):
    """Загрузить шаблоны глифов. Возвращает dict label('0'..'9','/') → norm-картинка."""
    tpls = {}
    for name in LABELS:
        path = os.path.join(tpl_dir, name + ".png")
        if not os.path.exists(path):
            continue
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        if img.shape != (GLYPH_H, GLYPH_W):
            img = norm_glyph(img)
        ch = "/" if name == "slash" else name
        tpls[ch] = img
    return tpls


def classify(glyph, tpls, match_min):
    """Глиф → символ ('0'..'9','/') или None, если ни один шаблон не похож."""
    best_ch, best_score = None, -1.0
    for ch, t in tpls.items():
        score = float(cv2.matchTemplate(glyph, t, cv2.TM_CCOEFF_NORMED)[0, 0])
        if score > best_score:
            best_ch, best_score = ch, score
    return best_ch if best_score >= match_min else None


class HPNumReader:
    """Читает HP из цифр HUD. ratio = current/max или None, если кадр нечитаем."""

    def __init__(self, cfg):
        self.roi = cfg["roi"]
        self.bin_thr = cfg.get("bin_thr", 130)
        self.match_min = cfg.get("match_min", 0.55)
        self.min_glyph_h = cfg.get("min_glyph_h", 10)
        # ожидаемый max HP — только для отсева мусора. Подстраивается сам: на полном
        # HP (current==max) запоминаем истинный максимум, поэтому смена шмота (другой
        # max) не ломает чтение — планка переезжает автоматически. 0 = не проверять.
        self.learned_max = cfg.get("max_hp", 0) or 0
        self.tpls = load_templates()

    @property
    def ready(self):
        """Готов ли: есть ВСЕ шаблоны (0-9 и '/'). Иначе номера читаться не будут
        надёжно — бот остаётся на детекции по орбу."""
        return "/" in self.tpls and sum(c.isdigit() for c in self.tpls) == 10

    def read_string_crop(self, roi_bgr):
        """Готовый кроп ROI → строка распознанных символов."""
        if roi_bgr.size == 0:
            return ""
        gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
        binary = binarize(gray, self.bin_thr)
        out = []
        for _x, g in segment(binary, self.min_glyph_h):
            ch = classify(g, self.tpls, self.match_min)
            out.append(ch if ch is not None else "?")
        return "".join(out)

    def read_string(self, frame_bgr):
        """Полный кадр → строка распознанных символов (ROI берётся по долям)."""
        fh, fw = frame_bgr.shape[:2]
        x1, y1, x2, y2 = roi_px(self.roi, fw, fh)
        return self.read_string_crop(frame_bgr[y1:y2, x1:x2])

    def _parse(self, s):
        if "?" in s or "/" not in s:
            return None
        cur_s, _, max_s = s.partition("/")
        if not cur_s or not max_s or not cur_s.isdigit() or not max_s.isdigit():
            return None
        cur, mx = int(cur_s), int(max_s)
        if mx <= 0 or cur > mx:
            return None
        # на полном HP max достоверен → запоминаем (адаптация к смене экипировки)
        if cur == mx:
            self.learned_max = mx
        # отсев структурного мусора (лишние/потерянные цифры дают абсурдный max).
        # Широкая планка 0.2×–4× от запомненного max не мешает смене билда.
        if self.learned_max and not (0.2 * self.learned_max <= mx <= 4 * self.learned_max):
            return None
        return cur, mx, cur / mx

    def read(self, frame_bgr):
        """Полный кадр → (current, max, ratio) или None (кадр нечитаем → откат на орб)."""
        return self._parse(self.read_string(frame_bgr))

    def read_crop(self, roi_bgr):
        """Готовый кроп ROI → (current, max, ratio) или None."""
        return self._parse(self.read_string_crop(roi_bgr))
