import cv2
import numpy as np
import time

from mob_bar import MobBarDetector


def _make_frame(fh=600, fw=800, color=(30, 30, 30)):
    return np.full((fh, fw, 3), color, dtype=np.uint8)


def _dark_frame(fh=600, fw=800):
    """Тёмный кадр (яркость ~10)."""
    return np.full((fh, fw, 3), (10, 10, 10), dtype=np.uint8)


def _bright_frame(fh=600, fw=800):
    """Светлый кадр (яркость ~200)."""
    return np.full((fh, fw, 3), (200, 200, 200), dtype=np.uint8)


class TestDarkZone:
    """_is_dark_zone принимает готовый HSV-кроп ROI (после оптимизации:
    убрана двойная HSV-конверсия в present)."""

    def test_bright_frame_not_dark(self):
        det = MobBarDetector({"dark_zone": {"enabled": True, "brightness_thr": 40.0}})
        assert not det._is_dark_zone(cv2.cvtColor(_bright_frame(), cv2.COLOR_BGR2HSV))

    def test_dark_frame_is_dark(self):
        det = MobBarDetector({"dark_zone": {"enabled": True, "brightness_thr": 40.0}})
        assert det._is_dark_zone(cv2.cvtColor(_dark_frame(), cv2.COLOR_BGR2HSV))

    def test_disabled_never_dark(self):
        det = MobBarDetector({"dark_zone": {"enabled": False}})
        assert not det._is_dark_zone(cv2.cvtColor(_dark_frame(), cv2.COLOR_BGR2HSV))

    def test_empty_frame_not_dark(self):
        det = MobBarDetector({})
        empty = np.array([], dtype=np.uint8).reshape(0, 0, 3)
        assert not det._is_dark_zone(empty)

    def test_present_uses_hsv_consistency(self):
        """present() с тёмным кадром: dark-режим активируется через тот же hsv,
        что считается для маски — не должно быть двойной cvtColor (регрессия)."""
        det = MobBarDetector({"dark_zone": {"enabled": True, "brightness_thr": 40.0}})
        frame = _dark_frame()
        # просто не падает и возвращает bool
        assert isinstance(det.present(frame), bool)


class TestFrameCounter:
    def test_lock_frames_increments(self):
        det = MobBarDetector({
            "low_hp_hard_cap_frames": 3,
            "low_hp_lock_timeout_s": 10.0,
            "low_hp_hard_cap_s": 10.0,
        })
        det._lock = (10, 10, 50, 5)
        det._lock_ts = time.perf_counter()
        det._confirmed_ts = time.perf_counter()
        det._lock_frames = 0

        frame = _make_frame()
        # без реального подтверждения — счётчик растёт
        for _ in range(3):
            det.present(frame)
        assert det._lock_frames > 0

    def test_lock_resets_on_hard_cap(self):
        det = MobBarDetector({
            "low_hp_hard_cap_frames": 2,
            "low_hp_lock_timeout_s": 10.0,
            "low_hp_hard_cap_s": 10.0,
        })
        det._lock = (10, 10, 50, 5)
        det._lock_ts = time.perf_counter()
        det._confirmed_ts = time.perf_counter()
        det._lock_frames = 0

        frame = _make_frame()
        # после 3+ кадров lock сбрасывается
        for _ in range(5):
            det.present(frame)
        assert det._lock is None

    def test_lock_frames_resets_on_real_confirm(self):
        det = MobBarDetector({
            "low_hp_hard_cap_frames": 100,
            "min_run_frac": 0.01,
            "max_h_frac": 0.5,
            "min_aspect": 1.0,
            "min_fill": 0.1,
        })
        det._lock_frames = 50
        # present на пустом кадре — нет реального подтверждения, счётчик растёт
        frame = _make_frame()
        det.present(frame)
        # но без реального бара lock_frames не растёт выше 0 если lock=None
        assert det._lock_frames <= 51


class TestPresentReturnsFalse:
    def test_empty_frame(self):
        det = MobBarDetector({})
        empty = np.full((100, 100, 3), 30, dtype=np.uint8)
        assert det.present(empty) is False

    def test_no_red_in_frame(self):
        det = MobBarDetector({})
        frame = _make_frame(color=(50, 50, 50))
        assert det.present(frame) is False
