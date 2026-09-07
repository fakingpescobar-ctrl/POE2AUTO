"""Integration tests for AttackWorker and LootWorker — submit/result cycle.

Covers critical path: frame → worker → detection → result retrieval.
Prevents regression where worker silently drops results or never starts.
"""
import time
import threading
import queue
from unittest.mock import MagicMock, patch
import sys

# Mock heavy dependencies
sys.modules.setdefault('game_memory', MagicMock())
sys.modules.setdefault('probe_cast', MagicMock())
sys.modules.setdefault('screen_capture', MagicMock())
sys.modules.setdefault('virtual_xbox', MagicMock())
sys.modules.setdefault('dualsense_bridge', MagicMock())
sys.modules.setdefault('nav_worker', MagicMock())

from workers import AttackWorker, LootWorker


class TestAttackWorker:
    """AttackWorker: submit frame → detect → get_result."""

    def test_submit_and_get_result_memory(self):
        """Memory mode: submit without frame, get mob_present/in_hideout."""
        mob_det = MagicMock()
        mob_det.update = MagicMock()
        mob_det.in_hideout.return_value = False
        mob_det.present.return_value = True

        worker = AttackWorker(mob_det, use_memory=True)
        worker.start()

        try:
            worker.submit(full_frame=None, foreground=True)
            # Worker processes asynchronously — poll with timeout
            result = None
            for _ in range(50):
                result = worker.get_result()
                if result is not None:
                    break
                time.sleep(0.05)

            assert result is not None, "AttackWorker never returned result"
            assert result["mob_present"] is True
            assert result["in_hideout"] is False
            mob_det.update.assert_called()
            mob_det.present.assert_called_once()
        finally:
            worker.stop()
            worker.join(timeout=2.0)

    def test_submit_and_get_result_hideout(self):
        """In hideout → mob_present must be False."""
        mob_det = MagicMock()
        mob_det.update = MagicMock()
        mob_det.in_hideout.return_value = True

        worker = AttackWorker(mob_det, use_memory=True)
        worker.start()

        try:
            worker.submit(full_frame=None, foreground=True)
            result = None
            for _ in range(50):
                result = worker.get_result()
                if result is not None:
                    break
                time.sleep(0.05)

            assert result is not None
            assert result["mob_present"] is False
            assert result["in_hideout"] is True
        finally:
            worker.stop()
            worker.join(timeout=2.0)

    def test_submit_cv_mode(self):
        """CV mode: frame is passed to mob_det.present(frame)."""
        mob_det = MagicMock()
        mob_det.present.return_value = True
        zone_det = MagicMock()
        zone_det.in_hideout.return_value = False

        fake_frame = MagicMock()

        worker = AttackWorker(mob_det, zone_det=zone_det, use_memory=False)
        worker.start()

        try:
            worker.submit(full_frame=fake_frame, foreground=True)
            result = None
            for _ in range(50):
                result = worker.get_result()
                if result is not None:
                    break
                time.sleep(0.05)

            assert result is not None
            assert result["mob_present"] is True
            mob_det.present.assert_called_with(fake_frame)
        finally:
            worker.stop()
            worker.join(timeout=2.0)

    def test_update_config_hot_swap(self):
        """update_config must swap detector mid-flight."""
        mob_det_old = MagicMock()
        mob_det_old.update = MagicMock()
        mob_det_old.in_hideout.return_value = True

        worker = AttackWorker(mob_det_old, use_memory=True)
        worker.start()

        try:
            # First submit with old detector (hideout)
            worker.submit(full_frame=None, foreground=True)
            result = None
            for _ in range(50):
                result = worker.get_result()
                if result is not None:
                    break
                time.sleep(0.05)
            assert result["in_hideout"] is True

            # Hot-swap to new detector (combat)
            mob_det_new = MagicMock()
            mob_det_new.update = MagicMock()
            mob_det_new.in_hideout.return_value = False
            mob_det_new.present.return_value = True
            worker.update_config(mob_det_new, zone_det=None, use_memory=True)

            worker.submit(full_frame=None, foreground=True)
            result = None
            for _ in range(50):
                result = worker.get_result()
                if result is not None:
                    break
                time.sleep(0.05)

            assert result is not None
            assert result["mob_present"] is True
            assert result["in_hideout"] is False
        finally:
            worker.stop()
            worker.join(timeout=2.0)

    def test_get_result_consume_once(self):
        """get_result() must return None on second call (consumed)."""
        mob_det = MagicMock()
        mob_det.update = MagicMock()
        mob_det.in_hideout.return_value = False
        mob_det.present.return_value = True

        worker = AttackWorker(mob_det, use_memory=True)
        worker.start()

        try:
            worker.submit(full_frame=None, foreground=True)
            for _ in range(50):
                r = worker.get_result()
                if r is not None:
                    break
                time.sleep(0.05)

            # Second call must return None
            assert worker.get_result() is None
        finally:
            worker.stop()
            worker.join(timeout=2.0)

    def test_worker_exception_doesnt_die(self):
        """Exception in detection must not kill the worker thread."""
        mob_det = MagicMock()
        mob_det.update.side_effect = RuntimeError("boom")

        worker = AttackWorker(mob_det, use_memory=True)
        worker.start()

        try:
            worker.submit(full_frame=None, foreground=True)
            time.sleep(0.3)

            # Worker must still be alive
            assert worker.is_alive(), "Worker died after exception"

            # Fix the error
            mob_det.update.side_effect = None
            mob_det.in_hideout.return_value = True
            worker.submit(full_frame=None, foreground=True)
            result = None
            for _ in range(50):
                result = worker.get_result()
                if result is not None:
                    break
                time.sleep(0.05)
            assert result is not None
        finally:
            worker.stop()
            worker.join(timeout=2.0)


