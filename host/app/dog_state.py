"""Lite3 state frames (0x0901) -> decoded dictionary, plus a live feed.

The dog pushes a 200-byte ``RobotStateUpload`` body every 10 ms to whoever is
configured as its status target (``network.toml`` on the motion host).  The
perception board relays those frames to the notebook on UDP 43900, so both a
one-shot decode (from a capture file) and a background listener live here.

The layout is reverse-engineered from a real capture and matches the vendor
manual only partially, so keep the field table in one place: an offset mistake
silently produces plausible numbers (see log 2026-10-26, 0x1C bug).

Python 3.6 compatible, standard library only: this file is also meant to run on
the ZYNQ-7020 board.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Any, Dict, Optional, Tuple

FRAME_ROBOT_STATE = 0x0901
HEADER_SIZE = 12
STATE_BODY_SIZE = 200

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

# Reverse-engineered: 6 is what this firmware reports while standing idle.
BASIC_STATE_HINT = {0: "lying", 1: "sit", 2: "stand", 3: "damping", 6: "stand/idle"}
STANDING_STATES = (2, 6)


def decode_robot_state(body: bytes) -> Dict[str, Any]:
    """Unpack the 200-byte state body into a flat dictionary."""
    if len(body) < STATE_BODY_SIZE:
        raise ValueError("state body too short: %d bytes" % len(body))
    out: Dict[str, Any] = {}
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


def decode_frame(packet: bytes) -> Optional[Dict[str, Any]]:
    """Decode one UDP datagram, or return None when it is not a 0x0901 frame."""
    if len(packet) < HEADER_SIZE:
        return None
    code, size = struct.unpack_from("<II", packet, 0)
    if code != FRAME_ROBOT_STATE:
        return None
    body = packet[HEADER_SIZE:HEADER_SIZE + size]
    if len(body) < STATE_BODY_SIZE:
        return None
    return decode_robot_state(body)


def position(state: Dict[str, Any]) -> Tuple[float, float, float]:
    return tuple(state["pos_world[3]"])  # type: ignore[return-value]


def travelled(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    """Straight-line distance between two states' world positions, in metres."""
    pa, pb = position(a), position(b)
    return sum((pb[i] - pa[i]) ** 2 for i in range(3)) ** 0.5


def is_standing(state: Dict[str, Any]) -> bool:
    return int(state["robot_basic_state"]) in STANDING_STATES


def fmt_state(state: Dict[str, Any]) -> str:
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


class StateFeed:
    """Background UDP listener that keeps the newest decoded state.

    ``latest()`` returns ``(state, age_seconds)``; age is ``None`` before the
    first frame, which is how the executor tells "no feedback" apart from
    "old feedback".
    """

    def __init__(self, port: int = 43900, host: str = "0.0.0.0") -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.2)
        self._lock = threading.Lock()
        self._state: Optional[Dict[str, Any]] = None
        self._stamp = 0.0
        self.frames = 0
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="state-feed")
        self._thread.daemon = True

    def start(self) -> "StateFeed":
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            state = decode_frame(data)
            if state is None:
                continue
            with self._lock:
                self._state = state
                self._stamp = time.monotonic()
                self.frames += 1

    def latest(self) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
        with self._lock:
            if self._state is None:
                return None, None
            return self._state, time.monotonic() - self._stamp

    def wait_for_state(self, timeout_s: float = 1.0) -> Dict[str, Any]:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            state, _ = self.latest()
            if state is not None:
                return state
            time.sleep(0.02)
        raise RuntimeError("no state frames on port %d after %.1f s"
                           % (self.sock.getsockname()[1], timeout_s))

    def close(self) -> None:
        self._closed.set()
        try:
            self.sock.close()
        except OSError:
            pass
