"""Turn the digit card in front of the camera into a sentence the dog can say.

This is the bridge between the vision half (``digit_reader``) and the dialogue
half (``pipeline`` / ``agent`` / ``tools/voice_agent.py``): it grabs a short
burst of frames, votes over the per-frame readings and returns both the raw
result (for the log and the JSON contract) and the line to speak.

    from vision.digit_service import read_card, spoken
    result = read_card(shots=3)
    LOG.info("read: %s", result.to_dict())
    speak(spoken(result))

One burst is deliberate: a single frame can catch the card mid-glare or with
the glyph half out of the ROI, while three frames plus the shared ``card_hint``
keep the reading self-consistent (see runbook_digit.md sections 10-11).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import time
from typing import Callable, List, Optional

from host.app.vision.digit_reader import (
    find_card,
    load_classifier,
    read_digits,
    read_digits_robust,
    to_gray,
)

DEFAULT_SHOTS = 3
#: Below this share of frames agreeing on one reading we say "unclear" instead
#: of asserting a digit: a wrong digit is worse than an honest "I can't tell".
MIN_AGREEMENT = 0.60

#: Real-run finding (2026-10-26 21:09): reading 0.7 s after the walk ended
#: returned "1" three times in a row with confidence 0.796 (the card said 2);
#: the same card read "2" at confidence 0.984 once the body had settled.  So a
#: reading is only trusted after the body has been still for `SETTLE_S`.
SETTLE_S = 1.5
# The 0x0901 frame reports a constant ~0.024 m/s on |vel_body| while the dog is
# standing perfectly still (measured 4 s, min = p50 = p90 = max = 0.0240 on
# 2026-10-27), so a 0.02 threshold could never be met and every read silently
# waited out the full timeout. 0.05 keeps detecting a real walk (0.10 m/s) while
# letting a parked dog settle.
STILL_VELOCITY_MPS = 0.05
SETTLE_TIMEOUT_S = 6.0
#: One extra burst when the first one is not confident enough.
RETRIES = 1
RETRY_DELAY_S = 1.0
MIN_CONFIDENT = 0.90
#: A stop-then-shoot burst: several frames close together, then keep the usable
#: ones (see frame_quality).  Four frames at 0.15 s cover the tail of the body's
#: settle wobble without making the operator wait.
BURST_SHOTS = 4
BURST_GAP_S = 0.15
BURST_TOP_K = 3


@dataclass
class ReadResult:
    """One card reading, as logged and as reported to the operator."""

    digits: str = ""
    confidence: float = 0.0
    agreement: float = 0.0
    shots: int = 0
    reason: str = "empty"  # ok | unclear | empty | unreachable | blurred

    def to_dict(self) -> dict:
        return {
            "digits": self.digits,
            "confidence": round(self.confidence, 3),
            "agreement": round(self.agreement, 3),
            "shots": self.shots,
            "reason": self.reason,
        }


def _default_grab() -> Callable[[], object]:
    """The live transport, imported lazily so tests never touch a socket."""
    from host.app.vision.camera_client import (
        CAMERA_HOST,
        CAMERA_PORT,
        grab_frame,
    )

    def grab():  # noqa: F811 - default transport
        return grab_frame(CAMERA_HOST, CAMERA_PORT)

    return grab


def read_card(shots: int = DEFAULT_SHOTS,
              grab: Optional[Callable[[], object]] = None,
              classifier=None,
              frames: Optional[List[object]] = None) -> ReadResult:
    """Read the card over ``shots`` frames and vote.

    ``grab`` and ``classifier`` are injected in tests; ``frames`` lets a caller
    that already grabbed a burst (e.g. the live tool) reuse it instead of
    hitting the camera twice.
    """
    if classifier is None:
        classifier = load_classifier()
    if classifier is None:
        return ReadResult(shots=0, reason="empty")

    if frames is None:
        if grab is None:
            grab = _default_grab()

        frames = []
        transport_error = None
        for _ in range(max(1, int(shots))):
            try:
                frames.append(grab())
            except Exception as exc:  # pragma: no cover - transport failures
                import logging

                transport_error = exc
                logging.getLogger(__name__).error("camera grab failed: %s", exc)
                break

    votes: Counter = Counter()
    scores: dict = {}
    card_hint = None
    for frame in frames:
        card = find_card(to_gray(frame)) if frame is not None else None
        if card is not None:
            card_hint = card
        if frame is None:
            continue
        # Plain reader: on the 52-case stress set the rotation sweep scores
        # 31/52 and the plain reader 44/52, so the sweep is opt-in only
        # (read_digits_escalate) and never the default path.
        text, details = read_digits(frame, classifier, card_hint=card_hint)
        if not text:
            continue
        votes[text] += 1
        scores.setdefault(text, []).append(min((d[1] for d in details), default=0.0))

    total = max(1, len(frames))
    if not votes:
        reason = "unreachable" if not frames and transport_error is not None else "empty"
        return ReadResult(shots=len(frames), reason=reason)

    digits, count = votes.most_common(1)[0]
    agreement = count / float(total)
    confidence = sum(scores[digits]) / float(len(scores[digits]))
    reason = "ok" if agreement >= MIN_AGREEMENT else "unclear"
    return ReadResult(digits=digits, confidence=confidence, agreement=agreement,
                      shots=len(frames), reason=reason)


def read_burst(grab: Optional[Callable[[], object]] = None,
               shots: int = BURST_SHOTS,
               gap: float = BURST_GAP_S,
               classifier=None,
               top_k: int = BURST_TOP_K,
               sleep: Callable[[float], None] = time.sleep,
               **quality_kwargs) -> ReadResult:
    """Grab a short burst, drop the unusable frames, vote over the best ones.

    This is the "stop, then shoot" reading: several frames are taken close
    together, each is scored (card present, big enough, fully in frame, sharp -
    see ``frame_quality``), and only the best ``top_k`` usable frames are voted
    on.  When *every* frame is unusable the result is "blurred" rather than a
    guess, because a blurred burst agrees with itself and produces a confident
    wrong digit (measured: `3` reads as `2` at confidence 0.985 once blurred).
    """
    from host.app.vision.frame_quality import pick_best

    if grab is None:
        grab = _default_grab()

    frames: List[object] = []
    transport_error = None
    for index in range(max(1, int(shots))):
        try:
            frames.append(grab())
        except Exception as exc:  # pragma: no cover - transport failures
            import logging

            transport_error = exc
            logging.getLogger(__name__).error("camera grab failed: %s", exc)
            break
        if index + 1 < shots:
            sleep(gap)

    if not frames:
        return ReadResult(shots=0, reason="unreachable" if transport_error else "empty")

    chosen, qualities = pick_best(frames, top_k=top_k, **quality_kwargs)
    if not chosen:
        saw_card = any(getattr(quality, "has_card", False) for quality in qualities)
        return ReadResult(shots=len(frames), reason="blurred" if saw_card else "empty")
    return read_card(frames=[frames[index] for index in chosen], classifier=classifier)


def wait_until_still(feed, epsilon: float = STILL_VELOCITY_MPS,
                     settle_s: float = SETTLE_S,
                     timeout_s: float = SETTLE_TIMEOUT_S,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> bool:
    """Wait until the body has been still for `settle_s`, bounded by `timeout_s`.

    Returns True when the body settled (or when no state feed was supplied, in
    which case `settle_s` is simply slept), False when it was still moving at
    the timeout.  Callers read the card either way; the flag is for the log.
    """
    if feed is None:
        sleep(settle_s)
        return True
    deadline = clock() + timeout_s
    quiet_since = None
    while clock() < deadline:
        state, age = feed.latest()
        moving = True
        if state is not None and age < 0.35:
            # Real frames carry the field as "vel_body[3]" (see dog_state).
            vel = state.get("vel_body[3]") or state.get("vel_body") or (0.0, 0.0, 0.0)
            moving = (vel[0] ** 2 + vel[1] ** 2 + vel[2] ** 2) ** 0.5 > epsilon
        if moving:
            quiet_since = None
        else:
            if quiet_since is None:
                quiet_since = clock()
            if clock() - quiet_since >= settle_s:
                return True
        sleep(0.05)
    return False


def _quality(result: ReadResult) -> float:
    """Rank two readings: a confident digit beats a hesitant one."""
    if not result.digits or result.reason == "empty":
        return -1.0
    return float(result.confidence) * float(result.agreement)


def read_settled(read_once: Callable[[], ReadResult],
                 feed=None,
                 settle_s: float = SETTLE_S,
                 retries: int = RETRIES,
                 retry_delay_s: float = RETRY_DELAY_S,
                 min_confidence: float = MIN_CONFIDENT,
                 min_agreement: float = MIN_AGREEMENT,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> ReadResult:
    """Read the card after the body has settled, retrying once when unsure.

    A moving camera produces *consistently wrong* readings (see the `SETTLE_S`
    note above), so waiting is the fix; a second burst covers glare and stray
    blobs when the first one is not confident enough.
    """
    wait_until_still(feed, settle_s=settle_s, sleep=sleep, clock=clock)
    best = None
    attempts = max(0, int(retries)) + 1
    for attempt in range(attempts):
        result = read_once()
        if best is None or _quality(result) > _quality(best):
            best = result
        if (result.reason == "ok"
                and result.confidence >= min_confidence
                and result.agreement >= min_agreement):
            return result
        if attempt + 1 < attempts:
            sleep(retry_delay_s)
    return best  # type: ignore[return-value]


def spoken(result: ReadResult) -> str:
    """The line the dog says after one reading.

    Three cases, because the operator must be able to tell them apart by ear:
    a confident digit, a hesitant guess, and "there is no card in my view".
    """
    from host.app.pipeline import CN_DIGITS

    digits = result.digits or ""
    cased = "".join(CN_DIGITS[int(ch)] for ch in digits if ch.isdigit())

    if result.reason == "unreachable":
        return "我的相机没连上，请检查一下网络"
    if result.reason == "blurred":
        return "有点糊，我再看一眼"
    if result.reason == "empty" or not digits:
        return "我没看到数字，能再靠近一点吗？"
    if result.reason == "unclear":
        return "我看到有点像{}，但不太确定".format(cased or digits)
    return "板子上写的是" + (cased or digits)
