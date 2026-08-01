"""Детектор мобов через ReaderBridge (Python↔C# pythonnet мост, один процесс).

Drop-in замена EC2MobDetector / MemoryMobDetector: тот же интерфейс, но данные
читаются напрямую из памяти PoE2 через PoE2ReaderLib.dll, БЕЗ TCP и БЕЗ отдельного
PoE2Reader.exe.

Преимущества:
  - ОДИН процесс: bot.py сам читает память, не нужно запускать отдельное приложение
  - НЕ зависит от внешних сервисов (нет ExileCore2/ShareData/TCP :50006)
  - Патчи PoE2: AOB scan GSO обновляется в C# коде (как и в PoE2Reader.exe)

Требования:
  - pythonnet>=3.0 (pip install pythonnet)
  - Скомпилированная PoE2ReaderLib.dll (C:\\Projects\\poe2-reader\\lib\\bin\\Release)
  - PoE2 запущен (PathOfExileSteam.exe)
  - Бот запущен с правами admin (OpenProcess PROCESS_VM_READ)

Ограничение (временное): HP сущностей пока не читается (life=[0,...]).
Все враждебные мобы считаются «живыми» (hp_ratio=1.0), чтобы авто-атака работала.
Как только в ReaderBridge добавится чтение Life-компонента — hp_ratio станет точным.
"""
from __future__ import annotations

import logging
import math
import time
from typing import List, Optional, Tuple

from entity_reader import EntityCategory, Rarity, EntityData, EntityReader
from reader_bridge import ReaderBridge

_log = logging.getLogger("poe2auto.readerdetect")

# Кеш hideout-результата: (area_hash, result, timestamp)
_HIDEOUT_CACHE_TTL = 3.0  # секунды


