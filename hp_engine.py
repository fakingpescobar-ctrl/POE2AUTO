"""HPEngine — принятие решений по HP: какой источник → low/high/unknown/clip.

Вынесен из bot.py для тестируемости. Сигнатура:
    engine.decide(num, hm, low, foreground) → HPDecision

Состояние (last_good_ratio, num_blind_since, low_streak и т.д.) живёт внутри
 engine и не洩露 наружу — bot.py только читает engine.decision / engine.low_streak.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Optional, Tuple


class HPDecision(Enum):
    LOW = "low"
    HIGH = "high"
    UNKNOWN = "unknown"
    CLIP = "clip"       # цифры обрезаны ROI → нужна перелокализация


class HPEngine:
    """Принимает решение по HP на основе двух источников: цифры HUD и орб.

    Внешний мир подаёт на вход decide():
      num — результат OCR: (current, max, ratio) | None
      hm  — результат hp_detector.is_low(): dict(ratio, fill_v, trip_below, red_frac)
      low — результат hp_detector.is_low() первый элемент (bool|None)
      foreground — окно игры в фокусе
      now — время (time.perf_counter()) для юнит-тестов с фиксированным временем
    """

    def __init__(self, *, threshold: float = 0.88, confirm_frames: int = 1,
                 num_low_grace_s: float = 0.35,
                 fallback_red_frac_min: float = 0.04,
                 valid_val: float = 45,
                 menu_grace_s: float = 0.5):
        self.threshold = threshold
        self.confirm_frames = confirm_frames
        self.num_low_grace_s = num_low_grace_s
        self.fallback_red_frac_min = fallback_red_frac_min
        self.valid_val = valid_val
        self.menu_grace_s = menu_grace_s

        # persistent state (межкадровое)
        self._last_good_ratio: Optional[float] = None
        self._num_blind_since: Optional[float] = None
        self._num_low_since: Optional[float] = None
        self._low_streak: int = 0
        self._last_clip_warn: float = -10.0

        # результат последнего decide()
        self.decision: HPDecision = HPDecision.HIGH
        self.hp_pct: Optional[float] = None
        self.hp_src: str = "-"
        self.need_relocate: bool = False

    @property
    def low_streak(self) -> int:
        return self._low_streak

    @property
    def last_good_ratio(self) -> Optional[float]:
        return self._last_good_ratio

    def reset(self) -> None:
        """Сбросить состояние (при потере фокуса)."""
        self._last_good_ratio = None
        self._num_blind_since = None
        self._num_low_since = None
        self._low_streak = 0
        self.decision = HPDecision.HIGH
        self.hp_pct = None
        self.hp_src = "-"
        self.need_relocate = False

    def decide(self, num: Optional[Tuple[int, int, float]],
               hm: dict,
               low: Optional[bool],
               foreground: bool,
               now: Optional[float] = None) -> HPDecision:
        """Принять решение по HP на основе текущего кадра.

        Args:
            num: (current, max, ratio) из hp_ocr | None если не читается
            hm:  dict(ratio, fill_v, trip_below, red_frac) из hp_detector
            low: bool из hp_detector.is_low() (первый элемент)
            foreground: окно игры в фокусе
            now: time.perf_counter() — для тестов с фиксированным временем

        Returns:
            HPDecision — LOW / HIGH / UNKNOWN / CLIP
        """
        if now is None:
            now = time.perf_counter()

        self.need_relocate = False

        if not foreground:
            self.reset()
            self.decision = HPDecision.HIGH
            return self.decision

        # --- выбор источника ---
        use_orb = False
        low_now: Optional[bool] = None  # True/False/None(неизвестно)
        if hm is None:
            hm = {}

        if num is not None:
            # --- ЦИФРЫ ЧИТАЮТСЯ ---
            self._num_blind_since = None
            num_ratio = num[2]
            orb_ratio = hm.get("ratio")
            orb_full = orb_ratio is not None and orb_ratio >= self.threshold
            self.hp_pct = num_ratio
            self.hp_src = "num"

            if orb_full and num_ratio < 0.25:
                # ЭКСТРЕМ-низкие цифры при полном орбе — обрезка ROI
                if now - self._last_clip_warn > 3.0:
                    self._last_clip_warn = now
                    self.need_relocate = True
                self._num_low_since = None
                low_now = None
            elif num_ratio < self.threshold:
                self._last_good_ratio = num_ratio
                if self._num_low_since is None:
                    self._num_low_since = now
                # анти-глитч: ждём стабильности при несогласном орбе
                low_now = not (orb_full and
                               now - self._num_low_since < self.num_low_grace_s)
            else:
                self._last_good_ratio = num_ratio
                self._num_low_since = None
                low_now = False

        elif num is None and hm is None:
            # --- ЦИФРЫ НЕ ЧИТАЮТСЯ + ОРБА НЕТ ---
            low_now = None
            self.hp_src = "-"
            self.hp_pct = None

        else:
            # --- ЦИФРЫ ВКЛЮЧЕНЫ, НО НЕ ЧИТАЮТСЯ НА ЭТОМ КАДРЕ ---
            self.hp_pct = hm.get("ratio")
            self.hp_src = "orb"
            if self._num_blind_since is None:
                self._num_blind_since = now
            if now - self._num_blind_since <= self.menu_grace_s:
                use_orb = True
            else:
                low_now = None  # меню/HUD скрыт → не пьём

        if use_orb:
            low_now = self._orb_logic(hm, low)

        # --- дебаунс ---
        if low_now is True:
            self._low_streak += 1
        elif low_now is None:
            pass  # неизвестно — держим стрик
        else:
            self._low_streak = 0

        # --- итоговое решение ---
        if self.need_relocate:
            self.decision = HPDecision.CLIP
        elif low_now and self._low_streak >= self.confirm_frames:
            self.decision = HPDecision.LOW
        elif low_now is None:
            self.decision = HPDecision.UNKNOWN
        else:
            self.decision = HPDecision.HIGH

        return self.decision

    def _orb_logic(self, hm: dict, low: Optional[bool]) -> Optional[bool]:
        """Fallback на орб: ratio → слепой орб посреди просадки."""
        orb_ratio = hm.get("ratio")
        if orb_ratio is not None:
            self._last_good_ratio = orb_ratio
            return low
        # слепой орб: красная лужа + tripwire + последнее HP было низким
        if (hm.get("trip_below")
                and hm.get("red_frac", 0) >= self.fallback_red_frac_min
                and hm.get("fill_v", 0) >= self.valid_val
                and self._last_good_ratio is not None
                and self._last_good_ratio < self.threshold):
            return True
        return None
