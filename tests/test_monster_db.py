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


# --- История моба (get_mob_history) ---

class TestMobHistory:
    def test_history_by_name(self, db):
        db.record_frame("A", [(_mob(eid=1, path="/Monsters/X/Runner", hp_cur=900), "combat")])
        db.record_frame("A", [(_mob(eid=1, path="/Monsters/X/Runner", hp_cur=300), "combat")])
        db.flush()
        time.sleep(0.3)
        hist = db.get_mob_history(name="Runner")
        assert len(hist) == 2
        assert hist[0]["name"] == "Runner"
        assert hist[0]["hp_cur"] == 900
        assert hist[1]["hp_cur"] == 300  # упорядочено по времени (убывание HP)

    def test_history_by_addr(self, db):
        db.record_frame("A", [(_mob(eid=12345, hp_cur=500), "combat")])
        db.flush()
        time.sleep(0.3)
        hist = db.get_mob_history(addr=12345)
        assert len(hist) == 1
        assert hist[0]["addr"] == 12345

    def test_history_empty_for_unknown(self, db):
        db.record_frame("A", [(_mob(path="/Monsters/Z"), "combat")])
        db.flush()
        time.sleep(0.3)
        hist = db.get_mob_history(name="Nonexistent")
        assert hist == []

    def test_history_has_status_and_dist(self, db):
        db.record_frame("A", [(_mob(distance=15.5), "ghost_static")])
        db.flush()
        time.sleep(0.3)
        hist = db.get_mob_history()
        assert hist[0]["status"] == "ghost_static"
        assert abs(hist[0]["dist"] - 15.5) < 0.01


# --- Детект убийств (get_kill_events) ---

class TestKillEvents:
    def test_kill_on_dead_status(self, db):
        # моб: combat → dead = убийство засчитано
        db.record_frame("A", [(_mob(eid=1, hp_cur=800), "combat")])
        db.record_frame("A", [(_mob(eid=1, hp_cur=0), "dead")])
        db.flush()
        time.sleep(0.3)
        kills = db.get_kill_events()
        assert len(kills) == 1
        assert kills[0]["name"] == "Test"

    def test_kill_on_disappear(self, db):
        # моб был combat, потом исчез (последняя запись combat, дальше нет) = убийство
        db.record_frame("A", [(_mob(eid=1, hp_cur=500), "combat")])
        db.flush()
        time.sleep(0.3)
        kills = db.get_kill_events()
        assert len(kills) == 1

    def test_no_kill_if_never_combat(self, db):
        # моб только ghost_static (бот не трогал) — НЕ убийство
        db.record_frame("A", [(_mob(eid=1), "ghost_static")])
        db.record_frame("A", [(_mob(eid=1, hp_cur=0), "dead")])
        db.flush()
        time.sleep(0.3)
        kills = db.get_kill_events()
        assert len(kills) == 0

    def test_kill_aggregates_by_name(self, db):
        # 3 разных моба (разные addr) одного имени — 3 убийства
        for eid in (10, 20, 30):
            db.record_frame("A", [(_mob(eid=eid), "combat")])
            db.record_frame("A", [(_mob(eid=eid, hp_cur=0), "dead")])
        db.flush()
        time.sleep(0.3)
        kills = db.get_kill_events()
        assert len(kills) == 3
        assert all(k["name"] == "Test" for k in kills)

    def test_kill_duration(self, db):
        # combat в t=0, dead в t=0.5 → duration ~0.5
        db.record_frame("A", [(_mob(eid=1), "combat")])
        time.sleep(0.5)
        db.record_frame("A", [(_mob(eid=1, hp_cur=0), "dead")])
        db.flush()
        time.sleep(0.3)
        kills = db.get_kill_events()
        assert kills[0]["duration_s"] >= 0.4  # допускаем погрешность таймера


# --- list_sessions ---

class TestSessions:
    def test_list_sessions(self, db):
        db.record_frame("A", [(_mob(), "combat")])
        db.flush()
        time.sleep(0.3)
        sessions = db.list_sessions()
        assert len(sessions) == 1
        assert sessions[0][0] == "test_session"
        assert sessions[0][3] == 1  # frames


