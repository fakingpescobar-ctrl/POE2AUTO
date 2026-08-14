"""map_run_flow.py — оркестратор автоматического запуска карт PoE2 (чистый VX360).

Весь ввод — через виртуальный геймпад (ViGEmBus), БЕЗ Win32 SendInput / win_mouse.
Пользователь решил: всё управление flow'а — только стик/кнопки геймпада.

Валидированные bindings (эксперимент 03.08.2026):
  tap(A) рядом со сташем          — открыть сташ (геймпад-режим UI)
  R1/L1                           — переключение вкладок сташа (только gamepad-mode)
  D-pad Right/Left                — переключение Tier-подвкладок внутри MAPS
  left stick                      — навигация курсора по сетке сташа/инвентаря
  tap(Y) на waystone в сташе      — автоматически переместить в инвентарь
  tap(B)                          — закрыть сташ
  tap(A) на Map Device            — открыть Atlas overview
  left stick по голубой точке     — навести курсор на доступный atlas-узел
  tap(A)                          — открыть info panel + кнопка TRAVERSE
  left stick на waystone в inv    — навести курсор на waystone в инвентаре
  tap(X) (Square)                 — waystone в слот Map Device (TRAVERSE загорается)
  left stick на TRAVERSE + tap(A) — запуск карты

Отличия от v1 (гибрид с мышкой):
  - убран win_mouse: все шаги через vx (tap/stick)
  - tier-subtab переключается D-pad Right/Left (а не только R1/L1)
  - waystone из сташа берётся tap(Y) (а не Ctrl+Click мышкой)
  - waystone в слот — tap(X) (а не Ctrl+Click)
  - Activate = наведение на светящуюся TRAVERSE кнопку + tap(A)

Калибруемые параметры (секция map_run.bindings в config.yaml):
  grid_cell_hold_ms  — сколько держать стик, чтобы курсор сдвинулся на 1 клетку
  grid_settle_ms     — пауза после сдвига
  grid_start_stash   — стартовая клетка курсора после открытия сташа [x,y]
  grid_start_inv     — стартовая клетка курсора в инвентаре [x,y]
  atlas_nudge_ms     — шаг стика при наведении на голубую atlas-точку
  traverse_region_screen — (L,T,R,B) зона кнопки TRAVERSE, если известна
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Tuple, Callable, List

from game_memory import GameMemory
from stash_reader import StashReader, StashItem
from stash_tab_browser import StashTabBrowser
from virtual_xbox import VirtualXbox360 as _VX, XUSB as _XUSB
import screen_capture as scap

_log = logging.getLogger("poe2auto.maprun")

XUSB_MAP = _XUSB  # dict btn-name → XInput mask


# ── helpers ─────────────────────────────────────────────────────────────────
def _settle(ms: int = 400) -> None:
    time.sleep(ms / 1000.0)


def _tap(vx, btn: str, hold_ms: int = 80) -> bool:
    mask = XUSB_MAP.get(btn)
    if mask is None:
        return False
    try:
        vx.tap(mask, hold_ms=hold_ms)
        return True
    except Exception as e:
        _log.warning("tap %s failed: %s", btn, e)
        return False


def _dpad(vx, direction: str, hold_ms: int = 60) -> bool:
    return _tap(vx, "dpad_%s" % direction, hold_ms=hold_ms)


def _stick_nudge(vx, dx: float, dy: float, hold_ms: int) -> None:
    """Короткий выдвиг левого стика в направлении (dx,dy) (нормируется до ±32767)."""
    n = (dx * dx + dy * dy) ** 0.5 or 1.0
    sx = int((dx / n) * 32767)
    sy = int((dy / n) * 32767)
    try:
        vx.set_left_stick(sx, sy)
    except AttributeError:
        vx.stick(sx, sy)
    _settle(hold_ms)
    try:
        vx.set_left_stick(0, 0)
    except AttributeError:
        vx.stick(0, 0)


def _grid_nav_to(vx, tx: int, ty: int, start: Tuple[int, int] = (0, 0),
                 cell_hold_ms: int = 150, settle_ms: int = 90,
                 max_cells: int = 60) -> Tuple[int, int]:
    """Провести геймпад-курсор от start до клетки (tx,ty) по сетке.

    grid_x растёт вправо, grid_y — вниз (экранные coords). Один nudge стика
    ≈ одна клетка (cell_hold_ms калибруется). Возвращает фактическую позицию.
    """
    cx, cy = start
    guard = 0
    while cx < tx and guard < max_cells:
        _stick_nudge(vx, 1, 0, cell_hold_ms)
        _settle(settle_ms)
        cx += 1
        guard += 1
    while cx > tx and guard < max_cells:
        _stick_nudge(vx, -1, 0, cell_hold_ms)
        _settle(settle_ms)
        cx -= 1
        guard += 1
    while cy < ty and guard < max_cells:
        _stick_nudge(vx, 0, 1, cell_hold_ms)
        _settle(settle_ms)
        cy += 1
        guard += 1
    while cy > ty and guard < max_cells:
        _stick_nudge(vx, 0, -1, cell_hold_ms)
        _settle(settle_ms)
        cy -= 1
        guard += 1
    return (cx, cy)


def _tier_from_path(path: str) -> int:
    """'Metadata/Items/Maps/MapKeyTier16' → 16. 0 if parse fails."""
    if "MapKeyTier" not in path:
        return 0
    try:
        return int(path.split("Tier")[-1].split("/")[0].split("_")[0])
    except (ValueError, IndexError):
        return 0


def _select_waystone(maps: List[StashItem], target_tier: int,
                     fallback: bool = True) -> Optional[StashItem]:
    """Выбрать waystone=target_tier или ближайший ниже (fallback)."""
    by_tier = [(m, _tier_from_path(m.path)) for m in maps]
    by_tier = [(m, t) for m, t in by_tier if t > 0]
    if not by_tier:
        return None
    by_tier.sort(key=lambda mt: mt[1], reverse=True)
    for m, t in by_tier:
        if t == target_tier:
            return m
    if fallback:
        for m, t in by_tier:
            if t < target_tier:
                return m
    return None


def _stick_pulse(vx, sx: int, sy: int, hold_ms: int) -> None:
    """Выдвиг левого стика с ЗАДАННОЙ силой (не нормализуя) + возврат в нейтраль."""
    try:
        vx.set_left_stick(sx, sy)
    except AttributeError:
        vx.stick(sx, sy)
    _settle(hold_ms)
    try:
        vx.set_left_stick(0, 0)
    except AttributeError:
        vx.stick(0, 0)


def _walk_to(mem: GameMemory, vx, target_world: Tuple[float, float],
             stop_dist: float = 2.5, max_time_s: float = 30.0
             ) -> Tuple[bool, str]:
    """Бег к world-координатам через НЕПРЕРЫВНЫЙ стик (без рывков).

    Держим левый стик полным ходом в направлении цели (stick_for — обратная
    матрица изометрии), каждые итерации пересчитываем угол по свежей позиции.
    Отпускаем только у цели/при застревании/по таймауту. Так персонаж БЕЖИТ,
    а не семенит импульсами.

    Если упёрлись в стену (позиция не меняется ~0.8с при активном стике) —
    обход через CoreMap A*: load(gs) → find_path(start, goal) → бежим по waypoints.
    """
    try:
        from probe_cast import find_player, components
        from move_to_portal import stick_for
        import game_offsets as off
        from core_map import CoreMap
        player, _slot, gs = find_player(mem)
        comps = components(mem, player)
        ren = comps.get("Render")
        if not ren:
            return False, "Render компонент не найден"
        FORCE = 32000          # полный ход стика = бег
        STOP_WP = 3.0          # радиус подъезда к waypoint обхода
        STUCK_S = 0.8          # окно проверки застревания
        STUCK_MOVE = 1.5       # минимум движения за окно, иначе stuck

        def push(sx, sy):
            try:
                vx.set_left_stick(sx, sy)
            except AttributeError:
                vx.stick(sx, sy)

        def release():
            try:
                vx.set_left_stick(0, 0)
            except AttributeError:
                vx.stick(0, 0)

        def drive(tx: float, ty: float, stop: float, max_t: float, tag: str
                  ) -> Tuple[bool, str]:
            """Непрерывный бег к (tx, ty). (ok, msg)."""
            last_pos = None
            last_t = time.time()
            t_end = time.time() + max_t
            dd = 0.0
            while time.time() < t_end:
                p = mem.read_vec3(ren + off.Render.WORLD_POSITION)
                ddx = tx - p[0]
                ddy = ty - p[1]
                dd = (ddx * ddx + ddy * ddy) ** 0.5
                if dd < stop:
                    release()
                    return True, tag + " dist=%.2f" % dd
                sx, sy = stick_for(ddx, ddy, FORCE)
                push(sx, sy)
                now = time.time()
                if now - last_t > STUCK_S:
                    if last_pos is not None:
                        moved = ((p[0] - last_pos[0]) ** 2
                                 + (p[1] - last_pos[1]) ** 2) ** 0.5
                        if moved < STUCK_MOVE:
                            release()
                            return False, tag + " stuck dist=%.1f moved=%.2f" % (dd, moved)
                    last_t = now
                    last_pos = (p[0], p[1])
                time.sleep(0.02)
            release()
            return False, tag + " timeout dist=%.1f" % dd

        _log.info("walk-> %s (stop=%.1f)", target_world, stop_dist)
        ok, msg = drive(target_world[0], target_world[1], stop_dist,
                        max_time_s, "walk")
        if ok:
            return True, msg
        if "stuck" not in msg:
            return False, msg

        # застревание → обход через CoreMap A*
        p = mem.read_vec3(ren + off.Render.WORLD_POSITION)
        _log.info("walk STUCK (%s) — bypass via CoreMap", msg)
        path = None
        obs = None
        try:
            cm = CoreMap(mem)
            if cm.load(gs):
                obs, _used = cm.collect_entity_obstacles(
                    cm.area, player_world=(p[0], p[1]), inflate=1)
                path = cm.find_path((p[0], p[1]), target_world, obstacles=obs)
        except Exception as e:
            _log.warning("CoreMap exception: %s", e)
        if not path:
            return False, "застревание (%s), обход не найден" % msg
        _log.info("walk bypass путь: %d waypoints, препятствий %d",
                  len(path), len(obs) if obs else 0)
        t0 = time.time()
        for i, wp in enumerate(path):
            remain = max(5.0, (t0 + max_time_s) - time.time())
            ok, msg2 = drive(wp[0], wp[1], STOP_WP, remain, "bypass wp%d" % i)
            if not ok:
                return False, "обход: " + msg2
        _log.info("walk bypass завершён, бежим к цели")
        remain = max(5.0, (t0 + max_time_s) - time.time())
        return drive(target_world[0], target_world[1], stop_dist, remain, "walk2")
    except Exception as e:
        _log.warning("_walk_to exception: %s", e)
        return False, "exception: %s" % e


# ── Phase 1: waystone из сташа в инвентарь ──────────────────────────────────
def _stash_open(br: StashTabBrowser) -> bool:
    try:
        return br.find_stc() is not None
    except Exception:
        return False


def _ensure_tier_subtab(mem: GameMemory, vx, target_tier: int,
                        max_taps: int = 16, settle_ms: int = 600
                        ) -> Tuple[bool, str]:
    """Открыть Tier-subtab с target_tier внутри MAPS через D-pad Right/Left.

    D-pad Right → tier выше, Left → tier ниже (валидировано 03.08.2026).
    Направление определяется по максимальному видимому tier в read_all_maps.
    """
    rd = StashReader(mem)
    for i in range(max_taps + 1):
        maps = rd.read_all_maps()
        tiers = {_tier_from_path(m.path) for m in maps}
        _log.info("tier_nav[%d] want=%d seen=%s max=%s", i, target_tier,
                  sorted(tiers), (max(tiers) if tiers else "-"))
        if target_tier in tiers:
            return True, "Tier %d уже виден" % target_tier
        if not tiers:
            _dpad(vx, "right", 60)
            _settle(settle_ms)
            continue
        direction = "right" if target_tier > max(tiers) else "left"
        _dpad(vx, direction, 60)
        _settle(settle_ms)
    return False, "Tier %d не открылся за %d D-pad" % (target_tier, max_taps)


def step1_pick_waystone(mem: GameMemory, vx, *,
                        target_tier: int = 16,
                        stash_world: Optional[Tuple[float, float]] = None,
                        bindings: Optional[dict] = None,
                        ) -> Tuple[bool, str]:
    """Открыть сташ → MAPS → Tier N subtab → tap(Y) на waystone → закрыть сташ.

    На выходе: waystone target_tier (или ближайший ниже) в player_inv.
    """
    b = StashTabBrowser(mem)
    if not b.attach_controller(vx):
        return False, "vx недоступен (attach_controller)"
    cfg = bindings or {}

    if stash_world is not None:
        ok, msg = _walk_to(mem, vx, tuple(stash_world),
                           stop_dist=cfg.get("stop_dist_stash", 2.5),
                           max_time_s=cfg.get("walk_max_time_s", 25.0))
        if not ok:
            return False, "walk to stash: " + msg

    # 1. открыть сташ геймпадом (именно так — иначе R1/L1 не работают)
    _tap(vx, "a", hold_ms=80)
    _settle(cfg.get("stash_open_settle_ms", 1200))
    if not _stash_open(b):
        return False, "сташ не открылся после tap(A)"

    # 2. MAPS вкладка через R1/L1
    idx = b.find_maps_tab()
    if idx is None:
        return False, "вкладка MAPS не найдена"
    ok, msg = b.scroll_to_tab(idx, settle_ms=cfg.get("tab_settle_ms", 450))
    if not ok:
        return False, "MAPS не открыта через R1/L1: %s" % msg

    # 2.5 диагностика: что видим в MAPS после скролла
    rd_pre = StashReader(mem)
    subs = rd_pre.find_map_subtabs()
    _log.info("MAPS open: subtab_containers=%d", len(subs))
    for c, w, h, mlist in subs:
        tt = sorted({_tier_from_path(m.path) for m in mlist})
        _log.info("  subtab container=0x%X %dx%d items=%d tiers=%s",
                  c, w, h, len(mlist), tt)

    # 3. Tier-subtab (D-pad Right/Left)
    ok, msg = _ensure_tier_subtab(mem, vx, target_tier,
                                  settle_ms=cfg.get("subtab_settle_ms", 600))
    if not ok:
        return False, msg

    # 4. найти waystone (перечитываем контейнер после переключений)
    rd = StashReader(mem)
    maps = rd.read_all_maps()
    waystone = _select_waystone(maps, target_tier, fallback=cfg.get("fallback_below", True))
    if waystone is None:
        return False, "waystone Tier>=%d не найден (всего карт: %d)" % (target_tier, len(maps))

    # 5. навести курсор на клетку waystone + tap(Y) → инвентарь
    start = tuple(cfg.get("grid_start_stash", [0, 0]))
    _grid_nav_to(vx, waystone.grid_x, waystone.grid_y, start=start,
                 cell_hold_ms=cfg.get("grid_cell_hold_ms", 150),
                 settle_ms=cfg.get("grid_settle_ms", 90))
    _settle(200)
    _tap(vx, "y", hold_ms=80)
    _settle(cfg.get("transfer_settle_ms", 600))

    # 6. проверка: waystone теперь в player_inv
    inv = rd.read_player_inventory()
    if inv is None:
        return False, "player_inv не найден после tap(Y)"
    has = any(it.path.startswith("Metadata/Items/Maps") for it in inv.items)
    if not has:
        return False, "waystone НЕ в player_inv (tap(Y) не сработал?)"

    # 7. закрыть сташ
    _tap(vx, "b", hold_ms=80)
    _settle(600)
    return True, "waystone Tier %d в инвентаре" % target_tier


# ── Phase 2: walk к Map Device ──────────────────────────────────────────────
def step2_walk_to_map_device(mem: GameMemory, vx, *,
                             map_device_world: Optional[Tuple[float, float]],
                             bindings: Optional[dict] = None,
                             ) -> Tuple[bool, str]:
    if map_device_world is None:
        return False, "map_device_world неизвестен (config map_run.map_device_world)"
    return _walk_to(mem, vx, tuple(map_device_world),
                    stop_dist=bindings.get("stop_dist_device", 2.5) if bindings else 2.5,
                    max_time_s=bindings.get("walk_max_time_s", 25.0) if bindings else 25.0)


# ── Phase 3: открыть Map Device UI (Atlas overview) ─────────────────────────
def step3_open_map_device(vx, settle_s: float = 2.0) -> Tuple[bool, str]:
    """tap(A) — interact с Map Device; открывается Atlas overview (~1-2s)."""
    _tap(vx, "a", hold_ms=80)
    _settle(int(settle_s * 1000))
    return True, "tap(A) — ждём Atlas overview"


# ── Phase 4: навести курсор на голубую atlas-точку → tap(A) ────────────────
def _screen_center() -> Tuple[int, int]:
    import win32api
    import win32con
    return (win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN) // 2,
            win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN) // 2)


def _nudge_towards_screen(vx, target_screen: Tuple[int, int],
                          nudge_ms: int, settle_ms: int = 120) -> None:
    """Один шаг стика в направлении target_screen от центра экрана."""
    cx0, cy0 = _screen_center()
    dx = target_screen[0] - cx0
    dy = target_screen[1] - cy0
    if dx == 0 and dy == 0:
        return
    _stick_nudge(vx, dx, dy, nudge_ms)
    _settle(settle_ms)


def step4_navigate_to_atlas_blue(vx, *,
                                 hwnd: Optional[int] = None,
                                 bindings: Optional[dict] = None,
                                 max_steps: int = 20,
                                 ) -> Tuple[bool, str]:
    """Навести геймпад-курсор на голубую atlas-точку и открыть info panel (tap A).

    Эвристика: курсор стартует в центре экрана; итеративно ищем голубые кластеры
    (scap.atlas_blue_finder) и подёргиваем стик к ближайшему. Когда голубая точка
    попадает в радиус R вокруг центра — tap(A) (info panel с TRAVERSE).
    """
    cfg = bindings or {}
    radius = cfg.get("atlas_hit_radius", 40)
    nudge_ms = cfg.get("atlas_nudge_ms", 120)

    for step in range(max_steps):
        try:
            blobs = scap.atlas_blue_finder(hwnd=hwnd)
        except Exception as e:
            _log.debug("atlas_blue_finder fail: %s", e)
            blobs = []
        if not blobs:
            _settle(200)
            continue
        cx0, cy0 = _screen_center()
        near = [b for b in blobs
                if abs(b[0] - cx0) < radius and abs(b[1] - cy0) < radius]
        if near:
            _log.info("atlas точка под курсором (step %d), tap(A)", step)
            _tap(vx, "a", hold_ms=80)
            _settle(cfg.get("atlas_tap_settle_ms", 1200))
            return True, "info panel открыта (голубая точка)"
        best = min(blobs, key=lambda b: (b[0] - cx0) ** 2 + (b[1] - cy0) ** 2)
        _log.info("atlas nav step %d → blob (%d,%d)", step, best[0], best[1])
        _nudge_towards_screen(vx, (best[0], best[1]), nudge_ms)
    return False, "не навёлся на голубую точку за %d шагов" % max_steps


# ── Phase 5: waystone из инвентаря в слот Map Device ────────────────────────
def step5_place_waystone(mem: GameMemory, vx, *,
                         target_tier: int = 16,
                         bindings: Optional[dict] = None,
                         ) -> Tuple[bool, str]:
    """Навести курсор на waystone в инвентаре (12×5) → tap(X) → слот Map Device.

    Проверка: после tap(X) waystone исчезает из player_inv (читается из памяти).
    """
    cfg = bindings or {}
    rd = StashReader(mem)
    inv = rd.read_player_inventory()
    if inv is None:
        return False, "player_inv не найден"
    way = _select_waystone(inv.items, target_tier, fallback=True)
    if way is None:
        return False, "waystone нет в инвентаре"

    start = tuple(cfg.get("grid_start_inv", [0, 0]))
    _grid_nav_to(vx, way.grid_x, way.grid_y, start=start,
                 cell_hold_ms=cfg.get("grid_cell_hold_ms", 150),
                 settle_ms=cfg.get("grid_settle_ms", 90))
    _settle(200)
    _tap(vx, "x", hold_ms=80)  # Square = поместить в слот
    _settle(cfg.get("place_settle_ms", 600))

    inv2 = rd.read_player_inventory()
    if inv2 is not None:
        still = [it for it in inv2.items if it.path.startswith("Metadata/Items/Maps")]
        if still:
            return False, "waystone всё ещё в инвентаре (tap(X) не сработал?)"
    return True, "waystone помещён в слот Map Device"


# ── Phase 6: навести на TRAVERSE → tap(A) ───────────────────────────────────
def step6_click_traverse(vx, *,
                         hwnd: Optional[int] = None,
                         bindings: Optional[dict] = None,
                         max_steps: int = 15,
                         ) -> Tuple[bool, str]:
    """Навести курсор на светящуюся TRAVERSE кнопку и активировать (tap A).

    Светящаяся кнопка детектится HSV (жёлтое свечение — как у Activate).
    Если traverse_region_screen задан — наводимся на его центр (без HSV).
    """
    cfg = bindings or {}
    region = cfg.get("traverse_region_screen", None)
    if region and len(region) == 4:
        target = ((region[0] + region[2]) // 2, (region[1] + region[3]) // 2)
        _nudge_towards_screen(vx, target, cfg.get("atlas_nudge_ms", 120))
        _settle(300)
        _tap(vx, "a", hold_ms=80)
        return True, "traverse кликнут (заданная зона)"

    for step in range(max_steps):
        try:
            if hwnd is None:
                img = scap.grab_desktop()
            else:
                img = scap.grab_client_region(hwnd)
        except Exception as e:
            _log.debug("grab fail: %s", e)
            _settle(200)
            continue
        mask = scap.sort_by_hsv(img, *scap.ACTIVATE_BTN_LIT_HSV)
        lit, _n = int(mask.sum() > 0), 0
        if lit:
            # центр свечения
            ys, xs = mask.nonzero()
            if hwnd is None:
                bx = int(xs.mean())
                by = int(ys.mean())
            else:
                off = win32gui_client_origin(hwnd)
                bx = int(xs.mean()) + off[0]
                by = int(ys.mean()) + off[1]
            _log.info("traverse свечение в (%d,%d), tap(A)", bx, by)
            _nudge_towards_screen(vx, (bx, by), cfg.get("atlas_nudge_ms", 120))
            _settle(300)
            _tap(vx, "a", hold_ms=80)
            return True, "traverse активирована (%d,%d)" % (bx, by)
        _settle(250)
    return False, "TRAVERSE свечение не найдено за %d шагов" % max_steps


def win32gui_client_origin(hwnd: int) -> Tuple[int, int]:
    import win32gui
    return win32gui.ClientToScreen(hwnd, (0, 0))


# ── Public orchestrator ──────────────────────────────────────────────────────
def run_map_flow(mem: GameMemory, vx, *,
                 target_tier: int = 16,
                 map_device_world: Optional[Tuple[float, float]] = None,
                 stash_world: Optional[Tuple[float, float]] = None,
                 enter_portal_after: bool = False,
                 bindings: Optional[dict] = None,
                 progress_cb: Optional[Callable[[str, str], None]] = None,
                 ) -> Tuple[bool, str]:
    """Главный оркестратор: все 6 шагов на чистом VX360.

    bindings — секция config map_run (тайминги, калибровка grid/atlas).
    Возвращает (ok, overall_msg).
    """
    cfg = bindings or {}

    def _phase(name: str, status: str) -> None:
        _log.info("[phase:%s] %s", name, status)
        if progress_cb:
            try:
                progress_cb(name, status)
            except Exception:
                pass

    # 1. waystone в инвентарь (сташ → tap(Y))
    _phase("start", "target Tier=%d" % target_tier)
    ok, msg = step1_pick_waystone(mem, vx, target_tier=target_tier,
                                  stash_world=stash_world, bindings=cfg)
    _phase("step1_pick_waystone", ("OK " if ok else "FAIL ") + msg)
    if not ok:
        return False, "step1: " + msg

    # 2. к Map Device
    ok, msg = step2_walk_to_map_device(mem, vx,
                                       map_device_world=map_device_world,
                                       bindings=cfg)
    _phase("step2_walk_to_device", ("OK " if ok else "FAIL ") + msg)
    if not ok:
        return False, "step2: " + msg

    # 3. открыть Atlas overview
    ok, msg = step3_open_map_device(vx)
    _phase("step3_open_device", msg)

    # 4. голубая atlas-точка → info panel
    ok, msg = step4_navigate_to_atlas_blue(vx, bindings=cfg)
    _phase("step4_atlas_blue", ("OK " if ok else "FAIL ") + msg)
    if not ok:
        return False, "step4: " + msg

    # 5. waystone из инвентаря в слот
    ok, msg = step5_place_waystone(mem, vx, target_tier=target_tier, bindings=cfg)
    _phase("step5_place_waystone", ("OK " if ok else "FAIL ") + msg)
    if not ok:
        return False, "step5: " + msg

    # 6. TRAVERSE
    ok, msg = step6_click_traverse(vx, bindings=cfg)
    _phase("step6_traverse", ("OK " if ok else "FAIL ") + msg)
    if not ok:
        return False, "step6: " + msg

    if enter_portal_after:
        _phase("step7_portal", "TODO — portal entry")
        _settle(30000)

    return True, "map_run_flow done — карта Tier %d запущена" % target_tier


# ── CLI: тест вне bot.py ──────────────────────────────────────────────────────
def main() -> int:
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mem = GameMemory()
    if not mem.connect():
        print("connect failed")
        return 1
    try:
        vx = _VX()
        if not vx.start():
            print("vx.start failed")
            return 1
        ok, msg = run_map_flow(mem, vx, target_tier=16, enter_portal_after=False)
        print("RESULT", ok, msg)
        vx.stop()
        return 0 if ok else 1
    finally:
        mem.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
