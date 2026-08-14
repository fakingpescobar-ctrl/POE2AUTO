using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace PoE2Reader;

// ──────────────────────────────────────
// Win32
// ──────────────────────────────────────
public static class Native
{
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, int dwProcessId);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool ReadProcessMemory(IntPtr hProcess, ulong lpBaseAddress, [Out] byte[] lpBuffer, int dwSize, out int lpNumberOfBytesRead);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr hObject);

    public const uint PROCESS_VM_READ = 0x0010;
    public const uint PROCESS_QUERY_INFORMATION = 0x0400;
}

// ──────────────────────────────────────
// Memory Reader
// ──────────────────────────────────────
public class MemoryReader(IntPtr _handle)
{
    public byte[] ReadBytes(ulong address, int size)
    {
        var buf = new byte[size];
        if (Native.ReadProcessMemory(_handle, address, buf, size, out int read))
        {
            if (read < size) Array.Resize(ref buf, read);
            return buf;
        }
        return [];
    }

    public ulong ReadU64(ulong address) => ReadBytes(address, 8) is { Length: >= 8 } b ? BitConverter.ToUInt64(b, 0) : 0;
    public uint ReadU32(ulong address) => ReadBytes(address, 4) is { Length: >= 4 } b ? BitConverter.ToUInt32(b, 0) : 0;
    public ushort ReadU16(ulong address) => ReadBytes(address, 2) is { Length: >= 2 } b ? BitConverter.ToUInt16(b, 0) : (ushort)0;
    public byte ReadByte(ulong address) { var b = ReadBytes(address, 1); return b.Length > 0 ? b[0] : (byte)0; }
    public float ReadFloat(ulong address) => ReadBytes(address, 4) is { Length: >= 4 } b ? BitConverter.ToSingle(b, 0) : 0;
    public int ReadI32(ulong address) => ReadBytes(address, 4) is { Length: >= 4 } b ? BitConverter.ToInt32(b, 0) : 0;
}

// ──────────────────────────────────────
// Pattern Scanner
// ──────────────────────────────────────
public class PatternScanner(MemoryReader _reader)
{
    public (ulong baseAddr, int size)? GetModule(int pid, string name)
    {
        foreach (ProcessModule m in Process.GetProcessById(pid).Modules)
            if (m.FileName.EndsWith(name, StringComparison.OrdinalIgnoreCase))
                return ((ulong)m.BaseAddress, m.ModuleMemorySize);
        return null;
    }

    public ulong FindGsoSlot(ulong moduleBase, int moduleSize)
    {
        byte[] pattern = [0x48, 0x39, 0x2D, 0x00, 0x00, 0x00, 0x00, 0x0F, 0x85, 0x16, 0x01, 0x00, 0x00];
        var data = _reader.ReadBytes(moduleBase, moduleSize);
        if (data.Length == 0) return 0;
        for (int i = 0; i <= data.Length - pattern.Length; i++)
        {
            if (data[i] != 0x48 || data[i + 1] != 0x39 || data[i + 2] != 0x2D) continue;
            if (data[i + 7] != 0x0F || data[i + 8] != 0x85 || data[i + 9] != 0x16 || data[i + 10] != 0x01 || data[i + 11] != 0x00 || data[i + 12] != 0x00) continue;
            int rel = BitConverter.ToInt32(data, i + 3);
            return moduleBase + (ulong)(i + 7) + (ulong)rel;
        }
        return 0;
    }
}

