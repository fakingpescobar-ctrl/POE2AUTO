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
  - Скомпилированная PoE2ReaderLib.dll (poe2-reader/lib/bin/Release; см. build_reader.bat)
  - PoE2 запущен (PathOfExileSteam.exe)
  - Бот запущен с правами admin (OpenProcess PROCESS_VM_READ)

Жив/мёртв определяется многоуровневой классификацией TargetStatus
(см. ниже): DEAD (труп, Life+0x10) → SPIRIT (дух/немоб) → LATENT (умирает,
HP≤порога) → GHOST_STATIC (спящий призрак, не двигался) → UNKNOWN (HP не
прочитан) → COMBAT (атаковать). HP читается из Life-компонента (reader_bridge.
_read_vitals). Атакуются только цели со статусом COMBAT.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import List, Optional, Tuple

from entity_reader import EntityCategory, Rarity, EntityData, EntityReader
from reader_bridge import ReaderBridge
from monster_db import MonsterDB

_log = logging.getLogger("poe2auto.readerdetect")

# Кеш hideout-результата: (area_hash, result, timestamp)
_HIDEOUT_CACHE_TTL = 3.0  # секунды


class TargetStatus:
    """Многоуровневый статус цели (вместо бинарного «жив/мёртв»).

    Заменяет разрозненные проверки _is_hostile_alive + _is_static_target_ok
    одной детерминированной классификацией. Порядок проверок — от самого
    «мёртвого» к самому «боеготовому».

    DEAD         — труп. Флаг смерти Life+0x10 == 1 (C# уже отсёк, is_alive=False).
    SPIRIT       — небоевой дух/эффект. Уже перекатегоризован в OTHER в _parse_entity,
                   сюда попадает как теоретический путь (других враждебных MONSTER
                   с этими keywords не бывает). Боевой фильтр добивает надёжно.
    LATENT       — alive, но hp_max>0 И hp_cur<=min_hp (умирает / пустышка-невидимка).
                   Не атакуем: это либо труп в процессе, либо сущность без реального HP.
    GHOST_STATIC — alive + HP есть, но НИ РАЗУ не двигался сверх grace-периода
                   (спящий утопленник, декоративный труп, который числится живым).
                   Не атакуем: висит R1 по невидимой цели.
    UNKNOWN      — alive, но HP НЕ прочитан (life_addr ещё не резолвнулся, холодный
                   кэш). Решение по attack_unknown_hp: по умолчанию ждём, для
                   rare/boss и «двигался» — даём поблажку.
    COMBAT       — ✅ атаковать. alive + (реальный hp>0) + (двигался ИЛИ grace ИЛИ
                   игрок под огнём ИЛИ rare/boss). Боеготовая цель.
    """
    DEAD = "dead"
    SPIRIT = "spirit"
    LATENT = "latent"
    GHOST_STATIC = "ghost_static"
    UNKNOWN = "unknown"
    COMBAT = "combat"

    # Статусы, которые считаются «достойными атаки».
    ATTACKABLE = frozenset({COMBAT})

    # Alive but on different Z-level or too far to be a valid target.
    UNREACHABLE = frozenset()

    # Recently dead entity (corpse) — may have loot, not a combat target.
    CORPSE = frozenset()

    # Статусы, которые технически живы (для статистики «всего живых»).
    LIVING = frozenset({LATENT, GHOST_STATIC, UNKNOWN, COMBAT, UNREACHABLE})


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
        self.corpse_timeout_s = float(cfg.get("corpse_timeout_s", 30.0))
        # Grace period after dead_since: don't attack mobs that died
        # within this window (death animation, C# flag lag).
        self.death_grace_s = float(cfg.get("death_grace_s", 2.0))
        # Порог HP: моб с hp_cur <= min_hp при hp_max>0 — LATENT (умирает/пустышка),
        # в атаку не идёт. Rare/Unique/боссы этим порогом не отсекаются.
        self.min_hp_threshold = int(cfg.get("min_hp_threshold", 0))
        # Что делать с живым мобом, у которого HP НЕ прочитан (холодный кэш Life):
        # True  — атаковать (старое поведение: HP читается не сразу после спавна).
        # False — ждать реальных данных, не атаковать вслепую.
        # Rare/boss и «когда-либо двигался» атакуются независимо от этого флага.
        self.attack_unknown_hp = bool(cfg.get("attack_unknown_hp", True))
        # SQLite телеметрия: полный дамп мобов по кадрам (для дебага классификации
        # и аналитики). Включается в config → monster_db. None = телеметрия выключена.
        mdb = cfg.get("monster_db") or {}
        self.db: Optional[MonsterDB] = None
        if mdb.get("enabled"):
            self.db = MonsterDB(
                path=mdb.get("path", "monster_telemetry.db"),
                batch_size=mdb.get("batch_size", 50),
                flush_interval_s=mdb.get("flush_interval_s", 1.0),
                keep_days=mdb.get("keep_days", 3.0),
            )
        self._static_track = {}   # addr -> {ever_moved, ever_hit, first_seen, last_seen, last}
        self._dead_track = {}       # addr -> death_timestamp
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
        # Старт SQLite телеметрии (если включена в config). session_id = время.
        if self.db is not None:
            self.db.start(datetime.now().strftime("%Y-%m-%d_%H%M%S"))
        return True

    def disconnect(self):
        """Отключиться от процесса PoE2."""
        if self.db is not None:
            self.db.close()
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
                          and e.is_reachable
                          and self._classify_target(e) in TargetStatus.ATTACKABLE]
        in_range = [e for e in monsters_alive if e.distance <= self.attack_range]

        self.stats["total_entities"] = len(self._last_entities)
        self.stats["hostile_in_range"] = len(in_range)
        self.stats["friendlies"] = sum(1 for e in self._last_entities if e.is_friendly)
        self.stats["chests"] = sum(1 for e in self._last_entities
                                   if e.category == EntityCategory.CHEST)
        # Распределение статусов целей (отладка: видно сколько мобов в каждой
        # категории — COMBAT/LATENT/GHOST_STATIC/UNKNOWN/DEAD/SPIRIT).
        status_counts = {}
        db_rows: List[Tuple[EntityData, str]] = []  # (entity, status) для телеметрии
        for e in self._last_entities:
            if e.category == EntityCategory.MONSTER and not e.is_friendly:
                st = self._classify_target(e)
                status_counts[st] = status_counts.get(st, 0) + 1
                if self.db is not None:
                    db_rows.append((e, st))
        self.stats["target_status"] = status_counts
        # SQLite телеметрия: полный снимок кадра (для дебага/аналитики). Неблокирующий.
        if self.db is not None and db_rows:
            self.db.record_frame(self._area_code, db_rows)
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
        """Все враждебные мобы (боеготовые, статус COMBAT)."""
        return [e for e in self._last_entities
                if e.category == EntityCategory.MONSTER
                and not e.is_friendly
                and e.is_reachable
                and self._classify_target(e) in TargetStatus.ATTACKABLE]

    def get_hostiles_in_range(self) -> List[EntityData]:
        """Враждебные мобы в радиусе атаки (статус COMBAT)."""
        return [e for e in self._last_entities
                if e.category == EntityCategory.MONSTER
                and not e.is_friendly
                and e.is_reachable
                and self._classify_target(e) in TargetStatus.ATTACKABLE
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
            confidence_penalty = 0 if e.confidence >= 0.7 else 1
            return (is_boss_penalty, rarity_penalty, confidence_penalty, e.distance)

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

    def get_corpses_in_range(self, radius: float = 5.0) -> List[EntityData]:
        """Трупы в радиусе — потенциальный лут.

        Возвращает недавно умерших мобов (CORPSE), которые ещё не
        исчезли из кадра и могут дропать предметы.
        """
        now = time.perf_counter()
        return [e for e in self._last_entities
                if e.category == EntityCategory.MONSTER
                and e.is_corpse
                and e.dead_since is not None
                and (now - e.dead_since) < self.corpse_timeout_s
                and e.distance <= radius]

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

    def _classify_target(self, entity: EntityData) -> str:
        """Единая многоуровневая классификация цели. См. TargetStatus.

        Заменяет прежние _is_hostile_alive + _is_static_target_ok. Возвращает
        один из TargetStatus.*. Атаковать только если результат в
        TargetStatus.ATTACKABLE (т.е. == COMBAT). Порядок проверок — от самого
        «мёртвого» к самому «боеготовому», каждая отсекает свою категорию шума.
        """
        # 1) DEAD — труп. C#-флаг Life+0x10 (is_alive) уже учтён в _parse_entity.
        if not entity.is_alive:
            if self._is_recently_dead(entity.id):
                return TargetStatus.CORPSE
            return TargetStatus.DEAD

        # 1b) RECENTLY_DEAD — C# ещё говорит alive, но мы знаем что моб
        # умер (исчез из кадра и вернулся). Не атакуем — анимация смерти.
        if entity.dead_since is not None:
            if (time.perf_counter() - entity.dead_since) < self.death_grace_s:
                return TargetStatus.DEAD

        # 1a) UNREACHABLE - alive but different Z-level or too far.
        if not entity.is_reachable:
            return TargetStatus.UNREACHABLE

        # 2) SPIRIT — небоевой дух/эффект (Tormented Spirit, Summoned, Daemon,
        # Invisible, MonsterMods). В _parse_entity они перекатегоризуются в OTHER
        # и сюда как MONSTER не дойдут, но фильтр добивает надёжно на любом пути.
        path = entity.metadata or ""
        if any(kw in path for kw in EntityReader.NON_COMBAT_KEYWORDS):
            return TargetStatus.SPIRIT

        significant = entity.rarity >= Rarity.RARE or entity.is_boss

        # 3) LATENT / UNKNOWN — проверка HP.
        if entity.hp_max > 0:
            # HP прочитан. LATENT: живой, но HP<=порога (умирает / пустышка).
            # Rare/boss не отсекаем — у них current может кратко читаться как 0.
            if entity.hp_current <= self.min_hp_threshold and not significant:
                return TargetStatus.LATENT
        else:
            # HP НЕ прочитан (холодный кэш Life). Поблажка только если разрешено
            # конфигом, либо это significant, либо моб уже «проявил себя» движением.
            moved = self._ever_engaged(entity.id)
            if not (self.attack_unknown_hp or significant or moved):
                return TargetStatus.UNKNOWN

        # 4) HP ok. Остался фильтр статики-призраков (движение/grace/огонь).
        return self._check_static(entity, significant)

    def _ever_engaged(self, eid: int) -> bool:
        """Моб когда-либо двигался или получал урон (значит он реальный)."""
        if not self.ignore_static:
            return True
        tr = self._static_track.get(eid)
        return bool(tr and (tr.get("ever_moved") or tr.get("ever_hit")))

    def _is_recently_dead(self, eid: int) -> bool:
        ts = self._dead_track.get(eid)
        if ts is None:
            return False
        return (time.perf_counter() - ts) < self.corpse_timeout_s

    def _compute_confidence(self, eid: int, is_alive: bool,
                            is_reachable: bool, hp_max: int,
                            hp_cur: int, significant: bool) -> float:
        """Confidence score (0.0-1.0) for the entity classification.

        Higher = more trustworthy the current status.
        Based on HP data availability, movement history, and death state.
        """
        c = 0.0
        if hp_max > 0:
            c += 0.3
        if hp_cur > 0:
            c += 0.2
        if is_reachable:
            c += 0.2
        if is_alive:
            c += 0.15
        # Movement/hit history boost
        tr = self._static_track.get(eid)
        if tr:
            if tr.get("ever_moved"):
                c += 0.1
            if tr.get("ever_hit"):
                c += 0.05
        # Corpse penalty
        if self._dead_track.get(eid) is not None:
            c -= 0.3
        # Unknown HP penalty (no Life component resolved yet)
        if hp_max == 0 and hp_cur == 0:
            c -= 0.2
        return max(0.0, min(1.0, round(c, 2)))

    def _check_static(self, entity: EntityData, significant: bool) -> str:
        """Фильтр статичных «призраков». COMBAT = атаковать, GHOST_STATIC = нет.

        Спящий утопленник/декоративный труп: жив в памяти, но НИ РАЗУ не двигался
        сверх grace-периода. Rare/boss не фильтруем (бьют с точки спавна).
        """
        if not self.ignore_static or significant:
            return TargetStatus.COMBAT
        now = time.perf_counter()
        # Игрок под вражеским огнём — в бою: стоячий стрелок становится целью.
        if now - self._player_fire_time < 2.0:
            return TargetStatus.COMBAT
        tr = self._static_track.get(entity.id)
        if tr is None:
            return TargetStatus.COMBAT  # нет трека — свежий, атакуем
        if tr.get("ever_moved") or tr.get("ever_hit"):
            return TargetStatus.COMBAT
        # Не двигался и не бит — призрак, если grace-период уже вышел.
        if now - tr.get("first_seen", 0.0) < self.static_grace_s:
            return TargetStatus.COMBAT  # grace — даём шанс шевельнуться
        return TargetStatus.GHOST_STATIC

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
        # Track deaths: entity was seen before but not in this frame -> dead
        for a, tr in list(self._static_track.items()):
            if a not in seen and tr.get("ever_moved"):
                self._dead_track[a] = now

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
        is_alive = bool(raw.get("alive", True))
        is_reachable = is_alive and (z_ok or significant)

        lc = raw.get("life") or []
        # ЧЕСТНОЕ чтение HP: life=[hp_max, hp_cur, ...] из reader_bridge._read_vitals.
        # Раньше здесь был фейк hp=1/1 для «живого без HP» — он маскировал мёртвых и
        # духов под «полный HP». Теперь если HP не прочитан — hp_max=hp_cur=0, и
        # решение принимает _classify_target (статус UNKNOWN/LATENT), а не враньё.
        if len(lc) >= 2 and (lc[0] > 0 or lc[1] > 0):
            hp_max = max(0, int(lc[0]))
            hp_cur = max(0, int(lc[1]))
        else:
            hp_max = 0
            hp_cur = 0
        hp_ratio = (hp_cur / hp_max) if hp_max > 0 else 1.0

        is_hostile = bool(raw.get("hostile", 0))
        is_friendly = not is_hostile
        eid = raw.get("addr", 0)

        dx = grid_x - player_x
        dy = grid_y - player_y
        distance = math.sqrt(dx * dx + dy * dy)

        confidence = self._compute_confidence(eid, is_alive, is_reachable,
                                             hp_max, hp_cur, significant)
        dead_since = self._dead_track.get(eid)

        return EntityData(
            id=eid, category=category, metadata=path,
            x=wx, y=wy, z=wz, grid_x=grid_x, grid_y=grid_y,
            hp_current=hp_cur, hp_max=hp_max, hp_ratio=hp_ratio,
            rarity=rarity, is_friendly=is_friendly,
            distance=distance, is_boss=is_boss, is_alive=is_alive,
            is_reachable=is_reachable,
            is_corpse=bool(self._dead_track.get(eid)),
            confidence=confidence, dead_since=dead_since,
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
