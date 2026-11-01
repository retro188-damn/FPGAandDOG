"""Tests for the stand-alone "read the card" path (B of the roadmap).

Covers the three seams that were added to make ``看看是什么数字`` work without
walking anywhere: the intent, the plan, and the sentence the assistant speaks.
"""
from __future__ import annotations

import json

import pytest

from host.app import assistant
from host.app.pipeline import plan_from_intent
from host.app.vision.digit_service import ReadResult
from host.app.voice.intent import parse_intent


@pytest.mark.parametrize("text", [
    "看看是什么数字",
    "看一下数字",
    "读一下号码",
    "板子上写的什么数",
    "念一下标号",
])
def test_read_only_commands_parse_as_read(text):
    out = parse_intent(text)
    assert out["intent"] == "read", out
    assert out["actions"] == ["read_digits"]
    assert out["target"] is None


@pytest.mark.parametrize("text", [
    "这个板子多少钱",   # 多少 alone must not mean "read the card"
    "随便看看",
    "你好",
    "别看了",
])
def test_ambiguous_sentences_stay_unknown(text):
    assert parse_intent(text)["intent"] == "unknown", text


def test_read_plan_has_a_read_step_and_no_motion():
    plan = plan_from_intent(parse_intent("看看是什么数字"))
    assert plan.intent == "read"
    assert "read_digits" in plan.actions
    assert plan.requires_hardware is False
    assert any(step.kind == "READ_DIGITS" for step in plan.steps)


def test_goto_with_read_still_keeps_both_actions():
    plan = plan_from_intent(parse_intent("去三号位置看看数字"))
    assert plan.intent == "goto"
    assert plan.target == 3
    assert plan.actions == ["read_digits"]
    kinds = [step.kind for step in plan.steps]
    assert kinds.index("MOVE") < kinds.index("READ_DIGITS")


def test_assistant_reads_and_speaks(monkeypatch, capsys):
    """--read-digits runs the injected reader and speaks its verdict."""
    from host.app.vision import digit_service

    monkeypatch.setattr(
        digit_service, "read_burst",
        lambda **kwargs: ReadResult(digits="4", confidence=0.9, agreement=1.0,
                                    shots=3, reason="ok"),
    )
    code = assistant.main(["--text", "看看是什么数字", "--read-digits",
                           "--no-tts"])
    out = capsys.readouterr().out
    assert code == 0
    assert "SAY: 板子上写的是四" in out
    assert '"digits": "4"' in out


def test_assistant_reports_empty_view(monkeypatch, capsys):
    from host.app.vision import digit_service

    monkeypatch.setattr(
        digit_service, "read_burst",
        lambda **kwargs: ReadResult(shots=3, reason="empty"),
    )
    assistant.main(["--text", "看看是什么数字", "--read-digits", "--no-tts"])
    assert "SAY: 我没看到数字" in capsys.readouterr().out


def test_assistant_json_contract_is_still_printed(monkeypatch, capsys):
    from host.app.vision import digit_service

    monkeypatch.setattr(
        digit_service, "read_burst",
        lambda **kwargs: ReadResult(digits="7", reason="unclear",
                                    agreement=0.33, shots=3),
    )
    assistant.main(["--text", "看看是什么数字", "--read-digits", "--no-tts"])
    tail = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(tail)
    assert payload["intent"] == "read"
