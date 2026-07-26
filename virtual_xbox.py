"""Виртуальный Xbox 360 контроллер (ViGEmBus через vgamepad).

PoE2 игнорирует виртуальные геймпады, когда подключён реальный DualSense.
Решение (как у DS4Windows): создаём виртуальный Xbox 360, держим его «живым»
heartbeat-потоком, а ввод реального DualSense пробрасываем в него мостом.

Все обращения к vgamepad идут через один лок — чтобы heartbeat, мост и команды
бота не дёргали контроллер одновременно.
"""
import logging
import threading
import time

_log = logging.getLogger("poe2auto.vxbox")

try:
    import vgamepad as vg
    HAS_VGAMEPAD = True
except ImportError:
    HAS_VGAMEPAD = False
    _log.warning("vgamepad не установлен. pip install vgamepad")

# XInput button masks
XUSB = {
    "dpad_up":      0x0001,
    "dpad_down":    0x0002,
    "dpad_left":    0x0004,
    "dpad_right":   0x0008,
    "start":        0x0010,
    "back":         0x0020,
    "left_stick":   0x0040,
    "right_stick":  0x0080,
    "left_bumper":  0x0100,
    "right_bumper": 0x0200,
    "guide":        0x0400,
    "a":            0x1000,
    "b":            0x2000,
    "x":            0x4000,
    "y":            0x8000,
}

_HEARTBEAT_HZ = 20  # 50 мс — держим контроллер активным для XInput


class VirtualXbox360:
    """Потокобезопасная обёртка над виртуальным Xbox 360."""

    def __init__(self):
        self.enabled = False
        self._pad = None
        self._lock = threading.Lock()
        self._hb_stop = threading.Event()
        self._hb_thread = None

    def start(self):
        if not HAS_VGAMEPAD:
            _log.error("vgamepad недоступен — виртуальный геймпад не создан")
            return False
        self._pad = vg.VX360Gamepad()
        # reset + update инициализируют контроллер для XInput
        self._pad.reset()
        self._pad.update()
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._hb_thread.start()
        self.enabled = True
        _log.info("Виртуальный Xbox 360 создан (heartbeat %d Гц)", _HEARTBEAT_HZ)
        return True

    def stop(self):
        self._hb_stop.set()
        if self._hb_thread:
            self._hb_thread.join(timeout=2.0)
            self._hb_thread = None
        if self._pad:
            try:
                self._pad.reset()
                self._pad.update()
            except Exception as e:
                _log.debug("Ошибка reset при остановке: %s", e)
            self._pad = None
        self.enabled = False
        _log.info("Виртуальный Xbox 360 остановлен")

    def _heartbeat(self):
        while not self._hb_stop.is_set():
            with self._lock:
                if self._pad:
                    try:
                        self._pad.update()
                    except Exception as e:
                        _log.warning("Heartbeat update failed: %s", e)
            self._hb_stop.wait(1.0 / _HEARTBEAT_HZ)

    # --- низкоуровневые потокобезопасные операции ---

    def press(self, mask):
        if not self._pad:
            return
        with self._lock:
            self._pad.press_button(button=mask)
            self._pad.update()

    def release(self, mask):
        if not self._pad:
            return
        with self._lock:
            self._pad.release_button(button=mask)
            self._pad.update()

    def set_left_stick(self, x, y):
        if not self._pad:
            return
        with self._lock:
            self._pad.left_joystick(x_value=x, y_value=y)
            self._pad.update()

    def set_right_stick(self, x, y):
        if not self._pad:
            return
        with self._lock:
            self._pad.right_joystick(x_value=x, y_value=y)
            self._pad.update()

    def set_left_trigger(self, value):
        if not self._pad:
            return
        with self._lock:
            self._pad.left_trigger(value=value)
            self._pad.update()

    def set_right_trigger(self, value):
        if not self._pad:
            return
        with self._lock:
            self._pad.right_trigger(value=value)
            self._pad.update()

    def tap(self, mask, hold_ms=80):
        """Коротко нажать и отпустить кнопку (для команд бота, напр. подбор=A)."""
        if not self._pad:
            return
        self.press(mask)
        time.sleep(hold_ms / 1000.0)
        self.release(mask)

    def tap_left_trigger(self, hold_ms=120, value=255):
        """Коротко выжать левый триггер (L2) — напр. HP фляга."""
        if not self._pad:
            return
        self.set_left_trigger(value)
        time.sleep(hold_ms / 1000.0)
        self.set_left_trigger(0)

    def tap_right_trigger(self, hold_ms=120, value=255):
        """Коротко выжать правый триггер (R2)."""
        if not self._pad:
            return
        self.set_right_trigger(value)
        time.sleep(hold_ms / 1000.0)
        self.set_right_trigger(0)
