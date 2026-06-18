"""POE2AUTO — шаг 1: захват DualSense через виртуальный Xbox 360.

Создаёт виртуальный Xbox 360, открывает реальный DualSense по HID и
пробрасывает в него весь ввод (стики/триггеры/кнопки). Для PoE2 контроллер
выглядит как Xbox. Команды бота (подбор и т.д.) будем слать в этот же
виртуальный геймпад на следующих шагах.

Запуск (нужны права администратора для ViGEmBus):
    python main.py
"""
import logging
import time

from virtual_xbox import VirtualXbox360
from dualsense_bridge import DualSenseBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("poe2auto")


def main():
    log.info("=== POE2AUTO :: захват джойстика ===")

    vx = VirtualXbox360()
    if not vx.start():
        log.error("Не удалось создать виртуальный Xbox 360. "
                  "Установлен ли ViGEmBus и vgamepad?")
        return 1

    bridge = DualSenseBridge(vx)
    if not bridge.start():
        log.warning("DualSense не найден по HID. Виртуальный Xbox активен, "
                    "но пробрасывать нечего. Подключи DualSense по USB/BT.")

    log.info("Готово. DualSense → виртуальный Xbox 360. Ctrl+C для выхода.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C")
    finally:
        bridge.stop()
        vx.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
