"""poe2_scope CLI — поиск и валидация оффсетов PoE2 после патчей.

Команды:
  --check          L0: резолв текущей цепочки → OK (патчей нет) / BROKEN
  --find [--force] L1-L6: поиск новых оффсетов, report.json + REPORT.md
  --verify         прогон legacy-сканеров (offset_scanner/mem_detect) — согласование
  --apply          автопатч файлов проекта с подтверждением (game_offsets, Program.cs, …)
  --tests          прогон юнит-тестов модулей бота (pytest, без integration)
  --auto           авто-чинка по тестам: тесты(до) → check → find → verify → apply → тесты(после)
  --report <path>  имя выходного JSON (default: poe2_scope/report.json)
  --dump-md        дополнительно REPORT.md
  --no-ui          без цветного вывода
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_HERE), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from chain_reader import ChainReader  # noqa: E402
from validators import Validators  # noqa: E402
from brute import Brute  # noqa: E402
import report as rep  # noqa: E402
import tests_runner as tr  # noqa: E402

OKC, ERC, WNC, END = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def _carry_findings(rep_: dict, report_path: str | Path) -> dict:
    """Не терять findings между шагами: check/verify перезаписывают report.json,
    но результаты --find должны пережить любой последующий шаг (иначе --apply
    после --verify увидит пустой отчёт). Если новый отчёт не содержит findings,
    а старый report.json — содержал — переносим их."""
    if rep_.get("findings"):
        return rep_
    prev: dict = {}
    try:
        p = Path(report_path)
        if p.exists():
            prev = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — повреждённый/битый отчёт: игнорируем
        prev = {}
    if prev.get("findings"):
        rep_["findings"] = prev["findings"]
    return rep_


def load_cfg() -> dict:
    path = Path(__file__).resolve().parent / "scope.json"
    if not path.exists():
        print(f"{ERC}scope.json не найден: {path}{END}")
        sys.exit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_check(cfg: dict, args) -> dict:
    cr = ChainReader(cfg=cfg)
    if not cr.connect():
        print(f"{ERC}Не удалось подключиться к PoE2 (не запущена? нужен admin?){END}")
        return rep.make_report("check", cfg, pid=0, base=0, chain_ok=False, notes=["connect failed"])

    print("Резолв цепочки (GSO AOB-скан…)", flush=True)
    chain = cr.find_chain()
    print(f"pid={cr.mem._pid} base=0x{cr.base:X}")
    print(f"GSO-слоты: {', '.join(hex(s) for s in cr.gso_slots()) or 'НЕТ'}")
    print(f"Цепочка: {chain.meta!r} — {'OK' if chain.ok else f'{ERC}BROKEN{END}'}")

    val = Validators(cr)
    results = {
        "v1_player_meta": val.v1_player_meta(chain),
        "v3_awake_tree": val.v3_awake_tree(chain.area),
        "v4_area_sanity": val.v4_area_sanity(chain.area),
        "v5_terrain_vtable": val.v5_terrain_vtable(chain.area),
    }
    if chain.igs:
        results["v2_camera_zoom"] = val.v2_camera_zoom(chain.igs)
    results["v6_read_entities"] = val.v6_read_entities(chain.area)

    for name, (ok, detail, w) in results.items():
        print(f"  {name}: {'✅' if ok else '❌'} {detail}")
    sc = val.score(results)
    print(f"score={sc['score']} verdict={'OK' if sc['verdict'] else 'FAIL'} ({sc['detail']})")

    return rep.make_report(
        "check", cfg, pid=cr.mem._pid, base=cr.base,
        chain_ok=chain.ok,
        chain={k: v for k, v in zip(["slot", "g", "igs", "area", "player", "meta"],
                                    [hex(chain.slot), hex(chain.g), hex(chain.igs),
                                     hex(chain.area), hex(chain.player), chain.meta])},
        validators=results, notes=["check завершён"])


def cmd_find(cfg: dict, args) -> dict:
    cr = ChainReader(cfg=cfg)
    if not cr.connect():
        print(f"{ERC}Не удалось подключиться к PoE2{END}")
        return rep.make_report("find", cfg, pid=0, base=0, notes=["connect failed"])

    print("Резолв цепочки (GSO AOB-скан…)", flush=True)
    chain = cr.find_chain()
    if chain.ok and not args.force:
        print(f"{OKC}Цепочка жива (meta={chain.meta!r}) — патчей нет. --force для принудительного скана.{END}")
        return rep.make_report("find", cfg, pid=cr.mem._pid, base=cr.base,
                               chain_ok=True, chain={"meta": chain.meta})

    print(f"{WNC}Цепочка сломана или --force. Запуск L1-L6…{END}")
    notes: list[str] = []
    findings: list[dict] = []
    val = Validators(cr)
    brute = Brute(cr, val)

    # L1: GSO slots
    slots = cr.gso_slots()
    print(f"L1 GSO-кандидаты: {[hex(s) for s in slots]}")
    notes.append(f"L1: GSO slots = {[hex(s) for s in slots]}")

    # L2: для каждого слота — игs по States[]
    igs = chain.igs
    if not igs:
        for s in slots:
            c = cr.resolve(s)
            if c.igs:
                igs = c.igs
                notes.append(f"L2: slot {hex(s)} → igs=0x{igs:X} (States[{cr.cfg['chain']['igs_states_base']}+i*{cr.cfg['chain']['igs_state_stride']}])")
                break
    if not igs:
        notes.append("L2: игs через States[] не найден — патч глубже, нужен ручной анализ")
        return rep.make_report("find", cfg, pid=cr.mem._pid, base=cr.base,
                               chain_ok=False, findings=findings, notes=notes)

    area = chain.area
    IGS = cfg["offsets"]["InGameState"]
    AI = cfg["offsets"]["AreaInstance"]
    # L3: area
    if not area:
        c3 = brute.find_area(igs)
        if c3.offset:
            area = cr.mem.read_pointer(igs + c3.offset)
            findings.append({"name": "InGameState.AREA_INSTANCE", "old": int(IGS["AREA_INSTANCE"]),
                             "new": c3.offset, "score": c3.score, "detail": c3.detail})
            print(f"L3 area: igs+0x{c3.offset:X} = 0x{area:X} ({c3.detail})")
        else:
            notes.append("L3: area-кандидат не найден")
            return rep.make_report("find", cfg, pid=cr.mem._pid, base=cr.base,
                                   chain_ok=False, findings=findings, notes=notes)

    player = chain.player
    # L4a: local player
    c4 = brute.find_player(area)
    if c4.offset:
        player = cr.mem.read_pointer(area + c4.offset)
        findings.append({"name": "AreaInstance.LOCAL_PLAYER", "old": int(AI["LOCAL_PLAYER"]),
                         "new": c4.offset, "score": c4.score, "detail": c4.detail})
        print(f"L4a player: area+0x{c4.offset:X} = 0x{player:X} ({c4.detail})")

    # L4b: поля
    c5 = brute.find_camera(igs)
    if c5.offset:
        findings.append({"name": "InGameState.CAMERA", "old": int(IGS["CAMERA"]), "new": c5.offset,
                         "score": c5.score, "detail": c5.detail})
        print(f"L4b camera: igs+0x{c5.offset:X} ({c5.detail})")
    c6 = brute.find_awake(area)
    if c6.offset:
        findings.append({"name": "AreaInstance.AWAKE_ENTITIES", "old": int(AI["AWAKE_ENTITIES"]),
                         "new": c6.offset, "score": c6.score, "detail": c6.detail})
        print(f"L4b awake: area+0x{c6.offset:X} ({c6.detail})")
    c7 = brute.find_area_info(area)
    if c7.offset:
        findings.append({"name": "AreaInstance.AREA_INFO", "old": int(AI["AREA_INFO"]),
                         "new": c7.offset, "score": c7.score, "detail": c7.detail})
        print(f"L4b area_info: area+0x{c7.offset:X} ({c7.detail})")
    c8 = brute.find_terrain(area)
    if c8.offset:
        findings.append({"name": "AreaInstance.TERRAIN_METADATA", "old": int(AI["TERRAIN_METADATA"]),
                         "new": c8.offset, "score": c8.score, "detail": c8.detail})
        print(f"L4b terrain: area+0x{c8.offset:X} ({c8.detail})")

    # L5/L6 — реализуются в l5_l6.py (вторая итерация)
    notes.append("L5/L6 (entity-layer/terrain-grid) — статический анализ, см. l5_l6.py (TODO)")

    rep_ = rep.make_report(
        "find", cfg, pid=cr.mem._pid, base=cr.base, chain_ok=False,
        chain={"igs": hex(igs), "area": hex(area or 0), "player": hex(player or 0)},
        findings=findings, notes=notes)
    return rep_


def cmd_verify(cfg: dict, args) -> dict:
    from legacy import verify_legacy
    return verify_legacy(cfg)


def cmd_tests(cfg: dict, args) -> dict:
    """Прогон юнит-тестов модулей бота (pytest, без интеграционных)."""
    print("Прогон юнит-тестов модулей (pytest, без integration)…", flush=True)
    t = tr.run_unit_tests()
    head = f"Тесты: {t['summary']}"
    print(f"{OKC if t['ok'] else ERC}{head}{END}")
    for f in t["failures"]:
        print(f"  {ERC}{f}{END}")
    notes = [head]
    if t["failures"]:
        notes.extend(f"FAILED/ERROR: {f}" for f in t["failures"])
    return rep.make_report("tests", cfg, ok=t["ok"], pid=0, base=0,
                           summary=t["summary"], notes=notes)


def cmd_apply(cfg: dict, args) -> dict:
    from apply import apply_all
    return apply_all(cfg, args.report, yes=args.yes)


def cmd_auto(cfg: dict, args) -> dict:
    """Авто-чинка по тестам: тесты(до) → check → (если ❌) find → verify → apply → тесты(после).

    DLL НЕ собирается автоматически: юзер сам проходит шаги 1-4 и жмёт
    «Применить и собрать DLL». apply идёт с интерактивными [Y/n] (без --yes).
    """
    print("=" * 60)
    print("AUTO: юнит-тесты модулей (до чинки)")
    tb = cmd_tests(cfg, args)
    print("AUTO: диагностика (--check)")
    r1 = cmd_check(cfg, args)
    vals = r1.get("validators") or {}
    failed = [n for n, (ok, _, _) in vals.items() if not ok]
    chain_ok = r1.get("chain_ok")
    if not chain_ok:
        print("AUTO: цепочка не резолвится — игра запущена? оффсеты сломаны")
    if not failed:
        print("AUTO: все валидаторы зелёные — чинить нечего, патч не требуется")
        r1["tests_before"] = tb
        return r1
    print(f"AUTO: красные валидаторы: {', '.join(failed)} → поиск (--find --force)")
    import argparse as _ap
    args2 = _ap.Namespace(report=args.report, dump_md=args.dump_md, no_ui=True,
                          check=False, find=True, verify=False, apply=False,
                          force=True, yes=False, auto=False, tests=False)
    r2 = cmd_find(cfg, args2)
    if not [f for f in (r2.get("findings") or []) if f.get("new")]:
        print("AUTO: --find не дал новых оффсетов — останавливаюсь")
        r2["tests_before"] = tb
        return r2
    # ВАЖНО: внутри одного процесса report.json ещё не записан (main пишет в конце) —
    # записываем отчёт find ВРУЧНУЮ, чтобы verify/apply читали СВЕЖИЕ findings.
    from pathlib import Path as _P
    out = _P(args.report)
    rep.dump_json(r2, out)
    if args.dump_md:
        rep.dump_md(r2, out.with_suffix(".md"))
    print("AUTO: легаси-проверка (--verify)")
    cmd_verify(cfg, args2)
    print("AUTO: применение (--apply, с подтверждениями; DLL соберёшь сам — шаг 4)")
    r4 = cmd_apply(cfg, args2)
    print("AUTO: юнит-тесты модулей (после чинки)")
    ta = cmd_tests(cfg, args)
    r4["tests_before"] = tb
    r4["tests_after"] = ta
    print("AUTO: done — ok=%s applied=%s notes=%s" % (r4.get("ok"), r4.get("applied"), r4.get("notes")))
    return r4


def main():
    ap = argparse.ArgumentParser(description="poe2_scope — оффсеты PoE2")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--find", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--auto", action="store_true", help="авто-чинка по тестам: тесты→check→find→verify→apply→тесты (без автобилда)")
    ap.add_argument("--tests", action="store_true", help="прогон юнит-тестов модулей бота (pytest)")
    ap.add_argument("--force", action="store_true", help="--find даже при живой цепочке")
    ap.add_argument("--yes", action="store_true", help="--apply без подтверждений (авто-да)")
    ap.add_argument("--report", default=str(_HERE / "report.json"))
    ap.add_argument("--dump-md", action="store_true")
    ap.add_argument("--no-ui", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    rep_ = None
    if args.check:
        rep_ = cmd_check(cfg, args)
    elif args.find:
        rep_ = cmd_find(cfg, args)
    elif args.verify:
        rep_ = cmd_verify(cfg, args)
    elif args.apply:
        rep_ = cmd_apply(cfg, args)
    elif args.tests:
        rep_ = cmd_tests(cfg, args)
    elif args.auto:
        rep_ = cmd_auto(cfg, args)
    else:
        ap.print_help()
        sys.exit(1)

    # findings живут сквозь все шаги: check/verify перезаписывают report.json,
    # но если в новом отчёте findings нет — переносим их из прошлого (см. _carry_findings)
    rep_ = _carry_findings(rep_, args.report)

    out = Path(args.report)
    rep.dump_json(rep_, out)
    print(f"Отчёт: {out}")
    if args.dump_md:
        md = out.with_suffix(".md")
        rep.dump_md(rep_, md)
        print(f"Отчёт MD: {md}")
    # apply: успех = правки применены и DLL собрана (наличие findings само по себе не сигнал успеха)
    if rep_.get("cmd") == "apply":
        sys.exit(0 if rep_.get("ok") else 1)
    sys.exit(0 if rep_.get("chain_ok") or rep_.get("ok") or rep_.get("findings") else 1)


if __name__ == "__main__":
    main()