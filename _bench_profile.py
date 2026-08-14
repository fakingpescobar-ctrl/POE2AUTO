"""Профиль каждого вызова внутри ReaderMobDetector.update() — ищем, что жрёт 400+ мс."""
import sys, time

sys.path.insert(0, r"C:\Projects\POE2AUTO")
from config import load_config
from reader_detect import ReaderMobDetector

cfg = load_config()
atk_cfg = cfg.get("auto_attack", {})
mob_det = ReaderMobDetector({**atk_cfg, "monster_db": cfg.get("monster_db", {})})
if not mob_det.connect():
    print("CONNECT FAIL"); sys.exit(1)
print("connected, pid=%d" % mob_det.bridge.pid)

for _ in range(3):
    mob_det.update()

N = 10
t = {k: [] for k in ("in_game", "game_state", "area_info", "read_raw",
                     "parse", "classify", "hostiles", "db_record")}

for i in range(N):
    t0 = time.perf_counter()
    mob_det.bridge.get_in_game()
    t["in_game"].append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    mob_det.bridge.get_game_state()
    t["game_state"].append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    area = mob_det.bridge.get_area_info()
    t["area_info"].append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    raw = mob_det.bridge.read_entities_raw()
    t["read_raw"].append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    ents = mob_det._parse_and_track(raw)   # единый проход: парсинг + треки
    t["parse"].append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    # треки «призраков»/снарядов теперь внутри _parse_and_track —
    # отдельного прохода нет (был _update_static_track)

    t0 = time.perf_counter()
    for e in ents:
        mob_det._classify_target(e)
    t["classify"].append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    h = mob_det.get_hostiles_in_range()
    t["hostiles"].append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    rows = [(e, "combat") for e in ents[:50]]
    mob_det.db.record_frame(area["name"] or "?", rows) if mob_det.db else None
    t["db_record"].append((time.perf_counter() - t0) * 1000)

def avg(a):
    return sum(a) / len(a)

print("\n=== ПРОФИЛЬ (avg по %d кадрам) ===" % N)
for k in t:
    print("  %-14s %8.2f ms" % (k, avg(t[k])))
print("\nсуммa основных: %.1f ms" % sum(avg(t[k]) for k in t))
print("hostile в радиусе: %d" % len(h))
