"""Калибровка HP-орба для автофляски (fill-line + tripwire).

Тыкаешь ЛКМ в центр орба HP, трекбаром подгоняешь радиус, чтобы белый круг обвёл орб.
Зелёная линия — найденный уровень HP, жёлтая — порог фляги. Цифра сверху — текущий HP%.
'd' — debug-снимок, 's' — сохранить в config.yaml (включает автофляску), 'q' — выход.

Запуск:
    python calibrate_hp.py
"""
import logging
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from config import load_config, save_config
from screen import ScreenCapture
from window import GameWindow
from hp_detector import HPDetector

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("poe2auto.calibrate_hp")

WIN = "POE2AUTO HP Calibrate"
MASK = "hp mask"
HELP = "POE2AUTO HP Help"

HELP_ITEMS = [
    ("ЛКМ — центр орба", "Кликни в середину орба HP. Белый круг покажет область детекции."),
    ("Radius — радиус орба", "Подгони, чтобы белый круг точно обвёл орб HP по краям."),
    ("Threshold — порог фляги", "HP в %, ниже которого бот пьёт флягу. Жёлтая линия = этот уровень."),
    ("RedSat / RedVal", "Пороги «красного». Поднимай, если в маску лезет тёмный фон; "
                        "опускай, если орб тусклый и плохо ловится."),
    ("RowFill — порог строки", "Какая доля строки должна быть красной, чтобы считать её залитой. "
                               "Больше = строже к поверхности жидкости."),
    ("Strip — ширина скана", "Ширина центральной полоски сканирования (доля радиуса)."),
]
HELP_KEYS = [
    "Зелёная линия = найденный уровень HP   Жёлтая = порог",
    "Цифра сверху = текущий HP%",
    "d — debug-снимок   s — сохранить (включит автофляску)   q — выход",
]


