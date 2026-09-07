"""Main loop — initialization, event loop, and cleanup."""
from __future__ import annotations

import logging
import time
from typing import Dict, Any

from pynput import keyboard

from bot.state import BotState
from bot.hp import rebuild_hp_geometry, try_locate_hud, startup_locate, do_cv_flask, do_memory_flask
from bot.hotkeys import on_press
from bot.overlay_sync import sync_overlay
from bot.reload import reload_cfg

from config import load_config
from screen import ScreenCapture
from window import GameWindow
from hp_engine import HPDecision
from hp_detector import HPDetector
from hp_ocr import HPNumReader
from hp_locate import find_digits_roi, derive_orb
from mob_bar import MobBarDetector
from zone_detector import ZoneDetector
from mem_detect import MemoryMobDetector
from reader_detect import ReaderMobDetector
from entity_reader import EntityCategory
from diag_dump import dump_loot, dump_attack_trigger
from overlay import Overlay, OverlayState
from auto_flask import AutoFlaskEngine, FlaskRule, FlaskState, default_rules
from virtual_xbox import VirtualXbox360, XUSB
from dualsense_bridge import DualSenseBridge
from utils import Toggle, build_detector, build_loot_engine
from nav_worker import NavWorker
from game_memory import GameMemory as _GM
from stash_flow import (open_maps_tab as _open_maps_tab, summarize_items as _summarize,
                         list_stash_tabs as _list_stash_tabs, filter_maps as _filter_maps,
                         read_all_maps as _read_all_maps)
from map_run_flow import run_map_flow as _run_map_flow
from workers import LootWorker, AttackWorker

log = logging.getLogger("poe2auto.bot")


