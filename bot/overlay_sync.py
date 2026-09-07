"""Overlay state sync — update ov_state fields from current bot state."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.state import BotState

from mem_detect import MemoryMobDetector
from reader_detect import ReaderMobDetector
from entity_reader import EntityCategory


def sync_overlay(s: BotState) -> None:
    """Update ov_state from current bot state. Called every frame in main loop."""
    ov = s.ov_state
    ov.auto_loot = s.auto.on
    ov.attack_enabled = s.mob_det is not None
    ov.attack_on = s.attack.on
    ov.attacking = s.r1_held
    ov.attack_range = float(getattr(s.mob_det, "attack_range", 0.0) or 0.0)

    # attack mode label
    is_mem = (s.use_memory and s.mob_det is not None
              and isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector))
              and s.mob_det.available)
    is_cv = (s.mob_det is not None
             and not isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector)))
    if is_mem:
        ov.attack_mode = "RDR" if isinstance(s.mob_det, ReaderMobDetector) else "MEM"
    elif is_cv:
        ov.attack_mode = "CV"
    else:
        ov.attack_mode = ""

    ov.flask_on = s.flask.on
    ov.diag = s.diag.enabled

    # HP pct: prefer memory over CV
    ov.hp_pct = s.hp_engine.hp_pct
    if is_mem:
        try:
            _ov_hp, _ = s.mob_det.read_player_vitals()
            if _ov_hp is not None:
                ov.hp_pct = _ov_hp
        except Exception:
            pass
        # Exact pools from reader
        _lc = getattr(s.mob_det, "_player_hp", None)
        if _lc:
            try:
                if len(_lc) >= 4:
                    ov.hp_max, ov.hp_cur = int(_lc[0]), int(_lc[1])
                    ov.es_max = int(_lc[4]) if len(_lc) >= 5 else None
                    ov.es_cur = int(_lc[5]) if len(_lc) >= 6 else None
                    ov.ward_max = int(_lc[6]) if len(_lc) >= 7 else None
                    ov.ward_cur = int(_lc[7]) if len(_lc) >= 8 else None
                else:
                    ov.hp_cur, ov.hp_max = int(_lc[0]), int(_lc[1])
                    ov.es_cur = ov.es_max = None
                    ov.ward_cur = ov.ward_max = None
            except Exception:
                pass

    # Module status dots
    _is_mem_det = (s.mob_det is not None
                   and isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector)))
    _is_rdr = s.mob_det is not None and isinstance(s.mob_det, ReaderMobDetector)
    _mem_conn = bool(getattr(s.mob_det, "_connected", False)) if _is_mem_det else False
    _mem_ok = _is_mem_det and _mem_conn and bool(s.mob_det.available)
    _det_ok = s.mob_det is not None and bool(s.mob_det.available)
    _det_name = "RDR" if _is_rdr else "MEM" if _is_mem_det else "CV"
    _cv_ok = s.hp_engine.hp_pct is not None
    _atk_ok = s.attack.on and _det_ok
    _flask_ok = s.auto_flask.enabled and s.flask.on
    try:
        _nst = s.nav_worker.get_status() if s.nav_worker is not None else None
        _nav_st = _nst.get("status", "idle") if _nst else "выкл"
        _nav_ok = _nst is not None and _nav_st not in ("error",)
    except Exception:
        _nav_st, _nav_ok = "?", False
    _pad_ok = getattr(s.vx, "_pad", None) is not None
    _ds_ok = getattr(s.bridge, "_dev", None) is not None
    ov.modules = [
        ("ReaderBridge", _mem_ok, "ok" if _mem_ok else ("no proc" if _is_mem_det else "off")),
        ("MobDetector", _det_ok, f"{_det_name} ok" if _det_ok else ("no data" if s.mob_det is not None else "off")),
        ("HpEngine", _cv_ok, f"src={s.hp_engine.hp_src}" if _cv_ok else "no data"),
        ("AutoAttack", _atk_ok, "ON" if _atk_ok else ("off" if s.attack.on else "OFF")),
        ("AutoFlask", _flask_ok, "ON" if _flask_ok else "OFF"),
        ("AutoLoot", s.auto.on, "ON" if s.auto.on else "OFF"),
        ("CoreMap", _nav_ok, _nav_st),
        ("VirtualXbox", _pad_ok, "ok" if _pad_ok else "no"),
        ("DualSense", _ds_ok, "ok" if _ds_ok else "no"),
    ]
    ov.foreground = s.win.is_foreground() if s.win.hwnd else True
    ov.flasks = s.flasks
    ov.presses = s.presses

    # Memory status + entity counts
    if s.atk_enabled and s.atk_cfg.get("use_memory", True):
        mem_ok = (s.use_memory and s.mob_det is not None
                  and isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector))
                  and s.mob_det.available)
        ov.memory_status = "ok" if mem_ok else "fallback"
        if mem_ok:
            # Авто-реконнект удался — убираем RDR-fallback из оверлея.
            if getattr(s, "_atk_fallback_reason", "").startswith("RDR"):
                s._atk_fallback_reason = ""
            ov.atk_fallback = ""
        if s.mob_det is not None and isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector)):
            ov.mem_connected = bool(getattr(s.mob_det, "_connected", False))
            ov.mem_reading = bool(s.mob_det.available)
            try:
                stats = getattr(s.mob_det, "stats", {})
                ov.ent_total = stats.get("total_entities", 0)
                all_hostiles = s.mob_det.get_all_hostiles() if hasattr(s.mob_det, "get_all_hostiles") else []
                ov.ent_hostile = len(all_hostiles)
                ov.ent_in_range = stats.get("hostile_in_range", 0)
                if all_hostiles:
                    ov.nearest_dist = min(e.distance for e in all_hostiles)
                else:
                    ov.nearest_dist = None
            except Exception:
                ov.ent_total = 0
                ov.ent_hostile = 0
                ov.ent_in_range = 0
                ov.nearest_dist = None
        else:
            ov.mem_connected = False
            ov.mem_reading = False
            ov.ent_total = 0
            ov.ent_hostile = 0
            ov.ent_in_range = 0
            ov.nearest_dist = None
    else:
        ov.memory_status = "off"
        ov.mem_connected = False
        ov.mem_reading = False
        ov.ent_total = 0
        ov.ent_hostile = 0
        ov.ent_in_range = 0
        ov.nearest_dist = None

    # Locate state
    if s.hp_enabled and s.num_cfg.get("enabled") and s.num_cfg.get("auto_locate"):
        ov.locate_state = "ok" if s.located else "search"
        ov.locate_info = s.loc_info["text"]
    else:
        ov.locate_state = "off"
    if is_mem:
        try:
            _lc = getattr(s.mob_det, "_player_hp", None)
            if _lc and len(_lc) >= 2 and _lc[0]:
                ov.locate_state = "ok"
                ov.locate_info = f"Life {_lc[1]}/{_lc[0]} (mem)"
            elif ov.hp_pct is not None:
                ov.locate_state = "ok"
                ov.locate_info = f"HP {ov.hp_pct*100:.0f}% (mem)"
        except Exception:
            pass
