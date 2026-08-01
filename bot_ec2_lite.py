"""POE2AUTO Lite — ExileCore2 + auto-attack only.

Минимальный бот: только то, что нужно для твоего сценария.
Ты играешь сам (DualSense), бот видит мобов через EC2 и жмёт R1.

Что делает:
  1. Создаёт виртуальный Xbox 360 + мост DualSense (чтобы PoE2 не уходил в AFK).
  2. Подключается к ExileCore2 ShareData (127.0.0.1:50006).
  3. Главный цикл ~20Hz:
     - EC2MobDetector.update() → читает позиции/HP всех мобов
     - Есть враждебный моб в радиусе атаки → жмём R1
     - Моб вышел из радиуса → отпускаем R1
     - HP/Mana ниже порога → жмём флягу
  4. Показывает оверлей: статус атаки/фляски, HP%, зона, кол-во мобов, FPS.
  5. НЕ делает: CV-захват экрана, подбор лута, калибровки.

Хоткеи: F8 — вкл/выкл авто-атаку, F3 — вкл/выкл авто-фляску,
         F6 — скрыть/показать оверлей, F12 — выход.

Запуск:
    1. ExileCore2 Loader.exe (инжектится в PoE2, поднимает ShareData TCP)
    2. python bot_ec2_lite.py
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time

from pynput import keyboard

from config import load_config
from ec2_detect import EC2MobDetector
from virtual_xbox import VirtualXbox360, XUSB
from dualsense_bridge import DualSenseBridge
from overlay import Overlay, OverlayState
from utils import Toggle

LOG_PATH = os.path.join(os.path.dirname(__file__), "bot_ec2_lite.log")

_fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_root.addHandler(_sh)
_fh = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
_root.addHandler(_fh)

log = logging.getLogger("poe2auto.lite")


class AutoFlaskRule:
    """Правило авто-фляски (упрощённая версия из auto_flask.py)."""
    def __init__(self, name: str, key: str, hp_below: float = None,
                 mana_below: float = None, enemies_nearby: int = 0,
                 enemy_radius: float = 0, cooldown_s: float = 1.0):
        self.name = name
        self.key = key  # "left_trigger" / "right_trigger"
        self.hp_below = hp_below
        self.mana_below = mana_below
        self.enemies_nearby = enemies_nearby
        self.enemy_radius = enemy_radius
        self.cooldown_s = cooldown_s
        self.last_used = 0.0

    def matches(self, hp_pct, mana_pct, enemies_count) -> bool:
        if self.hp_below is not None and (hp_pct is None or hp_pct >= self.hp_below):
            return False
        if self.mana_below is not None and (mana_pct is None or mana_pct >= self.mana_below):
            return False
        if self.enemies_nearby and enemies_count < self.enemies_nearby:
            return False
        return True

    def ready(self) -> bool:
        return time.perf_counter() - self.last_used >= self.cooldown_s


def main() -> int:
    cfg = load_config()
    atk_cfg = cfg.get("auto_attack", {})
    ec2_cfg = cfg.get("exilecore2", {})
    flask_cfg = cfg.get("auto_flask", {})

    # --- геймпад: виртуальный Xbox + мост DualSense ---
    vx = VirtualXbox360()
    if not vx.start():
        log.error("Не удалось создать виртуальный Xbox 360 (ViGEmBus/vgamepad?)")
        return 1
    bridge = DualSenseBridge(vx)
    if not bridge.start():
        log.warning("DualSense не найден — управление с него не пойдёт. "
                    "Подключи геймпад по USB и перезапусти.")

    atk_btn = XUSB.get(atk_cfg.get("button", "right_bumper"), XUSB["right_bumper"])
    atk_grace = atk_cfg.get("release_grace_s", 0.6)
    attack_range = atk_cfg.get("attack_range", 8.0)

    # --- EC2 детектор ---
    det = EC2MobDetector({
        "attack_range": attack_range,
        "ec2_host": ec2_cfg.get("host", "127.0.0.1"),
        "ec2_port": ec2_cfg.get("port", 50006),
    })
    log.info("Подключение к ExileCore2 ShareData (%s:%d)...",
             ec2_cfg.get("host", "127.0.0.1"), ec2_cfg.get("port", 50006))
    if not det.connect():
        log.error("Не удалось подключиться к ShareData. "
                  "Запусти ExileCore2 Loader.exe с плагином ShareData.")
        vx.stop()
        return 1
    log.info("EC2 подключён! Радиус атаки: %.1f", attack_range)

    # --- авто-фляска правила ---
    flask_rules = []
    flask_enabled = flask_cfg.get("enabled", True)
    if flask_enabled:
        for r in flask_cfg.get("rules", []):
            flask_rules.append(AutoFlaskRule(
                name=r.get("name", ""),
                key=r.get("key", "left_trigger"),
                hp_below=r.get("hp_below"),
                mana_below=r.get("mana_below"),
                enemies_nearby=r.get("enemies_nearby", 0),
                enemy_radius=r.get("enemy_radius", 0),
                cooldown_s=r.get("cooldown_s", 1.0),
            ))

    # --- хоткеи ---
    attack = Toggle(default=True)
    flask_toggle = Toggle(default=True)

    # --- оверлей ---
    from window import GameWindow
    win = GameWindow(cfg.get("game", {}).get("window_title", "Path of Exile 2"))
    region = None
    if win.find():
        region = win.get_region()
    if not region:
        region = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        log.warning("Окно PoE2 не найдено — оверлей в углу экрана")

    ov_state = OverlayState()
    ov_state.attack_enabled = True
    ov_state.flask_enabled = flask_enabled
    ov_state.attack_mode = "EC2"
    ov_state.diag = True  # lite-бот всегда показывает debug-строки (зона, мобы, HP)
    overlay = Overlay(ov_state, region, cfg.get("hotkeys", {}))
    if not overlay.start():
        log.warning("Оверлей не запущен (tkinter недоступен?)")

    def on_key(key):
        try:
            k = key.char.lower()
        except AttributeError:
            k = str(key).lower().replace("key.", "")
        hk = cfg.get("hotkeys", {})
        if k == hk.get("toggle", "f8"):
            state = attack.flip()
            ov_state.attack_on = state
            log.info("Авто-атака: %s", "ВКЛ" if state else "ВЫКЛ")
        elif k == hk.get("flask", "f3"):
            state = flask_toggle.flip()
            ov_state.flask_on = state
            log.info("Авто-фляска: %s", "ВКЛ" if state else "ВЫКЛ")
        elif k == hk.get("overlay", "f6"):
            vis = overlay.toggle()
            log.info("Оверлей: %s", "показ" if vis else "скрыт")
        elif k == hk.get("quit", "f12"):
            log.info("Выход (F12)")
            raise keyboard.Listener.StopException

    listener = keyboard.Listener(on_press=on_key)
    listener.start()

    log.info("=== POE2AUTO Lite готов ===")
    log.info("F8=атака(%s)  F3=фляска(%s)  F12=выход",
             "ВКЛ" if attack.on else "ВЫКЛ",
             "ВКЛ" if flask_toggle.on else "ВЫКЛ")

    r1_held = False
    last_bar_seen = 0.0
    last_status = time.perf_counter()
    iters = 0
    flasks_count = 0

    try:
        while True:
            now = time.perf_counter()
            iters += 1

            # Обновляем данные из EC2
            det.update()

            # Обновляем оверлей каждый кадр
            hp_pct_ov, mana_pct_ov = det.read_player_vitals() if det.available else (None, None)
            ov_state.hp_pct = hp_pct_ov
            ov_state.foreground = True  # lite-бот всегда считает что в фокусе
            ov_state.attack_on = attack.on
            ov_state.flask_on = flask_toggle.on
            ov_state.attacking = r1_held

            if det.in_hideout():
                # В убежище — не атакуем
                if r1_held:
                    vx.release(atk_btn)
                    r1_held = False
            elif attack.on and det.available:
                hostiles = det.get_hostiles_in_range()
                mob_present = len(hostiles) > 0

                if mob_present:
                    last_bar_seen = now
                    if not r1_held:
                        vx.press(atk_btn)
                        r1_held = True
                        log.info("ATTACK: %d моб(ов) в радиусе %.1f", len(hostiles), attack_range)
                        ov_state.notification = f"⚔ АТАКА: {len(hostiles)} моб в радиусе"
                        ov_state.notification_until = time.time() + 1.5
                elif r1_held and now - last_bar_seen > atk_grace:
                    vx.release(atk_btn)
                    r1_held = False
            elif r1_held:
                vx.release(atk_btn)
                r1_held = False

            # Авто-фляска
            if flask_toggle.on and flask_enabled and det.available and not det.in_hideout():
                hp_pct, mana_pct = det.read_player_vitals()
                enemies = len(det.get_hostiles_in_range())
                for rule in flask_rules:
                    if rule.ready() and rule.matches(hp_pct, mana_pct, enemies):
                        rule.last_used = now
                        flasks_count += 1
                        bridge.suppress_triggers = True
                        def _do_flask(key_name, hold_ms=120):
                            if "right" in key_name:
                                vx.tap_right_trigger(hold_ms)
                            else:
                                vx.tap_left_trigger(hold_ms)
                            bridge.suppress_triggers = False
                        threading.Thread(target=_do_flask,
                                         args=(rule.key,), daemon=True).start()
                        log.info("ФЛЯСКА [%s]: HP=%s Mana=%s enemies=%d",
                                 rule.name,
                                 f"{hp_pct*100:.0f}%" if hp_pct else "?",
                                 f"{mana_pct*100:.0f}%" if mana_pct else "?",
                                 enemies)
                        ov_state.notification = f"🧪 Фляска: {rule.name}"
                        ov_state.notification_until = time.time() + 2.0
                        break

            # Статус раз в секунду
            if now - last_status >= 1.0:
                fps = iters / (now - last_status)
                hp_pct, mana_pct = det.read_player_vitals()
                hostiles = len(det.get_hostiles_in_range())
                total = det.stats.get("total_entities", 0)
                area = det.get_area_info()
                in_ho = det.in_hideout()
                log.info(
                    "zone=%s hp=%s mana=%s mobs_total=%d in_range=%d "
                    "r1=%s atk=%s flask=%s fps=%.0f",
                    area["code"][-30:] if area["code"] else "?",
                    f"{hp_pct*100:.0f}%" if hp_pct else "-",
                    f"{mana_pct*100:.0f}%" if mana_pct else "-",
                    total, hostiles,
                    "ON" if r1_held else "off",
                    "ON" if attack.on else "off",
                    "ON" if flask_toggle.on else "off",
                    fps)
                # Обновляем строку статистики в оверлее
                zone_short = area["code"].split("/")[-1][:18] if area["code"] else "?"
                ov_state.fps = fps
                ov_state.flasks = flasks_count
                ov_state.debug_mob = (
                    f"zone={zone_short} {'🔒hideout' if in_ho else '⚔'} "
                    f"mobs={total} range={hostiles}"
                )
                ov_state.debug_hp = (
                    f"HP={hp_pct*100:.0f}% Mana={mana_pct*100:.0f}% "
                    if hp_pct and mana_pct else "HP=- Mana=- "
                ) + f"r1={'ON' if r1_held else 'off'}"
                ov_state.memory_status = "ok" if det.available else "off"
                last_status = now
                iters = 0

            time.sleep(0.05)  # 20Hz

    except (KeyboardInterrupt, keyboard.Listener.StopException):
        log.info("Остановка...")
    finally:
        if r1_held:
            vx.release(atk_btn)
        det.disconnect()
        listener.stop()
        overlay.stop()
        bridge.stop()
        vx.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
