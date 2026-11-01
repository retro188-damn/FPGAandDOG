"""Stress the digit pipeline on perturbations of real captured frames.

The clean-scene benchmark (tools/bench_digits.py) says 97.5%, but the arena is
not clean: the card is hand-held, the camera tilts, light is uneven and the
background is busy.  This harness takes a few *real* frames with a known digit
and re-measures the same reader under controlled perturbations, so the report
can say where the current pipeline actually breaks (and a vision model would
have to beat this baseline).

    python tools/bench_stress.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.app.vision.digit_reader import (  # noqa: E402
    load_classifier,
    read_digits,
    read_digits_escalate,
    read_digits_robust,
)

#: (file, expected digit) - real frames captured on 2026-10-26 with the dog.
CASES = [
    ("metrics/evidence/digit_20261026T201131.png", "2"),
    ("metrics/evidence/digit_20261026T201818.png", "7"),
    ("metrics/evidence/digit_20261026T202705.png", "3"),
    ("metrics/evidence/digit_20261026T204009.png", "2"),
]


def _crop_frame(img: Image.Image) -> Image.Image:
    """Drop the evidence header strip drawn by digit_live.py."""
    return img.crop((0, 26, img.width, img.height))


def variants(img: Image.Image):
    """Yield (name, perturbed image) pairs; every perturbation is realistic."""
    w, h = img.size
    yield "clean", img
    for deg in (-8, 8, -15, 15):
        yield "rotate%+d" % deg, img.rotate(deg, resample=Image.BILINEAR)
    for factor in (0.6, 0.75, 1.3):
        yield "bright%.2f" % factor, ImageEnhance.Brightness(img).enhance(factor)
    yield "contrast0.6", ImageEnhance.Contrast(img).enhance(0.6)
    yield "blur1.2", img.filter(ImageFilter.GaussianBlur(1.2))
    yield "blur2.0", img.filter(ImageFilter.GaussianBlur(2.0))
    # Perspective: the operator rarely holds the card square to the camera.
    corners = ((0, 0), (w, 0), (w, h), (0, h))
    for dx in (-14, 14):
        moved = ((dx, 0), (w + dx, 0), (w - dx, h), (-dx, h))
        yield "persp%+d" % dx, img.transform(
            (w, h), Image.PERSPECTIVE, _perspective_coeffs(corners, moved),
            resample=Image.BILINEAR,
        )


def _perspective_coeffs(src, dst):
    """Solve the 8 coefficients mapping src corners to dst corners."""
    a = []
    b = []
    for (x, y), (u, v) in zip(src, dst):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b.append(v)
    return np.linalg.solve(np.array(a, dtype=np.float64),
                           np.array(b, dtype=np.float64)).flatten().tolist()


READERS = {"plain": read_digits, "robust": read_digits_robust,
           "escalate": read_digits_escalate}


def _run(reader: str, clf, verbose: bool):
    rows = []
    for rel, expected in CASES:
        path = ROOT / rel
        if not path.exists():
            print("missing frame: %s" % rel)
            continue
        base = _crop_frame(Image.open(path).convert("RGB"))
        for name, img in variants(base):
            text, _details = READERS[reader](img, clf)
            rows.append((rel.split("/")[-1], expected, name, text))
    total = len(rows)
    right = sum(1 for r in rows if r[3] == r[1])
    wrong = sum(1 for r in rows if r[3] and r[3] != r[1])
    missed = total - right - wrong
    if verbose:
        print("")
        print("%-34s %-6s %-14s %-6s" % ("frame", "want", "variant", "got"))
        for frame, want, variant, got in rows:
            mark = "ok" if got == want else ("WRONG" if got else "miss")
            print("%-34s %-6s %-14s %-6s %s" % (frame, want, variant, got or "-", mark))
    print("%-7s: %d cases, %d right (%.1f%%), %d wrong, %d missed"
          % (reader, total, right, 100.0 * right / max(1, total), wrong, missed))
    return {r[2]: r[3] for r in rows}, right, total


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="", help="classifier npz to load")
    ap.add_argument("--reader", default="both",
                    choices=["plain", "robust", "escalate", "all"])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    clf = load_classifier(Path(args.model) if args.model else None)
    if clf is None:
        print("classifier not found: host/model/digits_mlp.npz")
        return 2

    if args.reader == "all":
        results = {}
        for name in ("plain", "robust", "escalate"):
            results[name] = _run(name, clf, args.verbose)
        plain_right, total = results["plain"][1], results["plain"][2]
        for name in ("robust", "escalate"):
            print("%-8s vs plain: %+d case(s) over %d"
                  % (name, results[name][1] - plain_right, total))
        return 0

    if args.reader == "both":
        plain, plain_right, total = _run("plain", clf, args.verbose)
        robust, robust_right, _ = _run("robust", clf, args.verbose)
        gain = robust_right - plain_right
        print("")
        print("robust vs plain: %+d case(s) over %d" % (gain, total))
        diff = [(k, plain[k], robust[k]) for k in plain if plain[k] != robust[k]]
        for variant, a, b in diff:
            print("  %-14s plain=%-4s robust=%s" % (variant, a or "-", b or "-"))
        return 0 if gain >= 0 else 1

    _mapping, right, total = _run(args.reader, clf, True)
    return 0 if right == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
