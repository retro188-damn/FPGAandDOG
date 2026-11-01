"""Offline Chinese speech recognition (sherpa-onnx Paraformer) for the host PC.

Audio in, text out.  Everything runs locally: no network, no cloud service, no
API key, which is what the competition rules require.

The int8 ONNX model is ~220 MB and takes ~3 s to load, so it is loaded lazily
on the first transcription and reused afterwards.  Importing this module costs
almost nothing (numpy/scipy only; sherpa_onnx is imported inside the loader).

Typical use::

    from host.app.voice.asr import ChineseAsr

    asr = ChineseAsr()                       # no model load yet
    text = asr.transcribe_file("cmd.wav")    # 16 kHz or 8 kHz, mono
    text = asr.transcribe(samples, 16000)    # float32 in [-1, 1]

Measured on the dev PC: 5.6 s of audio decoded in 0.13 s (RTF ~0.03).
"""
import logging
import time
import wave
from math import gcd
from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np
from scipy.signal import resample_poly

LOG = logging.getLogger(__name__)

#: Repository root, derived from this file: host/app/voice/asr.py -> 4 parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Local Paraformer model (reference material: read-only, never modified here).
MODEL_DIR = (
    REPO_ROOT
    / "需要的材料"
    / "源码"
    / "源码"
    / "模型文件"
    / "sherpa-onnx-paraformer-zh-2024-03-09"
)

MODEL_FILE = "model.int8.onnx"
TOKENS_FILE = "tokens.txt"
TEST_WAV_DIR = "test_wavs"

#: Paraformer on this CPU is best with 4 threads; 16 kHz / 80-dim fbank is fixed by the model.
TARGET_SAMPLE_RATE = 16000
DEFAULT_NUM_THREADS = 4

#: Decoding anything shorter than this is pointless and some sherpa builds choke on it.
MIN_SAMPLES = 160


def load_wav(path: Union[str, Path]) -> Tuple[np.ndarray, int]:
    """Read a PCM wav file as ``(mono float32 in [-1, 1], sample_rate)``.

    sherpa_onnx 1.13.8 has no `read_wave`, so the stdlib `wave` module does it.
    Only 16-bit PCM is supported (that is what both the test wavs and our
    recordings are); anything else raises ValueError with the actual format.
    """
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sample_width != 2:
        raise ValueError(
            "only 16-bit PCM wav is supported, %s has %d-byte samples" % (path, sample_width)
        )
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1).astype(np.float32)
    return np.ascontiguousarray(audio, dtype=np.float32), int(sample_rate)


def resample_to(
    samples: np.ndarray, sample_rate: int, target_rate: int = TARGET_SAMPLE_RATE
) -> np.ndarray:
    """Resample mono audio to `target_rate` with a polyphase filter (scipy)."""
    rate = int(sample_rate)
    target = int(target_rate)
    if rate <= 0 or target <= 0:
        raise ValueError("sample_rate must be positive, got %d" % rate)
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if rate == target or audio.size == 0:
        return np.ascontiguousarray(audio, dtype=np.float32)
    factor = gcd(rate, target)
    out = resample_poly(audio, target // factor, rate // factor).astype(np.float32)
    return np.ascontiguousarray(out, dtype=np.float32)



# ---------------------------------------------------------------------------
# Output guard
#
# The Paraformer model is a Chinese recogniser, but it does not know when the
# input contains no speech: on 2026-10-26 two silent clips decoded to English
# sentences ("it is 懂了 and i is 用了").  Those hallucinations are worse than a
# miss, because the agent then acts on a command nobody said.  Two cheap checks
# run before/after decoding:
#   * digital silence (no peak above the noise floor) is not sent to the model;
#   * a transcript dominated by Latin letters cannot be a Chinese command.
# The values are deliberately conservative - real commands are pure Chinese.
# ---------------------------------------------------------------------------

#: Below this peak the clip carries no audible content at all.
SILENCE_PEAK_THRESHOLD = 0.005

#: A transcript with at least this many Latin letters and at most 1/Nth as many
#: Han characters is treated as noise rather than a command.
NON_CHINESE_MIN_LETTERS = 4
NON_CHINESE_LETTER_RATIO = 2.0


def peak_amplitude(samples) -> float:
    """Largest absolute sample value (0.0 for empty input)."""
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return 0.0
    return float(np.max(np.abs(audio)))


def is_probably_silence(samples, threshold: float = SILENCE_PEAK_THRESHOLD) -> bool:
    """True when the clip is too quiet to contain speech."""
    return peak_amplitude(samples) < float(threshold)


def _han_count(text: str) -> int:
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def _latin_letter_count(text: str) -> int:
    return sum(1 for ch in text if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))


