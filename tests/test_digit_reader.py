"""Tests for the digit-reading chain (numpy/PIL only, no hardware).

Run from the repository root::

    python -m pytest tests/test_digit_reader.py -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from host.app.vision.digit_reader import (  # noqa: E402
    GLYPH_SIZE,
    DigitLibrary,
    MLPClassifier,
    extract_glyphs,
    ink_mask,
    load_classifier,
    normalise,
    otsu_threshold,
    read_digits,
    to_gray,
)

FONT = "C:/Windows/Fonts/arialbd.ttf"


def render_card(text: str, size: int = 220, width: int = 900, height: int = 420,
                paper: int = 245, ink: int = 20) -> Image.Image:
    """A white card with one black glyph: the demo's expected scene."""
    image = Image.new("RGB", (width, height), (paper, paper, paper - 5))
    draw = ImageDraw.Draw(image)
    draw.text((120, 80), text, fill=(ink, ink, ink), font=ImageFont.truetype(FONT, size))
    return image


# --------------------------------------------------------------------- basics
def test_otsu_splits_bimodal_histogram():
    gray = np.concatenate([np.full(500, 20.0), np.full(500, 230.0)]).astype(np.float32)
    assert 20 < otsu_threshold(gray) < 230


def test_ink_mask_rejects_flat_frame():
    gray = np.full((120, 160), 128.0, dtype=np.float32)
    assert not ink_mask(gray).any()


def test_ink_mask_marks_dark_glyph_on_paper():
    gray = to_gray(render_card("7"))
    mask = ink_mask(gray)
    assert mask.mean() > 0.01


# ---------------------------------------------------------------- extraction
def test_extract_glyphs_finds_one_digit_on_a_card():
    glyphs = extract_glyphs(to_gray(render_card("7")))
    assert len(glyphs) == 1
    assert glyphs[0].pixels.shape == (GLYPH_SIZE, GLYPH_SIZE)


