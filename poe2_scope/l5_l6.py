"""L5/L6 — нижние слои: entity-details и terrain-grid.

Не сдвигались в патче 2026-09-04, но после будущих патчей должны проверяться.
Все функции возвращают (ok, detail, findings) — кандидаты на новые оффсеты.
"""

from __future__ import annotations

import struct

from chain_reader import ChainReader


class L5L6:
    def __init__(self, cr: ChainReader):
        self.cr = cr

    # ── L5: entity layer ─────────────────────────────────────────
    def verify_entity_layer(self, area: int, sample: int = 150) -> tuple:
        """Процент сущностей с валидной metadata и компонентами (details-слой)."""
        ents = self.cr.walk_entities(area)[:sample]
        if not ents:
            return False, "нет сущностей для проверки слоя", []
        ok_meta = sum(1 for e in ents if self.cr.read_meta(e).startswith("Metadata/"))
        ok_comp = 0
        for e in ents:
            if self._has_components(e):
                ok_comp += 1
        ratio = ok_meta / len(ents)
        ok = ratio > 0.7 and ok_comp / len(ents) > 0.7
        return ok, f"meta={ok_meta}/{len(ents)} comps={ok_comp}/{len(ents)}", [
            {"name": "EntityLayer", "old": 0, "new": 0,
             "score": int(ratio * 100), "detail": f"meta_ratio={ratio:.2f}"}]

    def _has_components(self, entity: int) -> bool:
        det = self.cr.mem.read_pointer(entity + 8)
        if not det or not self.cr.is_heap(det):
            return False
        lookup = self.cr.mem.read_pointer(det + 0x28)
        if not lookup or not self.cr.is_heap(lookup):
            return False
        bb = self.cr.mem.read_pointer(lookup + 0x28)
        be = self.cr.mem.read_pointer(lookup + 0x30)
        if not bb or be <= bb or (be - bb) > 0x2000:
            return False
        clb = self.cr.mem.read_pointer(entity + 0x10)
        return bool(clb)

    # ── L6: terrain grid ─────────────────────────────────────────
    def verify_terrain_grid(self, area: int, terrain_off: int = None) -> tuple:
        """Проверка terrain-объекта: tiles, bytesPerRow, walkable vector — осмысленные."""
        if terrain_off is None:
            terrain_off = self.cr._off("AreaInstance", "TERRAIN_METADATA")
        t = self.cr.mem.read_pointer(area + terrain_off)
        if not t or not self.cr.is_heap(t):
            return False, f"terrain@+0x{terrain_off:X}=0", []

        # TOTAL_TILES (StdTuple2D<long>)
        d = self.cr.mem.read_bytes(t + 24, 16)
        tiles = struct.unpack("<qq", d) if d and len(d) == 16 else (0, 0)
        ok_tiles = 0 < tiles[0] < 100000 and 0 < tiles[1] < 100000

        # BYTES_PER_ROW
        bpr = self.cr.mem.read_int(t + 304)
        ok_bpr = bpr is not None and 0 < bpr <= 0x100000

        # GRID_WALKABLE_DATA StdVector {begin,end,cap}
        va = self.cr.mem.read_pointer(t + 208)
        ve = self.cr.mem.read_pointer(t + 216)
        ok_va = bool(va and self.cr.is_heap(va) and ve and ve >= va and (ve - va) < 0x4000000)

        ok = ok_tiles and ok_bpr and ok_va
        detail = f"tiles=({tiles[0]},{tiles[1]}) bytesPerRow={bpr} walkable=[0x{va or 0:X}..0x{ve or 0:X}]"
        return ok, detail, [
            {"name": "Terrain.TOTAL_TILES", "old": 0x18, "new": 0x18, "score": 100 if ok_tiles else 0, "detail": detail},
            {"name": "Terrain.BYTES_PER_ROW", "old": 0x130, "new": 0x130, "score": 100 if ok_bpr else 0, "detail": detail},
            {"name": "Terrain.GRID_WALKABLE_DATA", "old": 0xD0, "new": 0xD0, "score": 100 if ok_va else 0, "detail": detail}]