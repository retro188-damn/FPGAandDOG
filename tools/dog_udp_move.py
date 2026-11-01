"""First motion client for the Lite3 Pro motion host (PC -> dog, direct UDP).

Sequence (deliberately conservative):
    heartbeat -> autonomous mode -> small forward velocity -> zero -> manual mode

Frame format (from the vendor comms manual):
    struct CommandHead { uint32 code; uint32 parameters_size; uint32 type; }
    packet sent = sizeof(head) + parameters_size
    - simple command (type=0): parameters_size slot carries the command value
    - complex command (type=1): parameters_size is the payload byte count,
      payload follows the 12-byte header, doubles are little-endian IEEE754

Usage:
    python dog_udp_move.py --vx 0.15 --duration 1.5 --yes
    python dog_udp_move.py --dry-run

Kept Python 3.6 compatible so the same file can run on the perception board.
"""
import argparse
import logging
import os
import struct
import threading
import time
import socket
from pathlib import Path

DOG_ADDR = ("192.168.1.120", 43893)

HEARTBEAT = 0x21040001
MODE_AUTONOMOUS = 0x21010C03
MODE_MANUAL = 0x21010C02
VEL_FORWARD = 0x0140
VEL_TURN = 0x0141
VEL_SIDE = 0x0145

LOG_DIR = Path(os.environ.get("DOG_LOG_DIR", "logs"))


def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / time.strftime("dog_move_%Y%m%d.log")
    logger = logging.getLogger("dog_move")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s.%(msecs)03d] [%(levelname)s] [dog_move] %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")
    for handler in (logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


class MotionClient:
    """Minimal UDP motion client with a background heartbeat thread."""

    def __init__(self, logger: logging.Logger, heartbeat_hz: float = 10.0) -> None:
        self.log = logger
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self._hb_interval = 1.0 / heartbeat_hz
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)

    # --- framing -----------------------------------------------------------
    @staticmethod
    def simple_frame(code: int, value: int = 0) -> bytes:
        """type=0 command: the second head slot carries the value."""
        return struct.pack("<3I", code, value, 0)

    @staticmethod
    def complex_frame(code: int, payload: bytes) -> bytes:
        """type=1 command: parameters_size is the payload length in bytes."""
        return struct.pack("<3I", code, len(payload), 1) + payload

    @classmethod
    def velocity_frame(cls, code: int, value: float) -> bytes:
        return cls.complex_frame(code, struct.pack("<d", value))

    # --- transport ---------------------------------------------------------
    def send(self, packet: bytes) -> None:
        self.sock.sendto(packet, DOG_ADDR)
        if self.log.isEnabledFor(logging.DEBUG):
            # bytes.hex(sep) needs 3.8+, boards run 3.6
            self.log.debug("tx %d bytes %s", len(packet), " ".join("%02x" % b for b in packet))

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self.send(self.simple_frame(HEARTBEAT))
            time.sleep(self._hb_interval)

    def start(self) -> None:
        self._thread.start()
        self.log.info("heartbeat thread started at %.1f Hz", 1.0 / self._hb_interval)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self.sock.close()
        self.log.info("socket closed")

    # --- motion primitives -------------------------------------------------
    def set_mode(self, code: int) -> None:
        self.send(self.simple_frame(code))
        self.log.info("mode command 0x%08X", code)
        time.sleep(0.3)

    def drive(self, vx: float, duration: float, period: float = 0.02) -> None:
        """Stream one velocity setpoint for `duration` seconds at `period` cadence."""
        frame = self.velocity_frame(VEL_FORWARD, vx)
        deadline = time.perf_counter() + duration
        count = 0
        while time.perf_counter() < deadline:
            self.send(frame)
            count += 1
            time.sleep(period)
        self.log.info("drove vx=%.3f m/s for %.2fs (%d frames)", vx, duration, count)

    def stop(self, hold: float = 0.6) -> None:
        frame = self.velocity_frame(VEL_FORWARD, 0.0)
        deadline = time.perf_counter() + hold
        while time.perf_counter() < deadline:
            self.send(frame)
            time.sleep(0.02)
        self.log.info("zero velocity held for %.2fs", hold)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vx", type=float, default=0.15, help="forward velocity m/s")
    ap.add_argument("--duration", type=float, default=1.5, help="seconds of motion")
    ap.add_argument("--yes", action="store_true", help="required to actually move the robot")
    ap.add_argument("--dry-run", action="store_true", help="print frames only")
    args = ap.parse_args()

    log = setup_logger()
    client = MotionClient(log)

    if args.dry_run:
        log.info("dry run, heartbeat frame=%s", client.simple_frame(HEARTBEAT).hex(" "))
        log.info("dry run, mode frame=%s", client.simple_frame(MODE_AUTONOMOUS).hex(" "))
        log.info("dry run, vx=%.2f frame=%s", args.vx,
                 client.velocity_frame(VEL_FORWARD, args.vx).hex(" "))
        return 0
    if not args.yes:
        log.error("refusing to move: pass --yes after clearing the area around the robot")
        return 2

    expected = args.vx * args.duration
    log.info("plan: vx=%.3f m/s, %.2fs, expected travel ~%.2f m", args.vx, args.duration, expected)
    try:
        client.start()
        time.sleep(0.3)
        client.set_mode(MODE_AUTONOMOUS)
        time.sleep(0.3)
        client.drive(args.vx, args.duration)
        client.stop()
        client.set_mode(MODE_MANUAL)
        time.sleep(0.3)
    finally:
        client.close()
    log.info("motion test finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
