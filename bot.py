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
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from pynput import keyboard

from config import load_config, save_config
from screen import ScreenCapture
from window import GameWindow
from hp_detector import HPDetector
from hp_engine import HPEngine, HPDecision
from hp_ocr import HPNumReader
from hp_locate import find_digits_roi, derive_orb
from mob_bar import MobBarDetector
from zone_detector import ZoneDetector
from mem_detect import MemoryMobDetector
from reader_detect import ReaderMobDetector, TargetStatus
from entity_reader import EntityCategory
from diag_dump import HPDiag, dump_loot, dump_attack_trigger
from overlay import Overlay, OverlayState
from auto_flask import AutoFlaskEngine, FlaskRule, FlaskState, FlaskDecision, default_rules
from virtual_xbox import VirtualXbox360, XUSB
from dualsense_bridge import DualSenseBridge
from utils import Toggle, build_detector, build_loot_engine
from nav_worker import NavWorker
from game_memory import GameMemory as _GM
from stash_flow import open_maps_tab as _open_maps_tab, summarize_items as _summarize, list_stash_tabs as _list_stash_tabs, filter_maps as _filter_maps, read_all_maps as _read_all_maps
from map_run_flow import run_map_flow as _run_map_flow
from workers import LootWorker, AttackWorker

LOG_PATH = os.path.join(os.path.dirname(__file__), "bot.log")

_fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")


def _setup_logging() -> None:
    """Настроить корневой логгер: консоль + файл (ротация 3 × 1 МБ).

    Вызывается из main() — импорт bot.py не создаёт файл лога (чистый импорт).
    Повторные вызовы безопасны (handlers уже настроены — пропускаем).
    """
    _root = logging.getLogger()
    if _root.handlers:
        return
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


