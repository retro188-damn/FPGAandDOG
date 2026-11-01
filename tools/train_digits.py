"""Train a small dense network for handwritten digit recognition.

The demo needs digits read off a card lying on the floor, so the classifier has
to cope with hand-written strokes rather than a single printed font.  MNIST is
the lightest realistic source (11 MB of IDX files, no framework needed), and a
two-layer network trained with plain numpy reaches ~98% while running in well
under a millisecond per glyph on the laptop CPU.

Usage
-----
    python tools/train_digits.py                # train + evaluate + save
    python tools/train_digits.py --epochs 6     # quick pass

Outputs
-------
    host/model/digits_mlp.npz   weights + normalisation constants
    docs/digit_model.md         evaluation report (accuracy, confusion)
"""
from __future__ import annotations

import argparse
import gzip
import json
import struct
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "mnist"
MODEL = ROOT / "host" / "model" / "digits_mlp.npz"
REPORT = ROOT / "docs" / "digit_model.md"

FILES = {
    "train_images": "train-images-idx3-ubyte.gz",
    "train_labels": "train-labels-idx1-ubyte.gz",
    "test_images": "t10k-images-idx3-ubyte.gz",
    "test_labels": "t10k-labels-idx1-ubyte.gz",
}


def load_idx(path: Path) -> np.ndarray:
    """Parse an IDX file (images or labels) into a numpy array."""
    with gzip.open(path, "rb") as handle:
        magic, count = struct.unpack(">II", handle.read(8))
        if magic == 2051:  # images
            rows, cols = struct.unpack(">II", handle.read(8))
            data = np.frombuffer(handle.read(), dtype=np.uint8)
            return data.reshape(count, rows * cols)
        if magic == 2049:  # labels
            return np.frombuffer(handle.read(), dtype=np.uint8).reshape(count)
        raise ValueError(f"unsupported IDX magic {magic} in {path}")


def augment(images: np.ndarray, rng: np.random.Generator, shifts: int = 2) -> np.ndarray:
    """Shift each glyph by up to ``shifts`` pixels; cheap and stable."""
    batch = images.reshape(-1, 28, 28)
    out = np.empty_like(batch)
    for index, glyph in enumerate(batch):
        dy = int(rng.integers(-shifts, shifts + 1))
        dx = int(rng.integers(-shifts, shifts + 1))
        out[index] = np.roll(np.roll(glyph, dy, axis=0), dx, axis=1)
    return out.reshape(images.shape[0], -1)


def _fit28(patch: np.ndarray) -> np.ndarray:
    """Crop or pad a 2-D patch so it is exactly 28x28, keeping the centre."""
    out = np.zeros((28, 28), dtype=patch.dtype)
    h, w = patch.shape
    h, w = min(h, 28), min(w, 28)
    y0, x0 = (patch.shape[0] - h) // 2, (patch.shape[1] - w) // 2
    ty, tx = (28 - h) // 2, (28 - w) // 2
    out[ty:ty + h, tx:tx + w] = patch[y0:y0 + h, x0:x0 + w]
    return out


