"""cProfile полного update() — топ-функции по времени."""
import sys, cProfile, pstats, io

sys.path.insert(0, r"C:\Projects\POE2AUTO")
from config import load_config
from reader_detect import ReaderMobDetector

cfg = load_config()
mob_det = ReaderMobDetector({**cfg.get("auto_attack", {}), "monster_db": cfg.get("monster_db", {})})
if not mob_det.connect():
    print("CONNECT FAIL"); sys.exit(1)
print("connected")

for _ in range(3):
    mob_det.update()

pr = cProfile.Profile()
pr.enable()
for _ in range(5):
    mob_det._last_update = 0.0  # обойти throttle — измерить реальную работу
    mob_det.update()
pr.disable()

s = io.StringIO()
ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
ps.print_stats(20)
print(s.getvalue())
