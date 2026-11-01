"""Command bridge: PL safety gate -> Lite3 UDP frames.

Architecture (this file is the *carrier*, not the decision maker):

    decision layer  --writes CMD_VX/VY/WZ + CMD_ARM-->  PL safety gate
    PL safety gate  --OUT_VX/VY/WZ (already clamped/zeroed)-->  this bridge
    this bridge     --encodes Lite3 frames-->  UDP -> dog (or a test sink)

The safety decision stays in hardware: if the arming writes stop, the PL
drops OUT_* to zero within TIMEOUT_MS and this bridge forwards zeros.  The
bridge never invents a velocity of its own.

Failure policy (deliberate, fail-safe):
  * PL unreachable / overlay not loaded -> send NOTHING (the dog then relies
    on its own 250 ms command timeout).
  * gate closed or faulted -> send one explicit zero-velocity burst, then
    keep sending zeros while the gate is closed.

Runs on the ZYNQ PS (PYNQ venv) or on a dev PC with `--dry-run` / sim.

Examples
--------
    # No hardware: print the exact bytes that would go on the wire.
    python -m host.app.gate_bridge --dry-run --command 100,0,0

    # No dog: send to a sink on this PC, started in another shell with
    #   python -m host.app.udp_sink --listen 43901 --seconds 12
    python -m host.app.gate_bridge --source pynq --dest 127.0.0.1:43901

    # Towards the dog (requires an explicit --approve, see conventions.md §1)
    python -m host.app.gate_bridge --source pynq --dest 192.168.1.120:43893 --approve
"""
from __future__ import annotations

import argparse
import logging
import socket
import struct
import sys
import time
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------
# Lite3 wire format (little endian): head = {code, parameters_size, type}
# --------------------------------------------------------------------------
HEARTBEAT = 0x21040001
VEL_FORWARD = 0x0140  # double, m/s
VEL_SIDE = 0x0145     # double, m/s
VEL_TURN = 0x0141     # double, rad/s

DOG_IP = "192.168.1.120"
DOG_PORT = 43893

# PL register map (see fpga/src/safety_gate.v and docs/runbook_fpga.md)
REG_BASE = 0x43C00000
REG_VER = 0x00
REG_CTRL = 0x04
REG_TIMEOUT_MS = 0x08
REG_OUT_VX = 0x1C
REG_OUT_VY = 0x20
REG_OUT_WZ = 0x24
REG_STATUS = 0x28
REG_STATS = 0x2C
REG_HEARTBEAT = 0x30
REG_RELOADS = 0x34
REG_CMD_VX = 0x0C
REG_CMD_VY = 0x10
REG_CMD_WZ = 0x14
REG_ARM = 0x18

CTRL_ENABLE = 1 << 0
CTRL_ESTOP = 1 << 1
CTRL_CLEAR_FAULTS = 1 << 2
ST_GATE_OPEN = 1 << 0
ST_ARMED = 1 << 1
ST_ESTOP_N = 1 << 2
ST_ENABLE = 1 << 3
ST_FAULT_TIMEOUT = 1 << 4
ST_FAULT_ESTOP = 1 << 5

LOG = logging.getLogger("gate_bridge")


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] [gate_bridge] %(message)s")
    )
    LOG.addHandler(handler)
    LOG.setLevel(level)


# --------------------------------------------------------------------------
# Frame encoding
# --------------------------------------------------------------------------
def simple_frame(code: int, value: int = 0) -> bytes:
    """12-byte frame: {code, value, type=0}."""
    return struct.pack("<3I", code, value & 0xFFFFFFFF, 0)


def complex_frame(code: int, payload: bytes) -> bytes:
    """12-byte head {code, len(payload), type=1} followed by the payload."""
    return struct.pack("<3I", code, len(payload), 1) + payload


def velocity_frame(code: int, value: float) -> bytes:
    """20-byte velocity frame; the payload is one little-endian double."""
    return complex_frame(code, struct.pack("<d", float(value)))


