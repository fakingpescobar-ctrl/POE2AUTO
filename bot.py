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
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from pynput import keyboard

from config import load_config, save_config
from screen import ScreenCapture
from window import GameWindow
from loot_engine import LootEngine
from hp_detector import HPDetector
from hp_engine import HPEngine, HPDecision
from hp_ocr import HPNumReader
from hp_locate import find_digits_roi, derive_orb
from mob_bar import MobBarDetector
from zone_detector import ZoneDetector
from mem_detect import MemoryMobDetector
from diag_dump import HPDiag, dump_loot, dump_attack_trigger
from overlay import Overlay, OverlayState
from virtual_xbox import VirtualXbox360, XUSB
from dualsense_bridge import DualSenseBridge
from utils import Toggle, build_detector

LOG_PATH = os.path.join(os.path.dirname(__file__), "bot.log")

_fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
_root = logging.getLogger()
_root.setLevel(logging.INFO)
# консоль
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_root.addHandler(_sh)
# файл (ротация: 3 файла × 1 МБ — не теряем старые логи, но не засоряем диск)
_fh = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
_root.addHandler(_fh)

log = logging.getLogger("poe2auto.bot")


class LootWorker(threading.Thread):
    """Детект лута в отдельном потоке. Не блокирует главный цикл."""

    def __init__(self, detector, engine, pickup_mask, hold_ms, vx, bridge):
        super().__init__(daemon=True)
        self._cfg_lock = threading.Lock()
        self.detector = detector
        self.engine = engine
        self.pickup_mask = pickup_mask
        self.hold_ms = hold_ms
        self.vx = vx
        self.bridge = bridge
        self._frame_q: queue.Queue = queue.Queue(maxsize=1)
        self._result: Optional[Tuple[list, list]] = None
        self._result_lock = threading.Lock()
        self._stop_event = threading.Event()

    def update_config(self, detector, engine):
        """Обновить детектор и движок потокобезопасно."""
        with self._cfg_lock:
            self.detector = detector
            self.engine = engine

    def submit(self, loot_frame, loot_ctr, foreground):
        """Отправить кадр на обработку (неблокирующе). Старый кадр отбрасывается."""
        try:
            self._frame_q.put_nowait((loot_frame, loot_ctr, foreground))
        except queue.Full:
            pass

    def get_result(self) -> Optional[Tuple[list, list]]:
        """Взять последний результат (неблокирующе). None = ещё не обработан."""
        with self._result_lock:
            r = self._result
            self._result = None
            return r

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                loot_frame, loot_ctr, foreground = self._frame_q.get(timeout=0.1)
            except queue.Empty:
                continue
            with self._cfg_lock:
                detector = self.detector
                engine = self.engine
            points, _mask = detector.detect(loot_frame)
            in_radius = engine.targets_in_radius(points, loot_frame.shape, center=loot_ctr)
            with self._result_lock:
                self._result = (points, in_radius)


