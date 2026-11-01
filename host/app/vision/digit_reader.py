"""Digit-reading chain: capture side + recognition side.

Design notes
------------
* Capture runs on the perception board (has the RealSense + cv2 4.1.1).
* Recognition runs on the PC (has numpy + Pillow, no cv2) so the demo does not
  add load to the boards right before a run.
* Handwritten digits: a printed-font template library cannot match freehand
  strokes, so the classifier is a small nearest-neighbour library built from
  samples we capture from the actual writer(s).  Samples are normalised to a
  28x28 ink mask, which makes the match robust to size and position.
* No new dependency is required on either side.

Pipeline
--------
frame -> gray -> ink mask (Otsu on the card region) -> glyph components
      -> normalise each glyph (bbox -> 28x28) -> nearest neighbour -> digits

This module contains only the recognition half; the capture server lives in
``capture_server.py`` and is deployed to the perception board.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

try:  # scipy is optional: only the labelling fast path needs it
    from scipy import ndimage as _ndimage
except Exception:  # pragma: no cover - exercised only where scipy is missing
    _ndimage = None

GLYPH_SIZE = 28

# A digit is never a flat band: the paper rim's shadow is.  Anything thinner than
# this ratio (height / width) is scenery, not ink.
# A card lying on the floor under a camera 0.31 m high reaches us through a
# ~12 degree grazing angle, so a square digit is foreshortened to roughly 2:1
# (measured 2026-10-29: a real "8" at 1.15 m arrived 67x31 px, aspect 0.46).
# The old floor of 0.5 threw those away before the classifier ever saw them.
MIN_GLYPH_ASPECT = 0.30
MAX_GLYPH_ASPECT = 4.0

# 4-neighbour dilation passes applied to each glyph before normalising.
# Thin ink (a ballpoint line, fill under ~0.16) needs the maximum; a thick
# marker or MNIST-style digit needs none, and over-thickening it is what makes
# 3/8 and 2/7 ambiguous again (measured: see runbook_digit.md section 11).
MAX_STROKE_DILATE = 3
DIGITS = "0123456789"


# ---------------------------------------------------------------- image basics
def to_gray(image: Image.Image) -> np.ndarray:
    """Return a float32 grayscale array in 0..255."""
    return np.asarray(image.convert("L"), dtype=np.float32)


def otsu_threshold(gray: np.ndarray) -> int:
    """Classic Otsu threshold over an 8-bit histogram."""
    hist = np.bincount(gray.astype(np.uint8).ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 128
    levels = np.arange(256, dtype=np.float64)
    weight_bg = np.cumsum(hist)
    weight_fg = total - weight_bg
    sum_bg = np.cumsum(hist * levels)
    mean_bg = sum_bg / np.maximum(weight_bg, 1)
    mean_fg = (sum_bg[-1] - sum_bg) / np.maximum(weight_fg, 1)
    between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    best = float(between.max())
    if best <= 0.0:
        return int(np.median(gray))
    # A clean two-level image has a whole plateau of equally good thresholds;
    # take the middle of it instead of the left edge, otherwise the dark class
    # can end up exactly on the threshold.
    plateau = np.flatnonzero(between >= best * (1.0 - 1e-9))
    return int(round(float(plateau.mean())))


def ink_mask(gray: np.ndarray, min_contrast: float = 18.0) -> np.ndarray:
    """Return a boolean mask of dark ink on a light card.

    A hand-written digit is darker than the paper; a contrast floor rejects
    frames where nothing but the floor is visible.
    """
    threshold = otsu_threshold(gray)
    mask = gray < threshold
    if float(gray[mask].mean() if mask.any() else 0.0) > float(gray[~mask].mean()) - min_contrast:
        return np.zeros_like(mask)
    return mask


# ------------------------------------------------------------- glyph extraction
@dataclass
class Glyph:
    """One ink blob plus its normalised 28x28 crop."""

    box: Tuple[int, int, int, int]  # x0, y0, x1, y1 (exclusive)
    pixels: np.ndarray              # (GLYPH_SIZE, GLYPH_SIZE) float32 in 0..1


def _components(mask: np.ndarray) -> List[Tuple[int, int, int, int, int]]:
    """Label 8-connected components; returns (x0, y0, x1, y1, area).

    Two implementations with identical semantics: ``scipy.ndimage.label`` when
    scipy is importable (C speed: ~2 ms instead of ~150 ms on a 640x480 frame,
    which is what makes a three-shot read fast enough to feel instant), and a
    pure-Python union-find fallback so the module still works on a machine
    without scipy.
    """
    if _ndimage is not None:
        return _components_scipy(mask)
    return _components_python(mask)


def _components_scipy(mask: np.ndarray) -> List[Tuple[int, int, int, int, int]]:
    """8-connected labelling via scipy.ndimage (fast path)."""
    labels, count = _ndimage.label(mask, structure=np.ones((3, 3), dtype=np.int8))
    if count == 0:
        return []
    areas = np.bincount(labels.ravel(), minlength=count + 1)[1:]
    out: List[Tuple[int, int, int, int, int]] = []
    for index, box in enumerate(_ndimage.find_objects(labels), start=1):
        if box is None:
            continue
        # boxes are half-open (x1/y1 exclusive) like the union-find fallback
        # and like the callers' slicing: gray[y0:y1, x0:x1]
        y0, y1 = int(box[0].start), int(box[0].stop)
        x0, x1 = int(box[1].start), int(box[1].stop)
        out.append((x0, y0, x1, y1, int(areas[index - 1])))
    return out


def _components_python(mask: np.ndarray) -> List[Tuple[int, int, int, int, int]]:
    """Scanline union-find fallback (no scipy needed)."""
    height, width = mask.shape
    parent: dict = {}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    labels = np.zeros((height, width), dtype=np.int32)
    next_label = 1
    for y in range(height):
        row = mask[y]
        x = 0
        while x < width:
            if not row[x]:
                x += 1
                continue
            x0 = x
            while x < width and row[x]:
                x += 1
            label = next_label
            next_label += 1
            parent[label] = label
            labels[y, x0:x] = label
            if y > 0:
                for xx in range(max(0, x0 - 1), min(width, x + 1)):
                    above = labels[y - 1, xx]
                    if above:
                        union(label, above)
    if not parent:
        return []

    # Flatten the union-find result into consecutive labels, then take the
    # per-label bounding boxes with vectorised ufunc.at calls: O(pixels), not
    # O(labels * pixels), which matters on a noisy floor frame.
    roots = np.array([0] + [find(label) for label in range(1, next_label)], dtype=np.int32)
    flat = roots[labels].ravel()
    count = int(flat.max()) + 1 if flat.size else 0
    if count == 0:
        return []
    areas = np.bincount(flat, minlength=count)
    ys, xs = np.divmod(np.arange(flat.size), width)
    xmin = np.full(count, width, dtype=np.int32)
    xmax = np.full(count, -1, dtype=np.int32)
    ymin = np.full(count, height, dtype=np.int32)
    ymax = np.full(count, -1, dtype=np.int32)
    np.minimum.at(xmin, flat, xs.astype(np.int32))
    np.maximum.at(xmax, flat, xs.astype(np.int32))
    np.minimum.at(ymin, flat, ys.astype(np.int32))
    np.maximum.at(ymax, flat, ys.astype(np.int32))
    boxes = []
    for label in range(1, count):
        if areas[label] == 0:
            continue
        boxes.append((int(xmin[label]), int(ymin[label]),
                      int(xmax[label]) + 1, int(ymax[label]) + 1, int(areas[label])))
    return boxes


def normalise(patch: np.ndarray) -> np.ndarray:
    """Resample an ink patch the way MNIST was built: fit into 20x20, centre on
    the centre of mass inside a 28x28 frame.

    Matching MNIST's geometry matters more than any classifier tweak: a model
    trained on MNIST sees every glyph under exactly these conventions.
    """
    ys, xs = np.nonzero(patch)
    if xs.size == 0:
        return np.zeros((GLYPH_SIZE, GLYPH_SIZE), dtype=np.float32)
    crop = patch[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.float32)
    h, w = crop.shape
    scale = (GLYPH_SIZE - 8) / max(h, w)  # 20 px inside the 28 px frame
    new_h = max(1, int(round(h * scale)))
    new_w = max(1, int(round(w * scale)))
    small = np.asarray(
        Image.fromarray((crop * 255).astype(np.uint8)).resize((new_w, new_h), Image.BILINEAR),
        dtype=np.float32,
    ) / 255.0
    total = float(small.sum())
    if total <= 1e-6:
        canvas = np.zeros((GLYPH_SIZE, GLYPH_SIZE), dtype=np.float32)
        canvas[4:4 + new_h, 4:4 + new_w] = small
        return canvas
    gy, gx = np.mgrid[0:new_h, 0:new_w]
    cy = float((gy * small).sum() / total)
    cx = float((gx * small).sum() / total)
    top = int(round(GLYPH_SIZE / 2 - cy))
    left = int(round(GLYPH_SIZE / 2 - cx))
    top = max(0, min(GLYPH_SIZE - new_h, top))
    left = max(0, min(GLYPH_SIZE - new_w, left))
    canvas = np.zeros((GLYPH_SIZE, GLYPH_SIZE), dtype=np.float32)
    canvas[top:top + new_h, left:left + new_w] = small
    return canvas


def box_blur(array: np.ndarray, radius: int) -> np.ndarray:
    """Mean filter via an integral image (O(pixels), no scipy)."""
    padded = np.pad(array.astype(np.float64), radius, mode="edge")
    integral = padded.cumsum(0).cumsum(1)
    integral = np.pad(integral, ((1, 0), (1, 0)))
    height, width = array.shape
    k = 2 * radius + 1
    total = (integral[k:k + height, k:k + width]
             - integral[:height, k:k + width]
             - integral[k:k + height, :width]
             + integral[:height, :width])
    return total / (k * k)


def find_card(gray: np.ndarray, radius: int = 25, margin: float = 12.0,
              min_brightness: float = 160.0,
              min_contrast: float = 25.0) -> Optional[Tuple[int, int, int, int]]:
    """Locate the paper card by local contrast, not by a global threshold.

    A global Otsu threshold splits the *floor's own* light/dark variation when the
    card only covers a few percent of the frame; comparing each pixel with its
    neighbourhood instead finds the paper regardless of exposure.  Returns None
    when nothing stands out from its surroundings.
    """
    background = box_blur(gray, radius)
    lifted = (gray.astype(np.float64) - background) > margin
    if not lifted.any():
        return None
    # Local contrast marks the card's *rim*, not its flat interior, so the rim is
    # closed into one shape before looking for the largest component.
    closed = box_blur(lifted.astype(np.float64), 4) > 0.30
    if not closed.any():
        return None
    # A bright card only stands out when the scene around it is darker.  On a
    # frame that is uniformly bright (paper filling the view) every "region" is
    # just a halo around the ink, and treating it as a card destroys the ink.
    median = float(np.median(gray))
    dark_fraction = float((gray < median - 40.0).mean())
    # Only a frame that is *bright overall* is the "paper fills the view" case;
    # a mid-grey scene (brown board, median ~130) has few very dark pixels but
    # still contains a perfectly good card.  Measured 2026-10-28 on the bowed
    # close-up: dark_fraction 0.011 rejected a card at +54 contrast.
    if median > 185.0 and dark_fraction < 0.03:
        return None
    height, width = gray.shape
    total = closed.size
    best = None
    for x0, y0, x1, y1, area in _components(closed):
        w, h = x1 - x0, y1 - y0
        if area < 0.01 * total or area > 0.60 * total:
            continue
        # The card usually runs out of the bottom of the frame when the dog is
        # close to it, so a bottom edge is allowed; other edges are not.
        if x0 <= 1 or y0 <= 1 or x1 >= width - 1:
            continue
        if not (0.3 <= (w / max(h, 1)) <= 3.3):
            continue
        # The card is bright paper: reject a large dark blob (a chair, a person)
        # that happens to stand out from its surroundings just as much.
        inside = float(np.median(gray[y0:y1, x0:x1]))
        if inside < min_brightness:
            continue
        # ... and it has to lie *on* something darker.  Without this, a bright
        # patch of floor outlined by tile seams reads as a card: measured on
        # 2026-10-27, a real card on the brown board sits +86 grey levels above
        # its ring while that floor patch managed +12, which was enough to make
        # the seek lock onto bare floor and then fail to read a digit.
        if inside - _ring_stats(gray, (x0, y0, x1, y1))[0] < min_contrast:
            continue
        if best is None or area > best[4]:
            best = (x0, y0, x1, y1, area)
    return None if best is None else (best[0], best[1], best[2], best[3])


def adaptive_ink_mask(gray: np.ndarray, radius: int = 20, margin: float = 6.0) -> np.ndarray:
    """Ink as "darker than my neighbourhood", which survives shadows and a light pen.

    A global Otsu threshold inside the card is decided by whatever dominates the
    histogram - usually the paper's own shadowed edge - so a thin, light ballpoint
    stroke never becomes "ink".  Comparing each pixel with a local mean cancels
    that gradient; a 6-level dip is enough even for a faint red pen.
    """
    background = box_blur(gray, radius)
    return (background - gray.astype(np.float64)) > margin


def _dilate(mask: np.ndarray, times: int = 1) -> np.ndarray:
    """Thicken thin strokes to MNIST-like weight.

    A ballpoint line on paper is 1-2 px wide at this resolution, while MNIST's
    digits were drawn thick.  Normalising a hairline stroke to 20 px keeps it a
    hairline, and the classifier hesitates between shapes that differ only by
    stroke weight (3 vs 8).  One or two 4-neighbour passes fix that: measured
    confidence on real cards rose from 0.64-0.67 to 0.87-0.92 with one pass.
    """
    out = mask.astype(bool)
    for _ in range(max(0, times)):
        padded = np.pad(out, 1)
        out = (padded[:-2, 1:-1] | padded[2:, 1:-1] | padded[1:-1, :-2]
               | padded[1:-1, 2:] | padded[1:-1, 1:-1])
    return out


def _stroke_passes(patch: np.ndarray) -> int:
    """How many thinning-correcting dilations this glyph needs (0..3).

    Fill is ink pixels over bounding box: a hairline digit fills under 0.16 of
    its box, a thick marker or an MNIST glyph fills much more.
    """
    fill = float(patch.mean()) if patch.size else 0.0
    if fill < 0.20:
        return MAX_STROKE_DILATE
    if fill < 0.26:
        return 2
    if fill < 0.34:
        return 1
    return 0


def _glyphs_in_mask(mask: np.ndarray, offset: Tuple[int, int], min_area_frac: float,
                    max_area_frac: float, max_glyphs: int,
                    allow_sides: Tuple[bool, bool, bool, bool] = (False, False, False, False),
                    card: Optional[Tuple[int, int, int, int]] = None,
                    min_size_share: float = 0.25,
                    min_aspect: float = 0.5
                    ) -> List[Glyph]:
    if not mask.any():
        return []
    ox, oy = offset
    height, width = mask.shape
    total = mask.size
    left_ok, top_ok, right_ok, bottom_ok = allow_sides
    boxes: List[Tuple[int, int, int, int]] = []
    for x0, y0, x1, y1, area in _components(mask):
        w, h = x1 - x0, y1 - y0
        if area < min_area_frac * total or area > max_area_frac * total:
            continue
        # A stroke clipped by the *frame* is still readable; one clipped by the
        # card's own edge is usually the paper rim's shadow, not ink.
        if ((x0 <= 1 and not left_ok) or (y0 <= 1 and not top_ok)
                or (x1 >= width - 1 and not right_ok) or (y1 >= height - 1 and not bottom_ok)):
            continue
        # A glyph cut by the crop is fine; a component as large as the crop is
        # the paper's own rim, not ink.
        if w >= 0.8 * width and h >= 0.8 * height:
            continue
        if not (min_aspect <= h / max(w, 1) <= MAX_GLYPH_ASPECT):
            continue
        boxes.append((x0, y0, x1, y1))
    if card is not None:
        # Drop the paper rim / off-card clutter *before* merging: the rim wraps
        # the whole card, so a naive merge would glue it to the digit and hand
        # the classifier one giant blob (read as "0", measured 2026-10-28).
        local = (card[0] - ox, card[1] - oy, card[2] - ox, card[3] - oy)
        card_w = max(1, local[2] - local[0])
        card_h = max(1, local[3] - local[1])
        kept: List[Tuple[int, int, int, int]] = []
        for b in boxes:
            # A blob almost as big as the card is the paper's own rim (a closed
            # ring that the classifier happily calls "0"); a digit never fills
            # the card edge to edge.
            if (b[2] - b[0]) >= 0.9 * card_w and (b[3] - b[1]) >= 0.9 * card_h:
                continue
            if not _mostly_on_card(b, local) or not _big_enough_on_card(b, local, min_size_share):
                continue
            kept.append(b)
        boxes = kept
    boxes = _merge_stacked(boxes)
    glyphs: List[Glyph] = []
    for x0, y0, x1, y1 in boxes:
        patch = mask[y0:y1, x0:x1]
        patch = _dilate(patch, _stroke_passes(patch))
        glyphs.append(Glyph((x0 + ox, y0 + oy, x1 + ox, y1 + oy), normalise(patch)))
    glyphs.sort(key=lambda g: g.box[0])
    return glyphs[:max_glyphs]


def _merge_stacked(boxes: List[Tuple[int, int, int, int]],
                   overlap_share: float = 0.5,
                   max_gap_share: float = 0.30,
                   min_width_share: float = 0.5) -> List[Tuple[int, int, int, int]]:
    """Join blobs that are one digit split into a top and a bottom half.

    A hand-drawn ``8`` is often two loops with a visible gap, and an ``8`` whose
    loops do not touch arrives as *two* components -- read left to right that
    becomes "03" with a low per-glyph confidence (measured on the 2026-10-28
    card: 10/10 reads returned "03" for a real 8).

    Only vertically stacked pairs of *similar width* are joined: two digits
    written side by side share no horizontal overlap, and the paper rim (which
    is far wider than a stroke piece) is refused by ``min_width_share``.
    """
    merged = list(boxes)
    changed = True
    while changed and len(merged) > 1:
        changed = False
        best = None  # (gap, i, j)
        for i in range(len(merged)):
            ax0, ay0, ax1, ay1 = merged[i]
            for j in range(i + 1, len(merged)):
                bx0, by0, bx1, by1 = merged[j]
                aw, bw = max(1, ax1 - ax0), max(1, bx1 - bx0)
                if min(aw, bw) < min_width_share * max(aw, bw):
                    continue
                overlap = min(ax1, bx1) - max(ax0, bx0)
                if overlap < overlap_share * min(aw, bw):
                    continue
                gap = max(by0 - ay1, ay0 - by1)
                if gap > max_gap_share * max(ay1 - ay0, by1 - by0):
                    continue
                if best is None or gap < best[0]:
                    best = (gap, i, j)
        if best is not None:
            _, i, j = best
            ax0, ay0, ax1, ay1 = merged[i]
            bx0, by0, bx1, by1 = merged[j]
            merged[i] = (min(ax0, bx0), min(ay0, by0), max(ax1, bx1), max(ay1, by1))
            del merged[j]
            changed = True
    return merged


def _mostly_on_card(box: Tuple[int, int, int, int], card: Tuple[int, int, int, int],
                    share: float = 0.5) -> bool:
    """True when at least ``share`` of the blob's box lies on the detected card.

    The crop is deliberately grown past the card so ink near the rim is not cut,
    which also lets floor scenery (a tile seam, the chair's shadow) into the crop.
    Those sit *off* the card; a real digit always sits *on* it.
    """
    x0, y0, x1, y1 = box
    cx0 = max(x0, card[0])
    cy0 = max(y0, card[1])
    cx1 = min(x1, card[2])
    cy1 = min(y1, card[3])
    overlap = max(0, cx1 - cx0) * max(0, cy1 - cy0)
    area = max(1, (x1 - x0) * (y1 - y0))
    return overlap >= share * area


def _ring_stats(gray: np.ndarray, box: Tuple[int, int, int, int],
                pad: int = 12) -> Tuple[float, float, float]:
    """Median, std and 10-90 spread of the pixels just outside ``box``.

    A digit sits on paper: the ring around it is uniform (std under ~20).
    Clutter - a sleeve, a trouser edge, a chair leg next to a bottle - has a
    hard edge in the same ring (std over ~40, spread over ~100).
    """
    x0, y0, x1, y1 = box
    height, width = gray.shape
    X0, Y0 = max(0, x0 - pad), max(0, y0 - pad)
    X1, Y1 = min(width, x1 + pad), min(height, y1 + pad)
    outer = gray[Y0:Y1, X0:X1]
    keep = np.ones(outer.shape, dtype=bool)
    keep[y0 - Y0:y1 - Y0, x0 - X0:x1 - X0] = False
    ring = outer[keep]
    if ring.size == 0:
        return 0.0, 0.0, 0.0
    return (float(np.median(ring)), float(np.std(ring)),
            float(np.percentile(ring, 90) - np.percentile(ring, 10)))


#: A ring noisier than this is clutter, not paper (calibrated on real frames in
#: runbook_digit.md section 12: cards 4-20 std / 12-40 spread, a sleeve 43/123).
MAX_RING_STD = 30.0
MAX_RING_SPREAD = 80.0


def _is_clutter(gray: np.ndarray, box: Tuple[int, int, int, int]) -> bool:
    _med, std, spread = _ring_stats(gray, box)
    return std > MAX_RING_STD and spread > MAX_RING_SPREAD


def _roi_glyph(gray: np.ndarray, x0f: float = 0.20, x1f: float = 0.85,
                y0f: float = 0.58, min_size_share: float = 0.25) -> List[Glyph]:
    """Last-resort search in the patch of floor the dog actually faces.

    When the card is lit like the floor, no rim stands out and ``find_card``
    correctly refuses - but the digit's strokes still are the only dark, digit
    shaped blob in the near-field view.  Only the *largest* such blob is kept:
    at reading distance the card's digit is bigger than a chair leg or a bottle
    label, and a digits-string made of several blobs would be wrong anyway.
    """
    height, width = gray.shape
    x0, x1 = int(x0f * width), int(x1f * width)
    y0 = int(y0f * height)
    region = gray[y0:height, x0:x1]
    if region.size < 400:
        return []
    for mask in (adaptive_ink_mask(region), ink_mask(region)):
        glyphs = _glyphs_in_mask(mask, (x0, y0), 0.004, 0.20, 8, (False, False, False, True))
        glyphs = [g for g in glyphs if (g.box[3] - g.box[1]) >= min_size_share * region.shape[0]]
        if glyphs:
            biggest = max(glyphs, key=lambda g: (g.box[2] - g.box[0]) * (g.box[3] - g.box[1]))
            _med, std, spread = _ring_stats(gray, biggest.box)
            if std > MAX_RING_STD and spread > MAX_RING_SPREAD:
                # A sleeve or a chair edge, not a digit on paper: better to say
                # nothing than to read a confident wrong number.
                return []
            return [biggest]
    return []


def extract_glyphs(gray: np.ndarray, min_area_frac: float = 0.004,
                   max_area_frac: float = 0.20, max_glyphs: int = 4,
                   card_hint: Optional[Tuple[int, int, int, int]] = None,
                   min_size_share: float = 0.25) -> List[Glyph]:
    """Find the digit-sized ink blobs, left to right.

    Two-stage on purpose: locate the card first, then threshold inside it, which
    is what keeps a floor-wide Otsu split from swallowing the ink.  When the card
    is found but contains no usable glyph (the detector sometimes locks onto the
    bright halo around a dark digit), fall back to the whole-frame mask instead
    of reporting "nothing there".

    ``card_hint`` is the card box from a previous frame.  A card half out of view
    at the frame's bottom edge makes the rim detector miss now and then; reusing
    the last good box keeps a burst of shots coherent.
    """
    card = find_card(gray)
    if card is None and card_hint is not None:
        card = card_hint
    if card is not None:
        # The rim-based detector hugs the card's *contrasting* edge, which can cut
        # the paper short by a third of its width when one side fades into the
        # floor.  Grow the box so the ink is inside it; the extra floor is then
        # rejected either by the border rule or by the aspect filter.
        height, width = gray.shape
        pad = int(0.30 * min(card[2] - card[0], card[3] - card[1]))
        padded = (max(0, card[0] - pad), max(0, card[1] - pad),
                  min(width, card[2] + pad), min(height, card[3] + pad))
        # Try the card's *interior* first.  The padded crop unavoidably brings the
        # paper rim along, and that rim is a closed ring the classifier is happy
        # to call "0" -- worse, a thin transition pixel can glue the digit's top
        # loop onto it, so an "8" arrives as rim+"3" (measured 2026-10-28:
        # 10/10 reads returned "03").  Insetting the card keeps the rim out.
        inset_x = max(2, int(0.06 * (card[2] - card[0])))
        inset_y = max(2, int(0.06 * (card[3] - card[1])))
        inner = (card[0] + inset_x, card[1] + inset_y,
                 card[2] - inset_x, card[3] - inset_y)
        for box in (inner, padded):
            region = gray[box[1]:box[3], box[0]:box[2]]
            if region.size < 400:
                continue
            # Only sides that are also the frame's edge may be touched: a card
            # half out of view is still readable, while the crop's own edges
            # carry the paper rim's shadow.
            sides = (box[0] <= 0, box[1] <= 0, box[2] >= width, box[3] >= height)
            # adaptive first (thin / light ink), global Otsu second (bold ink,
            # where the local mean is pulled down by the stroke itself)
            for mask in (adaptive_ink_mask(region), ink_mask(region)):
                # Inside a confirmed card a digit may be legitimately squashed by
                # the grazing view (aspect ~0.46 measured); without a card there is
                # no such guarantee and a wide blob is usually clutter.
                glyphs = _glyphs_in_mask(mask, (box[0], box[1]),
                                         min_area_frac, max_area_frac, max_glyphs, sides,
                                         card=card, min_size_share=min_size_share,
                                         min_aspect=MIN_GLYPH_ASPECT)
                glyphs = [g for g in glyphs
                          if _mostly_on_card(g.box, card)
                          and _big_enough_on_card(g.box, card, min_size_share)]
                if glyphs:
                    return glyphs
    roi = _roi_glyph(gray)
    if roi:
        return roi
    # Global Otsu is the last resort (no card, no near-field blob).  Clutter is
    # filtered here too, otherwise a rejected sleeve would just be found again.
    candidates = _glyphs_in_mask(ink_mask(gray), (0, 0), min_area_frac,
                                 max_area_frac, max_glyphs)
    return [g for g in candidates if not _is_clutter(gray, g.box)]


def _big_enough_on_card(box: Tuple[int, int, int, int], card: Tuple[int, int, int, int],
                        share: float) -> bool:
    """A digit written to be read occupies a real fraction of the card.

    The notebook's spiral, a page crease or a shadow notch is digit-shaped but
    small; requiring a quarter of the card's height drops those without touching
    a normally written digit.
    """
    card_h = max(1, card[3] - card[1])
    card_w = max(1, card[2] - card[0])
    return (box[3] - box[1]) >= share * card_h and (box[2] - box[0]) >= 0.10 * card_w


# ---------------------------------------------------------------- recogniser
class DigitLibrary:
    """Nearest-neighbour digit library built from captured samples."""

    def __init__(self) -> None:
        self.samples: List[np.ndarray] = []
        self.labels: List[str] = []

    def __len__(self) -> int:
        return len(self.samples)

    def add(self, glyph: np.ndarray, label: str) -> None:
        self.samples.append(np.asarray(glyph, dtype=np.float32))
        self.labels.append(label)

    def add_folder(self, root: Path) -> int:
        """Load ``root/<digit>/*.png`` samples produced by the capture tool."""
        added = 0
        for digit in DIGITS:
            folder = root / digit
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.png")):
                with Image.open(path) as image:
                    array = np.asarray(image.convert("L"), dtype=np.float32) / 255.0
                self.add(array, digit)
                added += 1
        return added

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            samples=np.stack(self.samples) if self.samples else np.zeros((0, GLYPH_SIZE, GLYPH_SIZE)),
            labels=np.array(self.labels),
        )

    @classmethod
    def load(cls, path: Path) -> "DigitLibrary":
        library = cls()
        with np.load(path) as data:
            for sample, label in zip(data["samples"], data["labels"]):
                library.add(sample, str(label))
        return library

    def classify(self, glyph: np.ndarray, k: int = 3) -> Tuple[str, float]:
        """Return (digit, confidence) via k-nearest neighbour voting."""
        if not self.samples:
            return "?", 0.0
        bank = np.stack(self.samples)
        diff = (bank - glyph[None, :, :]) ** 2
        distances = diff.reshape(len(bank), -1).mean(axis=1)
        order = np.argsort(distances)[: max(1, k)]
        votes: dict = {}
        for index in order:
            weight = 1.0 / (float(distances[index]) + 1e-4)
            votes[self.labels[index]] = votes.get(self.labels[index], 0.0) + weight
        best = max(votes.items(), key=lambda kv: kv[1])
        total = sum(votes.values()) or 1.0
        return best[0], best[1] / total


class MLPClassifier:
    """Two-layer network trained by ``tools/train_digits.py`` on MNIST."""

    def __init__(self, w1: np.ndarray, b1: np.ndarray, w2: np.ndarray, b2: np.ndarray) -> None:
        self.w1, self.b1, self.w2, self.b2 = w1, b1, w2, b2

    @classmethod
    def load(cls, path: Path) -> "MLPClassifier":
        with np.load(path) as data:
            return cls(data["w1"], data["b1"], data["w2"], data["b2"])

    def classify(self, glyph: np.ndarray) -> Tuple[str, float]:
        x = np.asarray(glyph, dtype=np.float32).reshape(1, -1)
        hidden = np.maximum(x @ self.w1 + self.b1, 0.0)
        logits = hidden @ self.w2 + self.b2
        logits = logits - logits.max(axis=1, keepdims=True)
        probs = np.exp(logits)
        probs /= probs.sum(axis=1, keepdims=True)
        best = int(np.argmax(probs, axis=1)[0])
        return str(best), float(probs[0, best])


class CascadeClassifier:
    """Consult the font-trained model only when the MNIST model is unsure.

    Averaging the two models (see EnsembleClassifier) fixes hand-drawn 8s but
    costs 7 of 52 cases on the camera-stress benchmark.  A cascade keeps the
    MNIST model in charge when it is confident - printed and rotated digits stay
    exactly as good as before - and falls back to the font model only for the
    shapes MNIST never saw: measured 2026-10-29, real frames 3/8 -> 7/8 while
    bench_stress stayed 45/52.
    """

    def __init__(self, primary, second, threshold: float = 0.85,
                 margin: float = 0.25, min_second: float = 0.70) -> None:
        self.primary = primary
        self.second = second
        self.threshold = float(threshold)
        self.margin = float(margin)
        self.min_second = float(min_second)

    def classify(self, glyph: np.ndarray) -> Tuple[str, float]:
        label, conf = self.primary.classify(glyph)
        if conf >= self.threshold:
            return label, conf
        other_label, other_conf = self.second.classify(glyph)
        # Take the second opinion only when it is both confident on its own and
        # clearly better than the primary - a bare comparison lets the font model
        # hijack cases the MNIST model merely found hard (measured: that cost
        # 8 of 52 on bench_stress).
        if other_conf >= self.min_second and other_conf >= conf + self.margin:
            return other_label, other_conf
        return label, conf


class EnsembleClassifier:
    """Average the class probabilities of several MLPs.

    One model trained on MNIST is the best we have on printed/rotated digits, but
    it reads a hand-drawn *squashed* 8 as 0/5/7.  A second model trained with
    rendered fonts (see tools/render_digits.py) fixes those and loses a little on
    rotated print.  Averaging their probabilities keeps both strengths: measured
    2026-10-29 on eight real frames, {v1: 3/8, v2: 6/8, ensemble: 7/8}.
    """

    def __init__(self, models: List["MLPClassifier"]) -> None:
        self.models = list(models)

    def _probs(self, model: "MLPClassifier", x: np.ndarray) -> np.ndarray:
        hidden = np.maximum(x @ model.w1 + model.b1, 0.0)
        logits = hidden @ model.w2 + model.b2
        logits = logits - logits.max(axis=1, keepdims=True)
        probs = np.exp(logits)
        return probs / probs.sum(axis=1, keepdims=True)

    def classify(self, glyph: np.ndarray) -> Tuple[str, float]:
        x = np.asarray(glyph, dtype=np.float32).reshape(1, -1)
        total = None
        for model in self.models:
            probs = self._probs(model, x)
            total = probs if total is None else total + probs
        total /= len(self.models)
        best = int(np.argmax(total, axis=1)[0])
        return str(best), float(total[0, best])


def load_classifier(model_path: Optional[Path] = None,
                    library_path: Optional[Path] = None):
    """Prefer the trained network, fall back to a sample library, else None."""
    if model_path is None:
        model_path = Path(__file__).resolve().parents[3] / "host" / "model" / "digits_mlp.npz"
    if library_path is None:
        library_path = Path(__file__).resolve().parents[3] / "host" / "model" / "digits_library.npz"
    if model_path.exists():
        primary = MLPClassifier.load(model_path)
        second = model_path.with_name(model_path.stem + "_v2.npz")
        if second.exists() and model_path.stem == "digits_mlp":
            return CascadeClassifier(primary, MLPClassifier.load(second))
        return primary
    if library_path.exists():
        library = DigitLibrary.load(library_path)
        if len(library):
            return library
    return None


def read_digits(image: Image.Image, classifier,
                card_hint: Optional[Tuple[int, int, int, int]] = None
                ) -> Tuple[str, List[Tuple[str, float, Tuple[int, int, int, int]]]]:
    """Return (digits-string, per-glyph detail) for one frame.

    ``card_hint`` may carry the card box found in the previous shot of the same
    burst; it is only used when this frame's own detection comes up empty.
    """
    gray = to_gray(image)
    details = []
    for glyph in extract_glyphs(gray, card_hint=card_hint):
        digit, confidence = classifier.classify(glyph.pixels) if classifier else ("?", 0.0)
        details.append((digit, confidence, glyph.box))
    return "".join(d for d, _, _ in details), details


# ---------------------------------------------------------------------------
# Rotation robustness (test-time augmentation)
# ---------------------------------------------------------------------------
#: The card is laid down by hand, so the glyph arrives rotated; a 2 looks like a
#: 5/8 once tilted, and a leaning figure-8 reads as a 3 (measured 2026-10-28: a
#: digit written at ~45 deg came back "3" at 0.62 confidence, "8" at 0.61 only
#: when the frame was rotated by 45 deg).  Trying a few rotations is cheap (a few
#: tens of ms per frame) and needs no retraining.
ROBUST_ANGLES = (-45, -30, -15, 0, 15, 30, 45)


def read_digits_escalate(image: Image.Image, classifier,
                         angles=ROBUST_ANGLES,
                         escalate_below: float = 0.75,
                         margin: float = 0.05,
                         card_hint: Optional[Tuple[int, int, int, int]] = None
                         ) -> Tuple[str, List[Tuple[str, float, Tuple[int, int, int, int]]]]:
    """Plain read first; only spend the rotation sweep when it looks unsure.

    The sweep pays for itself on a card laid down at an angle (a 45-degree "8"
    reads as "3" at 0.62 confidence), but it also invents confident wrong
    answers when the card was already upright -- on the 52-case stress set the
    plain reader scores 44 and the sweeping one only 31.  So: trust a confident
    plain reading, and only sweep when it is weak, then require the sweep to beat
    the plain reading by ``margin`` before overriding it.
    """
    text, details = read_digits(image, classifier, card_hint=card_hint)
    confidence = min((d[1] for d in details), default=0.0)
    if text and confidence >= escalate_below:
        return text, details
    sweep_text, sweep_details = read_digits_robust(
        image, classifier, angles=angles, card_hint=card_hint)
    if not sweep_text:
        return text, details
    plain_score = confidence * min((_aspect_weight(d[2]) for d in details), default=1.0)
    sweep_score = min((d[1] for d in sweep_details), default=0.0) * min(
        (_aspect_weight(d[2]) for d in sweep_details), default=1.0)
    if sweep_score > plain_score + margin:
        return sweep_text, sweep_details
    return text, details


def _aspect_weight(box: Tuple[int, int, int, int]) -> float:
    """How digit-shaped is this bounding box?

    A digit on a card is taller than it is wide (or roughly square): 1, 3, 4, 7
    are clearly tall, 0 and 8 about 1.3:1.  A box that is *wider* than tall means
    the glyph is still rotated, so its classifier confidence is not trustworthy
    no matter how high it looks -- that is exactly how a 45 deg "8" wins as "3".
    """
    w = max(1, box[2] - box[0])
    h = max(1, box[3] - box[1])
    aspect = h / float(w)
    if aspect < 0.95:
        return 0.6
    if aspect <= 2.4:
        return 1.0
    return 0.85


def read_digits_robust(image: Image.Image, classifier,
                       angles=ROBUST_ANGLES,
                       card_hint: Optional[Tuple[int, int, int, int]] = None
                       ) -> Tuple[str, List[Tuple[str, float, Tuple[int, int, int, int]]]]:
    """Read a frame at several rotations and keep the most plausible reading.

    "Plausible" is the minimum glyph confidence discounted by how digit-shaped
    the glyph's box is (``_aspect_weight``); ties fall back to the smallest
    |angle| so an upright card is never overridden by a lucky tilt.
    """
    best = None  # (score, |angle|, text, details)
    for angle in angles:
        if angle == 0:
            sample = image
        else:
            sample = image.rotate(angle, resample=Image.BILINEAR)
            # A rotated frame no longer matches the previous frame's card box.
            card_hint = None
        text, details = read_digits(sample, classifier, card_hint=card_hint)
        if not text:
            continue
        confidence = min((d[1] for d in details), default=0.0)
        weight = min(_aspect_weight(d[2]) for d in details)
        score = confidence * weight
        key = (score, -abs(angle))
        if best is None or key > best[0]:
            best = (key, text, details)
    if best is None:
        return "", []
    return best[1], best[2]