def frames_for_command(vx: float, vy: float, wz: float) -> List[bytes]:
    """Three velocity frames (forward / side / turn), in a stable order."""
    return [
        velocity_frame(VEL_FORWARD, vx),
        velocity_frame(VEL_SIDE, vy),
        velocity_frame(VEL_TURN, wz),
    ]


def _to_int32(value: int) -> int:
    """Reinterpret a 32-bit MMIO read as a signed integer.

    The PL registers are signed (mm/s, mrad/s); MMIO returns them unsigned,
    so a negative command would otherwise look like ~4.29e9 mm/s.
    """
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


# --------------------------------------------------------------------------
# PL register access
# --------------------------------------------------------------------------
class PlGate:
    """Thin MMIO wrapper. The import is deferred so --dry-run needs no PYNQ."""

    def __init__(self, base: int = REG_BASE) -> None:
        from pynq import MMIO  # type: ignore[import-not-found]

        self._mmio = MMIO(base, 0x1000)
        # A previous session may have left the sticky timeout fault latched:
        # clear it explicitly, then enable and leave the gate closed until
        # the first arming write opens it again.
        self.write(REG_CTRL, CTRL_ENABLE | CTRL_CLEAR_FAULTS)
        self.write(REG_CTRL, CTRL_ENABLE)
        LOG.info(
            "PL gate ready: status=0x%02x out=(%d,%d,%d)",
            self.status(), _to_int32(self.read(REG_OUT_VX)),
            _to_int32(self.read(REG_OUT_VY)), _to_int32(self.read(REG_OUT_WZ)),
        )

    def write(self, offset: int, value: int) -> None:
        self._mmio.write(offset, value & 0xFFFFFFFF)

    def read(self, offset: int) -> int:
        return int(self._mmio.read(offset))

    def set_command(self, vx_mm_s: int, vy_mm_s: int, wz_mrad_s: int) -> None:
        self.write(REG_CMD_VX, vx_mm_s)
        self.write(REG_CMD_VY, vy_mm_s)
        self.write(REG_CMD_WZ, wz_mrad_s)

    def arm(self) -> None:
        self.write(REG_ARM, 1)

    def status(self) -> int:
        return self.read(REG_STATUS)

    def outputs(self) -> Tuple[float, float, float]:
        """Gate outputs in SI units (m/s, m/s, rad/s)."""
        return (
            _to_int32(self.read(REG_OUT_VX)) / 1000.0,
            _to_int32(self.read(REG_OUT_VY)) / 1000.0,
            _to_int32(self.read(REG_OUT_WZ)) / 1000.0,
        )


