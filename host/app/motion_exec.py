"""Turn a task plan step into real dog motion, through the PL safety gate.

The only path that may command the dog is:

    this module -> pl_service on the ZYNQ (UDP 43902) -> PL safety gate -> dog

so every move is still bounded by the hardware watchdog and the gate's arm
register; if this process dies the gate closes within ``deadman_ms``.

Design rules
------------
* ``allow_motion`` must be passed explicitly; without it every move refuses
  (``docs/conventions.md`` section 1: report first, then act).
* A move is closed-loop on the dog's own ``pos_world`` (100 Hz), never on the
  requested duration: a blocked or slipping dog must not "succeed".
* Three independent abort conditions: state feed stale, timeout, distance
  reached.  Any of them ends with a stop command.
* ``stop()`` is sent in a ``finally`` block, so no exception path leaves a
  non-zero velocity behind.

Units are SI at this layer (m, m/s); ``pl_service`` converts to the integer
millimetre registers.
"""
from __future__ import annotations

import math
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from host.app.dog_state import (
    StateFeed,
    is_standing,
    position,
)

# One board position per 0.6 m of forward travel.  Replace with a taught map
# once the arena layout is fixed; the executor only needs the distance.
STATION_SPACING_M = 0.6


class MotionRefused(RuntimeError):
    """Raised when a move is not allowed to start (safety gate at software level)."""


@dataclass
class MoveResult:
    ok: bool
    reason: str
    target_m: float
    travelled_m: float = 0.0
    elapsed_s: float = 0.0
    commands: int = 0
    peak_speed: float = 0.0
    start_pos: Optional[List[float]] = None
    end_pos: Optional[List[float]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.__dict__)
        return out


@dataclass
class TurnResult:
    """Outcome of a closed-loop in-place rotation (see ``MotionExecutor.turn``)."""

    ok: bool
    reason: str
    target_deg: float
    turned_deg: float = 0.0
    elapsed_s: float = 0.0
    commands: int = 0
    start_yaw_deg: Optional[float] = None
    end_yaw_deg: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _yaw_deg(state: Dict[str, Any]) -> float:
    """World yaw in degrees from the 0x0901 frame's ``rpy[3]``.

    The frame carries angles in **degrees**, not radians: checked live on
    2026-10-27 against ``/leg_odom`` on 192.168.1.103 (quaternion yaw 53.70 deg
    while ``rpy[3][2]`` read 53.86). Converting again inflated the heading 57x
    and made every closed-loop turn oscillate. Do not add a conversion here.
    """
    return float(state["rpy[3]"][2])


def _wrap_deg(angle: float) -> float:
    """Fold an angle into (-180, 180] so a turn never takes the long way round."""
    wrapped = math.fmod(angle + 180.0, 360.0)
    if wrapped <= 0:
        wrapped += 360.0
    return wrapped - 180.0


