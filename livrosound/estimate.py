"""How long the rest of a book's audio takes to make, in normal and in
audio-generation-only mode.

Pocket-TTS makes speech about 4.3× faster than it plays, and narration runs at
about 15 characters a second, so a Mac makes about DEFAULT_RATE characters of
text a second. Each Mac differs, so the window measures the real rate while a
run goes (RateMeter: the first block is left out, it carries the model's load)
and keeps a running average per mode in Settings, for the next estimate.

Generation-only mode turns off streaming, the text and page rendering, so the
CPU only makes audio; until it has been measured on this Mac it is assumed to
be GENERATION_ONLY_GAIN faster. That is an estimate, and the UI says so.
"""

from __future__ import annotations

import math
import time

DEFAULT_RATE = 60.0  # characters of text per second of generation
GENERATION_ONLY_GAIN = 1.08
MIN_MEASURED_S = 20.0  # a measurement shorter than this isn't kept
NARRATION_CHARS_PER_S = 15.0  # how fast the narrator reads at 1× (for audio not made yet)


class RateMeter:
    """Characters made per second during one run."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._start: float | None = None
        self._chars = 0

    def block_done(self, chars: int) -> None:
        if self._start is None:
            self._start = self._clock()  # the first block carries the model's load: measure after it
            return
        self._chars += chars

    def seconds(self) -> float:
        return 0.0 if self._start is None else self._clock() - self._start

    def rate(self) -> float | None:
        """Measured characters a second, once there is enough to go by."""
        seconds = self.seconds()
        if seconds < 5 or self._chars <= 0:
            return None
        return self._chars / seconds

    def worth_keeping(self) -> bool:
        return self.seconds() >= MIN_MEASURED_S and self.rate() is not None


def blend(saved: float, measured: float) -> float:
    """A running average of the rates measured on this Mac."""
    return measured if saved <= 0 else 0.6 * saved + 0.4 * measured


def rates(saved_normal: float, saved_only: float, live: float | None = None,
          live_is_generation_only: bool = False) -> tuple[float, float]:
    """(normal, generation-only) characters a second to estimate with."""
    normal = saved_normal if saved_normal > 0 else DEFAULT_RATE
    only = saved_only if saved_only > 0 else 0.0
    if live is not None:
        if live_is_generation_only:
            only = live
        else:
            normal = live
    if only <= 0:
        only = normal * GENERATION_ONLY_GAIN
    elif saved_normal <= 0 and live is None:
        normal = only / GENERATION_ONLY_GAIN
    return normal, only


def duration_text(seconds: float) -> str:
    """'2 h 10 min', '25 min', 'under a minute'."""
    minutes = math.ceil(seconds / 60)
    if minutes <= 1:
        return "under a minute" if seconds < 50 else "1 min"
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"
