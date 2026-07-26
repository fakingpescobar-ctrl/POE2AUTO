"""Auto-flask система на основе чтения памяти.

Правила (как в POE2Radar):
- HP% ниже порога → нажать флягу
- Mana% ниже порога → нажать флягу
- Врагов рядом больше порога → нажать флягу
- Кулдаун на каждое правило отдельно
- Foreground-gated (только в фокусе)

Адаптировано из POE2Radar (Sikaka, MIT).
"""
import time
import logging
from typing import Optional, List
from dataclasses import dataclass, field

_log = logging.getLogger("poe2auto.autoflask")


@dataclass
class FlaskRule:
    """Одно правило автофляски."""
    name: str
    key: str                       # virtual key code (например 'xinput_a')
    hp_below: Optional[float] = None      # HP% порог (0.0-1.0)
    mana_below: Optional[float] = None    # Mana% порог (0.0-1.0)
    enemies_nearby: Optional[int] = None  # мин. кол-во врагов рядом
    enemy_radius: float = 30.0            # радиус подсчёта врагов (сетевые единицы)
    cooldown_s: float = 1.0               # кулдаун между использованиями (секунды)
    enabled: bool = True

    # Внутреннее состояние
    _last_used: float = field(default=0.0, repr=False)

    def is_ready(self, now: float) -> bool:
        """Готово ли правило к использованию (кулдаун прошёл)."""
        return now - self._last_used >= self.cooldown_s

    def mark_used(self, now: float):
        """Пометить использование (сбросить кулдаун)."""
        self._last_used = now


@dataclass
class FlaskState:
    """Состояние для оценки правил."""
    hp_pct: Optional[float] = None      # 0.0-1.0 или None если неизвестно
    mana_pct: Optional[float] = None    # 0.0-1.0 или None если неизвестно
    enemies_in_range: int = 0
    foreground: bool = False
    in_hideout: bool = False


@dataclass
class FlaskDecision:
    """Решение какую флягу нажать."""
    rule: Optional[FlaskRule] = None
    reason: str = ""

    @property
    def should_press(self) -> bool:
        return self.rule is not None


class AutoFlaskEngine:
    """Движок автофляски с правилами.

    Использование:
        engine = AutoFlaskEngine(rules)
        decision = engine.evaluate(state)
        if decision.should_press:
            # нажать флягу decision.rule.key
            engine.mark_used(decision.rule)
    """

    def __init__(self, rules: Optional[List[FlaskRule]] = None):
        self.rules = rules or []
        self.enabled = True

    def evaluate(self, state: FlaskState) -> FlaskDecision:
        """Оценить все правила и вернуть лучшее решение."""
        if not self.enabled or state.in_hideout or not state.foreground:
            return FlaskDecision(reason="disabled/hideout/background")

        now = time.perf_counter()

        for rule in self.rules:
            if not rule.enabled or not rule.is_ready(now):
                continue

            # Проверяем HP порог
            if rule.hp_below is not None:
                if state.hp_pct is None:
                    continue  # HP неизвестно — не жмём
                if state.hp_pct > rule.hp_below:
                    continue  # HP выше порога

            # Проверяем Mana порог
            if rule.mana_below is not None:
                if state.mana_pct is None:
                    continue
                if state.mana_pct > rule.mana_below:
                    continue

            # Проверяем кол-во врагов
            if rule.enemies_nearby is not None:
                if state.enemies_in_range < rule.enemies_nearby:
                    continue

            # Правило сработало!
            reason_parts = []
            if rule.hp_below is not None:
                reason_parts.append(f"HP={state.hp_pct*100:.0f}%<{rule.hp_below*100:.0f}%")
            if rule.mana_below is not None:
                reason_parts.append(f"MP={state.mana_pct*100:.0f}%<{rule.mana_below*100:.0f}%")
            if rule.enemies_nearby is not None:
                reason_parts.append(f"enemies={state.enemies_in_range}>={rule.enemies_nearby}")

            return FlaskDecision(
                rule=rule,
                reason=f"{rule.name}: {', '.join(reason_parts)}"
            )

        return FlaskDecision(reason="no rule triggered")

    def mark_used(self, rule: FlaskRule):
        """Пометить что фляга была использована (сбросить кулдаун)."""
        rule.mark_used(time.perf_counter())

    def add_rule(self, rule: FlaskRule):
        """Добавить правило."""
        self.rules.append(rule)

    def remove_rule(self, name: str):
        """Удалить правило по имени."""
        self.rules = [r for r in self.rules if r.name != name]


def default_rules() -> List[FlaskRule]:
    """Стандартные правила фляски."""
    return [
        FlaskRule(
            name="HP Critical",
            key="xinput_left_trigger",
            hp_below=0.3,
            cooldown_s=1.0,
        ),
        FlaskRule(
            name="HP Low",
            key="xinput_left_trigger",
            hp_below=0.5,
            cooldown_s=3.0,
        ),
        FlaskRule(
            name="Mana Low",
            key="xinput_right_trigger",
            mana_below=0.3,
            cooldown_s=2.0,
        ),
        FlaskRule(
            name="Enemies + Low HP",
            key="xinput_left_trigger",
            hp_below=0.7,
            enemies_nearby=3,
            cooldown_s=2.0,
        ),
    ]
