"""Tests for map_run_flow.py — _tier_from_path and _select_waystone."""
import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass

# Mock heavy dependencies before importing
import sys
sys.modules['game_memory'] = MagicMock()
sys.modules['stash_reader'] = MagicMock()
sys.modules['stash_tab_browser'] = MagicMock()
sys.modules['virtual_xbox'] = MagicMock()
sys.modules['screen_capture'] = MagicMock()
sys.modules['probe_cast'] = MagicMock()
sys.modules['move_to_portal'] = MagicMock()
sys.modules['core_map'] = MagicMock()

from map_run_flow import _tier_from_path, _select_waystone


@dataclass
class FakeMapItem:
    """Fake StashItem for testing _select_waystone."""
    path: str
    id: int = 0


class TestTierFromPath:
    def test_tier_1(self):
        assert _tier_from_path("Metadata/Items/Maps/MapKeyTier1") == 1

    def test_tier_16(self):
        assert _tier_from_path("Metadata/Items/Maps/MapKeyTier16") == 16

    def test_tier_10(self):
        assert _tier_from_path("Metadata/Items/Maps/MapKeyTier10") == 10

    def test_tier_with_suffix(self):
        """Tier number before / or _ should be extracted."""
        assert _tier_from_path("Metadata/Items/Maps/MapKeyTier5_SomeMap") == 5

    def test_no_tier(self):
        assert _tier_from_path("Metadata/Items/Currency/Chaos") == 0

    def test_empty_string(self):
        assert _tier_from_path("") == 0

    def test_map_key_tier_0(self):
        """Tier 0 should parse correctly (invalid but parseable)."""
        assert _tier_from_path("Metadata/Items/Maps/MapKeyTier0") == 0

    def test_tier_with_extra_path(self):
        assert _tier_from_path("Metadata/Items/Maps/MapKeyTier7/SubPath") == 7


class TestSelectWaystone:
    def test_exact_match(self):
        maps = [
            FakeMapItem("Metadata/Items/Maps/MapKeyTier5"),
            FakeMapItem("Metadata/Items/Maps/MapKeyTier10"),
            FakeMapItem("Metadata/Items/Maps/MapKeyTier3"),
        ]
        result = _select_waystone(maps, target_tier=10)
        assert result is not None
        assert result.path == "Metadata/Items/Maps/MapKeyTier10"

    def test_fallback_lower_tier(self):
        """No exact match → pick closest lower."""
        maps = [
            FakeMapItem("Metadata/Items/Maps/MapKeyTier5"),
            FakeMapItem("Metadata/Items/Maps/MapKeyTier3"),
            FakeMapItem("Metadata/Items/Maps/MapKeyTier1"),
        ]
        result = _select_waystone(maps, target_tier=8, fallback=True)
        assert result is not None
        assert "Tier5" in result.path

    def test_no_fallback(self):
        """No exact match and fallback=False → None."""
        maps = [
            FakeMapItem("Metadata/Items/Maps/MapKeyTier5"),
            FakeMapItem("Metadata/Items/Maps/MapKeyTier3"),
        ]
        result = _select_waystone(maps, target_tier=8, fallback=False)
        assert result is None

    def test_empty_list(self):
        result = _select_waystone([], target_tier=5)
        assert result is None

    def test_no_valid_tiers(self):
        """Items without MapKeyTier → treated as invalid."""
        maps = [
            FakeMapItem("Metadata/Items/Currency/Chaos"),
            FakeMapItem("SomeRandomPath"),
        ]
        result = _select_waystone(maps, target_tier=5)
        assert result is None

    def test_higher_tier_not_selected(self):
        """Should NOT select a tier higher than target."""
        maps = [
            FakeMapItem("Metadata/Items/Maps/MapKeyTier15"),
            FakeMapItem("Metadata/Items/Maps/MapKeyTier10"),
            FakeMapItem("Metadata/Items/Maps/MapKeyTier3"),
        ]
        result = _select_waystone(maps, target_tier=10, fallback=True)
        assert result is not None
        assert "Tier10" in result.path  # Exact match, not Tier15

    def test_only_higher_tiers_no_fallback(self):
        """All tiers higher than target, no fallback → None."""
        maps = [
            FakeMapItem("Metadata/Items/Maps/MapKeyTier15"),
            FakeMapItem("Metadata/Items/Maps/MapKeyTier12"),
        ]
        result = _select_waystone(maps, target_tier=5, fallback=True)
        # fallback looks for t < 5, but all are > 5
        assert result is None

    def test_multiple_exact_matches(self):
        """Multiple maps with same tier → returns first."""
        maps = [
            FakeMapItem("Metadata/Items/Maps/MapKeyTier10", id=1),
            FakeMapItem("Metadata/Items/Maps/MapKeyTier10", id=2),
        ]
        result = _select_waystone(maps, target_tier=10)
        assert result is not None
        assert result.id == 1
