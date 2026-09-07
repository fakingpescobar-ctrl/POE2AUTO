"""Цепочка указателей PoE2: GSO-slot → GameState → InGameState → AreaInstance → Player.

Единый источник правды для резолва (копия логики GameReader.Igs() из poe2-reader/Program.cs).
Использует ctypes-транспорт game_memory.GameMemory — без pythonnet/DLL.

Патч 2026-09-04: InGameState больше НЕ лежит по vecFirst-deref стабильно —
ищется по 12 слотам States[] (g + 0x48 + i*0x10), валидируется полной цепочкой
до локального игрока с metadata "Metadata/…".
"""

from __future__ import annotations

import json
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from game_memory import GameMemory  # noqa: E402

LOG = print if "--debug" in sys.argv else (lambda *_: None)


@dataclass
class Chain:
    """Результат резолва цепочки."""

    slot: int = 0
    g: int = 0
    igs: int = 0
    area: int = 0
    player: int = 0
    meta: str = ""
    trace: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.player != 0 and self.meta.startswith("Metadata/")


class ChainReader:
    """Резолв цепочки поверх GameMemory (ctypes)."""

    def __init__(self, mem: Optional[GameMemory] = None, cfg: Optional[dict] = None):
        self.cfg = cfg
        self.mem = mem or GameMemory()
        self._gso_slots: list[int] = []
        self._has_module = False

    # ── подключение ──────────────────────────────────────────────
    def connect(self) -> bool:
        if not self.mem.connected and not self.mem.connect():
            return False
        self._has_module = self.mem._base > 0 and self.mem._size > 0
        return self._has_module

    @property
    def base(self) -> int:
        return self.mem._base

    def is_heap(self, v: int) -> bool:
        """Валидный heap-адрес: выше 0x10000 и ниже базы модуля (копия IsHeap C#)."""
        return 0x10000 <= v < self.mem._base

    # ── AOB / GSO-slot ───────────────────────────────────────────
    def gso_slots(self, pattern: Optional[bytes] = None,
                  wildcards: Optional[set] = None) -> list[int]:
        """Relaxed AOB → все GSO-slot адреса (результат RIP-относительной адресации)."""
        if self._gso_slots:
            return self._gso_slots
        if pattern is None:
            p = self.cfg["aob"]["game_states"]
            pattern = _pattern_to_bytes(p["pattern"])
            wildcards = {int(x) for x in p["wildcards"]}
        disp_off = int(p.get("disp_off", 3))
        instr_len = int(p.get("instr_len", 7))
        matches = self.mem.aob_scan(pattern, wildcards)
        slots = []
        for m in matches:
            d = self.mem.read_bytes(m + disp_off, 4)
            if not d or len(d) < 4:
                continue
            disp = struct.unpack("<i", d)[0]
            slots.append(m + instr_len + disp)
        self._gso_slots = sorted(set(slots))
        return self._gso_slots

    # ── цепочка ──────────────────────────────────────────────────
    def read_meta(self, entity: int) -> str:
        """Metadata путь сущности: entity → details(+0x08) → UTF-16 данные напрямую.

        ВАЖНО: sp (из деталей) — это УЖЕ указатель на wchar_t* данные, НЕ структура
        std::wstring. read_std_wstring(sp) здесь НЕ работает (первый wchar_t < 0x10000
        уходит в SSO-ветку и даёт мусор). Копия C# ReadEntityPath: ReadBytes(sp,256),
        UTF-16LE до нуля, только печатные ASCII 0x20..0x7E.
        """
        if not entity or not self.is_heap(entity):
            return ""
        det = self.mem.read_pointer(entity + 8)
        if not det or not self.is_heap(det):
            return ""
        sp = self.mem.read_pointer(det + 8)
        if not sp or sp < 0x100000000:
            return ""
        buf = self.mem.read_bytes(sp, 256)
        if not buf:
            return ""
        out = []
        for i in range(0, min(len(buf), 512) - 1, 2):
            c = (buf[i] | (buf[i + 1] << 8)) & 0xFFFF
            if c == 0:
                break
            if 0x20 <= c <= 0x7E:
                out.append(chr(c))
            else:
                break
        return "".join(out)

    def _validate_igs(self, igs: int) -> bool:
        if not igs or not self.is_heap(igs):
            return False
        ai = self.mem.read_pointer(igs + self._off("InGameState", "AREA_INSTANCE"))
        if not ai or not self.is_heap(ai):
            return False
        lp = self.mem.read_pointer(ai + self._off("AreaInstance", "LOCAL_PLAYER"))
        if not lp or not self.is_heap(lp):
            return False
        return self.read_meta(lp).startswith("Metadata/")

    def resolve_igs(self, g: int) -> int:
        """Как GameReader.Igs(): vecFirst-deref, fallback 12×States[]."""
        ch = self.cfg["chain"]
        ptr = int(ch["igs_current_state_ptr"])
        base = int(ch["igs_states_base"])
        stride = int(ch["igs_state_stride"])
        count = int(ch["igs_state_slot_count"])

        vec_first = self.mem.read_pointer(g + ptr)
        if vec_first and self.is_heap(vec_first):
            igs = self.mem.read_pointer(vec_first)
            if self._validate_igs(igs):
                return igs
        for i in range(count):
            igs = self.mem.read_pointer(g + base + i * stride)
            if self._validate_igs(igs):
                return igs
        return 0

    def resolve(self, slot: int) -> Chain:
        c = Chain(slot=slot)
        g = self.mem.read_pointer(slot)
        if not g or not self.is_heap(g):
            return c
        c.g = g
        igs = self.resolve_igs(g)
        if not igs:
            return c
        c.igs = igs
        ai = self.mem.read_pointer(igs + self._off("InGameState", "AREA_INSTANCE"))
        if ai and self.is_heap(ai):
            c.area = ai
            lp = self.mem.read_pointer(ai + self._off("AreaInstance", "LOCAL_PLAYER"))
            if lp and self.is_heap(lp):
                c.player = lp
                c.meta = self.read_meta(lp)
        c.trace = [("slot", hex(slot)), ("g", hex(c.g)), ("igs", hex(c.igs)),
                   ("area", hex(c.area)), ("player", hex(c.player)), ("meta", c.meta)]
        return c

    def find_chain(self) -> Chain:
        """Перебор всех GSO-slot кандидатов, возврат валидной цепочки (первой)."""
        for slot in self.gso_slots():
            c = self.resolve(slot)
            if c.ok:
                return c
        return Chain()

    # ── дерево сущностей (V3/V6/L5) ──────────────────────────────
    def walk_entities(self, area: int, sentinel_off: Optional[int] = None,
                      limit: int = 5000) -> list[int]:
        """Полный in-order обход red-black дерева (копия ReadEntities C# 414-446)."""
        if sentinel_off is None:
            sentinel_off = self._off("EntityTree", "AWAKE_SENTINEL")
        sentinel = self.mem.read_pointer(area + sentinel_off)
        if not sentinel or not self.is_heap(sentinel):
            return []
        leftmost = self.mem.read_pointer(sentinel)
        if not leftmost or leftmost == sentinel:
            return []
        seen: set = set()
        node = leftmost
        out: list[int] = []
        for i in range(limit):
            if not node or node == sentinel:
                break
            if node in seen:
                break
            seen.add(node)
            nd = self.mem.read_bytes(node, 0x30)
            if not nd or len(nd) < 0x30:
                break
            is_nil = nd[25]
            if is_nil == 0:
                ent = struct.unpack("<Q", nd[40:48])[0]
                if ent and self.is_heap(ent):
                    out.append(ent)
            # In-order: правая подветка, потом левые вниз (C#:414-427)
            right = struct.unpack("<Q", nd[16:24])[0]
            if right and right != sentinel:
                node = right
                for _j in range(500):
                    lb = self.mem.read_bytes(node, 8)
                    if not lb or len(lb) < 8:
                        node = 0
                        break
                    left_sub = struct.unpack("<Q", lb)[0]
                    if left_sub == sentinel or left_sub == 0:
                        break
                    node = left_sub
            else:
                # Backtrack по parent (C#:428-444)
                backed = False
                while True:
                    pb = self.mem.read_bytes(node + 0x08, 8)
                    if not pb or len(pb) < 8:
                        node = 0
                        break
                    parent = struct.unpack("<Q", pb)[0]
                    if parent == sentinel or parent == 0:
                        node = 0
                        break
                    lop = self.mem.read_bytes(parent, 8)
                    if not lop or len(lop) < 8:
                        node = 0
                        break
                    left_of_parent = struct.unpack("<Q", lop)[0]
                    if left_of_parent == node:
                        node = parent
                        backed = True
                        break
                    node = parent
                if not backed:
                    break
            if node == leftmost:
                break
        return out

    def entity_count(self, area: int, sentinel_off: Optional[int] = None) -> int:
        ents = self.walk_entities(area, sentinel_off)
        return len([e for e in ents if self.read_meta(e).startswith("Metadata/")])

    # ── helpers ──────────────────────────────────────────────────
    def _off(self, struct_name: str, field_name: str) -> int:
        return int(self.cfg["offsets"][struct_name][field_name])


def _pattern_to_bytes(text: str) -> bytes:
    out = bytearray()
    for tok in text.split():
        if tok == "??":
            out.append(0)
        else:
            out.append(int(tok, 16))
    return bytes(out)


def _wildcards_from_pattern(text: str) -> set:
    res, i = set(), 0
    for tok in text.split():
        if tok == "??":
            res.add(i)
        i += 1
    return res