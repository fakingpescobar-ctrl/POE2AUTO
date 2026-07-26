import numpy as np
import pytest

from hp_locate import _find_in_region, find_digits_roi, _components, _group_lines, derive_orb


# ─── helpers ──────────────────────────────────────────────────────────────────

class FakeReader:
    """Минимальный reader: bin_thr, _read_glyph(), read_string_crop()."""

    def __init__(self, read_result=None, glyph_results=None, bin_thr=130):
        self.bin_thr = bin_thr
        self._read_result = read_result or ""
        self._glyph_results = glyph_results or {}
        self._glyph_call_count = 0

    def _read_glyph(self, crop, depth=0):
        idx = self._glyph_call_count
        self._glyph_call_count += 1
        if idx in self._glyph_results:
            return self._glyph_results[idx]
        return self._read_result

    def read_string_crop(self, crop):
        return self._read_result


def _make_frame(fh=600, fw=800, color=(30, 30, 30)):
    """Создаёт тёмный кадр (фон HUD)."""
    return np.full((fh, fw, 3), color, dtype=np.uint8)


def _stamp_glyphs(frame, roi_xywh, glyphs, color=255):
    """Рисует белые прямоугольники в frame (эмуляция глифов). roi_xywh = (x, y, w, h)."""
    x0, y0, w, h = roi_xywh
    spacing = w // max(len(glyphs), 1)
    for i, ch in enumerate(glyphs):
        gx = x0 + i * spacing
        cv2_import = __import__("cv2")
        cv2_import.rectangle(frame, (gx, y0), (gx + spacing - 2, y0 + h), (color, color, color), -1)
    return frame


# ─── _components ──────────────────────────────────────────────────────────────

class TestComponents:
    def test_empty(self):
        bin_img = np.zeros((100, 100), dtype=np.uint8)
        assert _components(bin_img, 10, 50) == []

    def test_filters_too_small(self):
        bin_img = np.zeros((100, 100), dtype=np.uint8)
        bin_img[10:15, 10:30] = 255   # h=5 < min_h=10
        assert _components(bin_img, 10, 50) == []

    def test_finds_valid(self):
        bin_img = np.zeros((100, 100), dtype=np.uint8)
        bin_img[10:40, 10:30] = 255   # h=30, w=20
        comps = _components(bin_img, 10, 50)
        assert len(comps) == 1
        assert comps[0][2] == 20  # w


class TestGroupLines:
    def test_single_line(self):
        comps = [(10, 10, 5, 10), (30, 12, 5, 10), (50, 11, 5, 10)]
        lines = _group_lines(comps, y_tol=5)
        assert len(lines) == 1
        assert len(lines[0]["items"]) == 3

    def test_two_lines(self):
        comps = [(10, 10, 5, 10), (30, 12, 5, 10),   # line 1
                 (10, 60, 5, 10), (30, 62, 5, 10)]    # line 2
        lines = _group_lines(comps, y_tol=5)
        assert len(lines) == 2


class TestDeriveOrb:
    def test_basic(self):
        bbox = (100, 500, 200, 520)  # th=20
        orb = derive_orb(bbox, 800, 600)
        assert "cx" in orb and "cy" in orb and "r" in orb
        assert 0 < orb["r"] < 1  # доли кадра


# ─── find_digits_roi ─────────────────────────────────────────────────────────

class TestFindDigitsRoi:
    def test_returns_none_on_empty_frame(self):
        frame = _make_frame()
        reader = FakeReader()
        assert find_digits_roi(frame, reader) is None

    def test_returns_none_when_no_slash(self):
        """Reader возвращает '12345' (без '/') → None."""
        frame = _make_frame()
        reader = FakeReader(read_result="12345")
        assert find_digits_roi(frame, reader) is None

    def test_returns_none_when_reader_returns_question(self):
        """Reader возвращает '???' → None."""
        frame = _make_frame()
        reader = FakeReader(read_result="???")
        assert find_digits_roi(frame, reader) is None

    def test_fallback_to_full_frame(self):
        """Если в region не нашли — пробуем весь кадр. Но на пустом кадре всё равно None."""
        frame = _make_frame()
        reader = FakeReader(read_result="")
        # region = (0.0, 0.55, 0.42, 0.99) — не нашли → fallback на весь кадр → тоже None
        assert find_digits_roi(frame, reader, region=(0.0, 0.55, 0.42, 0.99)) is None

    def test_no_fallback_when_region_is_full(self):
        """Если region уже весь кадр — fallback не нужен."""
        frame = _make_frame()
        reader = FakeReader(read_result="")
        # full region — второго вызова не будет
        assert find_digits_roi(frame, reader, region=(0.0, 0.0, 1.0, 1.0)) is None


class TestFindInRegion:
    def test_none_on_empty(self):
        frame = _make_frame()
        reader = FakeReader()
        assert _find_in_region(frame, reader, None, (0.0, 0.55, 0.42, 0.99),
                               0.010, 0.060, 0.30) is None