class ReaderMobDetector:
    """Детект мобов через прямой ReaderBridge (один процесс, без TCP).

    Реализует тот же интерфейс что и EC2MobDetector / MemoryMobDetector:
      - connect() / disconnect()
      - available (property)
      - update() -> bool
      - present(frame_bgr=None) -> bool
      - in_hideout() -> bool
      - read_player_vitals() -> (hp_pct, mana_pct)
      - get_nearest_hostile() / get_all_hostiles() / get_hostiles_in_range()
      - get_best_target() / get_lowest_hp_target()
      - get_chests() / get_transitions()
      - get_area_info() -> dict
      - stats (dict)
      - last_match (compat с MobBarDetector)
    """

    # Маппинг entity_type (строка из ReaderBridge) → EntityCategory
    _TYPE_MAP = {
        "m": EntityCategory.MONSTER,
        "Player": EntityCategory.PLAYER,
        "c": EntityCategory.CHEST,
        "t": EntityCategory.TRANSITION,
        "n": EntityCategory.NPC,
        "fx": EntityCategory.OTHER,    # Effects — не интересны
        "p": EntityCategory.OTHER,     # Projectiles
        "mo": EntityCategory.OTHER,    # MiscellaneousObjects
        "o": EntityCategory.OTHER,
    }

    # Hideout / town area codes в PoE2 (миррор EC2MobDetector)
    _HIDEOUT_CODES = ("hideout", "playerhideout", "guildhideout", "delvemine")
    _TOWN_CODES = ("_town", "town")

    def __init__(self, cfg: dict = None):
        cfg = cfg or {}
        self.bridge = ReaderBridge()

        # Параметры
        self.attack_range = cfg.get("attack_range", 15.0)
        self.attack_range_sq = self.attack_range ** 2

        # Фильтр статичных «призраков»: моб числится живым в памяти (Life-флаг
        # смерти == 0), но НИ РАЗУ не сдвинулся с места с момента появления —
        # спящие утопленники в воде / декоративные трупы. На MapPort бот держал
        # R1 по невидимому DrownedCrewAxe на 22 grid. Цель = моб, который хоть
        # раз двигался, либо ещё не прошёл grace-период (только заспавненный).
        self.ignore_static = bool(cfg.get("ignore_static_mobs", True))
        self.static_grace_s = float(cfg.get("static_grace_s", 2.0))
        self._static_track = {}   # addr -> {ever_moved, ever_hit, first_seen, last_seen, last}
        self._last_area_hash = -1
        # Признак «игрок в бою»: вражеский снаряд, приближающийся к игроку.
        # Вражеские снаряды не лежат в awake-дереве — фича опциональна.
        self._player_fire_time = 0.0
        self._last_proj_d = {}    # addr -> (dist_to_player, ts)
        self._last_proj_ts = 0.0

        # Состояние
        self._connected = False
        self._last_entities: List[EntityData] = []
        self._last_update = 0.0
        self._update_interval = 0.05  # 50ms = 20Hz
        self._player_pos: Tuple[float, float] = (0.0, 0.0)
        self._player_z: float = 0.0
        self._player_hp: Optional[list] = None
        self._area_code = ""
        self._area_hash = 0
        self._game_state = 0
        self._hideout_cache: Optional[Tuple[int, bool, float]] = None

        # Совместимость с MobBarDetector
        self.last_match = None

        # Статистика
        self.stats = {
            "total_entities": 0,
            "hostile_in_range": 0,
            "friendlies": 0,
            "chests": 0,
        }

    def connect(self) -> bool:
        """Инициализировать ReaderBridge (найти PoE2, OpenProcess, AOB scan GSO).

        Idempotent: пропускает если уже подключён. Не падает если PoE2 не запущен
        (вернёт False, bridge продолжит попытки в update()).
        """
        if self._connected:
            return True
        ok = self.bridge.initialize()
        if not ok:
            _log.debug("ReaderBridge ещё не готов (PoE2 не запущен / нет admin?)")
            return False
        self._connected = True
        _log.info("ReaderDetector connected: pid=%d", self.bridge.pid)
        return True

    def disconnect(self):
        """Отключиться от процесса PoE2."""
        self.bridge.shutdown()
        self._connected = False
        self._area_code = ""
        self._area_hash = 0
        self._game_state = 0
        self._last_entities = []
        self._player_hp = None

    @property
    def available(self) -> bool:
        """True если bridge работает и персонаж в игре (game_state != 0)."""
        if not self._connected and not self.connect():
            return False
        return self.bridge.get_in_game()

    def update(self) -> bool:
        """Обновить данные сущностей. Вызывать каждый кадр.

        Returns:
            True если данные обновлены.
        """
        now = time.perf_counter()
        if now - self._last_update < self._update_interval:
            return False
        self._last_update = now

        if not self._connected:
            self.connect()
            if not self._connected:
                return False

        # Персонаж не в игре → нет данных (меню/char-select/loading)
        if not self.bridge.get_in_game():
            self._last_entities = []
            self._area_code = ""
            self._area_hash = 0
            self._game_state = 0
            self._player_hp = None
            self.stats = {"total_entities": 0, "hostile_in_range": 0,
                          "friendlies": 0, "chests": 0}
            return False

        # Обновляем состояние игры и зону
        self._game_state = self.bridge.get_game_state()
        area = self.bridge.get_area_info()
        self._area_code = area["name"] or area["code"]
        self._area_hash = area["hash"]
        if self._area_hash != self._last_area_hash:
            self._static_track.clear()
            self._last_area_hash = self._area_hash

        # Позиция игрока: из Render-компонента игрока в дереве (тот же источник,
        # что у мобов — корректна при любом патче). area+0xA90 протух (0xA90
        # после патча указывает не на позицию) — расходится на ~100+ grid.
        raw_entities = self.bridge.read_entities_raw()
        player_raw = [e for e in raw_entities if e["entity_type"] == "Player"]
        if player_raw and any(player_raw[0].get("grid_pos") or []):
            self._player_pos = tuple(player_raw[0]["grid_pos"])
            self._player_z = float(player_raw[0].get("z") or 0.0)
            self._player_hp = player_raw[0].get("life")
        else:
            self._player_pos = self.bridge.get_player_grid_pos()

        # Парсим сущности
        self._last_entities = self._parse_entities(raw_entities)
        self._update_static_track(raw_entities)
        self._track_projectiles(raw_entities)

        # Статистика
        monsters_alive = [e for e in self._last_entities
                          if e.category == EntityCategory.MONSTER
                          and not e.is_friendly
                          and self._is_hostile_alive(e)
                          and self._is_static_target_ok(e)]
        in_range = [e for e in monsters_alive if e.distance <= self.attack_range]

        self.stats["total_entities"] = len(self._last_entities)
        self.stats["hostile_in_range"] = len(in_range)
        self.stats["friendlies"] = sum(1 for e in self._last_entities if e.is_friendly)
        self.stats["chests"] = sum(1 for e in self._last_entities
                                   if e.category == EntityCategory.CHEST)
        return True

    def present(self, frame_bgr=None) -> bool:
        """Совместимый с MobBarDetector интерфейс.

        Returns:
            True если есть враждебный моб в радиусе атаки.
        """
        if not self._connected:
            self.connect()
        if not self._connected:
            return False

        self.update()
        return self.stats["hostile_in_range"] > 0

    # --- Entity queries (те же что у EC2MobDetector) ---

    def get_nearest_hostile(self) -> Optional[EntityData]:
        """Ближайший враждебный моб в радиусе атаки."""
        hostiles = self.get_hostiles_in_range()
        if not hostiles:
            return None
        return min(hostiles, key=lambda e: e.distance)

    def get_all_hostiles(self) -> List[EntityData]:
        """Все враждебные мобы."""
        return [e for e in self._last_entities
                if e.category == EntityCategory.MONSTER
                and not e.is_friendly
                and self._is_hostile_alive(e)
                and self._is_static_target_ok(e)]

    def get_hostiles_in_range(self) -> List[EntityData]:
        """Враждебные мобы в радиусе атаки."""
        return [e for e in self._last_entities
                if e.category == EntityCategory.MONSTER
                and not e.is_friendly
                and self._is_hostile_alive(e)
                and e.distance <= self.attack_range
                and self._is_static_target_ok(e)]

    def get_best_target(self) -> Optional[EntityData]:
        """Лучшая цель: босс > unique > rare > magic > normal, потом по расстоянию."""
        hostiles = self.get_hostiles_in_range()
        if not hostiles:
            return None

        rarity_order = {
            Rarity.UNIQUE: 0,
            Rarity.RARE: 1,
            Rarity.MAGIC: 2,
            Rarity.NORMAL: 3,
        }

        def target_score(e: EntityData) -> tuple:
            is_boss_penalty = 0 if e.is_boss else 1
            rarity_penalty = rarity_order.get(e.rarity, 4)
            return (is_boss_penalty, rarity_penalty, e.distance)

        return min(hostiles, key=target_score)

    def get_lowest_hp_target(self) -> Optional[EntityData]:
        """Цель с минимальным HP% в радиусе."""
        hostiles = self.get_hostiles_in_range()
        if not hostiles:
            return None
        return min(hostiles, key=lambda e: e.hp_ratio if e.hp_max > 0 else 1.0)

    def get_chests(self) -> List[EntityData]:
        """Сундуки."""
        return [e for e in self._last_entities
                if e.category == EntityCategory.CHEST]

    def get_transitions(self, dedup_radius: float = 0.5) -> List[EntityData]:
        """Уникальные переходы (portals) с дедупликацией по позиции."""
        transitions = [e for e in self._last_entities
                       if e.category == EntityCategory.TRANSITION]
        if not transitions:
            return []

        transitions.sort(key=lambda e: e.distance)

        unique: List[EntityData] = []
        for t in transitions:
            is_dup = False
            for u in unique:
                dx = t.grid_x - u.grid_x
                dy = t.grid_y - u.grid_y
                if dx * dx + dy * dy <= dedup_radius * dedup_radius:
                    is_dup = True
                    break
            if not is_dup:
                unique.append(t)

        return unique

    # --- Player / area queries ---

    def in_hideout(self) -> bool:
        """Определить, находится ли игрок в убежище/городе. Кеширует по area_hash."""
        now = time.perf_counter()
        if (self._hideout_cache is not None
                and self._hideout_cache[0] == self._area_hash
                and now - self._hideout_cache[2] < _HIDEOUT_CACHE_TTL):
            return self._hideout_cache[1]

        code = self._area_code.lower()
        result = False
        for hc in self._HIDEOUT_CODES:
            if hc in code:
                result = True
                break
        if not result:
            for tc in self._TOWN_CODES:
                if tc in code:
                    result = True
                    break

        self._hideout_cache = (self._area_hash, result, now)
        return result

    def get_area_info(self) -> dict:
        """Информация о текущей зоне."""
        return {
            "code": self._area_code,
            "hash": self._area_hash,
            "game_state": self._game_state,
        }

    def read_player_vitals(self) -> Tuple[Optional[float], Optional[float]]:
        """Прочитать HP% и Mana% игрока.

        Returns:
            (hp_pct, mana_pct) — доли 0..1, или (None, None) если данных нет.
            HP читается из Life.HEALTH VitalStruct (см. reader_bridge._read_life_hp).
        """
        lc = self._player_hp or []
        if len(lc) >= 2 and lc[0] > 0:
            return (lc[1] / lc[0]), None
        return None, None

    @staticmethod
    def _is_hostile_alive(entity: EntityData) -> bool:
        """Живой боевой моб.

        is_alive (C#-флаг Life+0x10) — базовая живость. Дополнительно:
        у невидимых «пустышек» (заспавненные, но не активированные сущности вроде
        RiverTaniwhaNoJank / FallenKaruiShieldAxeHalls) и у трупов без флага
        hp_cur == 0 при rarity < RARE — их не атакуем. Rare/Unique/боссов
        атакуем даже при hp_cur == 0 (их VitalStruct current может не читаться).
        """
        if not entity.is_alive:
            return False
        if entity.hp_current > 0:
            return True
        return entity.rarity >= Rarity.RARE or entity.is_boss

    # --- Parsing (internal) ---

    def _update_static_track(self, raw_entities: list) -> None:
        """Трекать позиции монстров, чтобы отличать живых от статичных «призраков».

        «Призрак» = моб, который с момента появления в дереве НИ РАЗУ не сдвинулся
        с места (спящий утопленник в воде, декоративный труп). Реальный враг почти
        всегда начинает движение/атаку — и остаётся валидной целью навсегда
        (ever_moved сохраняется, даже если потом встанет в упор и бьёт на месте).
        Дополнительный признак «в бою» — ever_hit: моб получал урон (hp_cur < hp_max),
        его уже били. Стоячий стрелок с полным HP остаётся «призраком», пока не
        шевельнётся / не получит урон / пока игрок под вражеским огнём.
        """
        if not self.ignore_static:
            self._static_track.clear()
            return
        now = time.perf_counter()
        seen = set()
        for raw in raw_entities:
            if raw.get("entity_type") != "m":
                continue
            addr = raw.get("addr", 0)
            if not addr:
                continue
            seen.add(addr)
            gp = raw.get("grid_pos") or [0, 0]
            gx, gy = float(gp[0]), float(gp[1])
            lc = raw.get("life") or []
            hit = len(lc) >= 2 and 0 < lc[1] < lc[0]
            tr = self._static_track.get(addr)
            if tr is None:
                self._static_track[addr] = {
                    "ever_moved": False, "ever_hit": hit, "first_seen": now,
                    "last_seen": now, "last": (gx, gy),
                }
            else:
                tr["last_seen"] = now
                if hit:
                    tr["ever_hit"] = True
                if (gx, gy) != tr["last"]:
                    tr["ever_moved"] = True
                    tr["last"] = (gx, gy)
        stale = [a for a, tr in self._static_track.items()
                 if a not in seen and now - tr["last_seen"] > 10.0]
        for a in stale:
            del self._static_track[a]

    def _track_projectiles(self, raw_entities: list) -> None:
        """Отметить «игрок в бою», если снаряд приближается к игроку.

        Снаряды игрока летят ОТ игрока (dist растёт), вражеские — К игроку
        (dist падает). Работает только если снаряды попадают в awake-дерево
        (пока не подтверждено — фича опциональна).
        """
        now = time.perf_counter()
        proj = [e for e in raw_entities if e.get("entity_type") == "p"]
        if not proj:
            if self._last_proj_d and now - self._last_proj_ts > 3.0:
                self._last_proj_d.clear()
            return
        px, py = self._player_pos
        fired = False
        seen = set()
        for raw in proj:
            addr = raw.get("addr", 0)
            gp = raw.get("grid_pos") or [0, 0]
            dx = float(gp[0]) - px
            dy = float(gp[1]) - py
            d = math.hypot(dx, dy)
            if d > 60.0:
                continue
            seen.add(addr)
            prev = self._last_proj_d.get(addr)
            if prev is not None and d < prev - 0.3:
                fired = True
            self._last_proj_d[addr] = d
        self._last_proj_ts = now
        for a in list(self._last_proj_d):
            if a not in seen:
                del self._last_proj_d[a]
        if fired:
            self._player_fire_time = now

    def _is_static_target_ok(self, entity: EntityData) -> bool:
        """True если моба можно атаковать с учётом фильтра статичных «призраков»."""
        if not self.ignore_static:
            return True
        # Rare/Unique/боссов не фильтруем: это значимые цели, их не отсекаем
        # за то, что они стоят на месте (боссы часто бьют с точки спавна).
        if entity.rarity >= Rarity.RARE or entity.is_boss:
            return True
        now = time.perf_counter()
        # Игрок под вражеским огнём (снаряд летит к нему) — в бою, статику
        # разблокируем на 2с: стоячий стрелок, бьющий с места, становится целью.
        if now - self._player_fire_time < 2.0:
            return True
        tr = self._static_track.get(entity.id)
        if tr is None:
            return True
        if tr["ever_moved"] or tr["ever_hit"]:
            return True
        return now - tr["first_seen"] < self.static_grace_s

    def _parse_entities(self, raw_entities: list) -> List[EntityData]:
        """Преобразовать список raw-сущностей из ReaderBridge → список EntityData."""
        entities: List[EntityData] = []
        px, py = self._player_pos

        for raw in raw_entities:
            if not isinstance(raw, dict):
                continue
            entity = self._parse_entity(raw, px, py)
            if entity is not None:
                entities.append(entity)

        return entities

    def _parse_entity(self, raw: dict,
                      player_x: float, player_y: float) -> Optional[EntityData]:
        """Парсинг одной сущности из ReaderBridge → EntityData."""
        et = raw.get("entity_type", "")
        category = self._TYPE_MAP.get(et, EntityCategory.OTHER)

        path = raw.get("path") or ""
        if category == EntityCategory.MONSTER and any(
                kw in path for kw in EntityReader.NON_COMBAT_KEYWORDS):
            # «Монстры» из эффектов модов: Tormented Spirits (дух-спутник),
            # Summoned/Daemon/Invisible — не боевые цели, в атаку не идут.
            category = EntityCategory.OTHER
        if not et and path:
            category = self._categorize_by_path(path)

        gp = raw.get("grid_pos") or []
        grid_x = float(gp[0]) if len(gp) >= 1 else 0.0
        grid_y = float(gp[1]) if len(gp) >= 2 else 0.0

        wp = raw.get("world_pos") or []
        wx = float(wp[0]) if len(wp) >= 1 else grid_x
        wy = float(wp[1]) if len(wp) >= 2 else grid_y
        wz = float(wp[2]) if len(wp) >= 3 else 0.0

        # Z-фильтр: сущности на другом Z-слое (аурные даемоны модов, подводные
        # твари) не видны и недостижимы — считаем их «не живыми» для атаки.
        # Исключение: Rare/Unique/боссы (у гигантов z читается криво, dz ~+95).
        dz = abs(float(raw.get("z") or 0.0) - self._player_z)
        z_ok = dz <= 25.0
        rarity = Rarity(raw.get("rarity", 0)) if raw.get("rarity") is not None else Rarity.NORMAL
        is_boss = bool(raw.get("is_boss", False))
        significant = rarity >= Rarity.RARE or is_boss

        # Живость: C#-флаг из Life+0x10 (0=жив, 1=труп) + Z-слой. Надёжнее
        # hp_cur>0: у живых боссов hp_cur может быть 0 при hp_max>0.
        is_alive = bool(raw.get("alive", True)) and (z_ok or significant)

        lc = raw.get("life") or []
        if len(lc) >= 2 and (lc[0] > 0 or lc[1] > 0):
            hp_max = lc[0]
            hp_cur = lc[1]
        elif is_alive and z_ok:
            hp_max = 1
            hp_cur = 1
        else:
            hp_max = 1
            hp_cur = 0
        hp_ratio = (hp_cur / hp_max) if hp_max > 0 else 1.0

        is_hostile = bool(raw.get("hostile", 0))
        is_friendly = not is_hostile
        eid = raw.get("addr", 0)

        dx = grid_x - player_x
        dy = grid_y - player_y
        distance = math.sqrt(dx * dx + dy * dy)

        return EntityData(
            id=eid, category=category, metadata=path,
            x=wx, y=wy, z=wz, grid_x=grid_x, grid_y=grid_y,
            hp_current=hp_cur, hp_max=hp_max, hp_ratio=hp_ratio,
            rarity=rarity, is_friendly=is_friendly,
            distance=distance, is_boss=is_boss, is_alive=is_alive,
        )

    @staticmethod
    def _categorize_by_path(path: str) -> EntityCategory:
        """Определить категорию по metadata path (fallback если 'et' пуст)."""
        if "/NPC/" in path:
            return EntityCategory.NPC
        if "/Monsters/" in path or "/Critters/" in path:
            if any(kw in path for kw in EntityReader.NON_COMBAT_KEYWORDS):
                return EntityCategory.OTHER
            return EntityCategory.MONSTER
        if "/Characters/" in path:
            return EntityCategory.PLAYER
        if "/Chests" in path:
            return EntityCategory.CHEST
        if "Transition" in path:
            return EntityCategory.TRANSITION
        return EntityCategory.OTHER


# ──────────────────────────────────────────────────────────────────────────
# Smoke-test:  python reader_detect.py
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    print("=== ReaderMobDetector smoke-test ===")
    det = ReaderMobDetector()
    if det.connect():
        det.update()
        area = det.get_area_info()
        print("area: code=%r state=%d" % (area["code"], area["game_state"]))
        print("in_hideout=%s" % det.in_hideout())
        print("player_pos=%s" % (det._player_pos,))
        print("stats=%s" % det.stats)
        in_range = det.get_hostiles_in_range()
        print("hostiles_in_range=%d" % len(in_range))
        for e in in_range[:5]:
            print("  d=%.1f %s hp=%d/%d %s" % (e.distance, e.rarity.name, e.hp_current, e.hp_max, e.metadata[-40:]))
    else:
        print("not connected (PoE2 не запущен?)")
    det.disconnect()
