"""Полупрозрачный оверлей поверх игры со статусами функций бота.

tkinter (stdlib), без новых зависимостей. Окно без рамки, always-on-top,
click-through (клики проходят в игру) и не ворует фокус. Работает поверх
оконного/безрамочного режима PoE2 (не поверх эксклюзивного фуллскрина).

Бот пишет состояние в OverlayState (просто атрибуты), оверлей раз в ~150мс
читает их и обновляет подписи. Tkinter крутится в своём потоке; из других
потоков к нему НЕ обращаемся — только пишем в state.
"""
import logging
import threading

_log = logging.getLogger("poe2auto.overlay")

try:
    import tkinter as tk
    HAS_TK = True
except Exception:                       # pragma: no cover
    HAS_TK = False

try:
    import ctypes
    _HAS_CTYPES = True
except Exception:                       # pragma: no cover
    _HAS_CTYPES = False

# --- палитра (тёмная панель, золотой акцент в духе PoE) ---
C_PANEL = "#15151c"
C_ACCENT = "#ffc24b"
C_TEXT = "#d8d8e0"
C_DIM = "#7a7a88"
C_ON = "#46d369"
C_OFF = "#5a5a66"
C_WARN = "#ff5d5d"

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080


class OverlayState:
    """Снимок состояния бота для отрисовки. Бот пишет поля напрямую."""
    def __init__(self):
        self.auto_loot = False
        self.attack_enabled = False  # авто-атака доступна (включена в конфиге)
        self.attack_on = False       # авто-атака включена (F10)
        self.attacking = False       # прямо сейчас держим R1 (виден моб)
        self.flask_enabled = False
        self.diag = False
        self.hp_pct = None          # 0..1 или None
        self.flasks = 0
        self.presses = 0
        self.fps = 0.0
        self.foreground = True
        self.locate_state = "off"   # авто-локализация HUD: "off" | "search" | "ok"
        self.locate_info = ""       # что зацепили, напр. "1371/1371"


def _hp_color(pct):
    if pct is None:
        return C_DIM
    if pct >= 0.88:
        return C_ON
    if pct >= 0.5:
        return C_ACCENT
    return C_WARN


