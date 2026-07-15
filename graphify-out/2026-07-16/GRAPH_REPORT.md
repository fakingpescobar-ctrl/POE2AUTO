# Graph Report - POE2AUTO  (2026-07-16)

## Corpus Check
- 24 files · ~22,436 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 271 nodes · 428 edges · 12 communities
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS · INFERRED: 2 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `3ce9e4b3`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- bot.py
- GameWindow
- calibrate_hp_digits.py
- POE2AUTO
- DualSenseBridge
- VirtualXbox360
- POE2AUTO — Проблемы и решения
- Overlay
- HPDiag
- LootEngine
- .measure
- zone_detector.py

## God Nodes (most connected - your core abstractions)
1. `VirtualXbox360` - 19 edges
2. `GameWindow` - 14 edges
3. `POE2AUTO` - 14 edges
4. `DualSenseBridge` - 13 edges
5. `HPNumReader` - 13 edges
6. `ScreenCapture` - 13 edges
7. `POE2AUTO — Проблемы и решения` - 13 edges
8. `main()` - 12 edges
9. `Overlay` - 12 edges
10. `HPDiag` - 11 edges

## Surprising Connections (you probably didn't know these)
- `Toggle` --uses--> `HPDiag`  [INFERRED]
  bot.py → diag_dump.py
- `Toggle` --uses--> `MobBarDetector`  [INFERRED]
  bot.py → mob_bar.py
- `main()` --calls--> `HPDiag`  [EXTRACTED]
  hp_diag.py → diag_dump.py
- `main()` --calls--> `load_config()`  [EXTRACTED]
  calibrate.py → config.py
- `main()` --calls--> `save_config()`  [EXTRACTED]
  calibrate.py → config.py

## Import Cycles
- None detected.

## Communities (12 total, 0 thin omitted)

### Community 0 - "bot.py"
Cohesion: 0.09
Nodes (21): build_detector(), main(), POE2AUTO — шаг 3: авто-подбор лута через виртуальный геймпад.  Что делает:   1., Потокобезопасный флаг авто-режима., Toggle, classify(), dump_attack_trigger(), dump_event() (+13 more)

### Community 1 - "GameWindow"
Cohesion: 0.08
Nodes (28): build_help_image(), _font(), main(), Калибровка HP-орба для автофляски (fill-line + tripwire).  Тыкаешь ЛКМ в центр о, _wrap(), _deep_merge(), load_config(), Конфиг POE2AUTO: загрузка/сохранение config.yaml с дефолтами. (+20 more)

### Community 2 - "calibrate_hp_digits.py"
Cohesion: 0.11
Nodes (31): capture_candidates(), collected(), grab(), main(), manual_calibrate(), Разовая калибровка шаблонов цифр HP для hp_ocr.py.  Снимает кадр игры, вырезает, Ручной режим: снимок → ты вписываешь что видишь → шаблоны сохраняются., Копит на диск кадры-кандидаты с недостающей цифрой (надёжно, без угадывания). (+23 more)

### Community 3 - "POE2AUTO"
Cohesion: 0.06
Nodes (32): assets — медиа для README, Как записать гифку (бесплатно), 1. Скачать проект, 2. Установить Python-зависимости, 3. Установить драйвер ViGEmBus, 4. Установить и настроить HidHide, 5. Подключить DualSense, POE2AUTO (+24 more)

### Community 4 - "DualSenseBridge"
Cohesion: 0.15
Nodes (8): DualSenseBridge, Мост DualSense → виртуальный Xbox 360 (raw HID, как DS4Windows).  Открывает Dual, 0-255 (центр 128) → -32767..32767., Нажать/отпустить кнопку Xbox по фронту бита DualSense., Читает DualSense по HID и форвардит в VirtualXbox360., main(), POE2AUTO — шаг 1: захват DualSense через виртуальный Xbox 360.  Создаёт виртуаль, Виртуальный Xbox 360 контроллер (ViGEmBus через vgamepad).  PoE2 игнорирует вирт

