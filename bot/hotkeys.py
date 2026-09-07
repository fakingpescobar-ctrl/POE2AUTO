"""Keyboard handler — dispatches hotkeys to BotState actions."""
from __future__ import annotations

import logging
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from bot.state import BotState

log = logging.getLogger("poe2auto.bot.hotkeys")


def on_press(key, s: BotState) -> Optional[bool]:
    """pynput keyboard callback. Returns False to stop listener."""
    name = getattr(key, "name", None)
    if name is None:
        return

    if name == s.k_quit:
        log.info("Выход (F12)")
        s.stop.set()
        return False
    if name == s.k_stash:
        s.trigger_stash()
        return
    if name == s.k_map_run:
        s.trigger_map_run()
        return
    if name == s.k_toggle:
        state = s.auto.flip()
        log.info("Авто-подбор: %s", "ВКЛ" if state else "выкл")
    if name == s.k_diag:
        state = s.diag.toggle()
        log.info("Диагностика HP: %s (улики → _debug/hp_events)",
                 "ВКЛ — грабим полный кадр" if state else "выкл")
    if name == s.k_shot:
        s.manual_shot.set()
    if name == s.k_overlay:
        if s.overlay is not None:
            vis = s.overlay.toggle()
            log.info("Оверлей: %s", "показан" if vis else "скрыт")
    if name == s.k_attack and s.mob_det is not None:
        state = s.attack.flip()
        log.info("Авто-атака: %s", "ВКЛ" if state else "выкл")
    if name == s.k_flask and s.hpg["hp_det"] is not None:
        state = s.flask.flip()
        log.info("Авто-фляска: %s", "ВКЛ" if state else "выкл")
    if name == s.k_radial:
        s.ov_state.radial_visible = not s.ov_state.radial_visible
        log.info("Круг радиуса: %s (R=%.0f, scale=%.1f px/grid)",
                 "показан" if s.ov_state.radial_visible else "скрыт",
                 s.ov_state.attack_range, s.ov_state.radial_scale)
    if name == s.k_reload:
        from bot.reload import reload_cfg
        reload_cfg(s)
    if name == s.k_nav and s.nav_worker is not None:
        if s.nav_worker.is_active():
            s.nav_worker.clear_target()
            log.info("Навигация: остановлена")
        else:
            tgt = s.nav_worker._read_target_file()
            if tgt:
                s.nav_worker.set_target(*tgt)
                log.info("Навигация: возобновлена к (%.1f, %.1f)", tgt[0], tgt[1])
            else:
                log.info("Навигация: нет цели в nav_target.json")
