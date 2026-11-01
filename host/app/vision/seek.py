"""Find the digit card when it is not straight ahead of the dog.

The head camera sees roughly 60-70 degrees horizontally, so a card lying to the
left, to the right or behind the dog is simply not in the picture.  Seeking is
therefore three small steps:

1. :func:`sweep_for_card` -- visit a fan of headings until ``find_card`` returns
   a box.  Nine stops 40 degrees apart cover a full turn with overlap.
2. :func:`centre_card` -- turn until the box sits near the middle of the frame.
   The camera's field of view is *not* calibrated: the controller starts from a
   conservative gain and halves it whenever a turn fails to shrink the offset,
   which makes it work for any lens without a measurement pass.
3. :func:`walk_to_width` -- the width-driven approach already proven on the dog
   (96 px reads as nothing, 120+ px reads at 0.97 confidence).

Nothing here touches hardware.  Each function takes callables, so the decision
layer is exercised off-robot in ``tests/test_seek.py`` while
``tools/seek_card.py`` supplies the real camera, the real executor and the real
classifier.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

#: Frames from the perception board are 640 px wide; the frame centre is where
#: a centred card must land.
FRAME_WIDTH = 640
CENTER_X = FRAME_WIDTH / 2.0

#: Headings relative to the start pose.  Alternating left/right keeps the sweep
#: near the dog's original facing for as long as possible, and 40 degree steps
#: leave overlap on both sides of a 60 degree field of view.
SWEEP_OFFSETS_DEG: Tuple[float, ...] = (0.0, 40.0, -40.0, 80.0, -80.0,
                                        120.0, -120.0, 160.0, -160.0)

#: Starting proportional gain, in degrees of body turn per pixel of offset.
#: 0.08 deg/px assumes a ~51 degree field of view over 640 px; the real lens is
#: a little wider, which only means the loop starts under-steering (safe) and
#: the measured response is used to correct it.
START_GAIN_DEG_PER_PX = 0.08

#: A turn this small would be swallowed by the executor's 3 degree tolerance,
#: so the loop stops instead of burning a motion command on it.
MIN_TURN_DEG = 3.0

Box = Tuple[int, int, int, int]


def box_center_x(box: Sequence[int]) -> float:
    return (float(box[0]) + float(box[2])) / 2.0


def box_width(box: Sequence[int]) -> int:
    return int(box[2]) - int(box[0])


def offset_px(box: Sequence[int], center_x: float = CENTER_X) -> float:
    """Signed pixels from the frame centre; positive means "card is to the right"."""
    return box_center_x(box) - center_x


@dataclass
class SweepResult:
    """Outcome of the heading fan."""

    found: bool
    reason: str
    heading_offset_deg: float = 0.0
    box: Optional[Box] = None
    stops: int = 0


@dataclass
class CentreResult:
    """Outcome of the turn-until-centred loop."""

    ok: bool
    reason: str
    box: Optional[Box] = None
    offset_px: float = 0.0
    gain_deg_per_px: float = START_GAIN_DEG_PER_PX
    turns: int = 0


@dataclass
class TargetResult:
    """Outcome of hunting for one particular digit among several cards."""

    found: bool
    reason: str
    seen: list = None          # [(heading_offset_deg, digits, confidence), ...]
    heading_offset_deg: float = 0.0
    box: Optional[Box] = None


@dataclass
class WalkResult:
    """Outcome of the width-driven approach."""

    ok: bool
    reason: str
    width_px: Optional[int] = None
    legs: int = 0


def sweep_for_card(look: Callable[[], Optional[Box]],
                   turn_to: Callable[[float], None],
                   offsets: Sequence[float] = SWEEP_OFFSETS_DEG) -> SweepResult:
    """Visit headings relative to the start pose until the card is in view.

    ``turn_to(offset_deg)`` rotates to ``start_heading + offset``; ``look()``
    returns a card box or ``None``.  A card within the field of view is found at
    the first stop that looks at it, so the fan is only walked as far as needed.
    """
    for stop, offset in enumerate(offsets, 1):
        turn_to(float(offset))
        box = look()
        if box is not None:
            return SweepResult(True, f"found at {offset:+.0f} deg", float(offset), box, stop)
    return SweepResult(False, "no card at any heading", stops=len(offsets))


def centre_card(look: Callable[[], Optional[Box]],
                turn_by: Callable[[float], None],
                gain_deg_per_px: float = START_GAIN_DEG_PER_PX,
                tolerance_px: float = 40.0,
                max_turns: int = 6,
                max_step_deg: float = 25.0,
                min_gain_deg_per_px: float = 0.005,
                centre_x: float = CENTER_X) -> CentreResult:
    """Turn until the card's centre is within ``tolerance_px`` of the middle.

    The gain is learned from the dog's own response: a turn that does not shrink
    the offset by at least 5% means the assumed degrees-per-pixel was too large
    (over-steer or a stalled turn), so it is halved and the next turn retries.
    """
    box = look()
    if box is None:
        return CentreResult(False, "card lost before centring", gain_deg_per_px=gain_deg_per_px)
    offset = offset_px(box, centre_x)

    for turn_no in range(1, max_turns + 1):
        if abs(offset) <= tolerance_px:
            return CentreResult(True, "already centred", box, offset, gain_deg_per_px, turn_no - 1)
        angle = -offset * gain_deg_per_px          # card on the right -> turn right
        angle = max(-max_step_deg, min(max_step_deg, angle))
        if abs(angle) < MIN_TURN_DEG:
            return CentreResult(True, "close enough", box, offset, gain_deg_per_px, turn_no - 1)
        turn_by(angle)

        new_box = look()
        if new_box is None:
            return CentreResult(False, "card lost while centring", box, offset,
                                gain_deg_per_px, turn_no)
        new_offset = offset_px(new_box, centre_x)
        if abs(new_offset) >= abs(offset) * 0.95 and gain_deg_per_px > min_gain_deg_per_px:
            gain_deg_per_px = max(gain_deg_per_px / 2.0, min_gain_deg_per_px)
        box, offset = new_box, new_offset

    ok = abs(offset) <= tolerance_px
    return CentreResult(ok, "centred" if ok else "did not converge", box, offset,
                        gain_deg_per_px, max_turns)


def hunt_for_digit(look: Callable[[], Optional[Box]],
                   centre: Callable[[Box], Optional[Box]],
                   read: Callable[[], Tuple[str, float]],
                   turn_to: Callable[[float], None],
                   want: str,
                   offsets: Sequence[float] = SWEEP_OFFSETS_DEG,
                   min_confidence: float = 0.85,
                   approach: Optional[Callable[[], bool]] = None,
                   max_legs_per_card: int = 2,
                   centre_before_read: bool = False) -> TargetResult:
    """Visit headings, read whatever card is there, stop on the one that matches.

    ``read()`` returns ``(digits, confidence)`` for the card in view.  A match is
    only accepted once its confidence clears ``min_confidence``.

    ``centre_before_read=False`` (the default) reads *before* aiming: the
    operator parks the dog squarely on a card, so aiming first produced a
    pointless "adjustment" turn at the very first stop (2026-10-28, m02320) and
    roughly doubled the time per stop.  Aiming is then reserved for a suspected
    hit -- the right digit with a shaky confidence -- and for the slow retry pass
    (``centre_before_read=True``) that only runs when the fast pass found nothing.
    """
    seen = []
    for offset in offsets:
        turn_to(float(offset))
        box = look()
        if box is None:
            continue

        if centre_before_read:
            lined_up = centre(box)
            if lined_up is not None:
                box = lined_up
        digits, confidence = read()

        # A card two metres away has no readable glyph, and a hasty reading is
        # exactly the "confident but wrong" case that made pencil look fine.
        # Walk closer before believing it -- but only when the reading was
        # unusable, never when the card clearly shows some other digit.
        legs = 0
        while (approach is not None and legs < max_legs_per_card
               and not (digits == want and confidence >= min_confidence)
               and (not digits or confidence < min_confidence)):
            if not approach():
                break
            legs += 1
            looked = look()
            if looked is not None:
                box = looked
            lined_up = centre(box)
            if lined_up is not None:
                box = lined_up
            digits, confidence = read()

        # Right digit, shaky confidence: line up properly and take another look
        # before rejecting.  Measured 2026-10-28: the "8" card read 8 @0.66 --
        # correct but under the 0.85 bar, so a genuine hit was thrown away.
        near_miss = digits == want
        for _ in range(2 if near_miss and confidence < min_confidence else 0):
            lined_up = centre(box)
            if lined_up is not None:
                box = lined_up
            again_digits, again_confidence = read()
            if again_digits == want and again_confidence > confidence:
                digits, confidence = again_digits, again_confidence
            if confidence >= min_confidence:
                break

        seen.append((float(offset), digits, float(confidence)))
        if digits == want and confidence >= min_confidence:
            return TargetResult(True, "found", seen, float(offset), box)

    if seen:
        reported = ", ".join(sorted({d or "?" for _o, d, _c in seen}))
        return TargetResult(False, "saw %s but not %s" % (reported, want), seen)
    return TargetResult(False, "no card in any heading", seen)


def walk_to_width(look_width: Callable[[], Optional[int]],
                  move_leg: Callable[[], bool],
                  target_px: int = 120,
                  max_px: int = 320,
                  max_legs: int = 6) -> WalkResult:
    """Step forward until the card is wide enough to read.

    ``look_width()`` returns the card's pixel width (``None`` when the card is
    not in view) and ``move_leg()`` returns ``False`` when the dog refuses or
    cannot move.  A card already wider than ``max_px`` stops the approach: past
    that point it starts falling out of the bottom of the frame.
    """
    width = look_width()
    for leg in range(1, max_legs + 1):
        if width is not None and width >= target_px:
            return WalkResult(True, "close enough to read", width, leg - 1)
        if not move_leg():
            return WalkResult(False, "walk refused", width, leg - 1)
        width = look_width()
        if width is not None and width > max_px:
            return WalkResult(False, "card too close, stopping", width, leg)
    ok = width is not None and width >= target_px
    return WalkResult(ok, "close enough to read" if ok else "still too small", width, max_legs)
