"""Standalone диагностика ExileCore2 ShareData.

Запуск БЕЗ бота — только проверка связи и просмотр мобов в реальном времени:
    python ec2_test.py

Показывает:
  - Статус подключения к ShareData
  - Текущую зону (area_raw_name)
  - HP/Mana игрока
  - Всех мобов с позициями, HP, расстоянием до игрока
  - Какие мобы в радиусе атаки (highlight)

Хоткеи: Q — выход, R — изменить радиус атаки, H — refresh area
"""
from __future__ import annotations

import logging
import sys
import time

from ec2_detect import EC2MobDetector

# Геймпад нужен чтобы PoE2 не уходил в AFK режим
# (без виртуального Xbox 360 + DualSense bridge игра "засыпает" и ShareData перестаёт отдавать данные)
try:
    from virtual_xbox import VirtualXbox360
    from dualsense_bridge import DualSenseBridge
    HAS_GAMEPAD = True
except ImportError:
    HAS_GAMEPAD = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("ec2test")


def fmt_dist(d: float) -> str:
    return f"{d:5.1f}"


def fmt_hp(cur: int, mx: int) -> str:
    if mx <= 0:
        return "  ---/---  "
    pct = cur / mx * 100
    return f"{cur:5d}/{mx:5d} ({pct:3.0f}%)"


def main():
    print("=" * 72)
    print("  EC2 ShareData Diagnostics — POE2AUTO")
    print("  Подключение к ExileCore2 ShareData (127.0.0.1:50006)")
    print("=" * 72)
    print()

    attack_range = 8.0
    det = EC2MobDetector({"attack_range": attack_range})

    # Поднимаем геймпад чтобы PoE2 не ушёл в AFK
    vx = None
    bridge = None
    if HAS_GAMEPAD:
        print("[0] Подъём виртуального геймпада (anti-AFK)...")
        vx = VirtualXbox360()
        if vx.start():
            bridge = DualSenseBridge(vx)
            bridge.start()
            print("✅ Геймпад активен (PoE2 не уйдёт в AFK)")
        else:
            print("⚠️  Не удалось создать виртуальный геймпад — продолжаем без него")
            vx = None
    else:
        print("⚠️  vgamepad/hid не установлен — геймпад не поднят. "
              "PoE2 может уйти в AFK.")

    print()
    print("[1] Подключение к ShareData...")
    if not det.connect():
        print()
        print("❌ НЕ УДАЛОСЬ подключиться к ShareData!")
        print()
        print("Проверь:")
        print("  1. PoE2 запущен?")
        print("  2. ExileCore2 Loader.exe запущен? (должен инжектиться в PoE2)")
        print("  3. Плагин ShareData включён в ExileCore2?")
        print("  4. Порт 50006 свободен? (netstat -an | findstr 50006)")
        print()
        print("Установка ExileCore2 + ShareData:")
        print("  См. docs/EC2_SETUP.md")
        return 1

    print("✅ Подключено!")
    print()

    # Проверяем первые данные
    print("[2] Ожидание данных (зайди в игру, выйди из меню)...")
    for i in range(20):
        det.update()
        if det.available:
            break
        time.sleep(0.5)
        sys.stdout.write(".")
        sys.stdout.flush()

    if not det.available:
        print()
        print("⚠️  Подключение есть, но данные не приходят.")
        print("    Возможные причины:")
        print("    - Персонаж в главном меню (зайди в игру)")
        print("    - Экран загрузки зоны")
        print("    - ShareData плагин не получает Tick() от ExileCore2")
        print()
        print("    Останусь в режиме ожидания — можешь зайти в игру.")
        print("    Нажми Q для выхода.\n")

    print()
    print("[3] Мониторинг мобов. Q=выход, R=радиус, H=refresh area")
    print(f"    Радиус атаки: {attack_range}")
    print()

    import tty
    import termios
    import select

    old_settings = termios.tcgetattr(sys.stdin) if sys.platform != "win32" else None

    try:
        while True:
            # Non-blocking key check (Windows)
            if sys.platform == "win32":
                import msvcrt
                while msvcrt.kbhit():
                    key = msvcrt.getch().decode("ascii", errors="ignore").lower()
                    if key == "q":
                        print("\nВыход...")
                        return 0
                    elif key == "r":
                        attack_range = attack_range + 2.0 if attack_range < 30 else 4.0
                        det.attack_range = attack_range
                        print(f"\n→ Радиус: {attack_range}")
                    elif key == "h":
                        det.client.force_refresh_area()
                        print("\n→ ForceRefreshArea отправлен")
            else:
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1).lower()
                    if key == "q":
                        return 0
                    elif key == "r":
                        attack_range = attack_range + 2.0 if attack_range < 30 else 4.0
                        det.attack_range = attack_range
                        print(f"\n→ Радиус: {attack_range}")
                    elif key == "h":
                        det.client.force_refresh_area()

            det.update()

            # Очистка экрана
            print("\033[2J\033[H", end="")

            area = det.get_area_info()
            in_ho = det.in_hideout()
            hp_pct, mana_pct = det.read_player_vitals()

            hp_s = f"{hp_pct*100:.0f}%" if hp_pct is not None else "---"
            mana_s = f"{mana_pct*100:.0f}%" if mana_pct is not None else "---"

            hostiles_all = det.get_all_hostiles()
            in_range = det.get_hostiles_in_range()

            print("=" * 72)
            zone_label = "🔒 HIDEOUT" if in_ho else "⚔️  IN COMBAT ZONE"
            print(f"  Zone: {area['code'][:50]:<50} {zone_label}")
            print(f"  Player: HP={hp_s}  Mana={mana_s}  "
                  f"State={area['game_state']}")
            print(f"  Range: {attack_range}  |  Mobs total: {len(hostiles_all)}  "
                  f"|  in range: {len(in_range)}")
            print("-" * 72)

            if not hostiles_all:
                print("  Нет враждебных мобов поблизости.")
            else:
                # Сортируем по расстоянию
                sorted_mobs = sorted(hostiles_all, key=lambda e: e.distance)
                print(f"  {'ID':>6}  {'HP':>20}  {'Dist':>6}  {'Rarity':<8}  "
                      f"{'GridPos':<16}  {'In Range':>8}")
                print("  " + "-" * 68)
                for m in sorted_mobs[:20]:
                    in_r = "► ATTACK" if m.distance <= attack_range else ""
                    rarity_s = m.rarity.name.lower()
                    boss_s = " 👑" if m.is_boss else ""
                    print(f"  {m.id:>6}  {fmt_hp(m.hp_current, m.hp_max)}  "
                          f"{fmt_dist(m.distance)}  {rarity_s:<8}{boss_s}  "
                          f"({m.grid_x:.0f},{m.grid_y:.0f})".ljust(50) +
                          f"  {in_r:>8}")

            if len(hostiles_all) > 20:
                print(f"  ... и ещё {len(hostiles_all) - 20} мобов")

            print("=" * 72)
            print("  Q=выход  R=радиус±  H=refresh  | "
                  f"TCP: {'✅' if det.client.connected else '❌'}")

            time.sleep(0.15)

    except KeyboardInterrupt:
        print("\nПрервано (Ctrl+C)")
    finally:
        det.disconnect()
        if bridge:
            bridge.stop()
        if vx:
            vx.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
