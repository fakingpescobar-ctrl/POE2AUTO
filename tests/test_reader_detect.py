"""Тесты для ReaderMobDetector — многоуровневая классификация целей.

Проверяет _classify_target (логику «жив/мёртв/дух/призрак/боеготов») без
загрузки .NET runtime: детектор создаётся через object.__new__, минуя
ReaderBridge (который грузит PoE2ReaderLib.dll).
"""
import time

from entity_reader import EntityCategory, Rarity, EntityData
from reader_detect import ReaderMobDetector, TargetStatus


# --- Helpers ---

def _make_detector(**overrides):
    """Создать ReaderMobDetector без загрузки .NET (для тестов классификации)."""
    det = object.__new__(ReaderMobDetector)
    det.attack_range = overrides.get("attack_range", 15.0)
    det.ignore_static = overrides.get("ignore_static", True)
    det.static_grace_s = overrides.get("static_grace_s", 2.0)
    det.min_hp_threshold = overrides.get("min_hp_threshold", 0)
    det.attack_unknown_hp = overrides.get("attack_unknown_hp", True)
    det._static_track = overrides.get("_static_track", {})
    det._player_fire_time = overrides.get("_player_fire_time", 0.0)
    return det


def _mob(eid=1, hp_cur=800, hp_max=1000, alive=True, rarity=Rarity.NORMAL,
         is_boss=False, path="/Monsters/Test", friendly=False, distance=10.0):
    """Создать EntityData-моба для классификации."""
    return EntityData(
        id=eid, category=EntityCategory.MONSTER, metadata=path,
        x=0, y=0, z=0, grid_x=0, grid_y=0,
        hp_current=hp_cur, hp_max=hp_max,
        hp_ratio=(hp_cur / hp_max) if hp_max > 0 else 1.0,
        rarity=rarity, is_friendly=friendly, distance=distance,
        is_boss=is_boss, is_alive=alive,
    )


# --- DEAD (труп) ---

