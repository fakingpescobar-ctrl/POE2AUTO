"""Чтение стэша (Stash) из памяти PoE2.

Стэш НЕ хранится на сущности Stash и НЕ в ServerData; он живёт в UI-дереве
как Backpack-контейнер, на который ссылаются ItemView UI-элементы.

Метод (валидирован 02.08.2026, билд после патча 2026-07-16):

1. ItemView UI-элемент: heap-объект X, у которого
     U64(X)        в CODE-диапазоне модуля (vtable) — устойчиво к drift, не хардкод vtable
     U64(X+0x08) == X                     (ElementSelfOff — self-ref UI-элемента)
     U64(X+0x758) в heap                  (container backlink → Backpack)
     U32(X+0x760) — малый item-id
2. Группировка ItemView по container = U64(X+0x758).
   Открытая вкладка стэша = контейнер с максимумом ItemViews и grid 12×12 или 24×24
     (U32(c+0x150)=w, U32(c+0x154)=h).
   Бэкпак игрока = 12×8 (НЕ 12×5 на нашем билде).
3. std::map предметов: sentinel = U64(container+0x188).
   Sentinel: byte(sent+0x19)==1; root = U64(sent+8); node real если byte(node+0x19)==0.
   In-order обход: left@0, right@0x10, parent@8.
   node: id = U32(node+0x20); valuePtr = U64(node+0x28).
4. InventoryItemStruct @ valuePtr:
     item      = U64(vp+0x00)
     gridX     = U32(vp+0x08),  gridY     = U32(vp+0x0C)
     gridXEnd  = U32(vp+0x10),  gridYEnd  = U32(vp+0x14)
     w = xEnd-x,  h = yEnd-y
5. metadata предмета: item+0x08 → EntityDetails; details+0x08 →
   СЫРОЙ UTF-16 NUL-terminated буфер (НЕ std::wstring header, НЕ NativeUtf16Text).
   Чтение: mem.read_string_utf16(sptr, 512).
   Карты: path начинается с 'Metadata/Items/Maps'.

ВАЖНО: в памяти находится только ОТКРЫТАЯ/недавно открытая вкладка стэша.
Чтобы перечислить ВСЕ вкладки и найти карты — бот должен кликать по табам
и после каждого клика читать открытый Backpack.

Источники реверса: github.com/imkk000/poe2-offsets (stash.go, backpack.go, element.go).
"""
import logging
from typing import Optional, Dict, List, Tuple, NamedTuple

import struct as _struct

from game_memory import GameMemory

_log = logging.getLogger("poe2auto.stash")

HEAP_LO = 0x10000000000
HEAP_HI = 0x7F0000000000

ITEMVIEW_SELF          = 0x08
ITEMVIEW_CONTAINER     = 0x758
ITEMVIEW_ITEM_ID       = 0x760

CONTAINER_GRID_W       = 0x150
CONTAINER_GRID_H       = 0x154
CONTAINER_MAP_SENTINEL = 0x188

NODE_LEFT     = 0x00
NODE_PARENT   = 0x08
NODE_RIGHT    = 0x10
NODE_IS_NIL   = 0x19
NODE_KEY_ID   = 0x20
NODE_VALUE    = 0x28

INV_ITEM       = 0x00
INV_GRID_X     = 0x08
INV_GRID_Y     = 0x0C
INV_GRID_X_END = 0x10
INV_GRID_Y_END = 0x14

ITEM_DETAILS   = 0x08
DETAILS_NAME   = 0x08

STASH_GRID_SHAPES = frozenset({(12, 12), (24, 24)})
MAP_PATH_PREFIX = "Metadata/Items/Maps"
# Подвкладки MAPS (folder tabs) — grid 12×8, по 49 ячеек в каждой. Хранят Waystone
# карт типа Metadata/Items/Maps/MapKeyTier1..N. Видны только недавние открытые.
MAP_SUBTAB_GRID = (12, 8)
# Backpack игрока — grid 12×5 (5 rows × 12 cols = 60 ячеек) в PoE2 build 2026-08.
# Не путать с MAPS subtabs (12×8). Проверено 03.08.2026 после drop Tier16 в inv.
PLAYER_INV_GRID = (12, 5)


class StashItem(NamedTuple):
    id: int
    grid_x: int
    grid_y: int
    width: int
    height: int
    path: str
    item_entity: int
    container: int


