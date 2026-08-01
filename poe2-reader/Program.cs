using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

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
    public const ulong PlayerPosOff = 0xA90;

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
    public ulong Player() { var a = Area(); return a != 0 ? _r.ReadU64(a + 0x278) : 0; }

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
        var a = Area(); if (a == 0) return (0, 0);
        return (_r.ReadFloat(a + Off.PlayerPosOff), _r.ReadFloat(a + Off.PlayerPosOff + 4));
    }

    public (float x, float y, float z) ReadPlayerPos3()
    {
        var a = Area(); if (a == 0) return (0, 0, 0);
        return (
            _r.ReadFloat(a + Off.PlayerPosOff),
            _r.ReadFloat(a + Off.PlayerPosOff + 4),
            _r.ReadFloat(a + Off.PlayerPosOff + 8)
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

// ──────────────────────────────────────
// JSON response classes (minimal ShareData)
// ──────────────────────────────────────
public class EntityJson
{
    [JsonPropertyName("p")] public string Path { get; set; } = "";
    [JsonPropertyName("i")] public int Id { get; set; }
    [JsonPropertyName("gp")] public int[] GridPos { get; set; } = [0, 0];
    [JsonPropertyName("wp")] public int[] WorldPos { get; set; } = [0, 0, 0];
    [JsonPropertyName("et")] public string EntityType { get; set; } = "m";
    [JsonPropertyName("h")] public int Hostile { get; set; } = 1;
    [JsonPropertyName("ia")] public int Attackable { get; set; } = 1;
    [JsonPropertyName("t")] public int Targetable { get; set; } = 1;
    [JsonPropertyName("rn")] public string RenderName { get; set; } = "";
    [JsonPropertyName("l")] public int[]? Life { get; set; }
}

public class PlayerInfoJson
{
    [JsonPropertyName("gp")] public int[] GridPos { get; set; } = [0, 0];
    [JsonPropertyName("im")] public int IsMoving { get; set; }
    [JsonPropertyName("l")] public int[] Life { get; set; } = [0, 0, 0, 0, 0, 0, 0, 0, 0];
    [JsonPropertyName("db")] public List<object> Debuffs { get; set; } = [];
    [JsonPropertyName("b")] public List<object> Buffs { get; set; } = [];
}

public class GetDataResponse
{
    [JsonPropertyName("g_s")] public int GameState { get; set; } = 20;
    [JsonPropertyName("m")] public float[]? Matrix { get; set; }
    [JsonPropertyName("terrain_string")] public string TerrainString { get; set; } = "";
    [JsonPropertyName("terrain_packed")] public string? TerrainPacked { get; set; }
    [JsonPropertyName("terrain_shape")] public int[]? TerrainShape { get; set; }
    [JsonPropertyName("area_raw_name")] public string AreaRawName { get; set; } = "";
    [JsonPropertyName("ah")] public uint AreaHash { get; set; }
    [JsonPropertyName("awake_entities")] public List<EntityJson> Entities { get; set; } = [];
    [JsonPropertyName("pi")] public PlayerInfoJson Player { get; set; } = new();
    [JsonPropertyName("t")] public int Tick { get; set; }
    [JsonPropertyName("w")] public List<int> WindowBorders { get; set; } = [0, 2560, 0, 1440];
    [JsonPropertyName("IsLoading_b")] public bool IsLoading { get; set; } = false;
    [JsonPropertyName("f")] public FlasksJson Flasks { get; set; } = new();
    [JsonPropertyName("ipv")] public bool InvitesPanelVisible { get; set; } = false;
    [JsonPropertyName("s")] public SkillsJson Skills { get; set; } = new();
    [JsonPropertyName("vl")] public List<object> VisibleLabels { get; set; } = [];
    [JsonPropertyName("i")] public List<object> GroundItems { get; set; } = [];
}

public class SkillsJson
{
    [JsonPropertyName("c_b_u")] public List<int> CanBeUsed { get; set; } = [];
    [JsonPropertyName("cs")] public List<int> CastTimes { get; set; } = [];
    [JsonPropertyName("i_n")] public List<string> InternalNames { get; set; } = [];
    [JsonPropertyName("d")] public List<object> Descriptions { get; set; } = [];
    [JsonPropertyName("tu")] public List<object> TotalUses { get; set; } = [];
}

public class FlasksJson
{
    [JsonPropertyName("n")] public List<string> Names { get; set; } = [];
    [JsonPropertyName("i")] public List<int> Indices { get; set; } = [];
    [JsonPropertyName("cu")] public List<int> CanUse { get; set; } = [];
}

public class ScreenPosResponse
{
    [JsonPropertyName("result")] public List<int> Result { get; set; } = [0, 0];
}

#if !READER_LIB
// ──────────────────────────────────────
// TCP Server
// ──────────────────────────────────────
internal class ShareDataServer(GameReader _reader)
{
    private readonly JsonSerializerOptions _json = new() { WriteIndented = false, DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull };

    public async Task Run(int port, CancellationToken ct)
    {
        var listener = new TcpListener(IPAddress.Any, port);
        listener.Start();
        Console.Error.WriteLine($"[Srv] Listening :{port}");
        try
        {
            while (!ct.IsCancellationRequested)
            {
                var client = await listener.AcceptTcpClientAsync(ct);
                _ = Handle(client, ct);
            }
        }
        catch (OperationCanceledException) { }
        finally { listener.Stop(); }
    }

    private async Task Handle(TcpClient client, CancellationToken ct)
    {
        try
        {
            using var stream = client.GetStream();
            var lineBuf = new byte[4096];
            while (!ct.IsCancellationRequested)
            {
                var line = await ReadLine(stream, ct);
                if (line == null) break;
                line = line.Trim();
                Console.Error.WriteLine($"[Srv] << {line}");

                string response;
                if (line.StartsWith("/getData"))
                {
                    response = JsonSerializer.Serialize(MakeGetData(), _json) + "\n";
                }
                else if (line.StartsWith("/getScreenPos"))
                {
                    response = HandleScreenPos(line);
                }
                else if (line is "/ForceRefreshArea" or "ping")
                {
                    response = JsonSerializer.Serialize(new GetDataResponse { Tick = Tick() }, _json) + "\n";
                }
                else if (line.StartsWith("/getResurrectUi"))
                {
                    // {"sz":[0,0,0,0],"v":false}
                    response = "{\"sz\":[0,0,0,0],\"v\":false}\n";
                }
                else if (line.StartsWith("/getVisibleLabels"))
                {
                    response = "[]\n";
                }
                else if (line.StartsWith("/getItemsOnGroundLabelsVisible"))
                {
                    response = "[]\n";
                }
                else if (line.StartsWith("/getVisibleLabelOnGroundEntities"))
                {
                    response = "[]\n";
                }
                else if (line.StartsWith("/getOpenedStashInfo"))
                {
                    response = "{\"status\":\"closed\",\"tab_index\":0,\"items\":[],\"total_stash_tab_count\":0,\"stash_tab_type\":\"\",\"s_b_p_ls\":[]}\n";
                }
                else if (line.StartsWith("/getInventoryInfo"))
                {
                    response = "{\"IsOpened\":false,\"items\":[],\"grid\":[]}\n";
                }
                else if (line.StartsWith("/getMinimapIcons"))
                {
                    response = "{\"icons\":[]}\n";
                }
                else if (line.StartsWith("/gemsToLevel"))
                {
                    response = "{\"gems\":[]}\n";
                }
                else if (line.StartsWith("/getHoveredItemInfo"))
                {
                    response = "{\"item\":null}\n";
                }
                else if (line.StartsWith("/getPreloadedFiles"))
                {
                    response = "{\"files\":[]}\n";
                }
                else if (line.StartsWith("/getNecropolisPopupUI"))
                {
                    response = "{\"eb_sz\":0,\"v\":false}\n";
                }
                else if (line.StartsWith("/getWaypointsState") || line.StartsWith("/getWaypoint"))
                {
                    response = "{\"waypoints\":[]}\n";
                }
                else if (line.StartsWith("/getSkillBar"))
                {
                    response = "{\"skills\":[]}\n";
                }
                else if (line.StartsWith("/getQuestStates"))
                {
                    response = "{\"quests\":[]}\n";
                }
                else if (line.StartsWith("/getQuestFlags"))
                {
                    response = "{\"flags\":{}}\n";
                }
                else if (line.StartsWith("/mapDeviceInfo"))
                {
                    response = "{\"v\":false}\n";
                }
                else if (line.StartsWith("/getBanditDialogueUi"))
                {
                    response = "{\"v\":false}\n";
                }
                else if (line.StartsWith("/getKirakMissionsUi"))
                {
                    response = "{\"v\":false}\n";
                }
                else if (line.StartsWith("/getPurchaseWindowHideoutUi"))
                {
                    response = "{\"v\":false}\n";
                }
                else if (line.StartsWith("/getRitualUi"))
                {
                    response = "{\"v\":false,\"rt_b_sz\":[0,0,0,0],\"r_b_sz\":[0,0,0,0],\"d_b_sz\":[0,0,0,0]}\n";
                }
                else if (line.StartsWith("/getAtlasProgress"))
                {
                    response = "{\"progress\":0}\n";
                }
                else if (line.StartsWith("/getWorldMapUi"))
                {
                    response = "{\"v\":false}\n";
                }
                else if (line.StartsWith("/getLocationOnScreen"))
                {
                    response = "{\"x\":0,\"y\":0,\"v\":false}\n";
                }
                else if (line.StartsWith("/getEntityIdByPlayerName"))
                {
                    response = "{\"id\":0}\n";
                }
                else if (line.StartsWith("/getPartyInfo"))
                {
                    response = "{\"members\":[]}\n";
                }
                else if (line.StartsWith("/getUltimatumNextWaveUi"))
                {
                    response = "{\"v\":false}\n";
                }
                else if (line.StartsWith("/getAnointUi"))
                {
                    response = "{\"v\":false,\"sz\":[0,0,0,0]}\n";
                }
                else if (line.StartsWith("/getNpcDialogueUi"))
                {
                    response = "{\"v\":false,\"sz\":[0,0,0,0],\"choices\":[]}\n";
                }
                else if (line.StartsWith("/getIncursionUi"))
                {
                    response = "{\"v\":false}\n";
                }
                else if (line.StartsWith("/getMapInfo"))
                {
                    response = "{\"v\":false,\"sz\":[0,0,0,0]}\n";
                }
                else if (line.StartsWith("/getAuctionHouseUi"))
                {
                    response = "{\"v\":false}\n";
                }
                else if (line.StartsWith("/getOpenedStash"))
                {
                    response = "{\"v\":false,\"items\":[]}\n";
                }
                else if (line.StartsWith("/diagLife"))
                {
                    response = HandleDiagLife() + "\n";
                }
                else if (line.StartsWith("/diagGameState"))
                {
                    response = HandleDiagGameState() + "\n";
                }
                else if (line.StartsWith("/diagGSO"))
                {
                    response = HandleDiagGSO() + "\n";
                }
                else if (line.StartsWith("/diagModuleScan"))
                {
                    response = HandleDiagModuleScan() + "\n";
                }
                else if (line.StartsWith("/diagCamera"))
                {
                    response = HandleDiagCamera() + "\n";
                }
                else
                {
                    Console.Error.WriteLine($"[Srv] unknown endpoint: {line}");
                    response = "{}\n";
                }

                var buf = Encoding.UTF8.GetBytes(response);
                await stream.WriteAsync(buf, ct);
            }
        }
        catch (Exception ex) { Console.Error.WriteLine($"[Srv] {ex.Message}"); }
    }

    private string HandleScreenPos(string request)
    {
        try
        {
            // /getScreenPos?y=1234&x=5678
            var q = request.Split('?')[1];
            var parts = q.Split('&');
            int y = 0, x = 0;
            foreach (var p in parts)
            {
                var kv = p.Split('=');
                if (kv[0] == "y") y = int.Parse(kv[1]);
                else if (kv[0] == "x") x = int.Parse(kv[1]);
            }
            var result = _reader.GetScreenPos(x, y);
            return JsonSerializer.Serialize(result, _json) + "\n";
        }
        catch
        {
            return "{\"error\":\"bad_request\"}\n";
        }
    }

    private string HandleDiagLife()
    {
        var entities = _reader.ReadEntities();
        var result = new Dictionary<string, object>();

        foreach (var (entity, path, ex, ey, ez, alive) in entities)
        {
            if (!path.Contains("Characters")) continue;
            var comps = _reader.ResolveComponents(entity);
            result["player_path"] = path;
            result["alive"] = alive;
            result["components"] = new List<string>(comps.Keys);

            if (comps.TryGetValue("Life", out ulong lifePtr) && lifePtr != 0)
            {
                result["life_ptr"] = $"0x{lifePtr:X}";
                var lifeBytes = _reader.Reader.ReadBytes(lifePtr, 128);
                var hexDump = new List<string>();
                for (int i = 0; i < lifeBytes.Length; i += 4)
                {
                    if (i + 4 <= lifeBytes.Length)
                    {
                        float f = BitConverter.ToSingle(lifeBytes, i);
                        uint u = BitConverter.ToUInt32(lifeBytes, i);
                        hexDump.Add($"+0x{i:X2}: f={f:F1} u={u} 0x{u:X8}");
                    }
                }
                result["life_dump"] = hexDump;
            }
            else
            {
                result["life_ptr"] = "not found";
            }

            break;
        }

        return JsonSerializer.Serialize(result, _json);
    }

    private string HandleDiagGameState()
    {
        var result = new Dictionary<string, object>();

        // Главный признак «в игре»: значение глобального GSO slot.
        // Работает даже когда InGameState==0 (меню/loading) — в отличие от старой версии,
        // которая урезала ответ на igs==0 и не показывала причину.
        var gsoVal = _reader.Reader.ReadU64(_reader.Gso);
        result["gso_slot"] = $"0x{_reader.Gso:X}";
        result["gso_value"] = $"0x{gsoVal:X}";
        result["in_game"] = gsoVal != 0;
        result["game_state"] = _reader.ReadGameState();

        var igs = _reader.Igs();
        result["igs"] = $"0x{igs:X}";

        if (igs == 0) return JsonSerializer.Serialize(result, _json);

        // Scan IGS for int32 values that could be game state (0, 1, 10, 20)
        var candidates = new List<Dictionary<string, object>>();
        var igsBytes = _reader.Reader.ReadBytes(igs, 0x1000);
        for (int off = 0; off + 4 <= igsBytes.Length; off += 4)
        {
            int val = BitConverter.ToInt32(igsBytes, off);
            if (val is 0 or 1 or 10 or 20)
            {
                candidates.Add(new Dictionary<string, object>
                {
                    ["offset"] = $"0x{off:X}",
                    ["value"] = val
                });
            }
        }
        result["candidates"] = candidates;

        // Also scan around AreaInstance for game state
        var area = _reader.Area();
        if (area != 0)
        {
            result["area"] = $"0x{area:X}";
            var areaBytes = _reader.Reader.ReadBytes(area, 0x1000);
            var areaCandidates = new List<Dictionary<string, object>>();
            for (int off = 0; off + 4 <= areaBytes.Length; off += 4)
            {
                int val = BitConverter.ToInt32(areaBytes, off);
                if (val is 0 or 1 or 10 or 20)
                {
                    areaCandidates.Add(new Dictionary<string, object>
                    {
                        ["offset"] = $"0x{off:X}",
                        ["value"] = val
                    });
                }
            }
            result["area_candidates"] = areaCandidates;
        }

        return JsonSerializer.Serialize(result, _json);
    }

    private string HandleDiagGSO()
    {
        var result = new Dictionary<string, object>();
        result["gso"] = $"0x{_reader.Gso:X}";

        // Read 0x100 bytes around GSO
        var gsoBytes = _reader.Reader.ReadBytes(_reader.Gso, 0x100);
        var gsoDump = new List<string>();
        for (int i = 0; i < gsoBytes.Length; i += 8)
        {
            ulong val = BitConverter.ToUInt64(gsoBytes, i);
            gsoDump.Add($"+0x{i:X2}: 0x{val:X}");
        }
        result["gso_dump"] = gsoDump;

        // Follow GSO[0] -> IGS -> follow pointers
        ulong gsoPtr0 = _reader.Reader.ReadU64(_reader.Gso);
        result["gso_ptr0"] = $"0x{gsoPtr0:X}";

        if (gsoPtr0 != 0)
        {
            // Dump 0x100 bytes at gsoPtr0
            var ptr0Bytes = _reader.Reader.ReadBytes(gsoPtr0, 0x100);
            var ptr0Dump = new List<string>();
            for (int i = 0; i < ptr0Bytes.Length; i += 8)
            {
                ulong val = BitConverter.ToUInt64(ptr0Bytes, i);
                ptr0Dump.Add($"+0x{i:X2}: 0x{val:X}");
            }
            result["gso_ptr0_dump"] = ptr0Dump;

            // Follow pointer at gsoPtr0+0x88 (IGS)
            ulong igs = _reader.Reader.ReadU64(gsoPtr0 + 0x88);
            result["igs_from_gso"] = $"0x{igs:X}";

            if (igs != 0)
            {
                // Dump first 0x200 bytes of IGS
                var igsBytes = _reader.Reader.ReadBytes(igs, 0x200);
                var igsDump = new List<string>();
                for (int i = 0; i < igsBytes.Length; i += 4)
                {
                    int val = BitConverter.ToInt32(igsBytes, i);
                    if (val != 0)
                        igsDump.Add($"+0x{i:X2}: {val} (0x{BitConverter.ToUInt32(igsBytes, i):X})");
                }
                result["igs_nonzero"] = igsDump;

                // Follow IGS+0x290 (AreaInstance)
                ulong area = _reader.Reader.ReadU64(igs + 0x290);
                result["area_from_igs"] = $"0x{area:X}";

                if (area != 0)
                {
                    // Check for int32=20 in area (game_state candidate)
                    var areaBytes = _reader.Reader.ReadBytes(area, 0x400);
                    var areaVals = new List<string>();
                    for (int i = 0; i < areaBytes.Length; i += 4)
                    {
                        int val = BitConverter.ToInt32(areaBytes, i);
                        if (val == 20 || val == 10 || val == 1)
                            areaVals.Add($"+0x{i:X2}: {val}");
                    }
                    result["area_g20_candidates"] = areaVals;

                    // Check area+0x278 (player pointer)
                    ulong player = _reader.Reader.ReadU64(area + 0x278);
                    result["player_ptr"] = $"0x{player:X}";

                    // Check for game state near the player or other known offsets
                    // Also check area+0x368 (camera)
                    ulong cam = _reader.Reader.ReadU64(area + 0x368);
                    result["cam_ptr"] = $"0x{cam:X}";
                }
            }
        }

        return JsonSerializer.Serialize(result, _json);
    }

    private string HandleDiagModuleScan()
    {
        var result = new Dictionary<string, object>();

        var (wx, wy, wz) = _reader.ReadPlayerPos3();
        result["world_pos"] = new float[] { wx, wy, wz };

        const float GridToWorld = 250f / 23f;
        var gx = (int)(wx / GridToWorld);
        var gy = (int)(wy / GridToWorld);
        result["grid_pos_calc"] = new int[] { gx, gy };

        var area = _reader.Area();
        result["area_ptr"] = $"0x{area:X}";

        var layer0 = _reader.Reader.ReadU64(area + Off.Layer0Off);
        var end0 = _reader.Reader.ReadU64(area + Off.Layer0Off + 8);
        result["layer0"] = $"0x{layer0:X}";
        result["end0"] = $"0x{end0:X}";
        long totalBytes = (long)(end0 - layer0);
        result["total_bytes"] = totalBytes;

        if (layer0 > 0 && end0 > layer0 && totalBytes > 0)
        {
            // Read terrain data
            var data = _reader.Reader.ReadBytes(layer0, (int)totalBytes);
            if (data.Length == (int)totalBytes)
            {
                // Try stride=1438 (totalBytes/1438 = 1771 exact)
                int strideA = 1438;
                int rowsA = (int)totalBytes / strideA;
                int colsA = strideA * 2;
                int passableA = 0, totalCellsA = 0;
                for (int r = 0; r < rowsA; r++)
                    for (int c = 0; c < strideA; c++)
                    {
                        int byteOff = r * strideA + c;
                        byte b = data[byteOff];
                        int hi = b & 0x0F;
                        int lo = b >> 4;
                        if (hi != 0) passableA++;
                        if (lo != 0) passableA++;
                        totalCellsA += 2;
                    }
                result["stride_1438"] = new { rows = rowsA, cols = colsA, passable = passableA, total = totalCellsA, ratio = totalCellsA > 0 ? (double)passableA / totalCellsA : 0 };

                // Try stride=154 (current)
                int strideB = 154;
                int rowsB = (int)totalBytes / strideB;
                int colsB = strideB * 2;
                int passableB = 0, totalCellsB = 0;
                for (int r = 0; r < rowsB; r++)
                    for (int c = 0; c < strideB; c++)
                    {
                        int byteOff = r * strideB + c;
                        byte b = data[byteOff];
                        int hi = b & 0x0F;
                        int lo = b >> 4;
                        if (hi != 0) passableB++;
                        if (lo != 0) passableB++;
                        totalCellsB += 2;
                    }
                result["stride_154"] = new { rows = rowsB, cols = colsB, passable = passableB, total = totalCellsB, ratio = totalCellsB > 0 ? (double)passableB / totalCellsB : 0 };

                // Try byte-packed stride=154 (1 value per byte)
                int passableC = 0;
                for (int i = 0; i < data.Length; i++) if (data[i] != 0) passableC++;
                result["byte_packed_154"] = new { rows = (int)totalBytes / 154, cols = 154, passable = passableC, total = data.Length, ratio = data.Length > 0 ? (double)passableC / data.Length : 0 };

                // Try stride=154 nibble-packed but with player at grid_x clamped
                int playerGx_clamped = Math.Min(gx, colsB - 1);
                int playerGy_clamped = Math.Min(gy, rowsB - 1);
                result["grid_clamped_stride154"] = new int[] { playerGx_clamped, playerGy_clamped };

                // Check player grid with stride=1438
                int playerGx_1438 = Math.Min(gx, colsA - 1);
                int playerGy_1438 = Math.Min(gy, rowsA - 1);
                result["grid_clamped_stride1438"] = new int[] { playerGx_1438, playerGy_1438 };

                // Dump 20 bytes from middle of terrain (at ~50% height, for both strides)
                int midRowB = rowsB / 2;
                int midByteB = midRowB * strideB + strideB / 2;
                if (midByteB + 20 <= data.Length)
                {
                    var midBytes = new List<int>();
                    for (int i = 0; i < 20; i++) midBytes.Add(data[midByteB + i]);
                    result["mid_terrain_stride154"] = new { row = midRowB, offset = midByteB, bytes = midBytes };
                }

                int midRowA = rowsA / 2;
                int midByteA = midRowA * strideA + strideA / 2;
                if (midByteA + 20 <= data.Length)
                {
                    var midBytesA = new List<int>();
                    for (int i = 0; i < 20; i++) midBytesA.Add(data[midByteA + i]);
                    result["mid_terrain_stride1438"] = new { row = midRowA, offset = midByteA, bytes = midBytesA };
                }

                // Dump 20 bytes near where player would be with stride=154
                if (gy < rowsB && gx / 2 < data.Length)
                {
                    int nearPlayerB = gy * strideB + gx / 2;
                    if (nearPlayerB + 20 <= data.Length && nearPlayerB >= 0)
                    {
                        var nearBytes = new List<int>();
                        for (int i = 0; i < 20; i++) nearBytes.Add(data[nearPlayerB + i]);
                        result["near_player_stride154"] = new { row = gy, byte_offset = nearPlayerB, bytes = nearBytes };
                    }
                }

                // Dump raw uint32 at area+0x9F0, 0x948, 0x900
                result["raw_at_9F0"] = _reader.Reader.ReadU32(area + 0x9F0);
                result["raw_at_948"] = _reader.Reader.ReadU32(area + 0x948);
                result["raw_at_900"] = _reader.Reader.ReadU32(area + 0x900);
                result["raw64_at_9F0"] = $"0x{_reader.Reader.ReadU64(area + 0x9F0):X}";
                result["raw64_at_948"] = $"0x{_reader.Reader.ReadU64(area + 0x948):X}";
            }
        }

        return JsonSerializer.Serialize(result, _json);
    }

    private string HandleDiagCamera()
    {
        var result = new Dictionary<string, object>();
        var cam = _reader.Cam();
        result["cam_ptr"] = $"0x{cam:X}";
        if (cam != 0)
        {
            // Use SAME camera ptr for matrix read too
            var buf = _reader.Reader.ReadBytes(cam + Off.CameraMatrixOff, 64);
            if (buf.Length >= 64)
            {
                var m = new List<float>();
                for (int i = 0; i < 64; i += 4)
                    m.Add(BitConverter.ToSingle(buf, i));
                result["matrix_at_0x1A0"] = m.Select(f => (double)f).ToList();
            }

            // Dump block at various offsets for identification
            int[] testOffs = [0x100, 0x140, 0x180, 0x1A0, 0x1C0, 0x1E0, 0x200];
            var rawDump = new List<string>();
            foreach (int off in testOffs)
            {
                ulong addr = cam + (ulong)off;
                var b = _reader.Reader.ReadBytes(addr, 64);
                if (b.Length < 64) continue;
                var floats = new List<float>();
                for (int i = 0; i < 64; i += 4)
                    floats.Add(BitConverter.ToSingle(b, i));
                rawDump.Add($"+0x{off:X2}: [{string.Join(", ", floats.Select(f => f.ToString("F6")))}]");
            }
            result["matrix_candidates"] = rawDump;
        }
        return JsonSerializer.Serialize(result, _json);
    }

    private GetDataResponse MakeGetData()
    {
        var r = new GetDataResponse { Tick = Tick() };

        // Реальный game_state: 0 = не в игре (меню/char-select/loading), 20 = в игре.
        // Заменяет захардкоженный дефолт 20 в классе GetDataResponse.
        r.GameState = _reader.ReadGameState();

        r.Matrix = _reader.ReadMatrix();
        var (code, name) = _reader.ReadAreaStrings();
        r.AreaRawName = name;
        r.AreaHash = _reader.ReadAreaHash();
        var (terrainPacked, terrainRows, terrainCols) = _reader.ReadTerrainPacked();
        if (terrainPacked.Length > 0)
        {
            r.TerrainPacked = Convert.ToBase64String(terrainPacked);
            r.TerrainShape = [terrainRows, terrainCols];
        }

        // Player position
        var (wx, wy, wz) = _reader.ReadPlayerPos3();
        if (Math.Abs(wx) > 1)
        {
            const float GridToWorld = 250f / 23f;
            var gx = (int)(wx / GridToWorld);
            var gy = (int)(wy / GridToWorld);
            r.Player = new PlayerInfoJson { GridPos = [gx, gy], Life = [5000, 5000, 0, 2000, 2000, 0, 0, 0, 0] };
        }

        // Entity tree
        const float G2W = 250f / 23f;
        var entities = _reader.ReadEntities();
        var entityList = new List<EntityJson>();
        int id = 0;
        foreach (var (entity, path, ex, ey, ez, alive) in entities)
        {
            if (ex == 0 && ey == 0 && path != "")
            {
                // Entity without Render position — skip (or use player pos for player only)
                if (!path.Contains("Characters")) continue;
            }
            int gx = ex != 0 ? (int)(ex / G2W) : 0;
            int gy = ey != 0 ? (int)(ey / G2W) : 0;

            string entityType = "m";
            int hostile = 1, attackable = 1, targetable = 1;
            string renderName = "";
            if (path.Contains("Characters")) { entityType = "Player"; hostile = 0; attackable = 0; }
            else if (path.Contains("Monsters")) { entityType = "m"; }
            else if (path.Contains("Chests")) { entityType = "c"; hostile = 0; attackable = 0; }
            else if (path.Contains("NPC")) { entityType = "n"; hostile = 0; attackable = 0; }
            else if (path.Contains("Effects")) { entityType = "fx"; hostile = 0; attackable = 0; }
            else if (path.Contains("Projectiles")) { entityType = "p"; hostile = 0; attackable = 0; }
            else if (path.Contains("Terrain")) { entityType = "t"; hostile = 0; attackable = 0; }
            else if (path.Contains("MiscellaneousObjects")) { entityType = "mo"; hostile = 0; attackable = 0; }

            // Extract name from path
            var lastSlash = path.LastIndexOf('/');
            if (lastSlash >= 0 && lastSlash < path.Length - 1)
            {
                renderName = path[(lastSlash + 1)..];
                var atIdx = renderName.IndexOf('@');
                if (atIdx > 0) renderName = renderName[..atIdx];
            }

            entityList.Add(new EntityJson
            {
                Path = path,
                Id = id++,
                GridPos = [gx, gy],
                WorldPos = [(int)ex, (int)ey, 0],
                EntityType = entityType,
                Hostile = hostile,
                Attackable = attackable,
                Targetable = targetable,
                RenderName = renderName,
                Life = [0, 0, 0, 0, 0, 0, 0, 0, 0],
            });
        }
        r.Entities = entityList;

        return r;
    }

    private static int Tick() => Environment.TickCount;

    private static async Task<string?> ReadLine(NetworkStream s, CancellationToken ct)
    {
        var sb = new StringBuilder();
        var buf = new byte[1];
        while (true)
        {
            int read = await s.ReadAsync(buf, ct);
            if (read == 0) return sb.Length > 0 ? sb.ToString() : null;
            char c = (char)buf[0];
            if (c == '\n') return sb.ToString();
            sb.Append(c);
        }
    }
}

// ──────────────────────────────────────
// Entry
// ──────────────────────────────────────
internal class Program
{
    private const string ExeName = "PathOfExileSteam.exe";
    private const int Port = 50006;

    static async Task Main()
    {
        Console.Error.WriteLine("[PoE2Reader] Starting...");

        int pid = -1;
        while (pid == -1)
        {
            var procs = Process.GetProcessesByName("PathOfExileSteam");
            if (procs.Length > 0) pid = procs[0].Id;
            else { Console.Error.WriteLine("[PoE2Reader] Waiting for PathOfExileSteam..."); await Task.Delay(2000); }
        }
        Console.Error.WriteLine($"[PoE2Reader] PID {pid}");

        var h = Native.OpenProcess(Native.PROCESS_VM_READ | Native.PROCESS_QUERY_INFORMATION, false, pid);
        if (h == IntPtr.Zero) { Console.Error.WriteLine("[PoE2Reader] OpenProcess FAILED (run as admin)"); return; }

        var reader = new MemoryReader(h);
        var scanner = new PatternScanner(reader);
        var mod = scanner.GetModule(pid, ExeName);
        if (mod == null) { Console.Error.WriteLine("[PoE2Reader] Module not found"); return; }
        Console.Error.WriteLine($"[PoE2Reader] Base 0x{mod.Value.baseAddr:X}");

        var gso = scanner.FindGsoSlot(mod.Value.baseAddr, mod.Value.size);
        if (gso == 0) { Console.Error.WriteLine("[PoE2Reader] GSO pattern not found"); return; }
        Console.Error.WriteLine($"[PoE2Reader] GSO 0x{gso:X}");

        var g = new GameReader(reader);
        g.SetGso(gso);
        g.SetModuleBase(mod.Value.baseAddr);

        var igs = g.Igs();
        Console.Error.WriteLine($"[PoE2Reader] IGS 0x{igs:X}");
        var area = g.Area();
        Console.Error.WriteLine($"[PoE2Reader] Area 0x{area:X}");
        var (code, name) = g.ReadAreaStrings();
        Console.Error.WriteLine($"[PoE2Reader] '{code}' '{name}'");

        var cts = new CancellationTokenSource();
        Console.Error.WriteLine($"[PoE2Reader] TCP on :{Port}");
        await new ShareDataServer(g).Run(Port, cts.Token);
    }
}
#endif
