"""--apply: автопатч оффсетов в файлах проекта (с подтверждением перед каждой правкой).

Патчит:
 1) game_offsets.py  — константы классов InGameState/AreaInstance (+ GameState.States*)
 2) poe2-reader/Program.cs — константы класса Off
 3) scope.json — обновление baseline
 4) offset_scanner.py / mem_detect.py — логика резолва igs (vecFirst-deref → States[])
 5) dotnet build (пересборка DLL)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_HERE), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import report as rep

PROJ = {
    "game_offsets": _ROOT / "game_offsets.py",
    "program_cs": _ROOT / "poe2-reader" / "Program.cs",
    "offset_scanner": _ROOT / "offset_scanner.py",
    "mem_detect": _ROOT / "mem_detect.py",
    "entity_reader": _ROOT / "entity_reader.py",
    "scope_json": _HERE / "scope.json",
    "csproj": _ROOT / "poe2-reader" / "lib" / "PoE2ReaderLib.csproj",
}

FIELD_MAP = {  # finding name → (file, struct, field)
    "InGameState.AREA_INSTANCE": ("game_offsets", "InGameState", "AREA_INSTANCE"),
    "InGameState.CAMERA": ("game_offsets", "InGameState", "CAMERA"),
    "InGameState.UI_ROOT": ("game_offsets", "InGameState", "UI_ROOT"),
    "AreaInstance.LOCAL_PLAYER": ("game_offsets", "AreaInstance", "LOCAL_PLAYER"),
    "AreaInstance.AWAKE_ENTITIES": ("game_offsets", "AreaInstance", "AWAKE_ENTITIES"),
    "AreaInstance.SLEEPING_ENTITIES": ("game_offsets", "AreaInstance", "SLEEPING_ENTITIES"),
    "AreaInstance.AREA_INFO": ("game_offsets", "AreaInstance", "AREA_INFO"),
    "AreaInstance.AREA_LEVEL": ("game_offsets", "AreaInstance", "AREA_LEVEL"),
    "AreaInstance.AREA_HASH": ("game_offsets", "AreaInstance", "AREA_HASH"),
    "AreaInstance.SERVER_DATA": ("game_offsets", "AreaInstance", "SERVER_DATA"),
    "AreaInstance.TERRAIN_METADATA": ("game_offsets", "AreaInstance", "TERRAIN_METADATA"),
}

CS_MAP = {  # C#-константы
    "InGameState.AREA_INSTANCE": "AreaInstanceOff",
    "InGameState.CAMERA": "CameraOff",
    "AreaInstance.LOCAL_PLAYER": "LocalPlayerOff",
}

# Константы, которые надо добавить в GameState, если их нет
STATES_CONSTS = """class GameState:
    CURRENT_STATE_PTR = 0x08       # → InGameState (StdVector deref)
    STATES_ARRAY = 0x48            # States[12] array, stride 0x10
    STATES_STRIDE = 0x10
    STATES_COUNT = 12