def main() -> int:
    """Entry point — initialize everything and run the main loop."""
    cfg: Dict[str, Any] = load_config()
    loot = cfg["loot"]
    s = BotState(cfg)

    # ── gamepad ──
    vx = VirtualXbox360()
    if not vx.start():
        log.error("Не удалось создать виртуальный Xbox 360 (ViGEmBus/vgamepad?)")
        return 1
    s.vx = vx
    bridge = DualSenseBridge(vx)
    if not bridge.start():
        log.warning("DualSense не найден — управление персонажем с него не пойдёт")
    s.bridge = bridge

    s.pickup_mask = XUSB.get(loot.get("pickup_button", "a"), XUSB["a"])
    s.hold_ms = loot.get("pickup_hold_ms", 80)

    # ── capture + window ──
    win = GameWindow(cfg["game"]["window_title"])
    if win.find() and (region := win.get_region()):
        log.info("Окно PoE2 найдено: %s", region)
    else:
        region = GameWindow.primary_region()
        log.warning("Окно PoE2 не найдено — захват всего монитора %s. "
                    "Гейт по фокусу отключён.", region)
    s.win = win
    s.region = region
    s.fw, s.fh = region["width"], region["height"]
    cap = ScreenCapture(cfg["capture"]["backend"])
    s.cap = cap
    s.det = build_detector(cfg)
    s.engine = build_loot_engine(loot)
    r_desc = (f"frac={loot['radius_frac']}" if loot.get("radius_frac", 0) > 0
              else f"{loot['pickup_radius_px']}px")
    log.info("Детектор: цвета=%s радиус=%s кулдаун=%dмс",
             s.det.colors, r_desc, loot["cooldown_ms"])

    # ── HP ──
    s.resolve_hp_config()
    s.rebuild_hp_engine()
    startup_locate(s)

    # ── loot region ──
    s.compute_loot_region()
    log.info("Захват лута: ROI %s, центр персонажа в кропе %s", s.loot_region, s.loot_center)

    # ── loot worker ──
    from workers import LootWorker
    s.loot_worker = LootWorker(s.det, s.engine, s.pickup_mask, s.hold_ms, vx, bridge)
    s.loot_worker.start()

    if s.hpg["hp_det"]:
        log.info("Автофляска ВКЛ (%s): порог=%.0f%% кулдаун=%dмс подтв=%d кадра кнопка=%s | быстрый захват орба %s",
                 cfg["hotkeys"].get("flask", "f3").upper(), s.hp_threshold * 100,
                 s.hp_cfg.get("cooldown_ms", 3000), s.hp_confirm,
                 "R2" if s.flask_right else "L2", s.hpg["orb_region"])
        if s.hpg["num_reader"]:
            log.info("Чтение HP по цифрам ВКЛ: ROI %s max_hp=%s",
                     {k: round(v, 4) for k, v in s.num_cfg["roi"].items()}, s.num_cfg.get("max_hp"))
    else:
        log.info("Автофляска выкл (запусти calibrate_hp.bat)")

    # ── attack detection ──
    if s.detect_mode == "reader" and s.atk_enabled:
        s.mob_det = ReaderMobDetector({
            **s.atk_cfg,
            "monster_db": cfg.get("monster_db", {}),
        })
        # CV-параллель: пока RDR не готов (PoE2 не запущен / не в зоне) —
        # AttackWorker использует CV как fallback, читалка сама переподключится.
        cv_cfg = {**s.atk_cfg, "min_aspect": 5.0, "min_run_frac": 0.025, "min_fill": 0.55,
                  "narrow_enabled": False, "bootstrap_enabled": False, "low_hp_lock_timeout_s": 0.0}
        s.cv_det = MobBarDetector(cv_cfg)
        s.zone_det = ZoneDetector(s.atk_cfg.get("zone", {}))
        if not s.mob_det.connect():
            err = getattr(s.mob_det.bridge, "_last_err", "bridge не готов")
            log.warning("Reader: bridge не готов (%s) — CV fallback, RDR переподключится сам", err)
            s._atk_fallback_reason = f"RDR: {err} — CV fallback"
        else:
            s._atk_fallback_reason = ""
            log.info("Reader detection: bridge подключён (pid=%d)" % s.mob_det.bridge.pid)
    elif s.detect_mode == "memory" and s.atk_enabled:
        s.mob_det = MemoryMobDetector({"attack_range": s.atk_cfg.get("attack_range", 15.0)})
        cv_cfg = {**s.atk_cfg, "min_aspect": 5.0, "min_run_frac": 0.025, "min_fill": 0.55,
                  "narrow_enabled": False, "bootstrap_enabled": False, "low_hp_lock_timeout_s": 0.0}
        s.cv_det = MobBarDetector(cv_cfg)
        s.zone_det = ZoneDetector(s.atk_cfg.get("zone", {}))
        if not s.mob_det.connect():
            log.warning("Memory detection: не удалось подключиться — откат на CV")
            s.mob_det = None
            s.cv_det = None
            s.use_memory = False
            s.detect_mode = "cv"
            s._atk_fallback_reason = "Memory → CV: нет подключения"
        elif not s.mob_det.available:
            log.warning("Memory detection: AOB scan не нашёл структуры — откат на CV")
            s.mob_det.disconnect()
            s.mob_det = None
            s.cv_det = None
            s.use_memory = False
            s.detect_mode = "cv"
            s._atk_fallback_reason = "Memory → CV: AOB scan failed"
        else:
            s._atk_fallback_reason = ""
    else:
        s.mob_det = None
        s.cv_det = None
        s.use_memory = False
        s.detect_mode = "cv"
        s._atk_fallback_reason = ""

    if s.detect_mode == "cv" and s.atk_enabled:
        s.mob_det = MobBarDetector(s.atk_cfg)
    if s.detect_mode == "cv" and s.atk_enabled:
        s.zone_det = ZoneDetector(s.atk_cfg.get("zone", {}))

    from workers import AttackWorker
    s.attack_worker = AttackWorker(s.mob_det, s.zone_det, use_memory=s.use_memory, cv_det=s.cv_det)
    if s.atk_enabled:
        s.attack_worker.start()
        mode = {"memory": "MEMORY", "reader": "READER", "cv": "CV"}.get(s.detect_mode, "CV")
        log.info("Авто-атака ВКЛ (%s): кнопка=%s режим=%s",
                 cfg["hotkeys"].get("attack", "f2").upper(),
                 s.atk_cfg.get("button", "right_bumper"), mode)

    # ── auto-flask (memory) ──
    s.init_auto_flask()
    if s.auto_flask.enabled:
        log.info("Auto-flask MEMORY: %d правил", len(s.auto_flask.rules))

    # ── navigation ──
    s.nav_enabled = bool(cfg.get("nav", {}).get("enabled", False))
    if s.nav_enabled:
        try:
            s.nav_worker = NavWorker(vx, cfg=cfg.get("nav", {}))
            s.nav_worker.start()
            log.info("Навигация (Core Map) ВКЛ — цель из nav_target.json (хоткей %s)",
                     s.k_nav.upper())
        except Exception as e:
            log.error("Навигация не запустилась: %s", e)
            s.nav_worker = None

    # ── overlay ──
    s.ov_state.flask_enabled = s.hpg["hp_det"] is not None
    rad_cfg = cfg.get("radial", {})
    s.ov_state.radial_scale = float(rad_cfg.get("scale_px_per_grid", 20.0))
    off = rad_cfg.get("center_offset_xy", [0, -40])
    s.ov_state.radial_center = (float(off[0]), float(off[1]))
    s.ov_state.attack_range = s.atk_cfg.get("attack_range", 0.0)
    overlay = Overlay(s.ov_state, region, cfg["hotkeys"])
    s.overlay = overlay
    overlay.start()
    if s._atk_fallback_reason:
        s.ov_state.notification = s._atk_fallback_reason
        s.ov_state.notification_until = time.time() + 5.0
        s.ov_state.atk_fallback = s._atk_fallback_reason

    # ── hotkeys listener ──
    def _on_press(key):
        return on_press(key, s)

    listener = keyboard.Listener(on_press=_on_press)
    listener.start()
    log.info("Готово. %s — подбор, %s — авто-атака, %s — авто-фляска, %s — диагностика, "
             "%s — снимок, %s — оверлей, %s — круг радиуса, %s — перезагрузка, %s — выход, "
             "%s — stash MAPS.",
             s.k_toggle.upper(), s.k_attack.upper(), s.k_flask.upper(), s.k_diag.upper(),
             s.k_shot.upper(), s.k_overlay.upper(), s.k_radial.upper(), s.k_reload.upper(),
             s.k_quit.upper(), s.k_stash.upper())

    # ══════════════════════════════════════════════════════════════════
    # MAIN LOOP
    # ══════════════════════════════════════════════════════════════════
    try:
        while not s.stop.is_set():
            foreground = s.win.is_foreground() if s.win.hwnd else True

            # ── FULL FRAME CAPTURE ──
            full = cap.grab(region)
            if full is None:
                time.sleep(0.02)
                continue
            full = full.copy()

            # ── HP crop ──
            hp_frame = None
            hp_d, num_rd = s.hpg["hp_det_crop"], s.hpg["num_reader_crop"]
            if s.hpg["orb_region"]:
                orb = s.hpg["orb_region"]
                ox, oy = orb["left"] - region["left"], orb["top"] - region["top"]
                hp_frame = full[oy:oy + orb["height"], ox:ox + orb["width"]]
            if hp_frame is None and s.hp_enabled:
                time.sleep(0.02)
                continue

            # ── Loot crop ──
            loot_frame, loot_ctr = None, None
            if s.auto.on:
                lx = s.loot_region["left"] - region["left"]
                ly = s.loot_region["top"] - region["top"]
                loot_frame = full[ly:ly + s.loot_region["height"],
                                  lx:lx + s.loot_region["width"]]
                loot_ctr = s.loot_center

            # ── ATTACK (background thread) ──
            if s.mob_det is not None and foreground and (s.attack.on or s.use_memory):
                try:
                    s.attack_worker.submit(full, foreground)
                    atk_result = s.attack_worker.get_result()
                    if atk_result is not None:
                        if atk_result["in_hideout"]:
                            if s.r1_held and time.perf_counter() - s.last_bar_seen > s.atk_grace:
                                vx.release(s.atk_btn)
                                s.r1_held = False
                        elif atk_result["mob_present"] and s.attack.on:
                            if not s.r1_held and time.perf_counter() - s.last_atk_dump > 10.0:
                                s.atk_dump_n += 1
                                s.last_atk_dump = time.perf_counter()
                                dump_attack_trigger(s.atk_dump_n, full, s.mob_det, foreground)
                            s.last_bar_seen = time.perf_counter()
                            vx.press(s.atk_btn)
                            s.r1_held = True
                        elif s.r1_held and time.perf_counter() - s.last_bar_seen > s.atk_grace:
                            vx.release(s.atk_btn)
                            s.r1_held = False
                except Exception as e:
                    log.debug("Attack worker error: %s", e)
            elif s.r1_held:
                vx.release(s.atk_btn)
                s.r1_held = False

            # ── LOOT DETECTION (background thread) ──
            points, in_radius = [], []
            if s.auto.on and loot_frame is not None:
                s.loot_worker.submit(loot_frame, loot_ctr, foreground)
                result = s.loot_worker.get_result()
                if result is not None:
                    points, in_radius = result
                    if foreground and in_radius and s.engine.ready():
                        vx.tap(s.pickup_mask, s.hold_ms)
                        s.engine.mark_pressed()
                        s.presses += 1

            # ── HP FLASK (CV + memory) ──
            if hp_d:
                low, hm = hp_d.is_low(hp_frame)
                num = num_rd.read(hp_frame) if num_rd else None
                if num is not None:
                    s.loc_info["text"] = f"{num[0]}/{num[1]}"
                    s.last_num_ok = time.perf_counter()
                s.diag.feed(hp_frame, hp_d, hm, foreground, time.perf_counter())
                if s.manual_shot.is_set():
                    s.manual_shot.clear()
                    s.diag.manual(hp_frame, hp_d, hm, foreground)
                    if s.auto.on and loot_frame is not None:
                        dump_loot(s.diag.n, loot_frame, s.det, s.engine, foreground, loot_ctr)
                s.hp_engine.decide(num, hm, low, foreground)
                if s.hp_engine.need_relocate:
                    log.warning("Цифры HP=%.0f%% при орбе — обрезка ROI, перелокализую",
                                (num[2] * 100) if num else 0)
                    s.located = False
                memory_flask_active = (
                    s.auto_flask.enabled and s.use_memory
                    and s.mob_det is not None and isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector))
                    and s.mob_det.available
                )
                if (s.flask.on and not memory_flask_active
                        and s.hp_engine.decision == HPDecision.LOW
                        and time.perf_counter() - s.last_flask >= s.hp_cooldown):
                    do_cv_flask(s)

            # ── MEMORY FLASK ──
            if (s.auto_flask.enabled and s.flask.on and s.use_memory
                    and s.mob_det is not None and isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector))
                    and s.mob_det.available and foreground and not s.mob_det.in_hideout()
                    and time.perf_counter() - s.last_flask >= s.hp_cooldown):
                hp_pct, mana_pct = s.mob_det.read_player_vitals()
                enemies = len(s.mob_det.get_hostiles_in_range())
                af_state = FlaskState(
                    hp_pct=hp_pct, mana_pct=mana_pct,
                    enemies_in_range=enemies,
                    foreground=foreground,
                    in_hideout=s.mob_det.in_hideout(),
                )
                af_decision = s.auto_flask.evaluate(af_state)
                if af_decision.should_press:
                    do_memory_flask(s, af_decision.rule.key)
                    s.auto_flask.mark_used(af_decision.rule)
                    log.info("Auto-flask: %s", af_decision.reason)

            # ── OVERLAY SYNC ──
            sync_overlay(s)

            # ── STATUS / LOG ──
            s.iters += 1
            now = time.perf_counter()

            # Self-heal: digits not reading ≥3s → re-locate
            if (s.located and s.hp_enabled and s.num_cfg.get("enabled") and s.num_cfg.get("auto_locate")
                    and s.hpg["num_reader"] is not None and foreground
                    and now - s.last_num_ok >= 3.0):
                log.info("Цифры HP не читаются ≥3с — перелокализую HUD")
                s.located = False
                s.last_num_ok = now

            # Background re-locate
            if (not s.located and s.hp_enabled and s.num_cfg.get("enabled")
                    and s.num_cfg.get("auto_locate") and now - s.last_locate >= 3.0):
                s.last_locate = now
                try_locate_hud(s, "фон")

            if now - s.last_status >= 1.0:
                _log_status(s, points, in_radius, now, full)

            time.sleep(0.01)
    except KeyboardInterrupt:
        log.info("Прервано (Ctrl+C)")
    finally:
        s.attack_worker.stop()
        s.attack_worker.join(timeout=1.0)
        if s.nav_worker is not None:
            s.nav_worker.stop()
            s.nav_worker.join(timeout=1.0)
            log.info("Навигация остановлена")
        if s.use_memory and s.mob_det is not None and isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector)):
            s.mob_det.disconnect()
        s.loot_worker.stop()
        s.loot_worker.join(timeout=1.0)
        listener.stop()
        overlay.stop()
        bridge.stop()
        vx.stop()
    return 0


