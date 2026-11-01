"""Unit tests for the motion executor (no dog, no board, no network).

The fake feed integrates the velocity the fake sender wrote, so a closed-loop
move can be simulated end to end; the clock is manual, so timeouts take no
wall-clock time.

    python -m pytest tests/test_motion_exec.py -q
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.app.dog_state import StateFeed  # noqa: E402
from host.app.motion_exec import (  # noqa: E402
    STATION_SPACING_M,
    MotionExecutor,
    MotionRefused,
)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


class FakeFeed:
    """State source whose position is driven by whatever the sender last wrote."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.pos = [0.0, 0.0, 0.313]
        self.vel = 0.0
        self.battery = 90.0
        self.basic = 6
        self.error = 0
        self.stale = False
        self.last_t = clock.t

    def wait_for_state(self, timeout_s: float = 1.0):
        return self._state()

    def _integrate(self) -> None:
        dt = self.clock.t - self.last_t
        if dt > 0:
            self.pos[0] += self.vel * dt
            self.last_t = self.clock.t

    def _state(self) -> dict:
        return {
            "robot_basic_state": self.basic,
            "battery_level": self.battery,
            "error_state": self.error,
            "pos_world[3]": tuple(self.pos),
            "vel_body[3]": (self.vel, 0.0, 0.0),
        }

    def latest(self):
        if self.stale:
            return self._state(), 9.9
        self._integrate()
        return self._state(), 0.005


class FakeSender:
    def __init__(self, feed: FakeFeed) -> None:
        self.feed = feed
        self.commands = 0
        self.stops = 0
        self.last = (0.0, 0.0, 0.0)
        self.on_send = None

    def send(self, vx: float, vy: float = 0.0, wz: float = 0.0) -> None:
        self.commands += 1
        self.last = (vx, vy, wz)
        self.feed.vel = vx
        if self.on_send is not None:
            self.on_send(self.commands)

    def stop(self) -> None:
        self.stops += 1
        self.last = (0.0, 0.0, 0.0)
        self.feed.vel = 0.0


@pytest.fixture()
def rig():
    clock = FakeClock()
    feed = FakeFeed(clock)
    sender = FakeSender(feed)
    ex = MotionExecutor(sender, feed, clock=clock, sleep=clock.sleep, allow_motion=True,
                        allow_reverse=True)
    return ex, sender, feed, clock


def test_is_a_state_feed_subclass_contract(rig):
    # The executor only needs latest(); make sure the real class exposes it.
    assert hasattr(StateFeed, "latest")


def test_reaches_target_and_stops(rig):
    ex, sender, feed, clock = rig
    res = ex.move(0.20, speed=0.10)
    assert res.ok is True
    assert res.reason == "reached"
    assert res.travelled_m >= 0.20
    assert res.travelled_m < 0.20 + 0.02
    assert sender.stops >= 1
    assert sender.last == (0.0, 0.0, 0.0)
    assert res.commands > 0


def test_refuses_without_permission(rig):
    _ex, sender, feed, clock = rig
    ex = MotionExecutor(sender, feed, clock=clock, sleep=clock.sleep, allow_motion=False)
    with pytest.raises(MotionRefused):
        ex.move(0.1)
    assert sender.commands == 0


def test_refuses_when_not_standing(rig):
    ex, sender, feed, clock = rig
    feed.basic = 0  # lying
    with pytest.raises(MotionRefused, match="not standing"):
        ex.move(0.1)


def test_refuses_when_state_missing(rig):
    ex, sender, feed, clock = rig
    feed.stale = True
    with pytest.raises(MotionRefused, match="old"):
        ex.move(0.1)


def test_aborts_when_feed_goes_stale(rig):
    ex, sender, feed, clock = rig

    def die_after(n):
        if n == 4:
            feed.stale = True

    sender.on_send = die_after
    res = ex.move(0.40, speed=0.10)
    assert res.ok is False
    assert "state feed lost" in res.reason
    assert sender.last == (0.0, 0.0, 0.0)


def test_aborts_on_low_battery(rig):
    ex, sender, feed, clock = rig

    def drain(n):
        if n == 3:
            feed.battery = 12.0

    sender.on_send = drain
    res = ex.move(0.40, speed=0.10)
    assert res.ok is False
    assert "battery" in res.reason
    assert sender.last == (0.0, 0.0, 0.0)


def test_timeout_when_dog_does_not_move(rig):
    ex, sender, feed, clock = rig

    def block(n):
        feed.vel = 0.0  # wheels slip / dog blocked, position never advances

    sender.on_send = block
    res = ex.move(0.30, speed=0.10, timeout_s=2.0)
    assert res.ok is False
    assert "not responding" in res.reason or "timeout" in res.reason
    assert res.travelled_m == 0.0