"""


AUTO_YES = False


def _confirm(msg: str) -> bool:
    """[Y/n]-подтверждение. При AUTO_YES (--yes / --auto) — всегда согласие."""
    if AUTO_YES:
        return True
    try:
        return input(f"{msg} [Y/n]: ").strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


def _patch_game_offsets(findings: list[dict], applied: list, notes: list) -> None:
    p = PROJ["game_offsets"]
    text = p.read_text(encoding="utf-8")
    changed = []

    # 1) константы
    for f in findings:
        if f["new"] == f["old"] or f["new"] == 0:
            continue
        key = FIELD_MAP.get(f["name"])
        if not key:
            continue
        _, struct, field = key
        # В game_offsets.py константы лежат голыми именами внутри class-блока
        # («TERRAIN_METADATA = 0x8D0»), поэтому class-префикс опционален:
        # ищем «Struct.Field = 0x…» или «Field = 0x…».
        m = re.search(rf"(?:(?:{struct})\.)?{field}\s*=\s*(0x[0-9A-Fa-f]+|\d+)",
                      text, re.MULTILINE)
        if not m:
            notes.append(f"game_offsets: не найден {struct}.{field} — {{ручная правка}}")
            continue
        prefix = m.group(0)[: m.group(0).find("=")].rstrip()
        new_line = f"{prefix} = 0x{f['new']:X}"
        old_line = m.group(0)
        if old_line == new_line:
            continue
        if not _confirm(f"  game_offsets.py: {old_line} → {new_line}?"):
            notes.append(f"game_offsets: {struct}.{field} пропущен пользователем")
            continue
        text = text.replace(old_line, new_line, 1)
        changed.append(new_line)

    # 2) GameState.State* — добавить если отсутствуют
    if "STATES_STRIDE" not in text:
        if _confirm("  game_offsets.py: добавить GameState.States* (пост-патч резолв)?"):
            text = re.sub(r"class GameState:.*?(?=\nclass |\Z)", STATES_CONSTS + "\n",
                          text, count=1, flags=re.S)
            changed.append("GameState.States* добавлены")

    p.write_text(text, encoding="utf-8")
    applied.extend(changed)


def _patch_program_cs(findings: list[dict], applied: list, notes: list) -> None:
    p = PROJ["program_cs"]
    text = p.read_text(encoding="utf-8")
    changed = []
    for f in findings:
        cs_name = CS_MAP.get(f["name"])
        if not cs_name or f["new"] == f["old"] or f["new"] == 0:
            continue
        m = re.search(rf"(public const ulong {cs_name}\s*=\s*)0x([0-9A-Fa-f]+)", text)
        if not m:
            notes.append(f"Program.cs: не найден {cs_name}")
            continue
        new_line = f"{m.group(1)}0x{f['new']:X}"
        if m.group(0) == new_line:
            continue
        if not _confirm(f"  Program.cs: {m.group(0)} → {new_line}?"):
            notes.append(f"Program.cs: {cs_name} пропущен пользователем")
            continue
        text = text.replace(m.group(0), new_line, 1)
        changed.append(new_line)
    p.write_text(text, encoding="utf-8")
    applied.extend(changed)


def _patch_resolve_logic(applied: list, notes: list) -> None:
    """offset_scanner / mem_detect / entity_reader: фиксы пост-патча (если сломаны)."""
    OLD = """            # GameState → InGameState
            vec_ptr = self._mem.read_pointer(game_state + off.GameState.CURRENT_STATE_PTR)
            if not vec_ptr:
                continue
            in_game = self._mem.read_pointer(vec_ptr)
            if not in_game:
                continue"""
    NEW = """            # GameState → InGameState (пост-патч 2026-09-04: States[]-перебор)
            # КАЖДЫЙ кандидат валидируется по metadata (как C# ValidateIgs):
            # vec-deref может дать ненулевой МУСОР — тогда берётся States[i].
            in_game = 0
            _cands = []
            vec_ptr = self._mem.read_pointer(game_state + off.GameState.CURRENT_STATE_PTR)
            if vec_ptr:
                _cands.append(self._mem.read_pointer(vec_ptr))
            for _i in range(off.GameState.STATES_COUNT):
                _cands.append(self._mem.read_pointer(
                    game_state + off.GameState.STATES_ARRAY + _i * off.GameState.STATES_STRIDE))
            for _cand in _cands:
                if not _cand or _cand < 0x10000:
                    continue
                _ai = self._mem.read_pointer(_cand + off.InGameState.AREA_INSTANCE)
                if not _ai or _ai < 0x10000:
                    continue
                _pl = self._mem.read_pointer(_ai + off.AreaInstance.LOCAL_PLAYER)
                if not _pl or _pl < 0x10000:
                    continue
                _meta = self._read_metadata(_pl)
                if _meta and "Metadata/" in _meta:
                    in_game = _cand
                    break
            if not in_game:
                continue"""
    OLD_MD = """            igs = self.mem.read_pointer(self.mem.read_pointer(game_state + 0x08))
            if not igs:
                continue
            area_inst = self.reader.find_area_instance(game_state)
            if not area_inst or area_inst < 0x10000:
                continue"""
    NEW_MD = """            # InGameState: States[]-перебор (пост-патч 2026-09-04).
            # vec-deref может дать ненулевой МУСОР — каждый кандидат валидируется.
            _cands = []
            _v = self.mem.read_pointer(game_state + 0x08)
            if _v:
                _cands.append(self.mem.read_pointer(_v))
            for _i in range(off.GameState.STATES_COUNT):
                _cands.append(self.mem.read_pointer(
                    game_state + off.GameState.STATES_ARRAY + _i * off.GameState.STATES_STRIDE))
            igs = 0
            area_inst = 0
            for _c in _cands:
                if not _c or _c < 0x10000:
                    continue
                _ai = self.mem.read_pointer(_c + off.InGameState.AREA_INSTANCE)
                if not _ai or _ai < 0x10000:
                    continue
                _vt = self.mem.read_pointer(_ai)
                if not _vt or not (self.mem._base <= _vt <= self.mem._base + self.mem._size):
                    continue
                _pl = self.mem.read_pointer(_ai + off.AreaInstance.LOCAL_PLAYER)
                if not _pl or _pl < 0x10000:
                    continue
                # Метадата игрока должна быть "Metadata/" — иначе мусорный кандидат
                _d = self.mem.read_pointer(_pl + off.Entity.DETAILS_PTR)
                _sp = self.mem.read_pointer(_d + off.EntityDetails.NAME) if _d else 0
                _buf = self.mem.read_bytes(_sp, 256) if _sp and _sp > 0x100000000 else None
                _meta = ""
                if _buf:
                    _chars = []
                    for _j in range(0, min(len(_buf), 512) - 1, 2):
                        _cc = (_buf[_j] | (_buf[_j + 1] << 8)) & 0xFFFF
                        if _cc == 0:
                            break
                        if 0x20 <= _cc <= 0x7E:
                            _chars.append(chr(_cc))
                        else:
                            break
                    _meta = "".join(_chars)
                if not _meta.startswith("Metadata/"):
                    continue
                igs = _c
                area_inst = _ai
                break
            if not igs or not area_inst:
                continue"""
    OLD_ER = """        # Area code (UTF-16)
        info_ptr = self._mem.read_pointer(area_instance + off.AreaInstance.AREA_INFO)
        if info_ptr:
            code = self._mem.read_std_wstring(info_ptr)
            info["code"] = code or \""""
    OLD_ER_FULL = OLD_ER + '"""\n        else:\n            info["code"] = ""'
    NEW_ER = """        # Area code (UTF-16) — area+0x98 УКАЗАТЕЛЬ на std::wstring → p → данные.
        # Декод строго по первому NUL с ASCII-фильтром (post-patch 2026-09-04).
        info_ptr = self._mem.read_pointer(area_instance + off.AreaInstance.AREA_INFO)
        code = ""
        if info_ptr and info_ptr > 0x100000000:
            p = self._mem.read_pointer(info_ptr)
            if p and p > 0x100000000:
                buf = self._mem.read_bytes(p, 128)
                if buf:
                    chars = []
                    for i in range(0, min(len(buf), 256) - 1, 2):
                        c = (buf[i] | (buf[i + 1] << 8)) & 0xFFFF
                        if c == 0:
                            break
                        if 0x20 <= c <= 0x7E:
                            chars.append(chr(c))
                        else:
                            break
                    code = "".join(chars)
        info["code"] = code"""
    # Якоря «NEW-логики»: фиксы пост-патча применялись в разной редакции
    # (комментарии/отступы отличаются от шаблонов выше) — точного NEW-блока
    # может не быть, но логика уже на месте (mem_detect/entity_reader, 07.09).
    ANCHORS = {
        "offset_scanner": [NEW[:80]],
        "mem_detect": ["_cands = []", "for _i in range(off.GameState.STATES_COUNT)"],
        "entity_reader": ["info_ptr > 0x100000000", "read_pointer(info_ptr)"],
    }
    for name, blk_old, blk_new, desc in (
            ("offset_scanner", OLD, NEW, "States[]-перебор"),
            ("mem_detect", OLD_MD, NEW_MD, "States[]-перебор"),
            ("entity_reader", OLD_ER_FULL, NEW_ER, "std::wstring area code"),
    ):
        p = PROJ.get(name)
        if not p or not p.exists():
            notes.append(f"{name}: путь не найден — ручная правка")
            continue
        text = p.read_text(encoding="utf-8")
        if blk_old not in text:
            anchors = ANCHORS.get(name, [blk_new])
            if any(a in text for a in anchors):
                # Фикс уже применён ранее в той или иной редакции — это нормальный статус.
                notes.append(f"{name}: резолв-фикс уже на месте — правка не нужна")
            else:
                notes.append(f"{name}: блок не найден (структура отличается) — ручная правка")
            continue
        if not _confirm(f"  {name}.py: заменить {desc}?"):
            notes.append(f"{name}: правка пропущена пользователем")
            continue
        text = text.replace(blk_old, blk_new, 1)
        p.write_text(text, encoding="utf-8")
        applied.append(f"{name}: {desc}")


