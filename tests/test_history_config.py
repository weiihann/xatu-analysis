from datetime import datetime, timezone

from state_access import history_config as hc


def test_anchors_end_at_static_anchor():
    a = hc.anchors()
    assert a[-1] == hc.END_BLOCK == 24_870_000


def test_anchors_sorted_ascending_and_evenly_spaced():
    a = hc.anchors()
    assert a == sorted(a)
    diffs = {b - x for x, b in zip(a, a[1:])}
    assert diffs == {hc.STEP}


def test_anchors_stay_within_post_merge_range():
    a = hc.anchors()
    assert a[0] >= hc.START_BLOCK
    assert a[0] - hc.STEP < hc.START_BLOCK  # can't fit another step below


def test_block_to_date_at_merge():
    assert hc.block_to_date(hc.MERGE_BLOCK) == datetime(2022, 9, 15, 6, 42, 59, tzinfo=timezone.utc)


def test_block_to_date_one_day_later():
    one_day = hc.block_to_date(hc.MERGE_BLOCK + 7_200)
    assert one_day == datetime(2022, 9, 16, 6, 42, 59, tzinfo=timezone.utc)
