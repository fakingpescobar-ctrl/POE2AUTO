"""Unit-тесты для loot_engine.py."""
import time
from unittest.mock import patch

import numpy as np

from loot_engine import LootEngine


class TestLootEngineRadius:
    def test_fixed_radius(self):
        e = LootEngine(250, [0, 0], 100)
        assert e._get_radius((1080, 1920)) == 250

    def test_frac_radius(self):
        e = LootEngine(250, [0, 0], 100, radius_frac=0.02)
        assert e._get_radius((1080, 1920)) == int(1080 * 0.02)

    def test_frac_uses_min(self):
        e = LootEngine(250, [0, 0], 100, radius_frac=0.1)
        assert e._get_radius((600, 800)) == 60  # min(600,800)*0.1


class TestLootEngineCenter:
    def test_default_center(self):
        e = LootEngine(250, [0, 0], 100)
        assert e.center((1080, 1920)) == (960, 540)

    def test_offset_center(self):
        e = LootEngine(250, [100, -50], 100)
        assert e.center((1080, 1920)) == (1060, 490)


class TestTargetsInRadius:
    def test_empty_points(self):
        e = LootEngine(250, [0, 0], 100)
        assert e.targets_in_radius([], (1080, 1920)) == []

    def test_point_in_radius(self):
        e = LootEngine(250, [0, 0], 100)
        points = [(960, 540, 100)]  # exactly at center
        result = e.targets_in_radius(points, (1080, 1920))
        assert len(result) == 1

    def test_point_outside_radius(self):
        e = LootEngine(10, [0, 0], 100)  # tiny radius
        points = [(960, 600, 100)]  # 60px away
        result = e.targets_in_radius(points, (1080, 1920))
        assert len(result) == 0

    def test_mixed_points(self):
        e = LootEngine(100, [0, 0], 100)
        points = [
            (960, 540, 100),   # center — in
            (960, 700, 100),   # 160px away — out
            (960, 600, 100),   # 60px away — in
        ]
        result = e.targets_in_radius(points, (1080, 1920))
        assert len(result) == 2

    def test_custom_center(self):
        e = LootEngine(50, [0, 0], 100)
        points = [(100, 100, 100)]
        result = e.targets_in_radius(points, (1080, 1920), center=(100, 100))
        assert len(result) == 1

    def test_boundary_exact(self):
        e = LootEngine(100, [0, 0], 100)
        # point exactly at radius distance
        points = [(1060, 540, 100)]  # 100px from center (960,540)
        result = e.targets_in_radius(points, (1080, 1920))
        assert len(result) == 1  # <= r2, so included

    def test_boundary_just_out(self):
        e = LootEngine(100, [0, 0], 100)
        points = [(1061, 540, 100)]  # 101px from center
        result = e.targets_in_radius(points, (1080, 1920))
        assert len(result) == 0


class TestLootEngineCooldown:
    def test_ready_initially(self):
        e = LootEngine(250, [0, 0], 100)
        assert e.ready() is True

    def test_not_ready_after_press(self):
        e = LootEngine(250, [0, 0], 500)  # 500ms cooldown
        e.mark_pressed()
        assert e.ready() is False

    def test_ready_after_cooldown(self):
        e = LootEngine(250, [0, 0], 1)  # 1ms cooldown
        e.mark_pressed()
        time.sleep(0.005)
        assert e.ready() is True


class TestPriorityMode:
    def test_size_priority(self):
        e = LootEngine(500, [0, 0], 100, priority_mode="size")
        points = [
            (960, 540, 50),   # центр, маленький
            (970, 550, 200),  # рядом, большой
            (950, 530, 100),  # рядом, средний
        ]
        result = e.targets_in_radius(points, (1080, 1920))
        areas = [p[2] for p in result]
        assert areas == sorted(areas, reverse=True)  # по убыванию площади

    def test_distance_priority(self):
        e = LootEngine(500, [0, 0], 100, priority_mode="distance")
        points = [
            (1060, 540, 200),  # далеко, большой
            (960, 540, 50),    # центр, маленький
            (970, 540, 100),   # рядом, средний
        ]
        result = e.targets_in_radius(points, (1080, 1920))
        # ближайший первый
        assert result[0] == (960, 540, 50)

    def test_distance_calculation(self):
        e = LootEngine(500, [0, 0], 100, priority_mode="distance")
        points = [
            (960, 640, 100),   # 100px от центра
            (960, 540, 50),    # 0px от центра
            (960, 590, 80),    # 50px от центра
        ]
        result = e.targets_in_radius(points, (1080, 1920))
        distances = [((p[0]-960)**2 + (p[1]-540)**2) for p in result]
        assert distances == sorted(distances)
