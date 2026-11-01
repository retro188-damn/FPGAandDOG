"""Score a captured frame so a burst can be trimmed to its usable shots.

Why this exists (measured 2026-10-27/28):

* A blurry card is *not* a card that cannot be found.  Blurring a known-good
  frame frees the detector all the way to radius ~11 px, while the classifier
  already turns a `3` into a `2` at radius 4 - with confidence 0.985.  A
  confidently wrong digit is worse than no answer, so the reading has to be
  gated on sharpness *before* it is spoken.
* Sharpness separates the two cases by three orders of magnitude on the same
  frame: the card region measures ~1300-1800 (variance of the Laplacian) when
  sharp and ~2 once blurred to radius 4.  A threshold anywhere in between is
  therefore safe; `MIN_SHARPNESS` is set low on purpose so a slightly soft but
  readable real frame still passes.
* The card also has to be fully inside the frame: at ~0.7 m the paper is pushed
  against the bottom edge (y1 == height) and the digit is cut in half - the
  frame then reads `empty` even though `find_card` happily returns a box.

``measure`` is pure (image in, numbers out) and ``pick_best`` is a tiny ranking
helper, so both are testable without a camera or a dog.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from host.app.vision.digit_reader import find_card, to_gray

#: Below this card width the glyph has too few pixels to be read (measured:
#: 86-111 px reads unreliably, 124-143 px is stable at 0.97+).
MIN_WIDTH_PX = 110
#: Sharp cards measure 1300-1800, blur radius >= 4 measures ~2 (2026-10-28).
MIN_SHARPNESS = 120.0
#: A card touching the bottom edge is cut off: the digit is partially outside.
EDGE_MARGIN_PX = 6
#: Same idea as `find_card`'s own contrast gate, kept here so a caller can see
#: why a frame was rejected without re-running the detector.
MIN_CONTRAST = 25.0


@dataclass
class FrameQuality:
    """What one frame is worth, and why."""

    has_card: bool = False
    width_px: int = 0
    sharpness: float = 0.0
    contrast: float = 0.0
    edge_cut: bool = False
    ok: bool = False
    score: float = -1.0
    box: Optional[Tuple[int, int, int, int]] = None
    reason: str = "no card"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "width_px": self.width_px,
            "sharpness": round(self.sharpness, 1),
            "contrast": round(self.contrast, 1),
            "edge_cut": self.edge_cut,
            "reason": self.reason,
        }


def _laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian on a small patch."""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    patch = gray.astype(np.float64)
    centre = patch[1:-1, 1:-1]
    return float((4.0 * centre - patch[:-2, 1:-1] - patch[2:, 1:-1]
                  - patch[1:-1, :-2] - patch[1:-1, 2:]).var())


def measure(image,
            min_width_px: int = MIN_WIDTH_PX,
            min_sharpness: float = MIN_SHARPNESS,
            min_contrast: float = MIN_CONTRAST,
            edge_margin_px: int = EDGE_MARGIN_PX) -> FrameQuality:
    """Score one frame: can this card be trusted to contain the digit?"""
    if image is None:
        return FrameQuality(reason="no frame")
    gray = to_gray(image)
    box = find_card(gray)
    if box is None:
        return FrameQuality(reason="no card")

    from host.app.vision.digit_reader import _ring_stats  # local: keeps imports light

    x0, y0, x1, y1 = box
    width = int(x1 - x0)
    height = int(y1 - y0)
    if width <= 1 or height <= 1:
        return FrameQuality(reason="degenerate box", box=box)

    roi = gray[y0:y1, x0:x1]
    sharpness = _laplacian_variance(roi)
    inside = float(np.median(roi))
    contrast = inside - _ring_stats(gray, box)[0]
    edge_cut = bool(y1 >= gray.shape[0] - edge_margin_px)

    quality = FrameQuality(has_card=True, width_px=width, sharpness=sharpness,
                           contrast=contrast, edge_cut=edge_cut, box=box)

    if width < min_width_px:
        quality.reason = "too small (%d px)" % width
        return quality
    if edge_cut:
        quality.reason = "card cut by the bottom edge"
        return quality
    if contrast < min_contrast:
        quality.reason = "low contrast (%.0f)" % contrast
        return quality
    if sharpness < min_sharpness:
        quality.reason = "blurred (sharpness %.1f)" % sharpness
        return quality

    quality.ok = True
    quality.reason = "ok"
    # Rank usable frames: sharper first, then wider (closer to the card is
    # more pixels per stroke), both capped so one huge outlier cannot dominate.
    quality.score = min(sharpness / 1000.0, 2.0) * min(width / 140.0, 1.5)
    return quality


def measure_all(frames: Sequence[object], **kwargs) -> List[FrameQuality]:
    return [measure(frame, **kwargs) for frame in frames]


def pick_best(frames: Sequence[object], top_k: int = 3, **kwargs):
    """Return ``(indices, qualities)`` for the best ``top_k`` usable frames.

    When no frame passes the gate the indices are empty on purpose: the caller
    should re-shoot instead of voting over damaged frames (three blurred frames
    agree with each other and produce a confident wrong digit).
    """
    qualities = measure_all(frames, **kwargs)
    usable = [i for i, q in enumerate(qualities) if q.ok]
    usable.sort(key=lambda i: qualities[i].score, reverse=True)
    return usable[:max(1, int(top_k))], qualities
