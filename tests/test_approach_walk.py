"""Tests for the closed-loop approach in ``tools/voice_agent.py``.

No dog, no camera: the card width and the legs are faked, so what is under test is
the *decision* - when to step, when to retry an ignored command, when to give up.
The numbers come from the 2026-10-27 real run: 96 px was too small to read, 124 px
read at 0.99, and the dog ignored the first command after every stop.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def runner_module():
    spec = importlib.util.spec_from_file_location(
        "voice_agent_tool", ROOT / "tools" / "voice_agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, travelled):
        self.travelled_m = travelled
        self.reason = "reached" if travelled else "not responding"


def _runner(module, widths, travelled=None):
    runner = object.__new__(module.Runner)
    runner.args = SimpleNamespace(
        approach_px=120, approach_max_px=320, approach_step=0.15,
        approach_steps=6, speed=0.10, dog_host="127.0.0.1",
    )
    widths = list(widths)
    travelled = None if travelled is None else list(travelled)
    calls = {"width": 0, "legs": 0, "modes": 0}

    def card_width():
        value = widths[min(calls["width"], len(widths) - 1)]
        calls["width"] += 1
        return value

    class _Executor:
        def move(self, distance, speed=0.10):
            calls["legs"] += 1
            if travelled is None:
                return _Result(distance)
            return _Result(travelled.pop(0))

    def send_mode(_code):
        calls["modes"] += 1

    runner.executor = _Executor()
    runner._card_width = card_width
    runner._send_mode = send_mode
    runner._read_line = lambda: "读完了"
    return runner, calls


def test_walks_until_the_card_is_big_enough(runner_module):
    runner, calls = _runner(runner_module, [80, 95, 130])
    assert runner._walk_to_card() == "读完了"
    assert calls["legs"] == 2


def test_one_forced_step_even_when_the_card_is_readable(runner_module):
    # The demo wants "found it -> take a step -> read" every single time; a 200 px
    # card gets the tapered 0.10 m step so it does not jump past the blur limit.
    runner, calls = _runner(runner_module, [200, 230])
    assert runner._walk_to_card() == "读完了"
    assert calls["legs"] == 1


def test_a_card_that_fills_the_view_is_read_in_place(runner_module):
    runner, calls = _runner(runner_module, [400])
    assert runner._walk_to_card() == "读完了"
    assert calls["legs"] == 0


def test_ignored_first_command_is_retried_once(runner_module):
    # 2026-10-27: after every stop the first leg moved 0.000 m and the retry worked.
    runner, calls = _runner(runner_module, [80, 130], travelled=[0.0, 0.15])
    assert runner._walk_to_card() == "读完了"
    assert calls["legs"] == 2


def test_gives_up_without_walking_when_the_card_vanished(runner_module):
    runner, calls = _runner(runner_module, [None])
    assert runner._walk_to_card() == "没看到数字，是不是放偏了？"
    assert calls["legs"] == 0


def test_stops_before_running_into_the_card(runner_module):
    runner, calls = _runner(runner_module, [80, 400])
    assert runner._walk_to_card() == "读完了"
    assert calls["legs"] == 1


def test_needs_approach_for_a_small_or_missing_card(runner_module):
    runner, _ = _runner(runner_module, [80])
    assert runner._needs_approach() is True
    runner, _ = _runner(runner_module, [None])
    assert runner._needs_approach() is True
    runner, _ = _runner(runner_module, [150])
    assert runner._needs_approach() is False
