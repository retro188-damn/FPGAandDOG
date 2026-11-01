"""Off-robot checks for the seek decisions (``host/app/vision/seek.py``).

The dog is simulated: a camera that only sees the card when it is inside the
field of view, and a body whose heading changes when it is told to turn.  That
is enough to prove the sweep covers a full turn, that the uncalibrated centring
loop converges for lenses both wider and narrower than its starting gain, and
that the approach stops at a width the classifier can actually read.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.app.vision import seek  # noqa: E402


class FakeDog:
    """A dog that can only see what its camera is pointing at."""

    def __init__(self, card_heading_deg: float, px_per_deg: float = 9.55,
                 half_fov_deg: float = 33.5, width_px: int = 110,
                 vanish_after_looks: int | None = None) -> None:
        self.heading = 0.0
        self.card_heading = card_heading_deg
        self.px_per_deg = px_per_deg
        self.half_fov = half_fov_deg
        self.width = width_px
        self.turns = []
        self.looks = 0
        self.vanish_after_looks = vanish_after_looks

    def bearing(self) -> float:
        delta = self.card_heading - self.heading
        return (delta + 180.0) % 360.0 - 180.0

    def look(self):
        self.looks += 1
        if self.vanish_after_looks is not None and self.looks > self.vanish_after_looks:
            return None
        bearing = self.bearing()
        if abs(bearing) > self.half_fov:
            return None
        # A card to the left of the view axis lands left of centre in the image.
        centre = seek.CENTER_X - bearing * self.px_per_deg
        half = self.width / 2.0
        return (int(centre - half), 200, int(centre + half), 260)

    def width_only(self):
        box = self.look()
        return None if box is None else seek.box_width(box)

    def turn_to(self, offset_deg):
        self.heading = float(offset_deg)
        self.turns.append(float(offset_deg))

    def turn_by(self, angle_deg):
        self.heading += float(angle_deg)
        self.turns.append(float(angle_deg))


def test_sweep_finds_card_behind_the_dog():
    dog = FakeDog(card_heading_deg=170.0)
    result = seek.sweep_for_card(dog.look, dog.turn_to)
    assert result.found
    assert result.heading_offset_deg == 160.0, "a card 170 deg behind is seen at +160"
    assert result.stops == 8, "eight stops: it never has to try -160"


def test_sweep_stops_at_first_visible_heading():
    dog = FakeDog(card_heading_deg=-45.0)
    result = seek.sweep_for_card(dog.look, dog.turn_to)
    assert result.found
    assert result.heading_offset_deg == -40.0, "three stops: 0, +40, -40"
    assert dog.turns == [0.0, 40.0, -40.0]


def test_sweep_reports_when_nothing_is_out_there():
    dog = FakeDog(card_heading_deg=0.0, half_fov_deg=5.0)  # narrow lens misses the sides
    result = seek.sweep_for_card(dog.look, dog.turn_to)
    assert result.found
    # Straight ahead is looked at first, so this one never gets to the "miss".
    assert result.heading_offset_deg == 0.0

    blind = FakeDog(card_heading_deg=0.0)
    blind.card_heading = 0.0
    blind.half_fov = -1.0  # nothing is ever in view
    assert not seek.sweep_for_card(blind.look, blind.turn_to).found


def test_centring_converges_for_a_wider_lens_than_the_starting_gain():
    # 9.55 px/deg is ~67 deg over 640 px, wider than the 0.08 deg/px guess.
    dog = FakeDog(card_heading_deg=0.0, px_per_deg=9.55)
    dog.heading = 15.0                        # card sits 15 deg to the left
    result = seek.centre_card(dog.look, dog.turn_by)
    assert result.ok
    assert abs(result.offset_px) <= 40.0
    assert dog.turns, "it must actually turn"
    assert abs(dog.bearing()) <= 6.0, "within the executor's own tolerance"


def test_centring_recovers_when_the_starting_gain_oversteers():
    # 25 px/deg is a much narrower lens: 0.08 deg/px would oversteer ~2x.
    dog = FakeDog(card_heading_deg=0.0, px_per_deg=25.0)
    dog.heading = 12.0
    result = seek.centre_card(dog.look, dog.turn_by)
    assert result.gain_deg_per_px < seek.START_GAIN_DEG_PER_PX
    assert (result.ok, abs(result.offset_px)) and abs(result.offset_px) <= 60.0


def test_centring_gives_up_when_the_card_vanishes():
    dog = FakeDog(card_heading_deg=0.0, vanish_after_looks=1)
    dog.heading = 8.0
    result = seek.centre_card(dog.look, dog.turn_by)
    assert not result.ok
    assert "lost" in result.reason


def test_approach_stops_once_the_card_is_wide_enough():
    widths = iter([96, 102, 108, 124, 130])
    legs = []

    def look_width():
        return next(widths, 130)

    def move_leg():
        legs.append(1)
        return True

    result = seek.walk_to_width(look_width, move_leg)
    assert result.ok
    assert result.width_px == 124
    assert result.legs == 3
    assert len(legs) == 3


def test_approach_stops_when_it_gets_too_close():
    widths = iter([100, 340])
    result = seek.walk_to_width(lambda: next(widths, 340), lambda: True,
                                target_px=120, max_px=320)
    assert not result.ok
    assert "too close" in result.reason


def test_approach_reports_a_refused_leg():
    result = seek.walk_to_width(lambda: 96, lambda: False)
    assert not result.ok
    assert result.reason == "walk refused"


def test_approach_accepts_a_card_that_is_already_close_enough():
    result = seek.walk_to_width(lambda: 130, lambda: True)
    assert result.ok and result.legs == 0


def test_hunt_stops_on_the_requested_digit():
    """Three cards, and only the third one that comes into view is the target."""
    cards = {0.0: ("3", 0.99), 80.0: ("8", 0.97)}
    dog = FakeDog(card_heading_deg=0.0)
    reads = []

    def look():
        bearing = dog.bearing()
        for heading, _ in cards.items():
            if abs(((heading - dog.heading) + 180) % 360 - 180) <= dog.half_fov:
                return (300, 200, 380, 260)
        return None

    def centre(box):
        dog.turn_by(0.0)          # pretend the card was already centred
        return box

    def read():
        heading = min(cards, key=lambda h: abs(((h - dog.heading) + 180) % 360 - 180))
        reads.append(heading)
        return cards[heading]

    result = seek.hunt_for_digit(look, centre, read, dog.turn_to, "8")
    assert result.found
    assert result.heading_offset_deg == 80.0
    assert [s[1] for s in result.seen] == ["3", "8"]
    assert reads == [0.0, 80.0]


def test_hunt_reports_the_digits_it_saw_when_none_match():
    dog = FakeDog(card_heading_deg=0.0)
    hit = {"n": 0}

    def look():
        return (300, 200, 380, 260)

    def centre(box):
        return box

    def read():
        hit["n"] += 1
        return ("3", 0.99)

    result = seek.hunt_for_digit(look, centre, read, dog.turn_to, "8")
    assert not result.found
    assert [s[1] for s in result.seen] == ["3"] * 9
    assert "8" in result.reason


def test_hunt_ignores_an_unconfident_match():
    dog = FakeDog(card_heading_deg=0.0)
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        return ("8", 0.40)        # the digit matches but the model is unsure

    result = seek.hunt_for_digit(lambda: (300, 200, 380, 260), lambda b: b, read,
                                 dog.turn_to, "8")
    assert not result.found
    # one read per stop plus two re-reads for each near miss (same digit, unsure)
    assert calls["n"] == 27


def test_hunt_rescues_a_near_miss_on_a_second_look():
    """The right digit at 0.66 is worth another shot -- it read 8 @0.66 at 1.5 m."""
    dog = FakeDog(card_heading_deg=-80.0)
    queue = [("8", 0.66), ("8", 0.91)]

    def read():
        return queue.pop(0) if queue else ("8", 0.91)

    result = seek.hunt_for_digit(lambda: (300, 200, 380, 260), lambda b: b, read,
                                 dog.turn_to, "8")
    assert result.found
    # the first look was under the bar, the re-read lifted it over
    assert result.seen[-1][2] >= 0.9


def test_hunt_reads_before_aiming_so_a_square_on_card_costs_no_turn():
    """2026-10-28: the dog was parked squarely on the blue 2 and still turned.

    Reading first is what removes that pointless "adjustment" turn; aiming is
    only spent on a suspected hit or on the retry pass.
    """
    dog = FakeDog(card_heading_deg=0.0)

    def centre(box):
        raise AssertionError("centring must not run before the first reading")

    result = seek.hunt_for_digit(dog.look, centre, lambda: ("8", 0.95),
                                 dog.turn_to, "8")
    assert result.found
    assert [t for t in dog.turns if abs(t) > 1e-9] == []   # no aiming turn at all


def test_hunt_retry_pass_aims_before_reading():
    dog = FakeDog(card_heading_deg=0.0)
    aimed = []

    def centre(box):
        aimed.append(box)
        return box

    result = seek.hunt_for_digit(dog.look, centre, lambda: ("8", 0.95),
                                 dog.turn_to, "8", centre_before_read=True)
    assert result.found
    assert len(aimed) == 1          # exactly one aim, on the card it then read
