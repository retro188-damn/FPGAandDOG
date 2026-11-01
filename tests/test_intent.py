"""Unit tests for the pure-function intent parser (host/app/voice/intent.py).

Every test name states the expectation, so a failure reads as a specification
mismatch.  Run from the repository root::

    python -m pytest tests/test_intent.py -q
"""
import sys
from pathlib import Path

import pytest

# Make `import host.app.voice.intent` work even when pytest is invoked as a
# bare `pytest tests/...` (which does not put the repo root on sys.path).
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from host.app.voice.intent import (  # noqa: E402
    CONF_AMBIGUOUS,
    CONF_CLEAN,
    CONF_NONE,
    CONF_WEAK,
    VALID_TARGET_MAX,
    VALID_TARGET_MIN,
    parse_intent,
)

SCHEMA_KEYS = ["intent", "target", "actions", "raw", "confidence"]


# --------------------------------------------------------------------------
# clean instructions
# --------------------------------------------------------------------------
def test_clean_goto_with_read_request_yields_goto_three_and_read_digits():
    out = parse_intent("去3号位置并查看板子上的数字")
    assert out["intent"] == "goto"
    assert out["target"] == 3
    assert out["actions"] == ["read_digits"]
    assert out["confidence"] == CONF_CLEAN
    assert out["raw"] == "去3号位置并查看板子上的数字"


def test_clean_goto_without_read_request_has_no_actions():
    out = parse_intent("去5号位置")
    assert out["intent"] == "goto"
    assert out["target"] == 5
    assert out["actions"] == []


def test_goto_accepts_all_movement_verbs():
    for text in ("去3号位置", "到3号位置", "前往3号位置", "走到3号位置", "带我去3号位置", "过去3号位置"):
        out = parse_intent(text)
        assert out["intent"] == "goto", text
        assert out["target"] == 3, text


# --------------------------------------------------------------------------
# noisy ASR output: spaces, fillers
# --------------------------------------------------------------------------
def test_spaces_between_every_character_are_ignored():
    out = parse_intent("去 三 号 位 置")
    assert out["intent"] == "goto"
    assert out["target"] == 3


def test_fillers_and_spaces_before_the_command_are_ignored():
    out = parse_intent("嗯那个请你去第 4 个位置看一下数字")
    assert out["intent"] == "goto"
    assert out["target"] == 4
    assert out["actions"] == ["read_digits"]
    assert out["confidence"] == CONF_CLEAN


def test_punctuation_from_asr_is_ignored():
    out = parse_intent("去，3号位置。查看数字！")
    assert out["intent"] == "goto"
    assert out["target"] == 3
    assert out["actions"] == ["read_digits"]


def test_repeated_filler_chars_do_not_break_the_command():
    out = parse_intent("呃呃啊那个那个去六号位置")
    assert out["intent"] == "goto"
    assert out["target"] == 6


# --------------------------------------------------------------------------
# numerals: Chinese and Arabic
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "numeral,expected",
    [("一", 1), ("二", 2), ("两", 2), ("三", 3), ("四", 4), ("五", 5), ("六", 6), ("七", 7), ("八", 8)],
)
def test_chinese_numerals_one_to_eight_map_to_their_value(numeral, expected):
    out = parse_intent("去%s号位置" % numeral)
    assert out["intent"] == "goto"
    assert out["target"] == expected


@pytest.mark.parametrize("digit", ["1", "2", "3", "4", "5", "6", "7", "8"])
def test_arabic_digits_one_to_eight_map_to_their_value(digit):
    out = parse_intent("去%s号位置" % digit)
    assert out["intent"] == "goto"
    assert out["target"] == int(digit)


def test_full_width_arabic_digit_is_normalized_to_ascii():
    out = parse_intent("去３号位置")
    assert out["intent"] == "goto"
    assert out["target"] == 3


