"""--verify: прогон легаси-сканеров (offset_scanner / mem_detect) на текущих оффсетах.

Не фиксит их (это делает --apply) — только согласует статус каждой системы.
Ловит ситуации, когда C#-цепочка жива, а ctypes-fallback сломан отдельно.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_HERE), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import report as rep


def verify_legacy(cfg: dict) -> dict:
    results = {}
    notes = []

    # 1) OffsetScanner (ctypes, game_offsets)
    try:
        from offset_scanner import OffsetScanner
        sc = OffsetScanner()
        r = sc.scan()
        ok_sc = bool(r.local_player_offset and r.area_instance_offset and "Metadata/" in (r.metadata or ""))
        results["offset_scanner"] = {
            "ok": ok_sc,
            "confidence": r.confidence,
            "strategies": r.strategies_tried,
            "success": r.strategy_success,
            "metadata": r.metadata or "",
            "area_offset": r.area_instance_offset,
            "player_offset": r.local_player_offset,
        }
        notes.append(f"OffsetScanner: {'OK' if ok_sc else 'BROKEN'} ({r.strategy_success or 'FAIL'})")
    except Exception as e:  # noqa: BLE001
        results["offset_scanner"] = {"ok": False, "error": str(e)}
        notes.append(f"OffsetScanner: исключение {e}")

    # 2) MemoryMobDetector (ctypes + EntityReader)
    try:
        from mem_detect import MemoryMobDetector
        det = MemoryMobDetector()
        ok = det.connect()
        ai = det.get_area_info() if ok else None
        code = str(ai.get("code", "") or "") if ai else ""
        level = ai.get("level", 0) if ai else 0
        ok_md = bool(
            ok and det.available
            and len(code) >= 4 and all(0x20 <= ord(c) <= 0x7E for c in code)
            and isinstance(level, int) and 1 <= level <= 100
        )
        results["mem_detect"] = {"ok": ok_md, "area": ai}
        if ok and det.available:
            vit = det.read_player_vitals()
            results["mem_detect"]["vitals"] = vit
        notes.append(f"mem_detect: {'OK' if ok_md else 'BROKEN'} code='{code}' level={level}")
    except Exception as e:  # noqa: BLE001
        results["mem_detect"] = {"ok": False, "error": str(e)}
        notes.append(f"mem_detect: исключение {e}")

    all_ok = all(v.get("ok", False) for v in results.values()) and bool(results)
    return rep.make_report("verify", cfg, ok=all_ok, systems=results, notes=notes,
                           pid=0, base=0)