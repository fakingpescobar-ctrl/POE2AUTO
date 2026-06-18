"""Логика подбора для геймпада: решает, жать ли pickup (A/Cross).

На контроллере нет курсора для лута — подбор это нажатие кнопки рядом с предметом.
Поэтому движок не «целится», а отвечает на вопрос: есть ли лут в радиусе и прошёл ли
кулдаун. Где именно стоять — двигает игрок (DualSense), бот только жмёт подбор.
"""
import time


class LootEngine:
    def __init__(self, radius, center_offset, cooldown_ms):
        self.radius = radius
        self.center_offset = list(center_offset)
        self.cooldown = cooldown_ms / 1000.0
        self._last_press = 0.0

    def center(self, frame_shape):
        """Центр персонажа в координатах кадра."""
        h, w = frame_shape[:2]
        return (w // 2 + self.center_offset[0], h // 2 + self.center_offset[1])

    def targets_in_radius(self, points, frame_shape):
        """Цели (cx, cy, area) в пределах радиуса от центра персонажа."""
        cx, cy = self.center(frame_shape)
        r2 = self.radius * self.radius
        return [p for p in points if (p[0] - cx) ** 2 + (p[1] - cy) ** 2 <= r2]

    def ready(self):
        """Прошёл ли кулдаун с прошлого нажатия."""
        return time.perf_counter() - self._last_press >= self.cooldown

    def mark_pressed(self):
        self._last_press = time.perf_counter()