class TestDead:
    def test_corpse_is_dead(self):
        det = _make_detector()
        e = _mob(alive=False, hp_cur=0)
        assert det._classify_target(e) == TargetStatus.DEAD

    def test_corpse_with_hp_still_dead(self):
        # is_alive=False бьёт раньше HP (флаг смерти Life+0x10 решающий)
        det = _make_detector()
        e = _mob(alive=False, hp_cur=500, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.DEAD


# --- SPIRIT (дух/немоб) ---

class TestSpirit:
    def test_tormented_spirit(self):
        det = _make_detector()
        e = _mob(path="/Monsters/TormentedSpirit/Guardian")
        assert det._classify_target(e) == TargetStatus.SPIRIT

    def test_summoned_daemon(self):
        det = _make_detector()
        e = _mob(path="/Monsters/Summoned/Daemon")
        assert det._classify_target(e) == TargetStatus.SPIRIT

    def test_invisible(self):
        det = _make_detector()
        e = _mob(path="/Monsters/InvisibleThing")
        assert det._classify_target(e) == TargetStatus.SPIRIT


# --- LATENT (умирает, HP<=порога) ---

class TestLatent:
    def test_zero_hp_normal_is_latent(self):
        det = _make_detector()
        e = _mob(hp_cur=0, hp_max=1000, rarity=Rarity.NORMAL)
        assert det._classify_target(e) == TargetStatus.LATENT

    def test_below_threshold_is_latent(self):
        det = _make_detector(min_hp_threshold=50)
        e = _mob(hp_cur=30, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.LATENT

    def test_rare_zero_hp_not_latent(self):
        # Rare/боссы не отсекаются порогом (current может кратко читаться как 0)
        det = _make_detector()
        e = _mob(hp_cur=0, hp_max=1000, rarity=Rarity.RARE)
        assert det._classify_target(e) != TargetStatus.LATENT

    def test_boss_zero_hp_not_latent(self):
        det = _make_detector()
        e = _mob(hp_cur=0, hp_max=10000, is_boss=True)
        assert det._classify_target(e) != TargetStatus.LATENT


# --- UNKNOWN (HP не прочитан) ---

class TestUnknown:
    def test_unknown_hp_no_attack(self):
        det = _make_detector(attack_unknown_hp=False)
        e = _mob(hp_cur=0, hp_max=0)  # HP не прочитан
        assert det._classify_target(e) == TargetStatus.UNKNOWN

    def test_unknown_hp_attack_allowed(self):
        # attack_unknown_hp=True → идёт дальше к проверке статики
        det = _make_detector(attack_unknown_hp=True)
        det._static_track = {}  # нет трека → свежий → COMBAT
        e = _mob(hp_cur=0, hp_max=0)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_unknown_hp_rare_attacks(self):
        # Rare атакуется даже при неизвестном HP и attack_unknown_hp=False
        det = _make_detector(attack_unknown_hp=False)
        det._static_track = {}
        e = _mob(hp_cur=0, hp_max=0, rarity=Rarity.RARE)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_unknown_hp_moved_attacks(self):
        # Моб двигался → реальный, атакуем даже при неизвестном HP
        det = _make_detector(attack_unknown_hp=False)
        det._static_track = {1: {"ever_moved": True, "ever_hit": False,
                                 "first_seen": 0.0, "last_seen": 0.0, "last": (0, 0)}}
        e = _mob(eid=1, hp_cur=0, hp_max=0)
        assert det._classify_target(e) == TargetStatus.COMBAT


# --- GHOST_STATIC (призрак, не двигался) ---

class TestGhostStatic:
    def test_static_past_grace_is_ghost(self):
        det = _make_detector(static_grace_s=2.0)
        det._static_track = {1: {"ever_moved": False, "ever_hit": False,
                                 "first_seen": time.perf_counter() - 10.0,
                                 "last_seen": 0.0, "last": (0, 0)}}
        e = _mob(eid=1, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.GHOST_STATIC

    def test_static_within_grace_is_combat(self):
        det = _make_detector(static_grace_s=2.0)
        det._static_track = {1: {"ever_moved": False, "ever_hit": False,
                                 "first_seen": time.perf_counter(),  # только что
                                 "last_seen": 0.0, "last": (0, 0)}}
        e = _mob(eid=1, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_moved_is_combat_not_ghost(self):
        det = _make_detector()
        det._static_track = {1: {"ever_moved": True, "ever_hit": False,
                                 "first_seen": 0.0, "last_seen": 0.0, "last": (0, 0)}}
        e = _mob(eid=1, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_hit_is_combat_not_ghost(self):
        det = _make_detector()
        det._static_track = {1: {"ever_moved": False, "ever_hit": True,
                                 "first_seen": 0.0, "last_seen": 0.0, "last": (0, 0)}}
        e = _mob(eid=1, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_player_fire_unlocks_static(self):
        # Игрок под огнём → призрак становится целью (стоячий стрелок)
        det = _make_detector()
        det._static_track = {1: {"ever_moved": False, "ever_hit": False,
                                 "first_seen": 0.0, "last_seen": 0.0, "last": (0, 0)}}
        det._player_fire_time = time.perf_counter()  # только что под огнём
        e = _mob(eid=1, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_ignore_static_off_never_ghost(self):
        det = _make_detector(ignore_static=False)
        det._static_track = {1: {"ever_moved": False, "ever_hit": False,
                                 "first_seen": 0.0, "last_seen": 0.0, "last": (0, 0)}}
        e = _mob(eid=1, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT


# --- COMBAT (боеготов) ---

class TestCombat:
    def test_healthy_mob_in_grace(self):
        det = _make_detector()
        det._static_track = {}
        e = _mob(hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_boss_combat_even_static(self):
        det = _make_detector()
        det._static_track = {1: {"ever_moved": False, "ever_hit": False,
                                 "first_seen": 0.0, "last_seen": 0.0, "last": (0, 0)}}
        e = _mob(eid=1, hp_cur=8000, hp_max=10000, is_boss=True)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_rare_combat_even_static(self):
        det = _make_detector()
        det._static_track = {1: {"ever_moved": False, "ever_hit": False,
                                 "first_seen": 0.0, "last_seen": 0.0, "last": (0, 0)}}
        e = _mob(eid=1, hp_cur=800, hp_max=1000, rarity=Rarity.RARE)
        assert det._classify_target(e) == TargetStatus.COMBAT


# --- ATTACKABLE множество ---

class TestAttackable:
    def test_only_combat_is_attackable(self):
        assert TargetStatus.ATTACKABLE == frozenset({TargetStatus.COMBAT})

    def test_living_set_excludes_dead_and_spirit(self):
        assert TargetStatus.DEAD not in TargetStatus.LIVING
        assert TargetStatus.SPIRIT not in TargetStatus.LIVING
        assert TargetStatus.COMBAT in TargetStatus.LIVING
