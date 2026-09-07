"""Tests for stash_flow.py — filter_maps, summarize_items, pick_map_for_device."""
import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass

# Mock heavy dependencies before importing
import sys
sys.modules['game_memory'] = MagicMock()
sys.modules['stash_reader'] = MagicMock()
sys.modules['stash_tab_browser'] = MagicMock()

from stash_flow import filter_maps, summarize_items, pick_map_for_device


@dataclass
class FakeStashItem:
    """Lightweight StashItem stand-in for testing (avoids mocking issues)."""
    path: str
    grid_x: int = 0
    grid_y: int = 0
    width: int = 1
    height: int = 1
    id: int = 0


def _make_item(path, grid_x=0, grid_y=0, width=1, height=1, item_id=0):
    """Create a StashItem-like object for testing."""
    return FakeStashItem(
        path=path, grid_x=grid_x, grid_y=grid_y,
        width=width, height=height, id=item_id,
    )


class TestFilterMaps:
    def test_empty_list(self):
        assert filter_maps([]) == []

    def test_only_maps(self):
        items = [
            _make_item("Metadata/Items/Maps/MapTier1"),
            _make_item("Metadata/Items/Maps/MapTier5"),
        ]
        result = filter_maps(items)
        assert len(result) == 2

    def test_no_maps(self):
        items = [
            _make_item("Metadata/Items/Currency/Chaos"),
            _make_item("Metadata/Items/Gems/SkillGem"),
        ]
        result = filter_maps(items)
        assert len(result) == 0

    def test_mixed_items(self):
        items = [
            _make_item("Metadata/Items/Maps/MapTier3"),
            _make_item("Metadata/Items/Currency/Exalted"),
            _make_item("Metadata/Items/Maps/MapTier10"),
            _make_item("Metadata/Items/Weapons/Sword"),
        ]
        result = filter_maps(items)
        assert len(result) == 2
        assert all(it.path.startswith("Metadata/Items/Maps") for it in result)


class TestSummarizeItems:
    def test_empty(self):
        assert summarize_items([]) == "пусто"

    def test_single_category(self):
        items = [
            _make_item("Metadata/Items/Maps/MapTier1"),
            _make_item("Metadata/Items/Maps/MapTier2"),
            _make_item("Metadata/Items/Maps/MapTier1"),  # duplicate path
        ]
        result = summarize_items(items)
        assert "items=3" in result
        assert "Maps=3" in result

    def test_multiple_categories(self):
        items = [
            _make_item("Metadata/Items/Maps/MapTier1"),
            _make_item("Metadata/Items/Currency/Chaos"),
            _make_item("Metadata/Items/Currency/Exalted"),
        ]
        result = summarize_items(items)
        assert "items=3" in result
        assert "Currency=2" in result
        assert "Maps=1" in result

    def test_non_metadata_items(self):
        items = [_make_item("SomeOther/Path")]
        result = summarize_items(items)
        assert "(other)=1" in result

    def test_path_counts(self):
        items = [
            _make_item("Metadata/Items/Maps/MapTier1"),
            _make_item("Metadata/Items/Maps/MapTier1"),
            _make_item("Metadata/Items/Maps/MapTier1"),
        ]
        result = summarize_items(items)
        assert "MapTier1×3" in result


class TestPickMapForDevice:
    def test_returns_todo(self):
        """Current implementation is a stub returning (False, 'TODO:...')."""
        item = _make_item(
            "Metadata/Items/Maps/MapTier5",
            grid_x=3, grid_y=2,
            width=1, height=1,
            item_id=42,
        )
        ok, msg = pick_map_for_device(MagicMock(), MagicMock(), item)
        assert ok is False
        assert "TODO" in msg
        assert "42" in msg  # item id in message