def _patch_scope_json(findings: list[dict], applied: list) -> None:
    """Обновить baseline в scope.json."""
    p = PROJ["scope_json"]
    cfg = json.loads(p.read_text(encoding="utf-8"))
    for f in findings:
        if f["new"] == 0 or f["new"] == f["old"]:
            continue
        parts = f["name"].split(".")
        if len(parts) == 2:
            s, fl = parts
            if s in cfg["offsets"] and fl in cfg["offsets"][s]:
                cfg["offsets"][s][fl] = f["new"]
                applied.append(f"scope.json {s}.{fl}: 0x{f['new']:X}")
    meta = cfg.get("meta", {})
    meta["captured"] = "updated"
    cfg["meta"] = meta
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_dll(notes: list) -> bool:
    csproj = PROJ["csproj"]
    if not csproj.exists():
        notes.append(f"csproj не найден: {csproj}")
        return False
    if not _confirm("  Пересобрать DLL (dotnet build)?"):
        notes.append("build пропущен пользователем")
        return False
    try:
        r = subprocess.run(
            [r"C:\Program Files\dotnet\dotnet.exe", "build", str(csproj),
             "-c", "Release", "-v", "q", "--nologo"],
            capture_output=True, text=True, timeout=180)
        ok = r.returncode == 0
        notes.append(f"dotnet build: {'OK' if ok else 'FAIL'} {r.stdout.strip()[-200:] if not ok else ''}")
        return ok
    except Exception as e:  # noqa: BLE001
        notes.append(f"dotnet build: исключение {e}")
        return False


