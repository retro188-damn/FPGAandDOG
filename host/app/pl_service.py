"""Resident PL safety-gate bridge (runs as a systemd service on the ZYNQ PS).

One long-lived process owns the overlay and turns velocity commands from the
notebook into gated Lite3 frames:

    notebook --UDP 43902--> pl_service --MMIO--> safety_gate (PL) --UDP--> dog

    dog state is *not* read here; the state tee on 192.168.1.103 handles that.

Why a service instead of the one-shot ``gate_bridge`` CLI: loading the overlay,
importing pynq and authenticating sudo costs 4-5 s per invocation, which is fine
for a bench test and fatal for a voice-driven demo.

Safety rules (all deliberate, fail-safe):

  * the gate starts CLOSED; it opens only after a fresh velocity command
  * a command older than ``--deadman-ms`` closes the gate *and* sends zeros
  * ``{"cmd": "stop"}`` closes the gate immediately (CTRL = 0)
  * SIGTERM/SIGINT close the gate before the process exits
  * a PL watchdog fault is reported but never cleared implicitly except on the
    next arming write

What is sent on the wire, every period, regardless of gate state:
    3 velocity frames (forward / side / turn) built from the gate OUTPUTS,
    plus the Lite3 heartbeat at 5 Hz.  When the gate is closed the outputs are
    zero, so the dog receives an explicit "stop" stream rather than silence.

Timing metrics (p50 / p99 / overruns of the loop period) are measured here and
reported to the notebook on UDP 43903; they are the numbers the competition
report needs for the real-time argument.

Examples
--------
    # No hardware at all (sim gate, frames go to a loopback sink):
    python -m host.app.pl_service --sim --dest 127.0.0.1:43999 --duration 5

    # On the board, one-shot smoke test before installing the unit:
    sudo /usr/local/share/pynq-venv/bin/python3 pl_service.py --duration 10
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import struct
import sys
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

# Works both as a package module (dev PC, pytest) and as a flat script that
# sits next to gate_bridge.py in /home/xilinx/safety_gate/.
try:  # pragma: no cover - import shim
    from host.app.gate_bridge import (
        CTRL_CLEAR_FAULTS,
        CTRL_ENABLE,
        CTRL_ESTOP,
        HEARTBEAT,
        REG_CTRL,
        REG_HEARTBEAT,
        REG_STATS,
        REG_STATUS,
        REG_VER,
        ST_ARMED,
        ST_ENABLE,
        ST_ARMED,
        ST_ENABLE,
        ST_FAULT_ESTOP,
        ST_FAULT_TIMEOUT,
        ST_GATE_OPEN,
        frames_for_command,
        simple_frame,
    )
except ImportError:  # pragma: no cover - board layout
    from gate_bridge import (  # type: ignore[no-redef]
        CTRL_CLEAR_FAULTS,
        CTRL_ENABLE,
        CTRL_ESTOP,
        HEARTBEAT,
        REG_CTRL,
        REG_HEARTBEAT,
        REG_STATS,
        REG_STATUS,
        REG_VER,
        ST_ARMED,
        ST_ENABLE,
        ST_ARMED,
        ST_ENABLE,
        ST_FAULT_ESTOP,
        ST_FAULT_TIMEOUT,
        ST_GATE_OPEN,
        frames_for_command,
        simple_frame,
    )

LOG = logging.getLogger("pl_service")

CLK_HZ = 27_777_778  # measured on this board, see safety_gate.v header
CLK_TOLERANCE = 0.05



# --------------------------------------------------------------------------
# Gate backends
# --------------------------------------------------------------------------
class FakeGate:
    """Clock-injected model of the RTL, used by ``--sim`` and the unit tests.

    Mirrors the parts the service depends on: enable bit, arm/retrigger,
    watchdog expiry with a sticky timeout fault, and gated outputs.
    """

    def __init__(self, clock=time.monotonic, timeout_ms: int = 200) -> None:
        self._clock = clock
        self._timeout_s = max(timeout_ms, 1) / 1000.0
        self._ctrl = 0
        self._cmd = (0, 0, 0)
        self._armed_at: Optional[float] = None
        self._f_timeout = False
        self._f_estop = False
        self.timeouts = 0
        self.arms = 0

    # -- register level ----------------------------------------------------
    def write(self, offset: int, value: int) -> None:
        now = self._clock()
        if offset == REG_CTRL:
            self._ctrl = value & 0xFFFF
            if value & CTRL_CLEAR_FAULTS:
                self._f_timeout = False
                self._f_estop = False
            if value & CTRL_ESTOP:
                self._f_estop = True
            if not (value & CTRL_ENABLE):
                self._armed_at = None  # disabling disarms, like the RTL
        elif offset in (0x0C, 0x10, 0x14):
            vx, vy, wz = self._cmd
            v = value & 0xFFFFFFFF
            v = v - 0x100000000 if v & 0x80000000 else v
            self._cmd = (v, vy, wz) if offset == 0x0C else (
                (vx, v, wz) if offset == 0x10 else (vx, vy, v))
        elif offset == 0x18:
            if self._ctrl & CTRL_ENABLE and not self._faulted():
                self._armed_at = now
                self.arms += 1

    def _faulted(self) -> bool:
        return self._f_timeout or self._f_estop

    def _expire(self) -> None:
        if self._armed_at is not None and self._clock() - self._armed_at > self._timeout_s:
            self._armed_at = None
            self._f_timeout = True
            self.timeouts += 1

    def read(self, offset: int) -> int:
        self._expire()
        if offset == REG_STATUS:
            st = 0
            if self._ctrl & CTRL_ENABLE:
                st |= ST_ENABLE
            if self._armed_at is not None:
                st |= ST_ARMED
            if self._f_timeout:
                st |= ST_FAULT_TIMEOUT
            if self._f_estop:
                st |= ST_FAULT_ESTOP
            if self.gate_open():
                st |= ST_GATE_OPEN
            return st
        if offset == REG_STATS:
            return ((self.arms & 0xFFFF) << 16) | (self.timeouts & 0xFFFF)
        if offset == REG_VER:
            return 0x00010000
        if offset == REG_HEARTBEAT:
            return int(self._clock() * CLK_HZ) & 0xFFFFFFFF
        return 0

    # -- semantic helpers --------------------------------------------------
    def gate_open(self) -> bool:
        self._expire()
        return bool(self._ctrl & CTRL_ENABLE) and self._armed_at is not None \
            and not self._faulted()

    def set_command(self, vx_mm_s: int, vy_mm_s: int, wz_mrad_s: int) -> None:
        self.write(0x0C, vx_mm_s & 0xFFFFFFFF)
        self.write(0x10, vy_mm_s & 0xFFFFFFFF)
        self.write(0x14, wz_mrad_s & 0xFFFFFFFF)

    def arm(self) -> None:
        self.write(0x18, 1)

    def status(self) -> int:
        return self.read(REG_STATUS)

    def outputs(self) -> Tuple[float, float, float]:
        if not self.gate_open():
            return 0.0, 0.0, 0.0
        vx, vy, wz = self._cmd
        return vx / 1000.0, vy / 1000.0, wz / 1000.0

    def close(self) -> None:
        pass


class HardwareGate:
    """Loads the overlay and wraps MMIO; imports pynq lazily."""

    def __init__(self, bitfile: Optional[str] = None, base: int = 0x43C00000) -> None:
        from pynq import MMIO, Overlay  # type: ignore[import-not-found]

        if bitfile:
            LOG.info("loading overlay %s", bitfile)
            Overlay(bitfile)
        self._mmio = MMIO(base, 0x1000)
        self.write(REG_CTRL, CTRL_ENABLE | CTRL_CLEAR_FAULTS)
        self.write(REG_CTRL, CTRL_ENABLE)
        self._check_clock(self._mmio)
        LOG.info("PL gate ready: ver=0x%08x status=0x%02x",
                 self.read(REG_VER), self.status())

    def _check_clock(self, mmio) -> None:
        """Refuse to run if the PL timebase is not the one the RTL assumes."""
        hb = lambda: int(mmio.read(REG_HEARTBEAT)) & 0xFFFFFFFF  # noqa: E731
        a = hb()
        t0 = time.monotonic()
        time.sleep(0.25)
        b = hb()
        dt = time.monotonic() - t0
        freq = ((b - a) & 0xFFFFFFFF) / dt
        ratio = freq / CLK_HZ
        LOG.info("clock check: %.1f MHz (expected %.1f, ratio %.3f)",
                 freq / 1e6, CLK_HZ / 1e6, ratio)
        if abs(ratio - 1.0) > CLK_TOLERANCE:
            raise RuntimeError(
                "PL clock %.1f MHz deviates from the RTL constant %.1f MHz by "
                "more than %.0f%%; refusing to run so the safety deadline cannot "
                "silently stretch" % (freq / 1e6, CLK_HZ / 1e6, CLK_TOLERANCE * 100))

    def write(self, offset: int, value: int) -> None:
        self._mmio.write(offset, value & 0xFFFFFFFF)

    def read(self, offset: int) -> int:
        return int(self._mmio.read(offset))

    def set_command(self, vx_mm_s: int, vy_mm_s: int, wz_mrad_s: int) -> None:
        self.write(0x0C, vx_mm_s & 0xFFFFFFFF)
        self.write(0x10, vy_mm_s & 0xFFFFFFFF)
        self.write(0x14, wz_mrad_s & 0xFFFFFFFF)

    def arm(self) -> None:
        self.write(0x18, 1)

    def status(self) -> int:
        return self.read(REG_STATUS)

    def outputs(self) -> Tuple[float, float, float]:
        def s32(v: int) -> int:
            v &= 0xFFFFFFFF
            return v - 0x100000000 if v & 0x80000000 else v

        return (s32(self.read(0x1C)) / 1000.0,
                s32(self.read(0x20)) / 1000.0,
                s32(self.read(0x24)) / 1000.0)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# Loop timing statistics
# --------------------------------------------------------------------------
class LoopStats:
    """Sliding window of loop periods, reported as p50 / p99 / overruns."""

    def __init__(self, window: int = 500, period_ms: float = 20.0) -> None:
        self._d: Deque[float] = deque(maxlen=window)
        self._period_ms = period_ms
        self.overruns = 0

    def add(self, dt_s: float) -> None:
        dt_ms = dt_s * 1000.0
        self._d.append(dt_ms)
        if dt_ms > self._period_ms * 1.5:
            self.overruns += 1

    def summary(self) -> Dict[str, float]:
        if not self._d:
            return {"p50": 0.0, "p99": 0.0, "max": 0.0}
        s = sorted(self._d)
        n = len(s)
        return {
            "p50": round(s[n // 2], 2),
            "p99": round(s[min(n - 1, int(n * 0.99))], 2),
            "max": round(s[-1], 2),
        }


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------
class PlService:
    """Resident loop: commands in on UDP, gated frames out on UDP."""

    def __init__(
        self,
        gate,
        dest: Tuple[str, int],
        cmd_port: int = 43902,
        status_port: int = 43903,
        status_peer: Optional[Tuple[str, int]] = None,
        period_ms: float = 20.0,
        deadman_ms: float = 300.0,
        heartbeat_ms: float = 200.0,
        status_ms: float = 100.0,
        clock=time.monotonic,
    ) -> None:
        self._gate = gate
        self._dest = dest
        self._clock = clock
        self._period = period_ms / 1000.0
        self._deadman = deadman_ms / 1000.0
        self._heartbeat_period = heartbeat_ms / 1000.0
        self._status_period = status_ms / 1000.0

        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._cmd_sock.bind(("0.0.0.0", cmd_port))
        self._cmd_sock.setblocking(False)

        self._dog_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._status_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Two audiences: the peer configured at start-up (so an operator can
        # watch without sending commands) and whoever last sent a command.
        self._fixed_status_peer = status_peer
        self._status_peer: Optional[Tuple[str, int]] = None

        self.stats = LoopStats(period_ms=period_ms)
        self.seq = 0
        self.cmds = 0
        self.deadman_trips = 0
        self.frames = 0
        self._target = (0, 0, 0)          # last commanded (mm/s, mm/s, mrad/s)
        self._last_cmd_at: Optional[float] = None
        self._gate_disabled = True        # start safe: gate closed
        self._next_beat = 0.0
        self._next_status = 0.0
        self._running = True

    # -- command input -----------------------------------------------------
    def poll(self, now: float) -> None:
        while True:
            try:
                data, peer = self._cmd_sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:  # pragma: no cover - socket failure
                LOG.error("command socket failed: %s", exc)
                return
            if self._status_peer is None:
                self._status_peer = (peer[0], 43903)
            self._apply(data, now)

    def _apply(self, data: bytes, now: float) -> None:
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            LOG.warning("ignoring malformed command %r", data[:64])
            return

        cmd = str(msg.get("cmd", "vel"))
        if cmd == "stop":
            self.disable(now, reason="stop command")
            return
        if cmd == "clear":
            self._gate.write(REG_CTRL, CTRL_ENABLE | CTRL_CLEAR_FAULTS)
            self._gate.write(REG_CTRL, CTRL_ENABLE)
            LOG.info("sticky faults cleared on request")
            return
        if cmd != "vel":
            LOG.warning("unknown command %r", cmd)
            return

        try:
            vx = int(msg.get("vx", 0))
            vy = int(msg.get("vy", 0))
            wz = int(msg.get("wz", 0))
        except (TypeError, ValueError):
            LOG.warning("non-integer velocity in %r", msg)
            return
        self.cmds += 1
        self._last_cmd_at = now
        self._target = (vx, vy, wz)
        self.enable(now)

    # -- gate control ------------------------------------------------------
    def enable(self, now: float) -> None:
        if self._gate_disabled:
            self._gate.write(REG_CTRL, CTRL_ENABLE | CTRL_CLEAR_FAULTS)
            self._gate.write(REG_CTRL, CTRL_ENABLE)
            self._gate_disabled = False
            LOG.info("gate enabled by fresh command")

    def disable(self, now: float, reason: str) -> None:
        """Close the gate immediately (CTRL = 0) and log why, once."""
        was_open = not self._gate_disabled
        self._gate.write(REG_CTRL, 0)
        self._gate_disabled = True
        self._target = (0, 0, 0)
        self._last_cmd_at = None
        if was_open:
            LOG.warning("gate closed: %s", reason)

    # -- one loop iteration ------------------------------------------------
    def step(self, now: float) -> None:
        self.seq += 1
        self.poll(now)

        if self._last_cmd_at is not None and now - self._last_cmd_at > self._deadman:
            self.deadman_trips += 1
            self.disable(now, reason="deadman: no command for %.0f ms"
                         % (self._deadman * 1000))

        if not self._gate_disabled:
            vx, vy, wz = self._target
            self._gate.set_command(vx, vy, wz)
            self._gate.arm()

        st = self._gate.status()
        open_ = bool(st & ST_GATE_OPEN)
        out = self._gate.outputs() if open_ else (0.0, 0.0, 0.0)

        frames = frames_for_command(*out)
        self._emit(frames)
        self.frames += len(frames)

        if now >= self._next_beat:
            self._emit([simple_frame(HEARTBEAT)])
            self._next_beat = now + self._heartbeat_period

        if now >= self._next_status:
            self._send_status(now, st, out)
            self._next_status = now + self._status_period

    def _emit(self, frames) -> None:
        for frame in frames:
            try:
                self._dog_sock.sendto(frame, self._dest)
            except OSError as exc:  # pragma: no cover - network failure
                LOG.warning("sendto %s failed: %s", self._dest, exc)
                return

    def _send_status(self, now: float, st: int, out) -> None:
        peers = [p for p in (self._fixed_status_peer, self._status_peer) if p]
        if not peers:
            return
        payload = {
            "t": round(now, 3),
            "seq": self.seq,
            "armed": bool(st & ST_ARMED),
            "enabled": bool(st & ST_ENABLE),
            "gate_open": bool(st & ST_GATE_OPEN),
            "fault_timeout": bool(st & ST_FAULT_TIMEOUT),
            "fault_estop": bool(st & ST_FAULT_ESTOP),
            "out": [round(v, 4) for v in out],
            "cmd": list(self._target),
            "loop_ms": self.stats.summary(),
            "overruns": self.stats.overruns,
            "cmds": self.cmds,
            "deadman_trips": self.deadman_trips,
            "frames": self.frames,
            "timeouts": (self._gate.read(REG_STATS) & 0xFFFF) & 0xFFFF,
        }
        blob = json.dumps(payload).encode("utf-8")
        for peer in set(peers):
            try:
                self._status_sock.sendto(blob, peer)
            except OSError:  # pragma: no cover - peer gone
                pass

    # -- lifecycle ---------------------------------------------------------
    def run(self, duration: float = 0.0, on_tick=None) -> None:
        deadline = self._clock() + duration if duration > 0 else None
        next_t = self._clock()
        prev = None
        LOG.info("service loop start: dest=%s period=%.0f ms deadman=%.0f ms",
                 self._dest, self._period * 1000, self._deadman * 1000)
        try:
            while self._running:
                now = self._clock()
                if deadline is not None and now >= deadline:
                    break
                if prev is not None:
                    self.stats.add(now - prev)  # period jitter, for p50/p99
                prev = now
                self.step(now)
                if on_tick is not None:
                    on_tick(self)
                next_t += self._period
                sleep = next_t - self._clock()
                if sleep < -self._period:
                    # Fell more than one period behind: resync instead of
                    # hammering the loop with a burst of catch-up iterations.
                    next_t = self._clock()
                    sleep = 0.0
                if sleep > 0:
                    time.sleep(sleep)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._gate.write(REG_CTRL, 0)  # close the gate before leaving
            self._gate.set_command(0, 0, 0)
        except Exception as exc:  # pragma: no cover - hardware gone
            LOG.error("failed to close the gate cleanly: %s", exc)
        for s in (self._cmd_sock, self._dog_sock, self._status_sock):
            try:
                s.close()
            except OSError:
                pass
        LOG.info("service stop: frames=%d cmds=%d deadman_trips=%d overruns=%d",
                 self.frames, self.cmds, self.deadman_trips, self.stats.overruns)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_dest(text: str) -> Tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host:
        raise argparse.ArgumentTypeError("expected HOST:PORT, got %r" % text)
    return host, int(port)


def parse_status_peer(text: Optional[str], port: int) -> Optional[Tuple[str, int]]:
    if not text:
        return None
    host, _, p = text.rpartition(":")
    if host and p.isdigit():
        return host, int(p)
    return text, port


def setup_logging(path: Optional[str], verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
    handlers: list = [logging.StreamHandler(sys.stderr)]
    if path:
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="Resident PL safety-gate bridge")
    p.add_argument("--sim", action="store_true", help="fake gate, no hardware")
    p.add_argument("--dest", type=parse_dest, default=("192.168.1.120", 43893))
    p.add_argument("--cmd-port", type=int, default=43902)
    p.add_argument("--status-port", type=int, default=43903)
    p.add_argument("--status-peer", default=None,
                   help="IP[:PORT] that always receives status packets "
                        "(so an operator can watch without commanding)")
    p.add_argument("--period-ms", type=float, default=20.0)
    p.add_argument("--deadman-ms", type=float, default=300.0)
    p.add_argument("--heartbeat-ms", type=float, default=200.0)
    p.add_argument("--status-ms", type=float, default=100.0)
    p.add_argument("--overlay", default=None, help="bitfile to load on hardware")
    p.add_argument("--duration", type=float, default=0.0, help="0 = run forever")
    p.add_argument("--log", default=None)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    setup_logging(args.log, args.verbose)
    if args.sim:
        gate = FakeGate()
        if args.dest == ("192.168.1.120", 43893):
            args.dest = ("127.0.0.1", 43999)
            LOG.info("sim mode: frames go to %s:%d", *args.dest)
    else:
        gate = HardwareGate(args.overlay)

    svc = PlService(
        gate=gate,
        dest=args.dest,
        cmd_port=args.cmd_port,
        status_port=args.status_port,
        period_ms=args.period_ms,
        deadman_ms=args.deadman_ms,
        heartbeat_ms=args.heartbeat_ms,
        status_ms=args.status_ms,
        status_peer=parse_status_peer(args.status_peer, args.status_port),
    )

    def _stop(signum, _frame):
        LOG.info("signal %d: closing gate and exiting", signum)
        svc._running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    svc.run(duration=args.duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
