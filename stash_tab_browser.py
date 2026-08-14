"""Браузер вкладок стэша PoE2: список имён вкладок + клик по вкладке.

Объединяет два механизма (валидированы 02.08.2026 на билде post 2026-07-16):

1. StashTabContainer (STC) discovery: сканирование heap на пару (begin,end) где
   span кратно 0x90, 8<=cnt<=400, и первые 2-3 entries — валидные std::wstring SSO
   с печатным именем. Динамический (без хардкода адреса STC).
   STC офсеты (imkk000, валидны): begin@+0x358, end@+0x360, visibleIdx@+0x370.
   Entry: name std::wstring @ +0x00; inv (panel) @ +0x80; tab-button UI @ +0x88.

2. UI element abs-positions (ElementParentOff=0xB8 ЭМПИРИЧЕСКИ): walkUp parent
   chain, суммируя ElementPositionOff (+0x118 x, +0x11C y). Дизайн canvas 2560×1600.

3. design→screen масштабирование = aspect-fit по высоте (pillarbox по горизонтали)
   через GetClientRect/ClientToScreen PoE2-окна (найдено по заголовку 'Path of Exile 2').

4. click через win32 SendInput MOUSEINPUT (absolute screen coords).
   Вкладки с design-Y>=1500 ОФФСКРИН (idx > ~19 для пользователя) — click вернёт False
   с подсказкой, что нужна прокрутка tab-strip (mouse wheel) заранее.

См. stash_reader.py — чтение предметов открытой вкладки (Backpack).
Источники реверса: github.com/imkk000/poe2-offsets; ElementParentOff найден эмпирически.
"""
import ctypes
import json
import logging
import struct as _struct
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional, List, Tuple

from game_memory import GameMemory
from stash_reader import StashReader, StashItem, HEAP_LO, HEAP_HI

_log = logging.getLogger("poe2auto.stash_browser")

# Локальный виртуальный Xbox 360 (ViGEmBus). Joy_daemon НЕ используется — он
# конфликтует с bridge из bot.py за XInput-слот. Каждый скрипт создаёт свой
# VirtualXbox360, тапает бамперы напрямую, и уничтожает его при выходе.
# ВАЖНО: если параллельно запущен bot.py со своим vx + DualSenseBridge —
# второй vx будет проигнорирован игрой; в этом случае управление вкладками
# должно происходить из того же процесса (bot.py) — а не из отдельного скрипта.
try:
    from virtual_xbox import VirtualXbox360 as _VXbox, XUSB as _XUSB
    _VX_AVAILABLE = True
except Exception as _e:
    _VX_AVAILABLE = False
    _log.warning("virtual_xbox недоступен: %s", _e)

# Кеш STC на диск (после full-scan ~40с STC остаётся на месте, пока стэш открыт).
# Меняется только при перезапуске игры или смене hideout. Формат: json {stc, begin, cnt, ts}.
_STC_CACHE_FILE = r"C:\Users\OLD\AppData\Local\Temp\opencode\stc_cache.json"
_STC_CACHE_TTL = 1800  # 30 минут


def _load_stc_cache() -> Optional[Tuple[int, int, int, int]]:
    """Возвращает (stc, begin, cnt, vi_placeholder) или None. vi перечитывается каждый раз."""
    try:
        with open(_STC_CACHE_FILE, "r") as f:
            c = json.load(f)
        if (time.time() - c.get("ts", 0)) > _STC_CACHE_TTL:
            return None
        return int(c["stc"]), int(c["begin"]), int(c["cnt"]), -1
    except Exception:
        return None


def _save_stc_cache(stc: int, begin: int, cnt: int) -> None:
    try:
        with open(_STC_CACHE_FILE, "w") as f:
            json.dump({"stc": stc, "begin": begin, "cnt": cnt, "ts": time.time()}, f)
    except Exception as e:
        _log.warning("save STC cache failed: %s", e)

# UI Element офсеты (валидированы)
EL_SELF        = 0x08
EL_CHILD_BEG   = 0x10
EL_CHILD_END   = 0x18
EL_POSITION    = 0x118
EL_SIZE        = 0x288
EL_PARENT      = 0xB8  # эмпирически (14 шагов до root для tab-button)

