"""Integration tests for HPEngine — decision logic scenarios.

Covers critical path: HP data → decide() → LOW/HIGH/CLIP/UNKNOWN.
Prevents regression where HP flask never triggers or triggers at wrong thresholds.
"""
import pytest
from hp_engine import HPEngine, HPDecision


class TestHPEngineDecide:
    """HPEngine.decide() with various input combinations."""

    def _make_engine(self, **kwargs) -> HPEngine:
        defaults = dict(
            threshold=0.88, confirm_frames=1,
            num_low_grace_s=0.35, fallback_red_frac_min=0.04,
            valid_val=45, menu_grace_s=0.5,
        )
        defaults.update(kwargs)
        return HPEngine(**defaults)

    # ── HIGH (HP above threshold) ──

    def test_high_num_above_threshold(self):
        """HP 90% → HIGH (above 88% threshold)."""
        e = self._make_engine()
        hm = {"ratio": 0.90, "fill_v": 60, "trip_below": False, "red_frac": 0.5}
        result = e.decide(num=(4500, 5000, 0.90), hm=hm, low=False, foreground=True)
        assert result == HPDecision.HIGH
        assert e.hp_pct == 0.90
        assert e.hp_src == "num"

    def test_high_num_exactly_threshold(self):
        """HP 88% (exactly threshold) → HIGH."""
        e = self._make_engine()
        hm = {"ratio": 0.88, "fill_v": 60, "trip_below": False, "red_frac": 0.5}
        result = e.decide(num=(4400, 5000, 0.88), hm=hm, low=False, foreground=True)
        assert result == HPDecision.HIGH

    # ── LOW (HP below threshold, confirmed) ──

    def test_low_num_below_threshold(self):
        """HP 50% → LOW (below 88% threshold, confirm_frames=1)."""
        e = self._make_engine(confirm_frames=1)
        hm = {"ratio": 0.50, "fill_v": 60, "trip_below": False, "red_frac": 0.5}
        # First call → streak=1 → LOW
        result = e.decide(num=(2500, 5000, 0.50), hm=hm, low=True, foreground=True)
        assert result == HPDecision.LOW
        assert e.hp_pct == 0.50
        assert e.low_streak == 1

    def test_low_requires_confirm_frames(self):
        """HP 50% with confirm_frames=2 → first call not LOW yet (streak<confirm), second LOW."""
        e = self._make_engine(confirm_frames=2)
        hm = {"ratio": 0.50, "fill_v": 60, "trip_below": False, "red_frac": 0.5}
        # First call → streak=1 < confirm_frames=2 → HIGH (not confirmed yet)
        result1 = e.decide(num=(2500, 5000, 0.50), hm=hm, low=True, foreground=True,
                           now=1000.0)
        assert result1 != HPDecision.LOW  # not confirmed yet
        assert e.low_streak == 1

        # Second call → streak=2 >= confirm_frames → LOW
        result2 = e.decide(num=(2500, 5000, 0.50), hm=hm, low=True, foreground=True,
                           now=1001.0)
        assert result2 == HPDecision.LOW
        assert e.low_streak == 2

    def test_low_resets_on_recovery(self):
        """HP drops then recovers → LOW then HIGH, streak resets."""
        e = self._make_engine(confirm_frames=1)
        hm_low = {"ratio": 0.50, "fill_v": 60, "trip_below": False, "red_frac": 0.5}
        hm_high = {"ratio": 0.90, "fill_v": 60, "trip_below": False, "red_frac": 0.5}

        e.decide(num=(2500, 5000, 0.50), hm=hm_low, low=True, foreground=True)
        assert e.decision == HPDecision.LOW
        assert e.low_streak == 1

        # Recovery
        e.decide(num=(4500, 5000, 0.90), hm=hm_high, low=False, foreground=True)
        assert e.decision == HPDecision.HIGH
        assert e.low_streak == 0

    # ── CLIP (ROI clipped) ──

    def test_clip_extreme_low_num_with_full_orb(self):
        """HP 5% with orb at 90% → CLIP (ROI misaligned)."""
        e = self._make_engine()
        hm = {"ratio": 0.90, "fill_v": 60, "trip_below": False, "red_frac": 0.5}
        result = e.decide(num=(250, 5000, 0.05), hm=hm, low=True, foreground=True,
                          now=2000.0)
        assert result == HPDecision.CLIP
        assert e.need_relocate is True

    # ── UNKNOWN (no data) ──

    def test_unknown_no_data(self):
        """No num, no orb → UNKNOWN."""
        e = self._make_engine()
        result = e.decide(num=None, hm={}, low=None, foreground=True)
        assert result == HPDecision.UNKNOWN
        assert e.hp_pct is None

    # ── Foreground handling ──

    def test_background_resets(self):
        """Background → HIGH, state reset."""
        e = self._make_engine()
        hm = {"ratio": 0.50, "fill_v": 60, "trip_below": False, "red_frac": 0.5}
        e.decide(num=(2500, 5000, 0.50), hm=hm, low=True, foreground=True)
        assert e.decision == HPDecision.LOW

        # Background → reset
        result = e.decide(num=(2500, 5000, 0.50), hm=hm, low=True, foreground=False)
        assert result == HPDecision.HIGH
        assert e.low_streak == 0
        assert e.hp_pct is None

    # ── Orb-only fallback ──

    def test_orb_only_low(self):
        """No num, orb ratio below threshold → LOW after grace."""
        e = self._make_engine(menu_grace_s=0.0)  # no grace for test
        hm = {"ratio": 0.50, "fill_v": 60, "trip_below": True, "red_frac": 0.5}
        result = e.decide(num=None, hm=hm, low=True, foreground=True, now=100.0)
        assert result == HPDecision.LOW

    def test_orb_only_blind_fallback(self):
        """No num, no orb ratio, but tripwire + red + low history → LOW."""
        e = self._make_engine(menu_grace_s=0.0)
        # First: establish _last_good_ratio below threshold
        e._last_good_ratio = 0.50
        hm = {"ratio": None, "fill_v": 60, "trip_below": True, "red_frac": 0.05}
        result = e.decide(num=None, hm=hm, low=True, foreground=True, now=100.0)
        assert result == HPDecision.LOW

    # ── Reset ──

    def test_reset_clears_state(self):
        """reset() returns engine to initial state."""
        e = self._make_engine()
        hm = {"ratio": 0.50, "fill_v": 60, "trip_below": False, "red_frac": 0.5}
        e.decide(num=(2500, 5000, 0.50), hm=hm, low=True, foreground=True)
        assert e.decision == HPDecision.LOW

        e.reset()
        assert e.decision == HPDecision.HIGH
        assert e.hp_pct is None
        assert e.low_streak == 0
        assert e.last_good_ratio is None

    # ── Grace period anti-glitch ──

    def test_num_low_grace_with_full_orb(self):
        """Num below threshold but orb full → grace period → not LOW."""
        e = self._make_engine(num_low_grace_s=0.5)
        hm = {"ratio": 0.90, "fill_v": 60, "trip_below": False, "red_frac": 0.5}

        # First call at t=100 — starts grace
        result = e.decide(num=(2500, 5000, 0.50), hm=hm, low=True, foreground=True, now=100.0)
        # low_now = not (orb_full and now - _num_low_since < grace) → False when orb full + within grace
        assert result != HPDecision.LOW

        # After grace (t=101 > 100+0.35) — now LOW
        result2 = e.decide(num=(2500, 5000, 0.50), hm=hm, low=True, foreground=True, now=101.0)
        assert result2 == HPDecision.LOW