def apply_all(cfg: dict, report_path: str, yes: bool = False) -> dict:
    global AUTO_YES
    AUTO_YES = bool(yes)
    rp = Path(report_path)
    if not rp.exists():
        return rep.make_report("apply", cfg, ok=False,
                               notes=["report.json не найден — сначала --find"], pid=0, base=0)
    data = json.loads(rp.read_text(encoding="utf-8"))
    findings = data.get("findings", [])
    if not findings:
        return rep.make_report("apply", cfg, ok=False,
                               notes=["в отчёте нет findings"], pid=0, base=0)

    notes: list = []
    applied: list = []
    print("Найденные оффсеты:")
    for f in findings:
        print(f"  {f['name']}: 0x{f['old']:X} → 0x{f['new']:X} (score={f['score']})")

    _patch_game_offsets(findings, applied, notes)
    _patch_program_cs(findings, applied, notes)
    _patch_scope_json(findings, applied)
    _patch_resolve_logic(applied, notes)
    ok = _build_dll(notes)
    print(f"dotnet build: {'OK' if ok else 'FAIL'}")
    if applied:
        print(f"Применено: {', '.join(applied)}")
    else:
        print("Все оффсеты актуальны — правки не требуются")

    return rep.make_report("apply", cfg, ok=ok, applied=applied, findings=findings,
                           notes=notes, pid=0, base=0)