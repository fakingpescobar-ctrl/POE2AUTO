"""Тесты для модуля auto_flask.py"""
import pytest
from unittest.mock import MagicMock, patch
from auto_flask import FlaskRule, FlaskState, FlaskDecision, AutoFlaskEngine, default_rules


class TestFlaskRule:
    """Тесты для класса FlaskRule."""

    def test_flask_rule_creation(self):
        """FlaskRule должен создаваться с корректными значениями."""
        rule = FlaskRule(
            name="HP Critical",
            key="xinput_left_trigger",
            hp_below=0.3,
            cooldown_s=1.0,
        )
        assert rule.name == "HP Critical"
        assert rule.key == "xinput_left_trigger"
        assert rule.hp_below == 0.3
        assert rule.cooldown_s == 1.0
        assert rule.enabled is True  # по умолчанию True

    def test_flask_rule_mana(self):
        """FlaskRule с mana_below должен корректно хранить значение."""
        rule = FlaskRule(
            name="Mana Low",
            key="xinput_right_trigger",
            mana_below=0.3,
            cooldown_s=2.0,
        )
        assert rule.mana_below == 0.3

    def test_flask_rule_default_values(self):
        """FlaskRule должен иметь корректные значения по умолчанию."""
        rule = FlaskRule(name="Test", key="key")
        assert rule.enabled is True
        assert rule._last_used == 0.0
        assert rule.hp_below is None
        assert rule.mana_below is None
        assert rule.enemies_nearby is None
        assert rule.enemy_radius == 30.0

    def test_flask_rule_is_ready_before_cooldown(self):
        """is_ready должен возвращать False если кулдаун не прошёл."""
        rule = FlaskRule(name="Test", key="key", cooldown_s=5.0)
        rule._last_used = 0.0
        assert rule.is_ready(2.0) is False  # 2 < 5


class TestFlaskState:
    """Тесты для класса FlaskState."""

    def test_flask_state_creation(self):
        """FlaskState должен создаваться с корректными дефолтными значениями."""
        state = FlaskState()
        assert state.hp_pct is None
        assert state.mana_pct is None
        assert state.enemies_in_range == 0
        assert state.foreground is False
        assert state.in_hideout is False

    def test_flask_state_with_values(self):
        """FlaskState должен принимать значения."""
        state = FlaskState(
            hp_pct=0.25,
            mana_pct=0.5,
            enemies_in_range=5,
            foreground=True,
            in_hideout=False,
        )
        assert state.hp_pct == 0.25
        assert state.mana_pct == 0.5
        assert state.enemies_in_range == 5
        assert state.foreground is True
        assert state.in_hideout is False


class TestFlaskDecision:
    """Тесты для класса FlaskDecision."""

    def test_flask_decision_no_rule(self):
        """should_press должен быть False если rule is None."""
        decision = FlaskDecision(reason="no rule triggered")
        assert decision.should_press is False

    def test_flask_decision_with_rule(self):
        """should_press должен быть True если rule задан."""
        rule = FlaskRule(name="HP Critical", key="key")
        decision = FlaskDecision(rule=rule, reason="HP low")
        assert decision.should_press is True


