"""Command client for the resident PL bridge (notebook side).

Streams velocity commands to ``pl_service`` on the ZYNQ board and prints the
status packets coming back, so the gate state and the loop timing are visible
while commanding.

    # hold 0.10 m/s forward for 2 s (needs --approve: the dog moves)
    python tools/pl_cmd.py --approve --vx 0.10 --seconds 2

    # just watch the service for 5 s, sending nothing
    python tools/pl_cmd.py --watch 5

    # close the gate now
    python tools/pl_cmd.py --stop

The velocity is given in SI units (m/s, rad/s) and converted to the integer
mm/s and mrad/s that the PL registers use, so command and gate read-back are
directly comparable.

Exit codes: 0 ok, 1 safety refusal or no service response.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from typing import Optional

BOARD = "192.168.1.100"
CMD_PORT = 43902
STATUS_PORT = 43903


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Send velocity commands to pl_service")
    p.add_argument("--board", default=BOARD, help="board IP running pl_service")
    p.add_argument("--cmd-port", type=int, default=CMD_PORT)
    p.add_argument("--status-port", type=int, default=STATUS_PORT)
    p.add_argument("--vx", type=float, default=0.0, help="forward m/s")
    p.add_argument("--vy", type=float, default=0.0, help="lateral m/s")
    p.add_argument("--wz", type=float, default=0.0, help="yaw rate rad/s")
    p.add_argument("--seconds", type=float, default=0.0, help="command duration")
    p.add_argument("--rate-hz", type=float, default=50.0)
    p.add_argument("--deadman-ms", type=float, default=300.0,
                   help="keep the stream alive this long after the last command")
    p.add_argument("--watch", type=float, default=0.0, help="listen only, seconds")
    p.add_argument("--stop", action="store_true", help="send a stop and exit")
    p.add_argument("--approve", action="store_true",
                   help="required for any non-zero velocity (conventions.md §1)")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def fmt_status(st: dict) -> str:
    loop = st.get("loop_ms", {})
    return ("gate=%s armed=%s out=(%.2f,%.2f,%.2f) cmd=%s "
            "p50=%.1fms p99=%.1fms over=%d trips=%d" % (
                "OPEN" if st.get("gate_open") else "closed",
                "y" if st.get("armed") else "n",
                *st.get("out", (0.0, 0.0, 0.0)),
                st.get("cmd"),
                loop.get("p50", 0.0), loop.get("p99", 0.0),
                st.get("overruns", 0), st.get("deadman_trips", 0)))


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    moving = any(abs(v) > 1e-9 for v in (args.vx, args.vy, args.wz))
    if moving and not args.approve:
        print("refusing: non-zero velocity needs --approve (conventions.md §1)",
              file=sys.stderr)
        return 1

    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    status = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    status.bind(("0.0.0.0", args.status_port))
    status.settimeout(0.2)
    addr = (args.board, args.cmd_port)

    if args.stop:
        ctl.sendto(json.dumps({"cmd": "stop"}).encode(), addr)
        print("stop sent to %s:%d" % addr)
        return 0

    if args.watch:
        end = time.monotonic() + args.watch
        seen = 0
        while time.monotonic() < end:
            try:
                data, _ = status.recvfrom(4096)
            except socket.timeout:
                continue
            seen += 1
            if not args.quiet:
                print(fmt_status(json.loads(data.decode())))
        if seen == 0:
            print("no status packets: is pl_service running on %s?" % args.board,
                  file=sys.stderr)
            return 1
        return 0

    # Command burst, then keep refreshing for the deadman window so the gate
    # stays open exactly as long as the command is meant to apply.
    vx_mm_s = int(round(args.vx * 1000))
    vy_mm_s = int(round(args.vy * 1000))
    wz_mrad_s = int(round(args.wz * 1000))
    payload = json.dumps({"vx": vx_mm_s, "vy": vy_mm_s, "wz": wz_mrad_s}).encode()

    hold_until = time.monotonic() + args.seconds + args.deadman_ms / 1000.0
    period = 1.0 / args.rate_hz
    sent = 0
    last_print = 0.0
    try:
        while time.monotonic() < hold_until:
            ctl.sendto(payload, addr)
            sent += 1
            try:
                data, _ = status.recvfrom(4096)
                if not args.quiet:
                    print(fmt_status(json.loads(data.decode())))
                    last_print = time.monotonic()
            except socket.timeout:
                pass
            time.sleep(period)
        ctl.sendto(json.dumps({"cmd": "stop"}).encode(), addr)
        time.sleep(0.15)
    except KeyboardInterrupt:
        ctl.sendto(json.dumps({"cmd": "stop"}).encode(), addr)
        print("\ninterrupted: stop sent")

    print("sent %d commands, then stop" % sent)
    if sent and last_print == 0.0 and not args.quiet:
        print("note: no status packets came back (service may be down)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