class AttackWorker(threading.Thread):
    """Детект мобов в отдельном потоке. Поддерживает CV (MobBarDetector) и память (MemoryMobDetector)."""

    def __init__(self, mob_det, zone_det=None, use_memory=False):
        super().__init__(daemon=True)
        self._cfg_lock = threading.Lock()
        self.mob_det = mob_det
        self.zone_det = zone_det
        self.use_memory = use_memory
        self._frame_q: queue.Queue = queue.Queue(maxsize=1)
        self._result: Optional[Dict[str, Any]] = None
        self._result_lock = threading.Lock()
        self._stop_event = threading.Event()

    def update_config(self, mob_det, zone_det, use_memory):
        """Обновить конфигурацию потокобезопасно."""
        with self._cfg_lock:
            self.mob_det = mob_det
            self.zone_det = zone_det
            self.use_memory = use_memory

    def submit(self, full_frame=None, foreground=True):
        """Отправить кадр на обработку (неблокирующе). Для memory-детекта frame не нужен."""
        try:
            self._frame_q.put_nowait((full_frame, foreground))
        except queue.Full:
            pass

    def get_result(self) -> Optional[Dict[str, Any]]:
        """Взять последний результат (неблокирующе). None = ещё не обработан."""
        with self._result_lock:
            r = self._result
            self._result = None
            return r

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                full_frame, foreground = self._frame_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self._cfg_lock:
                    mob_det = self.mob_det
                    zone_det = self.zone_det
                    use_memory = self.use_memory
                mob_present = False
                in_hideout = False
                if use_memory and mob_det is not None:
                    mob_det.update()
                    in_hideout = mob_det.in_hideout()
                    if not in_hideout:
                        mob_present = mob_det.present()
                elif mob_det is not None and foreground:
                    in_hideout = zone_det is not None and zone_det.in_hideout(full_frame)
                    if not in_hideout:
                        mob_present = mob_det.present(full_frame)
                with self._result_lock:
                    self._result = {"mob_present": mob_present, "in_hideout": in_hideout}
            except Exception as e:
                log.error("AttackWorker error: %s", e, exc_info=True)


