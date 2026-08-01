# POE2AUTO — Full Project Analysis

## 1. Overview

POE2AUTO is a Python bot for **Path of Exile 2** automation on Sony DualSense controller. It runs on Windows 10/11, requires admin rights for ViGEmBus, and has two detection modes:

- **CV (Computer Vision)** — screen capture via dxcam/mss, OpenCV color masks and template matching
- **Memory** — read-only `PROCESS_VM_READ` into PoE2 process, AOB scanning + entity reading from game structures

The bot creates a virtual Xbox 360 controller (ViGEmBus), bridges DualSense HID input through it, and injects button presses (pickup/attack/flask) into the same virtual gamepad.

## 2. Architecture

```
main.py                  → entrypoint, creates VirtualXbox + DualSenseBridge
bot.py (837 lines)       → orchestrator, hotkeys, main loop, all engines wired together
├── hp_detector.py       → HP orb liquid-level detection (fill-line scan)
├── hp_ocr.py            → OCR of HP digits in HUD (template matching, glyph segmentation)
├── hp_engine.py         → decision logic: num vs orb, grace periods, debounce
├── hp_locate.py         → auto-localization of HP string + orb on screen
├── hp_diag.py           → diagnostic dumper with debug screenshots
├── mem_detect.py        → memory-based mob detection (PoE2 process read)
├── entity_reader.py     → walks entity list, reads components (Positioned, Life, Rarity)
├── game_memory.py       → low-level PROCESS_VM_READ + NtReadVirtualMemory
├── game_offsets.py      → PoE2 memory offsets (AreaInstance, etc.)
├── offset_discovery.py  → AOB + value-scan + runtime validation for offsets
├── loot_engine.py       → pickup logic (radius, cooldown, priority)
├── color_detector.py    → HSV color mask for loot marker
├── mob_bar.py           → CV detection of mob HP bar (strict/narrow/low_hp_lock)
├── zone_detector.py     → zone/area detection from minimap
├── auto_flask.py        → configurable flask rules engine
├── dualsense_bridge.py  → raw HID → virtual Xbox bridge
├── virtual_xbox.py      → vgamepad wrapper for ViGEmBus
├── screen.py            → dxcam capture with mss fallback
├── window.py            → Win32 window finding + focus check
├── overlay.py           → tkinter click-through overlay
├── config.py            → YAML config loading, schema validation, deep merge
├── utils.py             → Toggle class, build_detector(), build_loot_engine()
├── diag_dump.py         → HP evidence dumper for debugging
├── research.py          → memory research tooling
└── research.py          → attack trigger analysis
```

