"""MonsterDB — SQLite телеметрия мобов для reader-режима.

Каждый кадр (20Hz) ридер видит всех мобов на карте. Этот модуль пишет полный
снимок в SQLite, чтобы потом анализировать:
  - дебаг классификации: «почему бот НЕ атаковал этого моба / что он видел»
    (полный снимок кадра со статусом каждого моба: COMBAT/GHOST_STATIC/...);
  - аналитика по карте: какие монстры водятся, у кого сколько HP, редкости,
    история по конкретному мобу (addr) — как менялся его HP со временем.

Архитектура (не тормозит бот на 20Hz):
  update() → record_frame() кладёт кадр в threading.Queue (O(1)).
  Фоновый daemon-поток _writer_loop() выгребает очередь пачками и делает
  одно executemany() в транзакции — запись батчами быстрее построчной.

CLI (аналитика):  python monster_db.py
  показывает сводку последней сессии: уникальные имена, распределение
  статусов, TOP по HP, историю по конкретному мобу.
"""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("poe2auto.monster_db")

_CLEANUP_INTERVAL_S = 3600.0  # автоочистка: раз в час удаляем старше keep_days

_SCHEMA = """
CREATE TABLE IF NOT EXISTS monster_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    ts          REAL    NOT NULL,
    area        TEXT,
    addr        INTEGER,
    name        TEXT,
    category    TEXT,
    rarity      TEXT,
    hp_cur      INTEGER,
    hp_max      INTEGER,
    hp_pct      REAL,
    grid_x      REAL,
    grid_y      REAL,
    dist        REAL,
    status      TEXT,
    hostile     INTEGER,
    is_boss     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_session ON monster_events(session_id);
CREATE INDEX IF NOT EXISTS idx_ts ON monster_events(ts);
CREATE INDEX IF NOT EXISTS idx_addr_session ON monster_events(addr, session_id);
CREATE INDEX IF NOT EXISTS idx_name_session ON monster_events(name, session_id);
"""


