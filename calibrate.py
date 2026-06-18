"""Мастер калибровки маски лута (как calibrate.bat в старом проекте).

Захватывает окно PoE2, показывает кадр с подсветкой целей и отдельное окно маски.
Крутишь трекбары/тыкаешь пипеткой — подбираешь цвет и пороги, жмёшь 's' — сохраняет
в config.yaml (секции vision + loot). Потом этот конфиг читает детектор бота.

Управление:
    ЛКМ           — взять основной цвет-маркер с экрана (пипетка)
    a             — добавить ещё один цвет (под курсором)
    x             — очистить дополнительные цвета
    Shift+ЛКМ     — задать центр персонажа
    i / k / j / l — сдвиг центра (вверх/вниз/влево/вправо)
    d             — сохранить debug-снимок (кадр/маска/подсветка) в _debug/
    s             — сохранить в config.yaml
    q             — выход

Запуск:
    python calibrate.py
"""
import logging
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from config import load_config, save_config
from screen import ScreenCapture
from window import GameWindow
from color_detector import ColorDetector, rgb_to_hsv_bounds

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("poe2auto.calibrate")

WIN = "POE2AUTO Calibrate"
MASK = "mask"
HELP = "POE2AUTO Help"

# Описание каждой настройки (заголовок, текст). Рисуется в окне «Справка».
HELP_ITEMS = [
    ("HueTol — допуск тона",
     "Насколько оттенок пикселя может отличаться от выбранного цвета, "
     "чтобы считаться «тем же». Больше = ловит больше похожих оттенков "
     "(и больше мусора), меньше = строже к цвету."),
    ("SatMin — мин. насыщенность",
     "Порог насыщенности цвета. Пиксели бледнее порога игнорируются. "
     "Поднимай, чтобы отсечь серый/блёклый фон и оставить только сочный цвет лута."),
    ("ValMin — мин. яркость",
     "Порог яркости. Пиксели темнее порога игнорируются. "
     "Поднимай, чтобы убрать тёмный фон и тени; опускай, если лут тусклый."),
    ("MinArea — мин. площадь пятна",
     "Минимальный размер цветного пятна (в пикселях), чтобы считать его целью. "
     "Больше = отсекает мелкие искры/шум, но может пропустить далёкий мелкий лут."),
    ("ClosePx — сшивка разрывов",
     "Склеивает близкие куски одной подписи в одно пятно (морфология «закрытие»). "
     "Больше = соединяет рваный текст, но может слить две соседние подписи в одну."),
    ("Radius — радиус подбора",
     "Радиус круга вокруг персонажа, в котором лут считается достижимым. "
     "Цели вне круга (красные) игнорируются, внутри (зелёные) — берутся."),
]

HELP_KEYS = [
    "ЛКМ — пипетка: взять основной цвет с экрана",
    "a — добавить ещё цвет под курсором   x — очистить доп. цвета",
    "Shift+ЛКМ или i/j/k/l — задать центр персонажа",
    "d — сохранить debug-снимки   s — сохранить config   q — выход",
]


