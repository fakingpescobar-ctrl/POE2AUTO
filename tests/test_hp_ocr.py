"""Unit-тесты для hp_ocr.py — чистые функции без зависимостей от шаблонов/игры."""
import numpy as np
import cv2
import pytest

from hp_ocr import roi_px, binarize, trim, segment, norm_glyph, GLYPH_H, GLYPH_W


class TestRoiPx:
    def test_full_frame(self):
        assert roi_px({"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}, 1920, 1080) == (0, 0, 1920, 1080)

    def test_partial(self):
        assert roi_px({"x1": 0.1, "y1": 0.2, "x2": 0.5, "y2": 0.8}, 1000, 500) == (100, 100, 500, 400)

    def test_zero_size(self):
        assert roi_px({"x1": 0.5, "y1": 0.5, "x2": 0.5, "y2": 0.5}, 1920, 1080) == (960, 540, 960, 540)


class TestBinarize:
    def test_white_above_threshold(self):
        gray = np.array([[100, 200], [50, 150]], dtype=np.uint8)
        b = binarize(gray, 120)
        assert b[0, 0] == 0
        assert b[0, 1] == 255
        assert b[1, 0] == 0
        assert b[1, 1] == 255

    def test_all_black(self):
        gray = np.zeros((10, 10), dtype=np.uint8)
        b = binarize(gray, 128)
        assert b.sum() == 0

    def test_all_white(self):
        gray = np.full((10, 10), 255, dtype=np.uint8)
        b = binarize(gray, 128)
        assert b.sum() == 255 * 100


class TestTrim:
    def test_empty(self):
        assert trim(np.array([], dtype=np.uint8)).size == 0

    def test_all_black(self):
        crop = np.zeros((5, 5), dtype=np.uint8)
        result = trim(crop)
        assert result.size == 0 or result.shape[0] == 0 or result.shape[1] == 0

    def test_single_pixel(self):
        crop = np.zeros((5, 5), dtype=np.uint8)
        crop[2, 3] = 255
        result = trim(crop)
        assert result.shape == (1, 1)
        assert result[0, 0] == 255

    def test_rect(self):
        crop = np.zeros((10, 10), dtype=np.uint8)
        crop[2:5, 3:7] = 255
        result = trim(crop)
        assert result.shape == (3, 4)
        assert result.sum() == 3 * 4 * 255


class TestNormGlyph:
    def test_output_shape(self):
        glyph = np.zeros((20, 12), dtype=np.uint8)
        glyph[5:15, 3:9] = 255
        result = norm_glyph(glyph)
        assert result.shape == (GLYPH_H, GLYPH_W)

    def test_preserves_content(self):
        glyph = np.zeros((20, 12), dtype=np.uint8)
        glyph[5:15, 3:9] = 255
        result = norm_glyph(glyph)
        assert result.max() == 255
        assert result.min() == 0

    def test_small_input(self):
        glyph = np.ones((3, 3), dtype=np.uint8) * 255
        result = norm_glyph(glyph)
        assert result.shape == (GLYPH_H, GLYPH_W)


class TestSegment:
    def test_empty(self):
        binary = np.zeros((30, 100), dtype=np.uint8)
        assert segment(binary) == []

    def test_single_glyph(self):
        binary = np.zeros((30, 100), dtype=np.uint8)
        binary[5:25, 10:25] = 255
        glyphs = segment(binary, min_h=5)
        assert len(glyphs) == 1
        assert glyphs[0][0] == 10  # x position

    def test_multiple_glyphs(self):
        binary = np.zeros((30, 100), dtype=np.uint8)
        binary[5:25, 10:20] = 255  # glyph 1
        binary[5:25, 30:40] = 255  # glyph 2
        binary[5:25, 50:60] = 255  # glyph 3
        glyphs = segment(binary, min_h=5)
        assert len(glyphs) == 3
        xs = [g[0] for g in glyphs]
        assert xs == sorted(xs)  # sorted by x

    def test_filters_small_height(self):
        binary = np.zeros((30, 100), dtype=np.uint8)
        binary[5:25, 10:25] = 255   # big glyph
        binary[28:29, 30:35] = 255  # tiny noise (h=1)
        glyphs = segment(binary, min_h=5)
        assert len(glyphs) == 1

    def test_filters_narrow(self):
        binary = np.zeros((30, 100), dtype=np.uint8)
        binary[5:25, 10:25] = 255  # big glyph
        binary[5:25, 30:31] = 255  # 1px wide — filtered by min_w=2
        glyphs = segment(binary, min_h=5, min_w=2)
        assert len(glyphs) == 1

    def test_filters_sparse(self):
        binary = np.zeros((30, 100), dtype=np.uint8)
        binary[5:25, 10:25] = 255  # solid glyph (area = 20*15 = 300, box = 20*15)
        # sparse glyph: only 2 pixels in 10x10 box (area=2 < 0.10*100=10)
        binary[5:6, 50:51] = 255
        binary[14:15, 59:60] = 255
        glyphs = segment(binary, min_h=5)
        assert len(glyphs) == 1


class TestHPNumReaderParse:
    """Тестируем _parse без реальных шаблонов — только парсинг строки."""

    def _make_reader(self):
        from hp_ocr import HPNumReader
        cfg = {"roi": {"x1": 0.0, "y1": 0.0, "x2": 0.1, "y2": 0.1},
               "bin_thr": 130, "match_min": 0.55, "min_glyph_h": 10, "max_hp": 0}
        return HPNumReader(cfg)

    def test_valid_ratio(self):
        r = self._make_reader()
        result = r._parse("1640/1640")
        assert result == (1640, 1640, 1.0)

    def test_partial_hp(self):
        r = self._make_reader()
        result = r._parse("820/1640")
        assert result == (820, 1640, 0.5)

    def test_no_slash(self):
        r = self._make_reader()
        assert r._parse("1640") is None

    def test_question_mark_split_glyph(self):
        """'?' от разрыва '4' (палочка+диагональ, светлый фон) — лишний символ:
        убираем и парсим. '508/14?66' → '508/1466'."""
        r = self._make_reader()
        assert r._parse("508/14?66") == (508, 1466, 508 / 1466)

    def test_zero_max(self):
        r = self._make_reader()
        assert r._parse("0/0") is None

    def test_current_exceeds_max(self):
        r = self._make_reader()
        assert r._parse("2000/1640") is None

    def test_non_numeric(self):
        r = self._make_reader()
        assert r._parse("abc/def") is None

    def test_empty(self):
        r = self._make_reader()
        assert r._parse("") is None

    def test_learned_max_filters_garbage(self):
        r = self._make_reader()
        r.learned_max = 1640
        # 100000/1640 — max相同但 current 太大? 不，current>max → None
        assert r._parse("100000/1640") is None

    def test_learned_max_adapts(self):
        r = self._make_reader()
        r.learned_max = 1640
        # new max within 0.2x-4x range
        result = r._parse("2000/2000")
        assert result == (2000, 2000, 1.0)
        assert r.learned_max == 2000  # learned_max updated

    def test_learned_max_rejects_outlier_partial(self):
        r = self._make_reader()
        r.learned_max = 1640
        # partial read: 8200/16400 — max is 10x learned, but current != max → filtered
        assert r._parse("8200/16400") is None

    def test_learned_max_accepts_new_full(self):
        r = self._make_reader()
        r.learned_max = 1640
        # full HP at new max: 16400/16400 — current==max → learns new max
        result = r._parse("16400/16400")
        assert result == (16400, 16400, 1.0)
        assert r.learned_max == 16400

    def test_slash_in_string(self):
        r = self._make_reader()
        result = r._parse("500/1000")
        assert result is not None
        assert result[2] == pytest.approx(0.5)


class TestEmptyTemplates:
    """HPNumReader без файлов шаблонов (hp_digits/ пуст): конструктор не падает,
    ready=False, _best деградирует на (None, -1) — читатель не используется."""

    def _make_reader(self, monkeypatch):
        from hp_ocr import HPNumReader
        monkeypatch.setattr("hp_ocr.load_templates", lambda: {})
        cfg = {"roi": {"x1": 0.0, "y1": 0.0, "x2": 0.1, "y2": 0.1},
               "bin_thr": 130, "match_min": 0.55, "min_glyph_h": 10, "max_hp": 0}
        return HPNumReader(cfg)

    def test_constructs_without_crash(self, monkeypatch):
        r = self._make_reader(monkeypatch)
        assert not r.ready

    def test_best_returns_none(self, monkeypatch):
        r = self._make_reader(monkeypatch)
        import numpy as np
        glyph = np.zeros((28, 18), np.uint8)
        ch, sc = r._best(glyph)
        assert ch is None and sc == -1.0

    def test_read_string_no_crash(self, monkeypatch):
        r = self._make_reader(monkeypatch)
        import cv2, numpy as np
        frame = np.zeros((60, 300, 3), np.uint8)
        assert r.read_string_crop(frame) == ""
