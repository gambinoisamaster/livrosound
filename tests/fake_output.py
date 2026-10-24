"""Stand-ins for BookPlayer's sound card and speed changer: deterministic, no hardware.

FakeOutput mimics the slice of QAudioSink that BookPlayer uses (push mode):
writes fill a fixed-size buffer, and advance(ms) "plays" that much of it, which
is what processedUSecs() reports. Everything ever written stays in `written`,
so a test can compare it with the blocks' audio byte for byte.

FakeOutputs is an output factory that keeps every FakeOutput it made (the
player opens a new one on every play, seek and speed change). InlineExecutor
runs stretch jobs at once; ManualExecutor holds them until the test says so
(a stretch that isn't ready yet). fake_stretch changes speed by picking
samples: lengths and timing behave like dsp.time_stretch, instantly.
"""

from __future__ import annotations

from concurrent.futures import Executor, Future

import numpy as np

from livrosound.dsp import SAMPLE_RATE, samples_of


class FakeOutput:
    def __init__(self, buffer_ms: int = 1000, *, can_start: bool = True):
        self.buffer_bytes = 2 * samples_of(buffer_ms)
        self.written = bytearray()
        self.played = 0  # bytes
        self.started = False
        self.stopped = False
        self._can_start = can_start

    # ---------- the AudioOutput protocol ----------

    def start(self):
        if not self._can_start:
            return None
        self.started = True
        return self

    def write(self, data: bytes) -> int:
        n = min(len(data), self.bytesFree())
        self.written += data[:n]
        return n

    def bytesFree(self) -> int:
        return self.buffer_bytes - (len(self.written) - self.played)

    def bufferSize(self) -> int:
        return self.buffer_bytes

    def processedUSecs(self) -> int:
        return self.played // 2 * 1_000_000 // SAMPLE_RATE

    def stop(self) -> None:
        self.stopped = True

    # ---------- the test's clock ----------

    def advance(self, ms: float) -> None:
        """Play ms of what's buffered (less if it runs dry)."""
        if self.started and not self.stopped:
            self.played = min(len(self.written), self.played + 2 * samples_of(ms))

    @property
    def heard(self) -> bytes:
        """Everything played so far."""
        return bytes(self.written[: self.played])


class FakeOutputs:
    """An output factory for BookPlayer that keeps every FakeOutput it made."""

    def __init__(self, **kwargs):
        self.made: list[FakeOutput] = []
        self._kwargs = kwargs

    def __call__(self, parent=None) -> FakeOutput:
        out = FakeOutput(**self._kwargs)
        self.made.append(out)
        return out

    @property
    def current(self) -> FakeOutput | None:
        return self.made[-1] if self.made else None


class InlineExecutor(Executor):
    """Runs each job at once, on the calling thread."""

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future


class ManualExecutor(Executor):
    """Holds jobs until run_all()."""

    def __init__(self):
        self.jobs: list[tuple[Future, object, tuple, dict]] = []

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future = Future()
        self.jobs.append((future, fn, args, kwargs))
        return future

    def run_all(self) -> None:
        jobs, self.jobs = self.jobs, []
        for future, fn, args, kwargs in jobs:
            if future.set_running_or_notify_cancel():
                future.set_result(fn(*args, **kwargs))


def fake_stretch(pcm: bytes, rate: float) -> bytes:
    """pcm rate times as fast by picking every rate-th sample (the pitch changes, the timing is exact)."""
    x = np.frombuffer(pcm, "<i2")
    picks = (np.arange(round(len(x) / rate)) * rate).astype(np.int64)
    return x[picks].tobytes()
