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
from hp_ocr import HPNumReader
from diag_dump import HPDiag, dump_loot
from overlay import Overlay, OverlayState
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
    hp_valid_val = hp_cfg.get("valid_val", 90)   # порог яркости «живого» орба для fallback
    # мин. доля красного, чтобы fallback на слепой кадр сработал: отделяет реальную
    # просадку под эффектом (красная лужа есть, ~0.08) от тёмного орба в меню (~0.001).
    hp_fallback_red_min = hp_cfg.get("fallback_red_frac_min", 0.04)
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

    # --- чтение HP по цифрам HUD (основной источник; орб — fallback) ---
    num_cfg = cfg.get("hp_num", {})
    num_reader = None        # читает по полному кадру (auto/diag вкл)
    num_reader_crop = None   # читает по быстрому кропу орба (auto выкл)
    if num_cfg.get("enabled") and hp_det:
        nr = HPNumReader(num_cfg)
        if not nr.ready:
            log.warning("hp_num включён, но цифры не откалиброваны "
                        "(запусти calibrate_hp_digits.py) — работаю по орбу")
        else:
            num_reader = nr
            # расширяем быстрый захват орба, чтобы в него попала строка цифр HP
            nx1 = region["left"] + num_cfg["roi"]["x1"] * fw
            ny1 = region["top"] + num_cfg["roi"]["y1"] * fh
            nx2 = region["left"] + num_cfg["roi"]["x2"] * fw
            ny2 = region["top"] + num_cfg["roi"]["y2"] * fh
            gx1 = min(orb_region["left"], int(nx1))
            gy1 = min(orb_region["top"], int(ny1))
            gx2 = max(orb_region["left"] + orb_region["width"], int(nx2) + 1)
            gy2 = max(orb_region["top"] + orb_region["height"], int(ny2) + 1)
            orb_region = {"left": gx1, "top": gy1, "width": gx2 - gx1, "height": gy2 - gy1}
            sw, sh = orb_region["width"], orb_region["height"]
            # пересобираем орб-детектор кропа под расширенный регион
            crop_cfg = dict(hp_cfg)
            crop_cfg["orb"] = {"cx": (ocx - gx1) / sw, "cy": (ocy - gy1) / sh, "r": orp / sh}
            hp_det_crop = HPDetector(crop_cfg)
            # ридер цифр для быстрого пути: ROI в долях расширенного кропа
            ncfg_crop = dict(num_cfg)
            ncfg_crop["roi"] = {"x1": (nx1 - gx1) / sw, "y1": (ny1 - gy1) / sh,
                                "x2": (nx2 - gx1) / sw, "y2": (ny2 - gy1) / sh}
            num_reader_crop = HPNumReader(ncfg_crop)
            log.info("Чтение HP по цифрам ВКЛ: ROI %s max_hp=%s",
                     num_cfg["roi"], num_cfg.get("max_hp"))

    # быстрый захват лута: детектим не весь экран, а окно вокруг персонажа
    # (лут важен только в радиусе подбора) → в разы меньше пикселей, выше fps.
    lr = int(loot["pickup_radius_px"] * 1.15)   # радиус подбора + запас
    lccx = region["left"] + region["width"] // 2 + loot["center_offset_xy"][0]
    lccy = region["top"] + region["height"] // 2 + loot["center_offset_xy"][1]
    ll = max(region["left"], lccx - lr)
    lt = max(region["top"], lccy - lr)
    lrr = min(region["left"] + region["width"], lccx + lr)
    lbb = min(region["top"] + region["height"], lccy + lr)
    loot_region = {"left": ll, "top": lt, "width": lrr - ll, "height": lbb - lt}
    loot_center = (lccx - ll, lccy - lt)        # центр персонажа в координатах ROI
    log.info("Захват лута: ROI %s, центр персонажа в кропе %s", loot_region, loot_center)

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
    k_overlay = cfg["hotkeys"].get("overlay", "f6")

    # --- оверлей со статусами (поверх игры, click-through) ---
    ov_state = OverlayState()
    ov_state.flask_enabled = hp_det is not None
    overlay = Overlay(ov_state, region)
    overlay.start()

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
        if name == k_overlay:
            vis = overlay.toggle()
            log.info("Оверлей: %s", "показан" if vis else "скрыт")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    log.info("Готово. F8 — подбор, F7 — диагностика, F9 — снимок, F6 — оверлей, F12 — выход.")

    # --- главный цикл ---
    last_status = 0.0
    last_flask = 0.0
    presses = 0
    flasks = 0
    hp_pct = None
    hp_v = None
    hp_src = "-"             # чем прочитано HP: num (цифры) / orb / - (нет)
    low_streak = 0
    last_good_ratio = None   # последнее достоверное HP — для fallback на слепые кадры
    iters = 0
    try:
        while not stop.is_set():
            foreground = win.is_foreground() if win.hwnd else True

            # --- захват ---
            # F8/F7 → ОДИН полный грабь, из него вырезаем зону орба (HP) и ROI вокруг
            #   персонажа (лут). Один грабь принципиально: dxcam на второй вызов подряд
            #   отдаёт устаревший кадр (None→last), что ломало детекцию лута.
            # F8 выкл → грабим только кроп орба → высокий fps.
            if auto.on or diag.enabled or orb_region is None:
                full = cap.grab(region)
                if full is None:
                    time.sleep(0.02)
                    continue
                hp_frame, hp_d, num_rd = full, hp_det, num_reader   # measure() кропает орб сам
                if auto.on:
                    lx = loot_region["left"] - region["left"]
                    ly = loot_region["top"] - region["top"]
                    loot_frame = full[ly:ly + loot_region["height"],
                                      lx:lx + loot_region["width"]]
                    loot_ctr = loot_center
                else:
                    loot_frame, loot_ctr = None, None
            else:
                hp_frame, hp_d, num_rd = cap.grab(orb_region), hp_det_crop, num_reader_crop
                loot_frame, loot_ctr = None, None
                if hp_frame is None:
                    time.sleep(0.02)
                    continue

            # детекция лута — только окно вокруг персонажа (радиус подбора)
            if auto.on and loot_frame is not None:
                points, _mask = det.detect(loot_frame)
                in_radius = engine.targets_in_radius(points, loot_frame.shape, center=loot_ctr)
                if foreground and in_radius and engine.ready():
                    vx.tap(pickup_mask, hold_ms)
                    engine.mark_pressed()
                    presses += 1
            else:
                points, in_radius = [], []

            # автофляска (в фокусе) + диагностика HP (всегда, чтобы поймать и «не в фокусе»)
            if hp_d:
                low, hm = hp_d.is_low(hp_frame)
                num = num_rd.read(hp_frame) if num_rd else None   # (cur,max,ratio) | None
                hp_pct = num[2] if num is not None else hm["ratio"]
                hp_src = "num" if num is not None else "orb"
                hp_v = hm["fill_v"]
                # диагностика потери HP: снимает улики на переходах ratio=None (если F7 вкл)
                diag.feed(hp_frame, hp_d, hm, foreground, time.perf_counter())
                if manual_shot.is_set():
                    manual_shot.clear()
                    diag.manual(hp_frame, hp_d, hm, foreground)
                    # если автоподбор вкл — заодно снимок лут-детекции (по ROI вокруг персонажа)
                    if auto.on and loot_frame is not None:
                        dump_loot(diag.n, loot_frame, det, engine, foreground, loot_ctr)
                if foreground:
                    # источник HP по приоритету (low_now = True/False/None=неизвестно):
                    #  • ЦИФРЫ HUD — точные, без неоднозначности None → решают первыми;
                    #  • иначе уровень орба: ratio известен → low = ratio < порога;
                    #  • иначе орб ослеп (ratio=None), но ЖИВОЙ (красная лужа: red_frac≥
                    #    порога, fill_v≥valid_val), tripwire ниже порога, И последнее HP было
                    #    < порога → ослеп ПОСРЕДИ просадки: держим питьё (fallback ПРОДОЛЖАЕТ);
                    #  • иначе None → неизвестно (меню/перекрытие на полном HP) — НЕ пьём.
                    if num is not None:
                        last_good_ratio = num[2]
                        low_now = num[2] < hp_threshold
                    elif hm["ratio"] is not None:
                        last_good_ratio = hm["ratio"]
                        low_now = low
                    elif (hm.get("trip_below")
                          and hm.get("red_frac", 0) >= hp_fallback_red_min
                          and hm.get("fill_v", 0) >= hp_valid_val
                          and last_good_ratio is not None
                          and last_good_ratio < hp_threshold):
                        low_now = True
                    else:
                        low_now = None
                    # дебаунс. None (неизвестно) НЕ сбрасывает стрик — иначе мерцание
                    # роняет счётчик и фляга не жмётся; low растит, высокий сбрасывает.
                    if low_now is True:
                        low_streak += 1
                    elif low_now is None:
                        pass                       # неизвестно — держим стрик как есть
                    else:
                        low_streak = 0
                    # жмём только на реально низком кадре. None держит стрик, но сам не
                    # стреляет; высокий — сбрасывает. Иначе залипание выстрелов.
                    if (low_now and low_streak >= hp_confirm
                            and time.perf_counter() - last_flask >= hp_cooldown):
                        bridge.suppress_triggers = True
                        if flask_right:
                            vx.tap_right_trigger(flask_hold)
                        else:
                            vx.tap_left_trigger(flask_hold)
                        bridge.suppress_triggers = False
                        last_flask = time.perf_counter()
                        flasks += 1
                        log.info("HP фляга (%s): HP=%s [%s] < %.0f%%",
                                 "R2" if flask_right else "L2",
                                 f"{hp_pct*100:.0f}%" if hp_pct is not None else "?",
                                 hp_src, hp_threshold * 100)
                else:
                    low_streak = 0
                    last_good_ratio = None     # вне фокуса забываем HP — иначе stale-low даст fallback при возврате
            else:
                low_streak = 0

            # обновляем оверлей (дешёвые записи в общий state, читает поток tkinter)
            ov_state.auto_loot = auto.on
            ov_state.diag = diag.enabled
            ov_state.hp_pct = hp_pct
            ov_state.foreground = foreground
            ov_state.flasks = flasks
            ov_state.presses = presses

            iters += 1
            now = time.perf_counter()
            if now - last_status >= 1.0:
                fps = iters / (now - last_status)
                ov_state.fps = fps
                log.info("targets=%d in_radius=%d auto=%s fg=%s presses=%d hp=%s(%s) v=%s flasks=%d fps=%.0f",
                         len(points), len(in_radius),
                         "ON" if auto.on else "off", foreground, presses,
                         f"{hp_pct*100:.0f}%" if hp_pct is not None else "-", hp_src, hp_v, flasks, fps)
                last_status = now
                iters = 0

            time.sleep(0.01)
    except KeyboardInterrupt:
        log.info("Прервано (Ctrl+C)")
    finally:
        listener.stop()
        overlay.stop()
        bridge.stop()
        vx.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
