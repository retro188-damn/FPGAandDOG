"""Tests for the mode doctor (E1 measurement helper).

The tool sends mode frames to a 20 kg robot, so its safety properties are
tested with fakes rather than by trusting the CLI:

* it can only ever send the two documented mode codes;
* it refuses to measure when no fresh feedback is available;
* a field change caused by a switch is actually detected.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.app.mode_doctor import (  # noqa: E402
    MODE_FRAMES,
    ModeDoctor,
    ModeDoctorError,
    describe,
    sample_from_state,
    summarize,
)

MANUAL = 0x21010C02
AUTO = 0x21010C03


def make_state(**over):
    state = {
        "robot_motion_state": 0,
        "basic_state": 6,
        "error_state": 0,
        "vel_body[3]": (0.0, 0.0, 0.0),
        "battery_level": 61.0,
    }
    state.update(over)
    return state


class FakeFeed:
    def __init__(self, state=None, age=0.05):
        self.state = state if state is not None else make_state()
        self.age = age
        self.calls = 0

    def latest(self):
        self.calls += 1
        return self.state, self.age


class FakeLink:
    def __init__(self, feed=None, on_send=None):
        self.feed = feed
        self.sent = []
        self._on_send = on_send

    def send(self, code, value=0):
        self.sent.append((code, value))
        if self._on_send:
            self._on_send(code, self.feed)


class FakeClock:
    def __init__(self, step=0.1):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def make_doctor(feed=None, on_send=None):
    feed = feed or FakeFeed()
    link = FakeLink(feed=feed, on_send=on_send)
    clock = FakeClock()
    doctor = ModeDoctor(link=link, feed=feed, clock=clock, sleep=lambda s: None)
    return doctor, link, feed


# --- safety properties ------------------------------------------------------


def test_only_the_two_mode_codes_are_sendable():
    assert set(MODE_FRAMES) == {MANUAL, AUTO}


def test_switch_sends_exactly_one_frame_of_the_right_code():
    doctor, link, _ = make_doctor()
    doctor.switch("autonomous")
    assert link.sent == [(AUTO, 0)]
    doctor.switch("manual")
    assert link.sent == [(AUTO, 0), (MANUAL, 0)]


def test_switch_rejects_unknown_mode_without_sending():
    doctor, link, _ = make_doctor()
    with pytest.raises(ModeDoctorError):
        doctor.switch("gallop")
    assert link.sent == []


def test_e1_sends_manual_then_autonomous():
    doctor, link, _ = make_doctor()
    doctor.e1()
    assert [code for code, _ in link.sent] == [MANUAL, AUTO]


# --- measurement preconditions ---------------------------------------------


def test_require_state_without_feedback_names_the_relay():
    doctor, _, _ = make_doctor(feed=FakeFeed(state=None, age=None))
    with pytest.raises(ModeDoctorError) as exc:
        doctor.require_state()
    assert "relay" in str(exc.value)


def test_require_state_rejects_stale_feedback():
    doctor, _, _ = make_doctor(feed=FakeFeed(age=2.5))
    with pytest.raises(ModeDoctorError):
        doctor.require_state()


def test_snapshot_rejects_non_positive_window():
    doctor, _, _ = make_doctor()
    with pytest.raises(ValueError):
        doctor.snapshot(0)


# --- reading the result -----------------------------------------------------


def test_summarize_reports_the_set_of_seen_values():
    samples = [
        sample_from_state(make_state(robot_motion_state=0), 0.0),
        sample_from_state(make_state(robot_motion_state=1), 0.1),
        sample_from_state(make_state(robot_motion_state=1), 0.2),
    ]
    out = summarize(samples)
    assert out["count"] == 3
    assert out["robot_motion_state_values"] == [0, 1]
    assert out["robot_motion_state"] == [0, 1]


def test_switch_detects_a_field_change():
    def on_send(code, feed):
        if code == AUTO:
            feed.state = make_state(robot_motion_state=1)

    doctor, link, _ = make_doctor(on_send=on_send)
    result = doctor.switch("autonomous")
    assert result["robot_motion_state_changed"] is True
    assert link.sent == [(AUTO, 0)]


def test_switch_reports_no_change_when_field_is_constant():
    doctor, _, _ = make_doctor()
    result = doctor.switch("autonomous")
    assert result["robot_motion_state_changed"] is False


def test_e1_with_a_constant_field_says_nothing_changed():
    doctor, _, _ = make_doctor()
    result = doctor.e1()
    assert result["any_change"] is False
    assert "unrelated" in describe(result)


def test_e1_with_a_reacting_field_says_something_changed():
    def on_send(code, feed):
        feed.state = make_state(robot_motion_state=1 if code == AUTO else 0)

    doctor, _, _ = make_doctor(on_send=on_send)
    result = doctor.e1()
    assert result["any_change"] is True
    assert "DID change" in describe(result)
