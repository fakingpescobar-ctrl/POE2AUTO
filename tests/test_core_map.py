"""Tests for core_map.py — CoreMap A* navigation and coordinate conversion."""
import pytest
from unittest.mock import MagicMock, patch

# Mock heavy C-extension/FFI dependencies before importing CoreMap.
# We must NOT replace game_offsets — it's a pure-Python module that other tests
# rely on. Instead, we only mock the expensive ones (game_memory, probe_cast).
import sys

_orig_gm = sys.modules.pop('game_memory', None)
_orig_probe = sys.modules.pop('probe_cast', None)
sys.modules['game_memory'] = MagicMock()
sys.modules['probe_cast'] = MagicMock()

# Set the RATIO before importing CoreMap
import game_offsets as off
off.WORLD_TO_GRID_RATIO = 250.0 / 23.0  # ≈10.8696

from core_map import CoreMap


class TestCoreMapCoordinates:
    """Test coordinate conversion (world <-> grid)."""

    def setup_method(self):
        self.mock_mem = MagicMock()
        self.cm = CoreMap(self.mock_mem)
        # Set loaded state for coordinate tests
        self.cm.loaded = True
        self.cm.width = 100
        self.cm.height = 100
        self.cm.walk = bytearray([1] * 10000)  # All walkable

    def test_world_to_grid(self):
        """world (X, Y) -> grid (gx, gy)."""
        # RATIO = 250/23 ≈ 10.8696
        ratio = self.cm.RATIO
        gx, gy = self.cm.world_to_grid(100.0, 200.0)
        assert abs(gx - 100.0 / ratio) < 0.001
        assert abs(gy - 200.0 / ratio) < 0.001

    def test_grid_to_world(self):
        """grid -> world (X, Y)."""
        ratio = self.cm.RATIO
        wx, wy = self.cm.grid_to_world(10, 20)
        assert abs(wx - 10 * ratio) < 0.001
        assert abs(wy - 20 * ratio) < 0.001

    def test_roundtrip(self):
        """world -> grid -> world should be approximately equal."""
        wx, wy = 500.0, 600.0
        gx, gy = self.cm.world_to_grid(wx, wy)
        wx2, wy2 = self.cm.grid_to_world(gx, gy)
        assert abs(wx - wx2) < 0.01
        assert abs(wy - wy2) < 0.01

    def test_grid_world_step(self):
        """grid_world_step should return RATIO."""
        assert self.cm.grid_world_step() == self.cm.RATIO


class TestCoreMapWalkability:
    """Test walkability checks."""

    def setup_method(self):
        self.mock_mem = MagicMock()
        self.cm = CoreMap(self.mock_mem)
        self.cm.loaded = True
        self.cm.width = 10
        self.cm.height = 10
        # Create a 10x10 grid with some walls
        # Row 0: all walkable
        # Row 5: walls at columns 3-6
        walk = bytearray([1] * 100)
        for x in range(3, 7):
            walk[5 * 10 + x] = 0  # Wall
        self.cm.walk = walk

    def test_in_bounds(self):
        assert self.cm.in_bounds(0, 0)
        assert self.cm.in_bounds(9, 9)
        assert not self.cm.in_bounds(-1, 0)
        assert not self.cm.in_bounds(10, 0)
        assert not self.cm.in_bounds(0, 10)

    def test_is_walkable(self):
        assert self.cm.is_walkable(0, 0)  # Walkable
        assert not self.cm.is_walkable(5, 5)  # Wall
        assert not self.cm.is_walkable(3, 5)  # Wall
        assert not self.cm.is_walkable(6, 5)  # Wall

    def test_is_walkable_out_of_bounds(self):
        assert not self.cm.is_walkable(-1, 0)
        assert not self.cm.is_walkable(10, 0)

    def test_is_walkable_not_loaded(self):
        self.cm.loaded = False
        assert not self.cm.is_walkable(0, 0)