def test_stop_is_sent_even_if_sender_raises(rig):
    ex, sender, feed, clock = rig

    def boom(n):
        if n == 2:
            raise OSError("network down")

    sender.on_send = boom
    with pytest.raises(OSError):
        ex.move(0.30, speed=0.10)
    assert sender.stops == 1
    assert sender.last == (0.0, 0.0, 0.0)


def test_station_mapping(rig):
    ex, sender, feed, clock = rig
    res = ex.move_to_station(1, speed=0.10)
    assert res.ok is True
    assert STATION_SPACING_M == pytest.approx(0.6)
    assert res.target_m == pytest.approx(0.6)
    assert res.travelled_m >= 0.6


def test_speed_limits(rig):
    ex, sender, feed, clock = rig
    with pytest.raises(MotionRefused, match="speed"):
        ex.move(0.1, speed=0.9)
    with pytest.raises(MotionRefused, match="speed"):
        ex.move(0.1, speed=0.0)


class ColdFeed(FakeFeed):
    """No frame yet on the first call: reproduces a fresh subscribe."""

    def __init__(self, clock):
        super().__init__(clock)
        self.ready = False

    def wait_for_state(self, timeout_s: float = 1.0):
        self.ready = True
        return self._state()

    def latest(self):
        if not self.ready:
            return None, None
        return super().latest()


def test_cold_feed_is_waited_for(rig):
    _ex, _sender, _feed, clock = rig
    feed = ColdFeed(clock)
    sender = FakeSender(feed)
    ex = MotionExecutor(sender, feed, clock=clock, sleep=clock.sleep, allow_motion=True)
    res = ex.move(0.10, speed=0.10)
    assert res.ok is True


def test_cold_feed_that_never_wakes_is_refused(rig):
    _ex, sender, _feed, clock = rig

    class Silent(FakeFeed):
        def wait_for_state(self, timeout_s: float = 1.0):
            raise RuntimeError("no state frames")

    ex = MotionExecutor(sender, Silent(clock), clock=clock, sleep=clock.sleep, allow_motion=True)
    with pytest.raises(MotionRefused, match="no dog state"):
        ex.move(0.10)


def test_reverse_move_uses_negative_velocity(rig):
    ex, sender, feed, clock = rig
    seen = []
    orig = sender.send

    def spy(vx, vy=0.0, wz=0.0):
        seen.append(vx)
        orig(vx, vy, wz)

    sender.send = spy
    res = ex.move_reverse(0.10, speed=0.08)
    assert res.ok is True
    assert seen and all(v < 0 for v in seen)
    assert sender.last == (0.0, 0.0, 0.0)


def test_long_distance_is_refused_by_default(rig):
    ex, _sender, _feed, _clock = rig
    with pytest.raises(MotionRefused) as exc:
        ex.move(1.5)
    assert "limit" in str(exc.value)


def test_reverse_is_refused_unless_enabled(rig):
    ex, _sender, _feed, _clock = rig
    ex.allow_reverse = False
    with pytest.raises(MotionRefused) as exc:
        ex.move(0.2, reverse=True)
    assert "backward" in str(exc.value)


