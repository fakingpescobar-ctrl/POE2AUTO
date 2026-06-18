"""Поиск окна PoE2 и его клиентской области в экранных координатах."""
import win32api
import win32gui


class GameWindow:
    def __init__(self, title_substr):
        self.title_substr = title_substr
        self.hwnd = None

    def find(self):
        matches = []

        def _cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title and self.title_substr.lower() in title.lower():
                    matches.append(hwnd)

        win32gui.EnumWindows(_cb, None)
        self.hwnd = matches[0] if matches else None
        return self.hwnd

    def get_region(self):
        """Клиентская область как dict(left, top, width, height) в экранных координатах."""
        if not self.hwnd:
            return None
        left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
        if right - left <= 0 or bottom - top <= 0:
            return None  # окно свёрнуто
        sx, sy = win32gui.ClientToScreen(self.hwnd, (left, top))
        ex, ey = win32gui.ClientToScreen(self.hwnd, (right, bottom))
        return {"left": sx, "top": sy, "width": ex - sx, "height": ey - sy}

    def is_foreground(self):
        return self.hwnd is not None and win32gui.GetForegroundWindow() == self.hwnd

    @staticmethod
    def primary_region():
        """Регион основного монитора — фолбэк, когда окно не найдено."""
        return {"left": 0, "top": 0,
                "width": win32api.GetSystemMetrics(0),
                "height": win32api.GetSystemMetrics(1)}
