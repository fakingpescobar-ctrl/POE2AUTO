"""POE2AUTO — шаг 3: авто-подбор лута через виртуальный геймпад.

Что делает:
  1. Поднимает виртуальный Xbox 360 + мост DualSense (ты управляешь персонажем как обычно).
  2. Захватывает экран, детектит лут по маске из config.yaml.
  3. Когда лут в радиусе и авто-режим включён (F8) — жмёт A (Cross) с кулдауном.

Подбор не «целится» — на контроллере это просто нажатие кнопки. Двигаешь персонажа ты,
бот добивает pickup, пока лут в радиусе.

Хоткеи: F8 — вкл/выкл авто-подбор, F12 — выход.

Запуск (нужны права администратора для ViGEmBus):
    python bot.py
"""
import logging
import os
import threading
import time

from pynput import keyboard

from config import load_config
from screen import ScreenCapture
from window import GameWindow
from color_detector import ColorDetector
from loot_engine import LootEngine
from hp_detector import HPDetector
from diag_dump import HPDiag
from virtual_xbox import VirtualXbox360, XUSB
from dualsense_bridge import DualSenseBridge

LOG_PATH = os.path.join(os.path.dirname(__file__), "bot.log")

_fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
_root = logging.getLogger()
_root.setLevel(logging.INFO)
# консоль
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_root.addHandler(_sh)
# файл (mode='w' — свежий лог на каждый запуск, для live-наблюдения)
_fh = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
_fh.setFormatter(_fmt)
_root.addHandler(_fh)

log = logging.getLogger("poe2auto.bot")


class Toggle:
    """Потокобезопасный флаг авто-режима."""
    def __init__(self):
        self._on = False
        self._lock = threading.Lock()

    def flip(self):
        with self._lock:
            self._on = not self._on
            return self._on

    @property
    def on(self):
        with self._lock:
            return self._on


def build_detector(cfg):
    v = cfg["vision"]
    colors = [list(v["marker_rgb"])] + [list(c) for c in v.get("extra_colors", []) or []]
    return ColorDetector(
        colors,
        hue_tol=v.get("hue_tolerance", 8),
        sat_min=v.get("sat_min", 120),
        val_min=v.get("val_min", 120),
        min_blob_area=v.get("min_blob_area", 120),
        close_px=v.get("close_px", 3),
    )


