"""Tests for the per-frame quality gate used by the stop-then-shoot burst.

Every frame here is synthetic: a bright card on a darker floor, with a dark
band standing in for the shoes that keep `find_card`'s ``dark_fraction`` test
happy.  No camera, no model, no dog.
"""
from __future__ import annotations

import numpy as np

from PIL import Image, ImageFilter

from host.app.vision.frame_quality import (
    MIN_SHARPNESS,
    MIN_WIDTH_PX,
    measure,
    pick_best,
)


def scene(card=(60, 60, 200, 140), blur=0, ink_noise=8.0, size=(240, 320)):
    """A bright card on a noisy floor, with a dark band at the top."""
    height, width = size
    rng = np.random.default_rng(3)
    frame = 100.0 + rng.normal(0, 18.0, (height, width))
    frame[0:22, :] = 30.0
    x0, y0, x1, y1 = card
    frame[y0:y1, x0:x1] = 220.0 + rng.normal(0, ink_noise, (y1 - y0, x1 - x0))
    image = Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8)).convert("RGB")
    return image.filter(ImageFilter.GaussianBlur(blur)) if blur else image


def test_sharp_card_passes_with_its_measurements():
    quality = measure(scene())
    assert quality.ok is True
    assert quality.has_card is True
    assert quality.width_px >= MIN_WIDTH_PX
    assert quality.sharpness > 1000.0
    assert quality.edge_cut is False
    assert quality.reason == "ok"


def test_blurred_card_is_rejected_before_it_is_read():
    # Measured on the real pipeline: at blur radius 4 a `3` reads as `2` with
    # confidence 0.985, while the card is still trivially detected.  The gate
    # has to catch that, because the detector will not.
    for radius in (2, 4):
        quality = measure(scene(blur=radius))
        assert quality.has_card is True, "a blurred card is still a card"
        assert quality.ok is False
        assert quality.sharpness < MIN_SHARPNESS
        assert "blurred" in quality.reason


def test_card_cut_by_the_bottom_edge_is_rejected():
    quality = measure(scene(card=(60, 150, 200, 240)))
    assert quality.has_card is True
    assert quality.edge_cut is True
    assert quality.ok is False
    assert "cut" in quality.reason


def test_small_card_is_rejected():
    quality = measure(scene(card=(120, 150, 190, 195)))
    assert quality.has_card is True
    assert quality.ok is False
    assert "small" in quality.reason


def test_frame_without_a_card_is_rejected():
    quality = measure(Image.new("RGB", (320, 240), (105, 105, 105)))
    assert quality.has_card is False
    assert quality.ok is False
    assert quality.reason == "no card"


def test_pick_best_returns_only_the_usable_frames():
    frames = [scene(blur=4), scene(), scene(blur=2)]
    indices, qualities = pick_best(frames, top_k=3)
    assert indices == [1]
    assert qualities[1].ok is True


def test_pick_best_is_empty_when_every_frame_is_damaged():
    indices, qualities = pick_best([scene(blur=4), scene(blur=2)])
    assert indices == []
    assert all(not quality.ok for quality in qualities)


def test_pick_best_ranks_the_sharpest_first():
    frames = [scene(blur=2), scene(), scene(ink_noise=20.0)]
    indices, _qualities = pick_best(frames, top_k=2)
    assert indices[0] == 1
    assert len(indices) == 2
