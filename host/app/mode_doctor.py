"""Mode doctor: measure what the Lite3 mode commands actually do.

Why this module exists
----------------------
The manual says ``0x21010C03`` selects autonomous mode (velocity commands own
the robot) and ``0x21010C02`` selects manual mode (the handheld remote owns
it).  On 2026-10-26 we sent both several times and the ``robot_motion_state``
field of the 0x0901 feedback frame stayed 0, yet a velocity command still
moved the dog.  So either the field does not mean what we think, or the
command needs a precondition/acknowledgement we have not found.

This module turns that confusion into a repeatable measurement:

* it never sends a velocity command, and never arms anything;
* the only frames it can send are the two mode codes (plus whatever heartbeat
  the transport already runs);
* every switch is bracketed by state snapshots, so the answer is "did any
  field move", not "did the dog do what I hoped".

The companion runbook section is ``docs/mode_protocol.md`` (experiments E1-E4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

#: The two mode codes this tool is allowed to send, and their labels.
MODE_FRAMES: Dict[int, str] = {
    0x21010C03: "autonomous",
    0x21010C02: "manual",
}

#: Fields worth watching across a mode switch.  The 0x0901 frame carries more,
#: but these are the ones a mode change could plausibly touch.
WATCHED_FIELDS: tuple = (
    "robot_motion_state",
    "basic_state",
    "error_state",
    "vel_body[3]",
)


class ModeDoctorError(RuntimeError):
    """Raised when a measurement cannot be made (no feedback, bad mode...)."""


@dataclass
class Sample:
    """One decoded 0x0901 frame, flattened for comparison."""

    t: float
    robot_motion_state: int
    basic_state: int
    error_state: int
    vel_x: float
    battery: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "t": round(self.t, 3),
            "robot_motion_state": self.robot_motion_state,
            "basic_state": self.basic_state,
            "error_state": self.error_state,
            "vel_x": round(self.vel_x, 4),
            "battery": round(self.battery, 1),
        }


def sample_from_state(state: Dict[str, Any], t: float) -> Sample:
    """Flatten a decoded frame into the few numbers this tool compares.

    ``state`` is what :func:`host.app.dog_state.decode_frame` returns: keys are
    the struct member names, so the velocity array is the string
    ``"vel_body[3]"``, not ``"vel_body"``.
    """
    vel = state.get("vel_body[3]") or (0.0, 0.0, 0.0)
    return Sample(
        t=t,
        robot_motion_state=int(state.get("robot_motion_state", -1)),
        basic_state=int(state.get("basic_state", -1)),
        error_state=int(state.get("error_state", -1)),
        vel_x=float(vel[0]),
        battery=float(state.get("battery_level", 0.0)),
    )


def summarize(samples: List[Sample]) -> Dict[str, Any]:
    """Return per-field first/last/values for a window of samples.

    The interesting question is not the average: it is "did this field ever
    leave the value it had before the switch", so the set of seen values is
    reported verbatim.
    """
    if not samples:
        return {"count": 0}
    first, last = samples[0], samples[-1]
    return {
        "count": len(samples),
        "robot_motion_state": [first.robot_motion_state, last.robot_motion_state],
        "robot_motion_state_values": sorted({s.robot_motion_state for s in samples}),
        "basic_state": [first.basic_state, last.basic_state],
        "basic_state_values": sorted({s.basic_state for s in samples}),
        "error_state_values": sorted({s.error_state for s in samples}),
        "vel_x_range": [
            round(min(s.vel_x for s in samples), 4),
            round(max(s.vel_x for s in samples), 4),
        ],
        "battery": round(last.battery, 1),
    }


@dataclass
class ModeDoctor:
    """Drive the E1 measurement: probe, switch, probe, switch back, probe.

    ``link`` only needs ``send(code, value)``; ``feed`` only needs
    ``latest()`` returning ``(state, age)``.  Both are injected so the whole
    measurement can be unit-tested without a dog.
    """

    link: Any
    feed: Any
    clock: Callable[[], float]
    sleep: Callable[[float], None] = None  # type: ignore[assignment]
    notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sleep is None:
            import time

            self.sleep = time.sleep

    # -- reading ---------------------------------------------------------
    def require_state(self, max_age_s: float = 1.0) -> Dict[str, Any]:
        state, age = self.feed.latest()
        if state is None or age is None or age > max_age_s:
            raise ModeDoctorError(
                "no fresh dog state (age=%s): is the 0x0901 relay running?"
                % ("never" if age is None else "%.2fs" % age)
            )
        return state

    def snapshot(self, seconds: float, hz: float = 10.0) -> Dict[str, Any]:
        """Sample the feedback for *seconds* and summarise it."""
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        period = 1.0 / hz
        samples: List[Sample] = []
        end = self.clock() + seconds
        while self.clock() < end:
            state = self.require_state()
            samples.append(sample_from_state(state, self.clock()))
            self.sleep(period)
        return summarize(samples)

    # -- acting ----------------------------------------------------------
    def switch(self, mode: str, wait_s: float = 3.0) -> Dict[str, Any]:
        """Send one mode frame and compare the state before and after.

        Only the two codes in :data:`MODE_FRAMES` are accepted; anything else
        (a stand command, a velocity command) cannot get through this method.
        """
        if mode not in ("autonomous", "manual"):
            raise ModeDoctorError("unknown mode %r (use autonomous/manual)" % (mode,))
        code = next(c for c, name in MODE_FRAMES.items() if name == mode)

        before = self.snapshot(2.0)
        self.link.send(code, 0)
        self.sleep(wait_s)
        after = self.snapshot(2.0)

        changed = before.get("robot_motion_state_values") != after.get(
            "robot_motion_state_values"
        )
        result = {
            "mode": mode,
            "code": "0x%08X" % code,
            "before": before,
            "after": after,
            "robot_motion_state_changed": changed,
        }
        self.notes.append(
            "switch to %s (0x%08X): robot_motion_state changed=%s"
            % (mode, code, changed)
        )
        return result

    def e1(self, wait_s: float = 3.0) -> Dict[str, Any]:
        """Experiment E1: does either mode code move any watched field?"""
        self.require_state()
        result = {
            "start": self.snapshot(2.0),
            "manual": self.switch("manual", wait_s=wait_s),
            "autonomous": self.switch("autonomous", wait_s=wait_s),
            "end": self.snapshot(2.0),
        }
        fields = ("robot_motion_state_values", "basic_state_values")
        result["any_change"] = any(
            result["start"].get(f) != result["end"].get(f) for f in fields
        )
        result["notes"] = list(self.notes)
        return result


def describe(result: Dict[str, Any]) -> str:
    """One-line reading of an E1 result, for the CLI and the log."""
    if result.get("any_change"):
        return "mode codes DID change a watched field - inspect the two switch blocks"
    return (
        "mode codes changed nothing we can see - treat robot_motion_state as "
        "unrelated to control ownership and verify by motion instead"
    )


__all__ = [
    "MODE_FRAMES",
    "WATCHED_FIELDS",
    "ModeDoctor",
    "ModeDoctorError",
    "Sample",
    "describe",
    "sample_from_state",
    "summarize",
]
