"""Отчёт: JSON (для машин) + REPORT.md (для человека)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def make_report(cmd: str, cfg: dict, **payload) -> dict:
    rep = {
        "cmd": cmd,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "patch": cfg.get("meta", {}).get("patch"),
        "game": {"pid": payload.get("pid", 0), "base": payload.get("base", 0)},
        **payload,
    }
    return rep


def dump_json(rep: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def dump_md(rep: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# poe2_scope — {rep['cmd']}",
        "",
        f"- Время: {rep['ts']} · Патч: {rep['patch']} · PID: {rep['game']['pid']} · Base: 0x{rep['game']['base']:X}",
        "",
    ]
    if rep.get("chain_ok") is not None:
        lines += [
            "## Цепочка",
            "",
            f"- **Статус:** {'OK — патчи не требуются' if rep['chain_ok'] else 'СЛОМАНА'}",
        ]
        for k, v in rep.get("chain", {}).items():
            lines.append(f"- {k}: {v}")
        lines.append("")
    if rep.get("validators"):
        lines += ["## Валидаторы", "", "| Валидатор | OK | Деталь | Вес |", "|---|---|---|---|"]
        for name, (ok, detail, weight) in rep["validators"].items():
            lines.append(f"| {name} | {'✅' if ok else '❌'} | {detail} | {weight} |")
        lines.append("")
    if rep.get("findings"):
        lines += ["## Найденные оффсеты", "", "| Поле | Старый | Новый | Score |", "|---|---|---|---|"]
        for f in rep["findings"]:
            lines.append(f"| {f['name']} | 0x{f['old']:X} | 0x{f['new']:X} | {f['score']} |")
        lines.append("")
    if rep.get("notes"):
        lines += ["## Заметки", ""]
        for n in rep["notes"]:
            lines.append(f"- {n}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class ChainEncoder(json.JSONEncoder):
    def default(self, o):
        if hasattr(o, "__dict__"):
            return o.__dict__
        return super().default(o)