class MonsterDB:
    """SQLite телеметрия мобов с фоновым writer-потоком.

    Потокобезопасный: record_frame() можно вызывать из главного потока бота,
    запись идёт в daemon-потоке. close() досрочно сливает очередь и закрывает БД.
    """

    _SENTINEL = object()  # сигнал остановки writer-потоку

    def __init__(self, path: str, batch_size: int = 50,
                 flush_interval_s: float = 1.0, keep_days: float = 3.0):
        self.path = os.path.abspath(path)
        self.batch_size = max(1, int(batch_size))
        self.flush_interval_s = max(0.1, float(flush_interval_s))
        self.keep_days = max(0.0, float(keep_days))
        self._conn: Optional[sqlite3.Connection] = None
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._last_cleanup = 0.0
        self.session_id: str = ""

    def start(self, session_id: str) -> None:
        """Открыть БД, создать схему, запустить writer-поток."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False,
                                     isolation_level=None)  # autocommit
        self._conn.executescript(_SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.session_id = session_id
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._writer_loop, name="monster-db-writer", daemon=True)
        self._thread.start()
        _log.info("MonsterDB: %s (session=%s, keep_days=%.1f)",
                  self.path, session_id, self.keep_days)

    def record_frame(self, area: str,
                     entities: List[Tuple[Any, str]]) -> None:
        """Записать снимок кадра. Неблокирующий (кладёт в очередь).

        Args:
            area: код текущей зоны.
            entities: список (entity, status) — EntityData + результат
                      _classify_target. Берётся только нужное для БД.
        """
        if self._conn is None or self._stopping.is_set():
            return
        ts = time.time()
        rows = [self._entity_to_row(area, ent, status, ts)
                for ent, status in entities]
        if rows:
            self._queue.put(rows)

    @staticmethod
    def _entity_to_row(area: str, ent: Any, status: str, ts: float) -> tuple:
        """EntityData + статус → row для INSERT (без session_id — добавляется в _flush)."""
        path = ent.metadata or ""
        name = path.rsplit("/", 1)[-1].split("@", 1)[0] or path
        hp_pct = (ent.hp_current / ent.hp_max) if ent.hp_max > 0 else None

        def _name(val) -> str:
            return val.name if hasattr(val, "name") else str(val)

        return (
            ts,
            area,
            int(getattr(ent, "id", 0) or 0),
            name,
            _name(getattr(ent, "category", "")),
            _name(getattr(ent, "rarity", "")),
            int(getattr(ent, "hp_current", 0) or 0),
            int(getattr(ent, "hp_max", 0) or 0),
            hp_pct,
            float(getattr(ent, "grid_x", 0) or 0),
            float(getattr(ent, "grid_y", 0) or 0),
            float(getattr(ent, "distance", 0) or 0),
            status,
            0 if getattr(ent, "is_friendly", True) else 1,
            1 if getattr(ent, "is_boss", False) else 0,
        )

    def _writer_loop(self) -> None:
        """Фоновый поток: выгребает очередь пачками, пишет батчами в одной транзакции."""
        assert self._conn is not None
        buf: List[tuple] = []
        while True:
            try:
                item = self._queue.get(timeout=self.flush_interval_s)
            except queue.Empty:
                item = None
            if item is self._SENTINEL:
                break
            if item is not None:
                buf.extend(item)
            if (len(buf) >= self.batch_size
                    or (item is None and buf)):
                self._flush(buf[:])
                buf.clear()
            self._maybe_cleanup()
        if buf:  # досливаем остаток при остановке
            self._flush(buf)
        _log.debug("MonsterDB writer-поток завершился")

    def _flush(self, rows: List[tuple]) -> None:
        """Записать батч строк (одна транзакция). session_id подставляется."""
        assert self._conn is not None and self.session_id
        sid = self.session_id
        # вставляем session_id после ts (позиция 1 в row без него)
        full = [(None, sid) + r for r in rows]
        try:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            cur.executemany(
                "INSERT INTO monster_events "
                "(id, session_id, ts, area, addr, name, category, rarity, "
                "hp_cur, hp_max, hp_pct, grid_x, grid_y, dist, status, hostile, is_boss) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                full)
            cur.execute("COMMIT")
        except sqlite3.Error as e:
            _log.warning("MonsterDB write failed (%d rows): %s", len(full), e)
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass

    def _maybe_cleanup(self) -> None:
        """Периодически удалять записи старше keep_days."""
        if self.keep_days <= 0:
            return
        now = time.time()
        if now - self._last_cleanup < _CLEANUP_INTERVAL_S:
            return
        self._last_cleanup = now
        cutoff = now - self.keep_days * 86400
        try:
            assert self._conn is not None
            self._conn.execute("DELETE FROM monster_events WHERE ts < ?", (cutoff,))
            _log.debug("MonsterDB cleanup: удалены записи старше %.1f дн", self.keep_days)
        except sqlite3.Error as e:
            _log.warning("MonsterDB cleanup failed: %s", e)

    def flush(self) -> None:
        """Досрочно слить очередь (синхронно). Для тестов / аккуратного выхода."""
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not self._SENTINEL:
                self._flush(item if isinstance(item, list) else [item])

    def close(self) -> None:
        """Остановить writer-поток, дослать остаток, закрыть БД."""
        if self._thread is None:
            return
        self._stopping.set()
        self._queue.put(self._SENTINEL)
        self._thread.join(timeout=5.0)
        self._thread = None
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()
            self._conn = None
        _log.info("MonsterDB закрыта")

    # ──────────────────────────────────────────────
    # Query API (аналитика — read-only, не мешает writer)
    # ──────────────────────────────────────────────

    @staticmethod
    def _connect_ro(path: str) -> sqlite3.Connection:
        """Открыть read-only соединение для аналитики (не мешает writer)."""
        uri_path = path.replace("\\", "/").replace("?", "%3f")
        return sqlite3.connect("file:%s?mode=ro" % uri_path, uri=True)

    def get_unique_names(self, session_id: Optional[str] = None) -> List[Tuple[str, int]]:
        """Уникальные имена мобов + сколько кадров. [(name, count), ...] убыванию."""
        conn = self._connect_ro(self.path)
        try:
            if session_id:
                return conn.execute(
                    "SELECT name, COUNT(*) FROM monster_events "
                    "WHERE name != '' AND session_id = ? GROUP BY name ORDER BY 2 DESC",
                    (session_id,)).fetchall()
            return conn.execute(
                "SELECT name, COUNT(*) FROM monster_events "
                "WHERE name != '' GROUP BY name ORDER BY 2 DESC").fetchall()
        finally:
            conn.close()

    def get_status_distribution(self, session_id: Optional[str] = None) -> Dict[str, int]:
        """Распределение целей по статусам (COMBAT/DEAD/GHOST_STATIC/...)."""
        conn = self._connect_ro(self.path)
        try:
            q = "SELECT status, COUNT(*) FROM monster_events"
            if session_id:
                q += " WHERE session_id = ?"
            q += " GROUP BY status"
            rows = conn.execute(q, (session_id,) if session_id else ()).fetchall()
            return {st or "(none)": cnt for st, cnt in rows}
        finally:
            conn.close()

    def get_session_summary(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Сводка по сессии: время, кол-во кадров, зоны, топ-мобы, статусы."""
        conn = self._connect_ro(self.path)
        try:
            if session_id is None:
                row = conn.execute(
                    "SELECT session_id FROM monster_events ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if not row:
                    return {"empty": True}
                session_id = row[0]
            ts_range = conn.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM monster_events WHERE session_id = ?",
                (session_id,)).fetchone()
            if not ts_range or ts_range[2] == 0:
                return {"session_id": session_id, "empty": True}
            areas = conn.execute(
                "SELECT area, COUNT(*) FROM monster_events WHERE session_id = ? "
                "GROUP BY area ORDER BY 2 DESC LIMIT 5", (session_id,)).fetchall()
            top_mobs = conn.execute(
                "SELECT name, MAX(hp_max), AVG(hp_pct), COUNT(*) FROM monster_events "
                "WHERE session_id = ? AND name != '' GROUP BY name ORDER BY 4 DESC LIMIT 10",
                (session_id,)).fetchall()
            statuses = conn.execute(
                "SELECT status, COUNT(*) FROM monster_events WHERE session_id = ? "
                "GROUP BY status", (session_id,)).fetchall()
            return {
                "session_id": session_id,
                "ts_min": ts_range[0],
                "ts_max": ts_range[1],
                "frames": ts_range[2],
                "areas": areas,
                "top_mobs": top_mobs,
                "statuses": {st or "(none)": cnt for st, cnt in statuses},
            }
        finally:
            conn.close()


