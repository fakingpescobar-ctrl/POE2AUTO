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
C_RADIAL_BG = "#010101"  # цвет фона radial-окна (прозрачный через transparentcolor)

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
        self.attack_on = False       # авто-атака включена (хоткей attack в config.yaml)
        self.attacking = False       # прямо сейчас держим R1 (виден моб)
        self.flask_enabled = False
        self.flask_on = True         # авто-фляска включена (хоткей flask в config.yaml, F3)
        self.diag = False
        self.hp_pct = None          # 0..1 или None
        # Точные пулы здоровья из памяти (жизнь/энергосчёт/ward) для отображения.
        # None = данные недоступны (не memory-режим или ридер не читается).
        self.hp_cur = None
        self.hp_max = None
        self.es_cur = None
        self.es_max = None
        self.ward_cur = None
        self.ward_max = None
        self.flasks = 0
        self.presses = 0
        self.fps = 0.0
        self.foreground = True
        self.locate_state = "off"   # авто-локализация HUD: "off" | "search" | "ok"
        self.locate_info = ""       # что зацепили, напр. "1371/1371"
        self.notification = ""      # краткое сообщение (появляется и исчезает)
        self.notification_until = 0.0  # когда скрыть (time.time())
        # debug info (показывается при diag=True)
        self.debug_hp = ""          # "num=1200/1371 orb=0.88 src=num streak=0"
        self.debug_mob = ""         # "mob=CV dark=no match=strict(45,12,8.2,0.7)"
        self.debug_zone = ""        # "zone=ok range=15.0"
        self.memory_status = "off"  # "ok" | "fallback" | "off" — memory-режим атаки/фляги
        self.attack_mode = ""      # "MEM" | "CV" | "" — текущий режим детекции автоатаки
        self.atk_fallback = ""     # причина фолбэка memory→CV, пусто если нет
        # Модули ядра: [(имя, ok, деталь)] — зелёная/красная точка в оверлее.
        # ok=True — модуль подключен и работает; ok=False — не подключен/ошибка.
        self.modules = []
        # Индикаторы модуля памяти (для нижней строки оверлея).
        # mem_connected — процесс PoE2 найден и подключён (OpenProcess handle есть)
        # mem_reading   — память реально читается (game_state != 0, данные идут)
        self.mem_connected = False
        self.mem_reading = False
        # Счётчики сущностей (для понимания что видит ридер)
        self.ent_total = 0       # всего сущностей в зоне
        self.ent_hostile = 0     # враждебных мобов всего
        self.ent_in_range = 0    # враждебных в радиусе атаки
        self.nearest_dist = None  # distance до ближайшего враждебного моба (grid), None если нет
        # Наглядный круг радиуса авто-атаки вокруг персонажа (хоткей radial, F4)
        self.radial_visible = False  # показывать круг радиуса поверх игры
        self.attack_range = 0.0      # текущий attack_range (grid) — для радиуса круга
        self.radial_scale = 20.0     # px на 1 grid (масштаб экрана, калибруется)
        self.radial_center = (0, 0)  # смещение персонажа от центра окна (px), из radial.center_offset_xy


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

    def __init__(self, state, region=None, hotkeys=None):
        self.state = state
        self.region = region or {"left": 0, "top": 0, "width": 1920, "height": 1080}
        self.hotkeys = hotkeys or {}
        self._thread = None
        self._root = None
        self._visible = True
        self._stop = False
        self._rows = {}
        self._rad = None          # radial-окно (круг радиуса поверх игры)
        self._rad_canvas = None

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

        # строки-статусы (метки хоткеев — из конфига, с дефолтами на случай отсутствия)
        hk = self.hotkeys
        k_toggle = hk.get("toggle", "f8").upper()
        k_attack = hk.get("attack", "f2").upper()
        k_diag = hk.get("diag", "f7").upper()
        k_shot = hk.get("snapshot", "f9").upper()
        k_overlay = hk.get("overlay", "f6").upper()
        k_quit = hk.get("quit", "f12").upper()
        k_flask = hk.get("flask", "f3").upper()
        k_radial = hk.get("radial", "f4").upper()
        self._rows["loot"] = self._status_row(pad, "Авто-подбор", k_toggle)
        self._rows["attack"] = self._status_row(pad, "Авто-атака", k_attack)
        self._rows["attack_mode"] = self._status_row(pad, "Режим", "")
        self._rows["flask"] = self._status_row(pad, "Авто-фляска", k_flask)
        # Ресурсы: две строки как в HUD — LIFE (фактический HP-пул) и E.SHIELD
        # (текущее количество энергощита). Значения из памяти (оверлей всегда честный).
        self._rows["vitals_hp"] = self._status_row(pad, "LIFE", "")
        self._rows["vitals_es"] = self._status_row(pad, "E.SHIELD", "")
        self._rows["locate"] = self._status_row(pad, "Калибровка HUD", "")
        self._rows["diag"] = self._status_row(pad, "Диагностика", k_diag)
        self._rows["radial"] = self._status_row(pad, "Радиус", k_radial)

        # Модули ядра: динамические строки с зелёной/красной точкой
        tk.Label(pad, text="МОДУЛИ ЯДРА", bg=C_PANEL, fg=C_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 0))
        self._mods_frame = tk.Frame(pad, bg=C_PANEL)
        self._mods_frame.pack(fill="x")
        self._module_rows = []   # [row_dict] — строки модулей (пересоздаются при смене списка)
        self._module_names = []  # имена модулей, для которых созданы строки

        tk.Frame(pad, bg="#2a2a34", height=1).pack(fill="x", pady=(8, 6))

        # stats-строка: основной stats слева + memory-индикатор справа (свой цвет)
        stats_row = tk.Frame(pad, bg=C_PANEL)
        stats_row.pack(fill="x")
        self._rows["stats"] = tk.Label(stats_row, text="", bg=C_PANEL, fg=C_DIM,
                                       font=("Consolas", 9), anchor="w", justify="left")
        self._rows["stats"].pack(side="left")
        self._rows["stats_mem"] = tk.Label(stats_row, text="", bg=C_PANEL, fg=C_DIM,
                                           font=("Consolas", 9), anchor="e", justify="right")
        self._rows["stats_mem"].pack(side="right")

        # вторая строка: счётчики сущностей (entities/mobs/nearest) — только для memory-режима
        self._rows["stats_ent"] = tk.Label(pad, text="", bg=C_PANEL, fg=C_DIM,
                                           font=("Consolas", 9), anchor="w", justify="left")
        self._rows["stats_ent"].pack(fill="x")

        self._rows["notif"] = tk.Label(pad, text="", bg=C_PANEL, fg=C_WARN,
                                       font=("Segoe UI Semibold", 9), anchor="w")
        self._rows["notif"].pack(fill="x")

        self._rows["debug_hp"] = tk.Label(pad, text="", bg=C_PANEL, fg=C_DIM,
                                          font=("Consolas", 8), anchor="w", justify="left")
        self._rows["debug_mob"] = tk.Label(pad, text="", bg=C_PANEL, fg=C_DIM,
                                           font=("Consolas", 8), anchor="w", justify="left")
        self._rows["debug_zone"] = tk.Label(pad, text="", bg=C_PANEL, fg=C_DIM,
                                            font=("Consolas", 8), anchor="w", justify="left")

        tk.Label(pad, text=f"{k_toggle} лут · {k_attack} атака · {k_flask} фляга · {k_radial} радиус · "
                            f"{k_diag} диаг · {k_shot} снимок · {k_overlay} скрыть · {k_quit} выход",
                 bg=C_PANEL, fg=C_DIM, font=("Segoe UI", 8)).pack(fill="x", pady=(8, 0))

        # click-through + не воровать фокус
        root.update_idletasks()
        self._make_click_through()

        # radial-окно: прозрачный canvas поверх игрового региона, рисует круг
        # радиуса авто-атаки (хоткей radial, F4). Чёрный цвет = прозрачный
        # (transparentcolor), сам круг цветной. Клики проходят в игру.
        rad = tk.Toplevel(root)
        rad.overrideredirect(True)
        rad.attributes("-topmost", True)
        reg = self.region
        rad.geometry(f"{reg['width']}x{reg['height']}+{reg['left']}+{reg['top']}")
        rad.configure(bg=C_RADIAL_BG)
        try:
            rad.attributes("-transparentcolor", C_RADIAL_BG)
        except Exception:                       # pragma: no cover
            _log.debug("transparentcolor не поддержан — radial фон непрозрачен")
        self._rad_canvas = tk.Canvas(rad, bg=C_RADIAL_BG, highlightthickness=0)
        self._rad_canvas.pack(fill="both", expand=True)
        self._rad = rad
        rad.withdraw()
        root.update_idletasks()
        self._apply_click_through(rad)

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
        return {"dot": dot, "val": val, "frame": row}

    def _make_click_through(self):
        if not _HAS_CTYPES:
            return
        self._apply_click_through(self._root)

    def _apply_click_through(self, win):
        if not _HAS_CTYPES:
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            if not hwnd:
                hwnd = win.winfo_id()
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
        elif not s.flask_on:
            self._set(self._rows["flask"], False, "OFF", C_OFF)
        else:
            hp = "—" if s.hp_pct is None else f"{s.hp_pct*100:.0f}%"
            self._set(self._rows["flask"], True, hp, _hp_color(s.hp_pct))

        # Ресурсы: LIFE — фактический HP-пул, E.SHIELD — текущий энергощит.
        # Значения из памяти (reader/memory-режим); в CV — прочерк.
        if s.hp_cur is not None:
            self._set(self._rows["vitals_hp"], True, f"{s.hp_cur}", _hp_color(s.hp_pct))
        else:
            self._set(self._rows["vitals_hp"], False, "—", C_DIM)
        if s.es_cur is not None:
            self._set(self._rows["vitals_es"], True, f"{s.es_cur}", C_ON)
        else:
            self._set(self._rows["vitals_es"], False, "—", C_DIM)

        if s.locate_state == "ok":
            self._set(self._rows["locate"], True,
                      f"захват ✓ {s.locate_info}".rstrip(), C_ON)
        elif s.locate_state == "search":
            self._set(self._rows["locate"], True, "поиск…", C_ACCENT)
        else:
            self._set(self._rows["locate"], False, "—", C_DIM)

        self._set(self._rows["diag"], s.diag, "ON" if s.diag else "OFF",
                  C_WARN if s.diag else C_OFF)   # диаг подсвечиваем как «внимание» (тормозит)

        # Радиус (круг F4): показывает включён ли показ круга и текущий attack_range
        if s.radial_visible:
            self._set(self._rows["radial"], True,
                      f"R={getattr(s, 'attack_range', 0.0):.0f}", C_ON)
        else:
            self._set(self._rows["radial"], False, "OFF", C_OFF)

        # Режим детекции автоатаки
        if s.attack_enabled and s.attack_on:
            if s.attack_mode == "MEM":
                mode_text = "Memory 🧠"
                mode_color = C_ON
            elif s.attack_mode == "RDR":
                mode_text = "Reader 📡"
                mode_color = C_ON
            elif s.attack_mode == "CV":
                mode_text = "Computer Vision 👁"
                mode_color = C_ACCENT
            else:
                mode_text = "—"
                mode_color = C_DIM
            if s.atk_fallback:
                mode_text += " (fallback)"
                mode_color = C_WARN
            self._set(self._rows["attack_mode"], True, mode_text, mode_color)
        elif s.attack_enabled:
            self._set(self._rows["attack_mode"], True, s.attack_mode or "—", C_DIM)
        else:
            self._set(self._rows["attack_mode"], False, "—", C_DIM)

        # Модули ядра: строки создаём/пересоздаём только при смене состава имён
        names = [m[0] for m in s.modules]
        if names != self._module_names:
            self._module_names = names
            for r in self._module_rows:
                try:
                    r["frame"].destroy()
                except Exception:
                    pass
            self._module_rows = []
            for name in names:
                self._module_rows.append(self._status_row(self._mods_frame, name, ""))
        for row, (_name, ok, detail) in zip(self._module_rows, s.modules):
            row["dot"].config(fg=C_ON if ok else C_WARN)
            row["val"].config(text=detail, fg=C_ON if ok else C_WARN)

        self._rows["focus"].config(fg=C_ON if s.foreground else C_OFF)
        # Основной stats (левый Label): фляги/подбор/fps — без индикаторов памяти.
        stats = f"фляг:{s.flasks}  подбор:{s.presses}  fps:{s.fps:.0f}"
        self._rows["stats"].config(text=stats)

        # Memory-индикатор (правый Label): показывает состояние модуля памяти.
        # Показывается только для memory-режимов (RDR/MEM), для CV — скрыт.
        mode_tag = s.attack_mode if s.attack_mode in ("RDR", "MEM") else ""
        if mode_tag:
            if s.mem_connected and s.mem_reading:
                # Всё работает: процесс подключён + память читается → зелёный.
                mem_text = f"{mode_tag} inj✓ mem✓"
                mem_color = C_ON
            elif s.mem_connected:
                # Подключились к процессу, но память не читается (игра в меню/loading) → жёлтый.
                mem_text = f"{mode_tag} inj✓ mem✗"
                mem_color = C_ACCENT
            else:
                # Процесс не найден / OpenProcess не прошёл → красный.
                mem_text = f"{mode_tag} inj✗"
                mem_color = C_WARN
            self._rows["stats_mem"].config(text=mem_text, fg=mem_color)
        else:
            # CV-режим или режим не определён — индикатор памяти скрыт.
            self._rows["stats_mem"].config(text="", fg=C_DIM)

        # Счётчики сущностей (вторая строка) — только для memory-режима.
        if mode_tag:
            nd = f"{s.nearest_dist:.0f}" if s.nearest_dist is not None else "-"
            ent_text = f"сущностей:{s.ent_total}  мобов:{s.ent_hostile}  в_радиусе:{s.ent_in_range}  ближ.дист:{nd}"
            # Цвет: есть мобы в радиусе → зелёный, есть мобы но не в радиусе → жёлтый, нет → серый
            if s.ent_in_range > 0:
                ent_color = C_ON
            elif s.ent_hostile > 0:
                ent_color = C_ACCENT
            else:
                ent_color = C_DIM
            self._rows["stats_ent"].config(text=ent_text, fg=ent_color)
        else:
            self._rows["stats_ent"].config(text="", fg=C_DIM)

        import time as _time
        # Причина fallback (RDR/MEM → CV) держится на строке уведомлений постоянно,
        # пока читалка не переподключится. Обычные уведомления перекрывают её на своё время.
        if s.atk_fallback:
            self._rows["notif"].config(text=s.atk_fallback, fg=C_WARN)
        elif s.notification and _time.time() < s.notification_until:
            self._rows["notif"].config(text=s.notification, fg=C_WARN)
        else:
            self._rows["notif"].config(text="")

        if s.diag and (s.debug_hp or s.debug_mob):
            self._rows["debug_hp"].config(text=s.debug_hp, fg=C_ACCENT)
            self._rows["debug_mob"].config(text=s.debug_mob, fg=C_ACCENT)
            self._rows["debug_zone"].config(text=s.debug_zone, fg=C_ACCENT)
        else:
            self._rows["debug_hp"].config(text="")
            self._rows["debug_mob"].config(text="")
            self._rows["debug_zone"].config(text="")

        # Оверлей ВСЕГДА поверх игры: не прячем при потере фокуса (юзер хочет
        # видеть панель, даже когда кликнул в другое окно). Топмост выставлен
        # в _run; скрытие только вручную (хоткей скрыть).
        self._update_radial(s)

        try:
            self._root.after(150, self._tick)
        except Exception:
            pass

    def _update_radial(self, s):
        """Круг радиуса авто-атаки вокруг персонажа (хоткей radial, F4)."""
        if self._rad is None:
            return
        # Круг виден пока включён (F4) — не зависит от фокуса игры.
        want = bool(s.radial_visible)
        if not want:
            if self._rad.winfo_viewable():
                self._rad.withdraw()
            return
        if not self._rad.winfo_viewable():
            self._rad.deiconify()
        c = self._rad_canvas
        c.delete("all")
        r = float(getattr(s, "attack_range", 0.0) or 0.0)
        if r <= 0:
            return
        scale = float(getattr(s, "radial_scale", 20.0) or 20.0)
        off = getattr(s, "radial_center", None) or (0, 0)
        w = self.region["width"]
        h = self.region["height"]
        cx = w / 2.0 + float(off[0])
        cy = h / 2.0 + float(off[1])
        rx = r * scale
        ry = rx / 2.0  # изометрия PoE2: вертикаль экрана сжата вдвое
        color = C_ACCENT
        c.create_oval(cx - rx, cy - ry, cx + rx, cy + ry, outline=color, width=2)
        c.create_text(cx, cy - ry - 10, text=f"R={r:.0f}", fill=color,
                      font=("Consolas", 11, "bold"))
        c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=color, outline="")