// ──────────────────────────────────────
// Offsets
// ──────────────────────────────────────
public static class Off
{
    // IGS / AreaInstance
    public const ulong IgsOff = 0x88;
    public const ulong AreaInstanceOff = 0x290;
    public const ulong CameraOff = 0x368;
    public const ulong CameraMatrixOff = 0x1A0;
    public const ulong TerrainWidthOff = 0x928;    // terrain cells per row (from poe2-offsets)
    public const ulong TerrainHeightOff = 0x92C;   // terrain total rows (from poe2-offsets)
    public const ulong Layer0Off = 0x990;           // terrain layer0 ptr (works for this PoE2 version)
    public const ulong Layer1Off = 0x988;
    public const ulong TerrainStrideOff = 0x9F0;   // stride in bytes per row (shifted +0x20 from poe2-offsets for this PoE2 version)
    public const ulong AreaHashOff = 0x11C;
    public const ulong AreaStrOff = 0xA0;
    public const ulong PlayerPosOff = 0xA90;       // протух (см. LocalPlayerOff) — только для /diag
    public const ulong LocalPlayerOff = 0x5C0;     // ✓ AreaInstance → local player Entity (patch 2026-07-16: 0x5A0→0x5C0)

    // Entity tree (red-black, sentinel-based)
    public const ulong AwakeSentinelOff = 0x6E0;   // AI+0x6E0 = sentinel
    public const ulong AwakeCountOff = 0x6E8;      // AI+0x6E8 = count

    // Tree node (0x30 bytes)
    public const ulong TreeNodeLeft = 0x00;
    public const ulong TreeNodeParent = 0x08;
    public const ulong TreeNodeRight = 0x10;
    public const ulong TreeNodeIsNil = 0x19;       // byte: 1=sentinel
    public const ulong TreeNodeIdHi = 0x20;        // uint32
    public const ulong TreeNodeEntity = 0x28;      // ptr to entity

    // Entity struct
    public const ulong EntityVtable = 0x00;
    public const ulong EntityDetails = 0x08;
    public const ulong EntityCompBegin = 0x10;     // NativePtrArray begin
    public const ulong EntityCompEnd = 0x18;       // NativePtrArray end

    // Details struct
    public const ulong DetailsLookup = 0x28;       // lookup table ptr

    // Lookup bucket
    public const ulong LookupBucketOff = 0x28;     // bucket begin
    public const ulong LookupBucketEndOff = 0x30;  // bucket end
    public const ulong BucketEntryStride = 0x10;   // 16 bytes per entry

    // Render component (position)
    public const ulong RenderPosX = 0x138;
    public const ulong RenderPosY = 0x13C;
    public const ulong RenderPosZ = 0x140;
}

// ──────────────────────────────────────
// Game Reader
// ──────────────────────────────────────
public class GameReader(MemoryReader _r)
{
    private ulong _gso;
    private ulong _modBase;

    public MemoryReader Reader => _r;
    public ulong Gso => _gso;
    public void SetGso(ulong v) => _gso = v;
    public void SetModuleBase(ulong b) => _modBase = b;

    public ulong Igs() { var g = _r.ReadU64(_gso); return g != 0 ? _r.ReadU64(g + Off.IgsOff) : 0; }
    public ulong Area() { var i = Igs(); return i != 0 ? _r.ReadU64(i + Off.AreaInstanceOff) : 0; }
    public ulong Cam() { var i = Igs(); return i != 0 ? _r.ReadU64(i + Off.CameraOff) : 0; }
    public ulong Player() { var a = Area(); return a != 0 ? _r.ReadU64(a + Off.LocalPlayerOff) : 0; }

    // game_state: 0 = НЕ в игре (главное меню / char-select / loading), 20 = в игре.
    // Признак «в игре»: значение глобального GSO slot ненулевое (существует GameStates объект).
    // Подтверждено live: в меню /diagGSO → gso_ptr0=0x0, в игре → валидный указатель.
    public int ReadGameState()
    {
        var g = _r.ReadU64(_gso);
        return g != 0 ? 20 : 0;
    }

    public (float x, float y) ReadPlayerPos()
    {
        var (wx, wy, _) = ReadPlayerPos3();
        return (wx, wy);
    }

