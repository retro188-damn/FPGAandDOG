"""Tests for the card-reading bridge (vision -> spoken sentence).

The camera and the classifier are both injected, so these run without the dog,
the boards or the trained model.
"""
from __future__ import annotations

import numpy as np

import pytest

from host.app.vision import digit_service
from host.app.vision.digit_service import (
    MIN_AGREEMENT,
    ReadResult,
    read_card,
    read_settled,
    spoken,
    wait_until_still,
)


class FakeFrame:
    """Stand-in for a PIL image; carries the digit the fake reader will report."""

    def __init__(self, digit: str, score: float = 0.9) -> None:
        self.digit = digit
        self.score = score


@pytest.fixture
def fake_reader(monkeypatch):
    """Make every frame report its own digit; empty frames report nothing."""

    def fake_to_gray(frame):
        return frame

    def fake_find_card(gray):
        return (0, 0, 10, 10)

    def fake_read_digits(image, classifier, card_hint=None, **kwargs):
        if not image.digit:
            return "", []
        return image.digit, [(image.digit, image.score, (0, 0, 10, 10))]

    monkeypatch.setattr(digit_service, "to_gray", fake_to_gray)
    monkeypatch.setattr(digit_service, "find_card", fake_find_card)
    monkeypatch.setattr(digit_service, "read_digits", fake_read_digits)


def test_unanimous_burst_is_ok(fake_reader):
    result = read_card(frames=[FakeFrame("3")] * 3, classifier=object())
    assert (result.digits, result.reason) == ("3", "ok")
    assert result.agreement == pytest.approx(1.0)
    assert result.shots == 3
    assert result.confidence == pytest.approx(0.9)


def test_majority_still_ok(fake_reader):
    result = read_card(frames=[FakeFrame("2"), FakeFrame("7"), FakeFrame("2")],
                       classifier=object())
    assert result.digits == "2"
    assert result.agreement == pytest.approx(2 / 3)
    assert result.reason == "ok"


def test_disagreeing_burst_is_unclear(fake_reader):
    result = read_card(frames=[FakeFrame("2"), FakeFrame("7"), FakeFrame("8")],
                       classifier=object())
    assert result.agreement < MIN_AGREEMENT
    assert result.reason == "unclear"


def test_no_glyphs_is_empty(fake_reader):
    result = read_card(frames=[FakeFrame(""), FakeFrame("")], classifier=object())
    assert (result.digits, result.reason) == ("", "empty")
    assert result.confidence == 0.0


def test_missing_classifier_degrades_to_empty(monkeypatch):
    monkeypatch.setattr(digit_service, "load_classifier", lambda *a, **k: None)
    result = read_card(frames=[FakeFrame("3")], classifier=None)
    assert result.reason == "empty"


def test_camera_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(digit_service, "load_classifier", lambda *a, **k: object())

    def boom():
        raise OSError("no camera")

    result = read_card(shots=2, grab=boom)
    # a dead transport is reported separately from "nothing in view"
    assert result.reason == "unreachable" and result.shots == 0


@pytest.mark.parametrize("digits,reason,expected", [
    ("3", "ok", "板子上写的是三"),
    ("7", "unclear", "我看到有点像七，但不太确定"),
    ("", "empty", "我没看到数字，能再靠近一点吗？"),
])
def test_spoken_lines(digits, reason, expected):
    assert spoken(ReadResult(digits=digits, reason=reason)) == expected


def test_result_dict_is_json_ready(fake_reader):
    payload = read_card(frames=[FakeFrame("5")], classifier=object()).to_dict()
    assert payload["digits"] == "5"
    assert set(payload) == {"digits", "confidence", "agreement", "shots", "reason"}


class _FakeFeed(object):
    """Scripted state feed: each latest() pops the next (vel_body) sample."""

    def __init__(self, samples):
        self.samples = list(samples)

    def latest(self):
        if not self.samples:
            return None, 9.9
        vel = self.samples.pop(0)
        return {"vel_body[3]": list(vel)}, 0.05


def _ok(digit="2", conf=0.98, agree=1.0):
    return ReadResult(digits=digit, confidence=conf, agreement=agree, shots=3,
                      reason="ok")


def test_read_settled_skips_retry_when_confident():
    calls = []

    def once():
        calls.append(1)
        return _ok()

    result = read_settled(once, feed=None, settle_s=0.0)
    assert result.digits == "2"
    assert len(calls) == 1, "a confident reading must not be retried"


def test_read_settled_retries_an_unclear_reading():
    script = [_ok("1", 0.79, 1.0), _ok("2", 0.98, 1.0)]

    def once():
        return script.pop(0)

    result = read_settled(once, feed=None, settle_s=0.0, retries=1)
    assert result.digits == "2"
    assert result.confidence == pytest.approx(0.98)


