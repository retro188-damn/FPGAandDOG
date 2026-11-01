"""Find the digit card wherever it is, walk up to it and read it.

    python tools/seek_card.py                        # plan only, nothing moves
    python tools/seek_card.py --approve              # sweep, face it, walk over, read
    python tools/seek_card.py --approve --no-sweep   # card is already in view

The dog's camera covers roughly 60-70 degrees, so a card beside or behind it is
not in the picture.  With ``--approve`` the tool turns in place through a fan of
headings, stops at the first where the card appears, turns until the card sits
in the middle of the frame, then steps in until the card is wide enough to
classify -- the same width-driven approach the voice agent uses.

Every motion is preceded by a fresh ``0x21010C03`` frame: after each stop the
dog drops our velocity stream and ignores the next command (measured three times
on 2026-10-27), so re-arming is cheaper than retrying.

On exit -- including on error -- the tool sends ``0x21010C02`` so the gamepad
has control again; the dog is never left accepting our velocity stream.
"""
from __future__ import annotations

import argparse
import logging
import math
import socket
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.app.dog_state import StateFeed  # noqa: E402
from host.app.motion_exec import (  # noqa: E402
    MotionExecutor,
    MotionRefused,
    VelocitySender,
    _wrap_deg,
)
from host.app.vision import seek  # noqa: E402

LOG = logging.getLogger("seek_card")

MODE_AUTONOMOUS = 0x21010C03
MODE_MANUAL = 0x21010C02
DOG_PORT = 43893


def send_mode(code: int, dog: str, port: int = DOG_PORT) -> None:
    """One control-mode frame: ``<3I`` of (code, value, 0)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(struct.pack("<3I", code, 0, 0), (dog, port))
    finally:
        sock.close()


_LOOK = {"shots": 2, "gap": 0.20, "dir": None, "index": 0}
STOPS = []


def _save_frame(frame, box, tag: str) -> None:
    """Keep the picture we reasoned about, so a miss can be explained later."""
    folder = _LOOK["dir"]
    if folder is None:
        return
    from PIL import ImageDraw

    folder.mkdir(parents=True, exist_ok=True)
    _LOOK["index"] += 1
    drawn = frame.copy()
    if box is not None:
        ImageDraw.Draw(drawn).rectangle(box, outline=(255, 0, 0), width=3)
    name = "%03d_%s.png" % (_LOOK["index"], tag.replace(" ", "_").replace("/", "-"))
    drawn.save(folder / name)
    LOG.info("saved %s (box %s)", name, box)


def card_width():
    """The card's pixel width in the latest frame, or None."""
    box = grab_box()
    return None if box is None else seek.box_width(box)


def grab_box(tag: str = ""):
    """Card box from the best of a few frames taken a moment apart.

    A single frame grabbed the instant a turn finishes is fragile: the body is
    still settling, so the same card can be missed on one frame and found on
    the next.  Take a short burst and keep the widest detection.
    """
    from host.app.vision.camera_client import grab_frame
    from host.app.vision.digit_reader import find_card, to_gray

    best = None
    for index in range(max(1, _LOOK["shots"])):
        if index:
            time.sleep(_LOOK["gap"])
        try:
            frame = grab_frame()
            box = find_card(to_gray(frame))
        except Exception as exc:  # camera down: looking again will say the same
            LOG.error("look failed: %s", exc)
            continue
        _save_frame(frame, box, "%s_%d" % (tag or "look", index))
        if box is not None and (best is None or seek.box_width(box) > seek.box_width(best)):
            best = box
    return best


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dog", default="192.168.1.120", help="dog motion board (mode frames)")
    p.add_argument("--board", default="192.168.1.100", help="PL bridge host")
    p.add_argument("--cmd-port", type=int, default=43902)
    p.add_argument("--state-port", type=int, default=43900)
    p.add_argument("--period", type=float, default=0.05)
    p.add_argument("--speed", type=float, default=0.10, help="m/s while walking in")
    p.add_argument("--step", type=float, default=0.15, help="m per approach leg")
    p.add_argument("--target-px", type=int, default=120)
    p.add_argument("--max-px", type=int, default=320)
    p.add_argument("--max-legs", type=int, default=6)
    p.add_argument("--max-turns", type=int, default=6)
    p.add_argument("--tolerance-px", type=float, default=40.0)
    p.add_argument("--gain", type=float, default=seek.START_GAIN_DEG_PER_PX)
    p.add_argument("--shots", type=int, default=3)
    p.add_argument("--settle", type=float, default=1.5)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--want", default="", help="only stop on this digit (e.g. --want 8)")
    p.add_argument("--min-confidence", type=float, default=0.85,
                   help="with --want, a reading must be this sure to count")
    p.add_argument("--card-step", type=float, default=0.40,
                   help="with --want, how far to walk towards a card whose digit is unreadable")
    p.add_argument("--look-shots", type=int, default=2,
                   help="frames to grab at each stop (the widest detection wins)")
    p.add_argument("--look-gap", type=float, default=0.20,
                   help="seconds between those frames, so body sway differs")
    p.add_argument("--save-frames", default="_scratch/seek_frames",
                   help="where to keep the frames we reasoned about")
    p.add_argument("--no-save-frames", action="store_true")
    p.add_argument("--single-pass", action="store_true", default=False,
                   help="do not retry the sweep with centring after the fast pass found nothing")
    p.add_argument("--no-approach", action="store_true",
                   help="turn only: never walk toward a card during the sweep "
                        "(the arena is small and a blind 0.4 m leg ended 0.28 m "
                        "from an obstacle on 2026-10-28)")
    p.add_argument("--min-clearance", type=float, default=0.40,
                   help="refuse to walk forward when the rangefinders read closer than "
                        "this (m); the arena is small, so 0.30 keeps a leg usable near "
                        "furniture while still stopping before contact")
    p.add_argument("--max-travel", type=float, default=1.20,
                   help="total forward travel allowed in one run (m)")
    p.add_argument("--no-sweep", action="store_true", help="card is already in view")
    p.add_argument("--no-read", action="store_true")
    p.add_argument("--approve", action="store_true", help="actually move the dog")
    return p.parse_args(argv)


