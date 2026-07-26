"""Unit-тесты для hp_engine.py — все ветки HP-решений."""
import pytest

from hp_engine import HPEngine, HPDecision


# ── helpers ──────────────────────────────────────────────────────────────

def _make_engine(**kw):
    defaults = dict(threshold=0.88, confirm_frames=1, num_low_grace_s=0.35,
                    fallback_red_frac_min=0.04, valid_val=45, menu_grace_s=0.5)
    defaults.update(kw)
    return HPEngine(**defaults)


def _num(cur, mx):
    """OCR result: (current, max, ratio)."""
    return (cur, mx, cur / mx if mx else 0)


def _orb(ratio=None, fill_v=100, trip_below=False, red_frac=0.1):
    return {"ratio": ratio, "fill_v": fill_v, "trip_below": trip_below,
            "red_frac": red_frac}


# ── HIGH: цифры выше порога ─────────────────────────────────────────────

class TestHigh:
    def test_num_above_threshold(self):
        e = _make_engine()
        r = e.decide(_num(1600, 1640), _orb(ratio=0.97), low=False, foreground=True, now=1.0)
        assert r == HPDecision.HIGH
        assert e.hp_pct == pytest.approx(1600 / 1640, abs=0.01)
        assert e.hp_src == "num"

    def test_orb_above_threshold_no_num(self):
        """Цифры включены но не читаются, орб полный → HIGH."""
        e = _make_engine()
        # num_reader exists but returns None → num_blind_since starts
        r = e.decide(None, _orb(ratio=0.95), low=False, foreground=True, now=1.0)
        # within menu_grace → orb fallback → HIGH
        assert r == HPDecision.HIGH


# ── LOW: цифры ниже порога, streak >= confirm ───────────────────────────

class TestLow:
    def test_num_below_threshold_single_confirm(self):
        """confirm_frames=1: первый же кадр ниже порога → LOW."""
        e = _make_engine(confirm_frames=1)
        r = e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=True, now=1.0)
        assert r == HPDecision.LOW
        assert e.low_streak == 1

    def test_num_below_threshold_needs_two_frames(self):
        """confirm_frames=2: первый кадр — streak=1, решение HIGH; второй — streak=2, LOW."""
        e = _make_engine(confirm_frames=2)
        r1 = e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=True, now=1.0)
        assert r1 == HPDecision.HIGH  # streak=1 < confirm=2
        assert e.low_streak == 1
        r2 = e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=True, now=1.1)
        assert r2 == HPDecision.LOW  # streak=2 >= confirm=2
        assert e.low_streak == 2

    def test_orb_fallback_low(self):
        """Цифры не читаются (включены), орб показывает low → LOW после grace."""
        e = _make_engine(confirm_frames=1, menu_grace_s=0.5)
        # кадр 1: цифры пропали, внутри grace → orb fallback
        r1 = e.decide(None, _orb(ratio=0.30), low=True, foreground=True, now=1.0)
        assert r1 == HPDecision.LOW  # orb says low, confirm=1


# ── CLIP: обрезка ROI ──────────────────────────────────────────────────

class TestClip:
    def test_extreme_low_num_with_full_orb(self):
        """Цифры <25% при полном орбе → CLIP (обрезка ROI)."""
        e = _make_engine()
        r = e.decide(_num(70, 1070), _orb(ratio=0.95), low=False, foreground=True, now=1.0)
        assert r == HPDecision.CLIP
        assert e.need_relocate is True

    def test_clip_only_logs_once(self):
        """CLIP логируется не чаще раза в 3с."""
        e = _make_engine()
        e.decide(_num(70, 1070), _orb(ratio=0.95), low=False, foreground=True, now=1.0)
        assert e.need_relocate is True
        # второй кадр сразу — need_relocate = False (throttle)
        e.decide(_num(70, 1070), _orb(ratio=0.95), low=False, foreground=True, now=1.5)
        assert e.need_relocate is False


# ── UNKNOWN: цифры пропали надолго ──────────────────────────────────────

class TestUnknown:
    def test_num_unavailable_long(self):
        """Цифры включены, не читаются > menu_grace_s → UNKNOWN."""
        e = _make_engine(menu_grace_s=0.5)
        # кадр 1: внутри grace → orb fallback
        e.decide(None, _orb(ratio=0.95), low=False, foreground=True, now=1.0)
        # кадр 2: за гранью grace → UNKNOWN
        r = e.decide(None, _orb(ratio=0.95), low=False, foreground=True, now=2.0)
        assert r == HPDecision.UNKNOWN

    def test_both_sources_none(self):
        """Ни цифры, ни орб не дают данных → UNKNOWN."""
        e = _make_engine()
        r = e.decide(None, None, None, foreground=True, now=1.0)
        assert r == HPDecision.UNKNOWN