def augment_affine(images: np.ndarray, rng: np.random.Generator,
                   max_rotate: float = 18.0, max_scale: float = 0.15,
                   max_shift: float = 2.0, max_shear: float = 0.15,
                   thickness: bool = False, photometric: bool = False,
                   max_squash: float = 0.0) -> np.ndarray:
    """Rotate/scale/shear/thicken each glyph: the card is never perfectly square.

    The reader normalises a glyph into a 28x28 box, but a card lying at an angle
    reaches the classifier rotated by up to ~20 degrees, and a marker pen draws
    either a hairline or a fat stroke.  Training on those variants is what makes
    inference-time rotation tricks unnecessary.
    """
    from scipy import ndimage

    batch = images.reshape(-1, 28, 28)
    out = np.empty_like(batch)
    for index, glyph in enumerate(batch):
        angle = float(rng.uniform(-max_rotate, max_rotate))
        scale = float(1.0 + rng.uniform(-max_scale, max_scale))
        shear = float(rng.uniform(-max_shear, max_shear))
        dy = float(rng.uniform(-max_shift, max_shift))
        dx = float(rng.uniform(-max_shift, max_shift))
        warped = ndimage.rotate(glyph, angle, reshape=False, order=1, mode="constant")
        warped = ndimage.zoom(warped, scale, order=1)
        warped = _fit28(warped)
        if max_squash:
            # a floor card under a low camera arrives vertically foreshortened
            from PIL import Image as _PILImage

            factor = float(1.0 - rng.uniform(0.0, max_squash))
            squeezed = _PILImage.fromarray((warped * 255).astype(np.uint8)).resize(
                (28, max(2, int(round(28 * factor)))), _PILImage.BILINEAR)
            warped = _fit28(np.asarray(squeezed, dtype=np.float32) / 255.0)
        matrix = np.array([[1.0, shear], [0.0, 1.0]], dtype=np.float64)
        warped = ndimage.affine_transform(warped, matrix, offset=[dy, dx],
                                          order=1, mode="constant")
        if thickness:
            mask = warped > 0.35
            if rng.random() < 0.5:
                mask = ndimage.binary_dilation(mask, iterations=1)
            else:
                mask = ndimage.binary_erosion(mask, iterations=1)
            warped = np.where(mask, np.maximum(warped, 0.55), warped)
        if photometric:
            gamma = float(rng.uniform(0.7, 1.45))
            warped = np.clip(warped, 1e-3, 1.0) ** gamma
            if rng.random() < 0.5:
                warped = ndimage.gaussian_filter(warped, sigma=float(rng.uniform(0.4, 0.9)))
        out[index] = np.clip(warped, 0.0, 1.0)
    return out.reshape(images.shape[0], -1)


def one_hot(labels: np.ndarray, classes: int = 10) -> np.ndarray:
    out = np.zeros((labels.size, classes), dtype=np.float32)
    out[np.arange(labels.size), labels] = 1.0
    return out


def train(x_train: np.ndarray, y_train: np.ndarray, hidden: int, epochs: int,
          batch: int, lr: float, seed: int = 0) -> dict:
    """Adam-optimised 784 -> hidden -> 10 network."""
    rng = np.random.default_rng(seed)
    n_in = x_train.shape[1]
    w1 = (rng.standard_normal((n_in, hidden)) * np.sqrt(2.0 / n_in)).astype(np.float32)
    b1 = np.zeros(hidden, dtype=np.float32)
    w2 = (rng.standard_normal((hidden, 10)) * np.sqrt(2.0 / hidden)).astype(np.float32)
    b2 = np.zeros(10, dtype=np.float32)
    params = [w1, b1, w2, b2]
    moments = [np.zeros_like(p) for p in params]
    velocities = [np.zeros_like(p) for p in params]
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    step = 0
    n = x_train.shape[0]

    for epoch in range(1, epochs + 1):
        order = rng.permutation(n)
        total_loss = 0.0
        for start in range(0, n, batch):
            idx = order[start:start + batch]
            x = x_train[idx]
            y = one_hot(y_train[idx])
            h_pre = x @ w1 + b1
            h = np.maximum(h_pre, 0.0)
            logits = h @ w2 + b2
            logits -= logits.max(axis=1, keepdims=True)
            probs = np.exp(logits)
            probs /= probs.sum(axis=1, keepdims=True)
            total_loss += float(-np.log(probs[np.arange(len(idx)), y_train[idx]] + 1e-9).mean()) * len(idx)

            d_logits = probs - y
            d_w2 = h.T @ d_logits / len(idx)
            d_b2 = d_logits.mean(axis=0)
            d_h = (d_logits @ w2.T) * (h_pre > 0)
            d_w1 = x.T @ d_h / len(idx)
            d_b1 = d_h.mean(axis=0)
            grads = [d_w1, d_b1, d_w2, d_b2]

            step += 1
            for i, (p, g) in enumerate(zip(params, grads)):
                moments[i] = beta1 * moments[i] + (1 - beta1) * g
                velocities[i] = beta2 * velocities[i] + (1 - beta2) * (g * g)
                m_hat = moments[i] / (1 - beta1 ** step)
                v_hat = velocities[i] / (1 - beta2 ** step)
                p -= lr * m_hat / (np.sqrt(v_hat) + eps)
        print(f"epoch {epoch:2d}/{epochs}  loss={total_loss / n:.4f}")
    return {"w1": w1, "b1": b1, "w2": w2, "b2": b2}