def test_read_settled_keeps_the_best_of_two_unsure_readings():
    script = [ReadResult(digits="", reason="empty"),
              ReadResult(digits="3", confidence=0.71, agreement=0.67, shots=3,
                         reason="unclear")]

    def once():
        return script.pop(0)

    result = read_settled(once, feed=None, settle_s=0.0, retries=1)
    assert result.digits == "3"
    assert result.reason == "unclear"


def test_wait_until_still_returns_once_velocity_drops():
    # moving for 2 samples, then still; settle_s 0.0 means immediately settled
    feed = _FakeFeed([(0.2, 0.0, 0.0), (0.1, 0.0, 0.0), (0.0, 0.0, 0.0)])
    ticks = iter([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

    assert wait_until_still(feed, settle_s=0.0, timeout_s=5.0,
                            sleep=lambda s: None, clock=lambda: next(ticks))


def test_wait_until_still_times_out_while_moving():
    feed = _FakeFeed([(0.5, 0.0, 0.0)] * 100)
    ticks = iter(range(0, 200))

    assert not wait_until_still(feed, settle_s=1.5, timeout_s=1.0,
                                sleep=lambda s: None,
                                clock=lambda: next(ticks) * 0.1)


def test_unreachable_camera_is_reported_as_such():
    def boom():
        raise OSError("connection refused")

    result = digit_service.read_card(shots=2, grab=boom, classifier=None)
    assert result.reason == "unreachable"
    assert result.shots == 0
    assert "相机" in digit_service.spoken(result)


def test_empty_scene_is_not_reported_as_camera_failure():
    from PIL import Image

    frame = Image.new("RGB", (160, 120), (200, 200, 200))
    result = digit_service.read_card(shots=2, grab=lambda: frame, classifier=None)
    assert result.reason == "empty"
    assert "靠近" in digit_service.spoken(result)


# -- stop-then-shoot burst ------------------------------------------------


class FakeQuality:
    def __init__(self, ok: bool, has_card: bool = True, score: float = 1.0) -> None:
        self.ok = ok
        self.has_card = has_card
        self.score = score


def _patch_pick_best(monkeypatch, keep):
    """Make ``pick_best`` keep the frames at ``keep`` (indices into the burst)."""
    from host.app.vision import frame_quality

    def fake_pick_best(frames, top_k=3, **kwargs):
        qualities = [FakeQuality(ok=(i in keep)) for i in range(len(frames))]
        return [i for i in range(len(frames)) if i in keep][:top_k], qualities

    monkeypatch.setattr(frame_quality, "pick_best", fake_pick_best)


def test_read_burst_votes_only_over_the_usable_frames(monkeypatch, fake_reader):
    _patch_pick_best(monkeypatch, keep={0, 2})
    frames = [FakeFrame("3"), FakeFrame("9"), FakeFrame("3")]
    queue = iter(frames)

    result = digit_service.read_burst(grab=lambda: next(queue), shots=3,
                                      sleep=lambda s: None, classifier=object())

    assert result.digits == "3"
    assert result.reason == "ok"
    assert result.agreement == pytest.approx(1.0)
    assert result.shots == 2, "the unusable frame must not be voted on"


def test_read_burst_says_blurred_instead_of_guessing(monkeypatch, fake_reader):
    _patch_pick_best(monkeypatch, keep=set())
    frames = [FakeFrame("2"), FakeFrame("2")]
    queue = iter(frames)

    result = digit_service.read_burst(grab=lambda: next(queue), shots=2,
                                      sleep=lambda s: None, classifier=object())

    assert result.digits == ""
    assert result.reason == "blurred"
    assert "糊" in spoken(result)


def test_read_burst_reports_no_card_when_the_gate_sees_nothing(monkeypatch, fake_reader):
    from host.app.vision import frame_quality

    def fake_pick_best(frames, top_k=3, **kwargs):
        return [], [FakeQuality(ok=False, has_card=False) for _ in frames]

    monkeypatch.setattr(frame_quality, "pick_best", fake_pick_best)
    frames = [FakeFrame(""), FakeFrame("")]
    queue = iter(frames)

    result = digit_service.read_burst(grab=lambda: next(queue), shots=2,
                                      sleep=lambda s: None, classifier=object())

    assert result.reason == "empty"
    assert "靠近" in spoken(result)


def test_read_burst_sleeps_between_frames(monkeypatch, fake_reader):
    _patch_pick_best(monkeypatch, keep={0, 1, 2})
    frames = [FakeFrame("3"), FakeFrame("3"), FakeFrame("3")]
    queue = iter(frames)
    sleeps = []

    result = digit_service.read_burst(grab=lambda: next(queue), shots=3, gap=0.15,
                                      sleep=sleeps.append, classifier=object())

    assert result.digits == "3"
    assert sleeps == [0.15, 0.15]


def test_read_burst_reports_a_transport_error(fake_reader):
    def boom():
        raise OSError("connection refused")

    result = digit_service.read_burst(grab=boom, shots=3, sleep=lambda s: None,
                                      classifier=object())
    assert result.reason == "unreachable"
    assert result.shots == 0