# ──────────────────────────────────────────────────────────────────────────
# CLI: python monster_db.py [db_path]  — сводка последней сессии
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "monster_telemetry.db")
    if not os.path.exists(db_path):
        print("БД не найдена: %s" % db_path)
        print("Запусти бота с monster_db.enabled: true — БД создастся сама.")
        sys.exit(1)
    db = MonsterDB(db_path)
    summary = db.get_session_summary()
    if summary.get("empty"):
        print("БД пуста (нет записей).")
        sys.exit(0)
    print("=" * 64)
    print("MonsterDB — сводка последней сессии")
    print("=" * 64)
    print("Сессия:    %s" % summary["session_id"])
    print("Кадров:    %d" % summary["frames"])
    print("Период:    %.0f сек" % (summary["ts_max"] - summary["ts_min"]))
    print("\n— Зоны (top 5) —")
    for area, cnt in summary["areas"]:
        print("  %-40s %d кадр." % ((area or "?")[:40], cnt))
    print("\n— Статусы целей —")
    for st, cnt in sorted(summary["statuses"].items(), key=lambda x: -x[1]):
        bar = "#" * min(40, cnt // max(1, summary["frames"] // 40))
        print("  %-14s %6d  %s" % (st, cnt, bar))
    print("\n— Топ мобов (по числу кадров, max HP, ср HP%) —")
    for name, max_hp, avg_pct, cnt in summary["top_mobs"]:
        hp_s = ("%d" % max_hp) if max_hp else "-"
        pct_s = ("%.0f%%" % (avg_pct * 100)) if avg_pct is not None else "-"
        print("  %-30s HPmax=%-7s avg=%-5s  %d кадр." % (
            name[:30], hp_s, pct_s, cnt))
    print("\nУникальных имён всего: %d" % len(db.get_unique_names()))



