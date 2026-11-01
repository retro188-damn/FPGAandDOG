"""Render handwritten-looking digits from system fonts with camera degradations.

Why this exists
---------------
The classifier is trained on MNIST, but the demo feeds it *photographs of marker
handwriting seen from a low camera at a shallow angle*.  Two things MNIST never
shows dominate our failures:

* the glyph arrives **vertically squashed** (a card 1.5 m away under a camera
  0.31 m high is seen at ~12 degrees, so a square digit becomes ~2:1 flat -
  measured: a hand-written 8 arrives as 56x28 px), and
* the stroke is **thin**, because a marker line is 2-4 px wide at that range.

So we synthesise exactly that: hundreds of fonts (many handwriting-like), then
squash, rotate, shear, thin/thicken, blur and add sensor noise before the same
normalise() the live reader uses.

Usage
-----
    python tools/render_digits.py --per-digit 6000 --out .cache/synth_digits.npz
"""
from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
from host.app.vision.digit_reader import normalise  # noqa: E402

FONT_DIRS = ("C:/Windows/Fonts", "/usr/share/fonts/truetype/*")
PREFERRED = ("comic", "hand", "script", "brush", "inkfree", "lhandw", "segoepr",
             "segoesc", "pala", "calibri", "arial", "times", "verdana")


def font_paths(limit: int = 160) -> list:
    found = []
    for pattern in FONT_DIRS:
        found += glob.glob(os.path.join(pattern, "*.ttf")) if "*" not in pattern \
            else glob.glob(pattern)
        found += glob.glob(os.path.join(pattern, "*.ttc")) if "*" not in pattern else []
    usable = []
    for path in found:
        try:
            ImageFont.truetype(path, 40)
        except Exception:
            continue
        usable.append(path)
    # handwriting-ish first, then everything else, deterministic order
    def rank(path):
        name = os.path.basename(path).lower()
        return (0 if any(k in name for k in PREFERRED) else 1, name)

    usable.sort(key=rank)
    return usable[:limit] if limit else usable


def _squash(patch: np.ndarray, factor: float) -> np.ndarray:
    """Vertical foreshortening: what a floor card looks like from a low camera."""
    h, w = patch.shape
    if abs(factor - 1.0) < 1e-3:
        return patch
    small = np.asarray(Image.fromarray(patch).resize((w, max(2, int(h * factor))),
                                                     Image.BILINEAR))
    out = np.zeros_like(patch)
    top = (h - small.shape[0]) // 2
    out[top:top + small.shape[0]] = small
    return out


_FONT_CACHE: dict = {}


def _font(font_path: str, size: int):
    """truetype() is the slow part (~10 ms); cache one font per (path, size)."""
    key = (font_path, size)
    font = _FONT_CACHE.get(key)
    if font is None:
        font = ImageFont.truetype(font_path, size)
        _FONT_CACHE[key] = font
    return font


def render_one(digit: int, font_path: str, rng: np.random.Generator) -> np.ndarray:
    # sizes are bucketed so the cache actually hits: 8 buckets instead of 60
    size = int(rng.integers(8, 11)) * 10
    font = _font(font_path, size)
    canvas = Image.new("L", (176, 176), 255)
    draw = ImageDraw.Draw(canvas)
    # random placement so the crop is not always centred
    x = int(rng.integers(18, 56))
    y = int(rng.integers(6, 34))
    draw.text((x, y), str(digit), font=font, fill=int(rng.integers(0, 70)))

    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    ink = arr < float(rng.uniform(0.55, 0.85))
    if ink.sum() < 60:
        return np.zeros((28, 28), dtype=np.float32)

    # stroke weight: erode for a hairline, dilate for a fat marker
    weight = rng.random()
    if weight < 0.30:
        ink = ndimage.binary_erosion(ink, iterations=1)
    elif weight > 0.80:
        ink = ndimage.binary_dilation(ink, iterations=1)
    if ink.sum() < 40:
        return np.zeros((28, 28), dtype=np.float32)

    # geometry: rotate, shear, and squash exactly like a floor card
    patch = ink.astype(np.float32)
    angle = float(rng.uniform(-32, 32))
    patch = ndimage.rotate(patch, angle, reshape=True, order=0, mode="constant")
    shear = float(rng.uniform(-0.30, 0.30))
    patch = ndimage.affine_transform(patch, np.array([[1.0, shear], [0.0, 1.0]]),
                                     offset=0.0, order=0, mode="constant")
    squash = float(rng.uniform(0.45, 0.95))
    patch = _squash((patch > 0.5).astype(np.uint8).astype(np.float32) * 255.0, squash)

    mask = patch > 127
    if mask.sum() < 40:
        return np.zeros((28, 28), dtype=np.float32)

    # focus + noise, then the very same normalisation the reader uses
    soft = ndimage.gaussian_filter(mask.astype(np.float32), float(rng.uniform(0.0, 0.8)))
    soft = np.clip(soft * float(rng.uniform(0.7, 1.0)), 0.0, 1.0)
    return normalise(soft)


def build(per_digit: int, seed: int = 0, fonts: int = 160):
    rng = np.random.default_rng(seed)
    paths = font_paths(fonts)
    print(f"fonts: {len(paths)}")
    images, labels = [], []
    for digit in range(10):
        made = 0
        guard = 0
        while made < per_digit and guard < per_digit * 20:
            guard += 1
            glyph = render_one(digit, paths[int(rng.integers(len(paths)))], rng)
            if glyph.max() <= 0:
                continue
            images.append(glyph.reshape(-1))
            labels.append(digit)
            made += 1
        print(f"  digit {digit}: {made}")
    return np.stack(images).astype(np.float32), np.asarray(labels, dtype=np.int64)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-digit", type=int, default=6000)
    ap.add_argument("--fonts", type=int, default=160)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=".cache/synth_digits.npz")
    args = ap.parse_args()
    x, y = build(args.per_digit, args.seed, args.fonts)
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, x=x, y=y)
    print(f"wrote {out}  x={x.shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
