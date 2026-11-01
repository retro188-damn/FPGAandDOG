"""Train the digit classifier on MNIST *plus* rendered handwriting.

The v1 model is MNIST-only, so a marker glyph photographed at a shallow angle
arrives squashed to about 2:1 with a hairline stroke - a shape MNIST never
contains.  v2 mixes in tools/render_digits.py output (many fonts, squashed,
thin) and trains the same 784-hidden-10 numpy MLP, so inference stays pure
numpy/PIL with no new dependency.

    python tools/train_digits_v2.py --synth .cache/synth_digits.npz --out host/model/digits_mlp_v2.npz
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.train_digits import (  # noqa: E402
    CACHE, FILES, augment_affine, load_idx, predict, train,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--synth", default=".cache/synth_digits.npz")
    ap.add_argument("--synth-weight", type=float, default=0.6,
                    help="synthetic samples as a fraction of the MNIST set")
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--out", default="host/model/digits_mlp_v2.npz")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    x_train = load_idx(CACHE / FILES["train_images"]).astype(np.float32) / 255.0
    y_train = load_idx(CACHE / FILES["train_labels"])
    x_test = load_idx(CACHE / FILES["test_images"]).astype(np.float32) / 255.0
    y_test = load_idx(CACHE / FILES["test_labels"])

    synth = ROOT / args.synth
    xs = ys = None
    if synth.exists():
        data = np.load(synth)
        xs, ys = data["x"].astype(np.float32), data["y"].astype(np.int64)
        order = rng.permutation(len(ys))
        xs, ys = xs[order], ys[order]
        hold = max(1, int(0.15 * len(ys)))
        xs_eval, ys_eval = xs[:hold], ys[:hold]
        xs, ys = xs[hold:], ys[hold:]
        take = int(len(y_train) * args.synth_weight)
        if take < len(ys):
            idx = rng.choice(len(ys), size=take, replace=False)
            xs, ys = xs[idx], ys[idx]
        print(f"synth: {xs.shape[0]} train / {xs_eval.shape[0]} eval, fonts mixed in")
    else:
        print(f"no synthetic set at {synth}; training MNIST-only")
        xs_eval = ys_eval = None

    print(f"mixing {x_train.shape[0]} MNIST + {0 if xs is None else xs.shape[0]} synthetic")
    started = time.time()
    parts_x = [x_train]
    parts_y = [y_train]
    if xs is not None:
        parts_x.append(xs)
        parts_y.append(ys)
    x_all = np.vstack(parts_x)
    y_all = np.concatenate(parts_y)
    model = train(x_all, y_all, args.hidden, max(6, args.epochs), args.batch, args.lr)
    print(f"first pass {time.time() - started:.0f}s")

    # second pass: MNIST style-shift only (the synthetic set is already degraded)
    extra = augment_affine(x_train, rng, max_rotate=25.0, max_scale=0.18,
                           max_shear=0.22, max_shift=2.5, thickness=True,
                           photometric=True, max_squash=0.55)
    x_aug = np.vstack([x_all, extra])
    y_aug = np.concatenate([y_all, y_train])
    model = train(x_aug, y_aug, args.hidden, max(4, args.epochs // 2), args.batch,
                  args.lr * 0.5)
    print(f"total {time.time() - started:.0f}s")

    for name, (xv, yv) in {
        "mnist-test": (x_test, y_test),
        **({"synth-heldout": (xs_eval, ys_eval)} if xs_eval is not None else {}),
    }.items():
        pred = predict(model, xv)
        acc = float((pred == yv).mean())
        print(f"{name}: {acc * 100:.2f}%  ({len(yv)} samples)")
        if name == "synth-heldout":
            conf = np.zeros((10, 10), dtype=np.int64)
            for t, g in zip(yv, pred):
                conf[t, g] += 1
            for d in range(10):
                row = " ".join(f"{v:5d}" for v in conf[d])
                print(f"  {d}: {row}")

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, **model)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
