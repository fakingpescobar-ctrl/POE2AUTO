"""Сохранение улик при потере HP — общий код для bot.py (вживую) и hp_diag.py.

«Потеря HP» = HPDetector вернул ratio=None (низ орба не красный / гейт валидности).
В этот момент сохраняем 4 файла в _debug/hp_events/ и пишем разбор причины в лог:
  <время>_<номер>_<событие>_full.png  — весь кадр (контекст: меню/эффект/карта)
  ..._crop.png  — кроп орба
  ..._mask.png  — маска красного (что детектор считает «красным»)
  ..._vis.png   — оверлей: орб / порог (жёлтый) / найденный уровень (зелёный)

События: lost (HP потерян), ok (восстановлен — для сравнения), manual (ручной снимок).
"""
import logging
import os
from datetime import datetime

import cv2
import numpy as np

from hp_detector import _red_mask

EVT_DIR = os.path.join(os.path.dirname(__file__), "_debug", "hp_events")
log = logging.getLogger("poe2auto.diag")

# не заваливать диск во время долгого «потерянного» состояния
MAX_SHOTS_PER_STREAK = 6      # сколько кадров снять за один эпизод потери
STREAK_SHOT_EVERY = 2.0       # пауза между снимками внутри эпизода (сек)


def classify(crop_bgr, mask, m, foreground):
    """Эвристическая причина потери HP по кропу орба и маске красного."""
    mean_v = float(cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)[:, :, 2].mean())
    red_frac = float((mask > 0).mean())
    reasons = []
    if mean_v < 8:
        reasons.append("чёрный/пустой кадр (захват не отдал картинку?)")
    elif red_frac < 0.01:
        reasons.append("красного почти нет — орб перекрыт эффектом или регион сместился")
    elif mean_v < 40:
        reasons.append("орб тусклый — вероятно меню/затемнение (gate по яркости)")
    if m["ratio"] is None and m.get("fill_v", 0) and red_frac >= 0.01:
        reasons.append(f"красное есть, но отсечено гейтом валидности (fill_v={m.get('fill_v')})")
    if not foreground:
        reasons.append("игра не в фокусе")
    if not reasons:
        reasons.append("причина неочевидна — смотри _vis.png/_mask.png")
    return mean_v, red_frac, "; ".join(reasons)