def dry_run(args) -> None:
    steps = [
        "check the dog is standing, powered and free of faults (preflight)",
        "send 0x21010C03 so the dog accepts our velocity stream",
    ]
    fan = ", ".join(f"{o:+.0f}" for o in seek.SWEEP_OFFSETS_DEG)
    if not args.no_sweep and args.want:
        steps.append(f"turn in place through {fan} degrees; at each stop read first and "
                     f"only aim at the card when it looks like {args.want} at "
                     f"{args.min_confidence:.0%} confidence")
        if args.no_approach:
            steps.append("never walk during the sweep (--no-approach: the arena is small)")
        else:
            steps.append(f"when a card is too small to read, walk {args.card_step:.2f} m "
                         f"closer at most {2} times")
        if not args.single_pass:
            steps.append("if nothing matched, walk the circle again aiming at every card "
                         "before reading (slow second pass)")
    elif not args.no_sweep:
        steps.append(f"turn in place through {fan} degrees until the card is in view")
    steps += [
        f"then turn until the card is within {args.tolerance_px:.0f} px of the centre "
        f"(gain {args.gain:.3f} deg/px, at most {args.max_turns} turns)",
        f"step {args.step:.2f} m at a time until the card is {args.target_px} px wide "
        f"(stop if it passes {args.max_px} px, at most {args.max_legs} legs)",
        "settle 1.5 s, take 3 shots, vote, and speak the result",
        f"every leg is gated: refuse to walk when the rangefinders read under "
        f"{args.min_clearance:.2f} m, or after {args.max_travel:.2f} m of travel in total",
    ]
    print("plan (nothing moves without --approve):")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")


def _write_stops() -> None:
    """One CSV row per stop, next to the saved frames."""
    folder = _LOOK["dir"]
    if folder is None or not STOPS:
        return
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "stops.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write("heading,digits,confidence,agreement,drift_m\n")
        for row in STOPS:
            fh.write("%s,%s,%.3f,%.2f,%.3f\n" % row)
    LOG.info("stop log: %s", path)


