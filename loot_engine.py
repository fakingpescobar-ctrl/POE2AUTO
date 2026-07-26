"""Логика подбора для геймпада: решает, жать ли pickup (A/Cross).

На контроллере нет курсора для лута — подбор это нажатие кнопки рядом с предметом.
Поэтому движок не «целится», а отвечает на вопрос: есть ли лут в радиусе и прошёл ли
кулдаун. Где именно стоять — двигает игрок (DualSense), бот только жмёт подбор.

priority_mode:
  size      — крупные первые (по умолчанию)
  distance  — ближе к центру персонажа first
"""
import math
import time


class LootEngine:
    def __init__(self, radius, center_offset, cooldown_ms, radius_frac=0.0,
                 priority_mode="size"):
        # radius в пикселях (для совместимости) или radius_frac как доля от min(h,w)
        self.radius_px = radius
        self.radius_frac = radius_frac
        self.center_offset = list(center_offset)
        self.cooldown = cooldown_ms / 1000.0
        self.priority_mode = priority_mode
        self._last_press = 0.0

    def _get_radius(self, frame_shape):
        """Радиус в пикселях: если задан radius_frac > 0, считаем от меньшего размера кадра."""
        if self.radius_frac > 0:
            h, w = frame_shape[:2]
            return int(min(h, w) * self.radius_frac)
        return self.radius_px

    def center(self, frame_shape):
        """Центр персонажа в координатах кадра."""
        h, w = frame_shape[:2]
        return (w // 2 + self.center_offset[0], h // 2 + self.center_offset[1])

    def targets_in_radius(self, points, frame_shape, center=None):
        """Цели (cx, cy, area) в пределах радиуса от центра персонажа.
        center — переопределить центр (для детекции по ROI-кропу вокруг персонажа).
        Сортировка по priority_mode."""
        cx, cy = center if center is not None else self.center(frame_shape)
        r = self._get_radius(frame_shape)
        r2 = r * r
        in_r = [p for p in points if (p[0] - cx) ** 2 + (p[1] - cy) ** 2 <= r2]
        if self.priority_mode == "distance":
            in_r.sort(key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        else:
            in_r.sort(key=lambda p: p[2], reverse=True)
        return in_r

    def ready(self):
        """Прошёл ли кулдаун с прошлого нажатия."""
        return time.perf_counter() - self._last_press >= self.cooldown

    def mark_pressed(self):
        self._last_press = time.perf_counter()
