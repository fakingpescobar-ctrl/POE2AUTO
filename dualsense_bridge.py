"""Мост DualSense → виртуальный Xbox 360 (raw HID, как DS4Windows).

Открывает DualSense по HID, парсит байты репорта и пробрасывает ввод в
VirtualXbox360. Реальный DualSense при этом остаётся «как Xbox» для PoE2.

Раскладка кнопок DualSense → Xbox:
  Cross(✕)→A  Circle(◯)→B  Square(□)→X  Triangle(△)→Y
  L1→LB  R1→RB  L2/R2→триггеры  L3/R3→стики  Create→Back  Options→Start
  D-pad через hat-switch.
"""
import logging
import threading

import hid

from virtual_xbox import XUSB

_log = logging.getLogger("poe2auto.bridge")

# DualSense USB VID/PID
DS_VID = 0x054C
DS_PID = 0x0CE6

# Раскладка HID-репорта (Report ID 0x01):
#   b1..b4  стики LX, LY, RX, RY (0-255, центр 128)
#   b5,b6   триггеры L2, R2 (0-255)
#   b8      hat(биты0-3) | Square(0x10) Cross(0x20) Circle(0x40) Triangle(0x80)
#   b9      L1(0x01) R1(0x02) L2btn(0x04) R2btn(0x08) Create(0x10) Options(0x20) L3(0x40) R3(0x80)
#   b10     PS(0x01) Touchpad(0x02) Mute(0x04)

# Значения hat-switch (8 направлений + центр)
_HAT_TO_DPAD = {
    0: XUSB["dpad_up"],
    1: XUSB["dpad_up"] | XUSB["dpad_right"],
    2: XUSB["dpad_right"],
    3: XUSB["dpad_down"] | XUSB["dpad_right"],
    4: XUSB["dpad_down"],
    5: XUSB["dpad_down"] | XUSB["dpad_left"],
    6: XUSB["dpad_left"],
    7: XUSB["dpad_up"] | XUSB["dpad_left"],
    # 8 = центр (ничего не нажато)
}
_ALL_DPAD = XUSB["dpad_up"] | XUSB["dpad_down"] | XUSB["dpad_left"] | XUSB["dpad_right"]

# Активационный репорт — включает input-репорты на DualSense
_ACTIVATION = bytes([0x02] + [0xFF] * 47)


class DualSenseBridge:
    """Читает DualSense по HID и форвардит в VirtualXbox360."""

    def __init__(self, vxbox):
        self._vx = vxbox
        self._dev = None
        self._thread = None
        self._stop = threading.Event()
        # бот выставляет True, пока сам жмёт триггер (чтобы мост не перетирал)
        self.suppress_triggers = False

    def start(self):
        try:
            self._dev = hid.device()
            self._dev.open(DS_VID, DS_PID)
        except Exception as e:
            _log.warning("DualSense не открылся по HID: %s", e)
            return False

        try:
            self._dev.write(_ACTIVATION)
        except Exception as e:
            _log.debug("Активационный репорт: %s (возможно уже активен)", e)

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log.info("DualSense bridge запущен (VID %04X / PID %04X)", DS_VID, DS_PID)
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._dev:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    def _loop(self):
        prev8 = prev9 = 0
        while not self._stop.is_set():
            try:
                report = self._dev.read(64, timeout_ms=200)
                if not report or len(report) < 11:
                    continue

                # --- стики (центр 128 → -32767..32767, ось Y инвертирована) ---
                lx = self._axis(report[1])
                ly = -self._axis(report[2])
                rx = self._axis(report[3])
                ry = -self._axis(report[4])
                self._vx.set_left_stick(lx, ly)
                self._vx.set_right_stick(rx, ry)

                # --- триггеры (0-255), если бот их не занял ---
                if not self.suppress_triggers:
                    self._vx.set_left_trigger(report[5])
                    self._vx.set_right_trigger(report[6])

                b8, b9 = report[8], report[9]

                # --- лицевые и плечевые кнопки по фронту ---
                self._edge(b8, prev8, 0x20, XUSB["a"])   # Cross → A
                self._edge(b8, prev8, 0x40, XUSB["b"])   # Circle → B
                self._edge(b8, prev8, 0x10, XUSB["x"])   # Square → X
                self._edge(b8, prev8, 0x80, XUSB["y"])   # Triangle → Y
                self._edge(b9, prev9, 0x01, XUSB["left_bumper"])
                self._edge(b9, prev9, 0x02, XUSB["right_bumper"])
                self._edge(b9, prev9, 0x40, XUSB["left_stick"])
                self._edge(b9, prev9, 0x80, XUSB["right_stick"])
                self._edge(b9, prev9, 0x10, XUSB["back"])    # Create → Back
                self._edge(b9, prev9, 0x20, XUSB["start"])   # Options → Start

                # --- D-pad (hat-switch) по смене значения ---
                hat, prev_hat = b8 & 0x0F, prev8 & 0x0F
                if hat != prev_hat:
                    self._set_dpad(hat)

                prev8, prev9 = b8, b9
            except Exception as e:
                _log.debug("Ошибка чтения HID: %s", e)

    @staticmethod
    def _axis(v):
        """0-255 (центр 128) → -32767..32767."""
        val = (v - 128) * 257
        return max(-32767, min(32767, val))

    def _edge(self, cur, prev, bit, mask):
        """Нажать/отпустить кнопку Xbox по фронту бита DualSense."""
        now, was = cur & bit, prev & bit
        if now and not was:
            self._vx.press(mask)
        elif not now and was:
            self._vx.release(mask)

    def _set_dpad(self, hat):
        dpad = _HAT_TO_DPAD.get(hat, 0)
        # сперва снимаем все направления, потом ставим актуальные
        self._vx.release(_ALL_DPAD)
        if dpad:
            self._vx.press(dpad)