class TestAutoFlaskEngine:
    """Тесты для класса AutoFlaskEngine."""

    def test_engine_creation_empty_rules(self):
        """AutoFlaskEngine должен создаваться без правил."""
        engine = AutoFlaskEngine()
        assert engine.rules == []
        assert engine.enabled is True

    def test_engine_creation_with_rules(self):
        """AutoFlaskEngine должен принимать список правил."""
        rules = [
            FlaskRule(name="HP Critical", key="key", hp_below=0.3),
            FlaskRule(name="Mana Low", key="key", mana_below=0.3),
        ]
        engine = AutoFlaskEngine(rules)
        assert len(engine.rules) == 2

    def test_engine_disable(self):
        """disabled engine должен всегда возвращать no rule triggered."""
        engine = AutoFlaskEngine()
        engine.enabled = False
        state = FlaskState(hp_pct=0.1, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is False

    def test_engine_hideout(self):
        """В hideout engine должен возвращать no rule triggered."""
        engine = AutoFlaskEngine()
        state = FlaskState(hp_pct=0.1, foreground=True, in_hideout=True)
        decision = engine.evaluate(state)
        assert decision.should_press is False

    def test_engine_background(self):
        """В неактивном окне engine должен возвращать no rule triggered."""
        engine = AutoFlaskEngine()
        state = FlaskState(hp_pct=0.1, foreground=False)
        decision = engine.evaluate(state)
        assert decision.should_press is False

    def test_hp_below_trigger(self):
        """HP ниже порога должен срабатывать."""
        rule = FlaskRule(name="HP Critical", key="key", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.2, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is True
        assert decision.rule == rule

    def test_hp_above_no_trigger(self):
        """HP выше порога не должен срабатывать."""
        rule = FlaskRule(name="HP Critical", key="key", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.8, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is False

    def test_mana_below_trigger(self):
        """Mana ниже порога должна срабатывать."""
        rule = FlaskRule(name="Mana Low", key="key", mana_below=0.3)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(mana_pct=0.2, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is True
        assert decision.rule == rule

    def test_enemies_nearby_trigger(self):
        """Врагов достаточно много должен срабатывать."""
        rule = FlaskRule(name="Enemies", key="key", enemies_nearby=3)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(enemies_in_range=5, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is True
        assert decision.rule == rule

    def test_enemies_not_enough_no_trigger(self):
        """Врагов мало не должен срабатывать."""
        rule = FlaskRule(name="Enemies", key="key", enemies_nearby=3)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(enemies_in_range=2, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is False

    def test_hp_none_no_trigger(self):
        """HP unknown (None) не должен срабатывать."""
        rule = FlaskRule(name="HP Critical", key="key", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=None, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is False

    def test_rule_cooldown_prevents_trigger(self):
        """Правило с кулдауном не должно срабатывать."""
        rule = FlaskRule(name="HP Critical", key="key", hp_below=0.5, cooldown_s=5.0)
        engine = AutoFlaskEngine([rule])
        # Имитируем недавнее использование
        rule._last_used = 0.0
        state = FlaskState(hp_pct=0.1, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is False  # кулдаун 5с, прошло только 2с

    def test_mark_used_resets_cooldown(self):
        """mark_used должен сбрасывать кулдаун."""
        rule = FlaskRule(name="HP Critical", key="key", cooldown_s=1.0)
        engine = AutoFlaskEngine([rule])
        # Помечаем использование
        engine.mark_used(rule)
        # После mark_used кулдаун сброшен
        state = FlaskState(hp_pct=0.1, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is True  # кулдаун сброшен

    def test_add_rule(self):
        """add_rule должен добавлять правило."""
        engine = AutoFlaskEngine()
        rule = FlaskRule(name="New Rule", key="key")
        engine.add_rule(rule)
        assert len(engine.rules) == 1
        assert engine.rules[0].name == "New Rule"

    def test_remove_rule(self):
        """remove_rule должен удалять правило по имени."""
        rules = [
            FlaskRule(name="Rule1", key="key"),
            FlaskRule(name="Rule2", key="key"),
        ]
        engine = AutoFlaskEngine(rules)
        engine.remove_rule("Rule1")
        assert len(engine.rules) == 1
        assert engine.rules[0].name == "Rule2"

    def test_remove_nonexistent_rule(self):
        """Удаление несуществующего правила не должно вызывать ошибку."""
        engine = AutoFlaskEngine([])
        engine.remove_rule("NonExistent")
        assert engine.rules == []

    def test_multiple_rules_priority(self):
        """Первое сработавшее правило должно быть возвращено."""
        rules = [
            FlaskRule(name="HP Critical", key="key", hp_below=0.3),
            FlaskRule(name="HP Low", key="key", hp_below=0.5),
        ]
        engine = AutoFlaskEngine(rules)
        state = FlaskState(hp_pct=0.2, foreground=True)
        decision = engine.evaluate(state)
        # Первое правило (HP Critical) должно быть первым
        assert decision.rule.name == "HP Critical"


class TestDefaultRules:
    """Тесты для функции default_rules."""

    def test_default_rules_count(self):
        """default_rules должен возвращать 4 правила."""
        rules = default_rules()
        assert len(rules) == 4

    def test_default_rules_names(self):
        """default_rules должен иметь правильные имена правил."""
        rules = default_rules()
        names = [r.name for r in rules]
        assert "HP Critical" in names
        assert "HP Low" in names
        assert "Mana Low" in names
        assert "Enemies + Low HP" in names

    def test_hp_critical_below_0_3(self):
        """HP Critical должен иметь hp_below=0.3."""
        rules = default_rules()
        hp_critical = next(r for r in rules if r.name == "HP Critical")
        assert hp_critical.hp_below == 0.3

    def test_hp_low_below_0_5(self):
        """HP Low должен иметь hp_below=0.5."""
        rules = default_rules()
        hp_low = next(r for r in rules if r.name == "HP Low")
        assert hp_low.hp_below == 0.5

    def test_mana_low_below_0_3(self):
        """Mana Low должен иметь mana_below=0.3."""
        rules = default_rules()
        mana_low = next(r for r in rules if r.name == "Mana Low")
        assert mana_low.mana_below == 0.3

    def test_enemies_low_hp(self):
        """Enemies + Low HP должен иметь hp_below=0.7 и enemies_nearby=3."""
        rules = default_rules()
        enemies_rule = next(r for r in rules if r.name == "Enemies + Low HP")
        assert enemies_rule.hp_below == 0.7
        assert enemies_rule.enemies_nearby == 3


class TestAutoFlaskIntegration:
    """Интеграционные тесты для auto_flask."""

    def test_full_engine_with_rules(self):
        """Полный сценарий работы engine с правилами."""
        rules = [
            FlaskRule(name="HP Critical", key="lt", hp_below=0.3, cooldown_s=1.0),
            FlaskRule(name="Mana Low", key="rt", mana_below=0.3, cooldown_s=2.0),
        ]
        engine = AutoFlaskEngine(rules)

        # HP низкий - должно сработать HP Critical
        state = FlaskState(hp_pct=0.1, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is True
        assert decision.rule.name == "HP Critical"

        # Помечаем использование
        engine.mark_used(decision.rule)

        # Теперь кулдаун HP, но Mana Low может сработать
        state = FlaskState(mana_pct=0.2, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is True
        assert decision.rule.name == "Mana Low"


class TestFlaskRuleEdgeCases:
    """Тесты для граничных случаев FlaskRule."""

    def test_hp_at_exactly_threshold(self):
        """HP ровно на пороге не должно срабатывать (> порога, а не <=)."""
        rule = FlaskRule(name="Test", key="key", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.5, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is False  # hp_below = 0.5, hp_pct = 0.5 - не ниже

    def test_hp_below_threshold(self):
        """HP чуть ниже порога должно срабатывать."""
        rule = FlaskRule(name="Test", key="key", hp_below=0.5)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.49, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is True

    def test_zero_hp(self):
        """HP = 0.0 должно срабатывать для любого порога."""
        rule = FlaskRule(name="Test", key="key", hp_below=0.9)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=0.0, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is True

    def test_flask_state_unity(self):
        """HP 1.0 (полное) не должно срабатывать."""
        rule = FlaskRule(name="Test", key="key", hp_below=0.9)
        engine = AutoFlaskEngine([rule])
        state = FlaskState(hp_pct=1.0, foreground=True)
        decision = engine.evaluate(state)
        assert decision.should_press is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
