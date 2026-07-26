"""Research tools для обнаружения и валидации оффсетов PoE2.

Использование:
    python research.py --chain          # валидация цепочки GameState → Player
    python research.py --hp <value>     # поиск Life компонента по HP значению
    python research.py --entity <addr>  # дамп компонентов сущности
    python research.py --find-entities  # поиск AwakeEntities в AreaInstance
    python research.py --dump <addr> <size>  # hex dump памяти

Адаптировано из POE2Radar.Research (Sikaka, MIT).
"""
import sys
import os
import struct
import logging

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.dirname(__file__))

from game_memory import GameMemory
from entity_reader import EntityReader
import game_offsets as off

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("research")


def cmd_chain(mem: GameMemory, reader: EntityReader):
    """Валидация полной цепочки: GameState → InGameState → AreaInstance → LocalPlayer."""
    print("=== Chain Validation ===\n")

    # AOB scan для GameStates
    pattern_info = off.AOB_PATTERNS.get("game_states")
    if not pattern_info:
        print("ERROR: No AOB pattern for game_states")
        return False

    pattern, wildcards, disp_off, instr_len, desc = pattern_info
    print(f"AOB pattern: {desc}")
    print(f"Scanning .exe sections...")

    matches = mem.aob_scan(pattern, wildcards)
    if not matches:
        print("ERROR: No AOB matches found")
        return False

    print(f"Found {len(matches)} matches")

    for i, match_addr in enumerate(matches):
        print(f"\n--- Match {i+1} @ 0x{match_addr:X} ---")

        # Resolve RIP-relative
        next_instr = match_addr + instr_len
        disp_data = mem.read_bytes(match_addr + disp_off, 4)
        if not disp_data:
            print("  Cannot read displacement")
            continue

        displacement = struct.unpack("<i", disp_data)[0]
        slot_addr = next_instr + displacement
        print(f"  Slot address: 0x{slot_addr:X}")

        # Dereference slot
        game_state = mem.read_pointer(slot_addr)
        if not game_state:
            print("  GameState: NULL")
            continue
        print(f"  GameState: 0x{game_state:X}")

        # GameState → InGameState
        vec_ptr = mem.read_pointer(game_state + off.GameState.CURRENT_STATE_PTR)
        if not vec_ptr:
            print("  InGameState: cannot read CurrentStatePtr")
            continue

        in_game = mem.read_pointer(vec_ptr)
        if not in_game:
            print("  InGameState: NULL")
            continue
        print(f"  InGameState: 0x{in_game:X}")

        # InGameState → AreaInstance
        area_inst = mem.read_pointer(in_game + off.InGameState.AREA_INSTANCE)
        if not area_inst:
            print("  AreaInstance: NULL")
            continue
        print(f"  AreaInstance: 0x{area_inst:X}")

        # AreaInstance → LocalPlayer
        player = mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
        if not player:
            print("  LocalPlayer: NULL")
            continue
        print(f"  LocalPlayer: 0x{player:X}")

        # Validate player metadata
        meta = reader._get_metadata(player)
        if meta and ("Metadata/" in meta or "data/" in meta):
            print(f"  Player metadata: {meta}")
            print(f"  ✓ VALID CHAIN")
            return True
        else:
            print(f"  Player metadata: {meta or 'NULL'}")
            print(f"  ✗ Invalid metadata")

    print("\n✗ No valid chain found")
    return False


def cmd_hp(mem: GameMemory, target_hp: int):
    """Поиск Life компонента по значению HP (value scan)."""
    print(f"=== HP Value Scan: {target_hp} ===\n")

    reader = EntityReader(mem)
    area_inst = _find_area_instance(mem, reader)
    if not area_inst:
        print("ERROR: Cannot find AreaInstance")
        return

    player = mem.read_pointer(area_inst + off.AreaInstance.LOCAL_PLAYER)
    if not player:
        print("ERROR: Cannot find LocalPlayer")
        return

    # Resolve Life component
    comps = reader._resolve_components(player)
    life_addr = comps.get("Life")
    if not life_addr:
        print("ERROR: Life component not found")
        return

    print(f"Life component: 0x{life_addr:X}")

    # Scan Life component for VitalStruct candidates
    print(f"\nScanning Life+0x80..0x400 for VitalStruct with Max={target_hp}...")
    found = []
    for offset in range(0x80, 0x401, 4):
        data = mem.read_bytes(life_addr + offset, off.VitalStruct.SIZE)
        if not data:
            continue
        max_val = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX+4])[0]
        current = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT+4])[0]
        if max_val == target_hp and current >= 0:
            found.append((offset, max_val, current))
            print(f"  +0x{offset:03X}: Max={max_val}, Current={current} ✓ MATCH")
        elif abs(max_val - target_hp) < target_hp * 0.1 and current >= 0:
            print(f"  +0x{offset:03X}: Max={max_val}, Current={current} (close)")

    if found:
        print(f"\n✓ Found {len(found)} candidates")
        print(f"  Recommended Health offset: Life + 0x{found[0][0]:X}")
    else:
        print(f"\n✗ No VitalStruct with Max={target_hp} found")


