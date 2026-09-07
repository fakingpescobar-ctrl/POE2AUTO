"""Tests for auto_flask.py — AutoFlaskEngine, FlaskRule, FlaskState, FlaskDecision."""
import time
from unittest.mock import patch

import pytest

from auto_flask import AutoFlaskEngine, FlaskDecision, FlaskRule, FlaskState, default_rules


class TestFlaskRule:
    def test_is_ready_initial(self):
        rule = FlaskRule(name="test", key="a", hp_below=0.5)
        assert rule.is_ready(time.perf_counter())

    def test_is_ready_after_cooldown(self):
        rule = FlaskRule(name="test", key="a", hp_below=0.5, cooldown_s=1.0)
        rule.mark_used(time.perf_counter())
        assert not rule.is_ready(time.perf_counter())
        # After cooldown
        with patch("auto_flask.time.perf_counter", return_value=time.perf_counter() + 1.1):
            assert rule.is_ready(time.perf_counter())

    def test_mark_used(self):
        rule = FlaskRule(name="test", key="a", hp_below=0.5)
        rule.mark_used(100.0)
        assert rule._last_used == 100.0

    def test_disabled_rule(self):
        rule = FlaskRule(name="test", key="a", hp_below=0.5, enabled=False)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.1, foreground=True)
        decision = engine.evaluate(state)
        assert not decision.should_press


class TestFlaskState:
    def test_defaults(self):
        state = FlaskState()
        assert state.hp_pct is None
        assert state.mana_pct is None
        assert state.enemies_in_range == 0
        assert not state.foreground
        assert not state.in_hideout


class TestFlaskDecision:
    def test_should_press(self):
        rule = FlaskRule(name="test", key="a")
        decision = FlaskDecision(rule=rule)
        assert decision.should_press

    def test_should_not_press(self):
        decision = FlaskDecision()
        assert not decision.should_press


class TestAutoFlaskEngine:
    def test_empty_rules(self):
        engine = AutoFlaskEngine([])
        state = FlaskState(hp_pct=0.1, foreground=True)
        decision = engine.evaluate(state)
        assert not decision.should_press
        assert decision.reason == "no rule triggered"

    def test_hp_below_triggers(self):
        rule = FlaskRule(name="HP Low", key="a", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.3, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press
        assert decision.rule == rule
        assert "HP=30%<50%" in decision.reason

    def test_hp_above_no_trigger(self):
        rule = FlaskRule(name="HP Low", key="a", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.7, foreground=True)
        decision = engine.evaluate(state)
        assert not decision.should_press

    def test_mana_below_triggers(self):
        rule = FlaskRule(name="Mana Low", key="b", mana_below=0.3)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(mana_pct=0.2, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press
        assert "MP=20%<30%" in decision.reason

    def test_enemies_nearby_triggers(self):
        rule = FlaskRule(name="Swarm", key="c", enemies_nearby=3)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(enemies_in_range=5, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press
        assert "enemies=5>=3" in decision.reason

    def test_combined_hp_enemies(self):
        rule = FlaskRule(name="HP+Enemies", key="a", hp_below=0.7, enemies_nearby=3)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.5, enemies_in_range=4, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press
        assert "HP=50%<70%" in decision.reason
        assert "enemies=4>=3" in decision.reason

    def test_hp_none_skips(self):
        rule = FlaskRule(name="HP Low", key="a", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=None, foreground=True)
        decision = engine.evaluate(state)
        assert not decision.should_press

    def test_mana_none_skips(self):
        rule = FlaskRule(name="Mana Low", key="b", mana_below=0.3)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(mana_pct=None, foreground=True)
        decision = engine.evaluate(state)
        assert not decision.should_press

    def test_background_no_trigger(self):
        rule = FlaskRule(name="HP Low", key="a", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.1, foreground=False)
        decision = engine.evaluate(state)
        assert not decision.should_press
        assert "background" in decision.reason

    def test_hideout_no_trigger(self):
        rule = FlaskRule(name="HP Low", key="a", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.1, foreground=True, in_hideout=True)
        decision = engine.evaluate(state)
        assert not decision.should_press
        assert "hideout" in decision.reason

    def test_cooldown_prevents_double_press(self):
        rule = FlaskRule(name="HP Low", key="a", hp_below=0.5, cooldown_s=2.0)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.1, foreground=True)

        # First press
        decision1 = engine.evaluate(state)
        assert decision1.should_press
        engine.mark_used(decision1.rule)

        # Second press immediately — cooldown
        decision2 = engine.evaluate(state)
        assert not decision2.should_press

    def test_priority_first_matching_wins(self):
        rule1 = FlaskRule(name="HP Critical", key="a", hp_below=0.3)
        rule2 = FlaskRule(name="HP Low", key="b", hp_below=0.5)
        engine = AutoFlaskEngine([rule1, rule2])
        state = FlaskState(hp_pct=0.2, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press
        assert decision.rule.name == "HP Critical"  # First matching

    def test_add_remove_rule(self):
        engine = AutoFlaskEngine()
        rule = FlaskRule(name="test", key="a", hp_below=0.5)
        engine.add_rule(rule)
        assert len(engine.rules) == 1
        engine.remove_rule("test")
        assert len(engine.rules) == 0

    def test_engine_disabled(self):
        engine = AutoFlaskEngine([FlaskRule(name="test", key="a", hp_below=0.5)])
        engine.enabled = False
        state = FlaskState(hp_pct=0.1, foreground=True)
        decision = engine.evaluate(state)
        assert not decision.should_press
        assert "disabled" in decision.reason


class TestDefaultRules:
    def test_default_rules_count(self):
        rules = default_rules()
        assert len(rules) == 4

    def test_default_rules_names(self):
        rules = default_rules()
        names = [r.name for r in rules]
        assert "HP Critical" in names
        assert "HP Low" in names
        assert "Mana Low" in names
        assert "Enemies + Low HP" in names

    def test_default_hp_critical(self):
        rules = default_rules()
        hp_crit = next(r for r in rules if r.name == "HP Critical")
        assert hp_crit.hp_below == 0.3
        assert hp_crit.cooldown_s == 1.0
