"""Валидаторы оффсетов V1-V6 (веса из scope.json) + скор-логика.

Правило принятия: score >= min_validators (2), обязательно сработал must_have (V1).
Каждый валидатор возвращает (ok: bool, detail: str, weight) — для трасс в отчёте.
"""

from __future__ import annotations

from typing import Optional

from chain_reader import Chain, ChainReader


class Validators:
    def __init__(self, cr: ChainReader):
        self.cr = cr
        self.cfg = cr.cfg

    # ── V1: player metadata ──────────────────────────────────────
    def v1_player_meta(self, chain: Chain) -> tuple:
        ok = chain.ok
        return ok, f"player=0x{chain.player:X} meta='{chain.meta}'", int(self.cfg["validators"]["v1_player_meta"])

    # ── V2: camera zoom в разумных пределах ─────────────────────
    def v2_camera_zoom(self, igs: int, cam_off: int = None) -> tuple:
        """zoom float на cam+0x528 (Camera.ZOOM). НЕ равен ровно 1.0:
        игрок крутит колесо мыши (0.2..5.0) — валидатор должен быть жив."""
        if igs == 0:
            return False, "igs==0", int(self.cfg["validators"]["v2_camera_zoom"])
        if cam_off is None:
            cam_off = self.cr._off("InGameState", "CAMERA")
        cam = self.cr.mem.read_pointer(igs + cam_off)
        if not cam or not self.cr.is_heap(cam):
            return False, f"cam@igs+0x{cam_off:X}=0", int(self.cfg["validators"]["v2_camera_zoom"])
        z = self.cr.mem.read_float(cam + self.cr._off("Camera", "ZOOM"))
        ok = z is not None and 0.2 <= z <= 5.0
        return ok, f"cam=0x{cam:X} zoom={z}", int(self.cfg["validators"]["v2_camera_zoom"])

    # ── V3: awake tree (sentinel heap + живой обход) ─────────────
    def v3_awake_tree(self, area: int, awake_off: int = None) -> tuple:
        w = int(self.cfg["validators"]["v3_awake_tree"])
        if not area:
            return False, "area==0", w
        if awake_off is None:
            awake_off = self.cr._off("EntityTree", "AWAKE_SENTINEL")
        # Проверяем ОБА поля: chain-дерево (EntityTree.AWAKE_SENTINEL — резолв/check)
        # и AreaInstance.AWAKE_ENTITIES — то, что реально читает бот (entity_reader).
        # ВАЖНО (2026-09-07): автоцикл записал в AreaInstance ложный 0x690 (2-4 узла),
        # chain-check остался зелёным (EntityTree 0x6F0 = 345) — бот бы сломался.
        # Теперь рассинхрон ловится здесь как FAIL.
        checks = [(awake_off, "EntityTree")]
        try:
            ai_off = int(self.cr.cfg["offsets"]["AreaInstance"].get("AWAKE_ENTITIES", 0) or 0)
            if ai_off and ai_off != awake_off:
                checks.append((ai_off, "AreaInstance"))
        except Exception:  # noqa: BLE001 — отсутствие секции не фатально
            pass
        parts = []
        for off, src in checks:
            sentinel = self.cr.mem.read_pointer(area + off)
            if not sentinel or not self.cr.is_heap(sentinel):
                return False, f"{src}+0x{off:X}: sentinel=0x{sentinel or 0:X} (не heap)", w
            ents = self.cr.walk_entities(area, off)
            parts.append(f"{src}+0x{off:X}=0x{sentinel:X} n={len(ents)}")
        # Порог: ложные сдвиги дают 1-4 узла, настоящий awake-дерево — десятки+
        # (live-проба 2026-09-07: 0x690 дал 2-4, настоящий 0x6F0 — 342). 16 отсекает
        # мини-деревья, не задевая реальные зоны (vs старый >=4 — ловил мусор).
        ok = all(
            len(self.cr.walk_entities(area, off)) >= 16 for off, _ in checks
        )
        return ok, "; ".join(parts), w

    # ── V4: area sanity (level / hash / code) ────────────────────
    def v4_area_sanity(self, area: int, info_off: int = None,
                       level_off: int = None, hash_off: int = None) -> tuple:
        w = int(self.cfg["validators"]["v4_area_sanity"])
        if not area:
            return False, "area==0", w
        o = self.cr.cfg["offsets"]["AreaInstance"]
        info_off = info_off or int(o["AREA_INFO"])
        level_off = level_off or int(o["AREA_LEVEL"])
        hash_off = hash_off or int(o["AREA_HASH"])

        code = ""
        si = self.cr.mem.read_pointer(area + info_off)
        if si and self.cr.is_heap(si):
            p = self.cr.mem.read_pointer(si)
            if p:
                # данные = Id\0\0LocName\0\0… (несколько строк подряд): берём первую до NUL
                raw = self.cr.mem.read_string_utf16(p) or ""
                code = raw.split("\x00")[0][:64]

        level = self.cr.mem.read_int(area + level_off)
        hsh = self.cr.mem.read_uint(area + hash_off)
        ok_level = level is not None and 1 <= level <= 100
        ok_hash = hsh is not None and hsh != 0
        # code строго ASCII [0x20..0x7E] и длиной >= 4 (мусор-юникод отваливается)
        ok_code = len(code) >= 4 and all(0x20 <= ord(c) <= 0x7E for c in code)
        ok = ok_level and ok_hash and ok_code
        return ok, f"level={level} hash=0x{hsh or 0:X} code='{code}'", w

    # ── V5: terrain vtable + terrain-поля ─────────────────────────
    def v5_terrain_vtable(self, area: int, terrain_off: int = None) -> tuple:
        w = int(self.cfg["validators"]["v5_terrain_vtable"])
        if not area:
            return False, "area==0", w
        if terrain_off is None:
            terrain_off = self.cr._off("AreaInstance", "TERRAIN_METADATA")
        # 2026-09-06: TerrainStruct ИНЛАЙН в AreaInstance (не heap-указатель):
        # area+terrain_off = vtable (в модуле), +0x18 = total тайлов (int),
        # +0x28 = details (heap), +0xD0 = walkable-грид (first ptr).
        tb = area + terrain_off
        vt = self.cr.mem.read_pointer(tb)
        in_mod = vt and self.cr.base <= vt < self.cr.base + self.cr.mem._size
        o = self.cr.cfg["offsets"]["Terrain"]
        total = self.cr.mem.read_int(tb + int(o["TOTAL_TILES"]))
        details = self.cr.mem.read_pointer(tb + int(o["TILE_DETAILS_PTR"]))
        ok_tiles = (total is not None and 0 < total < 100000
                    and details is not None and self.cr.is_heap(details))
        va = self.cr.mem.read_pointer(tb + int(o["GRID_WALKABLE_DATA"]))
        ok = bool(in_mod) and ok_tiles
        return ok, f"terrain@area+0x{terrain_off:X}=0x{tb:X} vtable=0x{vt or 0:X} total_tiles={total} details={'OK' if details and self.cr.is_heap(details) else '—'} walkable={'OK' if va and self.cr.is_heap(va) else '—'}", w

    # ── V6: сквозное чтение сущностей ────────────────────────────
    def v6_read_entities(self, area: int) -> tuple:
        w = int(self.cfg["validators"]["v6_read_entities"])
        if not area:
            return False, "area==0", w
        n = self.cr.entity_count(area)
        ok = n > 0
        return ok, f"entities_with_meta={n}", w

    # ── скор-логика ──────────────────────────────────────────────
    def score(self, results: dict) -> dict:
        """results: {validator_name: (ok, detail, weight)} → скор и вердикт."""
        total = 0
        passed = []
        for name, (ok, detail, weight) in results.items():
            if ok:
                total += int(weight)
                passed.append(name)
        must = self.cfg["rules"]["must_have"]
        min_n = int(self.cfg["rules"]["min_validators"])
        verdict = bool(passed and (len(passed) >= min_n) and all(m in passed for m in must))
        return {
            "score": total,
            "passed": passed,
            "verdict": verdict,
            "detail": f"{len(passed)}/{len(results)} валидаторов, score={total}",
        }