def predict(model: dict, x: np.ndarray, chunk: int = 4096) -> np.ndarray:
    out = np.empty(x.shape[0], dtype=np.int64)
    for start in range(0, x.shape[0], chunk):
        xb = x[start:start + chunk]
        h = np.maximum(xb @ model["w1"] + model["b1"], 0.0)
        out[start:start + chunk] = np.argmax(h @ model["w2"] + model["b2"], axis=1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--hidden", type=int, default=128)
    parser.add_argument("--batch", type=int, default=256)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--limit", type=int, default=0, help="use only N training samples")
    parser.add_argument("--out", default="", help="write the model here instead of the default")
    parser.add_argument("--aug", choices=["shift", "affine"], default="shift",
                        help="augmentation used for the second pass")
    parser.add_argument("--aug-copies", type=int, default=1,
                        help="how many augmented copies to add to the second pass")
    parser.add_argument("--aug-rotate", type=float, default=18.0,
                        help="max rotation in degrees for affine augmentation")
    parser.add_argument("--aug-photo", action="store_true",
                        help="vary gamma and blur (camera exposure / focus)")
    parser.add_argument("--aug-thick", action="store_true",
                        help="also vary stroke thickness (thin vs thick pen)")
    args = parser.parse_args()

    missing = [name for name, file in FILES.items() if not (CACHE / file).exists()]
    if missing:
        print(f"missing MNIST files in {CACHE}: {missing}")
        return 2

    x_train = load_idx(CACHE / FILES["train_images"]).astype(np.float32) / 255.0
    y_train = load_idx(CACHE / FILES["train_labels"])
    x_test = load_idx(CACHE / FILES["test_images"]).astype(np.float32) / 255.0
    y_test = load_idx(CACHE / FILES["test_labels"])
    if args.limit:
        x_train, y_train = x_train[: args.limit], y_train[: args.limit]
    print(f"train {x_train.shape}  test {x_test.shape}")

    rng = np.random.default_rng(0)
    started = time.time()
    model = train(x_train, y_train, args.hidden, args.epochs, args.batch, args.lr)
    train_s = time.time() - started

    # second pass with shifted copies: the camera rarely centres a glyph perfectly
    started = time.time()
    if args.aug == "affine":
        extra = [augment_affine(x_train, rng, max_rotate=args.aug_rotate,
                               thickness=args.aug_thick, photometric=args.aug_photo)
                 for _ in range(max(1, args.aug_copies))]
    else:
        extra = [augment(x_train, rng) for _ in range(max(1, args.aug_copies))]
    model = train(np.vstack([x_train] + extra),
                  np.concatenate([y_train] * (1 + len(extra))),
                  args.hidden, max(3, args.epochs // 2), args.batch, args.lr * 0.5)
    aug_s = time.time() - started

    pred = predict(model, x_test)
    accuracy = float((pred == y_test).mean())
    confusion = np.zeros((10, 10), dtype=np.int64)
    for truth, guess in zip(y_test, pred):
        confusion[truth, guess] += 1

    out_path = Path(args.out) if args.out else MODEL
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_path, **model)
    print(f"test accuracy {accuracy * 100:.2f}%  (train {train_s:.0f}s + aug {aug_s:.0f}s)")
    print("confusion (rows=true, cols=pred):")
    for digit in range(10):
        row = " ".join(f"{v:5d}" for v in confusion[digit])
        print(f"  {digit}: {row}")

    report = [
        "# Handwritten digit model",
        "",
        "Trained from MNIST with a two-layer numpy network (no framework).",
        "",
        f"- architecture: 784 -> {args.hidden} ReLU -> 10 softmax",
        f"- training: {len(y_train)} samples + {args.aug} augmentation, Adam lr={args.lr}",
        f"- test accuracy: **{accuracy * 100:.2f}%** on {len(y_test)} MNIST test images",
        f"- weights: `{out_path.relative_to(ROOT)}` ({out_path.stat().st_size // 1024} KiB)",
        "",
        "## Confusion matrix",
        "",
        "| true \\ pred | " + " | ".join(str(d) for d in range(10)) + " |",
        "|---" * 11 + "|",
    ]
    for digit in range(10):
        report.append(f"| {digit} | " + " | ".join(str(v) for v in confusion[digit]) + " |")
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"wrote {out_path.relative_to(ROOT)} and {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
