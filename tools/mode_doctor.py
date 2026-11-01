"""CLI for the mode doctor (E1 measurement).  Read-only unless --approve-mode.

It answers one question: when we send the autonomous/manual mode codes, does
anything in the 0x0901 feedback actually change?  It never sends a velocity
command and never arms the safety gate.

Usage (dog powered, standing, remote in hand):

    python tools/mode_doctor.py probe --seconds 5
    python tools/mode_doctor.py switch --to autonomous --approve-mode
    python tools/mode_doctor.py e1 --approve-mode

Notes
-----
* State frames arrive from the perception board relay (UDP 43900).  If nothing
  arrives the tool refuses to run instead of reporting a false "no change".
* Every run appends one JSON file under ``metrics/evidence/`` so the result can
  be quoted in the report without re-running the experiment.
"""
import argparse
import datetime
import json
import os
import pathlib
import socket
import struct
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.app import mode_doctor as md  # noqa: E402
from host.app.dog_state import StateFeed, fmt_state  # noqa: E402

HEARTBEAT = 0x21040001
DOG = ("192.168.1.120", 43893)


class HeartbeatLink(object):
    """Send simple frames to the motion host while keeping the link alive."""

    def __init__(self, addr=DOG, hz=10.0):
        self.addr = addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.period = 1.0 / hz
        self.sent = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat)
        self._thread.daemon = True

    def _beat(self):
        while not self._stop.is_set():
            self.sock.sendto(struct.pack("<3I", HEARTBEAT, 0, 0), self.addr)
            time.sleep(self.period)

    def start(self):
        self._thread.start()
        return self

    def send(self, code, value=0):
        frame = struct.pack("<3I", code, value, 0)
        self.sock.sendto(frame, self.addr)
        self.sent.append((code, value))

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1.0)
        self.sock.close()


def _evidence(name, payload):
    out = ROOT / "metrics" / "evidence"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = out / ("%s_%s.json" % (name, stamp))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _log(line):
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / ("mode_doctor_%s.log" % datetime.datetime.now().strftime("%Y%m%d"))
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("[%s] [INFO] [mode_doctor] %s\n" % (stamp, line))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="read-only: print the state for N seconds")
    p_probe.add_argument("--seconds", type=float, default=5.0)

    p_switch = sub.add_parser("switch", help="send ONE mode frame and compare")
    p_switch.add_argument("--to", choices=["autonomous", "manual"], required=True)
    p_switch.add_argument("--wait", type=float, default=3.0)
    p_switch.add_argument("--approve-mode", action="store_true")

    p_e1 = sub.add_parser("e1", help="probe -> manual -> probe -> autonomous -> probe")
    p_e1.add_argument("--wait", type=float, default=3.0)
    p_e1.add_argument("--approve-mode", action="store_true")

    ap.add_argument("--state-port", type=int, default=43900)
    args = ap.parse_args(argv)

    link = HeartbeatLink().start()
    feed = StateFeed(port=args.state_port).start()
    doctor = md.ModeDoctor(link=link, feed=feed, clock=time.monotonic)

    try:
        doctor.require_state()
        state, age = feed.latest()
        print("dog state: %s (age %.2fs)" % (fmt_state(state), age or -1.0))

        if args.cmd == "probe":
            summary = doctor.snapshot(args.seconds)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            path = _evidence("mode_probe", summary)
            _log("probe %.1fs -> %s (evidence %s)" % (args.seconds, summary.get("robot_motion_state_values"), path.name))
            print("evidence ->", path)
            return 0

        if not args.approve_mode:
            print(
                "refused: this sends a mode frame to the dog.  Re-run with\n"
                "  --approve-mode   (dog standing, remote in hand, someone watching)",
                file=sys.stderr,
            )
            return 3

        if args.cmd == "switch":
            result = doctor.switch(args.to, wait_s=args.wait)
        else:
            result = doctor.e1(wait_s=args.wait)

        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.cmd == "e1":
            print("\nreading:", md.describe(result))
        path = _evidence("mode_%s" % args.cmd, result)
        _log("%s -> sent %s; any_change=%s (evidence %s)"
             % (args.cmd, link.sent, result.get("any_change"), path.name))
        print("evidence ->", path)
        return 0
    except md.ModeDoctorError as exc:
        print("error:", exc, file=sys.stderr)
        return 4
    finally:
        link.close()


if __name__ == "__main__":
    raise SystemExit(main())