# --------------------------------------------------------------------------
# target phrase variants
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("请去第5个位置", 5),           # 第N个位置
        ("去第6号位", 6),              # 第N号位
        ("去三号位", 3),               # 三号位
        ("去3号点", 3),                # N号点
        ("去位置3", 3),                # 位置N
        ("去标号3", 3),                # 标号N
        ("去第 7 个 位置", 7),          # spaces inside the phrase
    ],
)
def test_target_phrase_variants_are_recognized(text, expected):
    out = parse_intent(text)
    assert out["intent"] == "goto"
    assert out["target"] == expected


def test_bare_number_without_target_phrase_is_accepted_but_less_confident():
    out = parse_intent("去3")
    assert out["intent"] == "goto"
    assert out["target"] == 3
    assert out["confidence"] == CONF_WEAK


def test_target_phrase_without_movement_verb_is_unknown():
    # Spec: goto needs BOTH a movement verb and a target.
    for text in ("三号位", "位置3", "第4个位置"):
        assert parse_intent(text)["intent"] == "unknown", text


# --------------------------------------------------------------------------
# missing / invalid numbers
# --------------------------------------------------------------------------
def test_movement_verb_without_any_number_is_unknown_with_zero_confidence():
    out = parse_intent("去那个位置")
    assert out["intent"] == "unknown"
    assert out["target"] is None
    assert out["confidence"] == CONF_NONE


def test_spurious_one_in_yi_xia_is_not_treated_as_a_target():
    # 一下 contains 一; without this guard it would parse as position 1.
    out = parse_intent("去位置看一下")
    assert out["intent"] == "unknown"


def test_real_digit_after_yi_xia_still_wins():
    out = parse_intent("去一下3号位置")
    assert out["intent"] == "goto"
    assert out["target"] == 3


@pytest.mark.parametrize("text", ["去十号位置", "去十二号位置", "去100号位置"])
def test_out_of_range_numbers_are_unknown(text):
    out = parse_intent(text)
    assert out["intent"] == "unknown"
    assert out["target"] is None
    assert out["confidence"] == CONF_NONE


def test_range_boundaries_are_inclusive():
    assert parse_intent("去%d号位置" % VALID_TARGET_MIN)["intent"] == "goto"
    assert parse_intent("去%d号位置" % VALID_TARGET_MAX)["intent"] == "goto"
    assert parse_intent("去%d号位置" % (VALID_TARGET_MAX + 1))["intent"] == "unknown"


# --------------------------------------------------------------------------
# stop
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["停", "停下", "停止", "别动", "不要动", "急停", "站住", "停 下"])
def test_every_stop_variant_yields_stop(text):
    out = parse_intent(text)
    assert out["intent"] == "stop"
    assert out["target"] is None
    assert out["actions"] == []
    assert out["confidence"] == CONF_CLEAN


def test_stop_inside_parking_lot_is_not_a_stop_command():
    out = parse_intent("去停车场")
    assert out["intent"] == "unknown"


# --------------------------------------------------------------------------
# stand / lie
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["起立", "站起来", "起身", "站立"])
def test_every_stand_variant_yields_stand(text):
    out = parse_intent(text)
    assert out["intent"] == "stand"
    assert out["target"] is None
    assert out["confidence"] == CONF_CLEAN


@pytest.mark.parametrize("text", ["趴下", "卧倒", "趴着"])
def test_every_lie_variant_yields_lie(text):
    out = parse_intent(text)
    assert out["intent"] == "lie"
    assert out["target"] is None
    assert out["confidence"] == CONF_CLEAN


def test_first_posture_mentioned_wins_when_both_appear():
    out = parse_intent("趴下别站起来")
    assert out["intent"] == "lie"
    assert out["confidence"] == CONF_AMBIGUOUS


# --------------------------------------------------------------------------
# unrelated sentences
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "今天天气不错",
        "你好",
        "你叫什么名字",
        "这个板子多少钱",
        "我们下午开会",
        "",
        "   ",
        "嗯",
        "ok",
    ],
)
def test_unrelated_text_is_unknown(text):
    out = parse_intent(text)
    assert out["intent"] == "unknown"
    assert out["target"] is None
    assert out["actions"] == []
    assert out["confidence"] == CONF_NONE
    assert out["raw"] == text


