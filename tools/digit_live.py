"""Read the digit card in front of the dog and show what the model saw.

Grabs frames from the perception board, runs the digit chain, draws the detected
glyph boxes on the frame and writes both the raw frame and the annotated image
under ``metrics/evidence/`` so every read is auditable later.

    python tools/digit_live.py --shots 3
    python tools/digit_live.py --save-card      # also keep the card crop
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "host" / "app") not in sys.path:
    sys.path.insert(0, str(ROOT / "host" / "app"))

from PIL import ImageDraw  # noqa: E402

from vision.camera_client import CAMERA_HOST, CAMERA_PORT, grab_frame_raw  # noqa: E402
from vision.digit_reader import find_card, load_classifier, read_digits, to_gray  # noqa: E402

EVIDENCE = ROOT / "metrics" / "evidence"


def annotate(image, details, text, confidence):
    """Return a copy of the frame with glyph boxes and the verdict drawn on."""
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    for index, (digit, score, (x0, y0, x1, y1)) in enumerate(details):
        draw.rectangle((x0 - 4, y0 - 4, x1 + 4, y1 + 4), outline=(0, 220, 0), width=2)
        draw.text((x0, max(0, y0 - 16)), f"{digit}:{score:.2f}", fill=(0, 220, 0))
    draw.rectangle((4, 4, 240, 34), fill=(0, 0, 0))
    draw.text((10, 10), f"read={text!r} conf={confidence:.2f}", fill=(255, 255, 0))
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=CAMERA_HOST)
    parser.add_argument("--port", type=int, default=CAMERA_PORT)
    parser.add_argument("--shots", type=int, default=3, help="frames to average over")
    parser.add_argument("--min-confidence", type=float, default=0.60)
    parser.add_argument("--model", default=str(ROOT / "host" / "model" / "digits_mlp.npz"))
    parser.add_argument("--tag", default="digit")
    parser.add_argument("--save-card", action="store_true", help="keep the card crop too")
    args = parser.parse_args()

    classifier = load_classifier(Path(args.model), ROOT / "host" / "model" / "digits_library.npz")
    if classifier is None:
        print(f"no model at {args.model}: train it with tools/train_digits.py")
        return 2

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    votes: Counter = Counter()
    best = None
    last_card = None
    started = time.time()
    for shot in range(args.shots):
        image, transfer_ms = grab_frame_raw(args.host, args.port)
        # A card at the frame's bottom edge is detected in most shots but not
        # all; carrying the last good box keeps a burst self-consistent.
        card = find_card(to_gray(image))
        if card is not None:
            last_card = card
        text, details = read_digits(image, classifier, card_hint=last_card)
        confidence = min((d[1] for d in details), default=0.0)
        print(f"shot {shot + 1}: read={text!r} glyphs={len(details)} "
              f"conf={confidence:.2f} transfer={transfer_ms:.0f} ms")
        if text:
            votes[text] += 1
        if details and (best is None or confidence > best[2]):
            best = (text, details, confidence, image)
        if shot == 0:
            raw_path = EVIDENCE / f"{args.tag}_{stamp}_raw.jpg"
            image.save(raw_path)

    decision = votes.most_common(1)[0][0] if votes else ""
    decision_conf = (votes[decision] / max(1, args.shots)) if decision else 0.0
    print(f"decision={decision!r} agreement={decision_conf:.2f} over {args.shots} shots "
          f"({time.time() - started:.1f}s)")

    if best is not None:
        text, details, confidence, image = best
        annotated = annotate(image, details, text, confidence)
        out = EVIDENCE / f"{args.tag}_{stamp}.png"
        annotated.save(out)
        record = {
            "stamp": stamp,
            "decision": decision,
            "agreement": decision_conf,
            "shots": args.shots,
            "min_confidence": args.min_confidence,
            "glyphs": [{"digit": d, "confidence": c, "box": list(b)} for d, c, b in details],
            "image": str(out.relative_to(ROOT)),
        }
        (EVIDENCE / f"{args.tag}_{stamp}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"evidence -> {out.relative_to(ROOT)}")
        if args.save_card:
            for index, (digit, _, (x0, y0, x1, y1)) in enumerate(details):
                pad = 20
                image.crop((max(0, x0 - pad), max(0, y0 - pad), x1 + pad, y1 + pad)).save(
                    EVIDENCE / f"{args.tag}_{stamp}_glyph{index}_{digit}.png"
                )

    if not decision:
        print("nothing detected: no card in view")
        return 1
    if decision_conf < args.min_confidence:
        print(f"low agreement ({decision_conf:.2f}) - report as unclear")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