class TestCoreMapAStar:
    """Test A* pathfinding."""

    def setup_method(self):
        self.mock_mem = MagicMock()
        self.cm = CoreMap(self.mock_mem)
        self.cm.loaded = True
        self.cm.width = 20
        self.cm.height = 20
        # All walkable grid
        self.cm.walk = bytearray([1] * 400)

    def test_find_path_same_point(self):
        """Path from point to itself."""
        ratio = self.cm.RATIO
        start = (5 * ratio, 5 * ratio)
        goal = (5 * ratio, 5 * ratio)
        path = self.cm.find_path(start, goal)
        assert path is not None
        assert len(path) == 1

    def test_find_path_straight_line(self):
        """Path in straight line (no obstacles)."""
        ratio = self.cm.RATIO
        start = (2 * ratio, 2 * ratio)
        goal = (5 * ratio, 2 * ratio)
        path = self.cm.find_path(start, goal)
        assert path is not None
        assert len(path) > 0
        # Last point should be near goal grid coords
        last_gx = path[-1][2]
        assert last_gx == 5

    def test_find_path_around_wall(self):
        """Path around a wall."""
        # Add a wall at row 10, columns 5-14
        for x in range(5, 15):
            self.cm.walk[10 * 20 + x] = 0  # Wall

        ratio = self.cm.RATIO
        start = (2 * ratio, 10 * ratio)
        goal = (17 * ratio, 10 * ratio)
        path = self.cm.find_path(start, goal)
        assert path is not None
        # Path should go around the wall (more than just start+end)
        assert len(path) > 2

    def test_find_path_no_path(self):
        """No path if goal is on a non-walkable island."""
        # Make a 5x5 island of walls centered at (10,10)
        for x in range(8, 13):
            for y in range(8, 13):
                self.cm.walk[y * 20 + x] = 0  # Wall

        ratio = self.cm.RATIO
        # Goal is inside the wall island — _nearest_walkable won't find
        # anything within default radius=40 if the whole island is solid
        start = (2 * ratio, 2 * ratio)
        goal = (10 * ratio, 10 * ratio)
        path = self.cm.find_path(start, goal)
        # _nearest_walkable should find a walkable cell near the island edge
        # so this may still find a path. Use a fully enclosed scenario instead:
        # Make the goal cell itself non-walkable and no walkable neighbor
        for x in range(9, 12):
            for y in range(9, 12):
                self.cm.walk[y * 20 + x] = 0
        # Now (10,10) has no walkable neighbors within radius=2
        path2 = self.cm.find_path(start, (10 * ratio, 10 * ratio))
        # _nearest_walkable with radius=40 will find edge cells, so it can
        # still find a path. The real no-path is when start==goal on wall.
        # Instead test: start on walkable, goal unreachable due to complete ring
        # Create ring walls that cut the grid in half
        for y in range(0, 20):
            self.cm.walk[y * 20 + 10] = 0  # Full vertical wall
        # Start at (5,10), goal at (15,10) — separated by wall
        path3 = self.cm.find_path((5 * ratio, 10 * ratio), (15 * ratio, 10 * ratio))
        assert path3 is None  # No path possible

    def test_find_path_max_cells(self):
        """Path finding respects max_cells limit."""
        ratio = self.cm.RATIO
        start = (0, 0)
        goal = (19 * ratio, 19 * ratio)
        # Very small max_cells should fail to find path across large grid
        path = self.cm.find_path(start, goal, max_cells=5)
        assert path is None

    def test_find_path_max_cells(self):
        """Path finding respects max_cells limit."""
        ratio = self.cm.RATIO
        start = (0, 0)
        goal = (19 * ratio, 19 * ratio)
        # Very small max_cells should fail
        path = self.cm.find_path(start, goal, max_cells=5)
        assert path is None


class TestCoreMapNearestWalkable:
    """Test _nearest_walkable helper."""

    def setup_method(self):
        self.mock_mem = MagicMock()
        self.cm = CoreMap(self.mock_mem)
        self.cm.loaded = True
        self.cm.width = 20
        self.cm.height = 20
        self.cm.walk = bytearray([1] * 400)

    def test_nearest_walkable_on_walkable(self):
        """If current cell is walkable, should return it."""
        result = self.cm._nearest_walkable(10, 10)
        assert result == (10, 10)

    def test_nearest_walkable_near_wall(self):
        """Should find nearest walkable cell."""
        # Make (10, 10) a wall
        self.cm.walk[10 * 20 + 10] = 0
        result = self.cm._nearest_walkable(10, 10)
        assert result is not None
        gx, gy = result
        assert self.cm.is_walkable(gx, gy)
        # Should be close
        assert abs(gx - 10) <= 1
        assert abs(gy - 10) <= 1

    def test_nearest_walkable_in_island(self):
        """Should return None if no walkable cell in radius."""
        # Create a 5x5 island of walls
        for x in range(8, 13):
            for y in range(8, 13):
                self.cm.walk[y * 20 + x] = 0
        result = self.cm._nearest_walkable(10, 10, radius=2)
        assert result is None


class TestCoreMapHeuristic:
    """Test A* heuristic function."""

    def test_heuristic_manhattan(self):
        """Manhattan distance heuristic."""
        a = (0, 0)
        b = (3, 4)
        h = CoreMap._h(a, b)
        assert h == 7  # |3-0| + |4-0|

    def test_heuristic_same_point(self):
        """Heuristic from point to itself is 0."""
        a = (5, 5)
        assert CoreMap._h(a, a) == 0


class TestCoreMapDirections:
    """Test direction generation."""

    def test_4_directions(self):
        """4-directional movement (no diagonals)."""
        dirs = CoreMap._dirs(diagonal=False)
        assert len(dirs) == 4
        # All costs should be 1.0
        for _, _, cost in dirs:
            assert cost == 1.0

    def test_8_directions(self):
        """8-directional movement (with diagonals)."""
        dirs = CoreMap._dirs(diagonal=True)
        assert len(dirs) == 8
        # Diagonal costs should be sqrt(2) ≈ 1.414
        diagonal_dirs = [d for d in dirs if d[2] > 1.1]
        assert len(diagonal_dirs) == 4
        for _, _, cost in diagonal_dirs:
            assert abs(cost - 1.414) < 0.01