# --------------------------------------------------------------------------
# read_digits detection
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "去3号位置看数字",
        "去3号位置读数字",
        "去3号位置识别数字",
        "去3号位置查号码",
        "去3号位置查看板子",
        "去3号位置看板子上的标号",
        "去标号3",  # 标号 itself is listed as a read trigger by the spec
    ],
)
def test_read_keywords_add_the_read_digits_action(text):
    assert parse_intent(text)["actions"] == ["read_digits"]


@pytest.mark.parametrize("text", ["去3号位置", "去3号位置停下来", "去第4个位置"])
def test_plain_goto_without_read_intent_has_empty_actions(text):
    out = parse_intent(text)
    assert out["intent"] == "goto"
    assert out["actions"] == []


def test_negated_read_keyword_suppresses_the_read_digits_action():
    out = parse_intent("去3号位置不要看数字")
    assert out["intent"] == "goto"
    assert out["target"] == 3
    assert out["actions"] == []


# --------------------------------------------------------------------------
# priority and confidence
# --------------------------------------------------------------------------
def test_goto_wins_over_stop_but_confidence_drops():
    out = parse_intent("停下然后去3号位置")
    assert out["intent"] == "goto"
    assert out["target"] == 3
    assert out["confidence"] == CONF_AMBIGUOUS


def test_goto_wins_over_lie_but_confidence_drops():
    out = parse_intent("趴下然后去3号位置")
    assert out["intent"] == "goto"
    assert out["confidence"] == CONF_AMBIGUOUS


def test_stop_wins_over_stand_but_confidence_drops():
    out = parse_intent("别动 站起来")
    assert out["intent"] == "stop"
    assert out["confidence"] == CONF_AMBIGUOUS


# --------------------------------------------------------------------------
# output contract
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text", ["去3号位置查看数字", "停下", "站起来", "趴下", "今天天气不错"]
)
def test_every_result_has_the_documented_keys_in_order(text):
    out = parse_intent(text)
    assert list(out.keys()) == SCHEMA_KEYS


@pytest.mark.parametrize(
    "text", ["去3号位置查看数字", "停下", "站起来", "趴下", "今天天气不错", "去十号位置"]
)
def test_confidence_is_always_a_float_between_zero_and_one(text):
    confidence = parse_intent(text)["confidence"]
    assert isinstance(confidence, float)
    assert 0.0 <= confidence <= 1.0


@pytest.mark.parametrize("text", ["去3号位置", "停下", "今天天气不错"])
def test_target_is_an_int_for_goto_and_none_otherwise(text):
    out = parse_intent(text)
    if out["intent"] == "goto":
        assert isinstance(out["target"], int)
    else:
        assert out["target"] is None


def test_parser_is_pure_and_keeps_the_original_text_in_raw():
    text = "嗯那个请你去第 4 个位置看一下数字"
    first = parse_intent(text)
    second = parse_intent(text)
    assert first == second
    assert first["raw"] == text


def test_none_input_degrades_to_unknown_instead_of_raising():
    out = parse_intent(None)  # type: ignore[arg-type]
    assert out["intent"] == "unknown"
    assert out["raw"] == ""


# -- ASR slip repair (transcripts captured on 2026-10-26) -------------------
@pytest.mark.parametrize("heard,target,read", [
    ("去一号后位置", 1, False),          # 号 -> 后
    ("去一号位子", 1, False),            # 位置 -> 位子
    ("去三号位置看看是什么书", 3, True),  # 数字 -> 书
    ("去三号位置看看是什么数子", 3, True),
])
def test_target_still_parsed_after_asr_slip(heard, target, read):
    parsed = parse_intent(heard)
    assert parsed["intent"] == "goto"
    assert parsed["target"] == target
    assert ("read_digits" in parsed["actions"]) is read


@pytest.mark.parametrize("heard", ["看看是什么书", "按看是什么数字", "按看是什么书"])
def test_read_only_command_survives_asr_slip(heard):
    parsed = parse_intent(heard)
    assert parsed["intent"] == "read"
    assert parsed["actions"] == ["read_digits"]


def test_repair_does_not_invent_a_target():
    # 后 alone must not become 号 without a numeral in front of it
    assert parse_intent("向后位置看看")["intent"] != "goto"