    public (float x, float y, float z) ReadPlayerPos3()
    {
        // Позиция игрока читается из Render-компонента локального игрока
        // (тот же источник, что у мобов). PlayerPosOff (area+0xA90) протух
        // после патча — расходится на ~100+ grid.
        var p = Player();
        if (p == 0 || !IsHeap(p)) return (0, 0, 0);
        var comps = ResolveComponents(p);
        if (!comps.TryGetValue("Render", out var render) || render == 0) return (0, 0, 0);
        return (
            _r.ReadFloat(render + Off.RenderPosX),
            _r.ReadFloat(render + Off.RenderPosY),
            _r.ReadFloat(render + Off.RenderPosZ)
        );
    }

    public float[]? ReadMatrix()
    {
        var cam = Cam(); if (cam == 0) return null;
        var buf = _r.ReadBytes(cam + Off.CameraMatrixOff, 64);
        if (buf.Length < 64) return null;
        var m = new float[16];
        for (int i = 0; i < 16; i++) m[i] = BitConverter.ToSingle(buf, i * 4);
        return m;
    }

    public List<int> GetScreenPos(int gridX, int gridY)
    {
        const float GridToWorld = 250f / 23f;
        float worldX = gridX * GridToWorld;
        float worldY = gridY * GridToWorld;
        var matrix = ReadMatrix();
        if (matrix == null) return [0, 0];
        float w = matrix[3] * worldX + matrix[7] * worldY + matrix[15];
        if (Math.Abs(w) < 0.001f) return [0, 0];
        float screenX = (matrix[0] * worldX + matrix[4] * worldY + matrix[12]) / w;
        float screenY = (matrix[1] * worldX + matrix[5] * worldY + matrix[13]) / w;
        return [(int)screenX, (int)screenY];
    }

    public (string code, string name) ReadAreaStrings()
    {
        var area = Area(); if (area == 0) return ("", "");
        var strStruct = _r.ReadU64(area + Off.AreaStrOff);
        if (strStruct == 0) return ("", "");
        var ptr = _r.ReadU64(strStruct);
        if (ptr == 0) return ("", "");
        var buf = _r.ReadBytes(ptr, 256);
        var strs = SplitUtf16(buf);
        return (strs.Count > 0 ? strs[0] : "", strs.Count > 1 ? strs[1] : "");
    }

    public uint ReadAreaHash() { var a = Area(); return a != 0 ? _r.ReadU32(a + Off.AreaHashOff) : 0; }

    // ────────────── Entity tree traversal ──────────────
    // Heap-валидация указателей — динамическая: валидный адрес = выше 0x10000
    // и ниже базы модуля игры (куча процесса всегда ниже модуля).
    // Раньше был жёсткий диапазон (v >> 32) in [0x3a4, 0x3af] — ломался при
    // переезде кучи между запусками (ASLR): сущности в 0x39C-регионе
    // отфильтровывались как "не-heap" → ReadEntities возвращал 0.
    bool IsHeap(ulong v) => v >= 0x10000 && v < _modBase;

    string ReadEntityPath(ulong entity)
    {
        var det = _r.ReadU64(entity + Off.EntityDetails);
        if (det == 0 || !IsHeap(det)) return "";
        var sp = _r.ReadU64(det + 0x08);
        if (sp < 0x100000000) return "";
        var buf = _r.ReadBytes(sp, 256);
        var sb = new StringBuilder();
        for (int i = 0; i + 2 <= buf.Length; i += 2)
        {
            char c = (char)BitConverter.ToUInt16(buf, i);
            if (c == 0) break;
            if (c is >= ' ' and <= '~') sb.Append(c);
            else break;
        }
        return sb.ToString();
    }

