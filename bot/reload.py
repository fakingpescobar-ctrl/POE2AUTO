"""Hot-reload: перечитать config.yaml и применить безопасные параметры на лету."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.state import BotState

from config import load_config
from hp_engine import HPEngine
from mob_bar import MobBarDetector
from zone_detector import ZoneDetector
from mem_detect import MemoryMobDetector
from reader_detect import ReaderMobDetector
from virtual_xbox import XUSB
from utils import build_detector, build_loot_engine
from bot.hp import rebuild_hp_geometry

log = logging.getLogger("poe2auto.bot.reload")


def reload_cfg(s: BotState) -> None:
    """Hot-reload config.yaml and apply safe parameters on the fly."""
    try:
        new_cfg = load_config()
    except Exception as e:
        log.error("Ошибка чтения config.yaml: %s", e)
        return

    s.cfg = new_cfg
    cfg = s.cfg  # local alias
    s.loot = cfg["loot"]
    hk = cfg.get("hotkeys", {})
    s.k_toggle = hk.get("toggle", "f8")
    s.k_quit = hk.get("quit", "f12")
    s.k_diag = hk.get("diag", "f7")
    s.k_shot = hk.get("snapshot", "f9")
    s.k_overlay = hk.get("overlay", "f6")
    s.k_attack = hk.get("attack", "f2")
    s.k_flask = hk.get("flask", "f3")
    s.k_radial = hk.get("radial", "f4")
    s.k_nav = hk.get("nav", "f1")
    s.k_stash = hk.get("stash", "f11")
    s.k_map_run = hk.get("map_run", "f10")

    # loot: detector + engine
    s.det = build_detector(cfg)
    s.engine = build_loot_engine(s.loot)

    # HP config
    s.hp_cfg = cfg["hp"]
    s.num_cfg = cfg.get("hp_num", {})
    s.resolve_hp_config()
    s.rebuild_hp_engine()
    rebuild_hp_geometry(s)

    # attack config
    s.atk_cfg = cfg.get("auto_attack", {})
    s.resolve_attack_config()

    # radial
    rad_cfg = cfg.get("radial", {})
    s.ov_state.radial_scale = float(rad_cfg.get("scale_px_per_grid", 20.0))
    rc_off = rad_cfg.get("center_offset_xy", [0, -40])
    s.ov_state.radial_center = (float(rc_off[0]), float(rc_off[1]))

    # re-create mob detector if mode changed
    old_mob_det = s.mob_det
    s.cv_det = None
    if s.use_memory and s.atk_enabled:
        if s.mob_det is None or not isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector)):
            if s.detect_mode == "reader":
                s.mob_det = ReaderMobDetector({"attack_range": s.atk_cfg.get("attack_range", 15.0)})
            else:
                s.mob_det = MemoryMobDetector({"attack_range": s.atk_cfg.get("attack_range", 15.0)})
            s.mob_det.connect()
        else:
            s.mob_det.attack_range = s.atk_cfg.get("attack_range", 15.0)
            s.mob_det.attack_range_sq = s.mob_det.attack_range ** 2
            if isinstance(s.mob_det, ReaderMobDetector):
                s.mob_det.ignore_static = bool(s.atk_cfg.get("ignore_static_mobs", True))
                s.static_grace_s = float(s.atk_cfg.get("static_grace_s", 2.0))
        cv_cfg = {**s.atk_cfg, "min_aspect": 5.0, "min_run_frac": 0.025, "min_fill": 0.55,
                  "narrow_enabled": False, "bootstrap_enabled": False, "low_hp_lock_timeout_s": 0.0}
        s.cv_det = MobBarDetector(cv_cfg)
    elif s.atk_enabled:
        s.mob_det = MobBarDetector(s.atk_cfg)
    else:
        s.mob_det = None

    if old_mob_det is not None and isinstance(old_mob_det, (MemoryMobDetector, ReaderMobDetector)):
        if old_mob_det is not s.mob_det:
            try:
                old_mob_det.disconnect()
            except Exception:
                pass

    # zone_det нужен и при use_memory: AttackWorker использует его для CV fallback,
    # пока RDR/MEM не готовы (PoE2 не запущен / не в зоне).
    s.zone_det = ZoneDetector(s.atk_cfg.get("zone", {})) if s.atk_enabled else None
    s.atk_btn = XUSB.get(s.atk_cfg.get("button", "right_bumper"), XUSB["right_bumper"])
    s.atk_grace = s.atk_cfg.get("release_grace_s", 0.6)

    # update workers
    s.loot_worker.update_config(s.det, s.engine)
    s.attack_worker.update_config(s.mob_det, s.zone_det, s.use_memory, s.cv_det)

    log.info("Config перезагружен (F5): детектор=%s порог=%.0f%% кулдаун=%dмс",
             s.det.colors, s.hp_threshold * 100, s.loot["cooldown_ms"])