class StashTab(NamedTuple):
    container: int
    grid_w: int
    grid_h: int
    item_count: int
    items: List[StashItem]


class StashReader:
    """Читает открытую вкладку стэша PoE2 из памяти.

    essает открытую Backpack-вкладку через ItemView-скан UI-дерева;
    если стэш закрыт — возвращает None.
    """

    def __init__(self, mem: GameMemory):
        self._mem = mem
        self._module_lo = 0
        self._module_hi = 0
        self._bind_module()
        # Кеш find_itemviews: полный скан heap (~30с) делаем ОДИН раз за сессию.
        # Контейнеры стабильны пока игра работает; содержимое читаем заново
        # через read_tab_items при каждом вызове. При открытии/закрытии сташа
        # кеш НЕ сбрасывается (itemviews подсветки всё равно живут в heap),
        # но если адреса протухли (перезапуск игры) — читаем сырые данные
        # и кеш автоматически обновляется (контейнеры те же, но с новыми адресами).
        self._views_cache: Optional[List[Tuple[int, int, int]]] = None
        self._views_cache_base = 0

    def _bind_module(self) -> None:
        self._module_lo = self._mem._base
        self._module_hi = self._mem._base + self._mem._size

    def _u64(self, a: int) -> int:
        d = self._mem.read_bytes(a, 8)
        return _struct.unpack("<Q", d)[0] if d else 0

    def _u32(self, a: int) -> int:
        d = self._mem.read_bytes(a, 4)
        return _struct.unpack("<I", d)[0] if d else 0

    def _byte(self, a: int) -> int:
        d = self._mem.read_bytes(a, 1)
        return d[0] if d else 0xFF

    def _is_heap(self, p: int) -> bool:
        return HEAP_LO <= p < HEAP_HI

    def _is_code(self, p: int) -> bool:
        return self._module_lo <= p < self._module_hi

    def _read_path(self, sp: int) -> str:
        if not self._is_heap(sp):
            return ""
        return self._mem.read_string_utf16(sp, 512) or ""

    def _item_path(self, item: int) -> str:
        if not self._is_heap(item):
            return ""
        details = self._u64(item + ITEM_DETAILS)
        if not self._is_heap(details):
            return ""
        sp = self._u64(details + DETAILS_NAME)
        return self._read_path(sp)

    def find_itemviews(self) -> List[Tuple[int, int, int]]:
        """Сканирует heap, возвращает [(itemview_addr, container, item_id), ...].

        Результат кешируется (полный скан ~30с делаем один раз). Кеш валиден,
        пока процесс тот же (base совпадает). Сброс — при перезапуске игры.
        """
        if self._views_cache is not None and self._views_cache_base == self._mem._base:
            return self._views_cache
        import ctypes
        from ctypes import wintypes

        class MBI(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD),
            ]

        try:
            import numpy as _np
        except ImportError:
            _np = None

        handle = self._mem._handle
        out: List[Tuple[int, int, int]] = []
        addr = 0
        while addr < 0x7FFFFFFFFFFF:
            mbi = MBI()
            res = ctypes.windll.kernel32.VirtualQueryEx(
                handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
            )
            if res == 0:
                addr = addr + 0x10000 if addr else 0x10000
                continue
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if size and mbi.State == 0x1000 and mbi.Protect in (0x04, 0x40, 0x02, 0x42):
                chunk = 16 * 1024 * 1024
                pos = 0
                while pos < size:
                    n = min(chunk, size - pos)
                    rd = self._mem.read_bytes(base + pos, n)
                    if rd is None:
                        break
                    if _np is not None:
                        arr = _np.frombuffer(rd, dtype=_np.uint64)
                        offs = _np.arange(len(arr), dtype=_np.uint64) * 8
                        addrs = _np.uint64(base + pos) + offs
                        mask = (arr == addrs - 8) & (arr >= HEAP_LO) & (arr < HEAP_HI)
                        cands = _np.nonzero(mask)[0]
                        for k in cands:
                            X = int(arr[k])
                            if not self._is_code(self._u64(X)):
                                continue
                            c = self._u64(X + ITEMVIEW_CONTAINER)
                            if not self._is_heap(c):
                                continue
                            iid = self._u32(X + ITEMVIEW_ITEM_ID)
                            if iid == 0 or iid >= 0x10000000:
                                continue
                            out.append((X, c, iid))
                    else:
                        for i in range(0, len(rd) - 8, 8):
                            A = base + pos + i
                            v = _struct.unpack("<Q", rd[i:i + 8])[0]
                            if v != A - 8 or not (HEAP_LO <= v < HEAP_HI):
                                continue
                            X = v
                            if not self._is_code(self._u64(X)):
                                continue
                            c = self._u64(X + ITEMVIEW_CONTAINER)
                            if not self._is_heap(c):
                                continue
                            iid = self._u32(X + ITEMVIEW_ITEM_ID)
                            if iid == 0 or iid >= 0x10000000:
                                continue
                            out.append((X, c, iid))
                    pos += chunk
            addr = base + size
        self._views_cache = out
        self._views_cache_base = self._mem._base
        return out

    def find_stash_containers(self) -> List[Tuple[int, int, int, int]]:
        """Возвращает [(container, view_count, grid_w, grid_h), ...] только для стэш-вкладок (12×12 / 24×24)."""
        views = self.find_itemviews()
        counts: Dict[int, int] = {}
        for _iv, c, _iid in views:
            counts[c] = counts.get(c, 0) + 1
        result: List[Tuple[int, int, int, int]] = []
        for c, n in counts.items():
            w = self._u32(c + CONTAINER_GRID_W)
            h = self._u32(c + CONTAINER_GRID_H)
            if (w, h) in STASH_GRID_SHAPES:
                result.append((c, n, w, h))
        result.sort(key=lambda t: t[1], reverse=True)
        return result

    def read_tab_items(self, container: int, want_paths: bool = True) -> List[StashItem]:
        """Проходит std::map контейнера, возвращает список предметов."""
        sentinel = self._u64(container + CONTAINER_MAP_SENTINEL)
        if not self._is_heap(sentinel):
            return []
        items: List[StashItem] = []

        def is_real(n: int) -> bool:
            return self._is_heap(n) and n != sentinel and self._byte(n + NODE_IS_NIL) == 0

        def leftmost(n: int) -> int:
            seen = set()
            while True:
                left = self._u64(n + NODE_LEFT)
                if not is_real(left) or left in seen:
                    return n
                seen.add(left)
                n = left

        root = self._u64(sentinel + 0x08)
        if not self._is_heap(root):
            return []
        node = leftmost(root)
        seen = set()
        limit = 20000
        while is_real(node) and len(seen) < limit:
            if node in seen:
                break
            seen.add(node)
            item_id = self._u32(node + NODE_KEY_ID)
            vp = self._u64(node + NODE_VALUE)
            if self._is_heap(vp):
                item = self._u64(vp + INV_ITEM)
                x = self._u32(vp + INV_GRID_X)
                y = self._u32(vp + INV_GRID_Y)
                x_end = self._u32(vp + INV_GRID_X_END)
                y_end = self._u32(vp + INV_GRID_Y_END)
                w = x_end - x
                h = y_end - y
                path = self._item_path(item) if (want_paths and self._is_heap(item)) else ""
                items.append(StashItem(
                    id=item_id, grid_x=x, grid_y=y, width=w, height=h,
                    path=path, item_entity=item, container=container,
                ))
            right = self._u64(node + NODE_RIGHT)
            if is_real(right):
                node = leftmost(right)
                continue
            while True:
                parent = self._u64(node + NODE_PARENT)
                if not self._is_heap(parent) or parent == sentinel:
                    return items
                if self._u64(parent + NODE_RIGHT) == node:
                    node = parent
                    continue
                node = parent
                break
        return items

    def find_visible_backpack(self, stc: int) -> Optional[Tuple[int, int, int, int]]:
        """Найти Backpack текущей открытой вкладки через STC.visibleIdx → inv_panel.

        Visibility-discriminator (валидирован эмпирически 02.08.2026):
            byte(ip + 0x181) — у ОТКРЫТОЙ вкладки = 0x2E, у lingering = 0x06.
        inv_panel (entry+0x80) для каждой вкладки self-ref valid; отличает только
        этот байт. Lining-контейнер от прошлой вкладки всё ещё живёт в heap и
        содержит те же 388 Jewels (или предметы той прошлой вкладки), НО его inv_panel
        имеет byte(+0x181)==0x06, тогда как у открытой ==0x2E.

        Returns (container, view_count_in_panel, grid_w, grid_h) or None.
        """
        begin = self._u64(stc + 0x358)
        end = self._u64(stc + 0x360)
        if not self._is_heap(begin) or end < begin:
            return None
        cnt = (end - begin) // 0x90
        vi = self._u32(stc + 0x370)
        if not (0 <= vi < cnt):
            return None
        entry = begin + vi * 0x90
        inv_panel = self._u64(entry + 0x80)
        if not self._is_heap(inv_panel):
            return None
        # валидация: inv_panel ОТКРЫТОЙ вкладки имеет byte(+0x181) == 0x2E
        vis_byte = self._byte(inv_panel + 0x181)
        if vis_byte != 0x2E:
            _log.warning("find_visible_backpack: visibleIdx=%d но byte(ip+0x181)=0x%X (ожидал 0x2E) — lingering?",
                         vi, vis_byte)
            return None
        from collections import deque
        seen = set()
        container = 0
        container_views = 0
        q = deque([(inv_panel, 0)])
        depth_max = 6
        nodes_max = 4000
        visited = 0
        while q and visited < nodes_max:
            node, depth = q.popleft()
            if node in seen or not self._is_heap(node):
                continue
            seen.add(node)
            visited += 1
            if self._u64(node + 0x08) == node:
                c = self._u64(node + 0x758)
                iid = self._u32(node + 0x760)
                if self._is_heap(c) and iid != 0 and iid < 0x10000000:
                    if self._is_code(self._u64(node + 0x00)):
                        if container == 0:
                            container = c
                        if c == container:
                            container_views += 1
            if depth < depth_max:
                cb = self._u64(node + 0x10)
                ce = self._u64(node + 0x18)
                if self._is_heap(cb) and ce > cb and (ce - cb) <= 0x4000:
                    n = (ce - cb) // 8
                    for i in range(n):
                        c = self._u64(cb + i * 8)
                        if c and c not in seen:
                            q.append((c, depth + 1))
        if not container:
            return None
        w = self._u32(container + 0x150)
        h = self._u32(container + 0x154)
        return (container, container_views, w, h)

    def read_open_tab(self) -> Optional[StashTab]:
        """Открытая (видимая) вкладка стэша или None, если стэш закрыт.

        Использует STC-адрес если он уже известен (через кеш или задан), иначе
        fallback на find_stash_containers (max ItemView views).
        """
        # приоритет: найти контейнер ТОЛЬКО через открытую inv_panel (visibleIdx)
        # — самы надёжный, не задевает lingering контейнеры старых вкладок.
        stc = getattr(self, "_stc_addr", 0)
        if not stc:
            # пробуем через stash_tab_browser — он хранит STC в кеш-файле
            try:
                from stash_tab_browser import _load_stc_cache
                cached = _load_stc_cache()
                if cached is not None:
                    stc, _begin, _cnt, _vi = cached
            except Exception:
                stc = 0
        if stc:
            res = self.find_visible_backpack(stc)
            if res is not None:
                container, _n, w, h = res
                items = self.read_tab_items(container)
                return StashTab(container=container, grid_w=w, grid_h=h,
                                item_count=len(items), items=items)
        # fallback: max views (старый метод — может дать lingering-контейнер)
        tabs = self.find_stash_containers()
        if not tabs:
            return None
        container, _n, w, h = tabs[0]
        items = self.read_tab_items(container)
        return StashTab(container=container, grid_w=w, grid_h=h,
                        item_count=len(items), items=items)

    def find_maps_in_loaded_tabs(self) -> List[Tuple[StashTab, List[StashItem]]]:
        """Ищет карты во всех загруженных вкладках стэша. Возвращает [(tab, [maps...]), ...]."""
        tabs = self.find_stash_containers()
        out: List[Tuple[StashTab, List[StashItem]]] = []
        for container, _n, w, h in tabs:
            items = self.read_tab_items(container)
            maps = [it for it in items if it.path.startswith(MAP_PATH_PREFIX)]
            if maps:
                tab = StashTab(container=container, grid_w=w, grid_h=h,
                               item_count=len(items), items=maps)
                out.append((tab, maps))
        return out

    def find_map_subtabs(self) -> List[Tuple[int, int, int, List[StashItem]]]:
        """Найти подвкладки MAPS (folder tab) — grid 12×8 контейнеры с Metadata/Items/Maps.

        Возвращает [(container, grid_w, grid_h, maps), ...]. Читает только контейнеры
        12×8 (подвкладки MAPS) и фильтрует по типу предмета. Быстрее `find_stash_containers`
        потому что не сортирует по view_count.

        Прим.: 12×8 это же размер player-inventory (backpack) — но там не лежат карты.
        Дискриминатор — path предметов: `Metadata/Items/Maps/MapKeyTierN`.
        """
        views = self.find_itemviews()
        counts: Dict[int, int] = {}
        for _iv, c, _iid in views:
            counts[c] = counts.get(c, 0) + 1
        out: List[Tuple[int, int, int, List[StashItem]]] = []
        for c, n in counts.items():
            w = self._u32(c + CONTAINER_GRID_W)
            h = self._u32(c + CONTAINER_GRID_H)
            if (w, h) != MAP_SUBTAB_GRID:
                continue
            if n < 1:
                continue
            items = self.read_tab_items(c)
            maps = [it for it in items if it.path.startswith(MAP_PATH_PREFIX)]
            if maps:
                out.append((c, w, h, maps))
        return out

    def read_all_maps(self) -> List[StashItem]:
        """Все карты из всех доступных в памяти подвкладок MAPS (12×8)."""
        out: List[StashItem] = []
        for _c, _w, _h, maps in self.find_map_subtabs():
            out.extend(maps)
        return out

    def read_player_inventory(self) -> Optional[StashTab]:
        """Найти Backpack игрока (12×5 в PoE2) в UI-дереве и вернуть его предметы.

        Отличается от MAPS-подвкладок (12×8):
          - grid 12×5 = Player Backpack (по умолчанию PoE2 build 2026-08)
          - grid 12×8 = MAPS subtab (Tier1/2/.../16 waystones)

        Возвращает самый крупный 12×5 контейнер в куче, items которого содержат
        mixed path (Gem, Currency, Armor, ... — типичный player backpack).
        Дискриминатор от 12×5 winged-overlay containers (currency-wheel и т.д.):
          - Player inv — ItemView count ≈ cell count (12×5=60 cells)
          - Winged-overlay — мало ItemViews, weird grid-shapes
        """
        views = self.find_itemviews()
        if not views:
            return None
        counts: Dict[int, int] = {}
        for _iv, c, _iid in views:
            counts[c] = counts.get(c, 0) + 1
        candidates: List[Tuple[int, int]] = []
        for c, n in counts.items():
            w = self._u32(c + CONTAINER_GRID_W)
            h = self._u32(c + CONTAINER_GRID_H)
            if (w, h) == PLAYER_INV_GRID:
                candidates.append((c, n))
        if not candidates:
            return None
        # предпочтение — контейнер с наибольшим count ItemViews (близко к max cells)
        candidates.sort(key=lambda t: t[1], reverse=True)
        c, _n = candidates[0]
        items = self.read_tab_items(c)
        return StashTab(
            container=c, grid_w=PLAYER_INV_GRID[0], grid_h=PLAYER_INV_GRID[1],
            item_count=len(items), items=items,
        )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mem = GameMemory()
    if not mem.connect():
        print("connect failed")
        return 1
    try:
        reader = StashReader(mem)
        tab = reader.read_open_tab()
        if tab is None:
            print("stash not open (no stash container found)")
            loaded = reader.find_stash_containers()
            print("stash containers in memory:", len(loaded))
            return 0
        print("open stash tab: container=0x%X grid=%dx%d items=%d"
              % (tab.container, tab.grid_w, tab.grid_h, tab.item_count))
        cats: Dict[str, int] = {}
        for it in tab.items:
            cat = it.path.split("/")[2] if it.path.startswith("Metadata/Items/") else ""
            cats[cat] = cats.get(cat, 0) + 1
        print("categories:", dict(sorted(cats.items(), key=lambda kv: -kv[1])))
        maps = [it for it in tab.items if it.path.startswith(MAP_PATH_PREFIX)]
        print("maps in open tab:", len(maps))
        for m in maps[:20]:
            print("  MAP id=%d (%d,%d) %dx%d %s" % (m.id, m.grid_x, m.grid_y, m.width, m.height, m.path))
        loaded = reader.find_maps_in_loaded_tabs()
        print("loaded tabs with maps:", len(loaded))
        for t, ms in loaded:
            print("  tab container=0x%X grid=%dx%d maps=%d" % (t.container, t.grid_w, t.grid_h, len(ms)))
            for m in ms[:5]:
                print("    MAP id=%d (%d,%d) %s" % (m.id, m.grid_x, m.grid_y, m.path))
    finally:
        mem.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())