def main() -> int:
    _setup_logging()
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
    engine = build_loot_engine(loot)
    r_desc = (f"frac={loot['radius_frac']}" if loot.get("radius_frac", 0) > 0
              else f"{loot['pickup_radius_px']}px")
    log.info("Детектор: цвета=%s радиус=%s кулдаун=%dмс",
             det.colors, r_desc, loot["cooldown_ms"])

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
    # не вышло → работаем по ROI из конфига и продолжаем искать в фоне.
    located = False
    if hp_enabled and num_cfg.get("enabled") and num_cfg.get("auto_locate"):
        for _ in range(5):                       # ~до 0.5с на старте (полный скан кэшируется)
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
    # detect_mode: "reader" (встроенный C# ридер, ОДИН процесс — pythonnet) |
    # "memory" (PROCESS_VM_READ, старый external) |
    # False/"cv" (CV-детект полосы HP)
    # Backward compat: True → "memory", False → "cv"
    _raw_mode = atk_cfg.get("use_memory", "reader")
    if _raw_mode is True or _raw_mode == "memory":
        detect_mode = "memory"
    elif _raw_mode == "reader":
        detect_mode = "reader"
    else:
        detect_mode = "cv"
    use_memory = detect_mode in ("memory", "reader")  # bool для совместимости

    if detect_mode == "reader" and atk_enabled:
        # Reader bridge: встроенный C# ридер через pythonnet (ОДИН процесс, без TCP).
        # bot.py сам читает память PoE2 — не нужен отдельный процесс.
        mob_det = ReaderMobDetector({
            **atk_cfg,
            # SQLite телеметрия мобов (полный дамп по кадрам для дебага/аналитики).
            "monster_db": cfg.get("monster_db", {}),
        })
        # CV НЕ используется: «живость» мобов определяется в reader_detect по
        # Life-vtable (vtable != 0 = живой; трупы/латентные отсекаются при парсинге).
        cv_det = None
        if not mob_det.connect():
            log.warning("Reader detection: не удалось инициализировать bridge "
                        "(PoE2 не запущен / нет admin / нет PoE2ReaderLib.dll?) — "
                        "откат на CV-детект. Пересобери: "
                        "cd C:\\Projects\\poe2-reader && dotnet build lib\\PoE2ReaderLib.csproj -c Release")
            mob_det = None
            use_memory = False
            detect_mode = "cv"
            _atk_fallback_reason = "Reader → CV: нет инициализации bridge"
        else:
            _atk_fallback_reason = ""
            log.info("Reader detection: bridge подключён (pid=%d)" % mob_det.bridge.pid)
    elif detect_mode == "memory" and atk_enabled:
        # Memory-based: чтение памяти PoE2 (PROCESS_VM_READ, старый external)
        mob_det = MemoryMobDetector({"attack_range": atk_cfg.get("attack_range", 15.0)})
        # CV fallback — строгие пороги, чтобы не ловить торчи/баннеры
        cv_cfg = {**atk_cfg, "min_aspect": 5.0, "min_run_frac": 0.025, "min_fill": 0.55,
                  "narrow_enabled": False, "bootstrap_enabled": False, "low_hp_lock_timeout_s": 0.0}
        cv_det = MobBarDetector(cv_cfg)
        if not mob_det.connect():
            log.warning("Memory detection: не удалось подключиться к PoE2 — "
                        "откат на CV-детект")
            mob_det = None
            cv_det = None
            use_memory = False
            detect_mode = "cv"
            _atk_fallback_reason = "Memory → CV: нет подключения"
        elif not mob_det.available:
            log.warning("Memory detection: подключён, но AOB scan не нашёл "
                        "игровые структуры (оффсеты устарели?) — откат на CV-детект")
            mob_det.disconnect()
            mob_det = None
            cv_det = None
            use_memory = False
            detect_mode = "cv"
            _atk_fallback_reason = "Memory → CV: AOB scan failed"
        else:
            _atk_fallback_reason = ""
    else:
        mob_det = None
        cv_det = None
        use_memory = False
        detect_mode = "cv"
        _atk_fallback_reason = ""

    if detect_mode == "cv" and atk_enabled:
        # CV-based fallback: детект полосы HP моба по цвету
        mob_det = MobBarDetector(atk_cfg)

    zone_det = None  # memory/reader detector делает in_hideout() сам
    if detect_mode == "cv" and atk_enabled:
        zone_det = ZoneDetector(atk_cfg.get("zone", {}))

    atk_btn = XUSB.get(atk_cfg.get("button", "right_bumper"), XUSB["right_bumper"])
    atk_grace = atk_cfg.get("release_grace_s", 0.6)

    # attack-детект в отдельном потоке
    attack_worker = AttackWorker(mob_det, zone_det, use_memory=use_memory, cv_det=cv_det)
    if atk_enabled:
        attack_worker.start()
        mode = {"memory": "MEMORY", "reader": "READER", "cv": "CV"}.get(detect_mode, "CV")
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

    # --- auto-flask engine (memory-based) ---
    af_cfg = cfg.get("auto_flask", {})
    af_enabled = af_cfg.get("enabled", True) and use_memory
    auto_flask = AutoFlaskEngine()
    if af_enabled:
        for rule_cfg in af_cfg.get("rules", default_rules()):
            auto_flask.add_rule(FlaskRule(
                name=rule_cfg.get("name", "Unnamed"),
                key=rule_cfg.get("key", "left_trigger"),
                hp_below=rule_cfg.get("hp_below"),
                mana_below=rule_cfg.get("mana_below"),
                enemies_nearby=rule_cfg.get("enemies_nearby"),
                enemy_radius=rule_cfg.get("enemy_radius", 30.0),
                cooldown_s=rule_cfg.get("cooldown_s", 1.0),
                enabled=rule_cfg.get("enabled", True),
            ))
    auto_flask.enabled = af_enabled
    if af_enabled:
        log.info("Auto-flask MEMORY: %d правил", len(auto_flask.rules))
    stop = threading.Event()
    k_toggle = cfg["hotkeys"].get("toggle", "f8")
    k_quit = cfg["hotkeys"].get("quit", "f12")
    k_diag = cfg["hotkeys"].get("diag", "f7")
    k_shot = cfg["hotkeys"].get("snapshot", "f9")
    k_overlay = cfg["hotkeys"].get("overlay", "f6")
    k_attack = cfg["hotkeys"].get("attack", "f2")
    k_flask = cfg["hotkeys"].get("flask", "f3")
    k_radial = cfg["hotkeys"].get("radial", "f4")
    k_reload = "f5"
    k_nav = cfg["hotkeys"].get("nav", "f1")
    k_stash = cfg["hotkeys"].get("stash", "f11")
    k_map_run = cfg["hotkeys"].get("map_run", "f10")

    # --- навигация (Core Mapping) ---
    nav_enabled = bool(cfg.get("nav", {}).get("enabled", False))
    nav_worker = None
    if nav_enabled:
        try:
            nav_worker = NavWorker(vx, cfg=cfg.get("nav", {}))
            nav_worker.start()
            log.info("Навигация (Core Map) ВКЛ — цель из nav_target.json (хоткей %s)",
                     k_nav.upper())
        except Exception as e:
            log.error("Навигация не запустилась: %s", e)
            nav_worker = None

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
        engine = build_loot_engine(loot)
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
        # параметры круга радиуса (F4) — применяются на лету
        rad_cfg = cfg.get("radial", {})
        ov_state.radial_scale = float(rad_cfg.get("scale_px_per_grid", 20.0))
        off = rad_cfg.get("center_offset_xy", [0, -40])
        ov_state.radial_center = (float(off[0]), float(off[1]))
        old_mob_det = mob_det
        cv_det = None
        if use_memory and atk_enabled:
            if mob_det is None or not isinstance(mob_det, (MemoryMobDetector, ReaderMobDetector)):
                mob_det = MemoryMobDetector({"attack_range": atk_cfg.get("attack_range", 15.0)})
                mob_det.connect()
            else:
                mob_det.attack_range = atk_cfg.get("attack_range", 15.0)
                mob_det.attack_range_sq = mob_det.attack_range ** 2
                if isinstance(mob_det, ReaderMobDetector):
                    mob_det.ignore_static = bool(atk_cfg.get("ignore_static_mobs", True))
                    mob_det.static_grace_s = float(atk_cfg.get("static_grace_s", 2.0))
            cv_cfg = {**atk_cfg, "min_aspect": 5.0, "min_run_frac": 0.025, "min_fill": 0.55,
                      "narrow_enabled": False, "bootstrap_enabled": False, "low_hp_lock_timeout_s": 0.0}
            cv_det = MobBarDetector(cv_cfg)
        elif atk_enabled:
            mob_det = MobBarDetector(atk_cfg)
        else:
            mob_det = None
        # закрыть handle старого memory-детектора если режим сменился
        if old_mob_det is not None and isinstance(old_mob_det, (MemoryMobDetector, ReaderMobDetector)):
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
        attack_worker.update_config(mob_det, zone_det, use_memory, cv_det)
        log.info("Config перезагружен (F5): детектор=%s порог=%.0f%% кулдаун=%dмс",
                 det.colors, hp_threshold * 100, loot["cooldown_ms"])

    # --- оверлей со статусами (поверх игры, click-through) ---
    ov_state = OverlayState()
    ov_state.flask_enabled = hpg["hp_det"] is not None
    rad_cfg = cfg.get("radial", {})
    ov_state.radial_scale = float(rad_cfg.get("scale_px_per_grid", 20.0))
    off = rad_cfg.get("center_offset_xy", [0, -40])
    ov_state.radial_center = (float(off[0]), float(off[1]))
    ov_state.attack_range = atk_cfg.get("attack_range", 0.0)
    overlay = Overlay(ov_state, region, cfg["hotkeys"])
    overlay.start()
    if _atk_fallback_reason:
        ov_state.notification = _atk_fallback_reason
        ov_state.notification_until = time.time() + 5.0
        ov_state.atk_fallback = _atk_fallback_reason

    # --- stash flow state (F11) ---
    _stash_running = {"on": False}
    _stash_lock = threading.Lock()

    def _stash_task() -> None:
        """Открыть вкладку MAPS и залогировать результат. Отдельный процесс-хэндл памяти
        (наш StashReader использует GameMemory с ReadProcessMemory — без конфликта с
        reader_bridge, который владеет pythonnet). Переключение R1/L1 — через тот же vx,
        что в bot.py main (DualSenseBridge → vx → XInput-слот PoE2)."""
        with _stash_lock:
            if _stash_running["on"]:
                return
            _stash_running["on"] = True
        try:
            mem = _GM()
            if not mem.connect():
                log.warning("Stash: GameMemory.connect() failed — PoE2 pid не найден")
                ov_state.notification = "Stash: не подключиться к PoE2"
                ov_state.notification_until = time.time() + 4.0
                return
            try:
                # сначала: какие вообще вкладки есть
                names = _list_stash_tabs(mem, vx)
                if names:
                    log.info("Stash tabs (%d): %s", len(names), ", ".join(names))
                # открываем MAPS — получаем ВСЕ items одним ItemView-scan (а не двумя)
                ok, msg, items = _open_maps_tab(mem, vx)
                main_maps = _filter_maps(items) if items else None
                log.info("Stash flow: ok=%s msg=%s items=%s main_tab_maps=%s",
                         ok, msg,
                         len(items) if items else 0,
                         len(main_maps) if main_maps else 0)
                if items:
                    log.info("Stash MAPS open tab dump: %s", _summarize(items))
                # отдельно — все карты из подвкладок MAPS (12×8 subtabs, разных tier)
                all_maps = _read_all_maps(mem)
                log.info("Stash all subtabs: total maps=%d (across %d subtabs)",
                         len(all_maps), len({m.container for m in all_maps}))
                if all_maps:
                    # group by tier (MapKeyTierNN)
                    tiers: dict = {}
                    for m in all_maps:
                        tier = m.path.split("MapKeyTier")[-1] if "MapKeyTier" in m.path else "?"
                        tiers[tier] = tiers.get(tier, 0) + 1
                    tiers_s = ", ".join("Tier%s=%d" % (t, n) for t, n in sorted(tiers.items()))
                    ov_state.notification = "Maps: %d (%s)" % (len(all_maps), tiers_s)
                    ov_state.notification_until = time.time() + 8.0
                    log.info("=== maps across all subtabs (total=%d, tiers: %s) ===",
                             len(all_maps), tiers_s)
                    for m in all_maps[:30]:
                        log.info("  MAP id=%d container=0x%X (%d,%d) %dx%d %s",
                                 m.id, m.container, m.grid_x, m.grid_y,
                                 m.width, m.height, m.path)
                elif main_maps:
                    ov_state.notification = "Maps: %d (main tab)" % len(main_maps)
                    ov_state.notification_until = time.time() + 6.0
                else:
                    ov_state.notification = "MAPS открыта, 0 карт (см. лог)"
                    ov_state.notification_until = time.time() + 6.0
            finally:
                mem.disconnect()
        finally:
            with _stash_lock:
                _stash_running["on"] = False

    def trigger_stash() -> None:
        if _stash_running["on"]:
            log.info("Stash занят, пропускаем F11")
            return
        threading.Thread(target=_stash_task, daemon=True).start()

    # ---------------- F10: map run flow (one-shot) ----------------
    _map_run_running = {"on": False}
    _map_run_lock = threading.Lock()

    def _map_run_task() -> None:
        """Полный цикл one-shot map-run (чистый VX360 flow, v2):
        1) подойти к сташу → tap(A) открыть → R1/L1 до MAPS → D-pad до Tier N
           → left stick на waystone → tap(Y) → в инвентарь → tap(B) закрыть
        2) пройти к Map Device entity
        3) tap(A) — открыть Atlas overview
        4) left stick на голубую точку → tap(A) → info panel
        5) left stick на waystone в инвентаре → tap(X) → слот Map Device
        6) left stick на светящуюся TRAVERSE → tap(A) → запуск карты

        Конфиг: cfg["map_run"] (target_tier, fallback_below, enter_portal_after,
        map_device_world, stash_world, bindings{тайминги/калибровка}).

        Использует собственный GameMemory (как _stash_task) — отдельный pythonnet
        не открывает, только reading memory через Win32 ReadProcessMemory.
        Использует vx из bot.py (владеет XInput-слотом), мышь НЕ используется —
        весь ввод через виртуальный геймпад.
        """
        with _map_run_lock:
            if _map_run_running["on"]:
                return
            _map_run_running["on"] = True
        try:
            mrcfg = cfg.get("map_run", {})
            target_tier = int(mrcfg.get("target_tier", 16))
            enter_portal_after = bool(mrcfg.get("enter_portal_after", False))
            map_device_world = mrcfg.get("map_device_world", None)
            if map_device_world and len(map_device_world) == 2:
                map_device_world = tuple(map_device_world)
            else:
                map_device_world = None
            stash_world = mrcfg.get("stash_world", None)
            if stash_world and len(stash_world) == 2:
                stash_world = tuple(stash_world)
            else:
                stash_world = None
            bindings = mrcfg.get("bindings", {}) or {}

            # overlay notification начинаем
            ov_state.notification = "Map run: Tier %d..." % target_tier
            ov_state.notification_until = time.time() + 30.0

            # retra за кадром лог — отдельный mem
            mem = _GM()
            if not mem.connect():
                log.warning("MapRun: GameMemory.connect() failed — PoE2 pid не найден")
                ov_state.notification = "MapRun: нет PoE2"
                ov_state.notification_until = time.time() + 4.0
                return
            try:
                # выполним
                def _progress(phase: str, status: str) -> None:
                    log.info("MapRun[%s] %s", phase, status)
                    if "FAIL" in status:
                        ov_state.notification = "MapRun: %s" % status
                        ov_state.notification_until = time.time() + 6.0
                ok, msg = _run_map_flow(
                    mem, vx,
                    target_tier=target_tier,
                    map_device_world=map_device_world,
                    stash_world=stash_world,
                    enter_portal_after=enter_portal_after,
                    bindings=bindings,
                    progress_cb=_progress,
                )
                log.info("MapRun result ok=%s msg=%s", ok, msg)
                ov_state.notification = ("MapRun: OK %s" % msg if ok
                                          else "MapRun FAIL: %s" % msg)[:120]
                ov_state.notification_until = time.time() + 8.0
            finally:
                mem.disconnect()
        finally:
            with _map_run_lock:
                _map_run_running["on"] = False

    def trigger_map_run() -> None:
        if _map_run_running["on"]:
            log.info("MapRun занят, пропускаем F10")
            return
        threading.Thread(target=_map_run_task, daemon=True).start()

    def on_press(key) -> Optional[bool]:
        name = getattr(key, "name", None)
        if name == k_quit:
            log.info("Выход (F12)")
            stop.set()
            return False
        if name == k_stash:
            trigger_stash()
            return
        if name == k_map_run:
            trigger_map_run()
            return
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
        if name == k_radial:
            ov_state.radial_visible = not ov_state.radial_visible
            log.info("Круг радиуса: %s (R=%.0f, scale=%.1f px/grid)",
                     "показан" if ov_state.radial_visible else "скрыт",
                     ov_state.attack_range, ov_state.radial_scale)
        if name == k_reload:
            reload_cfg()
        if name == k_nav and nav_worker is not None:
            if nav_worker.is_active():
                nav_worker.clear_target()
                log.info("Навигация: остановлена")
            else:
                # цель из nav_target.json (если есть) — resume
                tgt = nav_worker._read_target_file()
                if tgt:
                    nav_worker.set_target(*tgt)
                    log.info("Навигация: возобновлена к (%.1f, %.1f)", tgt[0], tgt[1])
                else:
                    log.info("Навигация: нет цели в nav_target.json")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    log.info("Готово. %s — подбор, %s — авто-атака, %s — авто-фляска, %s — диагностика, "
             "%s — снимок, %s — оверлей, %s — круг радиуса, %s — перезагрузка, %s — выход, "
             "%s — stash MAPS (R1/L1 + чтение карт).",
             k_toggle.upper(), k_attack.upper(), k_flask.upper(), k_diag.upper(),
             k_shot.upper(), k_overlay.upper(), k_radial.upper(), k_reload.upper(),
             k_quit.upper(), k_stash.upper())

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
            # В memory/reader-режиме сабмитим ВСЕГДА в фокусе (даже без attack.on),
            # чтобы счётчики сущностей в оверлее обновлялись постоянно.
            # В CV-режиме — только при attack.on (CV на каждый кадр дорогой).
            if mob_det is not None and foreground and (attack.on or use_memory):
                try:
                    # Всегда сабмитим фрейм — нужен для CV fallback
                    attack_worker.submit(full, foreground)
                    atk_result = attack_worker.get_result()
                    if atk_result is not None:
                        if atk_result["in_hideout"]:
                            if r1_held and time.perf_counter() - last_bar_seen > atk_grace:
                                vx.release(atk_btn)
                                r1_held = False
                        elif atk_result["mob_present"] and attack.on:
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
                # CV-фляга: пропускаем, если HP/Mana ведёт memory auto_flask
                memory_flask_active = (
                    auto_flask.enabled and use_memory
                    and mob_det is not None and isinstance(mob_det, (MemoryMobDetector, ReaderMobDetector))
                    and mob_det.available
                )
                if (flask.on and not memory_flask_active
                        and hp_engine.decision == HPDecision.LOW
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

            # --- AUTO-FLASK: memory-based rules (HP%, Mana%, enemies) ---
            if (auto_flask.enabled and flask.on and use_memory
                    and mob_det is not None and isinstance(mob_det, (MemoryMobDetector, ReaderMobDetector))
                    and mob_det.available and foreground and not mob_det.in_hideout()
                    and time.perf_counter() - last_flask >= hp_cooldown):
                hp_pct, mana_pct = mob_det.read_player_vitals()
                enemies = len(mob_det.get_hostiles_in_range())

                af_state = FlaskState(
                    hp_pct=hp_pct,
                    mana_pct=mana_pct,
                    enemies_in_range=enemies,
                    foreground=foreground,
                    in_hideout=mob_det.in_hideout(),
                )
                af_decision = auto_flask.evaluate(af_state)
                if af_decision.should_press:
                    key_name = af_decision.rule.key
                    _btn = XUSB.get(key_name, XUSB.get("left_trigger"))
                    bridge.suppress_triggers = True
                    def _do_af_flask(btn=_btn):
                        vx.tap(btn, hold_ms=flask_hold)
                        bridge.suppress_triggers = False
                    threading.Thread(target=_do_af_flask, daemon=True).start()
                    auto_flask.mark_used(af_decision.rule)
                    last_flask = time.perf_counter()
                    flasks += 1
                    log.info("Auto-flask: %s", af_decision.reason)

            # обновляем оверлей (дешёвые записи в общий state, читает поток tkinter)
            ov_state.auto_loot = auto.on
            ov_state.attack_enabled = mob_det is not None
            ov_state.attack_on = attack.on
            ov_state.attacking = r1_held
            ov_state.attack_range = float(getattr(mob_det, "attack_range", 0.0) or 0.0)
            is_mem = (use_memory and mob_det is not None
                      and isinstance(mob_det, (MemoryMobDetector, ReaderMobDetector))
                      and mob_det.available)
            is_cv = (mob_det is not None
                     and not isinstance(mob_det, (MemoryMobDetector, ReaderMobDetector)))
            if is_mem:
                if isinstance(mob_det, ReaderMobDetector):
                    ov_state.attack_mode = "RDR"
                else:
                    ov_state.attack_mode = "MEM"
            elif is_cv:
                ov_state.attack_mode = "CV"
            else:
                ov_state.attack_mode = ""
            ov_state.flask_on = flask.on
            ov_state.diag = diag.enabled
            ov_state.hp_pct = hp_engine.hp_pct
            ov_state.foreground = foreground
            ov_state.flasks = flasks
            ov_state.presses = presses
            if atk_enabled and atk_cfg.get("use_memory", True):
                mem_ok = (use_memory and mob_det is not None
                          and isinstance(mob_det, (MemoryMobDetector, ReaderMobDetector))
                          and mob_det.available)
                ov_state.memory_status = "ok" if mem_ok else "fallback"
                if mem_ok:
                    ov_state.atk_fallback = ""
                # Индикаторы модуля памяти для нижней строки оверлея:
                #   mem_connected — детектор создан и подключился к процессу PoE2
                #   mem_reading   — память реально читается (available = game_state != 0)
                if mob_det is not None and isinstance(mob_det, (MemoryMobDetector, ReaderMobDetector)):
                    ov_state.mem_connected = bool(getattr(mob_det, "_connected", False))
                    ov_state.mem_reading = bool(mob_det.available)
                    # Счётчики сущностей для оверлея
                    try:
                        stats = getattr(mob_det, "stats", {})
                        ov_state.ent_total = stats.get("total_entities", 0)
                        all_hostiles = mob_det.get_all_hostiles() if hasattr(mob_det, "get_all_hostiles") else []
                        ov_state.ent_hostile = len(all_hostiles)
                        ov_state.ent_in_range = stats.get("hostile_in_range", 0)
                        # nearest distance до ближайшего враждебного моба
                        if all_hostiles:
                            ov_state.nearest_dist = min(e.distance for e in all_hostiles)
                        else:
                            ov_state.nearest_dist = None
                    except Exception:
                        ov_state.ent_total = 0
                        ov_state.ent_hostile = 0
                        ov_state.ent_in_range = 0
                        ov_state.nearest_dist = None
                else:
                    ov_state.mem_connected = False
                    ov_state.mem_reading = False
                    ov_state.ent_total = 0
                    ov_state.ent_hostile = 0
                    ov_state.ent_in_range = 0
                    ov_state.nearest_dist = None
            else:
                ov_state.memory_status = "off"
                ov_state.mem_connected = False
                ov_state.mem_reading = False
                ov_state.ent_total = 0
                ov_state.ent_hostile = 0
                ov_state.ent_in_range = 0
                ov_state.nearest_dist = None
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
                # Навигация (Core Map)
                if nav_worker is not None:
                    nst = nav_worker.get_status()
                    ns = nst.get("status", "idle")
                    nt = nst.get("target")
                    nt_s = f"(%.0f,%.0f)" % nt if nt else "-"
                    log.info("NAV: %s target=%s wp=%d/%d obs=%d err=%s",
                             ns, nt_s, nst.get("wp_idx", 0), nst.get("path_len", 0),
                             nst.get("obstacles", 0), nst.get("error", ""))
                # Диагностика сущностей для memory-режима: total/hostile/in_range/nearest
                if mob_det is not None and isinstance(mob_det, (MemoryMobDetector, ReaderMobDetector)):
                    try:
                        _ah = mob_det.get_all_hostiles()
                        _nd = min((e.distance for e in _ah), default=None)
                        _nd_s = f"{_nd:.0f}" if _nd is not None else "-"
                        log.info("ENT: total=%d hostile=%d in_range=%d nearest=%s range=%.1f",
                                 ov_state.ent_total, len(_ah), ov_state.ent_in_range,
                                 _nd_s, mob_det.attack_range)
                        if isinstance(mob_det, ReaderMobDetector):
                            _ts = getattr(mob_det, "stats", {}).get("target_status", {})
                            # _player_fire_time пишется через time.perf_counter()
                            # (см. reader_detect._finalize_tracks) — сравнивать
                            # только с perf_counter, иначе мусорный «возраст»
                            # (был 1.7e9 c при time.time() - perf_counter()).
                            _fire = getattr(mob_det, "_player_fire_time", 0.0)
                            _age = (time.perf_counter() - _fire) if _fire else -1
                            log.info("ENTD: status=%s fire_age=%.1fs player=%s",
                                     _ts, _age, getattr(mob_det, "_player_pos", None))
                            _near = [e for e in mob_det._last_entities
                                     if e.category == EntityCategory.MONSTER and not e.is_friendly
                                     and e.distance < 70]
                            _near.sort(key=lambda e: e.distance)
                            _st_by_id = getattr(mob_det, "_status_by_id", {})
                            for e in _near[:6]:
                                # Классификация уже посчитана в update() и лежит
                                # в _status_by_id — не зовём дорогой _classify_target
                                # повторно (это и есть смысл кэша).
                                _st = _st_by_id.get(e.id, "?")
                                _tr = mob_det._static_track.get(e.id, {})
                                log.info("ENTD   d=%5.1f st=%-11s hp=%d/%d reach=%s | %s",
                                         e.distance, _st, e.hp_current, e.hp_max,
                                         e.is_reachable, e.metadata[-44:])
                    except Exception:
                        pass
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
        if nav_worker is not None:
            nav_worker.stop()
            nav_worker.join(timeout=1.0)
            log.info("Навигация остановлена")
        if use_memory and mob_det is not None and isinstance(mob_det, (MemoryMobDetector, ReaderMobDetector)):
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
