"""BotState — общий контейнер состояния бота.

Заменяет ~40 nonlocal-переменных из god-function main().
Каждый модуль получает ссылку на BotState и работает через s.<field>.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from config import load_config
from hp_engine import HPEngine, HPDecision
from overlay import OverlayState
from auto_flask import AutoFlaskEngine, FlaskRule, FlaskState, default_rules
from utils import Toggle, build_detector, build_loot_engine
from virtual_xbox import VirtualXbox360, XUSB
from entity_reader import EntityCategory
from mob_bar import MobBarDetector
from zone_detector import ZoneDetector
from mem_detect import MemoryMobDetector
from reader_detect import ReaderMobDetector
from diag_dump import HPDiag
from nav_worker import NavWorker
from game_memory import GameMemory as _GM
from stash_flow import (open_maps_tab as _open_maps_tab, summarize_items as _summarize,
                         list_stash_tabs as _list_stash_tabs, filter_maps as _filter_maps,
                         read_all_maps as _read_all_maps)
from map_run_flow import run_map_flow as _run_map_flow

log = logging.getLogger("poe2auto.bot")


class BotState:
    """Central state container — всё общее состояние бота в одном месте."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.stop = threading.Event()

        # ── hardware ──
        self.vx: Any = None       # VirtualXbox360
        self.bridge: Any = None   # DualSenseBridge
        self.cap: Any = None      # ScreenCapture
        self.win: Any = None      # GameWindow
        self.region: Dict[str, int] = {"left": 0, "top": 0, "width": 1920, "height": 1080}

        # ── loot ──
        self.loot: Dict[str, Any] = cfg["loot"]
        self.det: Any = None      # colour detector
        self.engine: Any = None   # loot engine
        self.pickup_mask: int = XUSB["a"]
        self.hold_ms: int = 80

        # ── HP detection ──
        self.hp_cfg = cfg["hp"]
        self.num_cfg = cfg.get("hp_num", {})
        self.hp_enabled: bool = False
        self.hp_threshold: float = 0.88
        self.hp_cooldown: float = 1.0
        self.flask_hold: int = 120
        self.flask_right: bool = False
        self.hp_confirm: int = 1
        self.hp_valid_val: int = 45
        self.hp_fallback_red_min: float = 0.04
        self.num_low_grace: float = 0.35
        self.hp_num_grace: float = 0.5
        self.hp_engine: Optional[HPEngine] = None

        # HP geometry (orb + ROI digits)
        self.hpg: Dict[str, Any] = {
            "hp_det": None, "hp_det_crop": None,
            "orb_region": None, "num_reader": None, "num_reader_crop": None,
        }
        self.loc_info: Dict[str, str] = {"text": ""}
        self.hud_anchor: Dict[str, float] = {"max": 0}
        self.located: bool = False

        # ── attack / mob detection ──
        self.atk_cfg = cfg.get("auto_attack", {})
        self.atk_enabled: bool = False
        self.detect_mode: str = "cv"
        self.use_memory: bool = False
        self.mob_det: Any = None
        self.cv_det: Any = None
        self.zone_det: Any = None
        self.atk_btn: int = XUSB["right_bumper"]
        self.atk_grace: float = 0.6
        self._atk_fallback_reason: str = ""

        # Resolve configs immediately
        self.resolve_hp_config()
        self.resolve_attack_config()

        # ── auto-flask (memory-based) ──
        self.auto_flask = AutoFlaskEngine()

        # ── toggles ──
        self.auto = Toggle()
        self.attack = Toggle()
        self.flask = Toggle(default=True)
        self.diag = HPDiag()
        self.manual_shot = threading.Event()

        # ── counters / loop state ──
        self.r1_held: bool = False
        self.last_bar_seen: float = 0.0
        self.presses: int = 0
        self.flasks: int = 0
        self.last_flask: float = 0.0
        self.last_status: float = 0.0
        self.last_locate: float = 0.0
        self.last_num_ok: float = time.perf_counter()
        self.last_atk_dump: float = 0.0
        self.atk_dump_n: int = 0
        self.iters: int = 0
        self.last_mem_retry: float = 0.0

        # ── workers ──
        self.loot_worker: Any = None
        self.attack_worker: Any = None
        self.nav_worker: Optional[NavWorker] = None
        self.nav_enabled: bool = False

        # ── overlay ──
        self.ov_state = OverlayState()
        self.overlay: Any = None

        # ── stash flow ──
        self._stash_running: Dict[str, bool] = {"on": False}
        self._stash_lock = threading.Lock()

        # ── map run flow ──
        self._map_run_running: Dict[str, bool] = {"on": False}
        self._map_run_lock = threading.Lock()

        # ── hotkeys (resolved once, names only) ──
        hk = cfg.get("hotkeys", {})
        self.k_toggle = hk.get("toggle", "f8")
        self.k_quit = hk.get("quit", "f12")
        self.k_diag = hk.get("diag", "f7")
        self.k_shot = hk.get("snapshot", "f9")
        self.k_overlay = hk.get("overlay", "f6")
        self.k_attack = hk.get("attack", "f2")
        self.k_flask = hk.get("flask", "f3")
        self.k_radial = hk.get("radial", "f4")
        self.k_reload = "f5"
        self.k_nav = hk.get("nav", "f1")
        self.k_stash = hk.get("stash", "f11")
        self.k_map_run = hk.get("map_run", "f10")

        # ── loot region (computed from region + config) ──
        self.loot_region: Dict[str, int] = {}
        self.loot_center = (0, 0)
        self.fw: int = 1920
        self.fh: int = 1080

        # ── overlay modules list (built in main loop) ──
        self._last_modules_build: float = 0.0

    # ── helpers ──

    def resolve_hp_config(self) -> None:
        """Parse hp_cfg/num_cfg into typed fields."""
        hp = self.hp_cfg
        num = self.num_cfg
        self.hp_enabled = bool(hp.get("enabled"))
        self.hp_threshold = hp.get("threshold", 0.88)
        self.hp_cooldown = hp.get("cooldown_ms", 333) / 1000.0
        self.flask_hold = hp.get("flask_hold_ms", 120)
        self.flask_right = hp.get("flask_button") == "right_trigger"
        self.hp_confirm = hp.get("confirm_frames", 1)
        self.hp_valid_val = hp.get("valid_val", 45)
        self.hp_fallback_red_min = hp.get("fallback_red_frac_min", 0.04)
        self.num_low_grace = hp.get("num_low_grace_s", 0.35)
        self.hp_num_grace = num.get("menu_grace_s", 0.5)

    def rebuild_hp_engine(self) -> None:
        """Create HPEngine from current config."""
        self.hp_engine = HPEngine(
            threshold=self.hp_threshold,
            confirm_frames=self.hp_confirm,
            num_low_grace_s=self.num_low_grace,
            fallback_red_frac_min=self.hp_fallback_red_min,
            valid_val=self.hp_valid_val,
            menu_grace_s=self.hp_num_grace,
        )

    def resolve_attack_config(self) -> None:
        """Parse atk_cfg into typed fields + detect_mode."""
        atk = self.atk_cfg
        self.atk_enabled = bool(atk.get("enabled"))
        _raw = atk.get("use_memory", "reader")
        if _raw is True or _raw == "memory":
            self.detect_mode = "memory"
        elif _raw == "reader":
            self.detect_mode = "reader"
        else:
            self.detect_mode = "cv"
        self.use_memory = self.detect_mode in ("memory", "reader")
        self.atk_btn = XUSB.get(atk.get("button", "right_bumper"), XUSB["right_bumper"])
        self.atk_grace = atk.get("release_grace_s", 0.6)

    def compute_loot_region(self) -> None:
        """Compute loot crop region from self.region + loot config."""
        loot = self.loot
        lr = int(loot["pickup_radius_px"] * 1.15)
        lccx = self.region["left"] + self.region["width"] // 2 + loot["center_offset_xy"][0]
        lccy = self.region["top"] + self.region["height"] // 2 + loot["center_offset_xy"][1]
        ll = max(self.region["left"], lccx - lr)
        lt = max(self.region["top"], lccy - lr)
        lrr = min(self.region["left"] + self.region["width"], lccx + lr)
        lbb = min(self.region["top"] + self.region["height"], lccy + lr)
        self.loot_region = {"left": ll, "top": lt, "width": lrr - ll, "height": lbb - lt}
        self.loot_center = (lccx - ll, lccy - lt)

    def init_auto_flask(self) -> None:
        """Build AutoFlaskEngine from config."""
        af_cfg = self.cfg.get("auto_flask", {})
        af_enabled = af_cfg.get("enabled", True) and self.use_memory
        self.auto_flask = AutoFlaskEngine()
        if af_enabled:
            for rule_cfg in af_cfg.get("rules", default_rules()):
                self.auto_flask.add_rule(FlaskRule(
                    name=rule_cfg.get("name", "Unnamed"),
                    key=rule_cfg.get("key", "left_trigger"),
                    hp_below=rule_cfg.get("hp_below"),
                    mana_below=rule_cfg.get("mana_below"),
                    enemies_nearby=rule_cfg.get("enemies_nearby"),
                    enemy_radius=rule_cfg.get("enemy_radius", 30.0),
                    cooldown_s=rule_cfg.get("cooldown_s", 1.0),
                    enabled=rule_cfg.get("enabled", True),
                ))
        self.auto_flask.enabled = af_enabled

    def stash_task(self) -> None:
        """Background task: open MAPS stash tab and log results."""
        with self._stash_lock:
            if self._stash_running["on"]:
                return
            self._stash_running["on"] = True
        try:
            mem = _GM()
            if not mem.connect():
                log.warning("Stash: GameMemory.connect() failed")
                self.ov_state.notification = "Stash: нет подключения к PoE2"
                self.ov_state.notification_until = time.time() + 4.0
                return
            try:
                names = _list_stash_tabs(mem, self.vx)
                if names:
                    log.info("Stash tabs (%d): %s", len(names), ", ".join(names))
                ok, msg, items = _open_maps_tab(mem, self.vx)
                main_maps = _filter_maps(items) if items else None
                log.info("Stash flow: ok=%s msg=%s items=%s main_tab_maps=%s",
                         ok, msg, len(items) if items else 0,
                         len(main_maps) if main_maps else 0)
                if items:
                    log.info("Stash MAPS open tab dump: %s", _summarize(items))
                all_maps = _read_all_maps(mem)
                log.info("Stash all subtabs: total maps=%d (across %d subtabs)",
                         len(all_maps), len({m.container for m in all_maps}))
                if all_maps:
                    tiers: dict = {}
                    for m in all_maps:
                        tier = m.path.split("MapKeyTier")[-1] if "MapKeyTier" in m.path else "?"
                        tiers[tier] = tiers.get(tier, 0) + 1
                    tiers_s = ", ".join("Tier%s=%d" % (t, n) for t, n in sorted(tiers.items()))
                    self.ov_state.notification = "Maps: %d (%s)" % (len(all_maps), tiers_s)
                    self.ov_state.notification_until = time.time() + 8.0
                    log.info("=== maps across all subtabs (total=%d, tiers: %s) ===",
                             len(all_maps), tiers_s)
                    for m in all_maps[:30]:
                        log.info("  MAP id=%d container=0x%X (%d,%d) %dx%d %s",
                                 m.id, m.container, m.grid_x, m.grid_y,
                                 m.width, m.height, m.path)
                elif main_maps:
                    self.ov_state.notification = "Maps: %d (main tab)" % len(main_maps)
                    self.ov_state.notification_until = time.time() + 6.0
                else:
                    self.ov_state.notification = "MAPS открыта, 0 карт (см. лог)"
                    self.ov_state.notification_until = time.time() + 6.0
            finally:
                mem.disconnect()
        finally:
            with self._stash_lock:
                self._stash_running["on"] = False

    def trigger_stash(self) -> None:
        """Start stash task in background thread."""
        if self._stash_running["on"]:
            log.info("Stash занят, пропускаем F11")
            return
        threading.Thread(target=self.stash_task, daemon=True).start()

    def map_run_task(self) -> None:
        """Background task: one-shot map run flow."""
        with self._map_run_lock:
            if self._map_run_running["on"]:
                return
            self._map_run_running["on"] = True
        try:
            mrcfg = self.cfg.get("map_run", {})
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

            self.ov_state.notification = "Map run: Tier %d..." % target_tier
            self.ov_state.notification_until = time.time() + 30.0

            mem = _GM()
            if not mem.connect():
                log.warning("MapRun: GameMemory.connect() failed")
                self.ov_state.notification = "MapRun: нет PoE2"
                self.ov_state.notification_until = time.time() + 4.0
                return
            try:
                def _progress(phase: str, status: str) -> None:
                    log.info("MapRun[%s] %s", phase, status)
                    if "FAIL" in status:
                        self.ov_state.notification = "MapRun: %s" % status
                        self.ov_state.notification_until = time.time() + 6.0
                ok, msg = _run_map_flow(
                    mem, self.vx,
                    target_tier=target_tier,
                    map_device_world=map_device_world,
                    stash_world=stash_world,
                    enter_portal_after=enter_portal_after,
                    bindings=bindings,
                    progress_cb=_progress,
                )
                log.info("MapRun result ok=%s msg=%s", ok, msg)
                self.ov_state.notification = ("MapRun: OK %s" % msg if ok
                                              else "MapRun FAIL: %s" % msg)[:120]
                self.ov_state.notification_until = time.time() + 8.0
            finally:
                mem.disconnect()
        finally:
            with self._map_run_lock:
                self._map_run_running["on"] = False

    def trigger_map_run(self) -> None:
        """Start map run task in background thread."""
        if self._map_run_running["on"]:
            log.info("MapRun занят, пропускаем F10")
            return
        threading.Thread(target=self.map_run_task, daemon=True).start()
