from __future__ import annotations

import pytest

from livrosound.timeline import Timeline

# Blocks 1, 3, 5, 6, 9 get audio (0, 2, 4, 7, 8 are images, separators-off, etc.).
ORDER = [1, 3, 5, 6, 9]


def full() -> Timeline:
    return Timeline(ORDER, {1: 4000, 3: 6000, 5: 3000, 6: 5000, 9: 8000})


# ---------- construction ----------


def test_order_must_ascend():
    with pytest.raises(ValueError):
        Timeline([3, 1], {})
    with pytest.raises(ValueError):
        Timeline([1, 1], {})


def test_durations_outside_the_order_are_ignored():
    t = Timeline(ORDER, {2: 1000, 3: 500})
    assert not t.ready(2)
    assert t.ready(3)
    t.set_duration(4, 900)
    assert not t.ready(4)


def test_set_duration_marks_ready_and_updates_offsets():
    t = Timeline(ORDER, {1: 4000})
    assert not t.ready(3)
    assert t.start_ms_of(5) is None
    t.set_duration(3, 6000)
    assert t.ready(3)
    assert t.start_ms_of(5) == 10000
    t.set_duration(3, 1000)  # regenerated
    assert t.start_ms_of(5) == 5000


# ---------- neighbours ----------


def test_first_at_or_after():
    t = full()
    assert t.first_at_or_after(0) == 1
    assert t.first_at_or_after(1) == 1
    assert t.first_at_or_after(2) == 3
    assert t.first_at_or_after(7) == 9
    assert t.first_at_or_after(9) == 9
    assert t.first_at_or_after(10) is None
    assert t.first_at_or_after(-5) == 1


def test_next_and_prev_ignore_readiness():
    t = Timeline(ORDER, {1: 1000})  # only block 1 ready
    assert t.next_block(1) == 3
    assert t.next_block(3) == 5
    assert t.next_block(9) is None
    assert t.prev_block(9) == 6
    assert t.prev_block(1) is None


def test_next_and_prev_from_blocks_off_the_order():
    t = full()
    assert t.next_block(2) == 3
    assert t.prev_block(2) == 1
    assert t.next_block(0) == 1
    assert t.prev_block(0) is None
    assert t.next_block(100) is None
    assert t.prev_block(100) == 9


def test_empty_order():
    t = Timeline([], {})
    assert t.first_at_or_after(0) is None
    assert t.next_block(0) is None
    assert t.prev_block(0) is None
    assert not t.ready(0)
    assert t.skip(0, 0, 10000) == (0, 0)
    assert t.skip(0, 0, -10000) == (0, 0)
    assert t.start_ms_of(0) == 0


# ---------- skip ----------


def test_skip_within_a_block():
    t = full()
    assert t.skip(3, 1000, 2000) == (3, 3000)
    assert t.skip(3, 3000, -2000) == (3, 1000)
    assert t.skip(3, 1000, 0) == (3, 1000)


def test_skip_back_into_ready_previous_block():
    t = full()
    # At 2 s into block 5, −10 s: 8 s back from block 5's start = 6000 - 8000 → into block 1.
    assert t.skip(5, 2000, -10000) == (1, 2000)
    # −3 s from 2 s into block 5 lands 1 s before the end of block 3.
    assert t.skip(5, 2000, -3000) == (3, 5000)


def test_skip_forward_across_blocks():
    t = full()
    assert t.skip(1, 3000, 2000) == (3, 1000)
    # 1 s left of block 1 + all 6 s of block 3 + all 3 s of block 5 = 10 s: block 6 starts.
    assert t.skip(1, 3000, 10000) == (6, 0)
    assert t.skip(1, 3000, 12500) == (6, 2500)
    assert t.skip(5, 0, 3000) == (6, 0)


def test_skip_lands_exactly_on_block_edges():
    t = full()
    assert t.skip(3, 0, -1) == (1, 3999)
    assert t.skip(3, 0, -4000) == (1, 0)
    assert t.skip(3, 5999, 1) == (5, 0)


def test_skip_forward_clamps_at_end_of_ready_run():
    t = Timeline(ORDER, {1: 4000, 3: 6000})  # 5 is not ready yet
    assert t.skip(3, 1000, 10000) == (3, 5999)
    assert t.skip(1, 0, 10**9) == (3, 5999)