class SimGate:
    """Software stand-in for --source sim: mirrors the RTL behaviour for tests."""

    def __init__(self, timeout_ms: int = 200) -> None:
        self._cmd = (0, 0, 0)
        self._last_arm = 0.0
        self._timeout_s = timeout_ms / 1000.0
        self._enabled = True

    def set_command(self, vx_mm_s: int, vy_mm_s: int, wz_mrad_s: int) -> None:
        self._cmd = (vx_mm_s, vy_mm_s, wz_mrad_s)

    def arm(self) -> None:
        self._last_arm = time.monotonic()

    def status(self) -> int:
        st = 1 << 3  # enable
        if time.monotonic() - self._last_arm <= self._timeout_s:
            return st | ST_GATE_OPEN | (1 << 1)
        return st | ST_FAULT_TIMEOUT

    def outputs(self) -> Tuple[float, float, float]:
        if not (self.status() & ST_GATE_OPEN):
            return 0.0, 0.0, 0.0
        vx, vy, wz = self._cmd
        return vx / 1000.0, vy / 1000.0, wz / 1000.0


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------
class Bridge:
    """Push gate outputs onto the wire at a fixed rate."""

    def __init__(
        self,
        gate,
        dest: Tuple[str, int],
        period: float = 0.02,
        heartbeat_period: float = 0.2,
        dry_run: bool = False,
        arm: bool = True,
    ) -> None:
        self._gate = gate
        self._dest = dest
        self._period = period
        self._heartbeat_period = heartbeat_period
        self._dry_run = dry_run
        self._arm = arm
        self._sock: Optional[socket.socket] = None
        if not dry_run:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)

    def _emit(self, frames: List[bytes]) -> None:
        for frame in frames:
            if self._dry_run:
                LOG.info("dry-run frame %s", frame.hex(" "))
            else:
                assert self._sock is not None
                try:
                    self._sock.sendto(frame, self._dest)
                except OSError as exc:  # network hiccup must not stop the loop
                    LOG.warning("sendto failed: %s", exc)

    def run(
        self,
        vx_mm_s: int,
        vy_mm_s: int,
        wz_mrad_s: int,
        seconds: float,
        zero_hold: float = 0.5,
    ) -> None:
        """Feed the gate for `seconds`, then stop feeding and emit zeros."""
        self._gate.set_command(vx_mm_s, vy_mm_s, wz_mrad_s)
        deadline = time.monotonic() + seconds
        next_beat = 0.0
        samples = 0
        LOG.info(
            "bridge start dest=%s:%d cmd=(%d,%d,%d) for %.2fs",
            self._dest[0], self._dest[1], vx_mm_s, vy_mm_s, wz_mrad_s, seconds,
        )
        while time.monotonic() < deadline:
            loop_start = time.monotonic()
            if self._arm:  # keep the hardware watchdog fed
                self._gate.arm()
            st = self._gate.status()
            if st & ST_GATE_OPEN:
                vx, vy, wz = self._gate.outputs()
            else:
                vx = vy = wz = 0.0
            self._emit(frames_for_command(vx, vy, wz))
            samples += 1
            if loop_start >= next_beat:
                self._emit([simple_frame(HEARTBEAT)])
                next_beat = loop_start + self._heartbeat_period
            sleep = self._period - (time.monotonic() - loop_start)
            if sleep > 0:
                time.sleep(sleep)

        LOG.info("bridge stop after %d samples; holding zeros %.2fs", samples, zero_hold)
        stop_deadline = time.monotonic() + zero_hold
        while time.monotonic() < stop_deadline:
            # Keep arming while holding zeros: a spurious watchdog fault here
            # would only latch confusion into the next session.  Once this
            # process exits, arming stops and the dog falls back to its own
            # 250 ms command timeout.
            if self._arm:
                self._gate.arm()
            self._emit(frames_for_command(0.0, 0.0, 0.0))
            time.sleep(self._period)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None


def parse_dest(text: str) -> Tuple[str, int]:
    host, _, port = text.partition(":")
    return host, int(port or DOG_PORT)


def parse_command(text: str) -> Tuple[int, int, int]:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("expected VX,VY,WZ in mm/s and mrad/s")
    return int(parts[0]), int(parts[1]), int(parts[2])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m host.app.gate_bridge",
        description="Forward PL safety-gate outputs as Lite3 UDP frames.",
    )
    parser.add_argument(
        "--source", choices=("pynq", "sim"), default="sim",
        help="pynq: read the real overlay; sim: software stand-in (default)",
    )
    parser.add_argument("--dest", default="127.0.0.1:43901", help="HOST:PORT to send to")
    parser.add_argument("--command", default="100,0,0", type=parse_command, help="VX,VY,WZ")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true", help="log frames instead of sending")
    parser.add_argument(
        "--approve", action="store_true",
        help="required before sending to the real dog (conventions.md §1)",
    )
    parser.add_argument(
        "--no-arm", dest="arm", action="store_false",
        help="pure carrier mode: never write CMD_ARM (the decision layer owns arming)",
    )
    args = parser.parse_args(argv)
    setup_logging()

    dest = parse_dest(args.dest)
    if dest[0] == DOG_IP and not args.approve:
        LOG.error(
            "%s is the dog: refusing to send without --approve "
            "(report the action plan first, see docs/conventions.md §1)",
            DOG_IP,
        )
        return 2

    gate = SimGate() if args.source == "sim" else PlGate()
    bridge = Bridge(gate, dest, dry_run=args.dry_run, arm=args.arm)
    try:
        bridge.run(*args.command, seconds=args.seconds)
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
