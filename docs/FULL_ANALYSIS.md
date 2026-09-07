# Полный анализ проекта POE2AUTO

> **Путь:** `C:\Projects\POE2AUTO`
> **Дата анализа:** 2026-08-19
> **Язык документации:** Русский

---

## 1. Что это за проект

**POE2AUTO** — это автоматизированный бот (автомат-фармилка) для игры **Path of Exile 2**.

Проект включает:
- Автоматический подбор лута (auto-loot pickup) через эмуляцию виртуального Xbox-контроллера (ViGEmBus)
- Автоматическое лечение (HP detection) на основе OCR с экрана + fallback на чтение orb-ов
- Различные скрипты автоматизации (quest.py, combat scripts)
- Оверлей на tkinter для отображения статуса бота
- Сканер смещений (AOB scanning) для memory-режима чтения
- Моб-детекшн через ReaderBridge

---

## 2. Структура проекта

```
C:\Projects\POE2AUTO\
├── bot.py                  # Главный контроллер бота (67 KB)
├── overlay.py              # Tkinter оверлей для UI
├── offset_scanner.py       # AOB сканер смещений
├── reader_detect.py        # ReaderBridge mob detection
├── entity_reader.py        # Чтение сущностей из памяти
├── quest.py                # Квестовая логика (242 KB)
├── utils_combat.py         # Combat utilities (133 KB)
├── poe_2_mapper.py         # Mapper для PoE2 (92 KB)
├── reader_bridge.py        # Reader bridge (23 KB)
├── monster_db.py           # SQLite телеметрия монстров
├── config.yaml             # Конфигурация бота
├── requirements.txt        # Зависимости Python
├── ANALYSIS.md             # Предыдущий анализ
├── ПРОБЛЕМЫ_И_РЕШЕНИЯ.md   # Known issues & solutions
├── УЛУЧШЕНИЯ.md            # Planned improvements
├── AGENTS.md               # Multi-agent documentation
└── ...                     # 243 Python файла, ~29,818 файлов всего
```

---

## 3. Архитектура

### Два режима обнаружения

1. **CV (Computer Vision) режим:**
   - `dxcam` / `mss` для захвата экрана
   - OpenCV цветовые маски (color masks) и template matching
   - OCR для определения HP

2. **Memory (Память) режим:**
   - Чтение памяти через `PROCESS_VM_READ`
   - AOB (Arrow of Bytes) сканирование через `PoE2ReaderLib.dll`
   - Чтение сущностей напрямую из памяти игры

### Основные компоненты

| Компонент | Назначение |
|-----------|------------|
| `bot.py` | Главный контроллер — инициализация виртуального Xbox/DualSense, захват экрана, обнаружение лута/HP, управление игрой |
| `overlay.py` | Tkinter UI оверлей для отображения статуса бота |
| `offset_scanner.py` | AOB сканер для поиска смещений в памяти |
| `reader_detect.py` | Обнаружение мобов через ReaderBridge |
| `entity_reader.py` | Чтение сущностей из памяти игры |
| `quest.py` | Логика квестов (242 KB — большой файл) |
| `utils_combat.py` | Утилиты для боя (133 KB) |
| `poe_2_mapper.py` | Маппер для Path of Exile 2 (92 KB) |
| `reader_bridge.py` | Мост для чтения памяти (23 KB) |
| `monster_db.py` | SQLite база телеметрии монстров |

---

## 4. Зависимости (dependencies)

```
vgamepad        # Эмуляция виртуального Xbox-контроллера (ViGEmBus)
hidapi          # HID API для работы с контроллерами
opencv-python   # Computer Vision (цветовые маски, template matching)
numpy           # Численные операции для CV
dxcam           # DirectX захват экрана
mss             # Screen capture
pywin32         # Windows API интеграция
PyYAML          # Парсинг конфигурации
Pillow          # Image processing
pynput          # Keyboard/Mouse control
pythonnet       # .NET interop
```

---

## 5. Конфигурация (config.yaml)

Основные параметры:
- **loot radius** — радиус подбора лута
- **HP thresholds** — пороги здоровья для лечения
- **Detection mode** — CV или Memory
- **Controller type** — Xbox или DualSense
- **Screen capture settings** — настройки захвата экрана

---

## 6. Ключевые риски (Risks)

### Критические
1. **Large file sizes** — `quest.py` (242 KB), `utils_combat.py` (133 KB), `poe_2_mapper.py` (92 KB) — эти файлы слишком велики и должны быть рефакторизованы
2. **243 Python файла** — слишком много модулей, высок риск конфликтов и дублирования кода
3. **~29,818 файлов** — общее количество файлов в проекте огромно; необходима очистка и организация

### Технические
1. **Memory reading через PROCESS_VM_READ** — может быть нестабильным при обновлениях игры
2. **AOB scanning** — может ломаться при патчах игры (смещения меняются)
3. **Tkinter overlay** — ограниченный UI фреймворк, может не справляться с сложными визуализациями
4. **Зависимость от ViGEmBus** — требует установки внешнего драйвера

### Безопасность
1. **Чтение памяти игры** — может быть расценено как чит (anti-cheat риски)
2. **Эмуляция контроллера** — также может быть обнаружена античит-системой

---

## 7. Сильные стороны (Strengths)

1. **Dual detection modes** — CV и Memory обеспечивают redundancy
2. **Auto-loot pickup** — практическая польза для геймплея
3. **Multi-agent architecture** — AGENTS.md описывает multi-agent подход
4. **SQLite telemetry** — monster_db.py собирает телеметрию для анализа
5. **Config-driven** — config.yaml позволяет настраивать без изменения кода
6. **Hybrid healing** — OCR + orb fallback для HP detection

---

## 8. Рекомендации (Recommendations)

### Приоритет P0 (Критические)
1. **Refactor large files** — разбить `quest.py`, `utils_combat.py`, `poe_2_mapper.py` на меньшие модули
2. **Очистка проекта** — убрать ~29,818 ненужных файлов, оставить только 243 Python файла и документацию
3. **Anti-cheat analysis** — провести аудит рисков банна от PoE2 anti-cheat

### Приоритет P1 (Важные)
4. **Upgrade overlay** — заменить tkinter на более современный UI фреймворк (PySide6, Dear PyGui)
5. **AOB stabilization** — добавить fallback механизмы для устойчивости к патчам
6. **Performance profiling** — замерить CPU/RAM usage каждого компонента

### Приоритет P2 (Желаемые)
7. **Multi-agent improvements** — улучшить AGENTS.md архитектуру
8. **Telemetry dashboard** — визуализация monster_db.py данных
9. **CI/CD pipeline** — автоматическое тестирование при каждом коммите

---

## 9. Статистика проекта

| Метрика | Значение |
|---------|----------|
| Python файлов | 243 |
| Всего файлов | ~29,818 |
| bot.py размер | 67 KB |
| quest.py размер | 242 KB |
| utils_combat.py размер | 133 KB |
| poe_2_mapper.py размер | 92 KB |
| reader_bridge.py размер | 23 KB |

---

*Синтезировано: 2026-08-19 | Язык: Русский | Тон: Неформальный ("брат")*
