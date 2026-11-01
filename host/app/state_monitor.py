"""Live monitor for the Lite3 state stream (runs on PC or on the ZYNQ board).

Listens for the raw 0x09xx frames that `host/app/state_relay.py` forwards, and
prints a compact line twice a second:

    basic=6 | rpy=(-0.15,-0.17,2.82) | pos=(0.264,0.006,0.313) ...
    vel_body=(-0.02,-0.01,-0.01) | batt=53% | us=(1.077,4.500)

Python 3.6 compatible (the perception board ships 3.6.9), standard library only,
because the same file is meant to run on the ZYNQ-7020 board later.

Usage:
    python3 state_monitor.py --port 43900 --json-log logs/state_20261025.jsonl
"""
import argparse
import json
import logging
import socket
import struct
import sys
import time

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

# reverse-engineered: 6 is what a standing/idle robot reports on this firmware
BASIC_STATE_HINT = {0: "lying", 1: "sit", 2: "stand", 3: "damping", 6: "stand/idle"}


def decode_robot_state(body):
    out = {}
    off = 0
    for name, fmt in ROBOT_STATE_FIELDS:
        size = struct.calcsize(fmt)
        if fmt.endswith("x"):
            off += size
            continue
        values = struct.unpack_from(fmt, body, off)
        out[name] = values[0] if len(values) == 1 else values
        off += size
    return out


def fmt_state(state):
    rpy = state["rpy[3]"]
    pos = state["pos_world[3]"]
    vel = state["vel_body[3]"]
    us = state["ultrasound[2]"]
    return ("basic=%s(%s) rpy=(%.2f,%.2f,%.2f) pos=(%.3f,%.3f,%.3f) "
            "vel_body=(%.2f,%.2f,%.2f) batt=%.0f%% us=(%.2f,%.2f) err=%s mot=%s%s"
            % (state["robot_basic_state"],
               BASIC_STATE_HINT.get(int(state["robot_basic_state"]), "?"),
               rpy[0], rpy[1], rpy[2], pos[0], pos[1], pos[2],
               vel[0], vel[1], vel[2], state["battery_level"], us[0], us[1],
               state["error_state"], state["robot_motion_state"],
               " charging" if state["is_charging"] else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=43900)
    ap.add_argument("--json-log", default="")
    ap.add_argument("--print-every", type=float, default=0.5)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] [%(levelname)s] [state_monitor] %(message)s")
    log = logging.getLogger("state_monitor")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # The dog streams ~100 Hz state plus a 600+ Hz 368-byte frame; a small
    # receive buffer silently drops frames, which shows up as a lower rate.
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.2)
    log.info("listening on udp/%d", args.port)

    jf = open(args.json_log, "w") if args.json_log else None
    latest = {}
    counts = {}
    frames = 0
    last_print = 0.0
    try:
        while True:
            try:
                data, peer = sock.recvfrom(4096)
            except socket.timeout:
                continue
            frames += 1
            if len(data) < 12:
                continue
            code, size, _typ = struct.unpack_from("<3I", data)
            body = data[12:12 + size]
            counts[code] = counts.get(code, 0) + 1
            if code == 0x0901 and len(body) >= 200:
                latest["state"] = decode_robot_state(body)
                latest["peer"] = peer
                if jf is not None:
                    rec = {"t": time.time()}
                    rec.update({k: v for k, v in latest["state"].items() if not k.startswith("_")})
                    jf.write(json.dumps(rec, default=list) + "\n")
                    jf.flush()
            elif code == 0x0905:
                latest["handle"] = struct.unpack("<6d", body)
            now = time.time()
            if now - last_print >= args.print_every and "state" in latest:
                last_print = now
                line = "frames=%d " % frames + fmt_state(latest["state"])
                if "handle" in latest and any(abs(v) > 1e-9 for v in latest["handle"]):
                    line += " handle=%s" % ([round(v, 3) for v in latest["handle"]],)
                print(line)
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("")
        log.info("frames=%d per-code=%s", frames,
                 {("0x%08X" % c): n for c, n in sorted(counts.items())})
    finally:
        if jf is not None:
            jf.close()


if __name__ == "__main__":
    main()