def looks_non_chinese(text: str) -> bool:
    """True when a transcript is dominated by Latin letters.

    Chinese commands contain Han characters and digits only, so a transcript
    such as "i fuck me you can make you happy all now" is noise from the
    recogniser, not an instruction.
    """
    letters = _latin_letter_count(text)
    han = _han_count(text)
    if letters < NON_CHINESE_MIN_LETTERS:
        return False
    return letters > NON_CHINESE_LETTER_RATIO * han


class ChineseAsr:
    """Lazy wrapper around the local Paraformer Chinese recognizer.

    One instance is enough for a whole process; `decode_stream` is not reentrant,
    so do not share an instance across threads without a lock.
    """

    def __init__(
        self,
        model_dir: Optional[Union[str, Path]] = None,
        num_threads: int = DEFAULT_NUM_THREADS,
    ) -> None:
        self.model_dir = Path(model_dir) if model_dir is not None else MODEL_DIR
        self.num_threads = int(num_threads)
        self._recognizer = None  # type: Optional[object]
        self.load_seconds = 0.0

    # -- model lifecycle ---------------------------------------------------
    def _ensure_recognizer(self):
        """Load the model on first use and cache it on the instance."""
        if self._recognizer is None:
            self._recognizer = self._create_recognizer()
        return self._recognizer

    def _create_recognizer(self):
        # Imported here so that `import host.app.voice.asr` does not pay for it.
        import sherpa_onnx

        model_path = self.model_dir / MODEL_FILE
        tokens_path = self.model_dir / TOKENS_FILE
        if not model_path.is_file():
            raise FileNotFoundError("ASR model not found: %s" % model_path)
        if not tokens_path.is_file():
            raise FileNotFoundError("ASR tokens not found: %s" % tokens_path)

        started = time.perf_counter()
        recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(model_path),
            tokens=str(tokens_path),
            num_threads=self.num_threads,
            sample_rate=TARGET_SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            debug=False,
        )
        self.load_seconds = time.perf_counter() - started
        LOG.info("paraformer model loaded in %.2fs (threads=%d)", self.load_seconds, self.num_threads)
        return recognizer

    def warm_up(self) -> float:
        """Force the model load now; returns the load time in seconds."""
        self._ensure_recognizer()
        return self.load_seconds

    # -- recognition -------------------------------------------------------
    def transcribe(self, samples: np.ndarray, sample_rate: int,
                   guard: bool = True) -> str:
        """Return the recognized text for mono audio in [-1, 1].

        `samples` may be any real-sampling dtype; it is converted to float32 and
        resampled to 16 kHz when `sample_rate` differs.  Very short / empty input
        returns "" instead of raising.
        """
        audio = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
        if audio.size < MIN_SAMPLES:
            LOG.debug("audio too short (%d samples), returning empty text", audio.size)
            return ""
        if guard and is_probably_silence(audio):
            LOG.info("input below the silence floor (peak %.4f), skipping decode",
                     peak_amplitude(audio))
            return ""
        audio = np.clip(audio, -1.0, 1.0)
        audio = resample_to(audio, sample_rate)

        recognizer = self._ensure_recognizer()
        stream = recognizer.create_stream()
        stream.accept_waveform(TARGET_SAMPLE_RATE, audio)
        recognizer.decode_stream(stream)
        text = stream.result.text if stream.result is not None else ""
        text = str(text or "").strip()
        if guard and looks_non_chinese(text):
            LOG.warning("discarding non-Chinese transcript (noise): %r", text)
            return ""
        return text

    def transcribe_file(self, path: Union[str, Path]) -> str:
        """Read a wav file and return its recognized text."""
        audio, sample_rate = load_wav(path)
        return self.transcribe(audio, sample_rate)
