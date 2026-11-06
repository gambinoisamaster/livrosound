"""Generation time estimates."""

from __future__ import annotations

import pytest

from livrosound import estimate


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_rate_meter_skips_the_first_block():
    clock = Clock()
    meter = estimate.RateMeter(clock)
    assert meter.rate() is None
    meter.block_done(5000)  # model load: not counted
    clock.now += 10
    meter.block_done(600)
    assert meter.rate() == pytest.approx(60.0) and not meter.worth_keeping()
    clock.now += 20
    meter.block_done(1200)
    assert meter.rate() == pytest.approx(60.0) and meter.worth_keeping()


def test_rates_and_texts():
    assert estimate.rates(0, 0) == (60.0, pytest.approx(64.8))
    assert estimate.rates(50, 0) == (50, pytest.approx(54.0))
    assert estimate.rates(50, 70) == (50, 70)
    assert estimate.rates(0, 70) == (pytest.approx(70 / 1.08), 70)
    assert estimate.rates(50, 0, live=40) == (40, pytest.approx(43.2))
    assert estimate.rates(50, 0, live=80, live_is_generation_only=True) == (50, 80)
    assert estimate.blend(0, 40) == 40 and estimate.blend(50, 40) == pytest.approx(46)
    assert estimate.duration_text(7800) == "2 h 10 min" and estimate.duration_text(1500) == "25 min"
    assert estimate.duration_text(3600) == "1 h" and estimate.duration_text(30) == "under a minute"
