"""Brute-поиск оффсетов (слои L3/L4) — окна вокруг baseline с валидацией ВАМ.

L3  find_area      — InGameState → AreaInstance (окно ±0x80, шаг 8)
L4a find_player    — AreaInstance → LocalPlayer (окно ±0x400, шаг 8, V1)
L4b find_field_*   — одиночные поля: камера(V2), asleep(V3), инфо зоны(V4), терраин(V5)

Все кандидаты собираются с трассами; возвращается лучший (score, обязательный V1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from chain_reader import Chain, ChainReader


@dataclass
class Candidate:
    name: str
    offset: int
    score: int
    passed: list = field(default_factory=list)
    detail: str = ""


class Brute:
    def __init__(self, cr: ChainReader, val: "Validators"):
        self.cr = cr
        self.val = val

    # ── L1/L2: IGS по States[] (архитектурно — в chain_reader) ──
    def find_chain(self) -> Chain:
        return self.cr.find_chain()

    # ── L3: AreaInstance ─────────────────────────────────────────
    def find_area(self, igs: int, window: int = 0x100, step: int = 8) -> Candidate:
        base = self.cr._off("InGameState", "AREA_INSTANCE")
        best = Candidate("area", 0, 0)
        for off in range(base - window, base + window + step, step):
            if off < 0x100 or off > 0x8000:
                continue
            ai = self.cr.mem.read_pointer(igs + off)
            if not ai or not self.cr.is_heap(ai):
                continue
            # player-кандидат должен сходиться по metadata — внутренний L4a-прогон
            ch = self._probe_player(ai)
            if not ch.ok:
                continue
            res = {
                "v1_player_meta": (True, ch.meta, 100),
                "v3_awake_tree": self.val.v3_awake_tree(ai),
                "v4_area_sanity": self.val.v4_area_sanity(ai),
                "v6_read_entities": self.val.v6_read_entities(ai),
            }
            s = self.val.score(res)
            cand = Candidate("area", off, s["score"], s["passed"], f"area=0x{ai:X} {s['detail']}")
            if s["verdict"] and cand.score > best.score:
                best = cand
        return best

    # ── L4a: LocalPlayer ─────────────────────────────────────────
    def find_player(self, area: int, window: int = 0x800, step: int = 8) -> Candidate:
        base = self.cr._off("AreaInstance", "LOCAL_PLAYER")
        best = Candidate("local_player", 0, 0)
        for off in range(base - window, base + window + step, step):
            if off < 0x100:
                continue
            ch = self._probe_player_off(area, off)
            if not ch.ok:
                continue
            cand = Candidate("local_player", off, 100, ["v1_player_meta"],
                             f"player=0x{ch.player:X} meta='{ch.meta}'")
            if cand.score > best.score:
                best = cand
        return best

    # ── L4b: одиночные поля ──────────────────────────────────────
    def find_camera(self, igs: int, window: int = 0x40, step: int = 8) -> Candidate:
        base = self.cr._off("InGameState", "CAMERA")
        return self._scan_field("camera", igs, base, window, step,
                                lambda off, ptr: self.val.v2_camera_zoom(igs, off))

    def find_awake(self, area: int, window: int = 0x80, step: int = 8) -> Candidate:
        base = self.cr._off("EntityTree", "AWAKE_SENTINEL")
        # НЕ «первый проходящий»: ложные мини-деревья (напр. 0x690, 2-4 узла) проходят
        # порог V3 и стоят РАНЬШЕ настоящего в окне — apply пропатчил бы мусор.
        # Берём кандидата с МАКСИМАЛЬНЫМ числом сущностей: настоящее awake-дерево
        # игры — десятки/сотни (live-проба: 342 против 2 у ложного). При равном count
        # предпочитаем baseline — стабильность между патчами. sleep-дерево (+0x700)
        # крупнее, но его sentinel не heap — V3 его отсекает.
        best = Candidate("awake", 0, 0)
        best_count = 0
        for off in range(base - window, base + window + step, step):
            if off < 0:
                continue
            ptr = self.cr.mem.read_pointer(area + off)
            if not ptr:
                continue
            ok, detail, weight = self.val.v3_awake_tree(area, off)
            if not ok:
                continue
            count = 0
            for part in detail.split():
                if part.startswith("entities="):
                    count = int(part.split("=", 1)[1])
            cand = Candidate("awake", off, int(weight), ["v3_awake_tree"],
                             f"ptr=0x{ptr:X} {detail}")
            if count > best_count or (count == best_count and off == base):
                best, best_count = cand, count
        return best

    def find_area_info(self, area: int, window: int = 0x40, step: int = 8) -> Candidate:
        o = self.cr.cfg["offsets"]["AreaInstance"]
        base = int(o["AREA_INFO"])
        return self._scan_field("area_info", area, base, window, step,
                                lambda off, ptr: self.val.v4_area_sanity(area, info_off=off))

    def _sample_walkable(self, area: int, off: int) -> Optional[tuple]:
        """БЫСТРАЯ проверка «живого» walkable-грида: размеры + сэмпл 512 байт.

        Полный read_terrain_walkable для КАЖДОГО кандидата непозволителен —
        он читает ВЕСЬ грид (до 64MB) и распаковывает миллионы клеток Python-циклом
        (на мусорных кандидатах это минуты зависания find). Здесь только:
        ptrs → size → bytes_per_row → эвристика «смесь нулей и ненулевых нибблов».
        Возвращает (width, rows) при успехе, иначе None.
        """
        import game_offsets as go
        t = area + off
        first = self.cr.mem.read_pointer(t + go.Terrain.GRID_WALKABLE_DATA)
        last = self.cr.mem.read_pointer(t + go.Terrain.GRID_WALKABLE_DATA + 8)
        if not first or not last or last <= first:
            return None
        total = last - first
        if total < 1024 or total > 64 * 1024 * 1024:
            return None
        bpr = self.cr.mem.read_int(t + go.Terrain.BYTES_PER_ROW)
        if not bpr or bpr <= 0 or bpr > 65536:
            return None
        sample = self.cr.mem.read_bytes(first, min(total, 512))  # дёшево
        if not sample:
            return None
        has_zero = any(b == 0 for b in sample)
        has_value = any(b != 0 for b in sample)
        if not (has_zero and has_value):
            return None  # однородный буфер — это не нав-грид (0x55-пустышка и т.п.)
        rows = total // bpr
        return (bpr * 2, rows)

    def find_terrain(self, area: int, window: int = 0x6000, step: int = 8) -> Candidate:
        base = self.cr._off("AreaInstance", "TERRAIN_METADATA")
        # 2026-09-06: TerrainStruct ИНЛАЙН в AreaInstance (vt первым полем).
        # Выбор умный: среди структур-кандидатов (v5) побеждает тот, у кого
        # РЕАЛЬНО читается walkable-грид (быстрый сэмпл, НЕ полный read —
        # данные сильнее внешнего вида, но без минутного распаковывающего цикла).
        # ВАЖНО: скан идёт ОТ 0x0 (не base-window). Террейн мог сдвинуться ВНИЗ
        # дальше типового окна (0x8D0 → 0xAAAA после патча = -0x1DDA) — поиск
        # от начала AreaInstance гарантированно накрывает любой сдвиг.
        import game_offsets as go
        best = Candidate("terrain", 0, 0)
        for off in range(0, base + window + step, step):
            if off < 0:
                continue
            ptr = self.cr.mem.read_pointer(area + off)
            if not ptr:
                continue
            ok, detail, weight = self.val.v5_terrain_vtable(area, off)
            if not ok:
                continue
            w = int(weight)
            r = self._sample_walkable(area, off)  # O(байты сэмпла), не O(грид)
            bonus = ""
            if r:
                w = max(w, int(weight) * 10)  # живой грид доминирует
                bonus = f" WxH={r[0]}x{r[1]}"
            cand = Candidate(name="terrain", offset=off, score=w,
                             passed=["terrain"],
                             detail=f"ptr=0x{ptr:X} {detail}{bonus}")
            if cand.score > best.score:
                best = cand
        return best

    # ── helpers ──────────────────────────────────────────────────
    def _probe_player(self, area: int) -> Chain:
        """Лучший player-кандидат внутри area (L4a внутренний прогон)."""
        c = Chain(area=area)
        base = self.cr._off("AreaInstance", "LOCAL_PLAYER")
        for off in range(base - 0x400, base + 0x400 + 8, 8):
            if off < 0x100:
                continue
            lp = self.cr.mem.read_pointer(area + off)
            if not lp or not self.cr.is_heap(lp):
                continue
            meta = self.cr.read_meta(lp)
            if meta.startswith("Metadata/"):
                c.player, c.meta = lp, meta
                return c
        return c

    def _probe_player_off(self, area: int, off: int) -> Chain:
        c = Chain(area=area)
        lp = self.cr.mem.read_pointer(area + off)
        if lp and self.cr.is_heap(lp):
            c.player, c.meta = lp, self.cr.read_meta(lp)
        return c

    def _scan_field(self, name: str, owner: int, base: int, window: int, step: int,
                    validator: Callable) -> Candidate:
        best = Candidate(name, 0, 0)
        for off in range(base - window, base + window + step, step):
            if off < 0:
                continue
            ptr = self.cr.mem.read_pointer(owner + off)
            if not ptr:
                continue
            ok, detail, weight = validator(off, ptr)
            if not ok:
                continue
            cand = Candidate(name, off, int(weight), [name], f"ptr=0x{ptr:X} {detail}")
            if cand.score > best.score:
                best = cand
        return best