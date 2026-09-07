"""Прогон юнит-тестов модулей бота (pytest) — для авто-чинки и UI-кнопки.

Интеграционные тесты (test_integration_*) исключены: им нужна живая игра
и/или железо, они не должны влиять на авто-чинку оффсетов.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

_EXCLUDED = frozenset({
    "test_integration_hotkeys.py",
    "test_integration_hp.py",
    "test_integration_main_loop.py",
    "test_integration_overlay_sync.py",
    "test_integration_workers.py",
})


def unit_test_files() -> list[Path]:
    """Юнит-тесты бота (без интеграционных)."""
    tests = _ROOT / "tests"
    if not tests.is_dir():
        return []
    return sorted(p for p in tests.glob("test_*.py") if p.name not in _EXCLUDED)


def run_unit_tests(timeout: int = 180) -> dict:
    """Запуск pytest на юнит-наборе. Возвращает сводку + хвост вывода.

    Ключи: ok, summary, passed, failed, errors, duration (сек),
           failures (список FAILED/ERROR строк, до 20), tail.
    """
    files = unit_test_files()
    if not files:
        return {"ok": False, "summary": "нет тестов в tests/", "passed": 0, "failed": 0,
                "errors": 0, "duration": 0.0, "failures": ["no test files"], "tail": ""}
    t0 = time.time()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", *(str(f) for f in files),
             "-q", "--tb=no", "-p", "no:cacheprovider"],
            cwd=str(_ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "summary": "timeout (pytest)", "passed": 0, "failed": 0,
                "errors": 0, "duration": float(timeout), "failures": ["pytest timeout"],
                "tail": f"pytest: таймаут {timeout}s"}
    dt = round(time.time() - t0, 1)
    out = (r.stdout or "") + (r.stderr or "")

    # "364 passed in 6.85s" | "350 passed, 14 failed" | "1 passed, 2 failed, 3 errors"
    m = re.search(r"(\d+) passed(?:[^\d]*?(\d+) failed)?(?:[^\d]*?(\d+) error)?", out)
    if m:
        passed = int(m.group(1))
        failed = int(m.group(2) or 0)
        errors = int(m.group(3) or 0)
        summary = f"{passed} passed"
        if failed:
            summary += f", {failed} failed"
        if errors:
            summary += f", {errors} error" + ("s" if errors != 1 else "")
    else:
        passed = failed = errors = 0
        summary = "результат не распознан"

    failures = [ln.strip() for ln in out.splitlines()
                if ln.strip().startswith(("FAILED ", "ERROR "))]
    if r.returncode != 0 and not failures:
        failures.append(f"pytest exit={r.returncode}")

    tail = "\n".join(out.strip().splitlines()[-20:])
    return {"ok": r.returncode == 0, "summary": f"{summary} ({dt}s)",
            "passed": passed, "failed": failed, "errors": errors, "duration": dt,
            "failures": failures[:20], "tail": tail}