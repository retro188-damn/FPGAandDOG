"""Unit tests for the intent -> plan layer and the TTS voice selection.

The hardware-facing pieces are deliberately not exercised here: they need the
dog, the FPGA, or a loudspeaker.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.app.pipeline import cn_number, digit_sentence, plan_from_intent  # noqa: E402
from host.app.voice.intent import parse_intent  # noqa: E402
from host.app.voice.tts import available_voices, pick_voice  # noqa: E402


def test_cn_number_covers_the_board_positions():
    assert cn_number(1) == "一"
    assert cn_number(9) == "九"
    assert cn_number(10) == "十"
    assert cn_number(11) == "十一"
    assert cn_number(20) == "二十"
    assert cn_number(23) == "二十三"


def test_goto_with_digits_builds_move_then_read():
    plan = plan_from_intent(parse_intent("去三号位置看看是什么数字"))
    assert plan.intent == "goto"
    assert plan.target == 3
    assert plan.actions == ["read_digits"]
    assert plan.say[0] == "收到，前往三号位置"
    kinds = [step.kind for step in plan.steps]
    assert kinds[0] == "MOVE"
    assert "READ_DIGITS" in kinds
    assert plan.requires_hardware is True


def test_goto_without_digits_does_not_read():
    plan = plan_from_intent(parse_intent("去1号位置"))
    assert plan.target == 1
    assert "READ_DIGITS" not in [step.kind for step in plan.steps]
    assert plan.say == ["收到，前往一号位置"]


def test_stop_is_software_only_and_short():
    plan = plan_from_intent(parse_intent("别动"))
    assert plan.intent == "stop"
    assert plan.say == ["好的，停下"]
    assert [step.kind for step in plan.steps] == ["STOP", "SPEAK"]


def test_stand_and_lie_map_to_posture_steps():
    assert [s.kind for s in plan_from_intent(parse_intent("站起来")).steps] == ["STAND", "SPEAK"]
    assert [s.kind for s in plan_from_intent(parse_intent("趴下")).steps] == ["LIE", "SPEAK"]


def test_unknown_intent_asks_again_and_needs_no_hardware():
    plan = plan_from_intent(parse_intent("今天天气不错"))
    assert plan.intent == "unknown"
    assert plan.say == ["没听清，请再说一遍"]
    assert plan.requires_hardware is False


def test_digit_sentence_renders_arabic_and_missing():
    assert digit_sentence("47") == "板子上写的是四七"
    assert digit_sentence(None) == "没看清板子上的数字"


def test_pick_voice_prefers_chinese_in_order():
    names = ["Microsoft Zira Desktop - English (United States)",
             "Microsoft Huihui Desktop - Chinese (Simplified)"]
    assert "Huihui" in pick_voice(names)
    assert pick_voice([]) is None
    assert pick_voice(["Only Voice"]) == "Only Voice"


def test_available_voices_is_best_effort():
    # Either SAPI answers with names, or it degrades to an empty list; never raises.
    assert isinstance(available_voices(), list)


def test_auto_threshold_scales_with_the_floor():
    from host.app.voice.mic import auto_threshold

    assert auto_threshold(0.0) == 0.003          # dead room: keep a sane floor
    assert auto_threshold(0.001) == 0.003        # 3x floor below the minimum
    assert auto_threshold(0.01) == 0.03          # loud room scales up
