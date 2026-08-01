"""Тесты для MonsterDB — SQLite телеметрия мобов.

Проверяет запись/чтение/очистку/аналитику. БД в temp-файле, чистится после.
EntityData мокается простым объектом (не нужен весь reader_detect).
"""
import os
import time

import pytest

from entity_reader import EntityCategory, Rarity, EntityData
from monster_db import MonsterDB


# --- Helpers ---

def _mob(eid=1, hp_cur=800, hp_max=1000, alive=True, rarity=Rarity.NORMAL,
         is_boss=False, path="/Monsters/Test", friendly=False, distance=10.0,
         gx=5.0, gy=7.0):
    return EntityData(
        id=eid, category=EntityCategory.MONSTER, metadata=path,
        x=0, y=0, z=0, grid_x=gx, grid_y=gy,
        hp_current=hp_cur, hp_max=hp_max,
        hp_ratio=(hp_cur / hp_max) if hp_max > 0 else 1.0,
        rarity=rarity, is_friendly=friendly, distance=distance,
        is_boss=is_boss, is_alive=alive,
    )


@pytest.fixture
def db(tmp_path):
    """MonsterDB во временной БД, авто-закрытие после теста."""
    path = str(tmp_path / "test.db")
    d = MonsterDB(path, batch_size=2, flush_interval_s=0.1, keep_days=0)
    d.start("test_session")
    yield d
    d.close()


# --- Запись / чтение ---

class TestRecordRead:
    def test_record_and_count(self, db):
        db.record_frame("Area1", [(_mob(eid=1), "combat")])
        db.record_frame("Area1", [(_mob(eid=2), "dead")])
        db.flush()
        time.sleep(0.3)  # writer-поток мог не успеть
        summary = db.get_session_summary()
        assert not summary.get("empty")
        assert summary["frames"] == 2
        assert summary["session_id"] == "test_session"

    def test_status_distribution(self, db):
        db.record_frame("A", [(_mob(1), "combat"), (_mob(2), "ghost_static")])
        db.flush()
        time.sleep(0.3)
        dist = db.get_status_distribution()
        assert dist.get("combat") == 1
        assert dist.get("ghost_static") == 1

    def test_name_extracted_from_path(self, db):
        db.record_frame("A", [(_mob(path="/Monsters/Zombie/Fast"), "combat")])
        db.flush()
        time.sleep(0.3)
        names = db.get_unique_names()
        assert names[0][0] == "Fast"

    def test_name_with_at_suffix(self, db):
        db.record_frame("A", [(_mob(path="/Monsters/Shambler@2"), "combat")])
        db.flush()
        time.sleep(0.3)
        names = db.get_unique_names()
        assert names[0][0] == "Shambler"


# --- Поля HP / редкость ---

class TestFields:
    def test_hp_stored(self, db):
        db.record_frame("A", [(_mob(hp_cur=750, hp_max=1000), "combat")])
        db.flush()
        time.sleep(0.3)
        conn = MonsterDB._connect_ro(db.path)
        try:
            row = conn.execute(
                "SELECT hp_cur, hp_max, hp_pct FROM monster_events LIMIT 1").fetchone()
        finally:
            conn.close()
        assert row[0] == 750
        assert row[1] == 1000
        assert abs(row[2] - 0.75) < 0.001

    def test_rarity_stored_as_name(self, db):
        db.record_frame("A", [(_mob(rarity=Rarity.RARE), "combat")])
        db.flush()
        time.sleep(0.3)
        conn = MonsterDB._connect_ro(db.path)
        try:
            row = conn.execute("SELECT rarity FROM monster_events LIMIT 1").fetchone()
        finally:
            conn.close()
        assert row[0] == "RARE"

    def test_boss_flag(self, db):
        db.record_frame("A", [(_mob(is_boss=True), "combat")])
        db.flush()
        time.sleep(0.3)
        conn = MonsterDB._connect_ro(db.path)
        try:
            row = conn.execute("SELECT is_boss FROM monster_events LIMIT 1").fetchone()
        finally:
            conn.close()
        assert row[0] == 1


# --- Пустая БД / очистка ---

class TestEdgeCases:
    def test_empty_db_summary(self, tmp_path):
        d = MonsterDB(str(tmp_path / "empty.db"))
        d.start("s1")
        time.sleep(0.2)
        d.close()
        # get_session_summary на пустой БД — {empty: True}
        d2 = MonsterDB(str(tmp_path / "empty.db"))
        summary = d2.get_session_summary()
        assert summary.get("empty") is True

    def test_no_db_record_frame_is_safe(self):
        # record_frame без start() — не падает
        d = MonsterDB("nonexistent.db")
        d.record_frame("A", [(_mob(), "combat")])  # no-op, conn is None

    def test_keep_days_cleanup(self, tmp_path):
        # keep_days=0 в fixture → старые записи не чистятся принудительно, но
        # проверим что cleanup-вызов не падает. Запишем и вызовем close.
        d = MonsterDB(str(tmp_path / "c.db"), batch_size=1, keep_days=0.0001)
        d.start("s")
        d.record_frame("A", [(_mob(), "combat")])
        d.flush()
        time.sleep(0.3)
        d._maybe_cleanup()  # должен удалить (keep_days крошечный)
        d.close()