def cmd_entity(mem: GameMemory, addr: int):
    """Дамп компонентов сущности."""
    print(f"=== Entity Dump @ 0x{addr:X} ===\n")

    reader = EntityReader(mem)

    # EntityDetails
    details = mem.read_pointer(addr + off.Entity.DETAILS_PTR)
    print(f"Details ptr: 0x{details:X}" if details else "Details ptr: NULL")

    if details:
        meta = reader._get_metadata(addr)
        print(f"Metadata: {meta}")

        # ComponentLookUp
        lookup = mem.read_pointer(details + off.EntityDetails.COMPONENT_LOOKUP)
        print(f"ComponentLookUp: 0x{lookup:X}" if lookup else "ComponentLookUp: NULL")

    # ComponentList
    comp_first = mem.read_pointer(addr + off.Entity.COMPONENT_LIST)
    comp_last = mem.read_pointer(addr + off.Entity.COMPONENT_LIST + 8)
    if comp_first and comp_last:
        count = (comp_last - comp_first) // 8
        print(f"ComponentList: 0x{comp_first:X} ({count} components)")

        # Dump each component address
        for i in range(min(count, 32)):
            comp_addr = mem.read_pointer(comp_first + i * 8)
            if comp_addr:
                print(f"  [{i}] 0x{comp_addr:X}")

    # Position
    comps = reader._resolve_components(addr)
    print(f"\nResolved components: {list(comps.keys())}")

    render_addr = comps.get("Render")
    if render_addr:
        pos = mem.read_vec3(render_addr + off.Render.WORLD_POSITION)
        if pos:
            print(f"  Position: ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})")
            print(f"  Grid: ({pos[0]/off.WORLD_TO_GRID_RATIO:.1f}, {pos[2]/off.WORLD_TO_GRID_RATIO:.1f})")

    life_addr = comps.get("Life")
    if life_addr:
        data = mem.read_bytes(life_addr + off.Life.HEALTH, off.VitalStruct.SIZE)
        if data:
            hp_max = struct.unpack("<i", data[off.VitalStruct.MAX:off.VitalStruct.MAX+4])[0]
            hp_cur = struct.unpack("<i", data[off.VitalStruct.CURRENT:off.VitalStruct.CURRENT+4])[0]
            print(f"  HP: {hp_cur}/{hp_max}")

    pos_addr = comps.get("Positioned")
    if pos_addr:
        reaction = mem.read_uint(pos_addr + off.Positioned.REACTION, 1)
        if reaction is not None:
            friendly = (reaction & 0x7F) == 1
            print(f"  Reaction: 0x{reaction:02X} ({'friendly' if friendly else 'hostile'})")


def cmd_find_entities(mem: GameMemory):
    """Поиск AwakeEntities в AreaInstance."""
    print("=== Find AwakeEntities ===\n")

    reader = EntityReader(mem)
    area_inst = _find_area_instance(mem, reader)
    if not area_inst:
        print("ERROR: Cannot find AreaInstance")
        return

    print(f"AreaInstance: 0x{area_inst:X}")
    print(f"Expected AwakeEntities offset: 0x{off.AreaInstance.AWAKE_ENTITIES:X}")

    # Scan AreaInstance for std::map candidates
    print(f"\nScanning AreaInstance+0x500..0x900 for std::map (Head ptr + Size)...")

    for offset in range(0x500, 0x900, 8):
        head = mem.read_pointer(area_inst + offset)
        if not head or head < 0x10000:
            continue
        size = mem.read_int(area_inst + offset + 8)
        if not size or size <= 0 or size > 10000:
            continue

        # Check if this looks like a valid std::map
        parent = mem.read_pointer(head + 0x08)  # parent of sentinel
        if parent and parent >= 0x10000 and parent != head:
            print(f"  +0x{offset:03X}: Head=0x{head:X}, Size={size}, Parent=0x{parent:X} ✓ CANDIDATE")


def cmd_dump(mem: GameMemory, addr: int, size: int):
    """Hex dump памяти."""
    print(f"=== Memory Dump @ 0x{addr:X} ({size} bytes) ===\n")

    data = mem.read_bytes(addr, size)
    if not data:
        print("ERROR: Cannot read memory")
        return

    for i in range(0, len(data), 16):
        hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i+16])
        print(f"  {addr+i:08X}  {hex_part:<48s}  {ascii_part}")


def _find_area_instance(mem: GameMemory, reader: EntityReader) -> int:
    """Найти AreaInstance через AOB scan."""
    pattern_info = off.AOB_PATTERNS.get("game_states")
    if not pattern_info:
        return 0

    pattern, wildcards, disp_off, instr_len, desc = pattern_info
    matches = mem.aob_scan(pattern, wildcards)

    for match_addr in matches:
        next_instr = match_addr + instr_len
        disp_data = mem.read_bytes(match_addr + disp_off, 4)
        if not disp_data:
            continue
        displacement = struct.unpack("<i", disp_data)[0]
        slot_addr = next_instr + displacement
        game_state = mem.read_pointer(slot_addr)
        if not game_state:
            continue
        area_inst = reader.find_area_instance(game_state)
        if area_inst:
            return area_inst
    return 0


def main():
    if not mem.connect():
        print("ERROR: Cannot connect to PoE2. Make sure the game is running.")
        return 1

    reader = EntityReader(mem)

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python research.py --chain")
        print("  python research.py --hp <value>")
        print("  python research.py --entity <hex_address>")
        print("  python research.py --find-entities")
        print("  python research.py --dump <hex_address> <size>")
        return 0

    cmd = sys.argv[1]

    if cmd == "--chain":
        return 0 if cmd_chain(mem, reader) else 1
    elif cmd == "--hp" and len(sys.argv) >= 3:
        cmd_hp(mem, int(sys.argv[2]))
    elif cmd == "--entity" and len(sys.argv) >= 3:
        cmd_entity(mem, int(sys.argv[2], 16))
    elif cmd == "--find-entities":
        cmd_find_entities(mem)
    elif cmd == "--dump" and len(sys.argv) >= 4:
        cmd_dump(mem, int(sys.argv[2], 16), int(sys.argv[3]))
    else:
        print(f"Unknown command: {cmd}")
        return 1

    return 0


if __name__ == "__main__":
    mem = GameMemory()
    try:
        sys.exit(main())
    finally:
        mem.disconnect()