# STC офсеты (imkk000, валидны на нашем билде)
STC_BEGIN_OFF       = 0x358
STC_END_OFF          = 0x360
STC_VISIBLE_IDX_OFF  = 0x370
STC_ENTRY_STRIDE     = 0x90
STC_ENTRY_NAME_OFF   = 0x00
STC_ENTRY_INV_OFF    = 0x80
STC_ENTRY_BTN_OFF    = 0x88

DESIGN_CANVAS_W = 2560.0
DESIGN_CANVAS_H = 1600.0
TASK_WINDOW_TITLE = "Path of Exile 2"
CLICK_MIN_ONSCREEN_DESIGN_Y = 1500.0  # вкладки с design-Y ниже — вне клиентской области


@dataclass
class TabInfo:
    idx: int
    name: str
    inv_panel: int       # entry+0x80 — UI panel открытой вкладки (НЕ Backpack!)
    button_addr: int     # entry+0x88 — tab-button UI element
    abs_x: float         # design-coord X
    abs_y: float         # design-coord Y
    width: float
    height: float
    is_open: bool        # True если idx == visibleIdx


class StashTabBrowser:
    """Discovery STC + список вкладок + click по вкладке."""

    def __init__(self, mem: GameMemory):
        self._mem = mem
        self._module_lo = mem._base
        self._module_hi = mem._base + mem._size
        self._reader = StashReader(mem)
        self._hwnd = None
        self._cache_stc: Optional[Tuple[int, int, int, int]] = None
        self._vx = None
        self._vx_owned = False

    def attach_controller(self, vx=None) -> bool:
        """Прикрепить существующий VirtualXbox360 (vx) ИЛИ создать свой.

        Если vx=None — создаём собственный ViGEm-Xbox (только если нет bot.py).
        Возвращает True если vx доступен (свой или переданный).
        """
        if vx is not None:
            self._vx = vx
            self._vx_owned = False
            return True
        if self._vx is not None:
            return True
        if not _VX_AVAILABLE:
            return False
        try:
            self._vx = _VXbox()
            if not self._vx.start():
                self._vx = None
                return False
            self._vx_owned = True
            return True
        except Exception as e:
            _log.warning("own vx.start failed: %s", e)
            self._vx = None
            return False

    def detach_controller(self) -> None:
        if self._vx_owned and self._vx is not None:
            try:
                self._vx.stop()
            except Exception as e:
                _log.debug("vx.stop: %s", e)
        self._vx = None
        self._vx_owned = False

    def _u64(self, a: int) -> int:
        d = self._mem.read_bytes(a, 8)
        return _struct.unpack("<Q", d)[0] if d else 0

    def _u32(self, a: int) -> int:
        d = self._mem.read_bytes(a, 4)
        return _struct.unpack("<I", d)[0] if d else 0

    def _f32(self, a: int) -> float:
        d = self._mem.read_bytes(a, 4)
        return _struct.unpack("<f", d)[0] if d else 0.0

    def _is_heap(self, p: int) -> bool:
        return HEAP_LO <= p < HEAP_HI

    def _read_std_wstring(self, addr: int) -> Optional[str]:
        h = self._mem.read_bytes(addr, 0x20) or b""
        if len(h) < 0x20:
            return None
        n = _struct.unpack("<Q", h[0x10:0x18])[0]
        cap = _struct.unpack("<Q", h[0x18:0x20])[0]
        if not (0 < n <= 256):
            return None
        if cap < 8:
            data = h[:n * 2]
        else:
            p = _struct.unpack("<Q", h[0:8])[0]
            if not self._is_heap(p):
                return None
            data = self._mem.read_bytes(p, n * 2) or b""
        s = ""
        for i in range(0, len(data) - 1, 2):
            c = data[i] | (data[i + 1] << 8)
            if c == 0:
                break
            s += chr(c)
        if not (1 <= len(s) <= 40):
            return None
        # имя вкладки: буквы/цифры/пробелы/пунктуация; допускаем unicode (кириллица и т.п.)
        return s if all(c.isprintable() for c in s) else None

    def find_stc(self) -> Optional[Tuple[int, int, int, int]]:
        """Динамический поиск STC.

        Возвращает (stc_addr, begin, count, visible_idx) или None.
        Кеширует результат на время жизни объекта (STC не двигается пока окно открыто).
        Сначала пытается загрузить из stc_cache.json (быстро), иначе делает full heap scan
        (~40с) и сохраняет кеш.
        """
        if self._cache_stc is not None:
            return self._cache_stc
        # 1) пробуем быстрый кеш из файла (stash_tab_browser_stc.json в Temp)
        cached = _load_stc_cache()
        if cached is not None:
            stc, begin, cnt, _vi = cached
            # валидация: первые две записи по-прежнему вкладки
            if (self._read_std_wstring(begin) is not None
                    and self._read_std_wstring(begin + 0x90) is not None):
                vis = self._u32(stc + STC_VISIBLE_IDX_OFF)
                if 0 <= vis < cnt:
                    self._cache_stc = (stc, begin, cnt, vis)
                    _log.info("STC from cache: stc=0x%X cnt=%d vi=%d", stc, cnt, vis)
                    return self._cache_stc
            _log.info("STC cache stale -> full scan")
        # 2) полный heap-scan
        found = self._scan_stc_full()
        if found is None:
            return None
        stc, begin, cnt, vis = found
        self._cache_stc = (stc, begin, cnt, vis)
        _save_stc_cache(stc, begin, cnt)
        return self._cache_stc

    def _scan_stc_full(self) -> Optional[Tuple[int, int, int, int]]:
        try:
            import numpy as np
        except ImportError:
            np = None
        handle = self._mem._handle
        candidates: List[Tuple[int, int, int]] = []
        addr = 0
        while addr < 0x7FFFFFFFFFFF:
            mbi = _MBI()
            res = ctypes.windll.kernel32.VirtualQueryEx(
                handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
            )
            if res == 0:
                addr = addr + 0x10000 if addr else 0x10000
                continue
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if size and mbi.State == 0x1000 and mbi.Protect in (0x04, 0x40, 0x02, 0x42):
                chunk = 16 * 1024 * 1024
                pos = 0
                while pos < size:
                    n = min(chunk, size - pos)
                    rd = self._mem.read_bytes(base + pos, n)
                    if rd is None:
                        pos += chunk
                        continue
                    if np is not None:
                        arr = np.frombuffer(rd, dtype=np.uint64)
                        begins = arr[:-1]
                        ends = arr[1:]
                        diff = (ends - begins).astype(np.int64)
                        mask = (begins >= HEAP_LO) & (begins < HEAP_HI) & (ends >= begins) \
                               & (diff > 0) & (diff <= 400 * 0x90) & (diff % 0x90 == 0)
                        for k in np.nonzero(mask)[0]:
                            begin = int(begins[k])
                            span = int(diff[k])
                            cnt = span // 0x90
                            if not (8 <= cnt <= 400):
                                continue
                            s0 = self._read_std_wstring(begin)
                            if not s0:
                                continue
                            s1 = self._read_std_wstring(begin + 0x90)
                            if not s1:
                                continue
                            pair_loc = int(np.uint64(base + pos) + np.uint64(k) * 8)
                            candidates.append((pair_loc, begin, cnt))
                    else:
                        for i in range(0, len(rd) - 16, 8):
                            begin = _struct.unpack("<Q", rd[i:i + 8])[0]
                            end = _struct.unpack("<Q", rd[i + 8:i + 16])[0]
                            if not (HEAP_LO <= begin < HEAP_HI) or end < begin:
                                continue
                            span = end - begin
                            if span == 0 or span % 0x90 != 0:
                                continue
                            cnt = span // 0x90
                            if not (8 <= cnt <= 400):
                                continue
                            s0 = self._read_std_wstring(begin)
                            if not s0:
                                continue
                            s1 = self._read_std_wstring(begin + 0x90)
                            if not s1:
                                continue
                            candidates.append((int(base + pos + i), begin, cnt))
                    pos += chunk
            addr = base + size
        if not candidates:
            return None
        # выбираем кандидата с максимальным числом валидных имён вкладок подряд
        best = None
        best_score = -1
        for pair_loc, begin, cnt in candidates:
            stc = pair_loc - STC_BEGIN_OFF
            vis = self._u32(stc + STC_VISIBLE_IDX_OFF)
            if not (0 <= vis < cnt):
                continue
            score = 0
            for ti in range(min(cnt, 8)):
                if self._read_std_wstring(begin + ti * STC_ENTRY_STRIDE) is not None:
                    score += 1
            # бонус за валидные tab-button UI элементы (self-ref)
            for ti in range(min(cnt, 5)):
                btn = self._u64(begin + ti * STC_ENTRY_STRIDE + STC_ENTRY_BTN_OFF)
                if self._is_heap(btn) and self._u64(btn + EL_SELF) == btn:
                    score += 2
            if score > best_score:
                best_score = score
                best = (stc, begin, cnt, vis)
        return best

    def abs_pos(self, element: int) -> Optional[Tuple[float, float]]:
        """Сумма ElementPositionOff вверх по parent-цепочке (ElementParentOff=0xB8)."""
        cur = element
        x = 0.0
        y = 0.0
        seen = set()
        for _ in range(48):
            if not self._is_heap(cur) or cur in seen:
                return None
            seen.add(cur)
            if self._u64(cur + EL_SELF) != cur:
                return None
            x += self._f32(cur + EL_POSITION)
            y += self._f32(cur + EL_POSITION + 4)
            nxt = self._u64(cur + EL_PARENT)
            if nxt == 0:
                return (x, y)
            cur = nxt
        return (x, y)

    def list_tabs(self) -> Optional[List[TabInfo]]:
        info = self.find_stc()
        if not info:
            return None
        _stc, begin, cnt, vis = info
        tabs: List[TabInfo] = []
        for ti in range(cnt):
            e = begin + ti * STC_ENTRY_STRIDE
            name = self._read_std_wstring(e) or ""
            inv = self._u64(e + STC_ENTRY_INV_OFF)
            btn = self._u64(e + STC_ENTRY_BTN_OFF)
            ax = ay = w = h = 0.0
            if self._is_heap(btn) and self._u64(btn + EL_SELF) == btn:
                ap = self.abs_pos(btn)
                if ap is not None:
                    ax, ay = ap
                w = self._f32(btn + EL_SIZE)
                h = self._f32(btn + EL_SIZE + 4)
            tabs.append(TabInfo(
                idx=ti, name=name, inv_panel=inv, button_addr=btn,
                abs_x=ax, abs_y=ay, width=w, height=h, is_open=(ti == vis),
            ))
        return tabs

    def list_tab_names(self) -> Optional[List[str]]:
        tabs = self.list_tabs()
        return [t.name for t in tabs] if tabs else None

    def find_tab_by_name(self, pattern: str, case_insensitive: bool = True) -> Optional[int]:
        tabs = self.list_tabs()
        if not tabs:
            return None
        if case_insensitive:
            p = pattern.lower()
            for t in tabs:
                if t.name and p in t.name.lower():
                    return t.idx
        else:
            for t in tabs:
                if t.name and pattern in t.name:
                    return t.idx
        return None

    def _find_hwnd(self) -> Optional[int]:
        if self._hwnd:
            return self._hwnd
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, TASK_WINDOW_TITLE)
        if not hwnd:
            return None
        self._hwnd = hwnd
        return hwnd

    def design_to_screen(self, dx: float, dy: float) -> Optional[Tuple[int, int]]:
        """Проекция design-координат (2560×1600 canvas) в screen-координаты через aspect-fit."""
        hwnd = self._find_hwnd()
        if not hwnd:
            return None
        user32 = ctypes.windll.user32
        cr = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(cr)):
            return None
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        cw = cr.right - cr.left
        ch = cr.bottom - cr.top
        # aspect-fit: Scale = min(cw/W, ch/H); pillarbox/letterbox center
        sx = cw / DESIGN_CANVAS_W
        sy = ch / DESIGN_CANVAS_H
        scale = min(sx, sy)
        offset_x = (cw - DESIGN_CANVAS_W * scale) / 2.0
        offset_y = (ch - DESIGN_CANVAS_H * scale) / 2.0
        screen_x = int(pt.x + offset_x + dx * scale)
        screen_y = int(pt.y + offset_y + dy * scale)
        return (screen_x, screen_y)

    def click_tab(self, idx: int) -> Tuple[bool, str]:
        """Клик по вкладке с индексом idx. Возвращает (ok, msg)."""
        tabs = self.list_tabs()
        if not tabs:
            return False, "STC not found / нет вкладок"
        if idx < 0 or idx >= len(tabs):
            return False, "idx вне диапазона (0..%d)" % (len(tabs) - 1)
        tab = tabs[idx]
        if not tab.button_addr or not (tab.abs_x or tab.abs_y):
            return False, "no button / no abs_pos для вкладки %d (%r)" % (idx, tab.name)
        if tab.abs_y >= CLICK_MIN_ONSCREEN_DESIGN_Y:
            return False, ("вкладка %r off-screen (design Y=%.0f >= %.0f) — нужен scroll tab-strip"
                           % (tab.name, tab.abs_y, CLICK_MIN_ONSCREEN_DESIGN_Y))
        sc = self.design_to_screen(tab.abs_x + tab.width / 2.0, tab.abs_y + tab.height / 2.0)
        if not sc:
            return False, "не найдено окно PoE2 (hwnd)"
        sx, sy = sc
        user32 = ctypes.windll.user32
        # absolute move + click left
        user32.SetCursorPos(sx, sy)
        _mouse_click(user32)
        _log.info("clicked tab[%d] %r @ screen(%d,%d)", idx, tab.name, sx, sy)
        return True, "clicked %r at (%d,%d)" % (tab.name, sx, sy)

    def _tap_button(self, btn: str, hold_ms: int = 80) -> bool:
        """Тапнуть кнопку виртуального геймпада. Использует self._vx (attach_controller)."""
        if self._vx is None:
            if not self.attach_controller():
                return False
        mask = _XUSB.get(btn)
        if mask is None:
            return False
        try:
            self._vx.tap(mask, hold_ms=hold_ms)
            return True
        except Exception as e:
            _log.warning("tap %s failed: %s", btn, e)
            return False

    def scroll_to_tab(self, target_idx: int, max_steps: int = 40,
                      settle_ms: int = 450) -> Tuple[bool, str]:
        """Переключиться на вкладку target_idx через R1/L1 на виртуальном геймпаде.

        Стратегия: tap'аем right_bumper (R1, вперёд) или left_bumper (L1, назад)
        до тех пор пока STC.visibleIdx == target_idx. После каждого тапа
        ждём settle_ms и перечитываем visibleIdx (кеш STC сбрасываем, затем
        find_stc читает u32(STC+0x370) — мгновенно из кеша файла).
        Предварительно нужно вызвать attach_controller() (ей вызывается
        автоматически при первом тапе, но лучше — в __main__).
        """
        info = self.find_stc()
        if not info:
            return False, "STC не найден"
        _stc, _begin, cnt, vis = info
        if not (0 <= target_idx < cnt):
            return False, "target idx вне диапазона"
        if vis == target_idx:
            return True, "уже открыта"
        last = vis
        moved = 0
        for step in range(max_steps):
            if vis < target_idx:
                ok = self._tap_button("right_bumper", 80)
            else:
                ok = self._tap_button("left_bumper", 80)
            if not ok:
                return False, "vx недоступен (R1/L1)"
            moved += 1
            time.sleep(settle_ms / 1000.0)
            self._cache_stc = None
            info = self.find_stc()
            if not info:
                return False, "STC потерян после тапа #%d" % (step + 1)
            _s, _b, _c, vis = info
            if vis == target_idx:
                return True, "переключили за %d тапов (R1/L1)" % (step + 1)
            if vis == last:
                _log.warning("тап #%d не сдвинул visibleIdx (=%d), продолжаем", step + 1, vis)
            last = vis
        return False, "не дошли (последний visibleIdx=%d, target=%d, тапов=%d)" % (vis, target_idx, moved)

    def read_open_tab_items(self) -> Optional[List[StashItem]]:
        """Прочитать предметы текущей открытой вкладки (Backpack через ItemView-скан)."""
        tab = self._reader.read_open_tab()
        return tab.items if tab else None

    def open_tab_and_read(self, idx: int, settle_ms: int = 600) -> Optional[List[StashItem]]:
        """Открыть вкладку idx через R1/L1 (виртуальный геймпад) и прочитать её Backpack.

        Единая стратегия: переключение бамперами работает для любой вкладки (on-screen
        или нет — UI сам скроллит viewport при R1/L1). После переключения ждём
        settle_ms и перечитываем Backpack (StashReader).
        """
        info = self.find_stc()
        stc_addr = info[0] if info else 0
        if info and info[3] == idx:
            # уже открыта
            self._reader = StashReader(self._mem)
            self._reader._stc_addr = stc_addr  # явный hint
            tab = self._reader.read_open_tab()
            return tab.items if tab else None
        ok, msg = self.scroll_to_tab(idx)
        _log.info("scroll_to_tab(%d): %s -> %s", idx, ok, msg)
        if not ok:
            # Fallback: R1/L1 не сработал (возможно сташ открыт мышкой и
            # геймпад-навигация не активна). Пробуем прямой Win32 mouse click
            # через design_to_screen по abs_pos кнопки вкладки.
            ok2, msg2 = self.click_tab(idx)
            _log.info("click_tab(%d) fallback: %s -> %s", idx, ok2, msg2)
            if not ok2:
                return None
            time.sleep(settle_ms / 1000.0)
        import time
        time.sleep(settle_ms / 1000.0)
        self._cache_stc = None
        info2 = self.find_stc()
        if info2:
            _stc, _begin, cnt, vis = info2
            if vis != idx:
                _log.warning("после открытия visibleIdx=%d, ожидался %d", vis, idx)
        self._reader = StashReader(self._mem)
        self._reader._stc_addr = info2[0] if info2 else stc_addr
        tab = self._reader.read_open_tab()
        return tab.items if tab else None

    def find_maps_tab(self) -> Optional[int]:
        for keyword in ("MAPS", "Maps", "MAP", "Карты", "карты"):
            idx = self.find_tab_by_name(keyword)
            if idx is not None:
                return idx
        return None

    def open_maps_tab(self) -> Tuple[bool, str, Optional[List[StashItem]]]:
        idx = self.find_maps_tab()
        if idx is None:
            return False, "вкладка с картами не найдена", None
        if self._cache_stc and self._cache_stc[3] == idx:
            return True, "MAPS уже открыта", self.read_open_tab_items()
        items = self.open_tab_and_read(idx)
        if items is None:
            return False, "не удалось кликнуть/прочитать вкладку %d" % idx, None
        maps = [it for it in items if it.path.startswith("Metadata/Items/Maps")]
        return True, "MAPS (idx=%d) открыта, items=%d maps=%d" % (idx, len(items), len(maps)), maps


