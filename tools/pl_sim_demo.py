"""Offline demo of the resident PL bridge: no board, no dog, no pynq.

Runs the real service loop (fake gate) against a UDP sink standing in for the
dog, and a scripted client on the command port, so the safety behaviour and the
timing numbers can be checked before anything touches hardware.

    python tools/pl_sim_demo.py
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.app.pl_service import FakeGate, PlService  # noqa: E402

CMD_PORT = 43902
STATUS_PORT = 43903
HEAD = struct.Struct("<3I")


def decode(frame: bytes):
    code, size, typ = HEAD.unpack_from(frame)
    if typ == 1 and size == 8:
        return code, struct.unpack_from("<d", frame, 12)[0]
    return code, size


class Collector(threading.Thread):
    """Stand-in for the dog: counts frames and remembers the last velocity."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(daemon=True)
        self.sock = sock
        self.counts = {}
        self.last = {}
        self.rate_samples = []
        self._stop = False
        self._window_start = time.monotonic()
        self._window_n = 0

    def run(self) -> None:
        self.sock.settimeout(0.05)
        while not self._stop:
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            code, value = decode(data)
            self.counts[code] = self.counts.get(code, 0) + 1
            self.last[code] = value
            self._window_n += 1
            now = time.monotonic()
            if now - self._window_start >= 0.5:
                self.rate_samples.append(self._window_n / (now - self._window_start))
                self._window_n = 0
                self._window_start = now

    def stop(self) -> None:
        self._stop = True


def status_reader(sock: socket.socket, out: list) -> None:
    sock.settimeout(0.05)
    end = time.monotonic() + 6.0
    while time.monotonic() < end:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        out.append(json.loads(data.decode()))


def main() -> int:
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    stat = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stat.bind(("127.0.0.1", STATUS_PORT))

    gate = FakeGate()
    svc = PlService(gate, dest=sink.getsockname(), cmd_port=CMD_PORT, status_port=STATUS_PORT)

    collector = Collector(sink)
    collector.start()
    statuses: list = []
    threading.Thread(target=status_reader, args=(stat, statuses), daemon=True).start()

    server = threading.Thread(target=lambda: svc.run(duration=4.2), daemon=True)
    server.start()

    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = ("127.0.0.1", CMD_PORT)
    t0 = time.monotonic()
    events = []

    def log(msg: str) -> None:
        events.append((round(time.monotonic() - t0, 2), msg))
        print("[%4.2f] %s" % (time.monotonic() - t0, msg), flush=True)

    time.sleep(0.7)
    log("idle: expect gate closed, zero frames -> last vel=%s" % collector.last.get(0x0140))
    assert collector.last.get(0x0140) == 0.0, "idle must emit zeros"

    log("sending velocity 0.10 m/s for 0.8 s")
    send_until = time.monotonic() + 0.8
    while time.monotonic() < send_until:
        ctl.sendto(json.dumps({"vx": 100, "vy": 0, "wz": 0}).encode(), addr)
        time.sleep(0.02)

    log("stopped sending: deadman should close the gate within ~300 ms")
    time.sleep(0.45)
    log("after deadman: last vel=%s (expect 0.0)" % collector.last.get(0x0140))
    assert collector.last.get(0x0140) == 0.0, "deadman must zero the wire"

    log("re-command 0.05 m/s then an explicit stop")
    for _ in range(10):
        ctl.sendto(json.dumps({"vx": 50, "vy": 0, "wz": 0}).encode(), addr)
        time.sleep(0.02)
    time.sleep(0.1)
    mid = collector.last.get(0x0140)
    log("while commanded: last vel=%.3f (expect 0.05)" % mid)
    ctl.sendto(json.dumps({"cmd": "stop"}).encode(), addr)
    time.sleep(0.2)
    log("after stop: last vel=%s (expect 0.0)" % collector.last.get(0x0140))
    assert collector.last.get(0x0140) == 0.0, "stop must zero immediately"

    server.join(timeout=5.0)
    collector.stop()

    ok = True
    print("\n--- frame counts (codes on the wire) ---")
    for code, n in sorted(collector.counts.items()):
        name = {0x0140: "vx", 0x0141: "wz", 0x0145: "vy", 0x21040001: "heartbeat"}.get(code, hex(code))
        print("  0x%08x %-10s %d" % (code, name, n))
    print("\n--- sink rate samples (frames/s) ---")
    print("  " + ", ".join("%.0f" % r for r in collector.rate_samples[:6]))

    if statuses:
        last = statuses[-1]
        print("\n--- last status packet ---")
        print("  %s" % json.dumps(last, indent=2).replace("\n", "\n  "))
        loop = last["loop_ms"]
        print("\n--- timing ---")
        print("  loop p50=%.2f ms  p99=%.2f ms  max=%.2f ms  overruns=%d"
              % (loop["p50"], loop["p99"], loop["max"], last["overruns"]))
        if last["overruns"]:
            print("  NOTE: overruns counted on this machine's scheduler")
    else:
        print("\nWARN: no status packets received")
        ok = False

    print("\n--- event timeline ---")
    for t, msg in events:
        print("  t=%4.2fs  %s" % (t, msg))
    print("\nSIM RESULT: %s" % ("PASS" if ok else "CHECK"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