def _log_status(s: BotState, points, in_radius, now: float, full=None) -> None:
    """Log periodic status line + debug info."""
    fps = s.iters / (now - s.last_status)
    s.ov_state.fps = fps
    atk_state = "-"
    if s.mob_det is not None:
        atk_state = f"{'ON' if s.attack.on else 'off'}/{'R1' if s.r1_held else '-'}"
        if s.attack.on and s.mob_det.last_match:
            atk_state += ("+boot" if s.mob_det.last_match.get("bootstrap")
                          else "+narrow" if s.mob_det.last_match.get("narrow")
                          else "+lowhp" if s.mob_det.last_match.get("low_hp_lock")
                          else "")
    mem_hp_s = ""
    if (s.use_memory and s.mob_det is not None
            and isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector))
            and s.mob_det.available):
        try:
            _mh, _mm = s.mob_det.read_player_vitals()
            if _mh is not None:
                mem_hp_s = f" mem={_mh*100:.0f}%"
                if _mm is not None:
                    mem_hp_s += f"/{_mm*100:.0f}%"
        except Exception:
            pass
    log.info("targets=%d in_radius=%d auto=%s fg=%s presses=%d hp=%s(%s)%s streak=%d flasks=%d atk=%s fps=%.0f",
             len(points), len(in_radius),
             "ON" if s.auto.on else "off", s.win.is_foreground() if s.win.hwnd else True,
             s.presses,
             f"{s.hp_engine.hp_pct*100:.0f}%" if s.hp_engine.hp_pct is not None else "-",
             s.hp_engine.hp_src, mem_hp_s,
             s.hp_engine.low_streak,
             s.flasks, atk_state, fps)

    # Navigation
    if s.nav_worker is not None:
        try:
            nst = s.nav_worker.get_status()
            ns = nst.get("status", "idle")
            nt = nst.get("target")
            nt_s = f"(%.0f,%.0f)" % nt if nt else "-"
            log.info("NAV: %s target=%s wp=%d/%d obs=%d err=%s",
                     ns, nt_s, nst.get("wp_idx", 0), nst.get("path_len", 0),
                     nst.get("obstacles", 0), nst.get("error", ""))
        except Exception:
            pass

    # Entity diagnostics (memory mode)
    if s.mob_det is not None and isinstance(s.mob_det, (MemoryMobDetector, ReaderMobDetector)):
        try:
            _ah = s.mob_det.get_all_hostiles()
            _nd = min((e.distance for e in _ah), default=None)
            _nd_s = f"{_nd:.0f}" if _nd is not None else "-"
            log.info("ENT: total=%d hostile=%d in_range=%d nearest=%s range=%.1f",
                     s.ov_state.ent_total, len(_ah), s.ov_state.ent_in_range,
                     _nd_s, s.mob_det.attack_range)
            if isinstance(s.mob_det, ReaderMobDetector):
                _ts = getattr(s.mob_det, "stats", {}).get("target_status", {})
                _fire = getattr(s.mob_det, "_player_fire_time", 0.0)
                _age = (time.perf_counter() - _fire) if _fire else -1
                log.info("ENTD: status=%s fire_age=%.1fs player=%s",
                         _ts, _age, getattr(s.mob_det, "_player_pos", None))
                _near = [e for e in s.mob_det._last_entities
                         if e.category == EntityCategory.MONSTER and not e.is_friendly
                         and e.distance < 70]
                _near.sort(key=lambda e: e.distance)
                _st_by_id = getattr(s.mob_det, "_status_by_id", {})
                for e in _near[:6]:
                    _st = _st_by_id.get(e.id, "?")
                    _tr = s.mob_det._static_track.get(e.id, {})
                    log.info("ENTD   d=%5.1f st=%-11s hp=%d/%d reach=%s | %s",
                             e.distance, _st, e.hp_current, e.hp_max,
                             e.is_reachable, e.metadata[-44:])
        except Exception:
            pass

    # Debug overlay
    if s.diag.enabled:
        hp_pct_s = f"{s.hp_engine.hp_pct*100:.0f}%" if s.hp_engine.hp_pct is not None else "-"
        s.ov_state.debug_hp = f"HP={hp_pct_s} src={s.hp_engine.hp_src}{mem_hp_s} streak={s.hp_engine.low_streak}"
        if s.mob_det is not None and hasattr(s.mob_det, 'last_match') and s.mob_det.last_match:
            m = s.mob_det.last_match
            kind = "boot" if m.get("bootstrap") else "narrow" if m.get("narrow") else "lowhp" if m.get("low_hp_lock") else "strict"
            s.ov_state.debug_mob = f"mob=CV kind={kind} x={m.get('x',0)} w={m.get('w',0)} aspect={m.get('aspect',0):.1f}"
        elif s.use_memory and s.mob_det is not None:
            s.ov_state.debug_mob = f"mob=MEM hostile={s.mob_det.stats.get('hostile_in_range',0)} total={s.mob_det.stats.get('total_entities',0)}"
        else:
            s.ov_state.debug_mob = "mob=—"
        s.ov_state.debug_zone = f"range={s.atk_cfg.get('attack_range',15):.0f} dark={'on' if (s.mob_det and hasattr(s.mob_det, '_is_dark_zone') and full is not None and s.mob_det._is_dark_zone(full)) else '—'}"

    s.last_status = now
    s.iters = 0
