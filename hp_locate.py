"""Авто-локализация HUD здоровья (строка «current/max» и орб) на старте бота.

Зачем: ROI цифр и геометрия орба в config.yaml заданы в долях кадра под КОНКРЕТНОЕ
разрешение/UI Scale. Сменишь разрешение (или запустит друг на своём мониторе) — доли
не попадут на HUD. Шаблоны цифр при этом менять не нужно: `norm_glyph` нормирует
каждый глиф, поэтому шрифт любого размера читается. Плывёт только ПОЛОЖЕНИЕ.

Поэтому здесь находим строку здоровья сами: бинаризуем белый текст в нижне-левой
зоне, группируем глифы в строки и берём ту, где есть подстрока, читаемая как валидное
`N/N` (тем же сплиттером слипшихся цифр, что и основной OCR). Орб не ищем вслепую
детектором кругов (пустой орб тёмный, ловится плохо) — выводим его геометрию от тугой
рамки найденного текста: орб и цифры — единый блок HUD, их взаимное смещение в единицах
ВЫСОТЫ текста постоянно при любом UI Scale.
"""
import re

import cv2
import numpy as np

from hp_ocr import binarize, norm_glyph

# Смещение орба от тугой рамки строки цифр, в единицах высоты текста (UI-инвариант).
# Откалибровано один раз скриптом calibrate_hp_layout.py от рабочего config.yaml.
# (orb_cx - text_left)/text_h, (orb_cy - text_top)/text_h, orb_r/text_h.
ORB_DX = -0.134
ORB_DY = 8.284
ORB_R = 4.051


def _components(binary, min_h, max_h, min_w=1):
    """Связные белые компоненты (x, y, w, h), отсев шума по высоте/заполненности."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if h < min_h or h > max_h or w < min_w:
            continue
        if area < 0.08 * w * h:                 # почти пустой бокс — шум
            continue
        out.append((int(x), int(y), int(w), int(h)))
    return out


def _group_lines(comps, y_tol):
    """Компоненты → строки по близости вертикального центра."""
    lines = []
    for c in sorted(comps, key=lambda c: c[1] + c[3] / 2):
        cy = c[1] + c[3] / 2
        for ln in lines:
            if abs(ln["cy"] - cy) <= y_tol:
                ln["items"].append(c)
                ln["cy"] = np.mean([it[1] + it[3] / 2 for it in ln["items"]])
                break
        else:
            lines.append({"cy": cy, "items": [c]})
    return lines


def find_digits_roi(frame_bgr, reader, region=(0.0, 0.55, 0.42, 0.99),
                    min_h_frac=0.010, max_h_frac=0.060, pad_frac=0.30):
    """Найти ROI строки здоровья. Возвращает dict(roi, text_bbox, cur, max) или None.

    region — нижне-левая зона поиска (доли кадра), исключает ману справа.
    """
    fh, fw = frame_bgr.shape[:2]
    rx1, ry1 = int(region[0] * fw), int(region[1] * fh)
    rx2, ry2 = int(region[2] * fw), int(region[3] * fh)
    sub = frame_bgr[ry1:ry2, rx1:rx2]
    if sub.size == 0:
        return None
    binary = binarize(cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY), reader.bin_thr)
    min_h, max_h = max(6, int(min_h_frac * fh)), int(max_h_frac * fh)
    comps = _components(binary, min_h, max_h)
    if not comps:
        return None
    med_h = float(np.median([c[3] for c in comps]))

    # читаем каждую строку; «ресурсная» = есть '/' и ≥2 цифр (жизнь/энергощит/дух)
    resource = []
    for ln in _group_lines(comps, y_tol=0.5 * med_h):
        items = sorted(ln["items"], key=lambda c: c[0])
        chars, char_comp, boxes = [], [], []
        for idx, (x, y, w, h) in enumerate(items):
            frag = reader._read_glyph(binary[y:y + h, x:x + w])  # 1+ символов (со сплитом)
            boxes.append((x + rx1, y + ry1, x + w + rx1, y + h + ry1))
            for ch in frag:
                chars.append(ch)
                char_comp.append(idx)
        s = "".join(chars)
        if "/" in s and sum(c.isdigit() for c in s) >= 2:
            top_y = min(b[1] for b in boxes)
            resource.append((top_y, s, char_comp, boxes))
    if not resource:
        return None

    # в нижне-левом углу PoE2 ресурсы стоят стопкой (жизнь / энергощит / дух); жизнь —
    # ВСЕГДА верхняя. Берём только верхнюю ресурсную строку и НЕ подставляем нижние
    # (ES/дух) как HP. Если она в этом кадре не читается чисто — None (переищем позже).
    resource.sort(key=lambda t: t[0])
    _, s, char_comp, boxes = resource[0]
    best = None
    for m in re.finditer(r"\d+/\d+", s):
        cur, mx = (int(v) for v in m.group().split("/"))
        if mx < 10 or cur > mx:                    # неправдоподобно — не HP
            continue
        used = sorted(set(char_comp[m.start():m.end()]))
        best = {"cur": cur, "max": mx, "text_bbox": (
            min(boxes[j][0] for j in used), min(boxes[j][1] for j in used),
            max(boxes[j][2] for j in used), max(boxes[j][3] for j in used))}
        break

    if best is None:
        return None
    bx1, by1, bx2, by2 = best["text_bbox"]
    pad = int(pad_frac * (by2 - by1))
    px1, py1 = max(0, bx1 - pad), max(0, by1 - pad)
    px2, py2 = min(fw, bx2 + pad), min(fh, by2 + pad)
    # самопроверка: ROI должен читаться основным ридером РОВНО как cur/max (без '?',
    # без лишних глифов). Отсекает огрызки вроде «1/13» из подпорченного кадра — тогда
    # вернём None и попробуем на следующем кадре (периодический переиск).
    if reader.read_string_crop(frame_bgr[py1:py2, px1:px2]) != f"{best['cur']}/{best['max']}":
        return None
    best["roi"] = {"x1": px1 / fw, "y1": py1 / fh, "x2": px2 / fw, "y2": py2 / fh}
    return best


def derive_orb(text_bbox, fw, fh):
    """Геометрия орба (доли кадра) от тугой рамки строки цифр по постоянным коэф-там."""
    bx1, by1, bx2, by2 = text_bbox
    th = by2 - by1
    return {"cx": (bx1 + ORB_DX * th) / fw,
            "cy": (by1 + ORB_DY * th) / fh,
            "r": (ORB_R * th) / fh}
