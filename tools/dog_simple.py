"""Send one or more simple (type=0) commands to the Lite3 motion host.

Keeps a heartbeat alive while the command is sent, so a client that was never
registered still has a live link. Python 3.6 compatible (runs on the boards too).

Usage:
    python dog_simple.py --code 0x21010202 --value 0 --heartbeat-s 1.0
    python dog_simple.py --code 0x21010C0A --value 22
"""
import argparse
import socket
import struct
import threading
import time

HEARTBEAT = 0x21040001
MODE_CODES = {
    0x21010C02: "manual mode (handheld remote owns the robot)",
    0x21010C03: "autonomous mode (velocity commands own the robot)",
    0x21010202: "stand/lie toggle",
    0x21020C0E: "software emergency stop",
}
DOG = ("192.168.1.120", 43893)


def simple_frame(code, value=0):
    return struct.pack("<3I", code, value, 0)


class Link(object):
    def __init__(self, hz=10.0, addr=DOG):
        self.addr = addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.stop = threading.Event()
        self.period = 1.0 / hz
        self.thread = threading.Thread(target=self._beat)
        self.thread.daemon = True

    def _beat(self):
        while not self.stop.is_set():
            self.sock.sendto(simple_frame(HEARTBEAT), self.addr)
            time.sleep(self.period)

    def start(self):
        self.thread.start()

    def send(self, code, value=0):
        self.sock.sendto(simple_frame(code, value), self.addr)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1.0)
        self.sock.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=lambda s: int(s, 0), required=True)
    ap.add_argument("--value", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--warmup", type=float, default=0.6, help="seconds of heartbeat before the command")
    ap.add_argument("--hold", type=float, default=0.6, help="seconds of heartbeat after the command")
    ap.add_argument("--port", type=int, default=43893, help="dog UDP port (43893 legacy, 43899 app, 34770 deepros)")
    ap.add_argument("--frames", type=int, default=1,
                    help="how many identical frames to send (1 is correct for toggle commands)")
    ap.add_argument("--approve", action="store_true",
                    help="required: confirms the motion plan was reported and approved")
    ap.add_argument("--approve-mode", action="store_true",
                    help="extra confirmation for control-mode / posture / e-stop codes")
    args = ap.parse_args()

    if not args.approve:
        print("refusing: motion commands need an approved plan (docs/conventions.md rule 1). Pass --approve.")
        raise SystemExit(2)

    # Control-mode, posture and e-stop codes change who owns the robot; the
    # operator must approve the *specific* action (2026-10-26 incident: an
    # unannounced autonomous-mode switch left the handheld remote powerless).
    if args.code in MODE_CODES and not args.approve_mode:
        print("refusing: 0x%08X is %s - that changes who controls the robot."
              % (args.code, MODE_CODES[args.code]))
        print("plan: send it once, then verify by reading the state feed; report "
              "the result before anything else. Re-run with --approve-mode ONLY "
              "after the operator says yes.")
        raise SystemExit(3)

    link = Link(addr=("192.168.1.120", args.port))
    try:
        link.start()
        time.sleep(args.warmup)
        for _ in range(args.frames):
            link.send(args.code, args.value)
            time.sleep(0.05)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        print("[%s] sent code=0x%08X value=%d frames=%d port=%d frame=%s"
              % (stamp, args.code, args.value, args.frames, args.port,
                 " ".join("%02x" % b for b in simple_frame(args.code, args.value))))
        time.sleep(args.hold)
    finally:
        link.close()


if __name__ == "__main__":
    main()
