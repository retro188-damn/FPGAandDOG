"""Unit tests for the resident PL bridge (no hardware, no dog, no network).

Run from the repository root:

    python -m pytest tests/test_pl_service.py -q
"""
from __future__ import annotations

import json
import socket
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.app.gate_bridge import HEARTBEAT  # noqa: E402
from host.app.pl_service import (  # noqa: E402
    CTRL_ENABLE,
    FakeGate,
    LoopStats,
    PlService,
    REG_CTRL,
)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def rig():
    """Service + fake gate + a UDP sink standing in for the dog."""
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.settimeout(0.05)
    clk = FakeClock()
    gate = FakeGate(clock=clk)
    svc = PlService(
        gate=gate,
        dest=sink.getsockname(),
        cmd_port=free_port(),
        clock=clk,
    )
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    yield svc, gate, clk, ctl, sink
    ctl.close()
    sink.close()
    try:
        svc.shutdown()
    except Exception:
        pass


def send_cmd(ctl, svc, payload):
    ctl.sendto(json.dumps(payload).encode(), ("127.0.0.1", svc._cmd_sock.getsockname()[1]))


def drain(sink):
    frames = []
    while True:
        try:
            frames.append(sink.recv(2048))
        except socket.timeout:
            return frames


def decode(frame):
    code, size, typ = struct.unpack("<3I", frame[:12])
    if typ == 1:
        return code, struct.unpack("<d", frame[12:12 + size])[0]
    return code, size


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------
def test_velocity_frames_are_little_endian_doubles():
    from host.app.gate_bridge import frames_for_command

    frames = frames_for_command(0.1, -0.05, 0.3)
    assert len(frames) == 3
    codes = [decode(f)[0] for f in frames]
    assert codes == [0x0140, 0x0145, 0x0141]
    values = [decode(f)[1] for f in frames]
    assert values == pytest.approx([0.1, -0.05, 0.3])
    assert all(len(f) == 20 for f in frames)


# --------------------------------------------------------------------------
# Safety behaviour
# --------------------------------------------------------------------------
def test_gate_starts_closed_and_frames_are_zero(rig):
    svc, gate, clk, ctl, sink = rig
    svc.step(clk.t)
    frames = drain(sink)
    assert frames, "service must emit zero frames even when idle"
    assert decode(frames[0])[1] == 0.0
    assert not gate.gate_open()
    assert svc.cmds == 0


def test_command_opens_gate_and_output_follows(rig):
    svc, gate, clk, ctl, sink = rig
    send_cmd(ctl, svc, {"vx": 100, "vy": 0, "wz": 0})
    svc.step(clk.t)
    assert gate.gate_open()
    assert gate.outputs() == pytest.approx((0.1, 0.0, 0.0))
    frames = drain(sink)
    values = [decode(f)[1] for f in frames if decode(f)[0] == 0x0140]
    assert values and values[-1] == pytest.approx(0.1)


def test_deadman_closes_gate_after_timeout(rig):
    svc, gate, clk, ctl, sink = rig
    send_cmd(ctl, svc, {"vx": 100, "vy": 0, "wz": 0})
    svc.step(clk.t)
    assert gate.gate_open()

    clk.advance(0.25)  # still inside the 300 ms deadman window
    svc.step(clk.t)
    assert gate.gate_open()

    clk.advance(0.10)  # now past it
    svc.step(clk.t)
    assert not gate.gate_open()
    assert svc.deadman_trips == 1
    assert gate.outputs() == pytest.approx((0.0, 0.0, 0.0))


def test_stop_command_closes_gate_immediately(rig):
    svc, gate, clk, ctl, sink = rig
    send_cmd(ctl, svc, {"vx": 100, "vy": 0, "wz": 0})
    svc.step(clk.t)
    assert gate.gate_open()

    send_cmd(ctl, svc, {"cmd": "stop"})
    svc.step(clk.t)
    assert not gate.gate_open()


def test_malformed_command_is_ignored(rig):
    svc, gate, clk, ctl, sink = rig
    ctl.sendto(b"not json", ("127.0.0.1", svc._cmd_sock.getsockname()[1]))
    svc.step(clk.t)
    assert svc.cmds == 0
    assert not gate.gate_open()


def test_clear_command_removes_sticky_fault(rig):
    svc, gate, clk, ctl, sink = rig
    # Latch a timeout fault the way the RTL would: arm, then let it expire.
    gate.write(REG_CTRL, CTRL_ENABLE)
    gate.set_command(100, 0, 0)
    gate.arm()
    clk.advance(0.5)
    assert gate.read(0x28) & (1 << 4), "timeout fault should be sticky"

    send_cmd(ctl, svc, {"cmd": "clear"})
    svc.step(clk.t)
    assert not (gate.read(0x28) & (1 << 4))


def test_shutdown_closes_gate(rig):
    svc, gate, clk, ctl, sink = rig
    send_cmd(ctl, svc, {"vx": 100, "vy": 0, "wz": 0})
    svc.step(clk.t)
    assert gate.gate_open()
    svc.shutdown()
    assert not gate.gate_open()
    assert gate._cmd == (0, 0, 0)


# --------------------------------------------------------------------------
# Timing statistics
# --------------------------------------------------------------------------
def test_loop_stats_percentiles():
    st = LoopStats(window=100, period_ms=20.0)
    for _ in range(98):
        st.add(0.020)
    st.add(0.032)   # overrun (> 1.5x period)
    st.add(0.045)
    summary = st.summary()
    assert summary["p50"] == pytest.approx(20.0)
    assert summary["p99"] >= 20.0
    assert summary["max"] == pytest.approx(45.0)
    assert st.overruns == 2


def test_fixed_status_peer_receives_status_without_commands():
    """An operator must be able to watch before sending anything."""
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    status = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    status.bind(("127.0.0.1", 0))
    status.settimeout(0.2)
    clk = FakeClock()
    svc = PlService(
        gate=FakeGate(clock=clk),
        dest=sink.getsockname(),
        cmd_port=free_port(),
        clock=clk,
        status_peer=status.getsockname(),
    )
    try:
        svc.step(clk.t)
        data, _ = status.recvfrom(4096)
        payload = json.loads(data.decode())
        assert payload["gate_open"] is False
        assert "loop_ms" in payload
    finally:
        svc.shutdown()
        sink.close()
        status.close()


def test_heartbeat_is_sent(rig):
    svc, gate, clk, ctl, sink = rig
    svc.step(clk.t)
    codes = [decode(f)[0] for f in drain(sink)]
    assert HEARTBEAT in codes
