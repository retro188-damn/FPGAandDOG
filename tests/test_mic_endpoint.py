"""Endpoint logic of the VAD capture, driven by a scripted fake microphone.

Real-run finding (2026-10-26): a sentence that started near the end of the
8 s window was cut mid-word.  These tests pin the two behaviours that matter:

1. a normal short utterance still stops early on the first pause;
2. speech that is still running when the window ends is extended until the
   speaker pauses (bounded by `extend_seconds`), instead of being truncated.
"""
import numpy as np
import pytest

from host.app.voice import mic


RATE = 16000
BLOCK_MS = 100
BLOCK = RATE * BLOCK_MS // 1000


class _FakeStream(object):
    """Scripted InputStream: yields successive blocks from one long signal."""

    def __init__(self, signal, channels):
        self._signal = signal
        self._channels = channels
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, block):
        chunk = self._signal[self._pos:self._pos + block]
        self._pos += block
        if chunk.size < block:  # pad with silence if the script runs out
            chunk = np.concatenate([chunk, np.zeros(block - chunk.size, np.float32)])
        if self._channels > 1:
            chunk = np.repeat(chunk, self._channels)
        return chunk.reshape(block, self._channels), False


class _FakeSD(object):
    def __init__(self, signal, channels):
        self.signal = signal
        self.channels = channels
        self.streams = 0

    def InputStream(self, **kwargs):  # noqa: N802 - mimics the sounddevice API
        self.streams += 1
        return _FakeStream(self.signal, self.channels)


def _blocks(levels):
    """Turn a list of per-block amplitudes into one contiguous signal."""
    parts = []
    for level in levels:
        parts.append(np.full(BLOCK, float(level), dtype=np.float32))
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def _capture(levels, monkeypatch, **kwargs):
    signal = _blocks(levels)
    fake = _FakeSD(signal, kwargs.get("channels", 1))
    monkeypatch.setattr(mic, "_require_sounddevice", lambda: fake)
    monkeypatch.setattr(mic, "_open_rate", lambda sd, device, rate, excl, ch: rate)
    params = dict(
        max_seconds=1.0, extend_seconds=2.0, silence_ms=300, sample_rate=RATE,
        block_ms=BLOCK_MS, rms_threshold=0.05, device=0,
    )
    params.update(kwargs)
    return mic.record_with_vad_endpoint(**params)


def test_short_utterance_stops_on_first_pause(monkeypatch):
    # 0.2 s loud, then quiet -> endpoint after ~300 ms of silence, well under 1 s
    audio = _capture([0.0, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], monkeypatch)
    assert audio.size > 0
    assert audio.size < RATE * 1.0  # came back before the window was full


def test_speech_running_past_the_window_is_extended(monkeypatch):
    # speech starts at 0.4 s and never pauses inside the 1 s window
    levels = [0.0] * 4 + [0.5] * 6 + [0.5] * 10 + [0.0] * 3
    audio = _capture(levels, monkeypatch)
    assert audio.size > RATE * 1.2, "capture was cut at the window boundary"
    assert audio.size <= RATE * 3.0  # bounded by max_seconds + extend_seconds


def test_extension_is_skipped_when_disabled(monkeypatch):
    levels = [0.0] * 4 + [0.5] * 6 + [0.5] * 10 + [0.0] * 3
    audio = _capture(levels, monkeypatch, extend_seconds=0.0)
    assert 0 < audio.size <= RATE * 1.0


def test_no_speech_returns_empty(monkeypatch):
    assert _capture([0.0] * 12, monkeypatch).size == 0


def test_negative_extend_is_rejected(monkeypatch):
    with pytest.raises(ValueError):
        _capture([0.0] * 3, monkeypatch, extend_seconds=-1.0)


def test_channels_two_is_downmixed(monkeypatch):
    audio = _capture([0.0, 0.5, 0.5, 0.0, 0.0, 0.0], monkeypatch, channels=2)
    assert audio.size > 0
    assert float(np.max(np.abs(audio))) == pytest.approx(0.5, abs=1e-6)
