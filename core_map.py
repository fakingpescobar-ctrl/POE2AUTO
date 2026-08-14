"""core_map.py - ядро маппинга (Core Mapping).

Отдельный пайплайн для бота: знает где бот, где препятствия (стены,
объекты, трупы), умеет строить маршрут в обход них (A*).

Источники данных (память PoE2):
- walkable grid (непроходимые клетки = стены/пропасти) из Terrain
- сущности зоны (объекты, трупы, мобы) как динамические препятствия

Маппинг координат:
    world (X, Y) -> grid (grid_x, grid_y) = (X / RATIO, Y / RATIO)
    grid -> world = (grid_x * RATIO, grid_y * RATIO)
"""
import sys, math, logging, heapq, os

sys.path.insert(0, r"C:\Projects\POE2AUTO")
logging.basicConfig(level=logging.CRITICAL)
from game_memory import GameMemory
import game_offsets as off
from probe_cast import find_player, components


class CoreMap:
    """Ядро маппинга: террейн + препятствия + путь."""

    RATIO = off.WORLD_TO_GRID_RATIO

    def __init__(self, mem: GameMemory):
        self.mem = mem
        self.area = 0
        self.width = 0
        self.height = 0
        self.walk = None            # bytearray 0/1, len = w*h
        self.loaded = False
        self.last_error = ""

    # ---------- загрузка ----------

    def load(self, gs: int) -> bool:
        """Загрузить террейн текущей зоны из памяти."""
        vec = self.mem.read_pointer(gs + off.GameState.CURRENT_STATE_PTR)
        ing = self.mem.read_pointer(vec) if vec else 0
        area = self.mem.read_pointer(ing + off.InGameState.AREA_INSTANCE) if ing else 0
        if not area:
            self.last_error = "no area"
            return False
        self.area = area
        td = self.mem.read_terrain_walkable(area)
        if not td:
            self.last_error = "no terrain"
            return False
        self.width, self.height, self.walk = td
        self.loaded = True
        return True

    # ---------- конверсия координат ----------

    def world_to_grid(self, wx: float, wy: float):
        """world (X,Y) -> grid (gx, gy)."""
        return wx / self.RATIO, wy / self.RATIO

    def grid_to_world(self, gx: float, gy: float):
        """grid -> world (X, Y)."""
        return gx * self.RATIO, gy * self.RATIO

    def grid_world_step(self):
        """Длина клетки в мировых единицах."""
        return self.RATIO

    # ---------- проходимость ----------

    def in_bounds(self, gx: int, gy: int) -> bool:
        return 0 <= gx < self.width and 0 <= gy < self.height

    def is_walkable(self, gx: int, gy: int) -> bool:
        """Walkable по сетке terrain (стены/пропасти)."""
        if not self.loaded or not self.in_bounds(gx, gy):
            return False
        return self.walk[gy * self.width + gx] == 1

    def is_walkable_world(self, wx: float, wy: float) -> bool:
        gx, gy = self.world_to_grid(wx, wy)
        return self.is_walkable(int(gx), int(gy))

    # ---------- A* путь ----------

    def find_path(self, start_w: tuple, goal_w: tuple, obstacles=None,
                  max_cells=40000, diagonal=True):
        """A* от world-точки до world-точки в обход препятствий.

        obstacles: dict {(gx, gy): True} — временные (трупы, объекты, мобы).
        Возвращает список waypoint-кортежей (wx, wy, gx, gy) или None.
        """
        if not self.loaded:
            return None
        s = (int(start_w[0] / self.RATIO), int(start_w[1] / self.RATIO))
        g = (int(goal_w[0] / self.RATIO), int(goal_w[1] / self.RATIO))
        if not self.is_walkable(*g):
            # цель на непроходимой клетке — ищем ближайшую проходимую
            g = self._nearest_walkable(g[0], g[1])
            if g is None:
                return None
        if s == g:
            return [(start_w[0], start_w[1], s[0], s[1])]
        obstacles = obstacles or {}

        def blocked(gx, gy):
            if not self.is_walkable(gx, gy):
                return True
            return (gx, gy) in obstacles

        open_heap = []
        start_id = s[0] + s[1] * self.width
        goal_id = g[0] + g[1] * self.width
        gscore = {start_id: 0.0}
        came = {}
        heapq.heappush(open_heap, (self._h(s, g), start_id))
        closed = set()
        dirs = self._dirs(diagonal)
        count = 0

        while open_heap and count < max_cells:
            f, cid = heapq.heappop(open_heap)
            if cid in closed:
                continue
            closed.add(cid)
            cx = cid % self.width
            cy = cid // self.width
            if cid == goal_id:
                return self._reconstruct(came, s, g)
            for ddx, ddy, cost in dirs:
                nx, ny = cx + ddx, cy + ddy
                if not self.in_bounds(nx, ny) or blocked(nx, ny):
                    continue
                nid = nx + ny * self.width
                if nid in closed:
                    continue
                ng = gscore[cid] + cost
                if ng < gscore.get(nid, float("inf")):
                    gscore[nid] = ng
                    came[nid] = cid
                    heapq.heappush(open_heap, (ng + self._h((nx, ny), g), nid))
            count += 1
        return None

    def _reconstruct(self, came, s, g):
        cid = g[0] + g[1] * self.width
        path = []
        while cid != s[0] + s[1] * self.width:
            x = cid % self.width
            y = cid // self.width
            path.append((x * self.RATIO, y * self.RATIO, x, y))
            cid = came.get(cid)
            if cid is None:
                break
        path.reverse()
        return path

    def _nearest_walkable(self, gx, gy, radius=40):
        best = None
        best_d = 1e18
        for r in range(0, radius + 1):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if abs(dx) != r and abs(dy) != r:
                        continue
                    nx, ny = gx + dx, gy + dy
                    if self.is_walkable(nx, ny):
                        d = dx * dx + dy * dy
                        if d < best_d:
                            best_d = d
                            best = (nx, ny)
            if best:
                return best
        return None

    @staticmethod
    def _h(a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    @staticmethod
    def _dirs(diagonal):
        d = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0)]
        if diagonal:
            d += [(-1, -1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (1, 1, 1.414)]
        return d

    # ---------- препятствия из сущностей ----------

    def collect_entity_obstacles(self, area: int, player_world=None,
                                 exclude_friendly=True, inflate=1,
                                 include: tuple = None, skip_metadata=None,
                                 exclude_npc=True):
        """Собрать динамические препятствия из сущностей зоны.

        Включает: трупы (is_corpse), объекты, мобы, боссов.
        NPC по умолчанию НЕ считаются препятствиями (exclude_npc=True) —
        это торговцы/квестовые, сквозь них/до них можно подходить.
        Позиции берём из Render.WORLD_POSITION (world X, Y).
        Каждая сущность "раздувается" на inflate клеток вокруг.

        return: (obstacles dict, entities list с полями)
        """
        from entity_reader import EntityReader, EntityCategory
        reader = EntityReader(self.mem)
        ents = reader.read_entities(area)
        obstacles = {}
        used = []
        ppos = None
        if player_world:
            ppos = (player_world[0], player_world[1])
        for e in ents:
            if e.metadata and e.metadata == ppos:
                continue
            if abs(e.x) < 0.01 and abs(e.y) < 0.01:
                continue
            if exclude_friendly and e.is_friendly:
                continue
            if exclude_npc and e.category == EntityCategory.NPC:
                continue
            if skip_metadata and e.metadata and skip_metadata in e.metadata:
                continue
            # пропускаем самого игрока (по позиции)
            if ppos and abs(e.x - ppos[0]) < 0.5 and abs(e.y - ppos[1]) < 0.5:
                continue
            gx, gy = int(e.x / self.RATIO), int(e.y / self.RATIO)
            if not self.in_bounds(gx, gy):
                continue
            is_player = (e.category == EntityCategory.PLAYER)
            if is_player:
                continue
            for dy in range(-inflate, inflate + 1):
                for dx in range(-inflate, inflate + 1):
                    nx, ny = gx + dx, gy + dy
                    if self.in_bounds(nx, ny) and self.is_walkable(nx, ny):
                        obstacles[(nx, ny)] = True
            used.append(e)
        return obstacles, used

    # ---------- статистика ----------

    def stats(self):
        total = self.width * self.height
        walkable = sum(self.walk) if self.walk else 0
        return {
            "size": (self.width, self.height),
            "cells": total,
            "walkable": walkable,
            "walkable_pct": 100.0 * walkable / total if total else 0.0,
        }

    # ---------- навигация ----------

    def move_along_path(self, path, player_render, max_time=60.0,
                        stop=0.5, dry=False, on_wp=None):
        """Идти по списку waypoint'ов ((wx,wy,gx,gy), ...).

        На каждом waypoint — точный подъезд (precision-подобный цикл),
        затем следующий. Возвращает True, если дошли до финала.
        dry=True — ничего не посылать на геймпад (только симуляция чтения).
        """
        from precision_move import post, focus_game
        t0 = time.time()
        for i, (wx, wy, gx, gy) in enumerate(path):
            while True:
                if time.time() - t0 > max_time:
                    print("[nav] time limit на waypoint %d" % i)
                    return False
                if not dry:
                    focus_game()
                ppos = self.mem.read_vec3(player_render + off.Render.WORLD_POSITION)
                if not ppos:
                    return False
                dx = wx - ppos[0]
                dy = wy - ppos[1]
                dist = math.hypot(dx, dy)
                if dist < stop:
                    break
                if dry:
                    continue
                force = int(min(32000, max(11000, 400.0 * dist)))
                from precision_move import stick_for
                sx, sy = stick_for(dx, dy, force)
                post("/stick?lx=%d&ly=%d" % (sx, sy))
                time.sleep(0.08)
                post("/stick?lx=0&ly=0")
                time.sleep(0.05)
            if on_wp:
                on_wp(i, wx, wy)
        if not dry:
            post("/stick?lx=0&ly=0")
        return True

    def render_ascii(self, center_world=None, radius=30, obstacles=None,
                     path=None, goal_world=None):
        """ASCII-карта окрестности вокруг точки (world). Возвращает строку.
        '#' - стена, '.' - проходимо, 'P' - игрок, 'o' - препятствие,
        'G' - цель, '*' - путь, ' ' - вне карты.
        """
        if not self.loaded:
            return "(no terrain)"
        cx = int(center_world[0] / self.RATIO) if center_world else self.width // 2
        cy = int(center_world[1] / self.RATIO) if center_world else self.height // 2
        obs = obstacles or {}
        pset = {}
        if path:
            pset = {(gx, gy) for wx, wy, gx, gy in path}
        lines = []
        for dy in range(radius, -radius - 1, -1):
            row = []
            for dx in range(-radius, radius + 1):
                gx, gy = cx + dx, cy + dy
                if not self.in_bounds(gx, gy):
                    row.append(" ")
                    continue
                if goal_world and (gx, gy) == (
                        int(goal_world[0] / self.RATIO), int(goal_world[1] / self.RATIO)):
                    row.append("G")
                elif center_world and (gx, gy) == (cx, cy):
                    row.append("P")
                elif (gx, gy) in pset:
                    row.append("*")
                elif (gx, gy) in obs:
                    row.append("o")
                elif self.is_walkable(gx, gy):
                    row.append(".")
                else:
                    row.append("#")
            lines.append("".join(row))
        return "\n".join(lines)