    // Resolve component name from entity details lookup table
    public Dictionary<string, ulong> ResolveComponents(ulong entity)
    {
        var result = new Dictionary<string, ulong>();
        var det = _r.ReadU64(entity + Off.EntityDetails);
        if (det == 0 || !IsHeap(det)) return result;

        var lookup = _r.ReadU64(det + Off.DetailsLookup);
        if (lookup == 0 || !IsHeap(lookup)) return result;

        var bb = _r.ReadU64(lookup + Off.LookupBucketOff);
        var be = _r.ReadU64(lookup + Off.LookupBucketEndOff);
        if (bb == 0 || be <= bb || (be - bb) > 0x2000) return result;

        var bucketData = _r.ReadBytes(bb, (int)(be - bb));
        var clBegin = _r.ReadU64(entity + Off.EntityCompBegin);
        if (clBegin == 0) return result;

        int stride = (int)Off.BucketEntryStride;
        for (int off = 0; off + stride <= bucketData.Length; off += stride)
        {
            ulong namePtr = BinaryPrimitives.ReadUInt64LittleEndian(bucketData.AsSpan(off, 8));
            uint nameIdx = BinaryPrimitives.ReadUInt32LittleEndian(bucketData.AsSpan(off + 8, 4));
            if (namePtr < 0x140000000 || nameIdx > 32) continue;

            // Read ASCII name
            var nb = _r.ReadBytes(namePtr, 48);
            var name = ReadAscii(nb);
            if (name.Length == 0) continue;

            ulong compPtr = _r.ReadU64(clBegin + nameIdx * 8);
            if (compPtr != 0) result[name] = compPtr;
        }
        return result;
    }

    static string ReadAscii(byte[] buf)
    {
        var sb = new StringBuilder();
        foreach (byte b in buf)
        {
            if (b == 0) break;
            if (b is >= 0x20 and <= 0x7E) sb.Append((char)b);
            else return "";
        }
        return sb.ToString();
    }

    public List<(ulong entity, string path, float x, float y, float z, bool alive)> ReadEntities()
    {
        var result = new List<(ulong, string, float, float, float, bool)>();
        var a = Area();
        if (a == 0) return result;

        ulong sentinel = _r.ReadU64(a + Off.AwakeSentinelOff);
        if (sentinel == 0) return result;

        ulong leftmost = _r.ReadU64(sentinel);
        if (leftmost == 0 || leftmost == sentinel) return result;

        var seen = new HashSet<ulong>();
        ulong node = leftmost;

        for (int i = 0; i < 5000 && node != 0 && node != sentinel; i++)
        {
            if (!seen.Add(node)) break;
            var nd = _r.ReadBytes(node, 0x30);
            if (nd.Length < 0x30) break;

            byte isNil = nd[(int)Off.TreeNodeIsNil];
            if (isNil == 0)
            {
                ulong entity = BinaryPrimitives.ReadUInt64LittleEndian(nd.AsSpan((int)Off.TreeNodeEntity, 8));
                if (entity != 0 && IsHeap(entity))
                {
                    var path = ReadEntityPath(entity);
                    float px = 0, py = 0, pz = 0;
                    bool alive = false;

                    // Try to get position from Render component
                    var comps = ResolveComponents(entity);
                    if (comps.TryGetValue("Render", out ulong render) && render != 0)
                    {
                        px = _r.ReadFloat(render + Off.RenderPosX);
                        py = _r.ReadFloat(render + Off.RenderPosY);
                        pz = _r.ReadFloat(render + Off.RenderPosZ);
                    }

                    // «Живость»: Life-компонент сконструирован (vtable != 0) И флаг
                    // смерти Life+0x10 == 0 (1 = труп). Проверено live:
                    //   игрок:            vtable=SET, flag10=0 (жив)
                    //   свежие трупы:     vtable=SET, flag10=1 (мёртв)
                    //   единственный живой моб: vtable=SET, flag10=0
                    if (comps.TryGetValue("Life", out ulong life) && life != 0)
                    {
                        alive = _r.ReadU64(life) != 0 && _r.ReadI32(life + 0x10) == 0;
                    }

                    result.Add((entity, path, px, py, pz, alive));
                }
            }

            // In-order tree walk
            ulong right = BinaryPrimitives.ReadUInt64LittleEndian(nd.AsSpan((int)Off.TreeNodeRight, 8));
            if (right != 0 && right != sentinel)
            {
                node = right;
                for (int j = 0; j < 500; j++)
                {
                    var leftBuf = _r.ReadBytes(node, 8);
                    if (leftBuf.Length < 8) break;
                    ulong leftSub = BinaryPrimitives.ReadUInt64LittleEndian(leftBuf);
                    if (leftSub == sentinel || leftSub == 0) break;
                    node = leftSub;
                }
            }
            else
            {
                bool backed = false;
                while (true)
                {
                    var parentBuf = _r.ReadBytes(node + Off.TreeNodeParent, 8);
                    if (parentBuf.Length < 8) break;
                    ulong parent = BinaryPrimitives.ReadUInt64LittleEndian(parentBuf);
                    if (parent == sentinel || parent == 0) { node = 0; break; }
                    var leftOfParentBuf = _r.ReadBytes(parent, 8);
                    if (leftOfParentBuf.Length < 8) break;
                    ulong leftOfParent = BinaryPrimitives.ReadUInt64LittleEndian(leftOfParentBuf);
                    if (leftOfParent == node) { node = parent; backed = true; break; }
                    node = parent;
                }
                if (!backed) break;
            }
            if (node == leftmost) break;
        }
        return result;
    }

