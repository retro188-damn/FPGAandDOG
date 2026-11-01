"""Capture a few seconds from every input device and report signal levels.

Used to find out *which* device and *which* mode actually carries speech, when
a known-good microphone suddenly reports silence.  Shared mode only: exclusive
mode can fail or return silence when another application holds the device.

    python tools/mic_sweep.py --seconds 3
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import sounddevice as sd

RATES = (48000, 44100)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--rate", type=int, default=0, help="0 = try 48k then 44.1k")
    args = ap.parse_args(argv)

    print("device_id | in_ch | name")
    candidates = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        print("%9d | %5d | %s" % (idx, dev["max_input_channels"], dev["name"]))
        candidates.append((idx, int(dev["max_input_channels"]), dev["name"]))

    print("\n=== capture sweep, %.1f s each (speak now!) ===" % args.seconds)
    try:
        default_in = sd.default.device[0]
    except Exception:  # pragma: no cover - depends on the host
        default_in = None
    print("default input device: %s" % (default_in,))

    for idx, chans, name in candidates:
        use = min(2, chans)
        for rate in ([args.rate] if args.rate else RATES):
            try:
                with sd.InputStream(device=idx, channels=use, samplerate=rate,
                                    dtype="float32") as stream:
                    frames = int(args.seconds * rate)
                    data, _ = stream.read(frames)
                break
            except Exception as exc:  # try the next rate
                data = None
                last_error = exc
        if data is None:
            print("%9d | %-28s | FAILED: %s" % (idx, name[:28], last_error))
            continue
        mono = data.reshape(len(data), -1).mean(axis=1)
        blocks = np.array_split(mono, max(1, int(args.seconds * 4)))
        levels = [rms(b) for b in blocks]
        print("%9d | %-28s | rate=%5d ch=%d rms=%.5f peak=%.4f min_rms=%.5f max_rms=%.5f"
              % (idx, name[:28], rate, use, rms(mono), float(np.max(np.abs(mono))),
                 min(levels), max(levels)))
    print("=== done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
