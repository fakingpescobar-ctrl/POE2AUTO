"""HP-related functions: geometry rebuild, HUD locate, flask execution."""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.state import BotState

from hp_detector import HPDetector
from hp_ocr import HPNumReader
from hp_locate import find_digits_roi, derive_orb

log = logging.getLogger("poe2auto.bot.hp")


def rebuild_hp_geometry(s: BotState) -> None:
    """(Пере)собрать всё, что зависит от геометрии орба и ROI цифр."""
    if not s.hp_enabled:
        return
    hp_cfg = s.hp_cfg
    num_cfg = s.num_cfg
    fw, fh = s.fw, s.fh
    region = s.region

    hp_det = HPDetector(hp_cfg)
    ocx = region["left"] + hp_cfg["orb"]["cx"] * fw
    ocy = region["top"] + hp_cfg["orb"]["cy"] * fh
    orp = hp_cfg["orb"]["r"] * fh
    half = orp * 1.25
    rl, rt = region["left"], region["top"]
    ox1, oy1 = max(rl, ocx - half), max(rt, ocy - half)
    ox2, oy2 = min(rl + fw, ocx + half), min(rt + fh, ocy + half)
    sw, sh = ox2 - ox1, oy2 - oy1
    orb_region = {"left": int(ox1), "top": int(oy1), "width": int(sw), "height": int(sh)}
    crop_cfg = dict(hp_cfg)
    crop_cfg["orb"] = {"cx": (ocx - ox1) / sw, "cy": (ocy - oy1) / sh, "r": orp / sh}
    hp_det_crop = HPDetector(crop_cfg)

    num_reader = None
    num_reader_crop = None
    if num_cfg.get("enabled"):
        nr = HPNumReader(num_cfg)
        if not nr.ready:
            log.warning("hp_num включён, но цифры не откалиброваны "
                        "(запусти calibrate_hp_digits.py) — работаю по орбу")
        else:
            num_reader = nr
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

    s.hpg.update(
        hp_det=hp_det, hp_det_crop=hp_det_crop,
        orb_region=orb_region,
        num_reader=num_reader, num_reader_crop=num_reader_crop,
    )


def try_locate_hud(s: BotState, reason: str) -> bool:
    """Найти строку HP на свежем кадре, переписать ROI и пересобрать геометрию."""
    if not (s.hp_enabled and s.num_cfg.get("enabled") and s.num_cfg.get("auto_locate")):
        return False
    probe = HPNumReader(s.num_cfg)
    if not probe.ready:
        return False
    frame = s.cap.grab(s.region)
    if frame is None:
        return False
    ffh, ffw = frame.shape[:2]
    found = find_digits_roi(frame, probe)
    if not found:
        return False
    if s.hud_anchor["max"] and found["max"] < 0.5 * s.hud_anchor["max"]:
        return False
    s.hud_anchor["max"] = found["max"]
    s.num_cfg["roi"] = found["roi"]
    s.hp_cfg["orb"] = derive_orb(found["text_bbox"], ffw, ffh)
    s.loc_info["text"] = f"{found['cur']}/{found['max']}"
    rebuild_hp_geometry(s)
    log.info("Авто-локализация HUD (%s): HP=%d/%d ROI=%s орб=%s",
             reason, found["cur"], found["max"],
             {k: round(v, 4) for k, v in s.num_cfg["roi"].items()},
             {k: round(v, 4) for k, v in s.hp_cfg["orb"].items()})
    return True


def do_cv_flask(s: BotState) -> None:
    """Execute CV-based flask press (L2/R2) in a thread."""
    s.bridge.suppress_triggers = True
    def _do():
        if s.flask_right:
            s.vx.tap_right_trigger(s.flask_hold)
        else:
            s.vx.tap_left_trigger(s.flask_hold)
        s.bridge.suppress_triggers = False
    threading.Thread(target=_do, daemon=True).start()
    s.last_flask = time.perf_counter()
    s.flasks += 1
    log.info("HP фляга (%s): HP=%s [%s] < %.0f%%",
             "R2" if s.flask_right else "L2",
             f"{s.hp_engine.hp_pct*100:.0f}%" if s.hp_engine.hp_pct is not None else "?",
             s.hp_engine.hp_src, s.hp_engine.threshold * 100)


def do_memory_flask(s: BotState, key_name: str) -> None:
    """Execute memory-based flask press by key name."""
    btn = XUSB.get(key_name, XUSB.get("left_trigger"))
    s.bridge.suppress_triggers = True
    def _do(btn=btn):
        s.vx.tap(btn, hold_ms=s.flask_hold)
        s.bridge.suppress_triggers = False
    threading.Thread(target=_do, daemon=True).start()
    s.last_flask = time.perf_counter()
    s.flasks += 1


def startup_locate(s: BotState) -> None:
    """Try auto-locate HUD on startup (up to 5 attempts)."""
    if not (s.hp_enabled and s.num_cfg.get("enabled") and s.num_cfg.get("auto_locate")):
        rebuild_hp_geometry(s)
        return
    located = False
    for _ in range(5):
        if try_locate_hud(s, "старт"):
            s.located = True
            located = True
            break
        time.sleep(0.1)
    if not located:
        log.info("Авто-локализация HUD на старте не удалась — работаю по ROI из "
                 "конфига, продолжу искать в фоне")
    if not s.located:
        rebuild_hp_geometry(s)


# Import needed at function scope to avoid circular
from virtual_xbox import XUSB