class VelocitySender:
    """UDP client for the resident PL bridge (``pl_service``)."""

    def __init__(self, board: str = "192.168.1.100", port: int = 43902) -> None:
        self.addr = (board, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.commands = 0

    def send(self, vx: float, vy: float = 0.0, wz: float = 0.0) -> None:
        import json

        payload = json.dumps({
            "vx": int(round(vx * 1000)),
            "vy": int(round(vy * 1000)),
            "wz": int(round(wz * 1000)),
        }).encode()
        self.sock.sendto(payload, self.addr)
        self.commands += 1

    def stop(self) -> None:
        import json

        try:
            self.sock.sendto(json.dumps({"cmd": "stop"}).encode(), self.addr)
            self.commands += 1
        except OSError:
            pass

    def close(self) -> None:
        self.stop()
        try:
            self.sock.close()
        except OSError:
            pass


class MotionExecutor:
    """Closed-loop forward motion with explicit safety interlocks."""

    def __init__(
        self,
        sender: Any,
        feed: StateFeed,
        clock=time.monotonic,
        sleep=time.sleep,
        period_s: float = 0.05,
        response_window_s: float = 0.6,
        allow_motion: bool = False,
        min_battery: float = 20.0,
        max_stale_s: float = 0.35,
        max_speed: float = 0.15,
        max_distance_m: float = 1.0,
        allow_reverse: bool = False,
        max_wz: float = 0.6,
        max_turn_deg: float = 180.0,
        log=None,
    ) -> None:
        self.sender = sender
        self.feed = feed
        self.clock = clock
        self.sleep = sleep
        self.period_s = period_s
        self.response_window_s = response_window_s
        self.allow_motion = allow_motion
        self.min_battery = min_battery
        self.max_stale_s = max_stale_s
        self.max_speed = max_speed
        self.max_distance_m = max_distance_m
        self.allow_reverse = allow_reverse
        self.max_wz = max_wz
        self.max_turn_deg = max_turn_deg
        self.log = log
        self._stop_requested = False

    # -- preconditions ----------------------------------------------------
    def request_stop(self) -> None:
        """Ask the running move to stop at its next control period (thread-safe)."""
        self._stop_requested = True

    def preflight(self, wait_s: float = 0.0) -> Dict[str, Any]:
        """Check the dog can move. Returns a dict; raises when unsafe.

        ``wait_s`` tolerates transient state codes right after a control-mode
        switch (basic=9 was observed for a moment on 2026-10-26): the check is
        retried until it passes or the budget is spent.
        """
        deadline = self.clock() + max(0.0, float(wait_s))
        while True:
            try:
                return self._preflight_once()
            except MotionRefused:
                if self.clock() >= deadline:
                    raise
                self.sleep(0.2)

    def _preflight_once(self) -> Dict[str, Any]:
        state, age = self.feed.latest()
        if state is None:
            raise MotionRefused("no dog state: is the 0x0901 relay running?")
        if age is None or age > self.max_stale_s:
            raise MotionRefused("dog state is %.2f s old (limit %.2f)"
                                % (age or 0.0, self.max_stale_s))
        if not is_standing(state):
            raise MotionRefused("dog is not standing (basic=%s, %s)"
                                % (state["robot_basic_state"],
                                   state["robot_basic_state"]))
        if float(state["battery_level"]) < self.min_battery:
            raise MotionRefused("battery %.0f%% below %.0f%%"
                                % (state["battery_level"], self.min_battery))
        if int(state["error_state"]) != 0:
            raise MotionRefused("dog reports error_state=%s" % state["error_state"])
        return state

    # -- motion -------------------------------------------------------------
    def move(
        self,
        distance_m: float,
        speed: float = 0.10,
        timeout_s: Optional[float] = None,
        slack_m: float = 0.05,
        reverse: bool = False,
    ) -> MoveResult:
        """Drive forward for ``distance_m`` metres, closed-loop on pos_world."""
        if not self.allow_motion:
            raise MotionRefused("motion disabled: construct with allow_motion=True")
        if distance_m <= 0:
            raise MotionRefused("distance must be positive, got %.3f" % distance_m)
        if distance_m > self.max_distance_m:
            raise MotionRefused(
                "distance %.2f m above the %.2f m limit (operator rule 2026-10-26: "
                "autonomous moves stay under 1 m)" % (distance_m, self.max_distance_m)
            )
        if reverse and not self.allow_reverse:
            raise MotionRefused(
                "backward motion is disabled by default (operator rule 2026-10-26)"
            )
        if speed <= 0 or speed > self.max_speed:
            raise MotionRefused("speed %.3f outside (0, %.3f]" % (speed, self.max_speed))
        if timeout_s is None:
            timeout_s = distance_m / speed * 3.0 + 2.0

        # The feed was just created when a command arrives; give the first
        # frame a moment instead of refusing on a cold start (seen live
        # 2026-10-26 18:43: "no dog state" right after a fresh subscribe).
        waiter = getattr(self.feed, "wait_for_state", None)
        if waiter is not None:
            try:
                waiter(1.0)
            except RuntimeError as exc:
                raise MotionRefused("no dog state: is the 0x0901 relay running? (%s)"
                                    % exc)

        self._stop_requested = False
        start = self.preflight(wait_s=3.0 if timeout_s and timeout_s > 4.0 else 2.0)
        start_pos = list(position(start))
        t0 = self.clock()
        elapsed = 0.0
        travelled = 0.0
        commands = 0
        result = MoveResult(ok=False, reason="not started", target_m=distance_m,
                            start_pos=start_pos)
        try:
            while True:
                state, age = self.feed.latest()
                now = self.clock()
                elapsed = now - t0
                if state is None or age is None or age > self.max_stale_s:
                    result.reason = "state feed lost mid-move"
                    break
                if self._stop_requested:
                    result.reason = "stopped by operator"
                    break
                travelled = _distance(start, state)
                if travelled >= distance_m:
                    result.reason = "reached"
                    result.ok = True
                    break
                if elapsed > timeout_s:
                    result.reason = "timeout after %.1f s" % elapsed
                    break
                if float(state["battery_level"]) < self.min_battery:
                    result.reason = "battery dropped to %.0f%%" % state["battery_level"]
                    break
                # A dog in manual mode acknowledges nothing: commands flow,
                # velocity and position stay at zero.  Abort fast instead of
                # waiting for the timeout (seen live 2026-10-26 18:44).
                el = elapsed
                if (el >= self.response_window_s
                        and travelled < 0.01
                        and abs(float(state["vel_body[3]"][0])) < 0.05):
                    result.reason = ("dog is not responding to velocity commands "
                                     "(manual mode? nothing moved in %.1f s)" % el)
                    break
                if int(state["error_state"]) != 0:
                    result.reason = "dog fault: error_state=%s" % state["error_state"]
                    break
                self.sender.send(-speed if reverse else speed, 0.0, 0.0)
                commands += 1
                self.sleep(self.period_s)
        finally:
            self.sender.stop()

        state, _ = self.feed.latest()
        result.travelled_m = round(travelled, 3)
        result.elapsed_s = round(elapsed, 3)
        result.commands = commands
        result.peak_speed = speed if commands else 0.0
        result.end_pos = list(position(state)) if state is not None else None
        result.extra["slack_m"] = slack_m
        if self.log is not None:
            self.log(result)
        return result

    def move_reverse(self, distance_m: float, speed: float = 0.08) -> MoveResult:
        """Walk backwards, still closed-loop on pos_world (used to recycle space)."""
        return self.move(distance_m, speed=speed, reverse=True)

    def move_to_station(self, station: int, speed: float = 0.10) -> MoveResult:
        if station <= 0:
            raise MotionRefused("station must be positive, got %r" % (station,))
        return self.move(STATION_SPACING_M * station, speed=speed)

    # -- rotation -----------------------------------------------------------
    def _hold_command(self, state: Dict[str, Any], start_xy: tuple,
                      deadband_m: float = 0.02, gain_per_s: float = 2.5,
                      max_mps: float = 0.14) -> tuple:
        """Body-frame velocity that walks the dog back onto the spot it left.

        The gait translates while the body pivots (live 2026-10-28: ~0.3 m of
        backwards drift over one turn).  Errors under ``deadband_m`` send
        nothing -- the state feed's position noise alone is a few millimetres --
        and larger ones are proportional and clipped to ``max_mps`` so a big
        excursion is walked back gently instead of bolted back.
        """
        cx, cy = position(state)[:2]
        ex = start_xy[0] - cx
        ey = start_xy[1] - cy
        if math.hypot(ex, ey) < deadband_m:
            return (0.0, 0.0)
        yaw = math.radians(_yaw_deg(state))
        cos_y, sin_y = math.cos(yaw), math.sin(yaw)
        vx = gain_per_s * (ex * cos_y + ey * sin_y)
        vy = gain_per_s * (-ex * sin_y + ey * cos_y)
        return (max(-max_mps, min(max_mps, vx)),
                max(-max_mps, min(max_mps, vy)))

    def turn(self, angle_deg: float, max_wz: float = 0.6,
             tolerance_deg: float = 3.0, timeout_s: Optional[float] = None,
             settle_cycles: int = 3, hold_position: bool = True) -> "TurnResult":
        """Rotate in place by ``angle_deg`` (+ = counter-clockwise), closed-loop.

        The absolute heading is taken from ``rpy[3][2]``; the loop stops once the
        heading error has stayed inside ``tolerance_deg`` for ``settle_cycles``
        control periods, so residual body sway does not end the turn early.

        ``hold_position`` keeps the spot the turn started on: the dog's own gait
        walks it backwards while it pivots (seen live 2026-10-28 -- 0.3 m of
        drift over a turn, which is what forced a "walk back" before every
        read).  The correction is a small body-frame velocity built from the
        world-frame position error, so it costs nothing when the dog stays put
        and stays inside ``hold_max_mps`` when it does not.
        """
        if not self.allow_motion:
            raise MotionRefused("motion disabled: construct with allow_motion=True")
        if angle_deg == 0:
            raise MotionRefused("angle must be non-zero")
        if abs(angle_deg) > self.max_turn_deg:
            raise MotionRefused(
                "turn of %.0f deg above the %.0f deg limit (operator rule 2026-10-26)"
                % (abs(angle_deg), self.max_turn_deg)
            )
        if max_wz <= 0 or max_wz > self.max_wz:
            raise MotionRefused("wz %.3f outside (0, %.3f]" % (max_wz, self.max_wz))

        waiter = getattr(self.feed, "wait_for_state", None)
        if waiter is not None:
            try:
                waiter(1.0)
            except RuntimeError as exc:
                raise MotionRefused("no dog state: is the 0x0901 relay running? (%s)"
                                    % exc)

        self._stop_requested = False
        start = self.preflight(wait_s=2.0)
        start_yaw = _yaw_deg(start)
        start_xy = position(start)[:2]
        target_yaw = start_yaw + float(angle_deg)
        if timeout_s is None:
            timeout_s = abs(angle_deg) / 45.0 * 6.0 + 3.0

        t0 = self.clock()
        elapsed = 0.0
        commands = 0
        inside = 0
        turned = 0.0
        result = TurnResult(ok=False, reason="not started", target_deg=angle_deg,
                            start_yaw_deg=round(start_yaw, 1))
        try:
            while True:
                state, age = self.feed.latest()
                elapsed = self.clock() - t0
                if state is None or age is None or age > self.max_stale_s:
                    result.reason = "state feed lost mid-turn"
                    break
                if self._stop_requested:
                    result.reason = "stopped by operator"
                    break
                error = _wrap_deg(target_yaw - _yaw_deg(state))
                turned = _wrap_deg(_yaw_deg(state) - start_yaw)
                if abs(error) <= tolerance_deg:
                    inside += 1
                    if inside >= settle_cycles:
                        result.reason = "reached"
                        result.ok = True
                        break
                else:
                    inside = 0
                if elapsed > timeout_s:
                    result.reason = "timeout after %.1f s" % elapsed
                    break
                if float(state["battery_level"]) < self.min_battery:
                    result.reason = "battery dropped to %.0f%%" % state["battery_level"]
                    break
                if int(state["error_state"]) != 0:
                    result.reason = "dog fault: error_state=%s" % state["error_state"]
                    break
                if (elapsed >= self.response_window_s
                        and abs(turned) < 2.0
                        and abs(float(state["rpy_vel[3]"][2])) < 0.05):
                    result.reason = ("dog is not responding to rotation commands "
                                     "(manual mode? nothing turned in %.1f s)" % elapsed)
                    break
                wz = max(-max_wz, min(max_wz, 2.0 * math.radians(error)))
                vx, vy = (self._hold_command(state, start_xy)
                          if hold_position else (0.0, 0.0))
                self.sender.send(vx, vy, wz)
                commands += 1
                self.sleep(self.period_s)
        finally:
            self.sender.stop()

        state, _ = self.feed.latest()
        result.turned_deg = round(turned, 1)
        result.elapsed_s = round(elapsed, 3)
        result.commands = commands
        result.end_yaw_deg = round(_yaw_deg(state), 1) if state is not None else None
        if self.log is not None:
            self.log(result)
        return result

    def turn_to_heading(self, heading_deg: float, max_wz: float = 0.6,
                        tolerance_deg: float = 3.0) -> "TurnResult":
        """Rotate to an absolute world heading, relative to the current one."""
        state = self.preflight(wait_s=2.0)
        delta = _wrap_deg(float(heading_deg) - _yaw_deg(state))
        if abs(delta) < tolerance_deg:
            return TurnResult(ok=True, reason="already there",
                              target_deg=round(delta, 1),
                              start_yaw_deg=round(_yaw_deg(state), 1),
                              end_yaw_deg=round(_yaw_deg(state), 1))
        return self.turn(delta, max_wz=max_wz, tolerance_deg=tolerance_deg)

    def move_to_point(self, dx_m: float, dy_m: float, speed: float = 0.10,
                      max_wz: float = 0.6) -> "List[object]":
        """Drive to a point relative to the current pose: turn, then move.

        This is the building block for "left-front / right-front" stations:
        a station is a point in the body frame at the moment of the command,
        not a distance along the current heading.
        """
        distance = math.hypot(float(dx_m), float(dy_m))
        if distance <= 0:
            raise MotionRefused("target point must not be the current position")
        if distance > self.max_distance_m:
            raise MotionRefused(
                "distance %.2f m above the %.2f m limit (operator rule 2026-10-26: "
                "autonomous moves stay under 1 m)" % (distance, self.max_distance_m)
            )
        if dx_m < -1e-6 and not self.allow_reverse:
            raise MotionRefused(
                "backward motion is disabled by default (operator rule 2026-10-26)"
            )
        # yaw of the target point in the body frame; the robot yaw sign
        # convention is +left (counter-clockwise, right-handed z-up).
        heading_deg = math.degrees(math.atan2(float(dy_m), float(dx_m)))
        turned = self.turn_to_heading(_yaw_deg(self.preflight(wait_s=2.0)) + heading_deg,
                                      max_wz=max_wz)
        walked = self.move(distance, speed=speed)
        return [turned, walked]


def _distance(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    pa, pb = position(a), position(b)
    return math.sqrt(sum((pb[i] - pa[i]) ** 2 for i in range(3)))
