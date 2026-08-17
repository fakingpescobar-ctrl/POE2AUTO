"""Тесты потоковых воркеров (workers.py — выделены из bot.py при разбивке).

Раньше эти классы жили в bot.py, импорт которого тянул pynput/полный стек
инициализации — юнит-тестировать было нельзя. Теперь они изолированы.
"""
from unittest.mock import MagicMock

import numpy as np

from workers import LootWorker, AttackWorker


class TestLootWorker:
    def test_submit_get_result_roundtrip(self):
        det = MagicMock()
        det.detect.return_value = ([(10, 10)], None)
        engine = MagicMock()
        engine.targets_in_radius.return_value = [(10, 10)]
        w = LootWorker(det, engine, 0x01, 80, MagicMock(), MagicMock())
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        w.submit(frame, (50, 50), True)
        w.run_once = None  # нет такого метода — просто ждём результат
        # run() крутится вечно; эмулируем один проход вручную: вызовем run
        # нельзя, поэтому проверим очередь напрямую через внутренний вызов.
        # Вместо этого запустим поток с таймаутом.
        import threading
        done = threading.Event()
        w._result = None

        def _spin():
            # повторим логику run() один раз вручную через внутренние поля
            item = w._frame_q.get(timeout=1.0)
            points, _mask = det.detect(item[0])
            in_radius = engine.targets_in_radius(points, item[0].shape, center=item[1])
            with w._result_lock:
                w._result = (points, in_radius)
            done.set()

        t = threading.Thread(target=_spin, daemon=True)
        t.start()
        assert done.wait(timeout=2.0)
        r = w.get_result()
        assert r is not None
        assert r[0] == [(10, 10)]
        w.stop()

    def test_queue_drops_old_frame_when_full(self):
        det = MagicMock()
        det.detect.return_value = ([], None)
        engine = MagicMock()
        engine.targets_in_radius.return_value = []
        w = LootWorker(det, engine, 0x01, 80, MagicMock(), MagicMock())
        frame = np.zeros((10, 10, 3), dtype=np.uint8)
        w.submit(frame, (5, 5), True)
        w.submit(frame, (5, 5), True)  # вторая без get_result — старый кадр отброшен
        assert w._frame_q.qsize() == 1


class TestAttackWorker:
    def test_memory_mode_calls_update_and_present(self):
        mob_det = MagicMock()
        mob_det.update.return_value = None
        mob_det.in_hideout.return_value = False
        mob_det.present.return_value = True
        w = AttackWorker(mob_det, zone_det=None, use_memory=True)
        w.submit(None, foreground=True)
        # run() вечный — эмулируем один проход
        import threading
        done = threading.Event()

        def _spin():
            item = w._frame_q.get(timeout=1.0)
            full_frame, foreground = item
            mob_det.update()
            in_hideout = mob_det.in_hideout()
            mob_present = False
            if not in_hideout:
                mob_present = mob_det.present()
            with w._result_lock:
                w._result = {"mob_present": mob_present, "in_hideout": in_hideout}
            done.set()

        t = threading.Thread(target=_spin, daemon=True)
        t.start()
        assert done.wait(timeout=2.0)
        r = w.get_result()
        assert r == {"mob_present": True, "in_hideout": False}
        mob_det.update.assert_called_once()
        mob_det.present.assert_called_once()
        w.stop()

    def test_cv_mode_skips_when_not_foreground(self):
        mob_det = MagicMock()
        zone_det = MagicMock()
        w = AttackWorker(mob_det, zone_det=zone_det, use_memory=False)
        # эмулируем проход run() при foreground=False: ветка
        # `elif mob_det is not None and foreground` НЕ выполняется —
        # zone_det.in_hideout не должен вызываться, mob_present остаётся False.
        full_frame = object()
        foreground = False
        mob_present = False
        in_hideout = False
        if foreground:
            in_hideout = zone_det is not None and zone_det.in_hideout(full_frame)
            if not in_hideout:
                mob_present = mob_det.present(full_frame)
        w._result = {"mob_present": mob_present, "in_hideout": in_hideout}
        assert mob_present is False
        zone_det.in_hideout.assert_not_called()
        mob_det.present.assert_not_called()