def _font(size):
    for p in (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_help_image(width=560):
    f_t, f_n, f_b = _font(22), _font(18), _font(15)
    pad, max_w = 16, width - 32
    img = Image.new("RGB", (width, 4000), (24, 24, 30))
    d = ImageDraw.Draw(img)
    y = pad
    d.text((pad, y), "Справка по калибровке HP", font=f_t, fill=(0, 230, 140)); y += 38
    for name, body in HELP_ITEMS:
        d.text((pad, y), name, font=f_n, fill=(120, 200, 255)); y += 26
        for ln in _wrap(d, body, f_b, max_w):
            d.text((pad + 10, y), ln, font=f_b, fill=(220, 220, 220)); y += 21
        y += 10
    d.text((pad, y), "Подсказки", font=f_n, fill=(255, 200, 100)); y += 28
    for ln in HELP_KEYS:
        d.text((pad + 10, y), ln, font=f_b, fill=(200, 200, 200)); y += 22
    y += pad
    img = img.crop((0, 0, width, y))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def main():
    cfg = load_config()
    hp = cfg["hp"]

    win = GameWindow(cfg["game"]["window_title"])
    region = win.get_region() if win.find() else GameWindow.primary_region()
    cap = ScreenCapture(cfg["capture"]["backend"])
    log.info("Регион=%s backend=%s", region, cap.backend)

    state = {"frame": None, "cx_frac": hp["orb"]["cx"], "cy_frac": hp["orb"]["cy"]}

    def on_mouse(event, x, y, flags, _p):
        if event == cv2.EVENT_LBUTTONDOWN and state["frame"] is not None:
            h, w = state["frame"].shape[:2]
            state["cx_frac"] = x / w
            state["cy_frac"] = y / h
            log.info("Центр орба → (%.4f, %.4f)", state["cx_frac"], state["cy_frac"])

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.namedWindow(MASK, cv2.WINDOW_NORMAL)
    cv2.namedWindow(HELP, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WIN, on_mouse)
    help_img = build_help_image()
    cv2.resizeWindow(HELP, help_img.shape[1], min(help_img.shape[0], 900))

    noop = lambda _v: None
    # радиус хранится как доля высоты кадра → в трекбаре в пикселях по текущему кадру
    fh0 = region["height"]
    cv2.createTrackbar("Radius", WIN, int(hp["orb"]["r"] * fh0), 400, noop)
    cv2.createTrackbar("Threshold", WIN, int(hp["threshold"] * 100), 100, noop)
    cv2.createTrackbar("RedSat", WIN, int(hp["red_sat_min"]), 255, noop)
    cv2.createTrackbar("RedVal", WIN, int(hp["red_val_min"]), 255, noop)
    cv2.createTrackbar("RowFill", WIN, int(hp["row_fill_frac"] * 100), 100, noop)
    cv2.createTrackbar("Strip", WIN, int(hp["strip_frac"] * 100), 60, noop)

    log.info("ЛКМ=центр орба, трекбары=радиус/порог/цвет, s=сохранить, q=выход")

    while True:
        frame = cap.grab(region)
        if frame is None:
            if (cv2.waitKey(20) & 0xFF) == ord("q"):
                break
            continue
        state["frame"] = frame
        fh, fw = frame.shape[:2]

        try:
            if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                break
        except cv2.error:
            break

        radius_px = max(4, cv2.getTrackbarPos("Radius", WIN))
        threshold = cv2.getTrackbarPos("Threshold", WIN) / 100.0
        red_sat = cv2.getTrackbarPos("RedSat", WIN)
        red_val = cv2.getTrackbarPos("RedVal", WIN)
        row_fill = max(1, cv2.getTrackbarPos("RowFill", WIN)) / 100.0
        strip = max(5, cv2.getTrackbarPos("Strip", WIN)) / 100.0

        hp_cfg = dict(hp)
        hp_cfg["orb"] = {"cx": state["cx_frac"], "cy": state["cy_frac"], "r": radius_px / fh}
        hp_cfg.update(threshold=threshold, red_sat_min=red_sat, red_val_min=red_val,
                      row_fill_frac=row_fill, strip_frac=strip)
        det = HPDetector(hp_cfg)
        m = det.measure(frame)
        cx, cy, r = m["orb"]

        vis = frame.copy()
        cv2.circle(vis, (cx, cy), r, (255, 255, 255), 1)
        cv2.drawMarker(vis, (cx, cy), (255, 255, 255), cv2.MARKER_CROSS, 12, 1)
        if m["thr_y"] is not None:
            cv2.line(vis, (cx - r, m["thr_y"]), (cx + r, m["thr_y"]), (0, 220, 220), 1)
        if m["surface_y"] is not None:
            cv2.line(vis, (cx - r, m["surface_y"]), (cx + r, m["surface_y"]), (0, 255, 0), 2)

        ratio = m["ratio"]
        pct = "n/a" if ratio is None else f"{ratio * 100:.0f}%"
        trip = m["trip_below"]
        lines = [
            f"HP {pct}   threshold {threshold*100:.0f}%   trip_below={trip}",
            f"orb cx={state['cx_frac']:.4f} cy={state['cy_frac']:.4f} r={radius_px}px",
            "LMB=center  trackbars=radius/threshold/color  d=debug  s=save  q=quit",
        ]
        for i, t in enumerate(lines):
            yy = 24 + i * 24
            cv2.putText(vis, t, (10, yy), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(vis, t, (10, yy), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1, cv2.LINE_AA)

        # маска орба для контроля
        x1, y1 = max(0, cx - r), max(0, cy - r)
        x2, y2 = min(fw, cx + r), min(fh, cy + r)
        crop = frame[y1:y2, x1:x2]
        if crop.size > 0:
            from hp_detector import _red_mask
            cv2.imshow(MASK, _red_mask(crop, red_sat, red_val))

        cv2.imshow(WIN, vis)
        cv2.imshow(HELP, help_img)
        k = cv2.waitKey(1) & 0xFF

        if k == ord("q"):
            break
        elif k == ord("s"):
            cfg["hp"]["enabled"] = True
            cfg["hp"]["orb"] = {"cx": round(state["cx_frac"], 4),
                                "cy": round(state["cy_frac"], 4),
                                "r": round(radius_px / fh, 4)}
            cfg["hp"]["threshold"] = round(threshold, 2)
            cfg["hp"]["red_sat_min"] = int(red_sat)
            cfg["hp"]["red_val_min"] = int(red_val)
            cfg["hp"]["row_fill_frac"] = round(row_fill, 2)
            cfg["hp"]["strip_frac"] = round(strip, 2)
            path = save_config(cfg)
            log.info("Сохранено в %s (автофляска включена)", path)
        elif k == ord("d"):
            os.makedirs("_debug", exist_ok=True)
            cv2.imwrite("_debug/hp_frame.png", frame)
            cv2.imwrite("_debug/hp_vis.png", vis)
            log.info("Debug сохранён в _debug/")

    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
