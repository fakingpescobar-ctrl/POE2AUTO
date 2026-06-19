"""POE2AUTO — диагностика «потери HP» (v=0 / ratio=None), БЕЗ геймпада.

Анализатор: тот же захват орба и тот же HPDetector, что в bot.py, но ничего не
жмёт и не поднимает DualSense. Удобно гонять отдельно, когда геймпад не нужен.
Хочешь ловить улики ВЖИВУЮ (играя, с автофляской) — включи диагностику в самом
боте по F7, она использует этот же diag_dump.

Складывает улики в _debug/hp_events/ и пишет разбор в hp_diag.log
(см. diag_dump.dump_event — full/crop/mask/vis + причина).

Хоткеи: F9 — снять снимок прямо сейчас, F12 — выход.

Запуск:
    python hp_diag.py
"""
import logging
import os
import threading
import time

from pynput import keyboard

from config import load_config
from screen import ScreenCapture
from window import GameWindow
from hp_detector import HPDetector
from diag_dump import HPDiag, EVT_DIR

LOG_PATH = os.path.join(os.path.dirname(__file__), "hp_diag.log")

_fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_root.addHandler(_sh)
_fh = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
_fh.setFormatter(_fmt)
_root.addHandler(_fh)
log = logging.getLogger("poe2auto.hpdiag")


def main():
    cfg = load_config()
    hp_cfg = cfg["hp"]
    if not hp_cfg.get("enabled"):
        log.error("hp.enabled=false — сначала откалибруй орб (calibrate_hp.bat)")
        return 1

    win = GameWindow(cfg["game"]["window_title"])
    if win.find() and win.get_region():
        region = win.get_region()
        log.info("Окно PoE2 найдено: %s", region)
    else:
        region = GameWindow.primary_region()
        log.warning("Окно PoE2 не найдено — захват всего монитора %s", region)

    cap = ScreenCapture(cfg["capture"]["backend"])
    det = HPDetector(hp_cfg)  # полный кадр, как bot.py при F8=ON
    log.info("HP-детектор: порог=%.0f%% red_sat>=%d red_val>=%d valid_val>=%d min_valid=%.2f",
             det.threshold * 100, det.sat_min, det.val_min, det.valid_val, det.min_valid_ratio)
    log.info("Слежу за орбом. F9 — ручной снимок, F12 — выход. События пишу в %s", EVT_DIR)

    diag = HPDiag(enabled=True)  # анализатор всегда «включён»
    stop = threading.Event()
    manual = threading.Event()

    def on_press(key):
        name = getattr(key, "name", None)
        if name == "f12":
            stop.set()
            return False
        if name == "f9":
            manual.set()

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    last_status = 0.0
    iters = 0
    try:
        while not stop.is_set():
            foreground = win.is_foreground() if win.hwnd else True
            frame = cap.grab(region)
            if frame is None:
                time.sleep(0.02)
                continue

            m = det.measure(frame)
            now = time.perf_counter()
            diag.feed(frame, det, m, foreground, now)
            if manual.is_set():
                manual.clear()
                diag.manual(frame, det, m, foreground)

            iters += 1
            if now - last_status >= 1.0:
                fps = iters / (now - last_status)
                log.info("hp=%s v=%s trip=%s fg=%s lost=%s shots=%d fps=%.0f",
                         "-" if m["ratio"] is None else f"{m['ratio']*100:.0f}%", m.get("fill_v"),
                         m.get("trip_below"), foreground, diag.lost, diag.n, fps)
                last_status = now
                iters = 0

            time.sleep(0.01)
    except KeyboardInterrupt:
        log.info("Прервано (Ctrl+C)")
    finally:
        listener.stop()
    log.info("Готово. Снимков: %d. Лог: %s", diag.n, LOG_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
