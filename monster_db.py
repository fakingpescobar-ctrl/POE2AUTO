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
_WAL_CHECKPOINT_INTERVAL_S = 600.0  # WAL checkpoint: каждые 10 минут

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
-- Kill-cooldown запрос: SELECT DISTINCT addr WHERE session_id=? AND status='dead'
-- AND ts>=? AND EXISTS(... combat ...). Без этого индекса SQLite сканирует ВСЮ
-- сессию (сотни тысяч строк) на КАЖДЫЙ вызов get_recently_killed_addrs — ридер
-- вставал на 3 сек при раздутой БД (1.1M строк).
CREATE INDEX IF NOT EXISTS idx_session_status_ts
    ON monster_events(session_id, status, ts);
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
        self._last_wal_checkpoint = 0.0
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
        """Записать батч строк. session_id подставляется. (autocommit-режим:
        executemany сам коммитит, ручные BEGIN/COMMIT не нужны.)"""
        assert self._conn is not None and self.session_id
        sid = self.session_id
        full = [(None, sid) + r for r in rows]
        try:
            self._conn.executemany(
                "INSERT INTO monster_events "
                "(id, session_id, ts, area, addr, name, category, rarity, "
                "hp_cur, hp_max, hp_pct, grid_x, grid_y, dist, status, hostile, is_boss) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                full)
        except sqlite3.Error as e:
            _log.warning("MonsterDB write failed (%d rows): %s", len(full), e)

    def _maybe_cleanup(self) -> None:
        """Периодически удалять записи старше keep_days и сжимать файл БД.

        DELETE оставляет в файле «дыры» — размер БД не уменьшается, пока не
        выполнить VACUUM (проверено: 462 МБ после сессий с purge). VACUUM
        дорогой (пересборка всей БД), поэтому бежим только если реально
        удалили записи, и не чаще раза в час. В WAL/autocommit режиме безопасен:
        writer-поток — единственный пишущий, транзакции не открыты.

        WAL CHECKPOINT: каждые 10 минут делаем TRUNCATE, чтобы WAL файл не рос.
        """
        if self.keep_days <= 0:
            return
        now = time.time()
        
        # WAL checkpoint каждые 10 минут (отдельно от cleanup)
        if now - self._last_wal_checkpoint >= _WAL_CHECKPOINT_INTERVAL_S:
            self._last_wal_checkpoint = now
            try:
                assert self._conn is not None
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                _log.debug("MonsterDB WAL checkpoint выполнен")
            except sqlite3.Error as wce:
                _log.warning("MonsterDB WAL checkpoint failed: %s", wce)
        
        # Cleanup (удаление старых записей + VACUUM)
        if now - self._last_cleanup < _CLEANUP_INTERVAL_S:
            return
        self._last_cleanup = now
        cutoff = now - self.keep_days * 86400
        try:
            assert self._conn is not None
            before = self._conn.total_changes
            self._conn.execute("DELETE FROM monster_events WHERE ts < ?", (cutoff,))
            deleted = self._conn.total_changes - before
            if deleted:
                _log.debug("MonsterDB cleanup: удалены записи старше %.1f дн "
                           "(%d строк)", self.keep_days, deleted)
                try:
                    self._conn.execute("VACUUM")
                    _log.debug("MonsterDB cleanup: VACUUM выполнен, файл сжат")
                except sqlite3.Error as ve:
                    _log.warning("MonsterDB VACUUM failed (повтор через час): %s", ve)
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

    def get_mob_history(self, name: Optional[str] = None,
                        addr: Optional[int] = None,
                        session_id: Optional[str] = None,
                        limit: int = 5000) -> List[Dict[str, Any]]:
        """История одного моба: как менялись HP/статус/позиция со временем.

        Фильтр по name (все мобы с этим именем) ИЛИ addr (один конкретный моб).
        Возвращает список словарей {ts, addr, name, hp_cur, hp_max, hp_pct,
        status, dist, grid_x, grid_y} отсортированных по времени.

        Применение: «почему этот моб умер», «как падал HP босса», «когда моб
        стал GHOST_STATIC и бот перестал его атаковать».
        """
        conn = self._connect_ro(self.path)
        try:
            sid = session_id
            if sid is None:
                row = conn.execute(
                    "SELECT session_id FROM monster_events ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if not row:
                    return []
                sid = row[0]
            clauses = ["session_id = ?"]
            params: list = [sid]
            if name is not None:
                clauses.append("name = ?")
                params.append(name)
            if addr is not None:
                clauses.append("addr = ?")
                params.append(int(addr))
            where = " AND ".join(clauses)
            rows = conn.execute(
                "SELECT ts, addr, name, hp_cur, hp_max, hp_pct, status, dist, "
                "grid_x, grid_y FROM monster_events WHERE %s "
                "ORDER BY ts ASC LIMIT ?" % where,
                params + [int(limit)],
            ).fetchall()
            return [{"ts": r[0], "addr": r[1], "name": r[2], "hp_cur": r[3],
                     "hp_max": r[4], "hp_pct": r[5], "status": r[6],
                     "dist": r[7], "grid_x": r[8], "grid_y": r[9]} for r in rows]
        finally:
            conn.close()

    def get_kill_events(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Детект убийств: кого бот убил в этой сессии.

        «Убийство» = моб, который был в COMBAT (бот его атаковал), а затем:
          - исчез из записей (умер, сущность выгружена), ИЛИ
          - его статус стал DEAD (Life+0x10).
        Уникальные вхождения мобов, которые НИ РАЗУ не были COMBAT (бот их
        не трогал), не считаются убийствами — они могли умереть от окружения
        или просто деспавниться.

        Возвращает список {addr, name, first_combat_ts, kill_ts, duration_s,
        hp_start, hp_max} отсортированный по времени убийства.
        """
        conn = self._connect_ro(self.path)
        try:
            sid = session_id
            if sid is None:
                row = conn.execute(
                    "SELECT session_id FROM monster_events ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if not row:
                    return []
                sid = row[0]
            # Берём весь таймлайн по addr в сессии, группируем в Python
            # (детект перехода COMBAT→конец — это state-machine, удобнее кодом).
            rows = conn.execute(
                "SELECT addr, ts, name, status, hp_cur, hp_max FROM monster_events "
                "WHERE session_id = ? AND addr > 0 ORDER BY addr, ts ASC",
                (sid,)).fetchall()
        finally:
            conn.close()

        # Группируем по addr, сохраняя порядок времени
        per_addr: Dict[int, list] = {}
        for r in rows:
            per_addr.setdefault(r[0], []).append(r)

        kills = []
        for addr, events in per_addr.items():
            ever_combat = any(e[3] == "combat" for e in events)
            if not ever_combat:
                continue  # бот не трогал — не наше убийство
            first = None
            first_combat = None
            for e in events:
                if first is None:
                    first = e
                if first_combat is None and e[3] == "combat":
                    first_combat = e
            last = events[-1]
            # Убийство = последний статус DEAD, ИЛИ моб исчез (последняя запись
            # была COMBAT, дальше ничего нет — умер и выгрузился).
            died = (last[3] == "dead"
                    or (last[3] == "combat"))
            if not died:
                continue
            fc_ts = first_combat[1] if first_combat else (first[1] if first else 0)
            kills.append({
                "addr": addr,
                "name": last[2] or (first[2] if first else ""),
                "first_combat_ts": fc_ts,
                "kill_ts": last[1],
                "duration_s": max(0.0, last[1] - fc_ts),
                "hp_start": first_combat[4] if first_combat else 0,
                "hp_max": first_combat[5] if first_combat else 0,
            })
        kills.sort(key=lambda k: k["kill_ts"])
        return kills

    def get_recently_killed_addrs(self, session_id: Optional[str] = None,
                                    cooldown_s: float = 10.0) -> set:
        """Адреса мобов, которых бот убил в последние cooldown_s секунд.

        «Убийство» = моб был в COMBAT (бот атаковал), а затем
        статус стал DEAD или моб исчез из записи.
        Используется для kill cooldown: не атаковать недавно убитых
        (дроп ещё не поднят, анимация смерти).

        Returns:
            Множество addr мобов, убитых недавно.
        """
        conn = self._connect_ro(self.path)
        try:
            sid = session_id
            if sid is None:
                row = conn.execute(
                    "SELECT session_id FROM monster_events "
                    "ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if not row:
                    return set()
                sid = row[0]
            # ВАЖНО: ts пишется как time.time() (Unix-эпоха, см. record_frame).
            # Раньше тут был time.perf_counter() — монотонный счётчик с маленькими
            # значениями, условие ts >= cutoff выполнялось для ВСЕХ строк сессии,
            # и cooldown превращался в «вечный». Теперь часы согласованы.
            now = time.time()
            cutoff = now - cooldown_s
            # ВАЖНО: cooldown только для мобов, которые РЕАЛЬНО атаковались
            # (были COMBAT) в этой сессии и ПОТОМ стали dead. Раньше брался
            # любой status='dead' — а record_frame пишет текущую классификацию
            # каждый кадр. Моб, однажды увиденный dead (исчез из кадра / был
            # убит кем-то), застревал в cooldown НАВСЕГДА: dead-строка
            # продлевала cooldown, cooldown возвращал DEAD, DEAD снова писался.
            # Пример: VaalBloodPriestFemale hp 8315/12281 (живой!) числился
            # dead минутами. Требуем наличие combat-строки ДО dead.
            rows = conn.execute(
                "SELECT DISTINCT d.addr FROM monster_events d "
                "WHERE d.session_id = ? AND d.status = 'dead' AND d.ts >= ? "
                "AND EXISTS (SELECT 1 FROM monster_events c "
                "            WHERE c.session_id = d.session_id "
                "              AND c.addr = d.addr AND c.status = 'combat' "
                "              AND c.ts >= ? AND c.ts <= d.ts)",
                (sid, cutoff, cutoff)).fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows if r[0]}

    def list_sessions(self) -> List[Tuple[str, float, float, int]]:
        """Все сессии в БД: [(session_id, ts_min, ts_max, frames), ...] по убыванию."""
        conn = self._connect_ro(self.path)
        try:
            return conn.execute(
                "SELECT session_id, MIN(ts), MAX(ts), COUNT(*) "
                "FROM monster_events GROUP BY session_id ORDER BY 3 DESC"
            ).fetchall()
        finally:
            conn.close()

    @staticmethod
    def force_cleanup(path: str, keep_days: float = 3.0) -> dict:
        """Принудительная очистка БД (вызывать когда бот НЕ запущен).

        1. Удаляет записи старше keep_days
        2. Делает WAL checkpoint (TRUNCATE)
        3. Делает VACUUM (сжатие файла)

        Returns:
            {"deleted": int, "before_bytes": int, "after_bytes": int}
        """
        result = {"deleted": 0, "before_bytes": 0, "after_bytes": 0}
        try:
            result["before_bytes"] = os.path.getsize(path)
        except OSError:
            pass
        
        conn = sqlite3.connect(path)
        try:
            # Удаляем старые записи
            cutoff = time.time() - keep_days * 86400
            before = conn.total_changes
            conn.execute("DELETE FROM monster_events WHERE ts < ?", (cutoff,))
            result["deleted"] = conn.total_changes - before
            
            # WAL checkpoint
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            
            # VACUUM
            if result["deleted"] > 0:
                conn.execute("VACUUM")
            
            _log.info("MonsterDB force cleanup: удалено %d записей", result["deleted"])
        finally:
            conn.close()
        
        try:
            result["after_bytes"] = os.path.getsize(path)
        except OSError:
            pass
        
        return result



# ──────────────────────────────────────────────────────────────────────────
# CLI:
#   python monster_db.py                    — сводка последней сессии
#   python monster_db.py --name Shambler    — HP-кривая моба(ов) по имени
#   python monster_db.py --addr 0x1A2B      — история конкретного моба по адресу
#   python monster_db.py --kills            — кого бот убил (детект убийств)
#   python monster_db.py --sessions         — список всех сессий
#   python monster_db.py --session ID       — сводка конкретной сессии
#   python monster_db.py --cleanup          — принудительная очистка (бот НЕ запущен!)
#   (опц. позиционный аргумент = путь к БД)
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from datetime import datetime as _dt

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")

    opts = {"db": None, "name": None, "addr": None, "kills": False,
            "sessions": False, "session": None, "cleanup": False}
    rest = []
    i = 0
    argv = sys.argv[1:]
    while i < len(argv):
        a = argv[i]
        if a == "--name" and i + 1 < len(argv):
            opts["name"] = argv[i + 1]; i += 2
        elif a == "--addr" and i + 1 < len(argv):
            opts["addr"] = int(argv[i + 1], 0); i += 2  # 0x.. или dec
        elif a == "--session" and i + 1 < len(argv):
            opts["session"] = argv[i + 1]; i += 2
        elif a == "--kills":
            opts["kills"] = True; i += 1
        elif a == "--sessions":
            opts["sessions"] = True; i += 1
        elif a == "--cleanup":
            opts["cleanup"] = True; i += 1
        else:
            rest.append(a); i += 1
    if rest:
        opts["db"] = rest[0]

    def _t(ts):
        return _dt.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"

    db_path = opts["db"] or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "monster_telemetry.db")
    
    # Принудительная очистка (бот НЕ должен быть запущен!)
    if opts["cleanup"]:
        if not os.path.exists(db_path):
            print("БД не найдена: %s" % db_path); sys.exit(1)
        size_before = os.path.getsize(db_path)
        print("Очистка БД: %s" % db_path)
        print("Размер ДО: %.1f МБ" % (size_before / 1024 / 1024))
        result = MonsterDB.force_cleanup(db_path, keep_days=3.0)
        size_after = os.path.getsize(db_path)
        print("Удалено записей: %d" % result["deleted"])
        print("Размер ПОСЛЕ: %.1f МБ" % (size_after / 1024 / 1024))
        print("Освобождено: %.1f МБ" % ((size_before - size_after) / 1024 / 1024))
        sys.exit(0)
    if not os.path.exists(db_path):
        print("БД не найдена: %s" % db_path)
        print("Запусти бота с monster_db.enabled: true — БД создастся сама.")
        sys.exit(1)
    db = MonsterDB(db_path)

    if opts["sessions"]:
        sessions = db.list_sessions()
        if not sessions:
            print("БД пуста (нет сессий)."); sys.exit(0)
        print("%-22s %-10s %-10s %8s" % ("СЕССИЯ", "НАЧАЛО", "КОНЕЦ", "КАДРОВ"))
        print("-" * 54)
        for sid, tmin, tmax, n in sessions:
            print("%-22s %-10s %-10s %8d" % (sid, _t(tmin), _t(tmax), n))
        sys.exit(0)

    if opts["kills"]:
        kills = db.get_kill_events(opts["session"])
        if not kills:
            print("Убийств не зафиксировано в этой сессии."); sys.exit(0)
        by_name = {}
        for k in kills:
            by_name.setdefault(k["name"], []).append(k)
        print("=" * 60)
        print("Детект убийств (%d шт.)" % len(kills))
        print("=" * 60)
        print("%-28s %5s %8s %8s" % ("ИМЯ", "КОЛ.", "СРЕД.с", "СРЕД.HP"))
        print("-" * 60)
        for nm in sorted(by_name, key=lambda x: -len(by_name[x])):
            ks = by_name[nm]
            print("%-28s %5d %7.1fc %8d" % (
                nm[:28], len(ks),
                sum(k["duration_s"] for k in ks) / len(ks),
                sum(k["hp_max"] for k in ks) // len(ks)))
        sys.exit(0)

    if opts["name"] is not None or opts["addr"] is not None:
        hist = db.get_mob_history(name=opts["name"], addr=opts["addr"],
                                  session_id=opts["session"])
        if not hist:
            who = ("name=%s" % opts["name"]) if opts["name"] else ("addr=0x%X" % opts["addr"])
            print("Записей не найдено (%s)." % who); sys.exit(0)
        print("=" * 72)
        print("История моба: %s  (%d записей)" % (hist[0]["name"], len(hist)))
        print("=" * 72)
        step = max(1, len(hist) // 50)
        print("%-9s %-9s %8s %8s %8s %8s" % ("ВРЕМЯ", "СТАТУС", "ADDR", "HP", "MAX", "ДИСТ"))
        print("-" * 72)
        for h in hist[::step]:
            hp = h["hp_cur"] if h["hp_cur"] else "-"
            mx = h["hp_max"] if h["hp_max"] else "-"
            print("%-9s %-9s %8s %8s %8s %8.1f" % (
                _t(h["ts"]), (h["status"] or "?")[:9],
                ("0x%X" % h["addr"]) if h["addr"] else "-", hp, mx, h["dist"]))
        sys.exit(0)

    summary = db.get_session_summary(opts["session"])
    if summary.get("empty"):
        print("БД пуста (нет записей)."); sys.exit(0)
    print("=" * 64)
    print("MonsterDB — сводка сессии")
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
    kills = db.get_kill_events(summary["session_id"])
    print("\n— Убийств детектировано: %d" % len(kills))
    print("\nУникальных имён всего: %d" % len(db.get_unique_names()))



