"""probe_cast.py - проверка что R1 реально кастует: счётчик Actor+0x23C."""
import sys, struct, time, urllib.request, logging

sys.path.insert(0, r"C:\Projects\POE2AUTO")
logging.basicConfig(level=logging.CRITICAL)
from game_memory import GameMemory
import game_offsets as off

DAEMON = "http://127.0.0.1:4322"


def find_player(mem):
    base = mem._base
    size = mem._size
    _PAT = b"\x48\x39\x2D"
    _TAIL = b"\x0F\x85\x16\x01\x00\x00"
    for chunk_start in range(base, base + size, 0x100000):
        chunk = mem.read_bytes(chunk_start, 0x100000)
        if not chunk:
            continue
        idx = 0
        while True:
            idx = chunk.find(_PAT, idx)
            if idx < 0:
                break
            if chunk[idx + 7:idx + 13] == _TAIL:
                next_instr = chunk_start + idx + 7
                disp = struct.unpack("<i", chunk[idx + 3:idx + 7])[0]
                slot = next_instr + disp
                gs = mem.read_pointer(slot)
                if gs and gs >= 0x10000:
                    vec = mem.read_pointer(gs + off.GameState.CURRENT_STATE_PTR)
                    in_game = mem.read_pointer(vec) if vec else 0
                    area = mem.read_pointer(in_game + off.InGameState.AREA_INSTANCE) if in_game else 0
                    player = mem.read_pointer(area + off.AreaInstance.LOCAL_PLAYER) if area else 0
                    if player:
                        return player, slot, gs
            idx += 3
    return 0, 0, 0


def components(mem, player):
    details = mem.read_pointer(player + off.Entity.DETAILS_PTR)
    lookup = mem.read_pointer(details + off.EntityDetails.COMPONENT_LOOKUP) if details else 0
    comp_first, comp_count = mem.read_std_vector(player + off.Entity.COMPONENT_LIST, elem_size=8)
    comps = {}
    if lookup and comp_first:
        bucket = mem.read_pointer(lookup + off.ComponentLookUp.BUCKET)
        bucket_end = mem.read_pointer(lookup + off.ComponentLookUp.BUCKET + 8)
        if bucket and bucket_end and 0 < (bucket_end - bucket) <= 0x2000:
            bdata = mem.read_bytes(bucket, int(bucket_end - bucket))
            stride = off.ComponentLookUp.ENTRY_STRIDE
            for i in range(0, len(bdata) - stride + 1, stride):
                name_ptr = struct.unpack("<Q", bdata[i:i + 8])[0]
                idx = struct.unpack("<I", bdata[i + 8:i + 12])[0]
                if name_ptr < 0x140000000 or idx >= comp_count:
                    continue
                nb = mem.read_bytes(name_ptr, 48)
                nm = ""
                for b in nb:
                    if b == 0:
                        break
                    if 0x20 <= b <= 0x7E:
                        nm += chr(b)
                    else:
                        nm = ""
                        break
                if nm:
                    comp_ptr = mem.read_pointer(comp_first + idx * 8)
                    if comp_ptr:
                        comps[nm] = comp_ptr
    return comps


def main():
    mem = GameMemory()
    if not mem.connect():
        print("ERROR: PoE2 не запущена")
        return 1
    try:
        player, slot, gs = find_player(mem)
        print("player=0x%X gs=0x%X" % (player, gs))
        comps = components(mem, player)
        print("components:", sorted(comps.keys()))
        actor = comps.get("Actor")
        if not actor:
            print("no Actor component")
            return 1
        print("Actor=0x%X" % actor)

        def read_counter():
            return mem.read_int(actor + 0x23C)

        print("Actor+0x23C до: %d" % read_counter())
        for i in range(3):
            urllib.request.urlopen(DAEMON + "/tap/right_bumper?hold=100", data=b"", timeout=5)
            time.sleep(0.4)
            print("  тап %d -> Actor+0x23C=%d" % (i + 1, read_counter()))
        time.sleep(0.5)
        print("Actor+0x23C после: %d" % read_counter())
    finally:
        mem.disconnect()


if __name__ == "__main__":
    sys.exit(main())