# ── Debounce: streak logic ──────────────────────────────────────────────

class TestDebounce:
    def test_streak_resets_on_high(self):
        """HIGH кадр сбрасывает streak."""
        e = _make_engine(confirm_frames=2)
        e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=True, now=1.0)
        assert e.low_streak == 1
        e.decide(_num(1600, 1640), _orb(ratio=0.97), low=False, foreground=True, now=2.0)
        assert e.low_streak == 0

    def test_streak_holds_on_unknown(self):
        """UNKNOWN не сбрасывает streak (держит как есть)."""
        e = _make_engine(confirm_frames=3, menu_grace_s=0.5)
        e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=True, now=1.0)
        assert e.low_streak == 1
        # UNKNOWN (menu_grace expired, no orb data)
        r = e.decide(None, _orb(ratio=None, trip_below=False), None, foreground=True, now=2.0)
        assert r == HPDecision.UNKNOWN
        assert e.low_streak == 1  # не сброшен

    def test_orb_full_grace_anti_glitch(self):
        """Цифры ниже порога, но орб полный и grace не истёк → HIGH (anti-glitch)."""
        e = _make_engine(threshold=0.88, num_low_grace_s=0.35)
        # кадр 1: цифры низкие, орб полный → num_low_since starts
        e.decide(_num(800, 1640), _orb(ratio=0.95), low=False, foreground=True, now=1.0)
        assert e.low_streak == 0  # grace block → not LOW
        # кадр 2: сразу же, grace не истёк → всё ещё HIGH
        r = e.decide(_num(800, 1640), _orb(ratio=0.95), low=False, foreground=True, now=1.2)
        assert r == HPDecision.HIGH
        # кадр 3: grace истёк → LOW
        r = e.decide(_num(800, 1640), _orb(ratio=0.95), low=False, foreground=True, now=1.5)
        assert r == HPDecision.LOW


# ── Foreground reset ────────────────────────────────────────────────────

class TestForeground:
    def test_out_of_focus_resets(self):
        """Потеря фокуса сбрасывает всё состояние."""
        e = _make_engine(confirm_frames=1)
        e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=True, now=1.0)
        assert e.low_streak == 1
        r = e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=False, now=2.0)
        assert r == HPDecision.HIGH
        assert e.low_streak == 0
        assert e.last_good_ratio is None


# ── Orb fallback: blind orb ────────────────────────────────────────────

class TestOrbBlind:
    def test_blind_orb_continues_drinking(self):
        """Орб ослеп (ratio=None), но красная лужа + tripwire + last_good низкий → продолжает пить."""
        e = _make_engine(threshold=0.88)
        # сначала установим last_good_ratio через цифры
        e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=True, now=1.0)
        assert e.last_good_ratio == pytest.approx(500 / 1640, abs=0.01)
        # теперь орб ослеп, но trip_below + red_frac → fallback продолжает
        r = e.decide(None, _orb(ratio=None, trip_below=True, red_frac=0.08, fill_v=60),
                     low=None, foreground=True, now=2.0)
        # num is None, within menu_grace → orb fallback
        assert r == HPDecision.LOW

    def test_blind_orb_no_tripwire_no_drink(self):
        """Орб ослеп, нет tripwire → UNKNOWN."""
        e = _make_engine(threshold=0.88)
        e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=True, now=1.0)
        r = e.decide(None, _orb(ratio=None, trip_below=False, red_frac=0.08, fill_v=60),
                     low=None, foreground=True, now=2.0)
        assert r == HPDecision.UNKNOWN  # no tripwire → unknown


# ── Reset ──────────────────────────────────────────────────────────────

class TestReset:
    def test_reset_clears_all(self):
        e = _make_engine()
        e.decide(_num(500, 1640), _orb(ratio=0.30), low=True, foreground=True, now=1.0)
        e.reset()
        assert e.low_streak == 0
        assert e.last_good_ratio is None
        assert e.decision == HPDecision.HIGH
        assert e.hp_pct is None
        assert e.hp_src == "-"
