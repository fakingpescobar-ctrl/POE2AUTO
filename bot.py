"""POE2AUTO — авто-подбор лута, авто-атака, авто-фляска через виртуальный геймпад.

Оркестрирует: VirtualXbox360 + DualSenseBridge, захват экрана, детекция лута/HP/мобов,
навигация (CoreMap), stash flow, map run flow, оверлей.

Модульная структура (bot/):
  bot/state.py       — BotState: общий контейнер состояния
  bot/hp.py          — HP geometry, locate HUD, flask execution
  bot/hotkeys.py     — keyboard handler
  bot/reload.py      — hot-reload config
  bot/overlay_sync.py — overlay state updates
  bot/main_loop.py   — init + main loop + cleanup

Запуск: python bot.py
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

LOG_PATH = os.path.join(os.path.dirname(__file__), "bot.log")
_fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")


def _setup_logging() -> None:
    """Настроить корневой логгер: консоль + файл (ротация 3 × 1 МБ)."""
    _root = logging.getLogger()
    if _root.handlers:
        return
    _root.setLevel(logging.INFO)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _root.addHandler(_sh)
    _fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(_fmt)
    _root.addHandler(_fh)


log = logging.getLogger("poe2auto.bot")


def main() -> int:
    _setup_logging()
    from bot.main_loop import main as _main
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())
