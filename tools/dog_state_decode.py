"""Decode Lite3 motion-host state frames (the 0x09xx family).

Wire format (verified against a live capture on 192.168.1.103:43897):
    struct CommandHead { uint32 code; uint32 parameters_size; uint32 type; }
    packet = 12 + parameters_size bytes, doubles little-endian.

Frames seen on this robot:
    0x00000901  200 B  RobotState   (posture, rpy, odom, battery, ultrasound)  50 Hz
    0x00000902   96 B  12 x double  (joint positions)                          50 Hz
    0x00000903   96 B  12 x double  (joint velocities)                         50 Hz
    0x00000904    4 B  int32        (unidentified status word)                 50 Hz
    0x00000905   48 B  6 x double   (handle/joystick: 2 axes + goal velocity)  50 Hz
    0x00000906  368 B  unidentified (46 x 8 bytes), 100 Hz
    0x00010901   40 B  unidentified (5 x 8 bytes), 50 Hz

RobotState is decoded with `#pragma pack(4)` exactly as the vendor's qnx2ros.cpp
declares it; the field offsets below were checked against that struct.

Usage:
    python tools/dog_state_decode.py _scratch/st.pcap
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROBOT_STATE_FIELDS = [
    ("robot_basic_state", "<i"),
    ("robot_gait_state", "<i"),
    ("rpy[3]", "<3d"),
    ("rpy_vel[3]", "<3d"),
    ("xyz_acc[3]", "<3d"),
    ("pos_world[3]", "<3d"),
    ("vel_world[3]", "<3d"),
    ("vel_body[3]", "<3d"),
    ("touch_down_and_stair_trot", "<I"),
    ("is_charging", "<?"),
    ("_pad1", "<3x"),
    ("error_state", "<I"),
    ("robot_motion_state", "<i"),
    ("battery_level", "<d"),
    ("task_state", "<i"),
    ("is_robot_need_move", "<?"),
    ("zero_position_flag", "<?"),
    ("_pad2", "<2x"),
    ("ultrasound[2]", "<2d"),
]

BASIC_STATE_HINT = {
    0: "lying/idle", 1: "standing", 2: "walking", 3: "damping",
}


def read_pcap(path: Path) -> list[bytes]:
    """Return UDP payloads parsed from a libpcap file (ethernet, IPv4, UDP)."""
    data = path.read_bytes()
    little = data[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
    endian = "<" if little else ">"
    off, payloads = 24, []
    while off + 16 <= len(data):
        _ts, _tus, incl, _orig = struct.unpack(endian + "4I", data[off:off + 16])
        frame = data[off + 16: off + 16 + incl]
        off += 16 + incl
        if len(frame) < 34:
            continue
        ip = frame[14:]
        ihl = (ip[0] & 0x0F) * 4
        udp = ip[ihl:]
        if ip[9] != 17 or len(udp) < 8:
            continue
        spo, dpo, length = struct.unpack(">3H", udp[:6])
        payloads.append(udp[8:length])
    return payloads


def decode_robot_state(payload: bytes) -> dict[str, object]:
    """Decode one 0x0901 payload (200 bytes after the 12-byte head)."""
    out: dict[str, object] = {}
    off = 0
    for name, fmt in ROBOT_STATE_FIELDS:
        size = struct.calcsize(fmt)
        if fmt.endswith("x"):  # explicit padding, no value
            off += size
            continue
        values = struct.unpack_from(fmt, payload, off)
        out[name] = values[0] if len(values) == 1 else values
        off += size
    if off != len(payload):
        out["_layout_mismatch"] = f"{off} != {len(payload)}"
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    payloads = read_pcap(Path(sys.argv[1]))
    seen: dict[int, int] = {}
    for payload in payloads:
        if len(payload) < 12:
            continue
        code, size, typ = struct.unpack_from("<3I", payload)
        seen[code] = seen.get(code, 0) + 1
        body = payload[12:12 + size]
        if code == 0x00000901:
            state = decode_robot_state(body)
            print("0x0901:", {
                "basic": state["robot_basic_state"],
                "hint": BASIC_STATE_HINT.get(int(state["robot_basic_state"]), "?"),  # type: ignore[arg-type]
                "gait": state["robot_gait_state"],
                "rpy_deg": [round(v, 2) for v in state["rpy[3]"]],
                "pos": [round(v, 4) for v in state["pos_world[3]"]],
                "vel_body": [round(v, 3) for v in state["vel_body[3]"]],
                "battery": round(state["battery_level"], 1),
                "ultrasound": [round(v, 3) for v in state["ultrasound[2]"]],
                "error_state": state["error_state"],
                "motion_state": state["robot_motion_state"],
                "charging": state["is_charging"],
            })
        elif code == 0x00000905:
            axes = struct.unpack("<6d", body)
            nonzero = any(abs(v) > 1e-9 for v in axes)
            print("0x0905 handle:", [round(v, 4) for v in axes], "active" if nonzero else "idle")
    print("frame counts:", {f"0x{c:08X}": n for c, n in sorted(seen.items())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
