"""Бенчмарк полного цикла ReaderMobDetector.update() на живой игре.

Запуск:  anaconda3 python _bench_update.py  (pythonnet нужен!)
"""
import sys, time

sys.path.insert(0, r"C:\Projects\POE2AUTO")
from config import load_config
from reader_detect import ReaderMobDetector

cfg = load_config()
atk_cfg = cfg.get("auto_attack", {})
mob_det = ReaderMobDetector({
    **atk_cfg,
    "monster_db": cfg.get("monster_db", {}),
})

if not mob_det.connect():
    print("CONNECT FAIL")
    sys.exit(1)
print("connected, pid=%d  attack_range=%.0f" % (mob_det.bridge.pid, mob_det.attack_range))

# прогрев: 3 кадра
for _ in range(3):
    mob_det.update()

N = 30
t_rd, t_parse, t_total = [], [], []
n_mon = n_host = n_tot = 0

for i in range(N):
    t0 = time.perf_counter()
    raw = mob_det.bridge.read_entities_raw()
    t1 = time.perf_counter()
    mob_det._parse_and_track(raw)   # единый проход: парсинг + треки
    t2 = time.perf_counter()
    mob_det.update()   # полный цикл (throttle может пропустить часть, но bridge уже тёплый)
    t3 = time.perf_counter()

    t_rd.append((t1 - t0) * 1000)
    t_parse.append((t2 - t1) * 1000)
    t_total.append((t3 - t0) * 1000)

    n_tot = len(raw)
    n_mon = sum(1 for e in raw if e.get("entity_type") == "m")
    n_host = len(mob_det.get_hostiles_in_range())

def avg(a):
    return sum(a) / len(a)

print("\n=== РЕЗУЛЬТАТ (N=%d кадров) ===" % N)
print("сущностей всего:   %d" % n_tot)
print("монстров (raw 'm'): %d" % n_mon)
print("hostile в радиусе: %d" % n_host)
print()
print("чтение памяти:     %.1f ms" % avg(t_rd))
print("парсинг:           %.1f ms" % avg(t_parse))
print("полный update():   %.1f ms  (=> %.1f Гц)" % (avg(t_total), 1000 / avg(t_total)))
print("макс полный:       %.1f ms" % max(t_total))
print("throttle 50 ms: %s" % ("УСПЕВАЕТ (20 Гц достижимо)" if avg(t_total) < 50 else "НЕ УСПЕВАЕТ — реально ниже 20 Гц"))