def main() -> int:
    cfg: Dict[str, Any] = load_config()
    loot: Dict[str, Any] = cfg["loot"]

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
    if win.find() and (region := win.get_region()):
        log.info("Окно PoE2 найдено: %s", region)
    else:
        region = GameWindow.primary_region()
        log.warning("Окно PoE2 не найдено — захват всего монитора %s. "
                    "Гейт по фокусу отключён.", region)
    cap = ScreenCapture(cfg["capture"]["backend"])
    det = build_detector(cfg)
    engine = LootEngine(loot["pickup_radius_px"], loot["center_offset_xy"],
                        loot["cooldown_ms"],
                        priority_mode=loot.get("priority_mode", "size"))
    log.info("Детектор: цвета=%s радиус=%d кулдаун=%dмс",
             det.colors, engine.radius_px, loot["cooldown_ms"])

    # --- автофляска HP ---
    hp_cfg = cfg["hp"]
    num_cfg = cfg.get("hp_num", {})
    hp_enabled = bool(hp_cfg.get("enabled"))
    hp_cooldown = hp_cfg.get("cooldown_ms", 333) / 1000.0
    flask_hold = hp_cfg.get("flask_hold_ms", 120)
    hp_threshold = hp_cfg.get("threshold", 0.88)
    hp_confirm = hp_cfg.get("confirm_frames", 1)
    hp_valid_val = hp_cfg.get("valid_val", 45)   # порог яркости «живого» орба для fallback
    # мин. доля красного, чтобы fallback на слепой кадр сработал: отделяет реальную
    # просадку под эффектом (красная лужа есть, ~0.08) от тёмного орба в меню (~0.001).
    hp_fallback_red_min = hp_cfg.get("fallback_red_frac_min", 0.04)
    # сколько цифры должны держаться низкими при НЕсогласном (полном) орбе, прежде чем
    # пить — фильтр транзиентных глитчей цифр от вспышек, не подавляя реальную просадку
    num_low_grace = hp_cfg.get("num_low_grace_s", 0.35)
    flask_right = hp_cfg.get("flask_button") == "right_trigger"
    # сколько цифры могут не читаться, прежде чем считать это МЕНЮ (а не мельканием
    # в бою). В пределах окна орб подстраховывает; дольше — HUD перекрыт → не пьём.
    hp_num_grace = num_cfg.get("menu_grace_s", 0.5)
    fw, fh = region["width"], region["height"]

    # HP-движок: принимает решения (low/high/unknown/clip) по цифрам/орбу
    hp_engine = HPEngine(
        threshold=hp_threshold,
        confirm_frames=hp_confirm,
        num_low_grace_s=num_low_grace,
        fallback_red_frac_min=hp_fallback_red_min,
        valid_val=hp_valid_val,
        menu_grace_s=hp_num_grace,
    )

    # Геометрия HP (орб + ROI цифр) и зависимые объекты живут в одном холдере, чтобы их
    # можно было ПЕРЕСОБРАТЬ на лету после авто-локализации HUD (другое разрешение/UI).
    hpg: Dict[str, Any] = {"hp_det": None, "hp_det_crop": None, "orb_region": None,
                           "num_reader": None, "num_reader_crop": None}
    loc_info: Dict[str, str] = {"text": ""}
    hud_anchor: Dict[str, float] = {"max": 0}

    def rebuild_hp_geometry() -> None:
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

    def try_locate_hud(reason: str) -> bool:
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
        found = find_digits_roi(frame, probe)   # верхняя ресурсная строка (жизнь)
        if not found:
            return False
        # защита от лока на МЕНЬШИЙ пул (Щит/ES): жизнь — крупнейший пул. Когда строка
        # жизни зашумлена эффектом и не распозналась, верхней становится Щит — его max
        # сильно меньше (251 ≪ 1084). Отклоняем, держим геометрию жизни; когда жизнь
        # снова читается — переиск её подхватит (само-восстановление, без застревания).
        if hud_anchor["max"] and found["max"] < 0.5 * hud_anchor["max"]:
            return False
        hud_anchor["max"] = found["max"]
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

    # лут-детект в отдельном потоке — не блокирует FPS в бою
    loot_worker = LootWorker(det, engine, pickup_mask, hold_ms, vx, bridge)
    loot_worker.start()

    if hpg["hp_det"]:
        log.info("Автофляска ВКЛ (%s): порог=%.0f%% кулдаун=%dмс подтв=%d кадра кнопка=%s | быстрый захват орба %s",
                 cfg["hotkeys"].get("flask", "f3").upper(), hp_threshold * 100,
                 hp_cfg.get("cooldown_ms", 3000), hp_confirm,
                 "R2" if flask_right else "L2", hpg["orb_region"])
        if hpg["num_reader"]:
            log.info("Чтение HP по цифрам ВКЛ: ROI %s max_hp=%s",
                     {k: round(v, 4) for k, v in num_cfg["roi"].items()}, num_cfg.get("max_hp"))
    else:
        log.info("Автофляска выкл (запусти calibrate_hp.bat)")

    # --- авто-атака: держим R1, пока в радиусе есть живой моб ---
    atk_cfg = cfg.get("auto_attack", {})
    atk_enabled = bool(atk_cfg.get("enabled"))
    use_memory = atk_cfg.get("use_memory", True)  # по умолчанию memory detection

    if use_memory and atk_enabled:
        # Memory-based: чтение памяти PoE2 (PROCESS_VM_READ)
        mob_det = MemoryMobDetector({"attack_range": atk_cfg.get("attack_range", 15.0)})
        if not mob_det.connect():
            log.warning("Memory detection: не удалось подключиться к PoE2 — "
                        "откат на CV-детект")
            mob_det = None
            use_memory = False
            _atk_fallback_reason = "Memory → CV: нет подключения"
        elif not mob_det.available:
            log.warning("Memory detection: подключён, но AOB scan не нашёл "
                        "игровые структуры (оффсеты устарели?) — откат на CV-детект")
            mob_det.disconnect()
            mob_det = None
            use_memory = False
            _atk_fallback_reason = "Memory → CV: AOB scan failed"
        else:
            _atk_fallback_reason = ""
    else:
        mob_det = None
        use_memory = False
        _atk_fallback_reason = ""

    if not use_memory and atk_enabled:
        # CV-based fallback: детект полосы HP моба по цвету
        mob_det = MobBarDetector(atk_cfg)

    zone_det = None  # memory detector делает in_hideout() сам
    if not use_memory and atk_enabled:
        zone_det = ZoneDetector(atk_cfg.get("zone", {}))

    atk_btn = XUSB.get(atk_cfg.get("button", "right_bumper"), XUSB["right_bumper"])
    atk_grace = atk_cfg.get("release_grace_s", 0.6)

    # attack-детект в отдельном потоке
    attack_worker = AttackWorker(mob_det, zone_det, use_memory=use_memory)
    if atk_enabled:
        attack_worker.start()
        mode = "MEMORY" if use_memory else "CV"
        log.info("Авто-атака ВКЛ (%s): кнопка=%s режим=%s",
                 cfg["hotkeys"].get("attack", "f2").upper(),
                 atk_cfg.get("button", "right_bumper"), mode)

    # --- хоткеи ---
    auto = Toggle()
    attack = Toggle()  # авто-атака (хоткей attack в config.yaml) — держит R1 на полосе HP моба
    # авто-фляска (хоткей flask в config.yaml) — ВКЛ по умолчанию (прежнее поведение без
    # тумблера — бот пил всегда, пока hp_cfg["enabled"]); F3 даёт выключить руками.
    flask = Toggle(default=True)
    diag = HPDiag()  # диагностика потери HP (F7) — пишет улики в _debug/hp_events
    manual_shot = threading.Event()
    stop = threading.Event()
    k_toggle = cfg["hotkeys"].get("toggle", "f8")
    k_quit = cfg["hotkeys"].get("quit", "f12")
    k_diag = cfg["hotkeys"].get("diag", "f7")
    k_shot = cfg["hotkeys"].get("snapshot", "f9")
    k_overlay = cfg["hotkeys"].get("overlay", "f6")
    k_attack = cfg["hotkeys"].get("attack", "f10")
    k_flask = cfg["hotkeys"].get("flask", "f3")
    k_reload = "f5"

    def reload_cfg() -> None:
        """Hot-reload: перечитать config.yaml и применить безопасные параметры на лету."""
        nonlocal cfg, loot, det, engine, hp_cfg, hp_threshold, hp_cooldown
        nonlocal flask_hold, flask_right, hp_confirm
        nonlocal mob_det, atk_cfg, atk_enabled, atk_btn, atk_grace
        nonlocal use_memory, zone_det, hp_engine, num_cfg
        try:
            new_cfg = load_config()
        except Exception as e:
            log.error("Ошибка чтения config.yaml: %s", e)
            return
        cfg = new_cfg
        loot = cfg["loot"]
        # лут: детектор цвета + радиус/кулдаун
        det = build_detector(cfg)
        engine = LootEngine(loot["pickup_radius_px"], loot["center_offset_xy"],
                            loot["cooldown_ms"],
                            priority_mode=loot.get("priority_mode", "size"))
        # HP: пороги, кулдаун, кнопка + пересбор геометрии
        hp_cfg = cfg["hp"]
        num_cfg = cfg.get("hp_num", {})
        hp_threshold = hp_cfg.get("threshold", 0.88)
        hp_cooldown = hp_cfg.get("cooldown_ms", 333) / 1000.0
        flask_hold = hp_cfg.get("flask_hold_ms", 120)
        flask_right = hp_cfg.get("flask_button") == "right_trigger"
        hp_confirm = hp_cfg.get("confirm_frames", 1)
        hp_engine = HPEngine(
            threshold=hp_threshold,
            confirm_frames=hp_confirm,
            num_low_grace_s=hp_cfg.get("num_low_grace_s", 0.35),
            fallback_red_frac_min=hp_cfg.get("fallback_red_frac_min", 0.04),
            valid_val=hp_cfg.get("valid_val", 45),
            menu_grace_s=num_cfg.get("menu_grace_s", 0.5),
        )
        rebuild_hp_geometry()
        # авто-атака: пороги (с disconnect старого детектора при смене режима)
        atk_cfg = cfg.get("auto_attack", {})
        atk_enabled = bool(atk_cfg.get("enabled"))
        use_memory = atk_cfg.get("use_memory", True)
        old_mob_det = mob_det
        if use_memory and atk_enabled:
            if mob_det is None or not isinstance(mob_det, MemoryMobDetector):
                mob_det = MemoryMobDetector({"attack_range": atk_cfg.get("attack_range", 15.0)})
                mob_det.connect()
            else:
                mob_det.attack_range = atk_cfg.get("attack_range", 15.0)
                mob_det.attack_range_sq = mob_det.attack_range ** 2
        elif atk_enabled:
            mob_det = MobBarDetector(atk_cfg)
        else:
            mob_det = None
        # закрыть handle старого memory-детектора если режим сменился
        if old_mob_det is not None and isinstance(old_mob_det, MemoryMobDetector):
            if old_mob_det is not mob_det:
                try:
                    old_mob_det.disconnect()
                except Exception:
                    pass
        zone_det = None if use_memory else (ZoneDetector(atk_cfg.get("zone", {})) if atk_enabled else None)
        atk_btn = XUSB.get(atk_cfg.get("button", "right_bumper"), XUSB["right_bumper"])
        atk_grace = atk_cfg.get("release_grace_s", 0.6)
        # обновить лут-воркер новыми детектором/движком
        loot_worker.update_config(det, engine)
        # обновить attack-воркер новыми детекторами
        attack_worker.update_config(mob_det, zone_det, use_memory)
        log.info("Config перезагружен (F5): детектор=%s порог=%.0f%% кулдаун=%dмс",
                 det.colors, hp_threshold * 100, loot["cooldown_ms"])

    # --- оверлей со статусами (поверх игры, click-through) ---
    ov_state = OverlayState()
    ov_state.flask_enabled = hpg["hp_det"] is not None
    overlay = Overlay(ov_state, region, cfg["hotkeys"])
    overlay.start()
    if _atk_fallback_reason:
        ov_state.notification = _atk_fallback_reason
        ov_state.notification_until = time.time() + 5.0

    def on_press(key) -> Optional[bool]:
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
        if name == k_flask and hpg["hp_det"] is not None:
            state = flask.flip()
            log.info("Авто-фляска: %s", "ВКЛ" if state else "выкл")
        if name == k_reload:
            reload_cfg()

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    log.info("Готово. %s — подбор, %s — авто-атака, %s — авто-фляска, %s — диагностика, "
             "%s — снимок, %s — оверлей, %s — перезагрузка, %s — выход.",
             k_toggle.upper(), k_attack.upper(), k_flask.upper(), k_diag.upper(),
             k_shot.upper(), k_overlay.upper(), k_reload.upper(), k_quit.upper())

    # --- главный цикл ---
    last_status = 0.0
    last_flask = 0.0
    presses = 0
    flasks = 0
    last_locate = 0.0        # таймер фонового переиска HUD (пока не локализован)
    last_num_ok = time.perf_counter()  # когда цифры читались в последний раз (для самолечения ROI)
    r1_held = False          # держит ли бот сейчас R1 (авто-атака)
    last_bar_seen = 0.0      # когда последний раз видели полосу HP моба
    atk_dump_n = 0           # счётчик улик ложных срабатываний авто-атаки
    last_atk_dump = 0.0
    last_mem_retry = 0.0     # когда последний раз пробовали memory detection
    iters = 0
    try:
        while not stop.is_set():
            foreground = win.is_foreground() if win.hwnd else True

            # --- ОДИН захват полного кадра — все зоны вырезаем из него ---
            # dxcam перезаписывает буфер при каждом grab(), поэтому несколько
            # grab() с разными регионами в одном кадре ломают детекцию.
            full = cap.grab(region)
            if full is None:
                time.sleep(0.02)
                continue
            full = full.copy()

            # HP: кроп орба из полного кадра
            hp_frame = None
            hp_d, num_rd = hpg["hp_det_crop"], hpg["num_reader_crop"]
            if hpg["orb_region"]:
                orb = hpg["orb_region"]
                ox, oy = orb["left"] - region["left"], orb["top"] - region["top"]
                hp_frame = full[oy:oy + orb["height"], ox:ox + orb["width"]]
            if hp_frame is None and hp_enabled:
                time.sleep(0.02)
                continue

            # Лут: кроп вокруг персонажа из полного кадра
            loot_frame, loot_ctr = None, None
            if auto.on:
                lx = loot_region["left"] - region["left"]
                ly = loot_region["top"] - region["top"]
                loot_frame = full[ly:ly + loot_region["height"],
                                  lx:lx + loot_region["width"]]
                loot_ctr = loot_center

            # авто-атака — в отдельном потоке
            if mob_det is not None and attack.on and foreground:
                try:
                    if use_memory:
                        attack_worker.submit(foreground=foreground)
                    else:
                        attack_worker.submit(full, foreground)
                    atk_result = attack_worker.get_result()
                    if atk_result is not None:
                        if atk_result["in_hideout"]:
                            if r1_held and time.perf_counter() - last_bar_seen > atk_grace:
                                vx.release(atk_btn)
                                r1_held = False
                        elif atk_result["mob_present"]:
                            if not r1_held and time.perf_counter() - last_atk_dump > 10.0:
                                atk_dump_n += 1
                                last_atk_dump = time.perf_counter()
                                dump_frame = full
                                dump_attack_trigger(atk_dump_n, dump_frame, mob_det, foreground)
                            last_bar_seen = time.perf_counter()
                            vx.press(atk_btn)
                            r1_held = True
                        elif r1_held and time.perf_counter() - last_bar_seen > atk_grace:
                            vx.release(atk_btn)
                            r1_held = False
                except Exception as e:
                    log.debug("Attack worker error: %s", e)
            elif r1_held:                       # авто-атака выкл / вне фокуса → отпустить
                vx.release(atk_btn)
                r1_held = False

            # детекция лута — в отдельном потоке (не блокирует FPS)
            if auto.on and loot_frame is not None:
                loot_worker.submit(loot_frame, loot_ctr, foreground)
                result = loot_worker.get_result()
                if result is not None:
                    points, in_radius = result
                    if foreground and in_radius and engine.ready():
                        vx.tap(pickup_mask, hold_ms)
                        engine.mark_pressed()
                        presses += 1
                else:
                    points, in_radius = [], []
            else:
                points, in_radius = [], []

            # автофляска (в фокусе) + диагностика HP (всегда, чтобы поймать и «не в фокусе»)
            if hp_d:
                low, hm = hp_d.is_low(hp_frame)
                num = num_rd.read(hp_frame) if num_rd else None   # (cur,max,ratio) | None
                if num is not None:
                    loc_info["text"] = f"{num[0]}/{num[1]}"
                    last_num_ok = time.perf_counter()
                # диагностика потери HP: снимает улики на переходах ratio=None (если F7 вкл)
                diag.feed(hp_frame, hp_d, hm, foreground, time.perf_counter())
                if manual_shot.is_set():
                    manual_shot.clear()
                    diag.manual(hp_frame, hp_d, hm, foreground)
                    # если автоподбор вкл — заодно снимок лут-детекции (по ROI вокруг персонажа)
                    if auto.on and loot_frame is not None:
                        dump_loot(diag.n, loot_frame, det, engine, foreground, loot_ctr)
                # HP-решение через движок
                hp_engine.decide(num, hm, low, foreground)
                if hp_engine.need_relocate:
                    log.warning("Цифры HP=%.0f%% при орбе — обрезка ROI, перелокализую",
                                (num[2] * 100) if num else 0)
                    located = False
                # жмём флягу по решению движка
                if (flask.on and hp_engine.decision == HPDecision.LOW
                        and time.perf_counter() - last_flask >= hp_cooldown):
                    bridge.suppress_triggers = True
                    def _do_flask():
                        if flask_right:
                            vx.tap_right_trigger(flask_hold)
                        else:
                            vx.tap_left_trigger(flask_hold)
                        bridge.suppress_triggers = False
                    threading.Thread(target=_do_flask, daemon=True).start()
                    last_flask = time.perf_counter()
                    flasks += 1
                    log.info("HP фляга (%s): HP=%s [%s] < %.0f%%",
                             "R2" if flask_right else "L2",
                             f"{hp_engine.hp_pct*100:.0f}%" if hp_engine.hp_pct is not None else "?",
                             hp_engine.hp_src, hp_engine.threshold * 100)

            # обновляем оверлей (дешёвые записи в общий state, читает поток tkinter)
            ov_state.auto_loot = auto.on
            ov_state.attack_enabled = mob_det is not None
            ov_state.attack_on = attack.on
            ov_state.attacking = r1_held
            ov_state.flask_on = flask.on
            ov_state.diag = diag.enabled
            ov_state.hp_pct = hp_engine.hp_pct
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
                atk_state = "-"
                if mob_det is not None:
                    atk_state = f"{'ON' if attack.on else 'off'}/{'R1' if r1_held else '-'}"
                    if attack.on and mob_det.last_match:
                        atk_state += ("+boot" if mob_det.last_match.get("bootstrap")
                                      else "+narrow" if mob_det.last_match.get("narrow")
                                      else "+lowhp" if mob_det.last_match.get("low_hp_lock")
                                      else "")
                log.info("targets=%d in_radius=%d auto=%s fg=%s presses=%d hp=%s(%s) streak=%d flasks=%d atk=%s fps=%.0f",
                         len(points), len(in_radius),
                         "ON" if auto.on else "off", foreground, presses,
                         f"{hp_engine.hp_pct*100:.0f}%" if hp_engine.hp_pct is not None else "-",
                         hp_engine.hp_src, hp_engine.low_streak,
                         flasks, atk_state, fps)
                # debug overlay info
                if diag.enabled:
                    hp_pct_s = f"{hp_engine.hp_pct*100:.0f}%" if hp_engine.hp_pct is not None else "-"
                    ov_state.debug_hp = f"HP={hp_pct_s} src={hp_engine.hp_src} streak={hp_engine.low_streak}"
                    if mob_det is not None and hasattr(mob_det, 'last_match') and mob_det.last_match:
                        m = mob_det.last_match
                        kind = "boot" if m.get("bootstrap") else "narrow" if m.get("narrow") else "lowhp" if m.get("low_hp_lock") else "strict"
                        ov_state.debug_mob = f"mob=CV kind={kind} x={m.get('x',0)} w={m.get('w',0)} aspect={m.get('aspect',0):.1f}"
                    elif use_memory and mob_det is not None:
                        ov_state.debug_mob = f"mob=MEM hostile={mob_det.stats.get('hostile_in_range',0)} total={mob_det.stats.get('total_entities',0)}"
                    else:
                        ov_state.debug_mob = "mob=—"
                    ov_state.debug_zone = f"range={atk_cfg.get('attack_range',15):.0f} dark={'on' if (mob_det and hasattr(mob_det, '_is_dark_zone') and mob_det._is_dark_zone(full)) else '—'}"
                last_status = now
                iters = 0

            time.sleep(0.01)
    except KeyboardInterrupt:
        log.info("Прервано (Ctrl+C)")
    finally:
        attack_worker.stop()
        attack_worker.join(timeout=1.0)
        if use_memory and mob_det is not None and isinstance(mob_det, MemoryMobDetector):
            mob_det.disconnect()
        loot_worker.stop()
        loot_worker.join(timeout=1.0)
        listener.stop()
        overlay.stop()
        bridge.stop()
        vx.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
