"""ReaderBridge — Python-обёртка над C# PoE2ReaderLib.dll через pythonnet.

Один процесс: читает память PoE2 напрямую через .NET сборку, вызывается из Python.

Архитектура:
    PoE2 (PathOfExileSteam.exe)
        ▲
        │ ReadProcessMemory (PROCESS_VM_READ)
    PoE2ReaderLib.dll (C#, public классы: MemoryReader, PatternScanner, GameReader)
        ▲
        │ pythonnet (clr / coreclr runtime)
    ReaderBridge (этот файл, Python)
        ▲
        │
    ReaderMobDetector → bot.py

Сборка DLL (после правок poe2-reader/Program.cs):
    cd C:\\Projects\\poe2-reader
    dotnet build lib\\PoE2ReaderLib.csproj -c Release
    → lib\\bin\\Release\\net8.0\\PoE2ReaderLib.dll

Зависимости: pythonnet>=3.0 (pip install pythonnet).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from game_offsets import (Life as _LifeOff, VitalStruct as _VitalOff,
                          ObjectMagicProperties, MonsterComponent)

_log = logging.getLogger("poe2auto.reader_bridge")

# Путь к скомпилированной DLL (относительно этого файла → переносимо при переносе POE2AUTO).
# Исходники C# и csproj лежат в подпапке poe2-reader/ рядом с проектом.
# См. build_reader.bat для пересборки DLL. Переопределить путь можно переменной POE2READER_DLL.
_DEFAULT_DLL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "poe2-reader", "lib", "bin", "Release", "net8.0", "PoE2ReaderLib.dll",
)
DLL_PATH = os.environ.get("POE2READER_DLL", _DEFAULT_DLL)
GAME_PROCESS = "PathOfExileSteam"
GAME_MODULE = "PathOfExileSteam.exe"

# Минимальный интервал между попытками инициализации (PoE2 может быть не запущен).
REINIT_INTERVAL_S = 2.0

# .NET runtime грузится один раз на процесс.
_pyNetLoaded = False


def _ensure_pydotnet():
    """Лениво загрузить coreclr runtime (один раз за процесс)."""
    global _pyNetLoaded
    if _pyNetLoaded:
        return
    import pythonnet
    pythonnet.load("coreclr")
    _pyNetLoaded = True


class ReaderBridge:
    """Прямой мост Python ↔ C# PoE2ReaderLib.

    Один процесс, ноль TCP.

    Жизненный цикл:
        bridge = ReaderBridge()
        bridge.initialize()         # найти PoE2, OpenProcess, AOB scan GSO
        bridge.get_game_state()     # прямой вызов GameReader.ReadGameState()
        bridge.shutdown()           # CloseHandle
    """

    def __init__(self):
        _ensure_pydotnet()
        from System.Reflection import Assembly
        Assembly.LoadFrom(DLL_PATH)
        from PoE2Reader import Native, MemoryReader, PatternScanner, GameReader
        from System.Diagnostics import Process
        from System import IntPtr

        self._Native = Native
        self._MemoryReader = MemoryReader
        self._PatternScanner = PatternScanner
        self._GameReader = GameReader
        self._Process = Process
        self._IntPtr = IntPtr

        self._handle = None
        self._reader = None
        self._available = False
        self._pid = 0
        self._last_init_attempt = 0.0
        # Кэш Life-компонентов (addr → life_addr) и меты мобов (addr → (life, rarity, is_boss)).
        self._life_cache = {}
        self._life_cache_ts = {}
        self._mob_meta_cache = {}
        self._mob_meta_cache_ts = {}

    @property
    def available(self) -> bool:
        """True если инициализирован и можно читать данные."""
        return self._available

    @property
    def pid(self) -> int:
        return self._pid

    # ──────────────────────────────────────────────
    # Инициализация
    # ──────────────────────────────────────────────
    def _find_pid(self) -> int:
        """Найти PID PathOfExileSteam (0 если не запущен)."""
        procs = self._Process.GetProcessesByName(GAME_PROCESS)
        try:
            if len(procs) == 0:
                return 0
            return procs[0].Id
        finally:
            for p in procs:
                p.Dispose()

    def initialize(self, force: bool = False) -> bool:
        """Попытка инициализации: найти PoE2, OpenProcess, AOB scan GSO.

        Повторяемая (idempotent): пропускает если уже инициализирован,
        или если попытка была недавно (кроме force=True). Возвращает True
        если готов к чтению данных.
        """
        if self._available and not force:
            return True
        now = time.perf_counter()
        if not force and now - self._last_init_attempt < REINIT_INTERVAL_S:
            return False
        self._last_init_attempt = now

        pid = self._find_pid()
        if pid == 0:
            _log.debug("PoE2 (%s) не запущен", GAME_PROCESS)
            return False

        flags = self._Native.PROCESS_VM_READ | self._Native.PROCESS_QUERY_INFORMATION
        handle = self._Native.OpenProcess(flags, False, pid)
        if handle == self._IntPtr.Zero:
            _log.warning("OpenProcess FAILED (нужен admin?) pid=%d", pid)
            return False

        mr = self._MemoryReader(handle)
        scanner = self._PatternScanner(mr)

        mod = scanner.GetModule(pid, GAME_MODULE)
        if mod is None:
            _log.warning("Module %s не найден в pid=%d", GAME_MODULE, pid)
            self._Native.CloseHandle(handle)
            return False

        # ValueTuple<ulong, int>?: извлекаем base/size
        base_addr = int(mod.Item1)
        size = int(mod.Item2)

        gso = int(scanner.FindGsoSlot(base_addr, size))
        if gso == 0:
            _log.warning("GSO AOB pattern не найден (pid=%d)", pid)
            self._Native.CloseHandle(handle)
            return False

        gr = self._GameReader(mr)
        gr.SetGso(gso)
        gr.SetModuleBase(base_addr)

        self._handle = handle
        self._reader = gr
        self._pid = pid
        self._available = True
        _log.info("ReaderBridge готов: pid=%d GSO=0x%X IGS=0x%X",
                  pid, gso, int(gr.Igs()))
        return True

    def _ensure(self) -> bool:
        """Ленивая авто-инициализация перед чтением данных."""
        if self._available:
            return True
        return self.initialize()

    # ──────────────────────────────────────────────
    # Чтение данных (прямые вызовы C#)
    # ──────────────────────────────────────────────
    def get_game_state(self) -> int:
        """game_state: 0 = НЕ в игре (меню/char-select/loading), 20 = в игре."""
        if not self._ensure():
            return 0
        return int(self._reader.ReadGameState())

    def get_in_game(self) -> bool:
        """True если персонаж в игре (game_state != 0)."""
        return self.get_game_state() != 0

    def get_area_info(self) -> dict:
        """Информация о зоне: {'code': internal, 'name': readable, 'hash': uint}."""
        if not self._ensure():
            return {"code": "", "name": "", "hash": 0}
        # ReadAreaStrings() возвращает C# ValueTuple<String,String> — НЕ питоновский
        # кортеж. pythonnet не умеет его распаковывать; обращаемся через .Item1/.Item2.
        t = self._reader.ReadAreaStrings()
        return {
            "code": str(t.Item1),
            "name": str(t.Item2),
            "hash": int(self._reader.ReadAreaHash()),
        }

    def get_player_grid_pos(self) -> tuple:
        """Позиция игрока в grid-координатах [gx, gy]. (0,0) если нет данных."""
        if not self._ensure():
            return (0, 0)
        # ReadPlayerPos3() возвращает C# ValueTuple<float,float,float> — .Item1/.Item2.
        t = self._reader.ReadPlayerPos3()
        wx = float(t.Item1)
        wy = float(t.Item2)
        if abs(wx) <= 1.0:
            return (0, 0)
        grid_to_world = 250.0 / 23.0
        return (int(wx / grid_to_world), int(wy / grid_to_world))

    def _read_vitals(self, life_addr: int) -> tuple:
        bts = bytes(self._reader.Reader.ReadBytes(life_addr + _LifeOff.HEALTH, 0x40))
        hp_max = int.from_bytes(bts[_VitalOff.MAX: _VitalOff.MAX + 4], "little")
        hp_cur = int.from_bytes(bts[_VitalOff.CURRENT: _VitalOff.CURRENT + 4], "little")
        return hp_max, hp_cur

    def _read_life_hp(self, addr: int) -> tuple:
        """Прочитать (hp_max, hp_cur, mp_max, mp_cur) сущности через Life VitalStruct.

        Оффсеты: Life+0x1B0 → VitalStruct {Max@0x2C, Current@0x30} (HP),
        Life+0x208 → VitalStruct (Mana). Оба читаются ОДНИМ куском 0x98 байт
        (MANA+0x34 - HEALTH = 0x98) — 1 вызов ReadBytes вместо 2.
        Кэшируется ТОЛЬКО адрес Life-компонента (ResolveComponents дорог), а сами
        значения HP/Mana читаются каждый вызов — чтобы смерть моба (hp_cur→0)
        виделась сразу, а не через TTL кэша.
        """
        now = time.monotonic()
        life = self._life_cache.get(addr)
        if life is None or now - self._life_cache_ts.get(addr, 0.0) > 1.0:
            try:
                cdict = self._reader.ResolveComponents(addr)
                if "Life" not in cdict.Keys:
                    return (0, 0, 0, 0)
                life = int(cdict["Life"])
                self._life_cache[addr] = life
                self._life_cache_ts[addr] = now
            except Exception:
                return (0, 0, 0, 0)
        try:
            base = life + _LifeOff.HEALTH
            # 0x98 = MANA(0x208) + SIZE(0x34) - HEALTH(0x1B0)
            bts = bytes(self._reader.Reader.ReadBytes(base, 0x98))
            hp_max = int.from_bytes(bts[_VitalOff.MAX: _VitalOff.MAX + 4], "little")
            hp_cur = int.from_bytes(bts[_VitalOff.CURRENT: _VitalOff.CURRENT + 4], "little")
            mana_rel = _LifeOff.MANA - _LifeOff.HEALTH  # 0x58
            mp_max = int.from_bytes(bts[mana_rel + _VitalOff.MAX: mana_rel + _VitalOff.MAX + 4], "little")
            mp_cur = int.from_bytes(bts[mana_rel + _VitalOff.CURRENT: mana_rel + _VitalOff.CURRENT + 4], "little")
        except Exception:
            return (0, 0, 0, 0)
        if len(self._life_cache) > 2048:
            self._life_cache.clear()
            self._life_cache_ts.clear()
        return (hp_max, hp_cur, mp_max, mp_cur)

    def _read_mob_meta(self, addr: int) -> tuple:
        """Прочитать мету моба: (life_addr, rarity, is_boss).

        rarity: ObjectMagicProperties.RARITY (0=Normal..3=Unique).
        is_boss: MonsterComponent.IS_BOSS (не всегда проставлен у боссов —
        босс надёжно ловится по rarity == Unique).
        Кэшируется только адреса компонентов (ResolveComponents дорог).
        """
        now = time.monotonic()
        cached = self._mob_meta_cache.get(addr)
        if cached is not None and now - self._mob_meta_cache_ts.get(addr, 0.0) > 1.0:
            del self._mob_meta_cache[addr]
            cached = None
        if cached is None:
            try:
                cdict = self._reader.ResolveComponents(addr)
                keys = cdict.Keys
                life = int(cdict["Life"]) if "Life" in keys else 0
                rarity = 0
                if "ObjectMagicProperties" in keys:
                    rarity = int.from_bytes(bytes(self._reader.Reader.ReadBytes(
                        int(cdict["ObjectMagicProperties"]) + ObjectMagicProperties.RARITY, 4)), "little")
                is_boss = 0
                if "Monster" in keys:
                    is_boss = int.from_bytes(bytes(self._reader.Reader.ReadBytes(
                        int(cdict["Monster"]) + MonsterComponent.IS_BOSS, 1)), "little")
            except Exception:
                return (0, 0, 0)
            cached = (life, rarity, is_boss)
            self._mob_meta_cache[addr] = cached
            self._mob_meta_cache_ts[addr] = now
        return cached

    def read_entities_raw(self) -> list:
        """Список сущностей (сырой формат, как из getData.awake_entities).

        Каждая: {'addr', 'path', 'grid_pos': [gx,gy], 'world_pos': [wx,wy,z],
                 'entity_type', 'hostile', 'alive', 'z', 'render_name', 'life': [...]}.
        'alive': Life-компонент жив (vtable != 0) И флаг смерти Life+0x10 == 0.
        'z': мировая Z (Render+0x140) — фильтр по Z-слою отсекает
        аурные даемоны модов/подводных тварей (они на другом слое).
        HP (life) пока не читается — поле life = [0,0,...] (открытая задача).
        """
        if not self._ensure():
            return []
        # GameReader.ReadEntities() → List<(ulong, string, float x, float y, float z, bool alive)>
        raw = self._reader.ReadEntities()
        grid_to_world = 250.0 / 23.0
        result = []
        for ent in raw:
            path = str(ent.Item2)
            ex = float(ent.Item3)
            ey = float(ent.Item4)
            ez = float(ent.Item5)
            alive = bool(ent.Item6)
            if ex == 0 and ey == 0 and path and "Characters" not in path:
                continue
            etype, hostile, attackable = self._classify(path)
            life = [0, 0, 0, 0, 0, 0, 0, 0, 0]
            rarity = 0
            is_boss = False
            if etype == "Player":
                hm, hc, mp_m, mp_c = self._read_life_hp(int(ent.Item1))
                life = [hm, hc, mp_m, mp_c, 0, 0, 0, 0, 0]
            elif etype == "m":
                life_addr, rarity, is_boss = self._read_mob_meta(int(ent.Item1))
                if life_addr:
                    hm, hc = self._read_vitals(life_addr)
                    life = [hm, hc, 0, 0, 0, 0, 0, 0, 0]
            result.append({
                "addr": int(ent.Item1),
                "path": path,
                "grid_pos": [
                    int(ex / grid_to_world) if ex else 0,
                    int(ey / grid_to_world) if ey else 0,
                ],
                "world_pos": [int(ex), int(ey), int(ez)],
                "entity_type": etype,
                "hostile": hostile,
                "attackable": attackable,
                "targetable": 1,
                "alive": alive,
                "z": ez,
                "render_name": self._render_name(path),
                "life": life,
                "rarity": rarity,
                "is_boss": bool(is_boss),
            })
        return result

    @staticmethod
    def _classify(path: str) -> tuple:
        """Определить тип сущности по metadata path (миррор логики Program.cs)."""
        if "Characters" in path:
            return ("Player", 0, 0)
        if "Monsters" in path:
            return ("m", 1, 1)
        if "Chests" in path:
            return ("c", 0, 0)
        if "NPC" in path:
            return ("n", 0, 0)
        if "Effects" in path:
            return ("fx", 0, 0)
        if "Projectiles" in path:
            return ("p", 0, 0)
        if "Terrain" in path:
            return ("t", 0, 0)
        if "MiscellaneousObjects" in path:
            return ("mo", 0, 0)
        return ("o", 0, 0)

    @staticmethod
    def _render_name(path: str) -> str:
        """Имя сущности из metadata path (последний сегмент до @)."""
        last_slash = path.rfind("/")
        name = path[last_slash + 1:] if last_slash >= 0 else path
        at = name.find("@")
        return name[:at] if at > 0 else name

    # ──────────────────────────────────────────────
    # Завершение
    # ──────────────────────────────────────────────
    def shutdown(self):
        """Освободить handle процесса."""
        if self._handle is not None:
            self._Native.CloseHandle(self._handle)
            self._handle = None
        self._available = False
        self._reader = None
        self._pid = 0
        _log.info("ReaderBridge остановлен")

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Smoke-test:  python reader_bridge.py
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    print("=== ReaderBridge smoke-test ===")
    print("DLL:", DLL_PATH)
    b = ReaderBridge()
    ok = b.initialize()
    print("available=%s pid=%s" % (b.available, b.pid))
    if ok:
        print("game_state=%d (0=меню, 20=в игре)" % b.get_game_state())
        area = b.get_area_info()
        print("area: code=%r name=%r hash=%#x" % (area["code"], area["name"], area["hash"]))
        gp = b.get_player_grid_pos()
        print("player grid_pos=%s" % (gp,))
        ents = b.read_entities_raw()
        print("entities: %d шт." % len(ents))
        for e in ents[:5]:
            print("  %3s h=%d %-25s gp=%s" % (e["entity_type"], e["hostile"], e["render_name"], e["grid_pos"]))
        if len(ents) > 5:
            print("  ... ещё %d" % (len(ents) - 5))
    b.shutdown()
