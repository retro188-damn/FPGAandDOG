"""Command-line demo for the speech-input chain (mic/wav -> text -> intent).

Examples::

    python -m host.app.voice.demo --selftest
    python -m host.app.voice.demo --wav 需要的材料/.../test_wavs/0.wav
    python -m host.app.voice.demo --mic 5
    python -m host.app.voice.demo --text "去3号位置并查看板子上的数字"

`--wav`, `--mic` and `--text` print exactly one line of intent JSON on stdout
(with non-ASCII characters kept readable) so the output can be piped into
another program; all log lines go to stderr.
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow `python host/app/voice/demo.py` as well as `python -m host.app.voice.demo`.
if __package__ in (None, ""):  # pragma: no cover - only when run as a file
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from host.app.voice import setup_logging  # noqa: E402
from host.app.voice.asr import (  # noqa: E402
    MODEL_DIR,
    TEST_WAV_DIR,
    ChineseAsr,
    load_wav,
)
from host.app.voice.intent import parse_intent  # noqa: E402

LOG_NAME = "demo"
log = logging.getLogger(LOG_NAME)


def _emit(intent: Dict[str, Any]) -> None:
    """Print the intent dict as one JSON line, readable (no \\uXXXX escapes)."""
    print(json.dumps(intent, ensure_ascii=False))


def run_selftest(asr: ChineseAsr) -> int:
    """Transcribe every wav in the model's `test_wavs/` and report durations/RTF."""
    wav_dir = asr.model_dir / TEST_WAV_DIR
    wavs = sorted(wav_dir.glob("*.wav"))  # type: List[Path]
    if not wavs:
        log.error("no test wavs under %s", wav_dir)
        return 2

    load_seconds = asr.warm_up()
    print("model: %s" % asr.model_dir)
    print("load: %.2fs   threads: %d" % (load_seconds, asr.num_threads))
    print("%-14s %8s %10s %6s  %s" % ("wav", "dur[s]", "decode[s]", "rtf", "text"))

    total_audio = 0.0
    total_decode = 0.0
    failures = 0
    for path in wavs:
        try:
            audio, sample_rate = load_wav(path)
            duration = audio.size / float(sample_rate)
            started = time.perf_counter()
            text = asr.transcribe(audio, sample_rate)
            elapsed = time.perf_counter() - started
        except Exception as exc:  # keep going: one bad file must not hide the rest
            failures += 1
            log.error("selftest failed on %s: %s", path.name, exc)
            print("%-14s %8s %10s %6s  FAILED %s" % (path.name, "-", "-", "-", exc))
            continue
        total_audio += duration
        total_decode += elapsed
        rtf = elapsed / duration if duration > 0 else 0.0
        print("%-14s %8.2f %10.3f %6.3f  %s" % (path.name, duration, elapsed, rtf, text))

    mean_rtf = total_decode / total_audio if total_audio > 0 else 0.0
    print("-" * 72)
    print(
        "wavs=%d  failures=%d  audio=%.2fs  decode=%.3fs  mean_rtf=%.3f  (model load %.2fs)"
        % (len(wavs), failures, total_audio, total_decode, mean_rtf, load_seconds)
    )
    return 0 if failures == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m host.app.voice.demo",
        description="Speech-input demo: wav/mic -> Chinese text -> intent JSON.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wav", metavar="PATH", help="transcribe a wav file, then print the intent")
    source.add_argument("--mic", metavar="SECONDS", type=float, help="record N seconds, then print the intent")
    source.add_argument("--selftest", action="store_true", help="transcribe test_wavs/*.wav and report RTF")
    source.add_argument("--text", metavar="TEXT", help="parse this text without touching the microphone")
    parser.add_argument("--model-dir", default=None, help="override the ASR model directory")
    parser.add_argument("--mic-vad", action="store_true", help="with --mic: stop early when the speaker pauses")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input device index")
    parser.add_argument("--exclusive", action="store_true", help="open the device in WASAPI exclusive mode")
    parser.add_argument("--gain", type=float, default=1.0, help="software gain after capture")
    parser.add_argument(
        "--channels", type=int, default=2,
        help="capture channels: 2 is REQUIRED on the dev-PC array (1ch exclusive is broken)",
    )
    parser.add_argument(
        "--vad-threshold", type=float, default=0.015,
        help="RMS threshold that marks speech start/end (2ch array is quiet: use ~0.004)",
    )
    args = parser.parse_args(argv)

    setup_logging()

    if args.text is not None:
        _emit(parse_intent(args.text))
        return 0

    asr = ChineseAsr(model_dir=args.model_dir or MODEL_DIR)

    if args.selftest:
        return run_selftest(asr)

    if args.wav:
        wav_path = Path(args.wav)
        if not wav_path.is_file():
            log.error("wav file not found: %s", wav_path)
            return 2
        text = asr.transcribe_file(wav_path)
        log.info("transcript: %s", text)
        _emit(parse_intent(text))
        return 0

    # --mic
    from host.app.voice.mic import record, record_with_vad_endpoint

    seconds = float(args.mic)
    try:
        if args.mic_vad:
            audio = record_with_vad_endpoint(
                max_seconds=max(seconds, 1.0),
                device=args.device, exclusive=args.exclusive, gain=args.gain,
                channels=args.channels, rms_threshold=args.vad_threshold,
            )
        else:
            audio = record(
                seconds, device=args.device, exclusive=args.exclusive, gain=args.gain,
                channels=args.channels,
            )
    except RuntimeError as exc:  # missing sounddevice / no input device
        log.error("%s", exc)
        return 3
    except ValueError as exc:
        log.error("bad recording request: %s", exc)
        return 2

    if audio.size == 0:
        log.warning("nothing recorded")
    text = asr.transcribe(audio, 16000)
    log.info("transcript: %s", text)
    _emit(parse_intent(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