**Tests/** — 9 test files, 165 tests, all passing (pytest 8.4.2)

**Dependencies (requirements.txt):**
| Package | Role |
|---------|------|
| vgamepad | Virtual Xbox 360 (ViGEmBus) |
| hidapi | DualSense raw HID |
| opencv-python | CV: masks, template matching |
| numpy | Frame arrays |
| dxcam | Fast DirectX screen capture |
| mss | Fallback screen capture |
| pywin32 | Window finding, focus check |
| PyYAML | Config I/O |
| Pillow | Calibration UI |
| pynput | Global hotkeys |

## 3. What Works Well

### 3.1 Test coverage — 165/165 passing
All 9 test files have solid coverage: `test_color_detector.py` (14), `test_hp_engine.py` (18), `test_hp_locate.py` (12), `test_hp_ocr.py` (44), `test_loot_engine.py` (19), `test_mem_detect.py` (48), `test_mob_bar.py` (9), `test_offset_discovery.py` (26).

### 3.2 Dual detection with graceful fallback
CV mode works everywhere; memory mode is faster and more accurate but depends on PoE2 offsets. When offsets go stale (game patch), AOB scan auto-finds new ones. If memory entirely fails, `mem_detect.py` falls back to CV automatically. This is a strong resilience pattern.

### 3.3 Clean module boundaries
- `hp_engine.py` is pure logic: `decide(num, hm, low, foreground) → HPDecision`. No screen I/O, no game-specific code. Easy to test in isolation.
- `loot_engine.py` is 57 lines, single responsibility: "is there loot in radius and past cooldown?"
- `utils.py` factory functions cleanly construct engines from config dicts.

### 3.4 Auto-localization of HUD
`hp_locate.py` finds the HP digit string and orb position at startup — no manual ROI calibration needed when resolution/UI Scale changes. Shown in the README as a key selling point.

### 3.5 Diagonal state machine in hp_engine.py
The HP engine tracks `_last_good_ratio`, `_num_blind_since`, `_num_low_since`, `_low_streak`, `_last_clip_warn` — a well-designed state machine that handles transitions between digit/OOR/menu states without false triggers.

### 3.6 Thread-safe config updates
`LootWorker.update_config()` uses a `threading.Lock` — hotkey-triggered config changes don't race with the worker's frame consumption.

### 3.7 Diagnostic ecosystem
- `diag_dump.py` saves HP evidence screenshots with reason codes
- `hp_diag.py` interactive diagnostic tool
- Debug files in `_debug/hp_events/` show OK/LOST cases with full/vis/mask crops
- This makes post-mortem debugging very effective

### 3.8 Hotkey system (pynput)
F2–F12 mapped to toggle modes, clean overlay feedback, `F12` exits gracefully with bridge cleanup in `finally` block.

## 4. Problems

### 4.1 Ruff: 42 diagnostics (28 auto-fixable)
```
conftest.py:4 — unused import pytest (F401)
test_offset_discovery.py:265 — unused variable `result` (F841)
```
Plus 40 more unused imports/variables across test files and production code. Low severity but noisy.

### 4.2 `_debug/` directory is polluted
14 Python files live in `_debug/` alongside 100+ debug PNGs (23MB+). Critically, `_debug/test_ingame_pattern.py` has **module-level `sys.exit(1)`** that causes an `INTERNALERROR` in pytest when it accidentally gets picked up. The `_debug/` directory is partially `.gitignore`d (`*.log`, `*.log.*`) but NOT the debug PNGs — they're tracked in git (several MB of transient screenshots).

### 4.3 `graphify-out/` is 900MB+ tracked in git
Massive auto-generated AST cache and UML graph data committed to the repo. The `.gitignore` already has `graphify-out/` but files were committed before the ignore rule.

### 4.4 pytest picks up `_debug/test_*.py` files
```
pytest -x  ←  fails on `_debug/test_ingame_pattern.py` (sys.exit at module level)
pytest tests/  ←  works (165 pass)
```
No `pytest.ini` or `[tool.pytest.ini_options]` to exclude `_debug/`.

### 4.5 bot.py is a monolith (837 lines / 44KB)
`bot.py` holds: `LootWorker`, `MobWorker`, `FlaskWorker`, main loop, hotkey handler, overlay management, config hot-reloading, all wired together. This is the primary orchestrator but has grown too large.

### 4.6 Missing project infrastructure
- No `pyproject.toml` (only `requirements.txt` with loose `>=` pins)
- No `.pre-commit-config.yaml` despite having `black`, `ruff`, `mypy` installed
- No CI/CD workflow
- No `pytest.ini` / `pyproject.toml` test config
- No `Makefile` / task runner shortcuts
- No `src/` layout or `__init__.py` packages — flat module structure

### 4.7 config.py schema validation is shallow
`validate_config()` only checks `isinstance(val, expected_type)`. No validation of:
- Value ranges (e.g. `threshold` should be 0..1, `hue_tolerance` 0..180)
- Required keys missing (just skips them with `continue`)
- Enum/constrained string values

### 4.8 Windows-only with no abstraction
All Win32 API calls (`OpenProcess`, `NtReadVirtualMemory`, `viGEmBus` driver, `HidHide`) are hardcoded. The `screen.py` does use dxcam which is Windows-specific by nature, but the abstraction boundary between "platform" and "logic" is very thin.

### 4.9 No type hints / mypy not configured
mypy is installed but can't run due to import structure issues (scripts import each other without package context). No `py.typed` marker.

### 4.10 Loose dependency pins
`requirements.txt` uses `>=` for everything:
```
opencv-python>=4.9
numpy>=1.26
dxcam>=0.0.5
```
No lockfile (`uv.lock`, `poetry.lock`, `pip-compile`). Possible breakage from minor updates.

### 4.11 Test files are large
`test_mem_detect.py` — 12KB, `test_offset_discovery.py` — 13KB, `test_hp_ocr.py` — 7KB. Tests are integration-heavy (read real fixture PNGs) which is good for validity but makes them slow and flaky if fixture paths change.

### 4.12 `requirements.txt` not comprehensive
Missing dev/test deps (`pytest`, `ruff`, `mypy`, `black`). No way for a new contributor to set up exactly the dev environment.

### 4.13 `nul` file in project root
A 51-byte file named `nul` in the project root — likely a leftover from accidental null redirect and committed to git.

### 4.14 Broken Windows paths in `.gitignore`
`.gitignore` has `nul` at the bottom, which works but is an artifact. The `_debug/` and `graphify-out/` entries help but are inconsistently applied to debug PNGs.

## 5. Improvement Recommendations

### 5.1 Immediate (low effort, high impact)
1. **Add `pytest.ini`** to exclude `_debug/`:
   ```ini
   [pytest]
   testpaths = tests
   ```
2. **Run `ruff check --fix`** — auto-fixes 28 of 42 lint errors
3. **Add `.gitignore` entries** for `_debug/*.png`, `_debug/*.py` (dev-only), and force-remove `graphify-out/` from git
4. **Remove `nul`** from project root and `.gitignore`
5. **Remove or rename `_debug/test_*.py`** files — they're not pytest-compatible and pollute discovery

### 5.2 Short-term (1–2 days)
6. **Add `pyproject.toml`** with `[project]`, `[project.optional-dependencies[dev]]`, `[tool.pytest.ini_options]`, `[tool.ruff]`, `[tool.mypy]`
7. **Add `uv.lock`** (or `pip-compile`) for reproducible dev environment
8. **Split `bot.py`** — extract `AutoAttackEngine` (mob_bar integration), `AutoFlaskManager` (hp_ocr + auto_flask), `InputController` (virtual_xbox + dualsense_bridge), and a `BotEngine` that wires them
9. **Add type hints** to production modules (start with `hp_engine.py`, `loot_engine.py`, `utils.py` — they're the cleanest candidates)

### 5.3 Medium-term (1 week)
10. **Add pre-commit hooks** (`ruff`, `mypy`, `black`, `pytest`)
11. **Add GitHub Actions CI** (`.github/workflows/test.yml`) — even on Windows, `pytest tests/` is fast (0.15s)
12. **Consider `src/` layout** — move all production modules under `poe2auto/` with `__init__.py`, enable proper mypy package scanning
13. **Pin critical CV dependencies** (`opencv-python`, `numpy`, `dxcam`) in a lockfile — these are the core detection engine and minor version bumps can break pixel-level logic

### 5.4 Nice-to-have
14. **Structured logging** — replace string-based log messages with `structlog` or at minimum JSON format for machine-parseable bot logs
15. **Config schema validation** — add value-range checks in `config.py` `validate_config()` (e.g. `threshold` must be 0..1)
16. **Memory offset versioning** — store offset discovery results with PoE2 build hashes so stale offsets are immediately detectable
17. **Performance profiling** — `bot.py` does screen capture + OCR + mob detection + loot detection per frame; add periodic profiling to catch frame time regressions
18. **Remove debug PNGs from git** — they're artifacts, not deliverables. Add a `_debug/` README explaining they're ephemeral

## 6. Dependency Health

| Dep | Version | Status |
|-----|---------|--------|
| opencv-python | 4.13.0.92 | Latest stable, core dependency |
| numpy | 2.3.5 | Latest, fine |
| dxcam | 0.3.0 | Low-level capture, no activity recently |
| mss | 10.2.0 | Stable fallback |
| Pillow | 12.0.0 | Fine |
| pywin32 | 311 | Fine |
| vgamepad | 0.1 | Wraps ViGEmBus, stable |
| hidapi | 0.15.0 | DualSense bridge |
| pynput | 1.8.2 | Hotkey system |
| PyYAML | 6.0.3 | Fine |
| pytest | 8.4.2 | Current |
| ruff | 0.12.0 | Current |
| black | 25.9.0 | Current |
| mypy | 1.17.1 | Current |

All installed and functional. No security-advisory-level risk in the deps.

## 7. Summary

| Metric | Value |
|--------|-------|
| Production files | 30 Python modules |
| Total lines | ~2,500 (non-test) |
| Tests | 165, all passing |
| Ruff errors | 42 (28 auto-fixable) |
| Largest module | `bot.py` (837 lines) |
| Largest test file | `test_mem_detect.py` (12KB) |
| Test speed | 0.15s |
| Git commits since June | ~48 |
| Dependencies | 10 runtime + 8 dev |

**Overall assessment:** solid project with good test coverage and clean detection logic. The main areas for improvement are project infrastructure (pyproject.toml, CI, locking), code hygiene (ruff, type hints, bot.py split), and repo hygiene (graphify-out, debug artifacts, nul file). Nothing is architecturally broken — it's a well-organized single-developer project that benefits from standard software engineering hardening.
