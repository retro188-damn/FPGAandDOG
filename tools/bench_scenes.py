"""Offline estimate of the digit chain on camera-like scenes.

The real test is a card on the floor in front of the dog, but that needs a person;
this benchmark composites MNIST glyphs onto the *actual* floor frame captured by
the head camera, with a perspective warp, paper shadow, sensor noise and JPEG
compression.  It answers "what accuracy should I expect in the room", not "what
is the MNIST accuracy".

    python _scratch/bench_digits.py --count 200
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

from vision.digit_reader import load_classifier, read_digits  # noqa: E402

CACHE = ROOT / ".cache" / "mnist"


def load_idx(path, images):
    import gzip
    import struct
    with gzip.open(path, "rb") as fh:
        magic, count = struct.unpack(">II", fh.read(8))
        if images:
            rows, cols = struct.unpack(">II", fh.read(8))
            return np.frombuffer(fh.read(), np.uint8).reshape(count, rows, cols)
        return np.frombuffer(fh.read(), np.uint8).reshape(count)


def find_coeffs(dst, src):
    """Coefficients for PIL's PERSPECTIVE transform (maps dst -> src)."""
    matrix = []
    for (x, y), (u, v) in zip(dst, src):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    a = np.array(matrix, dtype=np.float64)
    b = np.array(src, dtype=np.float64).reshape(8)
    return np.linalg.lstsq(a, b, rcond=None)[0]


def make_scene(background, glyph, rng):
    """Paste a perspective-warped digit card onto the floor frame."""
    card = Image.new("RGB", (240, 240), (248, 247, 242))
    glyph_img = Image.fromarray((255 - glyph).astype(np.uint8)).resize((150, 150), Image.BILINEAR)
    card.paste(glyph_img.convert("RGB"), (45, 45))

    # position on the floor: lower half, random x, size falls off with distance
    scale = rng.uniform(0.55, 1.05)
    side = int(240 * scale)
    cx = int(rng.uniform(0.30, 0.70) * background.width)
    cy = int(rng.uniform(0.55, 0.80) * background.height)
    jitter = side * 0.10
    dst = [(cx - side / 2, cy - side / 2), (cx + side / 2, cy - side / 2),
           (cx + side / 2, cy + side / 2), (cx - side / 2, cy + side / 2)]
    src = [(0, 0), (240, 0), (240, 240), (0, 240)]
    warped = card.transform((background.width, background.height), Image.PERSPECTIVE,
                            find_coeffs(dst, src), Image.BICUBIC,
                            fillcolor=(0, 0, 0))
    mask = Image.new("L", card.size, 255).transform(
        (background.width, background.height), Image.PERSPECTIVE, find_coeffs(dst, src),
        Image.BICUBIC, fillcolor=0)

    scene = background.copy()
    if rng.random() < 0.5:  # a soft shadow offset under the card
        shadow = mask.filter(ImageFilter.GaussianBlur(9))
        scene.paste(Image.new("RGB", scene.size, (90, 90, 90)),
                    (int(rng.uniform(-8, 8)), int(rng.uniform(-8, 8))), shadow.point(lambda v: v // 3))
    scene.paste(warped, (0, 0), mask)

    # sensor-level effects: dim lighting, noise, blur, jpeg
    gain = rng.uniform(0.75, 1.15)
    scene = Image.eval(scene, lambda v: int(min(255, v * gain)))
    if rng.random() < 0.6:
        scene = scene.filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 1.3)))
    array = np.asarray(scene).astype(np.float32)
    array += rng.normal(0, rng.uniform(1.5, 5.0), array.shape).astype(np.float32)
    scene = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))
    buffer = io.BytesIO()
    scene.save(buffer, format="JPEG", quality=int(rng.uniform(60, 90)))
    buffer.seek(0)
    return Image.open(buffer).convert("RGB"), dst


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="", help="classifier npz to load")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--background", default=str(ROOT / "_scratch" / "cam_test.jpg"))
    parser.add_argument("--save-failures", type=int, default=6)
    args = parser.parse_args()

    classifier = load_classifier(Path(args.model) if args.model else None)
    if classifier is None:
        print("train the model first: python tools/train_digits.py")
        return 2
    images = load_idx(CACHE / "t10k-images-idx3-ubyte.gz", True)
    labels = load_idx(CACHE / "t10k-labels-idx1-ubyte.gz", False)
    background = Image.open(args.background).convert("RGB")
    rng = np.random.default_rng(7)

    out_dir = ROOT / "metrics" / "evidence" / "digit_bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    hits = missed = empty = 0
    confusion = np.zeros((10, 10), dtype=int)
    failures = []
    for index in range(args.count):
        truth = int(rng.integers(0, 10))
        candidates = np.flatnonzero(labels == truth)
        pick = int(rng.choice(candidates))
        scene, _ = make_scene(background, images[pick], rng)
        text, details = read_digits(scene, classifier)
        if not text:
            empty += 1
        elif text == str(truth):
            hits += 1
        else:
            missed += 1
            confusion[truth, int(text[0])] += 1
            if len(failures) < args.save_failures:
                failures.append((index, truth, text, scene))

    total = args.count
    print(f"scenes={total}  correct={hits}  wrong={missed}  nothing-detected={empty}")
    print(f"accuracy on detected = {hits / max(1, hits + missed) * 100:.1f}%"
          f"   end-to-end = {hits / total * 100:.1f}%")
    if missed:
        print("confusion (row=true, col=read), non-zero only:")
        for truth in range(10):
            row = {d: int(confusion[truth, d]) for d in range(10) if confusion[truth, d]}
            if row:
                print(f"  {truth}: {row}")
    for index, truth, text, scene in failures:
        path = out_dir / f"fail_{index:03d}_true{truth}_read{text}.png"
        scene.save(path)
        print(f"  saved {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
