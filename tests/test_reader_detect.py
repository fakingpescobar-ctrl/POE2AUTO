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
    det.corpse_timeout_s = overrides.get("corpse_timeout_s", 30.0)
    det.death_grace_s = overrides.get("death_grace_s", 2.0)
    det.kill_cooldown_s = overrides.get("kill_cooldown_s", 5.0)
    det.min_hp_threshold = overrides.get("min_hp_threshold", 0)
    det.attack_unknown_hp = overrides.get("attack_unknown_hp", True)
    det._static_track = overrides.get("_static_track", {})
    det._dead_track = overrides.get("_dead_track", {})
    det._player_fire_time = overrides.get("_player_fire_time", 0.0)
    det.db = overrides.get("db", None)
    return det


def _mob(eid=1, hp_cur=800, hp_max=1000, alive=True, rarity=Rarity.NORMAL,
         is_boss=False, path="/Monsters/Test", friendly=False, distance=10.0,
         confidence=0.0, dead_since=None, is_corpse=False,
         category=None):
    """Создать EntityData-моба для классификации."""
    cat = category or EntityCategory.MONSTER
    return EntityData(
        id=eid, category=cat, metadata=path,
        x=0, y=0, z=0, grid_x=0, grid_y=0,
        hp_current=hp_cur, hp_max=hp_max,
        hp_ratio=(hp_cur / hp_max) if hp_max > 0 else 1.0,
        rarity=rarity, is_friendly=friendly, distance=distance,
        is_boss=is_boss, is_alive=alive, is_reachable=True,
        confidence=confidence, dead_since=dead_since,
        is_corpse=is_corpse,
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


# --- Confidence scoring ---

class TestConfidence:
    def test_combat_mob_has_high_confidence(self):
        det = _make_detector()
        det._static_track = {1: {"ever_moved": True, "ever_hit": True,
                                   "first_seen": 0.0, "last_seen": 0.0, "last": (0, 0)}}
        e = _mob(eid=1, hp_cur=800, hp_max=1000)
        # _classify_target doesn't set confidence; confidence is set in _parse_entity
        # Here we test the _compute_confidence method directly
        c = det._compute_confidence(1, True, True, 1000, 800, False)
        assert c > 0.5

    def test_dead_mob_has_low_confidence(self):
        det = _make_detector()
        det._dead_track = {1: time.perf_counter()}
        c = det._compute_confidence(1, False, True, 1000, 0, False)
        assert c < 0.5

    def test_no_hp_data_has_low_confidence(self):
        det = _make_detector()
        c = det._compute_confidence(1, True, True, 0, 0, False)
        assert c < 0.5

    def test_reachable_boosts_confidence(self):
        det = _make_detector()
        c_reachable = det._compute_confidence(1, True, True, 1000, 800, False)
        c_unreachable = det._compute_confidence(1, True, False, 1000, 800, False)
        assert c_reachable > c_unreachable

    def test_confidence_clamped_to_1(self):
        det = _make_detector()
        det._static_track = {1: {"ever_moved": True, "ever_hit": True,
                                   "first_seen": 0.0, "last_seen": 0.0, "last": (0, 0)}}
        c = det._compute_confidence(1, True, True, 1000, 800, True)
        assert c <= 1.0

    def test_confidence_clamped_to_0(self):
        det = _make_detector()
        det._dead_track = {1: time.perf_counter()}
        c = det._compute_confidence(1, False, False, 0, 0, False)
        assert c >= 0.0


# --- dead_since tracking ---

class TestDeadSince:
    def test_dead_since_set_for_recently_dead(self):
        det = _make_detector()
        now = time.perf_counter()
        det._dead_track = {1: now}
        e = _mob(eid=1, alive=False, hp_cur=0, dead_since=now)
        assert e.dead_since is not None
        assert e.dead_since == now

    def test_dead_since_none_for_alive(self):
        det = _make_detector()
        e = _mob(eid=1, alive=True, hp_cur=800, hp_max=1000)
        assert e.dead_since is None

    def test_is_recently_dead_uses_dead_since(self):
        det = _make_detector()
        now = time.perf_counter()
        det._dead_track = {1: now}
        assert det._is_recently_dead(1) is True

    def test_is_recently_dead_expires_after_timeout(self):
        det = _make_detector()
        det.corpse_timeout_s = 5.0
        det._dead_track = {1: time.perf_counter() - 10.0}
        assert det._is_recently_dead(1) is False


# --- death_grace_s: don't attack mobs that died recently ---

class TestDeathGrace:
    def test_recently_dead_not_combat(self):
        det = _make_detector(death_grace_s=2.0)
        now = time.perf_counter()
        det._dead_track = {1: now}
        e = _mob(eid=1, alive=True, hp_cur=800, hp_max=1000, dead_since=now)
        assert det._classify_target(e) == TargetStatus.DEAD

    def test_old_death_is_combat(self):
        det = _make_detector(death_grace_s=2.0)
        det._dead_track = {1: time.perf_counter() - 10.0}
        e = _mob(eid=1, alive=True, hp_cur=800, hp_max=1000, dead_since=time.perf_counter() - 10.0)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_no_dead_since_skips_grace(self):
        det = _make_detector(death_grace_s=2.0)
        e = _mob(eid=1, alive=True, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_zero_grace_disables(self):
        det = _make_detector(death_grace_s=0.0)
        now = time.perf_counter()
        det._dead_track = {1: now}
        e = _mob(eid=1, alive=True, hp_cur=800, hp_max=1000, dead_since=now)
        assert det._classify_target(e) == TargetStatus.COMBAT


# --- kill cooldown: don't attack recently killed mobs ---

class TestKillCooldown:
    def test_recently_killed_not_combat(self):
        det = _make_detector(kill_cooldown_s=5.0)
        now = time.perf_counter()

        class FakeDB:
            def get_recently_killed_addrs(self, cooldown_s):
                return {1}

        det.db = FakeDB()
        e = _mob(eid=1, alive=True, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.DEAD

    def test_old_kill_is_combat(self):
        det = _make_detector(kill_cooldown_s=5.0)

        class FakeDB:
            def get_recently_killed_addrs(self, cooldown_s):
                return set()  # no recently killed mobs

        det.db = FakeDB()
        e = _mob(eid=1, alive=True, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_no_db_skips_cooldown(self):
        det = _make_detector(kill_cooldown_s=5.0)
        det.db = None
        e = _mob(eid=1, alive=True, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT

    def test_zero_cooldown_disables(self):
        det = _make_detector(kill_cooldown_s=0.0)

        class FakeDB:
            def get_recently_killed_addrs(self, cooldown_s):
                return {1}

        det.db = FakeDB()
        e = _mob(eid=1, alive=True, hp_cur=800, hp_max=1000)
        assert det._classify_target(e) == TargetStatus.COMBAT


# --- get_corpses_in_range ---

class TestCorpsesInRange:
    def test_corpse_in_range(self):
        det = _make_detector()
        now = time.perf_counter()
        det._last_entities = [
            _mob(eid=1, alive=False, hp_cur=0, distance=3.0,
                 is_corpse=True, dead_since=now - 1.0),
        ]
        corpses = det.get_corpses_in_range(radius=5.0)
        assert len(corpses) == 1
        assert corpses[0].id == 1

    def test_corpse_out_of_range(self):
        det = _make_detector()
        now = time.perf_counter()
        det._last_entities = [
            _mob(eid=1, alive=False, hp_cur=0, distance=10.0,
                 is_corpse=True, dead_since=now - 1.0),
        ]
        corpses = det.get_corpses_in_range(radius=5.0)
        assert len(corpses) == 0

    def test_old_corpse_excluded(self):
        det = _make_detector()
        now = time.perf_counter()
        det._last_entities = [
            _mob(eid=1, alive=False, hp_cur=0, distance=3.0,
                 is_corpse=True, dead_since=now - 40.0),
        ]
        corpses = det.get_corpses_in_range(radius=5.0)
        assert len(corpses) == 0

    def test_alive_mob_not_corpses(self):
        det = _make_detector()
        now = time.perf_counter()
        det._last_entities = [
            _mob(eid=1, alive=True, hp_cur=800, hp_max=1000, distance=3.0),
        ]
        corpses = det.get_corpses_in_range(radius=5.0)
        assert len(corpses) == 0

    def test_non_monster_not_corpses(self):
        det = _make_detector()
        now = time.perf_counter()
        det._last_entities = [
            _mob(eid=1, category=EntityCategory.CHEST, alive=False,
                 hp_cur=0, distance=3.0, is_corpse=True, dead_since=now - 1.0),
        ]
        corpses = det.get_corpses_in_range(radius=5.0)
        assert len(corpses) == 0


# --- confidence in get_best_target ---

class TestBestTargetConfidence:
    def test_high_confidence_preferred(self):
        det = _make_detector()
        det._static_track = {}
        det._last_entities = [
            _mob(eid=1, hp_cur=800, hp_max=1000, distance=10.0, confidence=0.9),
            _mob(eid=2, hp_cur=800, hp_max=1000, distance=5.0, confidence=0.2),
        ]
        best = det.get_best_target()
        assert best is not None
        assert best.id == 1  # high confidence wins over closer low-confidence

    def test_boss_overrides_confidence(self):
        det = _make_detector()
        det._static_track = {}
        det._last_entities = [
            _mob(eid=1, hp_cur=800, hp_max=1000, distance=10.0, confidence=0.9, is_boss=False),
            _mob(eid=2, hp_cur=800, hp_max=1000, distance=15.0, confidence=0.3, is_boss=True),
        ]
        best = det.get_best_target()
        assert best is not None
        assert best.id == 2  # boss wins
