"""Stash flow — обёртка для stash_reader + stash_tab_browser, вызываемая из bot.py.

Использование из bot.py (где уже созданы `vx` и `mem`):

    from stash_flow import open_maps_tab, list_stash_tabs, pick_map_for_device

    # по горячей клавише F11 (или из nav-target):
    ok, msg, maps = open_maps_tab(vx, mem)
    if ok and maps:
        pick_map_for_device(vx, mem, maps[0])

ВАЖНО: `mem` здесь — открытый GameMemory() (как в bot.py через reader_bridge).
`vx` — VirtualXbox360 того же процесса (не joy_daemon — он конфликтует).

StashTabBrowser не создаёт свой vx, а использует переданный (attach_controller(vx)).
"""
import logging
from typing import Optional, List, Tuple

from game_memory import GameMemory
from stash_reader import StashItem
from stash_tab_browser import StashTabBrowser

_log = logging.getLogger("poe2auto.stash_flow")


def _make_browser(mem: GameMemory, vx) -> StashTabBrowser:
    br = StashTabBrowser(mem)
    br.attach_controller(vx)  # не создаёт свой vx — использует переданный
    return br


def list_stash_tabs(mem: GameMemory, vx=None) -> Optional[List[str]]:
    """Вернуть имена всех вкладок стэша (или None если стэш закрыт). Без переключения."""
    br = _make_browser(mem, vx)
    return br.list_tab_names()


def read_open_tab_items(mem: GameMemory, vx=None) -> Optional[List[StashItem]]:
    """Все предметы текущей открытой вкладки (без переключения). Без vx — только чтение."""
    br = _make_browser(mem, vx)
    return br.read_open_tab_items()


def open_maps_tab(mem: GameMemory, vx) -> Tuple[bool, str, Optional[List[StashItem]]]:
    """Открыть вкладку MAPS (через R1/L1) и вернуть ВСЕ её предметы (НЕ только карты).

    Returns (ok, msg, items). `items` — полный StashItem list открытой вкладки.
    Фильтр по картам делается вызывающей стороной (path.startswith('Metadata/Items/Maps')).
    """
    br = _make_browser(mem, vx)
    idx = br.find_maps_tab()
    if idx is None:
        return False, "вкладка с картами не найдена", None
    if br._cache_stc and br._cache_stc[3] == idx:
        items = br.read_open_tab_items()
        return (bool(items is not None),
                "MAPS уже открыта, items=%d" % (len(items) if items else 0), items)
    items = br.open_tab_and_read(idx)
    if items is None:
        return False, "не удалось открыть вкладку %d" % idx, None
    return True, "MAPS (idx=%d) открыта, items=%d" % (idx, len(items)), items


def filter_maps(items: List[StashItem]) -> List[StashItem]:
    """Отфильтровать карт-(Metadata/Items/Maps) из полного списка предметов."""
    return [it for it in items if it.path.startswith("Metadata/Items/Maps")]


def read_all_maps(mem: GameMemory) -> List[StashItem]:
    """Все карты из всех доступных в памяти подвкладок MAPS (12×8 контейнеры).

    Не требует scroll_to_tab — работает на то, что игрок недавно открыл внутри MAPS.
    Подвкладки (Tier1, Tier2, ..., Tier16) подгружаются lazy; видны только недавние.
    """
    from stash_reader import StashReader
    rd = StashReader(mem)
    return rd.read_all_maps()


def summarize_items(items: List[StashItem]) -> str:
    """Краткая сводка предметов для лога: категории + counts + уникальные path."""
    if not items:
        return "пусто"
    cats: dict = {}
    paths: dict = {}
    for it in items:
        cat = it.path.split("/")[2] if it.path.startswith("Metadata/Items/") else "(other)"
        cats[cat] = cats.get(cat, 0) + 1
        paths[it.path] = paths.get(it.path, 0) + 1
    parts = []
    for c, n in sorted(cats.items(), key=lambda kv: -kv[1]):
        parts.append("%s=%d" % (c, n))
    cat_str = " ".join(parts)
    # первые 8 уникальных paths
    path_str = ", ".join("%s×%d" % (p, n) for p, n in list(paths.items())[:8])
    return "items=%d  {%s}  paths: %s" % (len(items), cat_str, path_str)


def open_tab_by_name(mem: GameMemory, vx, name_pattern: str
                     ) -> Tuple[bool, str, Optional[List[StashItem]]]:
    """Открыть вкладку по имени (case-insensitive substring) и вернуть её предметы."""
    br = _make_browser(mem, vx)
    idx = br.find_tab_by_name(name_pattern)
    if idx is None:
        return False, "вкладка %r не найдена" % name_pattern, None
    items = br.open_tab_and_read(idx)
    if items is None:
        return False, "не удалось открыть вкладку %d (%r)" % (idx, name_pattern), None
    return True, "ok: вкладка idx=%d открыта, items=%d" % (idx, len(items)), items


def pick_map_for_device(mem: GameMemory, vx, map_item: StashItem) -> Tuple[bool, str]:
    """Взять указанную карту из открытой вкладки стэша (для переноса в Map Device).

    TODO: реализация в следующей итерации — навигация стиком в tile (map_item.grid_x/y),
    tap A (Cross) для drag-start, наведение в inventory или в Map Device, tap A для drop.
    Сейчас — заглушка: возвращает координаты карты для последующей автоматизации.
    """
    return (False, "TODO: drag map id=%d from (%d,%d) %dx%d %s"
            % (map_item.id, map_item.grid_x, map_item.grid_y,
               map_item.width, map_item.height, map_item.path))