def dump_event(tag, n, frame, det, m, foreground):
    """Сохранить улику по уже посчитанному measure-результату m (frame и det — те же)."""
    os.makedirs(EVT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S_%f")[:-3]
    base = os.path.join(EVT_DIR, f"{ts}_{n:04d}_{tag}")

    cx, cy, r = m["orb"]
    fh, fw = frame.shape[:2]
    x1, y1 = max(0, cx - r), max(0, cy - r)
    x2, y2 = min(fw, cx + r), min(fh, cy + r)
    crop = frame[y1:y2, x1:x2]
    mask = _red_mask(crop, det.sat_min, det.val_min) if crop.size else np.zeros((1, 1), np.uint8)

    vis = frame.copy()
    cv2.circle(vis, (cx, cy), r, (255, 255, 255), 1)
    if m["thr_y"] is not None:
        cv2.line(vis, (cx - r, m["thr_y"]), (cx + r, m["thr_y"]), (0, 220, 220), 1)
    if m["surface_y"] is not None:
        cv2.line(vis, (cx - r, m["surface_y"]), (cx + r, m["surface_y"]), (0, 255, 0), 2)

    cv2.imwrite(base + "_full.png", frame)
    if crop.size:
        cv2.imwrite(base + "_crop.png", crop)
        cv2.imwrite(base + "_mask.png", mask)
    cv2.imwrite(base + "_vis.png", vis)

    mean_v, red_frac, reason = classify(crop if crop.size else frame, mask, m, foreground)
    log.info("СНИМОК[%s #%d] ratio=%s fill_v=%s trip=%s | mean_v=%.1f red_frac=%.3f fg=%s -> %s",
             tag, n,
             "None" if m["ratio"] is None else f"{m['ratio']*100:.0f}%",
             m.get("fill_v"), m.get("trip_below"), mean_v, red_frac, foreground, reason)
    log.info("       файлы: %s_{full,crop,mask,vis}.png", os.path.basename(base))


def dump_loot(n, frame, det, engine, foreground, center=None):
    """Снимок лут-детекции: что детектор принял за лут (для отладки ложных целей).
    Сохраняет full/mask/vis (точки обведены, радиус подбора — жёлтый круг).
    center — центр персонажа в координатах кадра (для ROI-кропа)."""
    os.makedirs(EVT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S_%f")[:-3]
    base = os.path.join(EVT_DIR, f"{ts}_{n:04d}_loot")

    points, mask = det.detect(frame)
    cx, cy = center if center is not None else engine.center(frame.shape)
    in_r = engine.targets_in_radius(points, frame.shape, center=center)
    in_set = {(p[0], p[1]) for p in in_r}

    vis = frame.copy()
    cv2.circle(vis, (cx, cy), engine.radius, (0, 220, 220), 2)        # радиус подбора
    cv2.drawMarker(vis, (cx, cy), (0, 220, 220), cv2.MARKER_CROSS, 22, 2)
    for (px, py, area) in points:
        col = (0, 0, 255) if (px, py) in in_set else (255, 160, 0)    # красн=в радиусе, син=вне
        cv2.circle(vis, (px, py), 16, col, 2)
        cv2.putText(vis, str(int(area)), (px + 12, py), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)

    cv2.imwrite(base + "_full.png", frame)
    cv2.imwrite(base + "_mask.png", mask)
    cv2.imwrite(base + "_vis.png", vis)

    log.info("ЛУТ-СНИМОК[#%d] точек=%d в_радиусе=%d fg=%s цвета=%s", n, len(points), len(in_r),
             foreground, det.colors)
    for i, (px, py, area) in enumerate(points[:10]):
        log.info("   точка %d: (%d,%d) area=%.0f %s", i, px, py, area,
                 "В РАДИУСЕ" if (px, py) in in_set else "вне")
    log.info("       файлы: %s_{full,mask,vis}.png", os.path.basename(base))


ATK_DIR = os.path.join(os.path.dirname(__file__), "_debug", "attack_events")


def dump_attack_trigger(n, frame, mob_det, foreground):
    """Снимок улики в момент, когда авто-атака НАЖАЛА R1 (передний фронт детекта полосы).
    Сохраняет full/roi_mask/vis — по ним видно, что детектор принял за HP-бар моба."""
    os.makedirs(ATK_DIR, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S_%f")[:-3]
    base = os.path.join(ATK_DIR, f"{ts}_{n:04d}_attack")

    x1, y1, x2, y2 = mob_det.last_roi_px
    vis = frame.copy()
    cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 220, 220), 1)
    m = mob_det.last_match
    if m:
        bx1, by1 = x1 + m["x"], y1 + m["y"]
        bx2, by2 = bx1 + m["w"], by1 + m["h"]
        cv2.rectangle(vis, (bx1, by1), (bx2, by2), (0, 0, 255), 2)

    cv2.imwrite(base + "_full.png", frame)
    if mob_det.last_mask is not None:
        cv2.imwrite(base + "_mask.png", mob_det.last_mask)
    cv2.imwrite(base + "_vis.png", vis)

    if m:
        kind = ("bootstrap" if m.get("bootstrap") else
                 "low_hp_lock" if m.get("low_hp_lock") else "strict")
        log.info("АТАКА-ТРИГГЕР[#%d] bbox=(%d,%d,%d,%d) area=%.0f aspect=%.2f fill=%.2f kind=%s fg=%s",
                  n, m["x"], m["y"], m["w"], m["h"], m["area"], m["aspect"], m["fill"], kind, foreground)
    else:
        log.info("АТАКА-ТРИГГЕР[#%d] (нет match-данных) fg=%s", n, foreground)
    log.info("       файлы: %s_{full,mask,vis}.png", os.path.basename(base))


class HPDiag:
    """Трекер переходов «есть HP ↔ потеряно» с дампом улик. Используют bot.py и hp_diag.py."""

    def __init__(self, enabled=False):
        self.enabled = enabled
        self.n = 0
        self.lost = False
        self.streak_shots = 0
        self.last_shot = 0.0
        self.last_ratio = None
        self.last_v = None

    def toggle(self):
        self.enabled = not self.enabled
        return self.enabled

    def feed(self, frame, det, m, foreground, now):
        """Вызывать каждый кадр с результатом det.measure(frame). Сам решает, снимать ли."""
        ratio = m["ratio"]
        v = m.get("fill_v")
        is_lost = ratio is None

        if not self.enabled:
            self.last_ratio, self.last_v = ratio, v
            return

        if is_lost and not self.lost:
            self.n += 1
            log.info(">>> HP ПОТЕРЯН (был ratio=%s v=%s) — снимаю улику",
                     "None" if self.last_ratio is None else f"{self.last_ratio*100:.0f}%", self.last_v)
            dump_event("lost", self.n, frame, det, m, foreground)
            self.lost = True
            self.streak_shots = 1
            self.last_shot = now
        elif is_lost and self.lost:
            if (self.streak_shots < MAX_SHOTS_PER_STREAK
                    and now - self.last_shot >= STREAK_SHOT_EVERY):
                self.n += 1
                dump_event("lost", self.n, frame, det, m, foreground)
                self.streak_shots += 1
                self.last_shot = now
        elif not is_lost and self.lost:
            self.n += 1
            log.info("<<< HP ВОССТАНОВЛЕН (ratio=%.0f%% v=%s)", ratio * 100, v)
            dump_event("ok", self.n, frame, det, m, foreground)
            self.lost = False
            self.streak_shots = 0

        self.last_ratio, self.last_v = ratio, v

    def manual(self, frame, det, m, foreground):
        """Ручной снимок (по хоткею) — независимо от состояния потери."""
        self.n += 1
        log.info("Ручной снимок диагностики")
        dump_event("manual", self.n, frame, det, m, foreground)
