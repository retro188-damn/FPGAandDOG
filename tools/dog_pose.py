"""Lean the dog's body down (bow) and stand it back up, via axis commands.

The camera on the head is tilted about 45 degrees, so a card lying on the floor
closer than ~0.8 m falls below the frame entirely.  The motion host exposes an
in-place mode where the *same* axis command codes change the body attitude
instead of the walking velocity:

    in-place mode   0x21010D05        (move mode is 0x21010D06)
    body pitch      0x21010130        deadband +-6553, **positive = nose down**
    body height     0x21010102        deadband +-20000, positive = up
    roll / yaw      0x21010131 / 0x21010135

Axis commands must be streamed at >= 20 Hz (the host stops moving after 250 ms
without one) and the in-place mode returns to the normal stance after 1 s of
silence, so a bow is "stream pitch for N seconds, then stop".

Usage (Git Bash on the PC):

    python tools/dog_pose.py --pitch 12000 --seconds 2.0 --approve
    python tools/dog_pose.py --restore --approve

``--pitch`` above ~6553 does something; the full scale is +-32767.  Bowing
lowers the camera by roughly 20-30 cm at 14000, which is what brings a card at
0.4-0.6 m into view.
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

DOG = "192.168.1.120"
PORT = 43893

MODE_INPLACE = 0x21010D05
MODE_MOVE = 0x21010D06
AXIS_PITCH = 0x21010130
AXIS_HEIGHT = 0x21010102
AXIS_ROLL = 0x21010131
AXIS_YAW = 0x21010135
HEARTBEAT = 0x21040001

AXIS_DEADBAND = {AXIS_PITCH: 6553, AXIS_HEIGHT: 20000, AXIS_ROLL: 12553,
                 AXIS_YAW: 9553}

STREAM_HZ = 30.0


def frame(code: int, value: int = 0) -> bytes:
    return struct.pack("<3I", code, value & 0xFFFFFFFF, 0)


class Link:
    """UDP link that also keeps the 10 Hz heartbeat alive."""

    def __init__(self, dog: str = DOG, port: int = PORT) -> None:
        self.addr = (dog, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.5)

    def send(self, code: int, value: int = 0) -> None:
        self.sock.sendto(frame(code, value), self.addr)

    def stream(self, code: int, value: int, seconds: float,
               hz: float = STREAM_HZ, keep_alive: bool = True) -> int:
        """Send ``code``/``value`` at ``hz`` for ``seconds``; returns frames sent."""
        period = 1.0 / hz
        end = time.time() + seconds
        sent = 0
        while time.time() < end:
            self.send(code, value)
            if keep_alive:
                self.send(HEARTBEAT)
            sent += 1
            time.sleep(period)
        return sent

    def close(self) -> None:
        self.sock.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dog", default=DOG)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--pitch", type=int, default=0,
                    help="body pitch axis value, positive = nose down (deadband 6553)")
    ap.add_argument("--height", type=int, default=0,
                    help="body height axis value, positive = up (deadband 20000)")
    ap.add_argument("--seconds", type=float, default=2.0,
                    help="how long to stream the axis command")
    ap.add_argument("--restore", action="store_true",
                    help="just stand back up: stop streaming and switch to move mode")
    ap.add_argument("--move-mode", action="store_true",
                    help="leave the dog in move mode when --pitch is 0 too")
    ap.add_argument("--settle", type=float, default=0.6,
                    help="seconds to wait after the posture change")
    ap.add_argument("--approve", action="store_true",
                    help="required: the motion plan must be reported and approved")
    ap.add_argument("--approve-mode", action="store_true",
                    help="extra confirmation: this changes the robot's posture")
    args = ap.parse_args()

    if not args.approve:
        print("refusing: posture commands need an approved plan "
              "(docs/conventions.md rule 1). Pass --approve.", file=sys.stderr)
        return 2
    if not args.approve_mode:
        print("refusing: 0x21010D05/0x21010130 change the robot's posture - the "
              "operator must approve this specific action. Pass --approve-mode.",
              file=sys.stderr)
        return 2
    if args.pitch and not (abs(args.pitch) <= 32767):
        print("pitch out of range", file=sys.stderr)
        return 2

    link = Link(args.dog, args.port)
    try:
        if args.restore:
            # 1 s without an axis command puts the body back to its normal
            # stance; the explicit zero stream makes that happen immediately.
            link.stream(AXIS_PITCH, 0, 0.8)
            link.send(MODE_MOVE)
            time.sleep(args.settle)
            print("restored: normal stance, move mode")
            return 0

        link.send(MODE_INPLACE)
        time.sleep(0.4)
        sent = 0
        if args.pitch:
            sent = link.stream(AXIS_PITCH, args.pitch, args.seconds)
        if args.height:
            sent = link.stream(AXIS_HEIGHT, args.height, args.seconds)
        print("streamed %s for %.1f s (%d frames)" % (
            "pitch=%d" % args.pitch if args.pitch else "height=%d" % args.height,
            args.seconds, sent))
        time.sleep(args.settle)
        if args.move_mode:
            link.stream(AXIS_PITCH, 0, 0.3)
            link.send(MODE_MOVE)
            print("back to move mode")
        else:
            print("holding the posture; send --restore (or wait out the host "
                  "timeout) to stand back up")
        return 0
    finally:
        link.close()


if __name__ == "__main__":
    raise SystemExit(main())