### Community 5 - "VirtualXbox360"
Cohesion: 0.14
Nodes (5): Коротко нажать и отпустить кнопку (для команд бота, напр. подбор=A)., Коротко выжать левый триггер (L2) — напр. HP фляга., Коротко выжать правый триггер (R2)., Потокобезопасная обёртка над виртуальным Xbox 360., VirtualXbox360

### Community 6 - "POE2AUTO — Проблемы и решения"
Cohesion: 0.14
Nodes (13): 10. Персонаж умер — но это НЕ баг бота, 11. Спам L2 в меню (теперь через ОРБ, после перехода на цифры), 1. Фляга жмётся поздно, просадки по HP, 2. Fallback не срабатывал из-за стрик-счётчика, 3. Ложные фляги при перекрытии орба на полном HP, 4. Спам фляги в меню (через орб), 5. Задержка на КРИТИЧЕСКОЙ просадке (орб почти пустой), 6. ГЛАВНОЕ: чтение HP по цифрам HUD (OCR) (+5 more)

### Community 7 - "Overlay"
Cohesion: 0.14
Nodes (6): _hp_color(), Overlay, OverlayState, Полупрозрачный оверлей поверх игры со статусами функций бота.  tkinter (stdlib),, Снимок состояния бота для отрисовки. Бот пишет поля напрямую., Оверлей в отдельном потоке. start()/stop()/toggle().

### Community 8 - "HPDiag"
Cohesion: 0.14
Nodes (11): HPNumReader, Читает HP из цифр HUD. ratio = current/max или None, если кадр нечитаем., Готов ли: есть ВСЕ шаблоны (0-9 и '/'). Иначе номера читаться не будут         н, Нормированный глиф → (символ, лучший score) по всем шаблонам., Сырой блоб → распознанные символы (1+).          Слипшиеся цифры (хвост «7» липн, Готовый кроп ROI → строка распознанных символов., Полный кадр → строка распознанных символов (ROI берётся по долям)., Полный кадр → (current, max, ratio) или None (кадр нечитаем → откат на орб). (+3 more)

### Community 9 - "LootEngine"
Cohesion: 0.20
Nodes (5): LootEngine, Логика подбора для геймпада: решает, жать ли pickup (A/Cross).  На контроллере н, Центр персонажа в координатах кадра., Цели (cx, cy, area) в пределах радиуса от центра персонажа.         center — пер, Прошёл ли кулдаун с прошлого нажатия.

### Community 10 - ".measure"
Cohesion: 0.16
Nodes (15): build_help_image(), _load_font(), main(), Мастер калибровки маски лута (как calibrate.bat в старом проекте).  Захватывает, RGB пикселя кадра (с клампом в границы)., Статичная панель-справка с описанием настроек (Cyrillic через Pillow)., sample_rgb(), _wrap() (+7 more)

### Community 11 - "zone_detector.py"
Cohesion: 0.33
Nodes (3): Детект «мы в убежище» по заголовку панели заданий справа сверху.  В убежище пане, True, если в кадре виден заголовок «Убежище» панели заданий., ZoneDetector

## Knowledge Gaps
- **36 isolated node(s):** `Демо`, `Содержание`, `Для чего это и что умеет`, `Как это устроено`, `Python-библиотеки (`requirements.txt`)` (+31 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `VirtualXbox360` connect `VirtualXbox360` to `calibrate_hp_digits.py`, `DualSenseBridge`?**
  _High betweenness centrality (0.103) - this node is a cross-community bridge._
- **Why does `HPNumReader` connect `HPDiag` to `calibrate_hp_digits.py`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Why does `DualSenseBridge` connect `DualSenseBridge` to `calibrate_hp_digits.py`?**
  _High betweenness centrality (0.063) - this node is a cross-community bridge._
- **What connects `Демо`, `Содержание`, `Для чего это и что умеет` to the rest of the system?**
  _36 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `bot.py` be split into smaller, more focused modules?**
  _Cohesion score 0.08901515151515152 - nodes in this community are weakly interconnected._
- **Should `GameWindow` be split into smaller, more focused modules?**
  _Cohesion score 0.07729468599033816 - nodes in this community are weakly interconnected._
- **Should `calibrate_hp_digits.py` be split into smaller, more focused modules?**
  _Cohesion score 0.11229946524064172 - nodes in this community are weakly interconnected._