class TestLootWorker:
    """LootWorker: submit frame → detect → get_result."""

    def test_submit_and_get_result(self):
        """Basic submit → detect → result cycle."""
        detector = MagicMock()
        detector.detect.return_value = ([[100, 200]], None)
        engine = MagicMock()
        engine.targets_in_radius.return_value = [(100, 200)]

        worker = LootWorker(detector, engine, pickup_mask=0x1000,
                            hold_ms=80, vx=MagicMock(), bridge=MagicMock())
        worker.start()

        try:
            fake_frame = MagicMock()
            worker.submit(fake_frame, (500, 400), foreground=True)

            result = None
            for _ in range(50):
                result = worker.get_result()
                if result is not None:
                    break
                time.sleep(0.05)

            assert result is not None, "LootWorker never returned result"
            points, in_radius = result
            assert len(points) == 1
            assert points[0] == [100, 200]
            detector.detect.assert_called_once()
            engine.targets_in_radius.assert_called_once()
        finally:
            worker.stop()
            worker.join(timeout=2.0)

    def test_update_config_hot_swap(self):
        """update_config must swap detector/engine mid-flight."""
        det_old = MagicMock()
        det_old.detect.return_value = ([[1, 1]], None)
        engine_old = MagicMock()
        engine_old.targets_in_radius.return_value = []

        worker = LootWorker(det_old, engine_old, pickup_mask=0x1000,
                            hold_ms=80, vx=MagicMock(), bridge=MagicMock())
        worker.start()

        try:
            worker.submit(MagicMock(), (500, 400), foreground=True)
            for _ in range(50):
                r = worker.get_result()
                if r is not None:
                    break
                time.sleep(0.05)

            det_new = MagicMock()
            det_new.detect.return_value = ([[99, 88]], None)
            engine_new = MagicMock()
            engine_new.targets_in_radius.return_value = [(99, 88)]
            worker.update_config(det_new, engine_new)

            worker.submit(MagicMock(), (500, 400), foreground=True)
            result = None
            for _ in range(50):
                result = worker.get_result()
                if result is not None:
                    break
                time.sleep(0.05)

            assert result is not None
            points, in_radius = result
            assert len(in_radius) == 1
        finally:
            worker.stop()
            worker.join(timeout=2.0)

    def test_worker_exception_doesnt_die(self):
        """Exception in detection must not kill LootWorker."""
        detector = MagicMock()
        detector.detect.side_effect = RuntimeError("detect boom")

        worker = LootWorker(detector, MagicMock(), pickup_mask=0x1000,
                            hold_ms=80, vx=MagicMock(), bridge=MagicMock())
        worker.start()

        try:
            worker.submit(MagicMock(), (500, 400), foreground=True)
            time.sleep(0.3)
            assert worker.is_alive(), "LootWorker died after exception"
        finally:
            worker.stop()
            worker.join(timeout=2.0)
