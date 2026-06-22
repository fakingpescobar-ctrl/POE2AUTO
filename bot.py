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
from hp_locate import find_digits_roi, derive_orb
from mob_bar import MobBarDetector
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
    num_cfg = cfg.get("hp_num", {})
    hp_enabled = bool(hp_cfg.get("enabled"))
    hp_cooldown = hp_cfg.get("cooldown_ms", 3000) / 1000.0
    flask_hold = hp_cfg.get("flask_hold_ms", 120)
    hp_threshold = hp_cfg.get("threshold", 0.55)
    hp_confirm = hp_cfg.get("confirm_frames", 4)
    hp_valid_val = hp_cfg.get("valid_val", 90)   # порог яркости «живого» орба для fallback
    # мин. доля красного, чтобы fallback на слепой кадр сработал: отделяет реальную
    # просадку под эффектом (красная лужа есть, ~0.08) от тёмного орба в меню (~0.001).
    hp_fallback_red_min = hp_cfg.get("fallback_red_frac_min", 0.04)
    flask_right = hp_cfg.get("flask_button") == "right_trigger"
    # сколько цифры могут не читаться, прежде чем считать это МЕНЮ (а не мельканием
    # в бою). В пределах окна орб подстраховывает; дольше — HUD перекрыт → не пьём.
    hp_num_grace = num_cfg.get("menu_grace_s", 0.5)
    fw, fh = region["width"], region["height"]

    # Геометрия HP (орб + ROI цифр) и зависимые объекты живут в одном холдере, чтобы их
    # можно было ПЕРЕСОБРАТЬ на лету после авто-локализации HUD (другое разрешение/UI).
    hpg = {"hp_det": None, "hp_det_crop": None, "orb_region": None,
           "num_reader": None, "num_reader_crop": None}
    loc_info = {"text": ""}   # что зацепила авто-локализация (для оверлея), напр. "1371/1371"
    hud_anchor = {"y": None}  # доля Y известной строки ЖИЗНИ — чтобы переиск не уехал на Щит

    def rebuild_hp_geometry():
        """(Пере)собрать всё, что зависит от геометрии орба (hp_cfg['orb']) и ROI цифр
        (num_cfg['roi']). Берёт ТЕКУЩИЕ значения из cfg — зовём на старте и после
        успешной авто-локализации. Когда F8 выкл — грабим только зону орба (а не весь
        экран) → высокий fps, поэтому считаем компактный orb_region."""
        if not hp_enabled:
            return
        hp_det = HPDetector(hp_cfg)
        ocx = region["left"] + hp_cfg["orb"]["cx"] * fw
        ocy = region["top"] + hp_cfg["orb"]["cy"] * fh
        orp = hp_cfg["orb"]["r"] * fh
        half = orp * 1.25  # запас вокруг орба
        rl, rt = region["left"], region["top"]
        ox1, oy1 = max(rl, ocx - half), max(rt, ocy - half)
        ox2, oy2 = min(rl + fw, ocx + half), min(rt + fh, ocy + half)
        sw, sh = ox2 - ox1, oy2 - oy1
        orb_region = {"left": int(ox1), "top": int(oy1), "width": int(sw), "height": int(sh)}
        crop_cfg = dict(hp_cfg)
        crop_cfg["orb"] = {"cx": (ocx - ox1) / sw, "cy": (ocy - oy1) / sh, "r": orp / sh}
        hp_det_crop = HPDetector(crop_cfg)

        num_reader = None        # читает по полному кадру (auto/diag вкл)
        num_reader_crop = None   # читает по быстрому кропу орба (auto выкл)
        if num_cfg.get("enabled"):
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
                crop_cfg = dict(hp_cfg)
                crop_cfg["orb"] = {"cx": (ocx - gx1) / sw, "cy": (ocy - gy1) / sh, "r": orp / sh}
                hp_det_crop = HPDetector(crop_cfg)
                ncfg_crop = dict(num_cfg)
                ncfg_crop["roi"] = {"x1": (nx1 - gx1) / sw, "y1": (ny1 - gy1) / sh,
                                    "x2": (nx2 - gx1) / sw, "y2": (ny2 - gy1) / sh}
                num_reader_crop = HPNumReader(ncfg_crop)
        hpg.update(hp_det=hp_det, hp_det_crop=hp_det_crop, orb_region=orb_region,
                   num_reader=num_reader, num_reader_crop=num_reader_crop)

    def try_locate_hud(reason):
        """Найти строку HP на свежем кадре, переписать cfg['hp_num']['roi'] и
        cfg['hp']['orb'] (орб выводится от рамки цифр) и пересобрать геометрию.
        True — нашли. Делает портабельным к смене разрешения/UI Scale."""
        if not (hp_enabled and num_cfg.get("enabled") and num_cfg.get("auto_locate")):
            return False
        probe = HPNumReader(num_cfg)
        if not probe.ready:
            return False
        frame = cap.grab(region)
        if frame is None:
            return False
        ffh, ffw = frame.shape[:2]
        # переиск держим у известной строки жизни (anchor), чтобы не залочиться на Щит,
        # когда строка жизни временно зашумлена эффектом
        found = find_digits_roi(frame, probe, prefer_y=hud_anchor["y"])
        if not found:
            return False
        hud_anchor["y"] = found["text_bbox"][1] / ffh
        num_cfg["roi"] = found["roi"]
        hp_cfg["orb"] = derive_orb(found["text_bbox"], ffw, ffh)
        loc_info["text"] = f"{found['cur']}/{found['max']}"
        rebuild_hp_geometry()
        log.info("Авто-локализация HUD (%s): HP=%d/%d ROI=%s орб=%s",
                 reason, found["cur"], found["max"],
                 {k: round(v, 4) for k, v in num_cfg["roi"].items()},
                 {k: round(v, 4) for k, v in hp_cfg["orb"].items()})
        return True

    # старт: пробуем авто-локализацию (HUD может быть не виден сразу — несколько попыток);
    # не вышло → работаем по ROI из конфига и продолжаем искать в главном цикле.
    located = False
    if hp_enabled and num_cfg.get("enabled") and num_cfg.get("auto_locate"):
        for _ in range(20):                       # ~до 2с попыток на старте
            if try_locate_hud("старт"):
                located = True
                break
            time.sleep(0.1)
        if not located:
            log.info("Авто-локализация HUD на старте не удалась — работаю по ROI из "
                     "конфига, продолжу искать в фоне")
    if not located:
        rebuild_hp_geometry()

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

    if hpg["hp_det"]:
        log.info("Автофляска ВКЛ: порог=%.0f%% кулдаун=%dмс подтв=%d кадра кнопка=%s | быстрый захват орба %s",
                 hp_threshold * 100, hp_cfg.get("cooldown_ms", 3000), hp_confirm,
                 "R2" if flask_right else "L2", hpg["orb_region"])
        if hpg["num_reader"]:
            log.info("Чтение HP по цифрам ВКЛ: ROI %s max_hp=%s",
                     {k: round(v, 4) for k, v in num_cfg["roi"].items()}, num_cfg.get("max_hp"))
    else:
        log.info("Автофляска выкл (запусти calibrate_hp.bat)")

    # --- авто-атака: держим R1, пока вверху видна полоса HP моба ---
    atk_cfg = cfg.get("auto_attack", {})
    atk_enabled = bool(atk_cfg.get("enabled"))
    mob_det = MobBarDetector(atk_cfg) if atk_enabled else None
    atk_btn = XUSB.get(atk_cfg.get("button", "right_bumper"), XUSB["right_bumper"])
    atk_grace = atk_cfg.get("release_grace_s", 0.35)
    if atk_enabled:
        log.info("Авто-атака ВКЛ (F10): кнопка=%s ROI полосы=%s",
                 atk_cfg.get("button", "right_bumper"), atk_cfg.get("roi"))

    # --- хоткеи ---
    auto = Toggle()
    attack = Toggle()  # авто-атака (F10) — держит R1 на полосе HP моба
    diag = HPDiag()  # диагностика потери HP (F7) — пишет улики в _debug/hp_events
    manual_shot = threading.Event()
    stop = threading.Event()
    k_toggle = cfg["hotkeys"].get("toggle", "f8")
    k_quit = cfg["hotkeys"].get("quit", "f12")
    k_diag = cfg["hotkeys"].get("diag", "f7")
    k_shot = cfg["hotkeys"].get("snapshot", "f9")
    k_overlay = cfg["hotkeys"].get("overlay", "f6")
    k_attack = cfg["hotkeys"].get("attack", "f10")

    # --- оверлей со статусами (поверх игры, click-through) ---
    ov_state = OverlayState()
    ov_state.flask_enabled = hpg["hp_det"] is not None
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
        if name == k_attack and mob_det is not None:
            state = attack.flip()
            log.info("Авто-атака: %s", "ВКЛ" if state else "выкл")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    log.info("Готово. F8 — подбор, F10 — авто-атака, F7 — диагностика, F9 — снимок, F6 — оверлей, F12 — выход.")

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
    num_blind_since = None   # когда цифры перестали читаться (для детекта меню)
    last_locate = 0.0        # таймер фонового переиска HUD (пока не локализован)
    last_num_ok = time.perf_counter()  # когда цифры читались в последний раз (для самолечения ROI)
    last_glitch_warn = 0.0   # throttle варнинга анти-глитча цифр
    r1_held = False          # держит ли бот сейчас R1 (авто-атака)
    last_bar_seen = 0.0      # когда последний раз видели полосу HP моба
    iters = 0
    try:
        while not stop.is_set():
            foreground = win.is_foreground() if win.hwnd else True

            # --- захват ---
            # F8/F7 → ОДИН полный грабь, из него вырезаем зону орба (HP) и ROI вокруг
            #   персонажа (лут). Один грабь принципиально: dxcam на второй вызов подряд
            #   отдаёт устаревший кадр (None→last), что ломало детекцию лута.
            # F8 выкл → грабим только кроп орба → высокий fps.
            # F8/F10/F7 → нужен полный кадр (лут / полоса моба вверху / диагностика).
            if auto.on or attack.on or diag.enabled or hpg["orb_region"] is None:
                full = cap.grab(region)
                if full is None:
                    time.sleep(0.02)
                    continue
                hp_frame, hp_d, num_rd = full, hpg["hp_det"], hpg["num_reader"]   # measure() кропает орб сам
                if auto.on:
                    lx = loot_region["left"] - region["left"]
                    ly = loot_region["top"] - region["top"]
                    loot_frame = full[ly:ly + loot_region["height"],
                                      lx:lx + loot_region["width"]]
                    loot_ctr = loot_center
                else:
                    loot_frame, loot_ctr = None, None
            else:
                full = None
                hp_frame, hp_d, num_rd = cap.grab(hpg["orb_region"]), hpg["hp_det_crop"], hpg["num_reader_crop"]
                loot_frame, loot_ctr = None, None
                if hp_frame is None:
                    time.sleep(0.02)
                    continue

            # авто-атака: пока вверху видна полоса HP моба — держим R1 (в фокусе).
            # press каждый кадр идемпотентен и переподтверждает удержание; отпускаем
            # с грейсом (сглаживание мерцания / добив почти мёртвого моба).
            if mob_det is not None and attack.on and foreground and full is not None:
                if mob_det.present(full):
                    last_bar_seen = time.perf_counter()
                    vx.press(atk_btn)
                    r1_held = True
                elif r1_held and time.perf_counter() - last_bar_seen > atk_grace:
                    vx.release(atk_btn)
                    r1_held = False
            elif r1_held:                       # авто-атака выкл / вне фокуса → отпустить
                vx.release(atk_btn)
                r1_held = False

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
                # анти-глитч цифр: эффекты/искры поверх ROI дают ложный низкий % при
                # ПОЛНОМ HP (читает «0/1057» при полном орбе) → спам флягой. Орб ищет
                # КРАСНОЕ и белым частицам не подвержен → надёжный арбитр: если цифры
                # зовут пить, а орб уверенно полный (≥порога) — зануляем чтение, дальше
                # отработает орб (полный → не пьём). На РЕАЛЬНОМ сливе орб тоже падает
                # < порога (он читает текущий кадр, не отстаёт) → фляга идёт штатно.
                if (num is not None and num[2] < hp_threshold
                        and hm["ratio"] is not None and hm["ratio"] >= hp_threshold):
                    if time.perf_counter() - last_glitch_warn > 3.0:
                        log.warning("Цифры HP=%.0f%% при орбе %.0f%% (полный) — глитч цифр "
                                    "(эффект поверх ROI?), игнор", num[2] * 100, hm["ratio"] * 100)
                        last_glitch_warn = time.perf_counter()
                    num = None
                hp_pct = num[2] if num is not None else hm["ratio"]
                hp_src = "num" if num is not None else "orb"
                if num is not None:               # «захват ✓» в оверлее = ЖИВОЕ значение, не снимок старта
                    loc_info["text"] = f"{num[0]}/{num[1]}"
                    last_num_ok = time.perf_counter()
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
                    # выбор источника: цифры → (кратко нет цифр) орб → (долго нет = меню) стоп
                    use_orb = False
                    if num is not None:
                        num_blind_since = None
                        last_good_ratio = num[2]
                        low_now = num[2] < hp_threshold
                    elif num_rd is None:
                        use_orb = True          # цифры выключены/не калиброваны → орб основной
                    else:
                        # цифры включены, но не читаются на этом кадре
                        if num_blind_since is None:
                            num_blind_since = time.perf_counter()
                        if time.perf_counter() - num_blind_since <= hp_num_grace:
                            use_orb = True      # короткое мелькание в бою → подстрахует орб
                        else:
                            low_now = None      # надолго пропали → меню/перекрыт HUD → не пьём

                    if use_orb:
                        # орб-логика (fallback): ratio → слепой орб посреди просадки
                        if hm["ratio"] is not None:
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
                    num_blind_since = None
            else:
                low_streak = 0

            # обновляем оверлей (дешёвые записи в общий state, читает поток tkinter)
            ov_state.auto_loot = auto.on
            ov_state.attack_enabled = mob_det is not None
            ov_state.attack_on = attack.on
            ov_state.attacking = r1_held
            ov_state.diag = diag.enabled
            ov_state.hp_pct = hp_pct
            ov_state.foreground = foreground
            ov_state.flasks = flasks
            ov_state.presses = presses
            if hp_enabled and num_cfg.get("enabled") and num_cfg.get("auto_locate"):
                ov_state.locate_state = "ok" if located else "search"
                ov_state.locate_info = loc_info["text"]
            else:
                ov_state.locate_state = "off"

            iters += 1
            now = time.perf_counter()
            # самолечение: если цифры залокализованы, но перестали читаться ≥3с в фокусе
            # (число выросло шире ROI после левел-апа, HUD сместился) — сбрасываем
            # фиксацию, чтобы переиск ниже нашёл строку заново с актуальным ROI.
            if (located and hp_enabled and num_cfg.get("enabled") and num_cfg.get("auto_locate")
                    and hpg["num_reader"] is not None and foreground
                    and now - last_num_ok >= 3.0):
                log.info("Цифры HP не читаются ≥3с — перелокализую HUD (число выросло/HUD сместился)")
                located = False
                last_num_ok = now            # не зацикливать сброс, пока переиск не отработает
            # фоновый переиск HUD: пока не локализован (стартовая попытка не нашла —
            # игра грузилась/меню), пробуем раз в 3с на свежем кадре, потом фиксируем.
            if (not located and hp_enabled and num_cfg.get("enabled")
                    and num_cfg.get("auto_locate") and now - last_locate >= 3.0):
                last_locate = now
                located = try_locate_hud("фон")
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
