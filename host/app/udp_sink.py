"""UDP sink: decode Lite3 command frames and report what actually arrived.

Used to test the command path *without the dog*: point `gate_bridge --dest`
at this sink and compare the observed frame rates/values with what the PL
gate was producing.

    python -m host.app.udp_sink --listen 43901 --seconds 12

Frame layout (little endian):
    head   = code:int32 | parameters_size:int32 | type:int32   (12 bytes)
    payload= parameters_size bytes (type=1 only, type=0 uses the size field
             as an inline int32 value)

Known codes decoded here are the ones the bridge emits.
"""
from __future__ import annotations

import argparse
import collections
import logging
import socket
import struct
import sys
import time
from typing import Dict, List, Optional, Tuple

LOG = logging.getLogger("udp_sink")

CODE_NAMES = {
    0x21040001: "heartbeat",
    0x0140: "vel_forward",
    0x0141: "vel_turn",
    0x0145: "vel_side",
    0x0901: "robot_state",
    0x0902: "joint_state_1",
    0x0903: "joint_state_2",
}

HEAD = struct.Struct("<3I")


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] [udp_sink] %(message)s")
    )
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def decode(packet: bytes) -> Tuple[int, int, int, bytes]:
    """Return (code, size_field, type_field, payload)."""
    code, size, ftype = HEAD.unpack_from(packet, 0)
    payload = packet[HEAD.size : HEAD.size + min(size, 256)]
    return code, size, ftype, payload


class Sink:
    def __init__(self, port: int, bind: str = "0.0.0.0") -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:  # keep bursts from being dropped while we are printing
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        except OSError:
            pass
        self._sock.bind((bind, port))
        self._sock.settimeout(0.2)
        self.counts: Dict[int, int] = collections.Counter()  # type: ignore[assignment]
        self.last_value: Dict[int, float] = {}
        self.last_nonzero: Dict[int, float] = {}
        self.bad = 0
        self.first_seen = 0.0
        self.last_seen = 0.0
        self.samples: List[Tuple[float, int, float]] = []

    def run(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        LOG.info("listening for %.1fs", seconds)
        while time.monotonic() < end:
            try:
                packet, peer = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                LOG.warning("recvfrom failed: %s", exc)
                continue
            now = time.monotonic()
            if not self.first_seen:
                self.first_seen = now
            self.last_seen = now
            if len(packet) < HEAD.size:
                self.bad += 1
                continue
            code, size, ftype, payload = decode(packet)
            if len(packet) != HEAD.size + (size if ftype == 1 else 0):
                self.bad += 1
                continue
            self.counts[code] += 1
            value = 0.0
            if size == 8 and ftype == 1 and len(payload) == 8:
                value = struct.unpack("<d", payload)[0]
            self.last_value[code] = value
            if value != 0.0:
                self.last_nonzero[code] = value
            if len(self.samples) < 200000:
                self.samples.append((now, code, value))
        self._sock.close()

    def report(self) -> int:
        # A short burst would otherwise report nonsense rates: floor the window.
        span = max(self.last_seen - self.first_seen, 0.1)
        LOG.info("window %.2fs, %d packets, %d malformed", span, sum(self.counts.values()), self.bad)
        for code, count in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            name = CODE_NAMES.get(code, "unknown")
            LOG.info(
                "  0x%08X %-14s %8d  %7.1f/s  last=%.4f last_nonzero=%.4f",
                code, name, count, count / span,
                self.last_value.get(code, 0.0), self.last_nonzero.get(code, 0.0),
            )
        return 0 if sum(self.counts.values()) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m host.app.udp_sink")
    parser.add_argument("--listen", type=int, default=43901)
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    setup_logging()
    sink = Sink(args.listen)
    sink.run(args.seconds)
    return sink.report()


if __name__ == "__main__":
    sys.exit(main())
