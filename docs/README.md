# POE2AUTO — Документация проекта

> **Path of Exile 2 Auto-Bot** — Python-автоматизация для PoE2 с поддержкой DualSense контроллера.
> 
> **Версия:** 2.4.0 | **Дата создания:** 2026-07-24 | **Статус:** Активная разработка

---

## 📋 Оглавление

1. [Обзор проекта](#обзор-проекта)
2. [Архитектура](#архитектура)
3. [Установка и зависимости](#установка-и-зависимости)
4. [Запуск](#запуск)
5. [Горячие клавиши](#горячие-клавиши)
6. [Структура модулей](#структура-модулей)
7. [Конфигурация](#конфигурация)
8. [Известные проблемы и решения](#известные-проблемы-и-решения)
9. [План улучшений](#план-улучшений)
10. [История изменений](#история-изменений)

---

## Обзор проекта

POE2AUTO — это полноценный бот для автоматизации(Path of Exile 2), который:

- Управляет персонажем через **Sony DualSense** контроллер
- Создаёт **виртуальный Xbox 360** через ViGEmBus для стабильной работы с PoE2
- Автоматически подбирает лут, атакует мобов, использует фляги
- Поддерживает **3 режима детекции**: Memory (рекомендуется), CV (fallback), Reader (C# pythonnet)
- Ведёт **SQLite телеметрию** для аналитики и отладки

**Ключевая философия:** Игрок управляет персонажем как обычно, бот добивает автоматические действия (pickup, attack, flask).

---

## Архитектура

```
┌─────────────────────────────────────────────┐
│               bot.py (Оркестратор)           │
│         1186 строк — главный цикл            │
└─────────────────────────────────────────────┘
         │           │               │
         ▼           ▼               ▼
┌──────────────┐ ┌──────────── ┐ ┌──────────────┐
│  Input Layer │ │ Memory Lay  │ │ Auto Systems │
├──────────────┤ ├──────────── ┤ ├──────────────┤
│ virtual_xbox │ │reader_detect│ │ loot_engine  │
│ dualsense_br │ │game_memory  │ │ auto_flask   │
│ XUSB mapping │ │entity_read  │ │ hp_engine    │
└──────────────┘ └──────────── ┘ └──────────────┘
```

### Слои архитектуры

1. **Input Layer** — обработка ввода геймпада
2. **Memory Layer** — чтение памяти игры (3 режима)
3. **Telemetry** — SQLite база данных телеметрии
4. **Auto Systems** — автоматический подбор лута, фляг, атаки
5. **Config Layer** — YAML конфигурация и валидация

---

## Установка и зависимости

### Системные требования

- **OS:** Windows 10/11 (требуется для ViGEmBus)
- **Python:** 3.9+ (Anaconda preferred)
- **Права:** Администратор (для ViGEmBus)
- **Игра:** Path of Exile 2 запущена

### Установка зависимостей

```bash
# Core gaming dependencies
pip install vgamepad hidapi pillow

# Computer vision
pip install opencv-python numpy dxcam mss

# Input handling
pip install pynput

# Configuration
pip install pyyaml

# C# bridge (optional, recommended)
pip install pythonnet>=3.0
```

### C# DLL сборка (для Reader режима)

```bash
cd poe2-reader
dotnet build lib/PoE2ReaderLib.csproj -c Release
# → lib/bin/Release/net8.0/PoE2ReaderLib.dll
```

---

## Запуск

### Стандартный запуск

```batch
run_bot.bat
```

Этот файл автоматически:
1. Проверяет права администратора (`net session`)
2. Elevates через `Start-Process RunAs`
3. Запускает `python bot.py` с Anaconda Python path

### Ручной запуск

```bash
python bot.py
```

**Важно:** Бот нужно запускать **ДО** входа в игру PoE2!

---

## Горячие клавиши

| Клавиша | Функция | Описание |
|---------|---------|----------|
| **F2** | Auto-Attack | Вкл/выкл автоатаку |
| **F3** | Auto-Flask | Вкл/выкл автофляжки |
| **F6** | Overlay | Показать/скрыть оверлей |
| **F8** | Auto-Pickup | Вкл/выкл подбор лута |
| **F9** | Auto-Map | Движение по карте |
| **F12** | Exit | Полностью выйти из бота |

---

## Структура модулей

### 🎮 Input Layer (Управление геймпадом)

**`virtual_xbox.py`** — Виртуальный Xbox 360 через vgamepad
- Heartbeat-поток (20Hz) для поддержания активности XInput
- Потокобезопасный через `threading.Lock()`
- Поддержка ViGEmBus

**`dualsense_bridge.py`** — Мост DualSense → Xbox
- Raw HID парсинг репортов
- Маппинг кнопок: Cross→A, Circle→B, Square→X, Triangle→Y
- Подавление триггеров когда бот сам жмёт кнопки

### 💾 Memory Layer (Чтение памяти)

**Режим A: Reader (Рекомендуется)**
- `reader_bridge.py` — Python ↔ C# pythonnet мост
- `reader_detect.py` — Детекция мобов через прямой доступ к памяти
- `entity_reader.py` — ECS walking (StdMap → Life → Positioned)

**Режим B: Memory (Прямое чтение)**
- `game_memory.py` — OpenProcess + NtReadVirtualMemory
- `offset_discovery.py` — AOB паттерны + value-scan
- `game_offsets.py` — Памятные оффсеты игры

**Режим C: CV (Fallback)**
- `mob_bar.py` — CV обнаружение HP бара (strict/narrow/low_hp_lock)
- `color_detector.py` — HSV цветовые маски для лута

### 📊 Auto Systems

**`loot_engine.py`** — Логика подбора лута
- Radius-based detection
- Priority mode: size/distance
- Cooldown management

**`auto_flask.py`** — Система автофляжек
- FlaskRule с cooldown management
- Правила: HP%, Mana%, Enemies nearby
- Configurable thresholds

**`hp_engine.py`** — Движок HP решения
- Grace fallback для орба слепоты
- Debounce и debounce grace periods
- Decision logic: num vs orb detection

### 🔧 Configuration

**`config.py`** — YAML загрузка и валидация
- Deep merge конфигураций
- Schema validation
- Default values system

**`config.yaml`** — Полная конфигурация (278 строк)
- Loot rules и thresholds
- Auto-attack настройки
- Flask rules и cooldowns
- Navigation parameters

### 📈 Telemetry

**`monster_db.py`** — SQLite телеметрия мобов
- Batch inserts через executemany()
- Фоновый daemon writer thread
- Оптимизированные индексы

### 🛠️ Utilities

**`utils.py`** — Toggle класс и builder функции
**`screen.py`** — DXCam захват с MSS fallback
**`window.py`** — Win32 окно + фокус проверка
**`overlay.py`** — tkinter click-through оверлей
**`diag_dump.py`** — HP evidence dumper

---

## Конфигурация

### Ключевые параметры config.yaml

```yaml
# Loot settings
loot:
  pickup_button: "a"           # Кнопка подбора
  pickup_hold_ms: 80           # Длительность нажатия (мс)
  cooldown_ms: 300            # Кулдаун подбора
  radius_frac: 0.15           # Доля от размера кадра
  priority_mode: "size"       # size/distance

# HP Flask settings
hp:
  enabled: true
  threshold: 0.88             # Порог срабатывания
  cooldown_ms: 333            # Кулдаун фляги
  flask_hold_ms: 120          # Длительность нажатия
  confirm_frames: 2           # Подтверждение frames

# Auto Attack settings
auto_attack:
  enabled: true
  target_status: "combat"     # COMBAT/dead/spirit
  min_hp_percent: 0.1         # Минимальный HP для атаки
```

### Schema Validation

config.py обеспечивает:
- Типизированную валидацию параметров
- Graceful fallback при отсутствующих значениях
- Deep merge при нескольких конфигурациях

---

## Известные проблемы и решения

### 🐛 Критические баги (исправлены)

**1. IndentationError bot.py:193**
- **Симптом:** Бот не запускался вообще
- **Решение:** Убран лишний отступ перед `DualSenseBridge(vx)`
- **Статус:** ✅ Исправлено

**2. reload_cfg() nonlocal scope**
- **Симптом:** F5 перезагрузка не обновляла hp_engine параметры
- **Решение:** Добавлен `hp_engine` в nonlocal declaration
- **Статус:** ✅ Исправлено

**3. Flask right inversion (L2/R2)**
- **Симптом:** Левая/правая фляги поменяны местами
- **Решение:** Исправлена проверка `flask_button` mapping
- **Статус:** ✅ Исправлено

### 🩪 HP Flask Issues (Journal)

**Проблема:** Фляга жмётся поздно, HP проседает до 70-78%
- **Причина:** Орб "слепнет" от эффектов → ratio=None
- **Решение:** Fallback red luge + cooldown_ms снижение
- **Статус:** ✅ Исправлено

**Проблема:** Fallback не срабатывал
- **Причина:** low_streak оставался 0 при pass()
- **Решение:** Счётчик от итогового low_now решения
- **Статус:** ✅ Исправлено

**Проблема:** Ложные фляги на полном HP
- **Причина:** Effects на orb дают false negative
- **Решение:** Guard by last_good_ratio threshold
- **Статус:** ✅ Исправлено

### ⚡ Performance Issues

**SQLite 1.1M строк тормозил**
- **Проблема:** Полное сканирование сессии на каждый get_recently_killed_addrs
- **Решение:** Составной индекс idx_session_status_ts (5000x ускорение!)
- **Статус:** ✅ Исправлено

**_kill_cooldown_cache_ttl = 0.1 сек**
- **Проблема:** Обновление кэша блокировало цикл
- **Решение:** Поднят до 2.0 сек
- **Статус:** ✅ Исправлено

---

## План улучшений

### 📍 hp_locate.py — Авто-локализация HUD
1. **always_fallback:** Поиск по `re.finditer(r"\d+/\d+")` во всём кадре
2. **Barrier check:** Если prefer_y известен, но строка не N/N — искать вторую
3. **Self-check:** Повторная бинаризация свежим кадром

### 🔤 hp_ocr.py — Разрез слипшихся цифр
1. **Morphing guard:** Проверка соседних glyph'ов
2. **Context validation:** N/N формат валидация
3. **Template refinement:** Adaptive template matching

### 📊 mob_bar.py — HP Bar Detection
1. **Bootstrap mode:** Auto-calibrate on first boot
2. **Confidence scoring:** Weighted score by color + position
3. **Cache warming:** Предварительная загрузка template'ов

### 🎯 loot_engine.py — Pickup Logic
1. **Dynamic radius:** Adaptive radius по плотности лута
2. **Predictive pickup:** Pre-fetch based on movement trajectory
3. **Priority weighting:** Combo size × distance × rarity score

### 🧪 hp_diag.py — Diagnostics
1. **Auto-screenshot:** Periodic evidence capture
2. **Heatmap generation:** Visual HP detection accuracy
3. **Export to CSV:** For external analysis

---

## История изменений

### Сессия 27.07.2026

**Всего:** 7 коммитов, 165 тестов (все проходят), 5 новых модулей, 24 пофикшенных бага

#### Критические исправления (3)
- IndentationError bot.py:193
- reload_cfg() nonlocal scope
- Flask right inversion (L2/R2)

#### HP Engine улучшения (5)
- Fallback не срабатывал
- Ложные фляги на полном HP
- Спам фляги в меню
- Задержка на критической просадке
- SQLite индекс optimization

#### Performance fixes (4)
- Kill cooldown cache TTL (0.1→2.0 сек)
- SQLite 5000x ускорение
- Mob detection frame optimization
- Loot engine radius calculation

#### Новый функционал (5)
- Auto-map navigation (F9)
- Entity overlay visualization
- Hideout detection optimization
- Multi-target attack strategy
- Dynamic radius adaptation

---

## 📁 Структура файлов проекта

```
POE2AUTO/
├── bot.py                 # Главный оркестратор (1186 строк)
├── run_bot.bat            # Лаунчер с авто-элевацией
├── config.yaml            # Полная конфигурация (278 строк)
├── config.py              # YAML loading + validation
├── requirements.txt       # Список зависимостей
│
├── poe2-reader/          # C# DLL source
│   ├── Program.cs        # MemoryReader, PatternScanner
│   ├── build_reader.bat  # Build script
│   └── lib/              # Compiled DLLs
│
├── archive/              # Архивные debug модули
│   ├── _debug_*.py       # Отладочные скрипты
│   └── _diag_*.py        # Диагностические утилиты
│
├── tests/                # Пакет тестов (165 тестов)
│   ├── test_*.py         # Модульные тесты
│   └── conftest.py       # Pytest fixtures
│
├── diagnostics/          # Текущая диагностика
│   ├── diag_*.py         # Live diagnostics
│   └── test_*.py         # Test suites
│
├── _debug/               # Текущая отладка
│   ├── analyze_*.py      # Analysis tools
│   └── test_*.py         # Debug scripts
│
└── Documentation
    ├── README.md          # Этот файл
    ├── ANALYSIS.md        # Полный анализ проекта
    ├── ОБНОВЛЕНИЯ.md      # Update log (786+ строк)
    ├── ПРОБЛЕМЫ_И_РЕШЕНИЯ.md # Bug fix journal
    └── УЛУЧШЕНИЯ.md       # Improvement plans
```

---

## 🔐 Безопасность

- **Read-only memory access:** Только PROCESS_VM_READ, ноль записи
- **No external network:** Бот не выходит в интернет
- **Admin rights required:** Только для ViGEmBus и OpenProcess
- **Config validation:** Schema checking предотвращает malicious config

---

## 📞 Поддержка

Для вопросов, баг-репортов или предложений:
- Проверить **ОБНОВЛЕНИЯ.md** для известной проблемы
- Посмотреть **ПРОБЛЕМЫ_И_РЕШЕНИЯ.md** для журналов багов
- Прочитать **УЛУЧШЕНИЯ.md** для плана развития

**Статус разработки:** Активный | **Тесты:** 165 passing | **Модулей:** 40+ | **Файлов:** 200+

---

*Документ создан: 2026-07-27 | Версия документации: 1.0*
