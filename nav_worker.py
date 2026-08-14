"""nav_worker.py - поток навигации для бота (Core Mapping pipeline).

Берёт цель из файла nav_target.json (отдельный пайплайн может писать туда
координаты, напр. телеграм-бот или скрипт). Строит путь через CoreMap
(find_path + препятствия) и ведёт персонажа по waypoint'ам через
виртуальный геймпад (vx.set_left_stick).

Формат nav_target.json:
    {"wx": 3690.2, "wy": 3755.4, "active": true}
    "active": false -> стоп навигации

Использование из bot.py:
    nav = NavWorker(vx)
    nav.start()
    ... в цикле: nav.tick(gs)  # читает файл цели, двигает
"""
import json
import logging
import os
import threading
import time

import game_offsets as off

log = logging.getLogger("poe2auto.nav")

TARGET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nav_target.json")


class NavWorker(threading.Thread):
    """Поток навигации. Сам строит путь и двигает персонажа."""

    def __init__(self, vx, mem=None, cfg=None):
        super().__init__(daemon=True)
        self.vx = vx
        self.mem = mem          # GameMemory (если не передан — свой)
        self._mem_own = mem is None
        self.cfg = cfg or {}
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._target = None     # (wx, wy)
        self._active = False
        self.status = "idle"    # idle | loading | planning | moving | reached | error
        self.last_error = ""
        self.path_len = 0
        self.waypoint_idx = 0
        self.stats = {}

    # ---------- API ----------

    def set_target(self, wx, wy):
        with self._lock:
            self._target = (float(wx), float(wy))
            self._active = True
        log.info("Nav: цель установлена (%.1f, %.1f)", wx, wy)

    def clear_target(self):
        with self._lock:
            self._target = None
            self._active = False
        self._release_stick()
        log.info("Nav: цель сброшена")

    def is_active(self):
        with self._lock:
            return self._active

    def get_status(self):
        with self._lock:
            return {
                "status": self.status,
                "target": self._target,
                "path_len": self.path_len,
                "wp_idx": self.waypoint_idx,
                "error": self.last_error,
                **self.stats,
            }

    def stop(self):
        self.stop_event.set()
        self._release_stick()

    def _release_stick(self):
        try:
            self.vx.set_left_stick(0, 0)
        except Exception:
            pass

    # ---------- файл-цель ----------

    def _read_target_file(self):
        try:
            if not os.path.exists(TARGET_FILE):
                return None
            with open(TARGET_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not data.get("active"):
                return None
            wx = float(data.get("wx"))
            wy = float(data.get("wy"))
            return (wx, wy)
        except Exception as e:
            log.debug("Nav: ошибка чтения %s: %s", TARGET_FILE, e)
            return None

    # ---------- главный цикл ----------

    def run(self):
        if self._mem_own:
            from game_memory import GameMemory
            self.mem = GameMemory()
            if not self.mem.connect():
                log.error("Nav: не удалось подключиться к памяти")
                self.status = "error"
                self.last_error = "no memory"
                return
        try:
            while not self.stop_event.is_set():
                # читаем цель из файла (внешний пайплайн)
                ftarget = self._read_target_file()
                if ftarget is not None and not self.is_active():
                    self.set_target(*ftarget)
                if self.is_active() and ftarget is None:
                    # файл снял active -> стоп
                    with self._lock:
                        self._active = False
                        self._target = None
                    self.status = "idle"
                    self._release_stick()

                if self.is_active():
                    self._navigate()
                else:
                    time.sleep(0.2)
        finally:
            self._release_stick()
            if self._mem_own:
                try:
                    self.mem.disconnect()
                except Exception:
                    pass

    # ---------- навигация ----------

    def _navigate(self):
        from game_memory import GameMemory
        from core_map import CoreMap
        from probe_cast import find_player, components

        with self._lock:
            target = self._target
        if target is None:
            return

        mem = self.mem
        try:
            player, slot, gs = find_player(mem)
            if not player:
                self.status = "error"
                self.last_error = "no player"
                return
            comps = components(mem, player)
            ren = comps.get("Render")
            if not ren:
                self.status = "error"
                self.last_error = "no Render"
                return
            ppos = mem.read_vec3(ren + off.Render.WORLD_POSITION)
            if not ppos:
                return

            # собираем препятствия + строим путь
            cm = CoreMap(mem)
            if not cm.load(gs):
                self.status = "error"
                self.last_error = cm.last_error
                return
            self.stats["terrain"] = cm.stats()

            obs, ents = {}, []
            try:
                obs, ents = cm.collect_entity_obstacles(
                    cm.area, player_world=(ppos[0], ppos[1]),
                    inflate=int(self.cfg.get("obstacle_inflate", 1)))
            except Exception as e:
                log.debug("Nav: нет препятствий (%s)", e)
            self.stats["obstacles"] = len(obs)
            self.stats["entities"] = len(ents)

            path = cm.find_path((ppos[0], ppos[1]), target, obstacles=obs)
            if not path:
                self.status = "error"
                self.last_error = "no path"
                return

            self.status = "moving"
            self.path_len = len(path)
            self.waypoint_idx = 0

            # идём по waypoint'ам
            for i, (wx, wy, gx, gy) in enumerate(path):
                if self.stop_event.is_set():
                    break
                self.waypoint_idx = i
                ok = self._move_to_wp(mem, ren, wx, wy)
                if not ok:
                    self.status = "error"
                    self.last_error = "wp %d" % i
                    break
            else:
                self.status = "reached"
                with self._lock:
                    self._active = False
                    self._target = None
                self._release_stick()
                log.info("Nav: ДОШЛИ (%.1f, %.1f)", target[0], target[1])
        except Exception as e:
            self.status = "error"
            self.last_error = str(e)
            log.debug("Nav: ошибка навигации: %s", e, exc_info=True)
        finally:
            self._release_stick()

    def _move_to_wp(self, mem, ren, wx, wy, stop=None, max_sec=None):
        """Адаптивное движение к waypoint (как precision_move, но через vx)."""
        from precision_move import focus_game, stick_for
        stop = stop if stop is not None else float(self.cfg.get("stop_dist", 0.5))
        max_sec = max_sec if max_sec is not None else float(self.cfg.get("wp_max_sec", 20.0))
        t0 = time.time()
        dur = 0.05
        speed = 0.0
        speed_valid = False
        while time.time() - t0 < max_sec:
            if self.stop_event.is_set():
                return False
            focus_game()
            ppos = mem.read_vec3(ren + off.Render.WORLD_POSITION)
            if not ppos:
                return False
            dx = wx - ppos[0]
            dy = wy - ppos[1]
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < stop:
                return True
            force = int(min(32000, max(11000, 400.0 * dist)))
            sx, sy = stick_for(dx, dy, force)
            # vx использует -1..1
            self.vx.set_left_stick(sx / 32767.0, sy / 32767.0)
            time.sleep(dur)
            self.vx.set_left_stick(0, 0)

            p2 = mem.read_vec3(ren + off.Render.WORLD_POSITION)
            moved = ((p2[0] - ppos[0]) ** 2 + (p2[1] - ppos[1]) ** 2) ** 0.5
            ndist = ((wx - p2[0]) ** 2 + (wy - p2[1]) ** 2) ** 0.5
            if moved > 0.02:
                inst = moved / dur if dur > 0 else 0
                speed = inst if not speed_valid else 0.6 * speed + 0.4 * inst
                speed_valid = True
            if moved < 0.05:
                dur = min(dur * 1.5, 0.15)
            elif ndist >= dist - 0.02:
                dur *= 0.5
            else:
                target_step = 0.5 * dist
                want = max(0.015, min(0.15, target_step / speed)) if (speed_valid and speed > 0) else dur
                dur = 0.6 * dur + 0.4 * want
            dur = max(0.015, min(0.15, dur))
            self.stats["dur"] = dur
            self.stats["speed"] = speed
            self.stats["dist"] = dist
        return False