class Dog:
    """Thin wrapper that re-arms the dog before every motion command."""

    def __init__(self, args, executor: MotionExecutor) -> None:
        self.args = args
        self.ex = executor
        self.start_yaw = 0.0
        self.start_xy = (0.0, 0.0)
        self.motions = 0
        self.travelled = 0.0

    def drift_m(self) -> float:
        """How far the dog has wandered from where the sweep started."""
        state = self.ex.feed.latest()[0]
        if state is None:
            return 0.0
        px, py = float(state["pos_world[3]"][0]), float(state["pos_world[3]"][1])
        return math.hypot(px - self.start_xy[0], py - self.start_xy[1])

    def arm(self) -> None:
        send_mode(MODE_AUTONOMOUS, self.args.dog)
        time.sleep(0.3)

    def heading(self) -> float:
        return self.ex.preflight(wait_s=1.0)["rpy[3]"][2]

    def turn_to(self, offset_deg: float) -> None:
        """Turn to the heading ``start + offset``, re-arming until it sticks.

        ``MotionExecutor.turn`` returns a failed TurnResult instead of raising
        (timeout, "dog is not responding", feed lost), and the dog drops our
        stream after every stop -- measured 2026-10-28: stops 4..8 never turned
        at all, so the sweep "visited" the same bearing eight times and could
        not find a card sitting at 90 degrees.  Re-arming and retrying fixes it.
        """
        target = self.start_yaw + float(offset_deg)
        for attempt in (1, 2, 3):
            self.arm()
            self.motions += 1
            result = self.ex.turn_to_heading(target)
            if getattr(result, "ok", False):
                return
            LOG.warning("turn to %+0.0f deg failed (attempt %d): %s",
                        float(offset_deg), attempt, getattr(result, "reason", "?"))
            time.sleep(0.3)
        LOG.error("turn to %+0.0f deg never succeeded - the sweep will be wrong",
                  float(offset_deg))

    def turn_by(self, angle_deg: float) -> None:
        self.arm()
        self.motions += 1
        self.ex.turn(angle_deg)

    def clearance(self) -> float:
        """Smallest rangefinder reading, in metres (front-left and front-right)."""
        state = self.ex.preflight(wait_s=1.0)
        return min(float(v) for v in state["ultrasound[2]"])

    def leg(self, step: float = None) -> bool:
        """One approach leg; retried once when the dog ignores the command."""
        step = self.args.step if step is None else step
        if self.travelled + step > self.args.max_travel:
            LOG.error("REFUSING to walk: %.2f m already travelled, limit %.2f m",
                      self.travelled, self.args.max_travel)
            return False
        try:
            gap = self.clearance()
        except MotionRefused as exc:
            LOG.error("REFUSING to walk: cannot read the rangefinders (%s)", exc)
            return False
        if gap < self.args.min_clearance:
            LOG.error("REFUSING to walk: only %.2f m of clearance ahead (limit %.2f m)",
                      gap, self.args.min_clearance)
            return False
        for attempt in (1, 2):
            self.arm()
            self.motions += 1
            try:
                result = self.ex.move(step, speed=self.args.speed)
            except MotionRefused as exc:
                LOG.error("move refused: %s", exc)
                return False
            if result.travelled_m >= 0.02:
                self.travelled += result.travelled_m
                LOG.info("leg %d: %.3f m in %.2f s (total %.2f m, %s)",
                         attempt, result.travelled_m, result.elapsed_s,
                         self.travelled,
                         "clearance %.2f m" % self.clearance())
                return True
            LOG.warning("leg ignored (%s) - re-arming", result.reason)
            time.sleep(0.4)
        return False


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.approve:
        dry_run(args)
        return 0
    _LOOK["shots"] = max(1, args.look_shots)
    _LOOK["gap"] = args.look_gap
    _LOOK["dir"] = None if args.no_save_frames else Path(args.save_frames)

    feed = StateFeed(port=args.state_port).start()
    try:
        sender = VelocitySender(board=args.board, port=args.cmd_port)
        executor = MotionExecutor(sender, feed, allow_motion=True, period_s=args.period)
        # A stalled turn used to burn the full 0.6 s window before the retry;
        # 0.45 s fails fast enough to keep a nine-stop sweep under a minute.
        executor.response_window_s = 0.45
        dog = Dog(args, executor)
        state = executor.preflight(wait_s=2.0)
        LOG.info("preflight ok: basic=%s battery=%.0f%% err=%s",
                 state.get("basic"), float(state.get("battery_level", 0.0)),
                 state.get("error_state"))
        dog.start_yaw = float(state["rpy[3]"][2])
        dog.start_xy = (float(state["pos_world[3]"][0]), float(state["pos_world[3]"][1]))
        LOG.info("start heading %.1f deg", dog.start_yaw)
        current = {"offset": "start"}
        _turn_to = dog.turn_to

        def turn_to(offset_deg):
            current["offset"] = "%+.0f" % float(offset_deg)
            _turn_to(offset_deg)
            # Log where the dog *actually* ended up: the body drifts while it
            # steps in place, so the nominal offset is not ground truth.
            try:
                actual = dog.heading()
            except Exception as exc:          # state gap or fault: keep going
                LOG.warning("  %+6.0f deg -> heading unreadable (%s)", float(offset_deg), exc)
                return
            drift = _wrap_deg(actual - (dog.start_yaw + float(offset_deg)))
            LOG.info("  %+6.0f deg -> heading %7.1f (drift %+.1f)", float(offset_deg), actual, drift)

        if args.want and not args.no_sweep:
            def _read_now():
                from host.app.vision.digit_service import read_burst

                time.sleep(0.3)          # let the turn's blur settle
                reading = read_burst(shots=max(1, args.shots))
                drift = dog.drift_m()
                if drift > 0.10:
                    LOG.warning("  position drifted %.2f m from the start", drift)
                STOPS.append((current["offset"], reading.digits,
                              reading.confidence, reading.agreement, drift))
                return reading.digits, reading.confidence

            def _line_up(_box):
                centred = seek.centre_card(
                    grab_box, dog.turn_by,
                    gain_deg_per_px=args.gain,
                    tolerance_px=args.tolerance_px,
                    max_turns=args.max_turns,
                )
                return centred.box

            # Fast pass: read before aiming.  The operator parks the dog on a
            # card, so aiming first turned the dog at the very first stop for no
            # reason (2026-10-28) and made every stop ~1 s slower.
            started = time.time()
            hunt = seek.hunt_for_digit(
                grab_box, _line_up, _read_now, turn_to, args.want,
                min_confidence=args.min_confidence,
                approach=None if args.no_approach else (lambda: dog.leg(args.card_step)),
            )
            for offset, digits, confidence in hunt.seen:
                LOG.info("  %+6.0f deg -> %r @ %.2f", offset, digits, confidence)
            LOG.info("fast pass: %s (%.1f s, %d stop(s))",
                     hunt.reason, time.time() - started, len(hunt.seen))

            # Only if the fast pass came up empty: walk the circle again, this
            # time lining the dog up on every card before reading.  Slow, but it
            # catches a digit that was read confidently wrong off-centre.
            if not hunt.found and not args.single_pass:
                LOG.info("retrying the sweep with centring on every card")
                STOPS.clear()
                hunt = seek.hunt_for_digit(
                    grab_box, _line_up, _read_now, turn_to, args.want,
                    min_confidence=args.min_confidence,
                    approach=None if args.no_approach else (lambda: dog.leg(args.card_step)),
                    centre_before_read=True,
                )
                for offset, digits, confidence in hunt.seen:
                    LOG.info("  %+6.0f deg -> %r @ %.2f (centred)", offset, digits, confidence)
            _write_stops()
            if not hunt.found:
                heard = ", ".join(f"{d or '看不清'}" for _, d, _ in hunt.seen) or "什么也没看到"
                print(f"没有找到 {args.want}（看到的是：{heard}）")
                return 1
            LOG.info("hunt: %s", hunt.reason)

        box = grab_box()
        if box is not None and not args.no_sweep:
            LOG.info("card already in view (%d px wide) - skipping the sweep", seek.box_width(box))
        elif not args.no_sweep:
            sweep = seek.sweep_for_card(grab_box, dog.turn_to)
            LOG.info("sweep: %s after %d stop(s)", sweep.reason, sweep.stops)
            if not sweep.found:
                print("no card at any heading - is the white paper on the brown board?")
                return 1
            box = sweep.box

        centred = seek.centre_card(
            grab_box, dog.turn_by,
            gain_deg_per_px=args.gain,
            tolerance_px=args.tolerance_px,
            max_turns=args.max_turns,
        )
        LOG.info("centre: %s (offset %.0f px, gain %.3f deg/px, %d turns)",
                 centred.reason, centred.offset_px, centred.gain_deg_per_px, centred.turns)
        if not centred.ok:
            print(f"could not line up on the card: {centred.reason}")
            return 1

        walk = seek.walk_to_width(
            card_width,
            dog.leg,
            target_px=args.target_px,
            max_px=args.max_px,
            max_legs=args.max_legs,
        )
        LOG.info("approach: %s (card %s px after %d leg(s))", walk.reason, walk.width_px, walk.legs)

        if args.no_read:
            print(f"stopped in front of the card ({walk.width_px} px wide)")
        else:
            from host.app.vision.digit_service import read_burst, read_settled, spoken

            result = read_settled(
                lambda: read_burst(shots=args.shots),
                feed=feed,
                settle_s=args.settle,
                retries=args.retries,
            )
            line = spoken(result)
            LOG.info("READ %s", result.to_dict())
            print(line)
        LOG.info("%d motion command(s); hand control back with 0x21010C02", dog.motions)
        return 0
    except MotionRefused as exc:
        print(f"不走了：{exc}")
        LOG.error("stopped: %s", exc)
        return 3
    finally:
        try:
            send_mode(MODE_MANUAL, args.dog)
            LOG.info("0x21010C02 sent - the gamepad has control again")
        except OSError as exc:
            LOG.error("could not hand control back: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