    // ────────────── Terrain ──────────────
    public (byte[] packed, int rows, int cols) ReadTerrainPacked()
    {
        var area = Area(); if (area == 0) return ([], 0, 0);
        var layer0 = _r.ReadU64(area + Off.Layer0Off);
        var end0 = _r.ReadU64(area + Off.Layer0Off + 8);
        if (layer0 == 0 || end0 <= layer0) return ([], 0, 0);

        int totalBytes = (int)(end0 - layer0);
        // Read stride from memory instead of hardcoding
        int stride = (int)_r.ReadU32(area + Off.TerrainStrideOff);
        if (stride <= 0 || stride > 10000) stride = 154; // fallback
        int numValuesPerRow = stride * 2; // nibble-packed: 2 values per byte
        int totalRows = totalBytes / stride;
        if (totalRows <= 0) return ([], 0, 0);

        var data = _r.ReadBytes(layer0, totalBytes);
        if (data.Length < totalBytes) return ([], 0, 0);

        int cols = numValuesPerRow;
        int packedRowBytes = (cols + 7) / 8;
        var packed = new byte[totalRows * packedRowBytes];

        for (int r = 0; r < totalRows; r++)
        {
            int srcRow = totalRows - 1 - r;
            for (int c = 0; c < cols; c++)
            {
                int byteOff = srcRow * stride + c / 2;
                int val = 0;
                if (byteOff < data.Length)
                {
                    byte b = data[byteOff];
                    val = (c & 1) == 0 ? (b & 0x0F) : (b >> 4);
                }
                if (val != 0)
                {
                    int dstByte = r * packedRowBytes + c / 8;
                    int dstBit = 7 - (c % 8);
                    packed[dstByte] |= (byte)(1 << dstBit);
                }
            }
        }
        return (packed, totalRows, cols);
    }

    private static List<string> SplitUtf16(byte[] buf)
    {
        var r = new List<string>(); var sb = new StringBuilder();
        for (int i = 0; i + 2 <= buf.Length; i += 2)
        {
            char c = (char)BitConverter.ToUInt16(buf, i);
            if (c == 0) { if (sb.Length > 0) { r.Add(sb.ToString()); sb.Clear(); } continue; }
            if (c < 0x20 || c > 0x7E) { sb.Clear(); continue; }
            sb.Append(c);
        }
        if (sb.Length > 0) r.Add(sb.ToString());
        return r;
    }
}
