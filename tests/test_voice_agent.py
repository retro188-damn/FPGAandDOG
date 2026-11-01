"""Tests for the resident voice agent (no microphone, no model, no dog)."""
import numpy as np
import pytest

from host.app.voice.agent import (
    ARM,
    EXIT,
    EXECUTE,
    IGNORE,
    STOP,
    VoiceAgent,
    is_stop_command,
    parse,
    strip_wake_word,
)


def test_wake_word_at_start_is_stripped():
    assert strip_wake_word("小狗去一号位置") == (True, "去一号位置")
    assert strip_wake_word("小狗狗，去三号位置看看数字") == (True, "去三号位置看看数字")


def test_filler_before_wake_word_is_tolerated():
    heard, rest = strip_wake_word("那个小狗去一号")
    assert heard is True
    assert rest == "去一号"


def test_plain_sentence_is_not_a_command():
    assert strip_wake_word("我今天去一号位置") == (False, "")
    assert parse("我今天去一号位置")[0] == IGNORE


def test_wake_word_alone_arms():
    assert parse("小狗")[0] == ARM
    assert parse("小狗！")[0] == ARM


def test_stop_words_at_the_start_win():
    assert is_stop_command("停") is True
    assert is_stop_command("小狗停下") is True
    assert is_stop_command("停一下") is True
    assert is_stop_command("去一号位置") is False


def test_stop_word_inside_a_sentence_does_not_fire():
    # Live false positive 2026-10-26 19:18:22.
    assert is_stop_command("哎我看怎么停下来了") is False
    assert parse("哎我看怎么停下来了")[0] == IGNORE


def test_voice_exit_needs_the_wake_word():
    assert parse("小狗退出")[0] == EXIT
    assert parse("小狗，退出监听")[0] == EXIT
    assert parse("退出")[0] == IGNORE  # ambient talk must not kill the agent


def test_voice_exit_leaves_the_loop():
    rig = Rig([])
    agent = _agent(rig)
    rig.audio = [np.zeros(1)]
    rig.transcripts = ["小狗退出"]
    turns = agent.run_until(deadline=10.0)
    assert turns == 1
    assert agent.running is False
    assert rig.spoken == ["好的，我退出"]


def test_parse_execute_carries_clean_command():
    action, cmd = parse("小狗，去三号位置看看数字")
    assert action == EXECUTE
    assert cmd == "去三号位置看看数字"


class Rig:
    def __init__(self, transcripts):
        self.audio = [np.zeros(1) for _ in transcripts]
        self.transcripts = list(transcripts)
        self.spoken = []
        self.handled = []
        self.stops = 0
        self.clock_now = 0.0

    def capture(self, _seconds):
        return self.audio.pop(0) if self.audio else np.zeros(0)

    def transcribe(self, _audio):
        return self.transcripts.pop(0) if self.transcripts else ""

    def handle(self, text):
        self.handled.append(text)
        return "好的：" + text

    def speak(self, text):
        self.spoken.append(text)

    def stop_now(self):
        self.stops += 1

    def clock(self):
        self.clock_now += 1.0
        return self.clock_now


def _agent(rig):
    return VoiceAgent(
        capture=rig.capture,
        transcribe=rig.transcribe,
        handle=rig.handle,
        speak=rig.speak,
        stop_now=rig.stop_now,
        clock=rig.clock,
        mic_seconds=1.0,
    )


def test_ignored_speech_does_not_reach_the_handler():
    rig = Rig(["今天天气不错"])
    agent = _agent(rig)
    agent.run_until(deadline=10.0)
    assert rig.handled == []
    assert agent.ignored == 1


def test_one_breath_command_is_executed_and_answered():
    rig = Rig(["小狗去一号位置"])
    agent = _agent(rig)
    agent.run_until(deadline=10.0)
    assert rig.handled == ["去一号位置"]
    assert rig.spoken == ["好的：去一号位置"]


def test_two_breath_command_arms_then_executes():
    rig = Rig(["小狗", "去三号位置"])
    agent = _agent(rig)
    agent.run_until(deadline=10.0)
    assert rig.handled == ["去三号位置"]


def test_stop_phrase_calls_stop_without_the_handler():
    rig = Rig(["停下"])
    agent = _agent(rig)
    agent.run_until(deadline=10.0)
    assert rig.stops == 1
    assert rig.handled == []
    assert rig.spoken == ["停"]


def test_armed_turn_without_speech_disarms_silently():
    rig = Rig(["小狗", ""])
    agent = _agent(rig)
    agent.run_until(deadline=10.0)
    assert rig.handled == []
    assert rig.spoken == []


def test_default_stop_now_is_optional():
    agent = VoiceAgent(
        capture=lambda _s: np.zeros(0),
        transcribe=lambda _a: "",
        handle=lambda text: None,
        speak=lambda text: None,
    )
    assert agent.run_until(deadline=-1) == 0


def test_bare_read_command_needs_no_wake_word():
    # 2026-10-26 live run: "看看是什么数字" said alone was ignored every time.
    assert parse("看看是什么数字")[0] == EXECUTE
    assert parse("读一下号码")[0] == EXECUTE
    rig = Rig(["看看是什么数字"])
    agent = _agent(rig)
    agent.run_until(deadline=10.0)
    assert rig.handled == ["看看是什么数字"]


def test_bare_comment_about_digits_is_still_ignored():
    # "看" inside a sentence must not turn chatter into a command.
    assert parse("我在看这个数字")[0] == IGNORE
    assert parse("这个数字挺好看")[0] == IGNORE
    rig = Rig(["这个数字挺好看"])
    agent = _agent(rig)
    agent.run_until(deadline=10.0)
    assert rig.handled == []


def test_second_wake_word_keeps_waiting():
    # 20:54 run: "小狗" then "小狗小狗" answered "没听懂" and dropped the command.
    rig = Rig(["小狗", "小狗小狗", "去三号位置"])
    agent = _agent(rig)
    agent.run_until(deadline=10.0)
    assert rig.handled == ["去三号位置"]
    assert "没听懂，再说一遍" not in rig.spoken


def test_verbless_question_counts_as_a_read_command():
    # 21:04 run: "看看是什么数字" was transcribed as "共是什么数据" (verb lost).
    assert parse("共是什么数据")[0] == EXECUTE
    assert parse("上面写的什么数")[0] == EXECUTE


def test_runner_imports_vision_by_package_path():
    """Regression: the 21:04 run died with ModuleNotFoundError 'vision'."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "tools" / "voice_agent.py"
    text = src.read_text(encoding="utf-8")
    assert "from host.app.vision.digit_service import" in text
    assert "from vision.digit_service import" not in text
