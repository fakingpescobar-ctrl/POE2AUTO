"""Чтение памяти PoE2 через PROCESS_VM_READ (read-only).

Открывает процесс игры через OpenProcess с флагом PROCESS_VM_READ,
читает память через NtReadVirtualMemory (быстрее ReadProcessMemory).
Ничего не пишет в игру — только читает.

Адаптировано из POE2GPS (Sikaka/POE2Radar, MIT).
"""
import ctypes
import ctypes.wintypes as wintypes
import struct
import logging
import os
from typing import Optional

_log = logging.getLogger("poe2auto.memory")

# Windows constants
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT = 0x1000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
MEM_PRIVATE = 0x20000
PAGE_EXECUTE_FLAGS = 0xF0  # any execute bit
# NTSTATUS: чтение пересекло границу страниц с разными правами —
# вернулись частичные данные (n_read > 0), а не ошибка.
STATUS_PARTIAL_COPY = 0x8000000D

# Process names to search
PROC_NAMES = [
    "PathOfExile.exe",
    "PathOfExileSteam.exe",
    "PathOfExile_x64.exe",
]

# Ntdll types
NTSTATUS = ctypes.c_long


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class MODULEINFO(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", ctypes.c_void_p),
        ("SizeOfImage", wintypes.DWORD),
        ("EntryPoint", ctypes.c_void_p),
    ]


# Load DLLs
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

# Function signatures
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL

ntdll.NtReadVirtualMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
ntdll.NtReadVirtualMemory.restype = NTSTATUS

kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t

psapi = ctypes.WinDLL("psapi", use_last_error=True)

psapi.EnumProcessModulesEx.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
]
psapi.EnumProcessModulesEx.restype = wintypes.BOOL

psapi.GetModuleInformation.argtypes = [
    wintypes.HANDLE, wintypes.HMODULE,
    ctypes.POINTER(MODULEINFO), wintypes.DWORD,
]
psapi.GetModuleInformation.restype = wintypes.BOOL

# GetExitCodeProcess — проверка живости процесса (STILL_ACTIVE = 259)
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL


class GameMemory:
    """Read-only reader for PoE2 process memory."""

    def __init__(self):
        self._handle = None
        self._base = 0
        self._size = 0
        self._pid = 0

    @property
    def connected(self) -> bool:
        return self._handle is not None

    def connect(self) -> bool:
        """Find PoE2 process and open with read-only access."""
        import ctypes.wintypes
        import subprocess

        # Find PID
        pid = self._find_pid()
        if pid is None:
            return False

        # Open process
        handle = kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION,
            False, pid,
        )
        if not handle:
            _log.warning("OpenProcess failed (pid=%d): %s", pid, ctypes.get_last_error())
            return False

        self._handle = handle
        self._pid = pid

        # Get main module base
        if not self._resolve_module():
            self.disconnect()
            return False

        _log.info("Connected to PoE2 pid=%d base=0x%X size=0x%X", pid, self._base, self._size)
        return True

    def disconnect(self):
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None
            self._base = 0
            self._size = 0
            self._pid = 0

    def check_alive(self) -> bool:
        """True если подключённый процесс ещё жив.

        OpenProcess хендл остаётся валидным после смерти процесса — все
        read'ы просто начинают фейлиться. По одному фейлу не понять, жив ли
        процесс, поэтому проверяем GetExitCodeProcess (STILL_ACTIVE = 259).
        """
        if not self._handle:
            return False
        code = wintypes.DWORD(0)
        ok = kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code))
        if not ok:
            return False
        return code.value == 259  # STILL_ACTIVE

    def read_bytes(self, address: int, size: int) -> Optional[bytes]:
        """Read raw bytes from game memory."""
        if not self._handle:
            return None
        buf = (ctypes.c_char * size)()
        n_read = ctypes.c_size_t(0)
        status = ntdll.NtReadVirtualMemory(
            self._handle, ctypes.c_void_p(address),
            buf, size, ctypes.byref(n_read),
        )
        # STATUS_PARTIAL_COPY (0x8000000D): NTSTATUS, возвращается напрямую
        # NtReadVirtualMemory (не Win32 GetLastError!) когда буфер пересекает
        # страницу с другими правами доступа — возвращаем частично прочитанные
        # байты, а не теряем их целиком. ВАЖНО: это НЕ Win32 ERROR_PARTIAL_COPY
        # (170) — тот относится к kernel32.ReadProcessMemory, который здесь не
        # используется (путь — чистый ntdll API, restype=NTSTATUS).
        if status == STATUS_PARTIAL_COPY:
            if n_read.value > 0:
                return bytes(buf[:n_read.value])
            return None
        if status != 0:
            return None
        return bytes(buf[:n_read.value])

    def read_int(self, address: int, size: int = 4) -> Optional[int]:
        """Read integer (4 or 8 bytes)."""
        data = self.read_bytes(address, size)
        if data is None:
            return None
        if size == 4:
            return struct.unpack("<i", data)[0]
        elif size == 8:
            return struct.unpack("<q", data)[0]
        return int.from_bytes(data, "little", signed=True)

    def read_uint(self, address: int, size: int = 4) -> Optional[int]:
        data = self.read_bytes(address, size)
        if data is None:
            return None
        if size == 4:
            return struct.unpack("<I", data)[0]
        elif size == 8:
            return struct.unpack("<Q", data)[0]
        return int.from_bytes(data, "little", signed=False)

    def read_float(self, address: int) -> Optional[float]:
        data = self.read_bytes(address, 4)
        if data is None:
            return None
        return struct.unpack("<f", data)[0]

    def read_pointer(self, address: int) -> Optional[int]:
        """Read 64-bit pointer."""
        return self.read_uint(address, 8)

    def read_vec3(self, address: int) -> Optional[tuple]:
        """Read Vector3 (3 floats: x, y, z)."""
        data = self.read_bytes(address, 12)
        if data is None:
            return None
        return struct.unpack("<fff", data)

    def read_string_utf8(self, address: int, max_len: int = 256) -> Optional[str]:
        """Read null-terminated UTF-8 string."""
        raw = self.read_bytes(address, max_len)
        if raw is None:
            return None
        try:
            s = raw.decode("utf-8")
            idx = s.find("\x00")
            return s[:idx] if idx >= 0 else s
        except Exception:
            return None

    def read_string_utf16(self, address: int, max_len: int = 256) -> Optional[str]:
        """Read null-terminated UTF-16 string.

        PoE2 пути часто имеют leaked мусор после NUL-terminator (например,
        trailing `*\x01\x00\x00...` от соседних heap-allocations), что вызывает
        UnicodeDecodeError при strict decode. Решение: найти двойной-NUL runtime
        boundary и декодировать только валидный префикс.
        """
        raw = self.read_bytes(address, max_len * 2)
        if raw is None:
            return None
        # находим NUL-terminator (два подряд 0x00 байта на чётной позиции)
        nul = raw.find(b"\x00\x00")
        if nul > 0 and nul % 2 == 0:
            raw = raw[:nul]
        elif nul == 0:
            return ""
        try:
            return raw.decode("utf-16-le")
        except Exception:
            try:
                # fallback с errors='replace' — часть пути валидна
                s = raw.decode("utf-16-le", errors="replace")
                idx = s.find("\x00")
                return s[:idx] if idx >= 0 else s
            except Exception:
                return None

    def read_std_wstring(self, address: int) -> Optional[str]:
        """Read std::wstring. MSVC x64 layout:
        +0x00: pointer to heap data (for long strings) or inline buffer (SSO)
        +0x08: length (wchar_t count)
        If the value at +0x00 looks like a valid heap pointer (> 0x10000), dereference it.
        Otherwise try reading inline (SSO).
        """
        if not address:
            return None
        ptr = self.read_pointer(address)
        if not ptr:
            return None
        # If it looks like a heap pointer, dereference
        if ptr > 0x10000 and ptr < 0x7FFFFFFFFFFF:
            return self.read_string_utf16(ptr, max_len=256)
        # SSO: data is inline starting at address + 8
        return self.read_string_utf16(address + 8, max_len=256)

    def read_std_vector(self, address: int, elem_size: int = 8):
        """Read StdVector: {First, Last, End}. Returns (first_ptr, count)."""
        first = self.read_pointer(address)
        last = self.read_pointer(address + 8)
        if not first or not last:
            return None, 0
        count = (last - first) // elem_size
        return first, count

    def aob_scan(self, pattern: bytes, wildcards: Optional[set] = None,
                 start: int = None, size: int = None) -> list:
        """Scan for byte pattern in game memory. Returns list of addresses.

        Fast path: сканирование через C-реализацию bytes.find — ищется фиксированный
        префикс паттерна (до первого wildcard), затем проверяется остаток паттерна
        только на найденных позициях (раньше: двойной Python-цикл по каждому байту
        модуля — ~8-9 сек на 100МБ, превращало find в «зависание» без вывода).
        """
        if start is None:
            start = self._base
        if size is None:
            size = self._size
        if wildcards is None:
            wildcards = set()

        results = []
        step = 0x1000  # read in 4KB chunks
        end = start + size
        overlap = len(pattern) - 1

        # фиксированный префикс (до первого wildcard) — минимум 1 байт
        head_len = 0
        for j, byte in enumerate(pattern):
            if j in wildcards:
                break
            head_len += 1
        if head_len == 0:
            head_len = 1  # паттерн начинается с wildcard — берём один байт
        head = pattern[:head_len]

        for chunk_start in range(start, end, step):
            scan_start = max(start, chunk_start - overlap)
            chunk_end = min(chunk_start + step, end)
            chunk = self.read_bytes(scan_start, chunk_end - scan_start)
            if chunk is None:
                continue

            pos = chunk.find(head)
            while pos != -1:
                # позиция должна давать полный паттерн в чанке
                if pos + len(pattern) <= len(chunk):
                    match = True
                    for j in range(head_len, len(pattern)):
                        if j in wildcards:
                            continue
                        if chunk[pos + j] != pattern[j]:
                            match = False
                            break
                    if match:
                        results.append(scan_start + pos)
                pos = chunk.find(head, pos + 1)

        return results

    def enumerate_regions(self, start: int = None, size: int = None):
        """Enumerate committed executable memory regions."""
        if start is None:
            start = self._base
        if size is None:
            size = self._size

        regions = []
        addr = start
        end = start + size
        mbi = MEMORY_BASIC_INFORMATION()
        mbi_size = ctypes.sizeof(mbi)

        while addr < end:
            got = kernel32.VirtualQueryEx(
                self._handle, ctypes.c_void_p(addr),
                ctypes.byref(mbi), mbi_size,
            )
            if got == 0:
                break

            if (mbi.State == MEM_COMMIT and
                    mbi.Type == MEM_IMAGE and
                    mbi.Protect & PAGE_EXECUTE_FLAGS and
                    not (mbi.Protect & 0x100)):  # not PAGE_GUARD
                regions.append((mbi.BaseAddress, mbi.RegionSize))

            addr = mbi.BaseAddress + mbi.RegionSize

        return regions

    def read_module(self, address: int, size: int) -> Optional[bytes]:
        """Read bytes from a specific module region."""
        return self.read_bytes(address, size)

    # --- Internal helpers ---

    def _find_pid(self) -> Optional[int]:
        """Find PoE2 process ID."""
        import re
        # tasklist в русской локали пишет в cp866 — text=True ломается (0xFF в поле памяти).
        # Берём байты и декодируем с запасом; ASCII-часть (имя+pid) корректна при любой кодировке.
        csv_re = re.compile(r'"([^"]+)","(\d+)"')
        for name in (["PathOfExile.exe"] + ["PathOfExileSteam.exe", "PathOfExile_x64.exe"]):
            try:
                import subprocess
                raw = subprocess.check_output(
                    ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                continue
            for enc in ("utf-8", "cp866", "latin-1"):
                try:
                    out = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                out = raw.decode("latin-1", errors="replace")
            for line in out.split("\n"):
                m = csv_re.search(line)
                if m:
                    return int(m.group(2))
        return None

    def _resolve_module(self) -> bool:
        """Get main module base address and size."""
        cb_needed = wintypes.DWORD(0)
        # First call to get size
        psapi.EnumProcessModulesEx(
            self._handle, None, 0,
            ctypes.byref(cb_needed), 0x03,  # LIST_MODULES_ALL
        )
        if cb_needed.value == 0:
            _log.warning("EnumProcessModulesEx returned 0 modules")
            return False

        count = cb_needed.value // ctypes.sizeof(wintypes.HMODULE)
        modules = (wintypes.HMODULE * count)()
        if not psapi.EnumProcessModulesEx(
            self._handle, modules, ctypes.sizeof(modules),
            ctypes.byref(cb_needed), 0x03,
        ):
            _log.warning("EnumProcessModulesEx failed: %s", ctypes.get_last_error())
            return False

        # Find the PoE2 main module — look for PathOfExile executable
        best_base, best_size = 0, 0
        for i in range(count):
            info = MODULEINFO()
            mod = modules[i]
            if psapi.GetModuleInformation(
                self._handle, mod,
                ctypes.byref(info), ctypes.sizeof(info),
            ):
                # Get module filename
                buf = ctypes.create_unicode_buffer(260)
                try:
                    psapi.GetModuleFileNameExW(
                        self._handle, ctypes.c_void_p(mod), buf, 260
                    )
                    name = buf.value
                except Exception:
                    name = ""
                # Look for PoE2 executable
                if any(p in name.lower() for p in ["pathofexile", "poe2"]):
                    if name.lower().endswith(".exe"):
                        best_base = info.lpBaseOfDll
                        best_size = info.SizeOfImage
                        _log.info("Found PoE2 module: %s at 0x%X (size=0x%X)",
                                  os.path.basename(name), best_base, best_size)
                        break
                # Fallback: largest .exe
                if info.SizeOfImage > best_size and name.lower().endswith(".exe"):
                    best_base = info.lpBaseOfDll
                    best_size = info.SizeOfImage

        if best_base == 0:
            _log.warning("Could not find main module")
            return False

        self._base = best_base
        self._size = best_size
        return True

    def read_camera_matrix(self, in_game_state: int) -> Optional[list]:
        """Прочитать матрицу WorldToScreen (4x4 float, 16 значений).
        Camera находится по адресу InGameState + 0x368.
        Матрица по Camera + 0x1A0 (64 байта).
        """
        import game_offsets as off
        cam_ptr = self.read_pointer(in_game_state + off.InGameState.CAMERA)
        if not cam_ptr:
            return None
        data = self.read_bytes(cam_ptr + off.Camera.WORLD_TO_SCREEN_MATRIX, 64)
        if data is None:
            return None
        import struct
        return list(struct.unpack("<16f", data))

    def world_to_screen(self, world_x: float, world_y: float, world_z: float,
                        matrix: list, screen_w: int, screen_h: int) -> Optional[tuple]:
        """Проецировать мировые координаты в экранные через матрицу WorldToScreen.
        Возвращает (screen_x, screen_y) или None если за экраном.
        """
        if not matrix or len(matrix) < 16:
            return None

        # Row-major 4x4 matrix: screen = world * M
        # M[0..3] = row0, M[4..7] = row1, M[8..11] = row2, M[12..15] = row3
        x = world_x * matrix[0] + world_y * matrix[4] + world_z * matrix[8] + matrix[12]
        y = world_x * matrix[1] + world_y * matrix[5] + world_z * matrix[9] + matrix[13]
        w = world_x * matrix[3] + world_y * matrix[7] + world_z * matrix[11] + matrix[15]

        if w < 0.001:
            return None

        sx = (x / w) * 0.5 * screen_w + screen_w * 0.5
        sy = (1.0 - (y / w) * 0.5) * screen_h  # Y инвертирован

        if sx < 0 or sx > screen_w or sy < 0 or sy > screen_h:
            return None

        return (int(sx), int(sy))

    def read_terrain_walkable(self, area_instance: int) -> Optional[tuple]:
        """Прочитать walkable grid (nibble-packed, 2 cells/byte).
        Возвращает (width, height, walkable_bytes) или None.
        walkable_bytes — плоский массив 0/1 по одному на клетку.
        """
        import game_offsets as off
        terrain = area_instance + off.AreaInstance.TERRAIN_METADATA

        # StdVector: first/last pointers
        first = self.read_pointer(terrain + off.Terrain.GRID_WALKABLE_DATA)
        last = self.read_pointer(terrain + off.Terrain.GRID_WALKABLE_DATA + 8)
        if not first or not last:
            return None

        bytes_per_row = self.read_int(terrain + off.Terrain.BYTES_PER_ROW)
        if not bytes_per_row or bytes_per_row <= 0 or bytes_per_row > 65536:
            return None

        total_bytes = last - first
        if total_bytes <= 0 or total_bytes > 64 * 1024 * 1024:
            return None

        raw = self.read_bytes(first, total_bytes)
        if raw is None:
            return None

        rows = total_bytes // bytes_per_row
        width = bytes_per_row * 2  # 2 nibbles per byte

        # Unpack nibbles → flat 0/1 array
        walk = bytearray(width * rows)
        for y in range(rows):
            row_base = y * bytes_per_row
            for x in range(width):
                byte_val = raw[row_base + (x >> 1)]
                nibble = (byte_val & 0x0F) if (x & 1) == 0 else (byte_val >> 4)
                walk[y * width + x] = 1 if nibble != 0 else 0

        return (width, rows, bytes(walk))

    def is_walkable(self, terrain_data: tuple, grid_x: float, grid_y: float) -> bool:
        """Проверить, проходима ли клетка по данным terrain.
        grid_x, grid_y — в сетевых координатах (как у EntityData).
        """
        if not terrain_data:
            return False
        width, height, walk = terrain_data

        # Конвертируем сетевые координаты в индексы массива
        # Каждая клетка = 23×23 ячеек grid, но walkable data — это ячейки
        cell_x = int(grid_x)
        cell_y = int(grid_y)

        if cell_x < 0 or cell_x >= width or cell_y < 0 or cell_y >= height:
            return False

        return walk[cell_y * width + cell_x] == 1
