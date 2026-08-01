"""Детектор мобов через ExileCore2 (TCP JSON bridge).

Drop-in замена MemoryMobDetector: тот же интерфейс, но вместо PROCESS_VM_READ
получает готовые данные из ExileCore2 через TCP-клиент (ec2_client.py).

Преимущества:
  - НЕ ломается на патчах PoE2 (оффсеты обновляет сообщество)
  - НЕ нужен Admin (ExileCore2 инжектится через Loader.exe)
  - Точные HP/Mana/позиции без знания оффсетов

Требования:
  - Запущенный ExileCore2 Loader.exe (инжектируется в PoE2)
  - Установленный плагин ShareData (отдаёт JSON по TCP :50006)
"""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, List, Optional, Tuple

from ec2_client import EC2Client
from entity_reader import EntityCategory, Rarity, EntityData

_log = logging.getLogger("poe2auto.ec2detect")

# Кеш hideout-результата: (area_hash, result, timestamp)
_HIDEOUT_CACHE_TTL = 3.0  # секунды


class EC2MobDetector:
    """Детект мобов через ExileCore2 TCP bridge.

    Реализует тот же интерфейс что и MemoryMobDetector:
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

    # Маппинг entity_type (строка из JSON "et") → EntityCategory
    _TYPE_MAP: Dict[str, EntityCategory] = {
        "m": EntityCategory.MONSTER,    # monster
        "p": EntityCategory.PLAYER,     # player
        "c": EntityCategory.CHEST,      # chest
        "t": EntityCategory.TRANSITION, # transition
        "n": EntityCategory.NPC,        # npc
        "o": EntityCategory.OTHER,      # other
    }

    # Маппинг rarity (строка из JSON "r") → Rarity enum
    _RARITY_MAP: Dict[str, Rarity] = {
        "normal": Rarity.NORMAL,
        "magic": Rarity.MAGIC,
        "rare": Rarity.RARE,
        "unique": Rarity.UNIQUE,
    }

    # Hideout / town area codes в PoE2
    _HIDEOUT_CODES = ("hideout", "playerhideout", "guildhideout", "delvemine")
    _TOWN_CODES = ("_town", "town")

    def __init__(self, cfg: dict = None):
        cfg = cfg or {}
        host = cfg.get("ec2_host", "127.0.0.1")
        port = cfg.get("ec2_port", 50006)
        self.client = EC2Client(host, port)

        # Параметры
        self.attack_range = cfg.get("attack_range", 15.0)

        # Состояние
        self._connected = False
        self._last_entities: List[EntityData] = []
        self._last_update = 0.0
        self._update_interval = 0.05  # 50ms = 20Hz
        self._player_pos: Tuple[float, float] = (0.0, 0.0)
        self._player_hp: Optional[Tuple[int, int]] = None
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
        """Подключиться к ExileCore2 ShareData."""
        if self._connected and self.client.connected:
            return True

        if not self.client.connect():
            _log.warning("Could not connect to ExileCore2 ShareData "
                         "(is Loader.exe running with ShareData plugin?)")
            return False

        # Проверяем, что данные действительно приходят
        data = self.client.get_data()
        if data is None:
            _log.warning("EC2 connected but no data received — "
                         "is PoE2 in-game (not menu)?")
            # Соединение установлено, но данных пока нет — всё равно считаем подключённым
            # (игра может быть в меню/загрузке)

        self._connected = True
        _log.info("EC2 detector connected")
        return True

    @property
    def available(self) -> bool:
        """True если EC2 работает и данные получены."""
        return self._connected and self.client.connected and bool(self._area_code)

    def disconnect(self):
        """Отключиться от ShareData."""
        self.client.disconnect()
        self._connected = False
        self._area_code = ""
        self._area_hash = 0
        self._game_state = 0

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

        data = self.client.get_data()
        if data is None:
            return False

        # Обновляем состояние игры
        self._game_state = data.get("g_s", 0)
        self._area_code = data.get("area_raw_name", "") or ""
        self._area_hash = data.get("ah", 0) or 0

        # Парсим игрока
        self._parse_player(data)

        # Парсим сущности
        raw_entities = data.get("awake_entities") or []
        self._last_entities = self._parse_entities(raw_entities)

        # Статистика
        monsters_alive = [e for e in self._last_entities
                          if e.category == EntityCategory.MONSTER
                          and not e.is_friendly
                          and e.hp_current > 0]
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

    # --- Entity queries (те же что у MemoryMobDetector) ---

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
                and e.hp_current > 0]

    def get_hostiles_in_range(self) -> List[EntityData]:
        """Враждебные мобы в радиусе атаки."""
        return [e for e in self._last_entities
                if e.category == EntityCategory.MONSTER
                and not e.is_friendly
                and e.hp_current > 0
                and e.distance <= self.attack_range]

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
        """Определить, находится ли игрок в убежище/городе.

        Использует area_raw_name из JSON. Кеширует результат.
        """
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
        """
        return self._read_vitals(self._player_hp, slot=0)

    def _read_vitals(self, life_data: Optional[list],
                     slot: int = 0) -> Tuple[Optional[float], Optional[float]]:
        """Парсинг life-компонента: [hp_max, hp_cur, hp_res, es_max, es_cur, es_res,
        mana_max, mana_cur, mana_res].

        slot=0 — игрок, slot>0 не используется (оставлено для совместимости).
        """
        if not life_data or len(life_data) < 9:
            return None, None

        hp_max = life_data[0]
        hp_cur = life_data[1]
        mana_max = life_data[6]
        mana_cur = life_data[7]

        hp_pct = max(0.0, min(1.0, hp_cur / hp_max)) if hp_max > 0 else None
        mana_pct = max(0.0, min(1.0, mana_cur / mana_max)) if mana_max > 0 else None

        return hp_pct, mana_pct

    # --- Parsing (internal) ---

    def _parse_player(self, data: dict):
        """Извлечь позицию и vitals игрока из JSON."""
        pi = data.get("pi") or data.get("player_info")
        if not pi:
            self._player_pos = (0.0, 0.0)
            self._player_hp = None
            return

        # grid_position: [x, y]
        gp = pi.get("gp") or []
        if len(gp) >= 2:
            self._player_pos = (float(gp[0]), float(gp[1]))

        # life_component: [hp_max, hp_cur, hp_res, es_..., mana_max, mana_cur, mana_res]
        lc = pi.get("l") or []
        self._player_hp = lc if lc else None

    def _parse_entities(self, raw_entities: list) -> List[EntityData]:
        """Преобразовать список Entity_c JSON → список EntityData."""
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
        """Парсинг одной сущности из Entity_c JSON → EntityData."""
        # entity_type → category
        et = raw.get("et", "")
        category = self._TYPE_MAP.get(et, EntityCategory.OTHER)

        # Если тип не указан, пробуем определить по path
        path = raw.get("p") or ""
        if not et and path:
            category = self._categorize_by_path(path)

        # grid_position: [x, y]
        gp = raw.get("gp") or []
        grid_x = float(gp[0]) if len(gp) >= 1 else 0.0
        grid_y = float(gp[1]) if len(gp) >= 2 else 0.0

        # world_position: [x, y, z] (если есть)
        wp = raw.get("wp") or []
        wx = float(wp[0]) if len(wp) >= 1 else grid_x
        wy = float(wp[1]) if len(wp) >= 2 else grid_y
        wz = float(wp[2]) if len(wp) >= 3 else 0.0

        # life_component: [hp_max, hp_cur, hp_res, ...]
        lc = raw.get("l") or []
        hp_max = lc[0] if len(lc) >= 1 else 0
        hp_cur = lc[1] if len(lc) >= 2 else 0
        hp_ratio = (hp_cur / hp_max) if hp_max > 0 else 0.0

        # is_hostile (int 0/1) → is_friendly (inverted)
        is_hostile = bool(raw.get("h", 0))
        is_friendly = not is_hostile

        # rarity
        rarity_str = raw.get("r", "normal")
        rarity = self._RARITY_MAP.get(rarity_str, Rarity.NORMAL)

        # entity id
        eid = raw.get("i", 0)

        # distance from player (grid coords)
        dx = grid_x - player_x
        dy = grid_y - player_y
        distance = math.sqrt(dx * dx + dy * dy)

        # is_boss: unique rarity + monster
        is_boss = (category == EntityCategory.MONSTER and rarity == Rarity.UNIQUE)

        return EntityData(
            id=eid,
            category=category,
            metadata=path,
            x=wx,
            y=wy,
            z=wz,
            grid_x=grid_x,
            grid_y=grid_y,
            hp_current=hp_cur,
            hp_max=hp_max,
            hp_ratio=hp_ratio,
            rarity=rarity,
            is_friendly=is_friendly,
            distance=distance,
            is_boss=is_boss,
        )

    @staticmethod
    def _categorize_by_path(path: str) -> EntityCategory:
        """Определить категорию по metadata path (fallback если 'et' пуст)."""
        if "/NPC/" in path:
            return EntityCategory.NPC
        if "/Monsters/" in path or "/Critters/" in path:
            if any(kw in path for kw in ("Summoned", "/Daemon/", "Invisible", "MonsterMods")):
                return EntityCategory.OTHER
            return EntityCategory.MONSTER
        if "/Characters/" in path:
            return EntityCategory.PLAYER
        if "/Chests" in path:
            return EntityCategory.CHEST
        if "Transition" in path:
            return EntityCategory.TRANSITION
        return EntityCategory.OTHER