def _load_font(size):
    for path in (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def build_help_image(width=560):
    """Статичная панель-справка с описанием настроек (Cyrillic через Pillow)."""
    f_title = _load_font(22)
    f_name = _load_font(18)
    f_body = _load_font(15)
    pad = 16
    max_w = width - pad * 2

    img = Image.new("RGB", (width, 4000), (24, 24, 30))
    draw = ImageDraw.Draw(img)
    y = pad
    draw.text((pad, y), "Справка по настройкам", font=f_title, fill=(0, 230, 140))
    y += 38
    for name, body in HELP_ITEMS:
        draw.text((pad, y), name, font=f_name, fill=(120, 200, 255))
        y += 26
        for line in _wrap(draw, body, f_body, max_w):
            draw.text((pad + 10, y), line, font=f_body, fill=(220, 220, 220))
            y += 21
        y += 10

    draw.text((pad, y), "Управление", font=f_name, fill=(255, 200, 100))
    y += 28
    for line in HELP_KEYS:
        draw.text((pad + 10, y), line, font=f_body, fill=(200, 200, 200))
        y += 22
    y += pad

    img = img.crop((0, 0, width, y))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def sample_rgb(frame_bgr, x, y):
    """RGB пикселя кадра (с клампом в границы)."""
    h, w = frame_bgr.shape[:2]
    x = max(0, min(w - 1, x))
    y = max(0, min(h - 1, y))
    b, g, r = frame_bgr[y, x]
    return [int(r), int(g), int(b)]


def main():
    cfg = load_config()
    v = cfg["vision"]
    loot = cfg["loot"]

    win = GameWindow(cfg["game"]["window_title"])
    region = win.get_region() if win.find() else GameWindow.primary_region()
    cap = ScreenCapture(cfg["capture"]["backend"])
    log.info("Регион=%s backend=%s", region, cap.backend)

    colors = [list(v["marker_rgb"])] + [list(c) for c in v.get("extra_colors", []) or []]
    shared = {"frame": None, "colors": colors,
              "center_off": list(loot.get("center_offset_xy", [0, 0])),
              "last_xy": (0, 0)}

    def on_mouse(event, x, y, flags, _param):
        if event == cv2.EVENT_MOUSEMOVE:
            shared["last_xy"] = (x, y)
            return
        frame = shared["frame"]
        if frame is None or event != cv2.EVENT_LBUTTONDOWN:
            return
        if flags & cv2.EVENT_FLAG_SHIFTKEY:
            h, w = frame.shape[:2]
            shared["center_off"] = [x - w // 2, y - h // 2]
            log.info("Центр → offset %s", shared["center_off"])
        else:
            shared["colors"][0] = sample_rgb(frame, x, y)
            log.info("Основной цвет → RGB %s", shared["colors"][0])

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.namedWindow(MASK, cv2.WINDOW_NORMAL)
    cv2.namedWindow(HELP, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WIN, on_mouse)
    help_img = build_help_image()
    cv2.resizeWindow(HELP, help_img.shape[1], min(help_img.shape[0], 900))
    noop = lambda _v: None
    cv2.createTrackbar("HueTol", WIN, int(v.get("hue_tolerance", 8)), 30, noop)
    cv2.createTrackbar("SatMin", WIN, int(v.get("sat_min", 120)), 255, noop)
    cv2.createTrackbar("ValMin", WIN, int(v.get("val_min", 120)), 255, noop)
    cv2.createTrackbar("MinArea", WIN, int(v.get("min_blob_area", 120)), 2000, noop)
    cv2.createTrackbar("ClosePx", WIN, int(v.get("close_px", 3)), 25, noop)
    cv2.createTrackbar("Radius", WIN, int(loot.get("pickup_radius_px", 250)), 1500, noop)

    log.info("ЛКМ=цвет  a=+цвет  x=очистить  Shift+ЛКМ/ijkl=центр  d=debug  s=сохранить  q=выход")

    while True:
        frame = cap.grab(region)
        if frame is None:
            if (cv2.waitKey(20) & 0xFF) == ord("q"):
                break
            continue
        shared["frame"] = frame

        try:
            if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                break
        except cv2.error:
            break

        hue_tol = cv2.getTrackbarPos("HueTol", WIN)
        sat_min = cv2.getTrackbarPos("SatMin", WIN)
        val_min = cv2.getTrackbarPos("ValMin", WIN)
        min_area = cv2.getTrackbarPos("MinArea", WIN)
        close_px = cv2.getTrackbarPos("ClosePx", WIN)
        radius = cv2.getTrackbarPos("Radius", WIN)

        det = ColorDetector(shared["colors"], hue_tol, sat_min, val_min, min_area, close_px)
        points, mask = det.detect(frame)

        h, w = frame.shape[:2]
        cx = w // 2 + shared["center_off"][0]
        cy = h // 2 + shared["center_off"][1]

        vis = frame.copy()
        cv2.circle(vis, (cx, cy), radius, (255, 255, 255), 1)
        cv2.drawMarker(vis, (cx, cy), (255, 255, 255), cv2.MARKER_CROSS, 16, 1)
        n_in = 0
        for (px, py, _a) in points:
            inside = (px - cx) ** 2 + (py - cy) ** 2 <= radius * radius
            n_in += inside
            cv2.circle(vis, (px, py), 8, (0, 255, 0) if inside else (0, 0, 255), 2)

        lo, hi = rgb_to_hsv_bounds(shared["colors"][0], hue_tol, sat_min, val_min)[0]
        lines = [
            f"colors {shared['colors']}",
            f"primary HSV {lo.tolist()}..{hi.tolist()}",
            f"targets {len(points)}  in-radius {n_in}  radius {radius}",
            "LMB=color  a=+color  x=clear  Shift+LMB/ijkl=center  d=debug  s=save  q=quit",
        ]
        for i, t in enumerate(lines):
            y = 22 + i * 22
            cv2.putText(vis, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(vis, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)

        cv2.imshow(WIN, vis)
        cv2.imshow(MASK, mask)
        cv2.imshow(HELP, help_img)
        k = cv2.waitKey(1) & 0xFF

        if k == ord("q"):
            break
        elif k == ord("s"):
            cfg["vision"]["marker_rgb"] = list(shared["colors"][0])
            cfg["vision"]["extra_colors"] = [list(c) for c in shared["colors"][1:]]
            cfg["vision"]["hue_tolerance"] = int(hue_tol)
            cfg["vision"]["sat_min"] = int(sat_min)
            cfg["vision"]["val_min"] = int(val_min)
            cfg["vision"]["min_blob_area"] = int(min_area)
            cfg["vision"]["close_px"] = int(close_px)
            cfg["loot"]["pickup_radius_px"] = int(radius)
            cfg["loot"]["center_offset_xy"] = [int(shared["center_off"][0]), int(shared["center_off"][1])]
            path = save_config(cfg)
            log.info("Сохранено в %s", path)
        elif k == ord("a"):
            lx, ly = shared["last_xy"]
            c = sample_rgb(frame, lx, ly)
            shared["colors"].append(c)
            log.info("Добавлен цвет RGB %s (всего %d)", c, len(shared["colors"]))
        elif k == ord("x"):
            shared["colors"] = [shared["colors"][0]]
            log.info("Доп. цвета очищены")
        elif k == ord("d"):
            os.makedirs("_debug", exist_ok=True)
            cv2.imwrite("_debug/frame.png", frame)
            cv2.imwrite("_debug/vis.png", vis)
            cv2.imwrite("_debug/mask.png", mask)
            log.info("Debug сохранён в _debug/ (целей: %d)", len(points))
        elif k == ord("i"):
            shared["center_off"][1] -= 5
        elif k == ord("k"):
            shared["center_off"][1] += 5
        elif k == ord("j"):
            shared["center_off"][0] -= 5
        elif k == ord("l"):
            shared["center_off"][0] += 5

    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
