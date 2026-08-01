"""Оверлей с точками мобов поверх PoE2.
Рисует фигуры по редкости: круг(Normal), ромб(Magic), треугольник(Rare), звезда(Unique).

Зависимости: pywin32 (win32gui)
"""
import logging
import math
import struct
import threading
import time
from typing import Optional, List, Tuple

import win32gui
import win32api
import win32con

import game_offsets as off
from game_memory import GameMemory
from entity_reader import EntityReader, EntityCategory, Rarity

_log = logging.getLogger("poe2auto.overlay")

COLOR_KEY = win32api.RGB(255, 0, 255)  # magenta
COLOR_ENEMY = win32api.RGB(240, 60, 60)
COLOR_UNIQUE = win32api.RGB(255, 200, 50)


class EntityOverlay:
    def __init__(self):
        self._mem = GameMemory()
        self._reader = EntityReader(self._mem)
        self._hwnd = 0
        self._poe_hwnd = 0
        self._running = False
        self._data_lock = threading.Lock()
        self._entities: List = []
        self._cam_matrix: Optional[list] = None
        self._screen_w = 800
        self._screen_h = 600
        self._win_x = 0
        self._win_y = 0
        # Кеш AOB-цепочки — вычисляем один раз
        self._igs_ptr: Optional[int] = None

    def _find_poe_window(self) -> int:
        """Найти окно PoE2."""
        found = [0]

        def enum_proc(hwnd, _):
            cls = win32gui.GetClassName(hwnd)
            if "PathOfExile" in cls:
                found[0] = hwnd
                return False
            title = win32gui.GetWindowText(hwnd)
            if "Path of Exile 2" in title:
                found[0] = hwnd
                return False
            return True

        try:
            win32gui.EnumWindows(enum_proc, 0)
        except:
            pass
        return found[0]

    def _wnd_proc(self, hwnd: int, msg: int, wp: int, lp: int) -> int:
        """Window procedure."""
        if msg == win32con.WM_PAINT:
            hdc, ps = win32gui.BeginPaint(hwnd)
            mem_dc = win32gui.CreateCompatibleDC(hdc)
            bmp = win32gui.CreateCompatibleBitmap(hdc, self._screen_w, self._screen_h)
            old_bmp = win32gui.SelectObject(mem_dc, bmp)

            # Прозрачный фон
            self._fill_bg(mem_dc)
            # Рисуем мобов
            with self._data_lock:
                entities = list(self._entities)
                matrix = self._cam_matrix
            self._draw_shapes(mem_dc, entities, matrix)

            win32gui.BitBlt(hdc, 0, 0, self._screen_w, self._screen_h,
                            mem_dc, 0, 0, win32con.SRCCOPY)

            win32gui.SelectObject(mem_dc, old_bmp)
            win32gui.DeleteObject(bmp)
            win32gui.DeleteDC(mem_dc)
            win32gui.EndPaint(hwnd, ps)


            return 0

        if msg in (win32con.WM_DESTROY, win32con.WM_CLOSE):
            self._running = False
            win32gui.PostQuitMessage(0)
            return 0

        return win32gui.DefWindowProc(hwnd, msg, wp, lp)

    def _create_overlay(self, x: int, y: int, w: int, h: int) -> int:
        """Создать прозрачное окно."""
        hinst = win32api.GetModuleHandle(None)
        cls_name = "PoE2EntityRadar"

        # Register
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        wc.hbrBackground = win32gui.GetStockObject(win32con.NULL_BRUSH)
        try:
            win32gui.RegisterClass(wc)
        except:
            pass

        hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT |
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
            cls_name, "",
            win32con.WS_POPUP,
            x, y, w, h,
            0, 0, hinst, None
        )
        if not hwnd:
            return 0

        win32gui.SetLayeredWindowAttributes(hwnd, COLOR_KEY, 0, win32con.LWA_COLORKEY)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        return hwnd

    def _fill_bg(self, hdc: int):
        """Заливка прозрачным цветом."""
        brush = win32gui.CreateSolidBrush(COLOR_KEY)
        win32gui.FillRect(hdc, (0, 0, self._screen_w, self._screen_h), brush)
        win32gui.DeleteObject(brush)

    def _draw_shapes(self, hdc: int, entities: list, matrix: Optional[list]):
        """Нарисовать фигуры."""
        if not matrix:
            return
        for e in entities:
            if e.category != EntityCategory.MONSTER:
                continue
            if e.is_friendly:
                continue
            if (e.hp_max or 0) <= 0 or (e.hp_current or 0) <= 0:
                continue

            sx, sy = self._world_to_screen(e.x, e.z, e.y, matrix, hdc)
            if sx is None:
                continue

            if e.rarity == Rarity.UNIQUE:
                color = COLOR_UNIQUE
                r = 7
            else:
                color = COLOR_ENEMY
                r = 4 + e.rarity  # 4=norm, 5=magic, 6=rare

            brush = win32gui.CreateSolidBrush(color)
            pen = win32gui.CreatePen(win32con.PS_SOLID, 0, color)
            old_br = win32gui.SelectObject(hdc, brush)
            old_pe = win32gui.SelectObject(hdc, pen)

            if e.rarity == Rarity.UNIQUE:
                self._draw_star(hdc, sx, sy, r)
            elif e.rarity == Rarity.RARE:
                self._draw_triangle(hdc, sx, sy, r)
            elif e.rarity == Rarity.MAGIC:
                self._draw_diamond(hdc, sx, sy, r)
            else:
                self._draw_circle(hdc, sx, sy, r)

            win32gui.SelectObject(hdc, old_br)
            win32gui.SelectObject(hdc, old_pe)
            win32gui.DeleteObject(brush)
            win32gui.DeleteObject(pen)

    def _world_to_screen(self, wx: float, wy: float, wz: float,
                         matrix: list, hdc: int) -> Tuple[Optional[int], Optional[int]]:
        sw = self._screen_w
        sh = self._screen_h
        x = wx * matrix[0] + wy * matrix[4] + wz * matrix[8] + matrix[12]
        y = wx * matrix[1] + wy * matrix[5] + wz * matrix[9] + matrix[13]
        w = wx * matrix[3] + wy * matrix[7] + wz * matrix[11] + matrix[15]
        if w < 0.001:
            return None, None
        sx = int((x / w) * 0.5 * sw + sw * 0.5)
        sy = int((1.0 - (y / w) * 0.5) * sh)
        if sx < -100 or sx > sw + 100 or sy < -100 or sy > sh + 100:
            return None, None
        return sx, sy

    @staticmethod
    def _draw_circle(hdc: int, cx: int, cy: int, r: int):
        win32gui.Ellipse(hdc, cx - r, cy - r, cx + r, cy + r)

    @staticmethod
    def _draw_diamond(hdc: int, cx: int, cy: int, r: int):
        pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
        win32gui.Polygon(hdc, pts)

    @staticmethod
    def _draw_triangle(hdc: int, cx: int, cy: int, r: int):
        pts = [(cx, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
        win32gui.Polygon(hdc, pts)

    @staticmethod
    def _draw_star(hdc: int, cx: int, cy: int, r: int):
        pts = []
        for i in range(10):
            angle = math.pi * 2 * i / 10 - math.pi / 2
            radius = r if i % 2 == 0 else r * 0.4
            pts.append((cx + int(radius * math.cos(angle)),
                        cy + int(radius * math.sin(angle))))
        win32gui.Polygon(hdc, pts)

    def _resolve_chain(self) -> bool:
        """Вычислить InGameState через AOB (один раз), закешировать."""
        if self._igs_ptr:
            return True
        try:
            pattern = off.AOB_PATTERNS['game_states']
            matches = self._mem.aob_scan(pattern[0], pattern[1])
            if not matches:
                return False
            match = matches[0]
            disp = struct.unpack('<i', self._mem.read_bytes(match + 3, 4))[0]
            gs = self._mem.read_pointer(match + 7 + disp)
            igs = self._mem.read_pointer(self._mem.read_pointer(gs + 0x08))
            if not igs:
                return False
            self._igs_ptr = igs
            _log.info("AOB chain resolved: igs=0x%X", igs)
            return True
        except Exception as e:
            _log.debug("Chain resolve failed: %s", e)
            return False

    def _tick(self):
        """Обновить данные (быстро, без AOB)."""
        if not self._mem or not self._mem._handle:
            return
        if not self._resolve_chain():
            return
        try:
            ai = self._mem.read_pointer(self._igs_ptr + off.InGameState.AREA_INSTANCE)
            if not ai:
                return

            matrix = self._mem.read_camera_matrix(self._igs_ptr)
            if not matrix:
                return

            entities = self._reader.read_entities(ai)

            with self._data_lock:
                self._entities = entities
                self._cam_matrix = matrix

        except Exception as e:
            _log.debug("Tick: %s", e)

    def _pump_messages(self):
        """Разобрать очередь сообщений."""
        for _ in range(20):
            ret = win32gui.PeekMessage(None, 0, 0, win32con.PM_REMOVE)
            if not ret or not ret[0]:
                break
            msg = ret[1]
            if msg[1] == win32con.WM_QUIT:
                self._running = False
                return
            win32gui.TranslateMessage(msg)
            win32gui.DispatchMessage(msg)

    def run(self):
        """Запустить оверлей."""
        if not self._mem.connect():
            _log.error("Cannot connect to PoE2")
            return

        self._poe_hwnd = self._find_poe_window()
        if not self._poe_hwnd:
            _log.error("PoE2 window not found")
            return

        rect = win32gui.GetClientRect(self._poe_hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        pt = win32gui.ClientToScreen(self._poe_hwnd, (0, 0))

        self._screen_w = w
        self._screen_h = h
        self._win_x = pt[0]
        self._win_y = pt[1]

        _log.info("Overlay %dx%d at (%d,%d) over 0x%X",
                  w, h, pt[0], pt[1], self._poe_hwnd)

        self._hwnd = self._create_overlay(pt[0], pt[1], w, h)
        if not self._hwnd:
            return

        self._running = True

        # Следим за изменением окна PoE2
        try:
            while self._running:
                # Check PoE2 window
                if not win32gui.IsWindow(self._poe_hwnd):
                    _log.info("PoE2 closed")
                    break

                # Update position
                try:
                    new_rect = win32gui.GetClientRect(self._poe_hwnd)
                    new_w = new_rect[2] - new_rect[0]
                    new_h = new_rect[3] - new_rect[1]
                    new_pt = win32gui.ClientToScreen(self._poe_hwnd, (0, 0))

                    if (new_w != self._screen_w or new_h != self._screen_h or
                        new_pt[0] != self._win_x or new_pt[1] != self._win_y):
                        self._screen_w = new_w
                        self._screen_h = new_h
                        self._win_x = new_pt[0]
                        self._win_y = new_pt[1]
                        win32gui.SetWindowPos(self._hwnd, 0,
                            new_pt[0], new_pt[1], new_w, new_h,
                            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
                except:
                    pass

                self._tick()
                win32gui.InvalidateRect(self._hwnd, None, True)
                self._pump_messages()

                time.sleep(0.05)  # 50ms

        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        self._running = False
        if self._hwnd:
            win32gui.DestroyWindow(self._hwnd)
            self._hwnd = 0
        if self._mem:
            self._mem.disconnect()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ov = EntityOverlay()
    ov.run()


if __name__ == "__main__":
    main()
