"""Захват региона экрана: dxcam (DirectX) с фолбэком на mss. Возвращает BGR-кадр."""
import logging
import time

import numpy as np

_log = logging.getLogger("poe2auto.screen")


class ScreenCapture:
    def __init__(self, backend="dxcam"):
        self.backend = backend
        self._dxcam = None
        self._mss = None
        self._last = None
        self._last_none_time = 0.0
        self._none_streak = 0
        self._init_backend()

    def _init_backend(self):
        if self.backend == "dxcam":
            try:
                import dxcam
                self._dxcam = dxcam.create(output_color="BGR")
                if self._dxcam is None:
                    raise RuntimeError("dxcam.create() вернул None")
                return
            except Exception as e:
                _log.warning("dxcam недоступен (%s) — фолбэк на mss", e)
                self.backend = "mss"
        self._init_mss()

    def _init_mss(self):
        import mss
        self._mss = mss.mss()
        self.backend = "mss"

    def grab(self, region):
        """region: dict(left, top, width, height) → BGR-кадр (H, W, 3) или None."""
        if self.backend == "dxcam":
            l, t = region["left"], region["top"]
            r, b = l + region["width"], t + region["height"]
            try:
                frame = self._dxcam.grab(region=(l, t, r, b))
            except Exception:
                self._init_mss()
                return self.grab(region)
            if frame is None:
                now = time.perf_counter()
                self._none_streak += 1
                # debounce: не логируем каждое None, только каждые 2с
                if self._none_streak == 1 or now - self._last_none_time > 2.0:
                    _log.debug("dxcam вернул None (streak=%d)", self._none_streak)
                    self._last_none_time = now
                # dxcam внутренне восстанавливается — просто возвращаем последний кадр
                return self._last
            self._none_streak = 0
            self._last = frame
            return frame

        mon = {"left": region["left"], "top": region["top"],
               "width": region["width"], "height": region["height"]}
        img = np.asarray(self._mss.grab(mon))  # BGRA
        self._last = np.ascontiguousarray(img[:, :, :3])  # BGR
        return self._last