# -- rotation (P0: turning is what makes "left/right stations" possible) -----
class TurnFeed(FakeFeed):
    """Like FakeFeed, but integrates yaw from the wz the sender wrote."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.yaw = 0.0
        self.wz = 0.0
        self.rpy_vel = (0.0, 0.0, 0.0)
        self.vel_side = 0.0

    def _integrate(self) -> None:
        dt = self.clock.t - self.last_t
        super()._integrate()  # position from the linear velocity
        if dt > 0:
            # The 0x0901 frame carries angles in degrees, so the fake body
            # integrates the (rad/s) command into a degree-valued heading.
            self.yaw += math.degrees(self.wz) * dt
            self.rpy_vel = (0.0, 0.0, self.wz)
            self.last_t = self.clock.t

    def _state(self) -> dict:
        return {
            "robot_basic_state": self.basic,
            "battery_level": self.battery,
            "error_state": self.error,
            "pos_world[3]": tuple(self.pos),
            "vel_body[3]": (self.vel, 0.0, 0.0),
            "rpy[3]": (0.0, 0.0, self.yaw),
            "rpy_vel[3]": tuple(self.rpy_vel),
        }


def _turn_setup(allow_motion: bool = True, **kwargs):
    clock = FakeClock()
    feed = TurnFeed(clock)
    sender = FakeSender(feed)

    def send(vx, vy=0.0, wz=0.0):
        sender.commands += 1
        sender.last = (vx, vy, wz)
        feed.vel = vx
        feed.wz = wz

    sender.send = send
    ex = MotionExecutor(sender, feed, clock=clock, sleep=clock.sleep,
                        allow_motion=allow_motion, **kwargs)
    return ex, feed, sender


def test_turn_refused_without_allow_motion():
    ex, _feed, _sender = _turn_setup(allow_motion=False)
    with pytest.raises(MotionRefused):
        ex.turn(90)


def test_turn_refused_above_the_angle_limit():
    ex, _feed, _sender = _turn_setup(max_turn_deg=90.0)
    with pytest.raises(MotionRefused):
        ex.turn(120)


def test_turn_rotates_by_the_requested_angle():
    ex, feed, sender = _turn_setup()
    result = ex.turn(45.0)
    assert result.ok, result.reason
    assert result.reason == "reached"
    assert abs(result.turned_deg - 45.0) <= 4.0
    assert sender.stops >= 1, "a turn must always end with a stop"


def test_turn_wraps_the_long_way_round():
    ex, feed, sender = _turn_setup()
    result = ex.turn(-170.0)
    assert result.ok, result.reason
    assert abs(abs(result.turned_deg) - 170.0) <= 5.0


def test_turn_refuses_when_the_body_does_not_move():
    ex, feed, sender = _turn_setup()
    sender.send = lambda vx, vy=0.0, wz=0.0: None  # acknowledge nothing
    result = ex.turn(90.0)
    assert not result.ok
    assert "not responding to rotation" in result.reason


def test_move_to_point_turns_then_walks():
    ex, feed, sender = _turn_setup(max_distance_m=2.0)
    turned, walked = ex.move_to_point(0.0, 0.5)
    assert turned.ok and walked.ok, (turned.reason, walked.reason)
    assert abs(abs(turned.turned_deg) - 90.0) <= 6.0
    assert abs(walked.travelled_m - 0.5) <= 0.06


def test_move_to_point_refuses_backward_without_reverse():
    ex, _feed, _sender = _turn_setup(max_distance_m=2.0)
    with pytest.raises(MotionRefused):
        ex.move_to_point(-0.4, 0.0)


def test_move_to_point_refuses_beyond_the_one_metre_rule():
    ex, _feed, _sender = _turn_setup()
    with pytest.raises(MotionRefused):
        ex.move_to_point(0.0, 1.2)


class DriftingTurnFeed(TurnFeed):
    """A dog whose gait walks it backwards while it pivots (live 2026-10-28).

    Over a 90 deg turn the real dog slid ~0.3 m backwards; this fake reproduces
    that drift so the hold-position loop has something to correct.
    """

    drift_per_s = 0.10

    def _integrate(self) -> None:
        dt = self.clock.t - self.last_t
        if dt <= 0:
            return
        # The body moves along its own forward/left axes, so a correction has
        # to be rotated into world coordinates exactly like the real dog.
        yaw = math.radians(self.yaw)
        cos_y, sin_y = math.cos(yaw), math.sin(yaw)
        vx, vy = self.vel, self.vel_side
        self.pos[0] += (vx * cos_y - vy * sin_y) * dt
        self.pos[1] += (vx * sin_y + vy * cos_y) * dt
        self.yaw += math.degrees(self.wz) * dt
        self.rpy_vel = (0.0, 0.0, self.wz)
        self.last_t = self.clock.t
        if abs(self.wz) > 1e-6:
            self.pos[0] -= self.drift_per_s * dt


def _drifting_setup():
    clock = FakeClock()
    feed = DriftingTurnFeed(clock)
    sender = FakeSender(feed)

    def send(vx, vy=0.0, wz=0.0):
        sender.commands += 1
        sender.last = (vx, vy, wz)
        feed.vel = vx
        feed.vel_side = vy
        feed.wz = wz

    sender.send = send
    ex = MotionExecutor(sender, feed, clock=clock, sleep=clock.sleep,
                        allow_motion=True)
    return ex, feed, sender


def test_turn_holds_its_ground_against_the_gait_drift():
    ex, feed, sender = _drifting_setup()
    start = tuple(feed.pos)
    result = ex.turn(90.0)
    assert result.ok, result.reason
    drifted = abs(feed.pos[0] - start[0])
    assert drifted < 0.06, "hold-position should leave it within 6 cm, got %.3f m" % drifted


def test_turn_without_hold_lets_the_dog_wander():
    ex, feed, sender = _drifting_setup()
    start = tuple(feed.pos)
    result = ex.turn(90.0, hold_position=False)
    assert result.ok, result.reason
    assert abs(feed.pos[0] - start[0]) > 0.10, "without the hold the fake must drift"


def test_hold_sends_a_body_frame_correction_towards_the_start():
    ex, feed, sender = _drifting_setup()
    seen = []
    base = sender.send

    def spy(vx, vy=0.0, wz=0.0):
        seen.append((vx, vy, wz))
        base(vx, vy, wz)

    sender.send = spy
    ex.turn(90.0)
    # The drift is backwards (-x); heading stays ~0 for a small part of the
    # turn, so the correction must have a positive forward component somewhere.
    assert any(vx > 0.005 for (vx, _vy, _wz) in seen), seen[:10]
