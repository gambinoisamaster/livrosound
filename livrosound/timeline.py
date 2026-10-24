"""Position arithmetic over a book's audio: pure, no Qt, no I/O.

A position is (block, ms into that block's audio). The timeline knows the
blocks that get audio, in reading order (ascending block indices), and the
duration of each one generated so far. Generation can start anywhere (a click,
a Contents pick), so the audio that exists is a set of runs with gaps between
them; the rules below keep every move inside audio that's actually there.

Rules:
- skip() (−10 / +10 sec) walks across neighbouring blocks only while they're
  ready and clamps at the edge of the contiguous ready run: the run's first
  block at 0 going back, its last block at duration − 1 going forward.
- A block without audio (or one that isn't on the timeline, like an image)
  counts as its own start: skipping forward from it stays at (block, 0);
  skipping back walks into the ready audio just before it.
- next_block() / prev_block() step through the order whether or not the
  neighbour has audio (the player asks for audio it lacks).
- start_ms_of() is defined only while every ordered block before the block is
  ready, since otherwise its offset isn't known yet (export, chapter times).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right


class Timeline:
    def __init__(self, order: list[int], durations: dict[int, int]):
        if any(a >= b for a, b in zip(order, order[1:])):
            raise ValueError("order must be strictly ascending block indices")
        self._order = list(order)
        self._index = {block: i for i, block in enumerate(self._order)}
        self._durations: dict[int, int] = {}
        # Start offsets of order[0..k] while every block before them is ready; built lazily.
        self._starts: list[int] | None = None
        for block, duration_ms in durations.items():
            self.set_duration(block, duration_ms)

    def set_duration(self, block: int, duration_ms: int) -> None:
        """A block just got audio. Blocks that aren't on the timeline are ignored."""
        if block in self._index:
            self._durations[block] = max(0, duration_ms)
            self._starts = None

    def ready(self, block: int) -> bool:
        return block in self._durations

    def first_at_or_after(self, block_index: int) -> int | None:
        """The first ordered block at or after any block index (None past the end)."""
        i = bisect_left(self._order, block_index)
        return self._order[i] if i < len(self._order) else None

    def next_block(self, block: int) -> int | None:
        i = bisect_right(self._order, block)
        return self._order[i] if i < len(self._order) else None

    def prev_block(self, block: int) -> int | None:
        i = bisect_left(self._order, block)
        return self._order[i - 1] if i else None

    def skip(self, block: int, ms: int, delta_ms: int) -> tuple[int, int]:
        """Move delta_ms from (block, ms) through the ready audio, clamping at the edge of its run."""
        if not self.ready(block):
            ms = 0
        target = ms + delta_ms
        while target < 0:
            prev = self.prev_block(block)
            if prev is None or not self.ready(prev):
                return block, 0
            block = prev
            target += self._durations[block]
        if not self.ready(block):
            return block, 0
        while target >= self._durations[block]:
            nxt = self.next_block(block)
            if nxt is None or not self.ready(nxt):
                return block, max(0, self._durations[block] - 1)
            target -= self._durations[block]
            block = nxt
        return block, target

    def start_ms_of(self, block: int) -> int | None:
        """Offset of a block from the start of the first ordered block, if every ordered
        block before it is ready. Any block index works (an image's is where it sits)."""
        if self._starts is None:
            self._starts = [0]
            for b in self._order:
                if b not in self._durations:
                    break
                self._starts.append(self._starts[-1] + self._durations[b])
        i = bisect_left(self._order, block)
        return self._starts[i] if i < len(self._starts) else None
