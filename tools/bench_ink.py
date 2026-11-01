"""Which ink colours still read correctly?

"黑笔" is a demo instruction, not a technical requirement: the chain converts the
frame to grayscale, so hue does not matter and *luminance contrast against the
paper* does.  This script answers the question with numbers instead of opinion by
replaying the same realistic scene pipeline for several ink colours.

    python _scratch/bench_ink.py --per-colour 80
"""
import argparse
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host" / "app"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "_scratch"))

from bench_digits import CACHE, find_coeffs, load_idx  # noqa: E402
from vision.digit_reader import load_classifier, read_digits  # noqa: E402

INKS = {
    "black(30)": (30, 30, 30),
    "blue(40,60,190)": (40, 60, 190),
    "red(200,35,35)": (200, 35, 35),
    "pencil(120)": (120, 120, 120),
    "pencil-light(155)": (155, 155, 155),
}
PAPER = (248, 247, 242)


def card_image(glyph, ink):
    """White card with one glyph drawn in the given colour."""
    alpha = Image.fromarray(glyph).resize((150, 150), Image.BILINEAR)
    card = Image.new("RGB", (240, 240), PAPER)
    ink_layer = Image.new("RGB", (240, 240), ink)
    card.paste(ink_layer.crop((0, 0, 150, 150)), (45, 45), alpha)
    return card


def scene(background, card, rng):
    scale = rng.uniform(0.55, 1.05)
    side = int(240 * scale)
    cx = int(rng.uniform(0.30, 0.70) * background.width)
    cy = int(rng.uniform(0.55, 0.80) * background.height)
    dst = [(cx - side / 2, cy - side / 2), (cx + side / 2, cy - side / 2),
           (cx + side / 2, cy + side / 2), (cx - side / 2, cy + side / 2)]
    src = [(0, 0), (240, 0), (240, 240), (0, 240)]
    coefs = find_coeffs(dst, src)
    warped = card.transform((background.width, background.height), Image.PERSPECTIVE,
                            coefs, Image.BICUBIC, fillcolor=(0, 0, 0))
    mask = Image.new("L", card.size, 255).transform(
        (background.width, background.height), Image.PERSPECTIVE, coefs,
        Image.BICUBIC, fillcolor=0)
    out = background.copy()
    if rng.random() < 0.5:
        shadow = mask.filter(ImageFilter.GaussianBlur(9))
        out.paste(Image.new("RGB", out.size, (90, 90, 90)),
                  (int(rng.uniform(-8, 8)), int(rng.uniform(-8, 8))),
                  shadow.point(lambda v: v // 3))
    out.paste(warped, (0, 0), mask)

    gain = rng.uniform(0.75, 1.15)
    out = Image.eval(out, lambda v: int(min(255, v * gain)))
    if rng.random() < 0.6:
        out = out.filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 1.3)))
    array = np.asarray(out).astype(np.float32)
    array += rng.normal(0, rng.uniform(1.5, 5.0), array.shape).astype(np.float32)
    out = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))
    buffer = io.BytesIO()
    out.save(buffer, format="JPEG", quality=int(rng.uniform(60, 90)))
    buffer.seek(0)
    return Image.open(buffer).convert("RGB"), dst


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="", help="classifier npz to load")
    parser.add_argument("--per-colour", type=int, default=80)
    parser.add_argument("--background", default=str(ROOT / "_scratch" / "cam_test.jpg"))
    args = parser.parse_args()

    classifier = load_classifier(Path(args.model) if args.model else None)
    if classifier is None:
        print("train first: python tools/train_digits.py")
        return 2
    images = load_idx(CACHE / "t10k-images-idx3-ubyte.gz", True)
    labels = load_idx(CACHE / "t10k-labels-idx1-ubyte.gz", False)
    background = Image.open(args.background).convert("RGB")

    print(f"{'ink':22s} {'correct':>8s} {'wrong':>6s} {'missed':>7s} {'end-to-end':>11s}")
    for name, ink in INKS.items():
        rng = np.random.default_rng(3)
        hits = wrong = missed = 0
        for _ in range(args.per_colour):
            truth = int(rng.integers(0, 10))
            pick = int(rng.choice(np.flatnonzero(labels == truth)))
            frame, _ = scene(background, card_image(images[pick], ink), rng)
            text, _ = read_digits(frame, classifier)
            if not text:
                missed += 1
            elif text == str(truth):
                hits += 1
            else:
                wrong += 1
        total = args.per_colour
        print(f"{name:22s} {hits:8d} {wrong:6d} {missed:7d} {hits / total * 100:10.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
