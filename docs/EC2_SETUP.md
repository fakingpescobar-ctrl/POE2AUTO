# ExileCore2 + ShareData — Установка для POE2AUTO

## Что это даёт

ExileCore2 — C# фреймворк, который **инжектится внутрь процесса PoE2** и читает
игровую память напрямую (без knowledge оффсетов). Плагин **ShareData** отдаёт
данные по TCP (`127.0.0.1:50006`) в виде JSON — позиции мобов, HP, мана, зона.

POE2AUTO подключается к ShareData как TCP-клиент и получает готовые данные.
**Не нужен Admin. Не ломается на патчах. Не нужно знать оффсеты.**

---

## Архитектура

```
┌─────────────────────────────────────────────────┐
│  PoE2 (PathOfExile.exe)                         │
│  ┌───────────────────────────────────────────┐  │
│  │  ExileCore2 (инжектирован Loader.exe)     │  │
│  │  ┌─────────────────────────────────────┐  │  │
│  │  │  ShareData плагин                   │  │  │
│  │  │  TCP сервер :50006                  │  │  │
│  │  │  Отдаёт JSON: entities, HP, zone    │  │  │
│  │  └──────────────┬──────────────────────┘  │  │
│  └─────────────────┼─────────────────────────┘  │
└────────────────────┼────────────────────────────┘
                     │ TCP localhost
┌────────────────────┼────────────────────────────┐
│  POE2AUTO (Python) │                            │
│  ec2_client.py ←───┘  TCP-клиент                │
│  ec2_detect.py    →  EC2MobDetector             │
│  bot.py           →  оркестратор (автоатака)    │
└─────────────────────────────────────────────────┘
```

---

## Шаг 1: Установить .NET 8 SDK

Скачать: https://dotnet.microsoft.com/en-us/download/dotnet/8.0
Выбрать **SDK x64** для Windows.

Проверка:
```
dotnet --version
```

---

## Шаг 2: Установить VC++ 2015 Redistributable

Скачать: https://www.microsoft.com/en-US/download/details.aspx?id=48145
(нужен для нативных DLL ExileCore2)

---

## Шаг 3: Получить ExileCore2

Вариант A — взять из poe_bot (уже есть локально):
```
C:\Projects\poe_bot\0_guest\ExileCore2\
```

Вариант B — скачать с GitHub:
- https://github.com/exCore2/ (репозиторий ExileCore2)
- Discord: https://discord.gg/eMmeSyqEA3 (последние билды)

Скопировать всю папку `ExileCore2/` в удобное место, например:
```
C:\ExileCore2\
```

---

## Шаг 4: Установить плагин ShareData

ShareData — C# плагин, который поднимает TCP-сервер внутри игры.

Исходники уже есть локально:
```
C:\Projects\poe_bot\0_guest\Plugins\Source\ShareData\
  ├── ShareData.cs
  ├── ShareData.csproj
  ├── ShareDataSettings.cs
  └── Structs.cs
```

### Сборка плагина:

```powershell
cd C:\Projects\poe_bot\0_guest\Plugins\Source\ShareData
dotnet build -c Release
```

Результат: `bin/Release/net8.0/ShareData.dll`

### Установка в ExileCore2:

Скопировать `ShareData.dll` в папку Plugins:
```
<ExileCore2>/Plugins/ShareData.dll
```

---

## Шаг 5: Запуск

### 5.1 Запустить PoE2
Обычный запуск Path of Exile 2 (Steam или standalone).

### 5.2 Запустить ExileCore2 Loader
```
<ExileCore2>/Loader.exe
```
Loader инжектируется в процесс PoE2. В логе ExileCore2 должен появиться
ShareData плагин.

### 5.3 Проверить TCP-сервер
В игре (персонаж должен быть в зоне, не в меню):
```
python ec2_test.py
```

Должно показать:
```
✅ Подключено!
Zone: MapWorlds/...  ⚔️ IN COMBAT ZONE
Player: HP=90%  Mana=80%
Mobs total: 5 | in range: 2
```

### 5.4 Запустить бота
```
python bot.py
```

В config.yaml:
```yaml
auto_attack:
  use_memory: ec2    # режим ExileCore2
```

---

## Проверка порта

Если ec2_test.py не подключается:
```powershell
netstat -an | findstr 50006
```
Должен показать `LISTENING` или `ESTABLISHED`.

Если порт свободен — ShareData плагин не запущен. Проверь:
1. Loader.exe запущен?
2. В консоли ExileCore2 нет ошибок загрузки ShareData?
3. Персонаж в игре (не в меню)? — ShareData отвечает только in-game.

---

## Формат данных ShareData

ShareData отдаёт JSON по запросу `/getData?type=partial\n`:

```json
{
  "g_s": 20,                    // game state (20 = in-game)
  "area_raw_name": "MapWorlds/...",  // код зоны
  "ah": 12345,                  // area hash
  "pi": {                       // player info
    "gp": [100, 200],           // grid position [x, y]
    "l": [5000, 4500, 0, 200, 150, 0, 3000, 2800, 0],  // life component
    "b": [],                    // buffs
    "lv": 95                    // level
  },
  "awake_entities": [           // список сущностей
    {
      "i": 1234,                // entity id
      "et": "m",                // entity type: m/p/c/t/n/o
      "p": "/Monsters/Test",    // metadata path
      "h": 1,                   // is_hostile (0/1)
      "gp": [110, 200],         // grid position [x, y]
      "wp": [110, 200, 0],      // world position [x, y, z]
      "l": [1000, 800, 0, ...], // life: [hp_max, hp_cur, hp_res, es..., mana...]
      "r": "normal",            // rarity: normal/magic/rare/unique
      "ia": 1,                  // is_alive
      "t": 1                    // is_targetable
    }
  ]
}
```

Life component format (9 элементов):
```
[hp_max, hp_cur, hp_res, es_max, es_cur, es_res, mana_max, mana_cur, mana_res]
```

---

## Troubleshooting

### "Connection refused" на :50006
- ExileCore2 не инжектировался → проверь Loader.exe логи
- ShareData плагин не загружен → проверь папку Plugins/
- Игра в меню/загрузке → зайди в игру персонажем

### Данные приходят, но мобов нет
- `area_raw_name` содержит "hideout" → бот не атакует в убежище (by design)
- Все мобы `is_hostile=0` → это дружественные NPC/миньоны
- `game_state != 20` → персонаж не в игре

### HP показывает мусор
- Life component `[0,0,0,0,0,0,0,0,0]` → сущность без Life компонента
- Проверь через ec2_test.py — должно показывать реальные HP

---

## Ссылки

- ExileCore2 GitHub: https://github.com/exCore2/
- Plugin Browser: https://excore2.github.io/PluginBrowser
- Discord: https://discord.gg/eMmeSyqEA3
- GameOffsets2 (оффсеты от сообщества): обновляется через PluginUpdater