class Overlay:
    """Оверлей в отдельном потоке. start()/stop()/toggle()."""

    def __init__(self, state, region=None):
        self.state = state
        self.region = region or {"left": 0, "top": 0, "width": 1920, "height": 1080}
        self._thread = None
        self._root = None
        self._visible = True
        self._stop = False
        self._rows = {}

    def start(self):
        if not HAS_TK:
            _log.warning("tkinter недоступен — оверлей отключён")
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop = True
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:
                pass

    def toggle(self):
        self._visible = not self._visible
        root = self._root
        if root is not None:
            try:
                root.after(0, self._apply_visibility)
            except Exception:
                pass
        return self._visible

    # --- внутреннее (поток tkinter) ---

    def _apply_visibility(self):
        if not self._root:
            return
        if self._visible:
            self._root.deiconify()
        else:
            self._root.withdraw()

    def _run(self):
        root = tk.Tk()
        self._root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.90)
        root.configure(bg=C_PANEL)

        x = self.region["left"] + 24
        y = self.region["top"] + 64
        root.geometry(f"+{x}+{y}")

        pad = tk.Frame(root, bg=C_PANEL, padx=14, pady=10)
        pad.pack(fill="both", expand=True)

        # акцентная полоса + заголовок
        head = tk.Frame(pad, bg=C_PANEL)
        head.pack(fill="x")
        tk.Frame(head, bg=C_ACCENT, width=4, height=18).pack(side="left", padx=(0, 8))
        tk.Label(head, text="POE2AUTO", bg=C_PANEL, fg=C_ACCENT,
                 font=("Segoe UI Semibold", 13)).pack(side="left")
        self._rows["focus"] = tk.Label(head, text="●", bg=C_PANEL, fg=C_ON,
                                        font=("Segoe UI", 11))
        self._rows["focus"].pack(side="right")

        tk.Frame(pad, bg="#2a2a34", height=1).pack(fill="x", pady=(8, 6))

        # строки-статусы
        self._rows["loot"] = self._status_row(pad, "Авто-подбор", "F8")
        self._rows["attack"] = self._status_row(pad, "Авто-атака", "F10")
        self._rows["flask"] = self._status_row(pad, "Авто-фляска", "")
        self._rows["locate"] = self._status_row(pad, "Калибровка HUD", "")
        self._rows["diag"] = self._status_row(pad, "Диагностика", "F7")

        tk.Frame(pad, bg="#2a2a34", height=1).pack(fill="x", pady=(8, 6))

        self._rows["stats"] = tk.Label(pad, text="", bg=C_PANEL, fg=C_DIM,
                                       font=("Consolas", 9), anchor="w", justify="left")
        self._rows["stats"].pack(fill="x")

        tk.Label(pad, text="F8 лут · F7 диаг · F9 снимок · F6 скрыть · F12 выход",
                 bg=C_PANEL, fg=C_DIM, font=("Segoe UI", 8)).pack(fill="x", pady=(8, 0))

        # click-through + не воровать фокус
        root.update_idletasks()
        self._make_click_through()

        self._tick()
        try:
            root.mainloop()
        except Exception as e:                      # pragma: no cover
            _log.debug("overlay mainloop: %s", e)

    def _status_row(self, parent, title, key):
        row = tk.Frame(parent, bg=C_PANEL)
        row.pack(fill="x", pady=2)
        dot = tk.Label(row, text="●", bg=C_PANEL, fg=C_OFF, font=("Segoe UI", 11))
        dot.pack(side="left")
        tk.Label(row, text=title, bg=C_PANEL, fg=C_TEXT,
                 font=("Segoe UI", 10)).pack(side="left", padx=(6, 0))
        if key:
            tk.Label(row, text=key, bg=C_PANEL, fg=C_DIM,
                     font=("Segoe UI", 8)).pack(side="right")
        val = tk.Label(row, text="OFF", bg=C_PANEL, fg=C_OFF, font=("Segoe UI Semibold", 9))
        val.pack(side="right", padx=(0, 8))
        return {"dot": dot, "val": val}

    def _make_click_through(self):
        if not _HAS_CTYPES:
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id())
            if not hwnd:
                hwnd = self._root.winfo_id()
            user32 = ctypes.windll.user32
            ex = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            ex |= (_WS_EX_LAYERED | _WS_EX_TRANSPARENT
                   | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
            user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex)
        except Exception as e:                      # pragma: no cover
            _log.debug("click-through не выставлен: %s", e)

    def _set(self, row, on, text, color=None):
        row["dot"].config(fg=C_ON if on else C_OFF)
        row["val"].config(text=text, fg=color or (C_ON if on else C_OFF))

    def _tick(self):
        if self._stop:
            try:
                self._root.destroy()
            except Exception:
                pass
            return
        s = self.state
        self._set(self._rows["loot"], s.auto_loot, "ON" if s.auto_loot else "OFF")

        if not s.attack_enabled:
            self._set(self._rows["attack"], False, "—", C_DIM)
        elif s.attacking:
            self._set(self._rows["attack"], True, "АТАКА ⚔", C_WARN)   # держим R1: цель видна
        else:
            self._set(self._rows["attack"], s.attack_on,
                      "ждёт" if s.attack_on else "OFF", C_ON if s.attack_on else C_OFF)

        if not s.flask_enabled:
            self._set(self._rows["flask"], False, "—", C_DIM)
        else:
            hp = "—" if s.hp_pct is None else f"{s.hp_pct*100:.0f}%"
            self._set(self._rows["flask"], True, hp, _hp_color(s.hp_pct))

        if s.locate_state == "ok":
            self._set(self._rows["locate"], True,
                      f"захват ✓ {s.locate_info}".rstrip(), C_ON)
        elif s.locate_state == "search":
            self._set(self._rows["locate"], True, "поиск…", C_ACCENT)
        else:
            self._set(self._rows["locate"], False, "—", C_DIM)

        self._set(self._rows["diag"], s.diag, "ON" if s.diag else "OFF",
                  C_WARN if s.diag else C_OFF)   # диаг подсвечиваем как «внимание» (тормозит)

        self._rows["focus"].config(fg=C_ON if s.foreground else C_OFF)
        self._rows["stats"].config(
            text=f"фляг:{s.flasks}  подбор:{s.presses}  fps:{s.fps:.0f}")

        try:
            self._root.after(150, self._tick)
        except Exception:
            pass
