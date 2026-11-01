"""Tests for the recogniser's output guard.

The guard exists because two silent clips decoded to English sentences on
2026-10-26.  These tests pin both directions: noise must be dropped, and real
Chinese commands must never be dropped (a false positive is silent, which is
exactly the failure mode that is hard to notice in a demo).
"""
import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.app.voice.asr import (  # noqa: E402
    ChineseAsr,
    is_probably_silence,
    looks_non_chinese,
    peak_amplitude,
)

HALLUCINATIONS = [
    "it is 懂了 and i is 用了",
    "i fuck me you can make you happy all now",
    "by you and me to i i see our facctions",
]

COMMANDS = [
    "去一号位置",
    "去三号位置看看数字",
    "看看是什么数字",
    "一号位置看看是什么数字",
    "去 3 号位置并读数字",
    "停止",
    "站起来",
]


def tone(seconds=1.0, sr=16000, amp=0.3, freq=220.0):
    t = np.arange(int(seconds * sr)) / float(sr)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# --- amplitude -------------------------------------------------------------


def test_peak_amplitude_of_empty_input_is_zero():
    assert peak_amplitude(np.zeros(0)) == 0.0


def test_peak_amplitude_sees_the_tone():
    assert peak_amplitude(tone(amp=0.3)) == pytest.approx(0.3, abs=0.01)


def test_digital_silence_is_flagged():
    assert is_probably_silence(np.zeros(16000))
    assert is_probably_silence(tone(amp=0.001))


def test_a_real_level_is_not_flagged():
    assert not is_probably_silence(tone(amp=0.3))


# --- language --------------------------------------------------------------


@pytest.mark.parametrize("text", HALLUCINATIONS)
def test_english_hallucinations_are_dropped(text):
    assert looks_non_chinese(text)


@pytest.mark.parametrize("text", COMMANDS)
def test_chinese_commands_survive(text):
    assert not looks_non_chinese(text)


def test_empty_transcript_is_not_flagged():
    assert not looks_non_chinese("")


def test_short_latin_ok_word_survives():
    # "OK 好的" is a plausible answer; two letters are below the threshold
    assert not looks_non_chinese("OK 好的")


# --- wiring ----------------------------------------------------------------


def test_silence_is_rejected_before_the_model_is_loaded():
    asr = ChineseAsr()
    assert asr.transcribe(np.zeros(32000), 16000) == ""
    # the model must not have been created for input that cannot contain speech
    assert asr._recognizer is None


class _FakeStream:
    def __init__(self, text):
        self._text = text
        self.result = type("R", (), {"text": text})()

    def accept_waveform(self, sample_rate, audio):
        pass


class _FakeRecognizer:
    def __init__(self, text):
        self._text = text

    def create_stream(self):
        return _FakeStream(self._text)

    def decode_stream(self, stream):
        pass


def test_noise_transcript_is_dropped_and_can_be_kept_for_analysis():
    gibberish = "it is 懂了 and i is 用了"
    audio = tone(amp=0.3)

    asr = ChineseAsr()
    asr._recognizer = _FakeRecognizer(gibberish)   # bypass the real model
    assert asr.transcribe(audio, 16000) == ""

    asr_plain = ChineseAsr()
    asr_plain._recognizer = _FakeRecognizer(gibberish)
    assert asr_plain.transcribe(audio, 16000, guard=False) == gibberish