def test_skip_forward_clamps_at_end_of_book():
    t = full()
    assert t.skip(9, 5000, 10000) == (9, 7999)
    assert t.skip(1, 0, 10**12) == (9, 7999)


def test_skip_back_clamps_at_start_of_ready_run():
    t = Timeline(ORDER, {5: 3000, 6: 5000, 9: 8000})  # 1 and 3 are not ready
    assert t.skip(6, 1000, -10000) == (5, 0)
    assert t.skip(9, 0, -10**12) == (5, 0)


def test_skip_back_clamps_at_start_of_book():
    t = full()
    assert t.skip(1, 2000, -10000) == (1, 0)
    assert t.skip(3, 1000, -10**12) == (1, 0)


def test_skip_never_crosses_a_gap():
    # Two runs: 1-3 and 6-9, with 5 missing between them.
    t = Timeline(ORDER, {1: 4000, 3: 6000, 6: 5000, 9: 8000})
    assert t.skip(3, 5000, 10000) == (3, 5999)
    assert t.skip(6, 2000, -10000) == (6, 0)


def test_skip_from_a_block_without_audio():
    t = Timeline(ORDER, {1: 4000, 3: 6000})
    # Waiting at block 5 (no audio): it counts as its own start.
    assert t.skip(5, 0, 10000) == (5, 0)
    assert t.skip(5, 1234, 10000) == (5, 0)
    assert t.skip(5, 0, -1000) == (3, 5000)
    assert t.skip(5, 1234, -10000) == (1, 0)
    # Its previous block isn't ready either: nowhere to go.
    assert Timeline(ORDER, {1: 4000}).skip(5, 0, -10000) == (5, 0)


def test_skip_from_a_block_off_the_order():
    t = full()
    # Block 4 (an image) sits between 3 and 5: back walks into 3, forward stays.
    assert t.skip(4, 0, -1000) == (3, 5000)
    assert t.skip(4, 0, 1000) == (4, 0)


def test_skip_normalizes_a_position_past_the_block_end():
    t = full()
    assert t.skip(1, 5000, 0) == (3, 1000)
    assert t.skip(1, 5000, -500) == (3, 500)


def test_skip_with_negative_ms_walks_back():
    t = full()
    assert t.skip(3, -1000, 0) == (1, 3000)


def test_skip_through_zero_length_blocks():
    t = Timeline([1, 2, 3], {1: 1000, 2: 0, 3: 1000})
    assert t.skip(1, 500, 1000) == (3, 500)
    assert t.skip(3, 500, -1000) == (1, 500)
    assert Timeline([1, 2], {1: 1000, 2: 0}).skip(1, 0, 5000) == (2, 0)


def test_single_block():
    t = Timeline([4], {4: 2500})
    assert t.skip(4, 1000, 10000) == (4, 2499)
    assert t.skip(4, 1000, -10000) == (4, 0)
    assert t.skip(4, 1000, 1000) == (4, 2000)
    assert t.next_block(4) is None
    assert t.prev_block(4) is None
    assert t.start_ms_of(4) == 0
    assert t.start_ms_of(5) == 2500
    assert Timeline([4], {}).skip(4, 0, 10000) == (4, 0)


# ---------- start offsets ----------


def test_start_ms_of_when_everything_before_is_ready():
    t = full()
    assert t.start_ms_of(1) == 0
    assert t.start_ms_of(3) == 4000
    assert t.start_ms_of(5) == 10000
    assert t.start_ms_of(6) == 13000
    assert t.start_ms_of(9) == 18000
    assert t.start_ms_of(10) == 26000  # past the end: the total


def test_start_ms_of_off_the_order_matches_next_ordered_block():
    t = full()
    assert t.start_ms_of(0) == 0
    assert t.start_ms_of(2) == t.start_ms_of(3)
    assert t.start_ms_of(7) == t.start_ms_of(9)


def test_start_ms_of_needs_every_earlier_block():
    t = Timeline(ORDER, {1: 4000, 5: 3000, 6: 5000})  # 3 missing
    assert t.start_ms_of(1) == 0
    assert t.start_ms_of(3) == 4000  # 3 itself needn't be ready
    assert t.start_ms_of(5) is None
    assert t.start_ms_of(9) is None
    assert Timeline(ORDER, {}).start_ms_of(1) == 0
    assert Timeline(ORDER, {}).start_ms_of(3) is None
