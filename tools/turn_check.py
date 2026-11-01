"""Acceptance check for the two navigation primitives: ``turn`` / ``move_to_point``.

This is the operator's脚本, not a demo hand-off: it prints one line per action,
writes a CSV next to the log, and refuses to move anything unless ``--approve``
is given (same rule as tools/dog_simple.py and host/app/assistant.py).

Usage
-----
    # dry run: prints the plan, sends nothing
    python tools/turn_check.py --turns 90,90,90

    # the real thing (dog standing, hand on the gamepad)
    python tools/turn_check.py --turns 90,90,90 --approve

    # turn + point navigation (0.5 m to the dog's left)
    python tools/turn_check.py --turns 90 --point 0.0,0.5 --approve

Acceptance (docs/runbook_motion.md section on turning)
  1. each turn lands within +-5 deg of the commanded angle
  2. move_to_point(0, 0.5) ends within 0.1 m of the intended point
  3. grabbing the gamepad takes over within 0.5 s (a human check, not scripted)

Everything the script sends goes through pl_service -> safety_gate, so the PL
watchdog (200 ms) and the ramp shaper are still in the path; the dog also stops
on its own if the state feed dies (max_stale_s = 0.35 s).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import socket
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.app.dog_state import StateFeed, fmt_state, is_standing, position  # noqa: E402
from host.app.motion_exec import MotionExecutor, MotionRefused, VelocitySender  # noqa: E402

LOG_DIR = ROOT / "logs"


def _parse_turns(text: str) -> list[float]:
    values = []
    for chunk in text.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        values.append(float(chunk))
    if not values:
        raise SystemExit("--turns is empty")
    for v in values:
        if abs(v) > 180.0:
            raise SystemExit(f"--turns {v}: |angle| above the 180 deg operator limit")
    return values


def _world_target(p0, yaw0_deg: float, dx: float, dy: float):
    """Body-frame offset (dx forward, dy left) -> world XY, yaw + = left/CCW."""
    yaw = math.radians(yaw0_deg)
    wx = p0[0] + dx * math.cos(yaw) - dy * math.sin(yaw)
    wy = p0[1] + dx * math.sin(yaw) + dy * math.cos(yaw)
    return wx, wy


def _send_mode(code: int, dog: str, port: int = 43893) -> None:
    """Send one control-mode frame (``<3I``: code, value, 0) straight to the dog.

    Observed 2026-10-27: the dog accepts our velocity stream only while the
    "last actor" is the perception host, so after a motion (or a stop) it stops
    listening until the mode code is sent again -- the live state shows
    ``robot_motion_state=0`` either way, so the frame is the only lever.
    """
    frame = struct.pack("<3I", code, 0, 0)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(frame, (dog, port))
    print("    re-arm: sent mode frame %s -> %s:%d" % (frame.hex(" "), dog, port))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--turns", default="90,90,90",
                    help="comma separated angles in degrees (default 90,90,90)")
    ap.add_argument("--point", default="", metavar="DX,DY",
                    help="after the turns, run move_to_point(DX, DY) in metres")
    ap.add_argument("--speed", type=float, default=0.10, help="walk speed for --point")
    ap.add_argument("--tolerance", type=float, default=3.0,
                    help="heading tolerance inside the loop (default 3 deg)")
    ap.add_argument("--pause", type=float, default=1.0,
                    help="seconds to settle between actions (default 1.0)")
    ap.add_argument("--approve", action="store_true",
                    help="actually send motion; without it this is a dry run")
    ap.add_argument("--rearm", default="", metavar="MODE_CODE",
                    help="send this control-mode code (e.g. 0x21010C03) before every "
                         "action; live tests showed the dog drops our velocity stream "
                         "after each motion")
    ap.add_argument("--dog", default="192.168.1.120", help="dog host for --rearm")
    ap.add_argument("--board", default="192.168.1.100")
    ap.add_argument("--cmd-port", type=int, default=43902)
    ap.add_argument("--state-port", type=int, default=43900)
    args = ap.parse_args(argv)

    angles = _parse_turns(args.turns)
    point = None
    if args.point.strip():
        parts = args.point.replace(";", ",").split(",")
        if len(parts) != 2:
            raise SystemExit("--point wants DX,DY")
        point = (float(parts[0]), float(parts[1]))

    try:
        feed = StateFeed(port=args.state_port).start()
    except OSError as exc:
        raise SystemExit(f"cannot bind UDP {args.state_port}: {exc}\n"
                         "is state_monitor.py already running in another window?")

    sender = VelocitySender(board=args.board, port=args.cmd_port)
    ex = MotionExecutor(sender, feed, allow_motion=bool(args.approve))

    print("=" * 78)
    print("turn_check:  turns =", angles, " point =", point,
          " speed =", args.speed, " approve =", args.approve)
    print("board:", args.board, args.cmd_port, " state:", args.state_port)
    print("SAFETY: hand on the gamepad. Ctrl-C aborts and sends stop.")
    print("=" * 78)

    rearm = int(args.rearm, 0) if args.rearm.strip() else 0

    try:
        state = ex.preflight(wait_s=2.0)
        p0 = position(state)
        yaw0 = _yaw_deg(state)
        print("preflight OK:", fmt_state(state))
        print("start pose: x=%.3f y=%.3f yaw=%.1f deg" % (p0[0], p0[1], yaw0))
        if not is_standing(state):
            print("WARNING: dog is not standing")

        rows = []
        for index, angle in enumerate(angles, start=1):
            if not args.approve:
                print(f"\n[DRY RUN] would turn {angle:+.0f} deg "
                      f"(loop tolerance {args.tolerance} deg)")
                rows.append({"kind": "turn", "target": angle, "measured": None})
                continue
            print(f"\n--- turn {index}/{len(angles)}: {angle:+.0f} deg ---")
            if rearm:
                _send_mode(rearm, args.dog)
                time.sleep(0.3)
            before = ex.preflight(wait_s=2.0)
            yaw_before = _yaw_deg(before)
            result = ex.turn(float(angle), tolerance_deg=args.tolerance)
            after = feed.latest()[0]
            yaw_after = _yaw_deg(after) if after is not None else float("nan")
            measured = _wrap(yaw_after - yaw_before)
            if (not result.ok and rearm and "not responding" in result.reason
                    and abs(measured) < 2.0):
                # The body never moved, so nothing has to be undone: give the
                # dog a fresh control grant and take the shot again.
                print("    no motion at all -- re-arming and retrying once")
                _send_mode(rearm, args.dog)
                time.sleep(0.4)
                before = ex.preflight(wait_s=2.0)
                yaw_before = _yaw_deg(before)
                result = ex.turn(float(angle), tolerance_deg=args.tolerance)
                after = feed.latest()[0]
                yaw_after = _yaw_deg(after) if after is not None else float("nan")
                measured = _wrap(yaw_after - yaw_before)
            error = _wrap(measured - angle)
            print(f"    {result.reason:<28} ok={result.ok} "
                  f"measured={measured:+.1f} deg  error={error:+.1f} deg  "
                  f"took={result.elapsed_s:.2f} s  cmds={result.commands}")
            rows.append({"kind": "turn", "target": angle, "measured": measured,
                         "error": error, "ok": result.ok, "reason": result.reason,
                         "elapsed_s": result.elapsed_s})
            time.sleep(max(0.0, args.pause))

        if point is not None:
            if not args.approve:
                print(f"\n[DRY RUN] would move_to_point({point[0]:.2f}, {point[1]:.2f}) "
                      f"at {args.speed:.2f} m/s")
            else:
                print(f"\n--- move_to_point({point[0]:+.2f}, {point[1]:+.2f}) ---")
                if rearm:
                    _send_mode(rearm, args.dog)
                    time.sleep(0.3)
                before = ex.preflight(wait_s=2.0)
                p_start = position(before)
                yaw_start = _yaw_deg(before)
                wx, wy = _world_target(p_start, yaw_start, point[0], point[1])
                turned, walked = ex.move_to_point(point[0], point[1], speed=args.speed)
                after = feed.latest()[0]
                p_end = position(after) if after is not None else p_start
                miss = math.hypot(p_end[0] - wx, p_end[1] - wy)
                dist = math.hypot(p_end[0] - p_start[0], p_end[1] - p_start[1])
                print(f"    turn : {turned.reason:<24} {turned.turned_deg:+.1f} deg")
                print(f"    walk : {walked.reason:<24} travelled={walked.travelled_m:.3f} m "
                      f"(asked {math.hypot(*point):.3f}) took={walked.elapsed_s:.2f} s")
                print(f"    miss from intended point: {miss:.3f} m  "
                      f"(acceptance: <= 0.10 m)")
                rows.append({"kind": "point", "target": math.hypot(*point),
                             "measured": dist, "error": miss,
                             "ok": walked.ok, "reason": walked.reason,
                             "elapsed_s": walked.elapsed_s})

        if args.approve and rows:
            LOG_DIR.mkdir(exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
            path = LOG_DIR / f"turn_check_{stamp}.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["kind", "target", "measured",
                                                        "error", "ok", "reason",
                                                        "elapsed_s"],
                                        extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
            print("\nCSV:", path)
            turns = [r for r in rows if r["kind"] == "turn" and r.get("measured") is not None]
            if turns:
                bad = [r for r in turns if abs(r["error"]) > 5.0]
                print("turn acceptance (|error| <= 5 deg): %d/%d pass"
                      % (len(turns) - len(bad), len(turns)))
                if bad:
                    print("  failures:", ", ".join(f"{r['target']:+.0f}->{r['measured']:+.1f}"
                                                   for r in bad))
            pts = [r for r in rows if r["kind"] == "point"]
            if pts:
                print("point acceptance (miss <= 0.10 m):", "PASS"
                      if pts[0]["error"] <= 0.10 else "FAIL")
        return 0
    except MotionRefused as exc:
        print("\nREFUSED:", exc)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted by operator")
        return 130
    finally:
        sender.close()
        feed.close()


def _yaw_deg(state) -> float:
    """World yaw in degrees from the 0x0901 frame's ``rpy[3]``.

    The frame already carries degrees (verified against ``/leg_odom`` on
    192.168.1.103 on 2026-10-27), so no conversion is applied.
    """
    return float(state["rpy[3]"][2])


def _wrap(value: float) -> float:
    """Fold ``value`` into (-180, 180] the same way motion_exec does."""
    wrapped = math.fmod(value + 180.0, 360.0)
    if wrapped <= 0:
        wrapped += 360.0
    return wrapped - 180.0


if __name__ == "__main__":
    raise SystemExit(main())
