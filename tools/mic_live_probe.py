"""One-shot live probe: record -> save wav -> transcribe -> parse intent.

Dev utility for bringing up the microphone on the laptop:

    python tools/mic_live_probe.py --seconds 8 --device 9 --exclusive --gain 20 --channels 2

Prints UTF-8 regardless of the console code page and reports the RMS of every
0.5 s slice, so a "did it actually hear me" question does not depend on the
transcript alone.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import wave
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from host.app.voice.asr import MODEL_DIR, ChineseAsr  # noqa: E402
from host.app.voice.intent import parse_intent  # noqa: E402
from host.app.voice.mic import record  # noqa: E402


def slice_levels(audio: np.ndarray, rate: int, step: float = 0.5) -> str:
    n = max(1, int(rate * step))
    parts = []
    for start in range(0, len(audio), n):
        chunk = audio[start : start + n]
        parts.append("%.4f" % float(np.abs(chunk).max()) if chunk.size else "0")
    return " ".join(parts)


def save_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--device", type=int, default=9)
    parser.add_argument("--exclusive", action="store_true")
    parser.add_argument("--channels", type=int, default=2, help="capture channels (this array is 2ch; 1ch exclusive is broken)")
    parser.add_argument("--gain", type=float, default=60.0)
    parser.add_argument("--wav", default="metrics/evidence/mic_probe.wav")
    parser.add_argument("--normalize", action="store_true", help="peak-normalise to 0.9 before ASR")
    parser.add_argument("--model-dir", default=None)
    args = parser.parse_args()

    print("recording %.1fs ..." % args.seconds, flush=True)
    audio = record(
        args.seconds, 16000, device=args.device, exclusive=args.exclusive,
        gain=args.gain, channels=args.channels,
    )
    print("captured %d samples, peak=%.5f rms=%.5f"
          % (audio.size, float(np.abs(audio).max()), float(np.sqrt((audio * audio).mean()))))
    print("levels/0.5s:", slice_levels(audio, 16000))
    if args.normalize:
        peak = float(np.abs(audio).max())
        if peak > 1e-6:
            audio = np.clip(audio * (0.9 / peak), -1.0, 1.0)
            print("normalised by %.1fx (peak was %.5f)" % (0.9 / peak, peak))

    wav_path = ROOT / args.wav
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    save_wav(wav_path, audio, 16000)
    print("wav:", wav_path)

    asr = ChineseAsr(model_dir=args.model_dir or MODEL_DIR)
    text = asr.transcribe(audio, 16000)
    print("transcript:", repr(text))
    print("intent:", parse_intent(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