# === win32 helpers ===

class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


_INPUT_MOUSE = 0
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_ABSOLUTE = 0x8000


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUT(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


def _mouse_click(user32, button_down_flag=_MOUSEEVENTF_LEFTDOWN,
                 button_up_flag=_MOUSEEVENTF_LEFTUP) -> None:
    inputs = (_INPUT * 2)()
    inputs[0].type = _INPUT_MOUSE
    inputs[0].mi.dwFlags = button_down_flag
    inputs[1].type = _INPUT_MOUSE
    inputs[1].mi.dwFlags = button_up_flag
    user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(_INPUT))


def main() -> int:
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    open_maps_run = "-m" in sys.argv or "--maps" in sys.argv
    mem = GameMemory()
    if not mem.connect():
        print("connect failed")
        return 1
    try:
        br = StashTabBrowser(mem)
        if open_maps_run:
            if not br.attach_controller():
                print("WARNING: virtual_xbox недоступен — переключение R1/L1 не сработает")
        tabs = br.list_tabs()
        if not tabs:
            print("STC не найден (стэш закрыт?)")
            return 0
        info = br.find_stc()
        _stc, _begin, cnt, vis = info
        print("STC=0x%X cnt=%d visibleIdx=%d" % (_stc, cnt, vis))
        print("--- вкладки ---")
        for t in tabs:
            mark = " <-OPEN" if t.is_open else ""
            print("  [%02d] %-30r btn=0x%X abs=(%.0f,%.0f) size=(%.0f,%.0f)%s"
                  % (t.idx, t.name, t.button_addr, t.abs_x, t.abs_y, t.width, t.height, mark))
        idx = br.find_maps_tab()
        print("MAPS tab idx =", idx)
        if idx is not None:
            tab = tabs[idx]
            print("MAPS abs=(%.0f,%.0f) onscreen=%s" % (tab.abs_x, tab.abs_y, tab.abs_y < CLICK_MIN_ONSCREEN_DESIGN_Y))
        if open_maps_run and idx is not None:
            print("--- open_maps_tab() end-to-end ---")
            ok, msg, maps = br.open_maps_tab()
            print("open_maps_tab:", ok, msg)
            if ok and maps is not None:
                print("maps count:", len(maps))
                for it in maps[:20]:
                    print("  id=%d (%d,%d) %dx%d %s" % (it.id, it.grid_x, it.grid_y, it.width, it.height, it.path))
    finally:
        br.detach_controller()
        mem.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())