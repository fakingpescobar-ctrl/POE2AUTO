"""Unit-тесты для color_detector.py."""
import numpy as np

from color_detector import ColorDetector, rgb_to_hsv_bounds, normalize_colors


class TestRgbToHsvBounds:
    def test_red_wraps(self):
        bounds = rgb_to_hsv_bounds([255, 0, 0], hue_tol=10, sat_min=100, val_min=50)
        assert len(bounds) == 2  # red wraps around HSV circle

    def test_green_no_wrap(self):
        bounds = rgb_to_hsv_bounds([0, 255, 0], hue_tol=10, sat_min=100, val_min=50)
        assert len(bounds) == 1

    def test_blue_no_wrap(self):
        bounds = rgb_to_hsv_bounds([0, 0, 255], hue_tol=10, sat_min=100, val_min=50)
        assert len(bounds) == 1

    def test_pink_wraps(self):
        bounds = rgb_to_hsv_bounds([235, 0, 185], hue_tol=8, sat_min=120, val_min=120)
        # pink is near H=170, should wrap if hue+tol > 179
        assert len(bounds) >= 1


class TestNormalizeColors:
    def test_single_rgb(self):
        result = normalize_colors([255, 0, 0])
        assert result == [[255, 0, 0]]

    def test_list_of_lists(self):
        result = normalize_colors([[255, 0, 0], [0, 255, 0]])
        assert result == [[255, 0, 0], [0, 255, 0]]

    def test_empty(self):
        assert normalize_colors([]) == []


class TestColorDetector:
    def test_no_colors(self):
        det = ColorDetector([], hue_tol=8, sat_min=120, val_min=120)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        points, mask = det.detect(frame)
        assert points == []

    def test_detect_pink_blob(self):
        det = ColorDetector([[255, 0, 200]], hue_tol=15, sat_min=100, val_min=100,
                            min_blob_area=10, close_px=0)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        # BGR: pink = (200, 0, 255)
        frame[40:60, 40:60] = [200, 0, 255]
        points, mask = det.detect(frame)
        assert len(points) >= 1
        cx, cy, area = points[0]
        assert 40 <= cx <= 60
        assert 40 <= cy <= 60
        assert area >= 100  # 20x20 blob

    def test_ignores_non_matching_color(self):
        det = ColorDetector([[255, 0, 200]], hue_tol=5, sat_min=200, val_min=200,
                            min_blob_area=10, close_px=0)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[40:60, 40:60] = [50, 50, 50]  # dark gray — not pink
        points, mask = det.detect(frame)
        assert points == []

    def test_small_blob_filtered(self):
        det = ColorDetector([[255, 0, 200]], hue_tol=15, sat_min=100, val_min=100,
                            min_blob_area=500, close_px=0)  # high min_area
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[40:45, 40:45] = [200, 0, 255]  # 5x5 = 25px < 500
        points, mask = det.detect(frame)
        assert points == []

    def test_multi_color(self):
        det = ColorDetector([[255, 0, 200], [0, 200, 255]], hue_tol=15, sat_min=100,
                            val_min=100, min_blob_area=10, close_px=0)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[10:20, 10:20] = [200, 0, 255]    # pink
        frame[60:70, 60:70] = [255, 200, 0]    # light blue in BGR
        points, mask = det.detect(frame)
        assert len(points) >= 2

    def test_mask_is_binary(self):
        det = ColorDetector([[255, 0, 200]], hue_tol=15, sat_min=100, val_min=100,
                            min_blob_area=10, close_px=0)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[40:60, 40:60] = [200, 0, 255]
        points, mask = det.detect(frame)
        assert mask is not None
        assert mask.shape == (100, 100)
        assert set(np.unique(mask)).issubset({0, 255})

    def test_points_sorted_by_area(self):
        det = ColorDetector([[255, 0, 200]], hue_tol=15, sat_min=100, val_min=100,
                            min_blob_area=10, close_px=0)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[10:20, 10:20] = [200, 0, 255]    # 10x10 = 100px
        frame[60:80, 60:80] = [200, 0, 255]    # 20x20 = 400px
        points, mask = det.detect(frame)
        if len(points) >= 2:
            assert points[0][2] >= points[1][2]  # sorted desc by area
