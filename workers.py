"""Потоковые воркеры детекции (выделены из bot.py при разбивке на модули).

- LootWorker: детект лута в отдельном потоке — не блокирует главный цикл.
- AttackWorker: детект мобов в отдельном потоке — поддерживает CV
  (MobBarDetector), память (MemoryMobDetector) и reader (ReaderMobDetector).

Оба получают кадры неблокирующе (queue maxsize=1, старый кадр отбрасывается)
и отдают результат через get_result() — главный цикл не ждёт их.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("poe2auto.workers")


class LootWorker(threading.Thread):
    """Детект лута в отдельном потоке. Не блокирует главный цикл."""

    def __init__(self, detector, engine, pickup_mask, hold_ms, vx, bridge):
        super().__init__(daemon=True)
        self._cfg_lock = threading.Lock()
        self.detector = detector
        self.engine = engine
        self.pickup_mask = pickup_mask
        self.hold_ms = hold_ms
        self.vx = vx
        self.bridge = bridge
        self._frame_q: queue.Queue = queue.Queue(maxsize=1)
        self._result: Optional[Tuple[list, list]] = None
        self._result_lock = threading.Lock()
        self._stop_event = threading.Event()

    def update_config(self, detector, engine):
        """Обновить детектор и движок потокобезопасно."""
        with self._cfg_lock:
            self.detector = detector
            self.engine = engine

    def submit(self, loot_frame, loot_ctr, foreground):
        """Отправить кадр на обработку (неблокирующе). Старый кадр отбрасывается."""
        try:
            self._frame_q.put_nowait((loot_frame, loot_ctr, foreground))
        except queue.Full:
            pass

    def get_result(self) -> Optional[Tuple[list, list]]:
        """Взять последний результат (неблокирующе). None = ещё не обработан."""
        with self._result_lock:
            r = self._result
            self._result = None
            return r

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                loot_frame, loot_ctr, foreground = self._frame_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self._cfg_lock:
                    detector = self.detector
                    engine = self.engine
                points, _mask = detector.detect(loot_frame)
                in_radius = engine.targets_in_radius(points, loot_frame.shape, center=loot_ctr)
                with self._result_lock:
                    self._result = (points, in_radius)
            except Exception:
                # Защита от молчаливой гибели потока: падение в детекте лута
                # не должно убивать LootWorker (пикап бы молча умер).
                log.exception("LootWorker: ошибка детекта лута")


class AttackWorker(threading.Thread):
    """Детект мобов в отдельном потоке. Поддерживает CV (MobBarDetector) и память (MemoryMobDetector)."""

    def __init__(self, mob_det, zone_det=None, use_memory=False, cv_det=None):
        super().__init__(daemon=True)
        self._cfg_lock = threading.Lock()
        self.mob_det = mob_det
        self.zone_det = zone_det
        self.use_memory = use_memory
        self.cv_det = cv_det
        self._frame_q: queue.Queue = queue.Queue(maxsize=1)
        self._result: Optional[Dict[str, Any]] = None
        self._result_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.last_cv: Optional[bool] = None  # последний результат CV-подтверждения

    def update_config(self, mob_det, zone_det, use_memory, cv_det=None):
        """Обновить конфигурацию потокобезопасно."""
        with self._cfg_lock:
            self.mob_det = mob_det
            self.zone_det = zone_det
            self.use_memory = use_memory
            self.cv_det = cv_det

    def submit(self, full_frame=None, foreground=True):
        """Отправить кадр на обработку (неблокирующе). Для memory-детекта frame не нужен."""
        try:
            self._frame_q.put_nowait((full_frame, foreground))
        except queue.Full:
            pass

    def get_result(self) -> Optional[Dict[str, Any]]:
        """Взять последний результат (неблокирующе). None = ещё не обработан."""
        with self._result_lock:
            r = self._result
            self._result = None
            return r

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                full_frame, foreground = self._frame_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self._cfg_lock:
                    mob_det = self.mob_det
                    zone_det = self.zone_det
                    use_memory = self.use_memory
                mob_present = False
                in_hideout = False
                if use_memory and mob_det is not None:
                    mob_det.update()
                    in_hideout = mob_det.in_hideout()
                    if not in_hideout:
                        # «Живость» мобов определяется в reader_detect по Life-vtable
                        # (трупы/латентные сущности отсекаются на уровне парсинга),
                        # поэтому CV-подтверждение не нужно.
                        mob_present = mob_det.present()
                elif mob_det is not None and foreground:
                    in_hideout = zone_det is not None and zone_det.in_hideout(full_frame)
                    if not in_hideout:
                        mob_present = mob_det.present(full_frame)
                with self._result_lock:
                    self._result = {"mob_present": mob_present, "in_hideout": in_hideout}
            except Exception as e:
                log.error("AttackWorker error: %s", e, exc_info=True)