def main():
    cfg = load_config()
    loot = cfg["loot"]

    # --- геймпад: виртуальный Xbox + мост DualSense ---
    vx = VirtualXbox360()
    if not vx.start():
        log.error("Не удалось создать виртуальный Xbox 360 (ViGEmBus/vgamepad?)")
        return 1
    bridge = DualSenseBridge(vx)
    if not bridge.start():
        log.warning("DualSense не найден — управление персонажем с него не пойдёт")

    pickup_mask = XUSB.get(loot.get("pickup_button", "a"), XUSB["a"])
    hold_ms = loot.get("pickup_hold_ms", 80)

    # --- захват + детектор ---
    win = GameWindow(cfg["game"]["window_title"])
    if win.find() and win.get_region():
        region = win.get_region()
        log.info("Окно PoE2 найдено: %s", region)
    else:
        region = GameWindow.primary_region()
        log.warning("Окно PoE2 не найдено — захват всего монитора %s. "
                    "Гейт по фокусу отключён.", region)
    cap = ScreenCapture(cfg["capture"]["backend"])
    det = build_detector(cfg)
    engine = LootEngine(loot["pickup_radius_px"], loot["center_offset_xy"], loot["cooldown_ms"])
    log.info("Детектор: цвета=%s радиус=%d кулдаун=%dмс",
             det.colors, engine.radius, loot["cooldown_ms"])

    # --- автофляска HP ---
    hp_cfg = cfg["hp"]
    hp_det = HPDetector(hp_cfg) if hp_cfg.get("enabled") else None
    hp_cooldown = hp_cfg.get("cooldown_ms", 3000) / 1000.0
    flask_hold = hp_cfg.get("flask_hold_ms", 120)
    hp_threshold = hp_cfg.get("threshold", 0.55)
    hp_confirm = hp_cfg.get("confirm_frames", 4)
    flask_right = hp_cfg.get("flask_button") == "right_trigger"
    # быстрый захват: когда F8 выкл, грабим только зону орба (а не весь экран) → высокий fps
    hp_det_crop = None
    orb_region = None
    if hp_det:
        fw, fh = region["width"], region["height"]
        ocx = region["left"] + hp_cfg["orb"]["cx"] * fw
        ocy = region["top"] + hp_cfg["orb"]["cy"] * fh
        orp = hp_cfg["orb"]["r"] * fh
        half = orp * 1.25  # запас вокруг орба
        rl, rt = region["left"], region["top"]
        ox1 = max(rl, ocx - half)
        oy1 = max(rt, ocy - half)
        ox2 = min(rl + fw, ocx + half)
        oy2 = min(rt + fh, ocy + half)
        sw, sh = ox2 - ox1, oy2 - oy1
        orb_region = {"left": int(ox1), "top": int(oy1), "width": int(sw), "height": int(sh)}
        crop_cfg = dict(hp_cfg)
        crop_cfg["orb"] = {"cx": (ocx - ox1) / sw, "cy": (ocy - oy1) / sh, "r": orp / sh}
        hp_det_crop = HPDetector(crop_cfg)

    if hp_det:
        log.info("Автофляска ВКЛ: порог=%.0f%% кулдаун=%dмс подтв=%d кадра кнопка=%s | быстрый захват орба %s",
                 hp_threshold * 100, hp_cfg.get("cooldown_ms", 3000), hp_confirm,
                 "R2" if flask_right else "L2", orb_region)
    else:
        log.info("Автофляска выкл (запусти calibrate_hp.bat)")

    # --- хоткеи ---
    auto = Toggle()
    diag = HPDiag()  # диагностика потери HP (F7) — пишет улики в _debug/hp_events
    manual_shot = threading.Event()
    stop = threading.Event()
    k_toggle = cfg["hotkeys"].get("toggle", "f8")
    k_quit = cfg["hotkeys"].get("quit", "f12")
    k_diag = cfg["hotkeys"].get("diag", "f7")
    k_shot = cfg["hotkeys"].get("snapshot", "f9")

    def on_press(key):
        name = getattr(key, "name", None)
        if name == k_quit:
            log.info("Выход (F12)")
            stop.set()
            return False
        if name == k_toggle:
            state = auto.flip()
            log.info("Авто-подбор: %s", "ВКЛ" if state else "выкл")
        if name == k_diag:
            state = diag.toggle()
            log.info("Диагностика HP: %s (улики → _debug/hp_events)",
                     "ВКЛ — грабим полный кадр" if state else "выкл")
        if name == k_shot:
            manual_shot.set()

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    log.info("Готово. F8 — авто-подбор, F7 — диагностика HP, F9 — ручной снимок, F12 — выход.")

    # --- главный цикл ---
    last_status = 0.0
    last_flask = 0.0
    presses = 0
    flasks = 0
    hp_pct = None
    hp_v = None
    low_streak = 0
    iters = 0
    try:
        while not stop.is_set():
            foreground = win.is_foreground() if win.hwnd else True

            # F8 включён → грабим весь экран (нужен для детекции лута).
            # F8 выключен → грабим только зону орба → высокий fps, мгновенная фляга.
            # Диагностика (F7) → принудительно полный кадр, чтобы улика содержала весь экран.
            if auto.on or orb_region is None or diag.enabled:
                frame = cap.grab(region)
                hp_d, hp_frame = hp_det, frame
            else:
                frame = cap.grab(orb_region)
                hp_d, hp_frame = hp_det_crop, frame
            if frame is None:
                time.sleep(0.02)
                continue

            # детекция лута — тяжёлая (полный кадр), только при включённом авто-подборе
            if auto.on:
                points, _mask = det.detect(frame)
                in_radius = engine.targets_in_radius(points, frame.shape)
                if foreground and in_radius and engine.ready():
                    vx.tap(pickup_mask, hold_ms)
                    engine.mark_pressed()
                    presses += 1
            else:
                points, in_radius = [], []

            # автофляска (в фокусе) + диагностика HP (всегда, чтобы поймать и «не в фокусе»)
            if hp_d:
                low, hm = hp_d.is_low(hp_frame)
                hp_pct = hm["ratio"]
                hp_v = hm["fill_v"]
                # диагностика потери HP: снимает улики на переходах ratio=None (если F7 вкл)
                diag.feed(hp_frame, hp_d, hm, foreground, time.perf_counter())
                if manual_shot.is_set():
                    manual_shot.clear()
                    diag.manual(hp_frame, hp_d, hm, foreground)
                if foreground:
                    # дебаунс. None (орб не распознан/мерцание) НЕ сбрасывает стрик —
                    # иначе на низком HP мерцание роняет счётчик и фляга не жмётся.
                    if hm["ratio"] is None:
                        pass                       # неизвестно — держим стрик как есть
                    elif low:
                        low_streak += 1
                    else:
                        low_streak = 0
                    # жмём только на реально низком кадре (low). None держит стрик,
                    # но сам не стреляет; высокий — сбрасывает. Иначе залипание выстрелов.
                    if (low and low_streak >= hp_confirm
                            and time.perf_counter() - last_flask >= hp_cooldown):
                        bridge.suppress_triggers = True
                        if flask_right:
                            vx.tap_right_trigger(flask_hold)
                        else:
                            vx.tap_left_trigger(flask_hold)
                        bridge.suppress_triggers = False
                        last_flask = time.perf_counter()
                        flasks += 1
                        log.info("HP фляга (%s): HP=%s < %.0f%%",
                                 "R2" if flask_right else "L2",
                                 f"{hp_pct*100:.0f}%" if hp_pct is not None else "?",
                                 hp_threshold * 100)
                else:
                    low_streak = 0
            else:
                low_streak = 0

            iters += 1
            now = time.perf_counter()
            if now - last_status >= 1.0:
                fps = iters / (now - last_status)
                log.info("targets=%d in_radius=%d auto=%s fg=%s presses=%d hp=%s v=%s flasks=%d fps=%.0f",
                         len(points), len(in_radius),
                         "ON" if auto.on else "off", foreground, presses,
                         f"{hp_pct*100:.0f}%" if hp_pct is not None else "-", hp_v, flasks, fps)
                last_status = now
                iters = 0

            time.sleep(0.01)
    except KeyboardInterrupt:
        log.info("Прервано (Ctrl+C)")
    finally:
        listener.stop()
        bridge.stop()
        vx.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