def test_extract_glyphs_ignores_blobs_touching_the_frame_border():
    # A dark floor with one bright card: the floor is one huge border blob.
    image = Image.new("RGB", (640, 480), (40, 40, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle((200, 200, 420, 380), fill=(245, 245, 240))
    draw.text((240, 230), "3", fill=(15, 15, 15), font=ImageFont.truetype(FONT, 140))
    glyphs = extract_glyphs(to_gray(image))
    assert len(glyphs) == 1


def test_extract_glyphs_rejects_huge_blobs():
    glyphs = extract_glyphs(to_gray(render_card("8")), max_area_frac=0.0001)
    assert glyphs == []


def test_extract_glyphs_orders_left_to_right():
    image = Image.new("RGB", (1200, 420), (245, 245, 240))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 180)
    draw.text((80, 100), "1", fill=(10, 10, 10), font=font)
    draw.text((500, 100), "2", fill=(10, 10, 10), font=font)
    glyphs = extract_glyphs(to_gray(image))
    assert len(glyphs) == 2
    assert glyphs[0].box[0] < glyphs[1].box[0]


def test_normalise_centres_ink_on_the_glyph_frame():
    patch = np.zeros((40, 40), dtype=bool)
    patch[0:10, 0:5] = True  # ink in the top-left corner only
    out = normalise(patch)
    assert out.shape == (GLYPH_SIZE, GLYPH_SIZE)
    ys, xs = np.nonzero(out > 0.2)
    assert abs(ys.mean() - GLYPH_SIZE / 2) < 3.0
    assert abs(xs.mean() - GLYPH_SIZE / 2) < 3.0


def test_normalise_handles_empty_patch():
    assert not normalise(np.zeros((10, 10), dtype=bool)).any()


# ---------------------------------------------------------------- classifiers
def test_mlp_classifier_roundtrip(tmp_path):
    rng = np.random.default_rng(0)
    model = MLPClassifier(
        rng.standard_normal((GLYPH_SIZE * GLYPH_SIZE, 8)).astype(np.float32),
        np.zeros(8, dtype=np.float32),
        rng.standard_normal((8, 10)).astype(np.float32),
        np.zeros(10, dtype=np.float32),
    )
    path = tmp_path / "m.npz"
    np.savez_compressed(path, w1=model.w1, b1=model.b1, w2=model.w2, b2=model.b2)
    loaded = MLPClassifier.load(path)
    digit, confidence = loaded.classify(np.zeros((GLYPH_SIZE, GLYPH_SIZE), dtype=np.float32))
    assert digit in "0123456789"
    assert 0.0 <= confidence <= 1.0


def test_load_classifier_returns_none_when_no_model(tmp_path):
    assert load_classifier(tmp_path / "missing.npz", tmp_path / "missing2.npz") is None


def test_digit_library_classifies_by_nearest_sample(tmp_path):
    library = DigitLibrary()
    blank = np.zeros((GLYPH_SIZE, GLYPH_SIZE), dtype=np.float32)
    for digit in "0123456789":
        library.add(blank.copy(), digit)
    library.samples[7] = np.ones((GLYPH_SIZE, GLYPH_SIZE), dtype=np.float32) * 0.8
    digit, confidence = library.classify(library.samples[7])
    assert digit == "7"
    assert confidence > 0.5


def test_read_digits_uses_the_classifier_and_returns_boxes():
    class Fixed:
        def classify(self, glyph):
            return "5", 0.99

    text, details = read_digits(render_card("5"), Fixed())
    assert text == "5"
    assert len(details) == 1
    digit, confidence, box = details[0]
    assert (digit, confidence) == ("5", 0.99)
    assert len(box) == 4


def test_read_digits_without_classifier_marks_digits_unknown():
    text, details = read_digits(render_card("5"), None)
    assert text == "?"
    assert details[0][0] == "?"


import host.app.vision.digit_reader as dr  # noqa: E402


def test_component_boxes_are_half_open():
    """A 2x2 blob at the origin must be (0, 0, 2, 2, 4) in both labellers.

    The whole reader treats x1/y1 as exclusive (``gray[y0:y1, x0:x1]``), so an
    off-by-one here silently shrinks every card crop.
    """
    mask = np.zeros((6, 6), dtype=bool)
    mask[0:2, 0:2] = True
    expected = [(0, 0, 2, 2, 4)]
    assert dr._components_python(mask) == expected
    if dr._ndimage is not None:
        assert dr._components_scipy(mask) == expected


@pytest.mark.skipif(dr._ndimage is None, reason="scipy not installed")
def test_scipy_and_python_labellers_agree():
    """The scipy fast path must return exactly what the fallback returns."""
    rng = np.random.default_rng(3)
    for shape in ((80, 100), (37, 41)):
        for density in (0.15, 0.28, 0.5):
            mask = rng.random(shape) > density
            assert sorted(dr._components_scipy(mask)) == sorted(dr._components_python(mask))


def test_find_card_ignores_bright_floor_outlined_by_tile_seams():
    """A bright floor patch bounded by dark seams is not a card.

    Measured 2026-10-27: the seek locked onto bare floor (inside-ring contrast
    +12 grey levels) and then read nothing, because the "card" was a tile.
    """
    from host.app.vision.digit_reader import find_card

    gray = np.full((480, 640), 172.0)
    gray[300:306, :] = 40.0          # tile seam across the floor
    gray[:, 96:102] = 40.0
    gray[:, 470:476] = 40.0
    assert find_card(gray) is None


def test_find_card_finds_white_paper_on_a_darker_board():
    from host.app.vision.digit_reader import find_card

    gray = np.full((480, 640), 172.0)     # light tiled floor
    gray[0:120, :] = 60.0                 # a dark chair at the top of the view
    gray[180:340, 160:440] = 120.0        # brown board on the floor
    gray[210:310, 210:390] = 212.0        # white paper on the board
    box = find_card(gray)
    assert box is not None
    assert box[0] < 240 and box[2] > 370, "the paper, not the board"


def test_extract_glyphs_ignores_the_paper_rim():
    """The card's own shadow is a closed ring the classifier calls "0".

    Real 2026-10-28 failure: a real "8" came back as "03" -- the rim was read as
    a digit.  The rim wraps the whole card, so a blob nearly as big as the card
    must be rejected before it can be merged with the real glyph.
    """
    image = Image.new("RGB", (640, 480), (40, 40, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle((200, 160, 440, 360), fill=(235, 235, 230))
    draw.rectangle((206, 166, 434, 354), outline=(60, 60, 60), width=4)  # rim
    draw.ellipse((280, 220, 360, 300), outline=(20, 20, 20), width=12)
    glyphs = extract_glyphs(to_gray(image))
    assert len(glyphs) == 1
    x0, y0, x1, y1 = glyphs[0].box
    assert (x1 - x0) < 0.8 * (434 - 206)


def test_extract_glyphs_merges_stacked_halves_of_one_glyph():
    """A hand-drawn "8" with a gap between the loops is still ONE digit."""
    image = Image.new("RGB", (640, 480), (40, 40, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle((200, 140, 440, 380), fill=(240, 240, 235))
    draw.ellipse((290, 190, 350, 250), outline=(20, 20, 20), width=10)  # top loop
    draw.ellipse((290, 256, 350, 316), outline=(20, 20, 20), width=10)  # bottom loop
    glyphs = extract_glyphs(to_gray(image))
    assert len(glyphs) == 1
    assert (glyphs[0].box[3] - glyphs[0].box[1]) > 100  # both loops inside


def test_find_card_accepts_a_mid_grey_scene():
    """A brown board is mid-grey, so very few pixels are "dark".

    Real 2026-10-28 failure: the bowed close-up of a card on the brown board was
    rejected because the frame had dark_fraction 0.011 < 0.03, even though the
    card sat +54 grey levels above its surroundings.  The guard is only meant for
    a frame that is bright *everywhere* (paper filling the view).
    """
    from host.app.vision.digit_reader import find_card

    gray = np.full((480, 640), 132.0)      # brown board, mid grey
    gray[129:244, 131:337] = 183.0         # white card on it
    gray[470:480, :] = 40.0                # a sliver of dark floor line
    box = find_card(gray)
    assert box is not None, "a mid-grey scene must not disable card detection"
    assert box[0] < 200 and box[2] > 300


def test_find_card_still_rejects_a_uniformly_bright_frame():
    """Paper filling the view is not a card (it has no darker surroundings)."""
    from host.app.vision.digit_reader import find_card

    gray = np.full((480, 640), 210.0)
    gray[200:260, 280:360] = 190.0
    assert find_card(gray) is None


def test_squashed_glyph_on_a_card_is_not_rejected():
    """A floor card is seen at a grazing angle: a square digit arrives ~2:1.

    Real 2026-10-29 failure: a genuine "8" on a confirmed card measured 67x31 px
    (aspect 0.46) and the 0.5 aspect floor threw it away before the classifier
    saw it - which is why the reader answered "empty" at close range.  Inside a
    confirmed card the floor is now 0.30; without a card it stays 0.5.
    """
    from host.app.vision.digit_reader import MIN_GLYPH_ASPECT, _glyphs_in_mask

    mask = np.zeros((73, 139), dtype=bool)
    # a stroke, not a solid block: a real glyph fills only ~4% of the card crop
    mask[14:17, 40:107] = True             # 67x31 squashed glyph, aspect 0.46
    mask[42:45, 40:107] = True
    mask[14:45, 40:43] = True
    mask[14:45, 104:107] = True
    card = (0, 0, 139, 73)
    wide = _glyphs_in_mask(mask, (0, 0), 0.004, 0.20, 4, (False,) * 4,
                           card=card, min_aspect=MIN_GLYPH_ASPECT)
    assert wide, "a squashed glyph on a confirmed card must reach the classifier"
    assert MIN_GLYPH_ASPECT < 0.5
    narrow = _glyphs_in_mask(mask, (0, 0), 0.004, 0.20, 4, (False,) * 4,
                             card=card, min_aspect=0.5)
    assert not narrow, "the card-less fallback keeps the stricter floor"


def test_cascade_falls_back_only_when_the_primary_is_unsure():
    """The font model may rescue an unsure MNIST answer, never hijack a sure one."""
    from host.app.vision.digit_reader import CascadeClassifier

    class Fixed:
        def __init__(self, label, conf):
            self.label, self.conf = label, conf

        def classify(self, glyph):
            return self.label, self.conf

    glyph = np.zeros(GLYPH_SIZE * GLYPH_SIZE, dtype=np.float32)
    sure = CascadeClassifier(Fixed("2", 0.99), Fixed("7", 0.99))
    assert sure.classify(glyph) == ("2", 0.99)

    unsure = CascadeClassifier(Fixed("3", 0.40), Fixed("8", 0.94))
    assert unsure.classify(glyph) == ("8", 0.94)

    weak = CascadeClassifier(Fixed("3", 0.40), Fixed("8", 0.55))
    assert weak.classify(glyph)[0] == "3", "a weak second opinion must not win"
