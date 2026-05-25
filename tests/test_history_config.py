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


def test_anchors_floor_keeps_lookback_post_merge():
    a = hc.anchors(30)
    floor = hc.start_block(30)
    assert a[0] >= floor
    assert a[0] - hc.STEP < floor  # can't fit another step below the floor


def test_start_block_scales_with_window():
    # A larger window pushes the earliest valid anchor later (longer lookback).
    assert hc.start_block(365) > hc.start_block(180) > hc.start_block(90) > hc.start_block(30)
    assert hc.start_block(90) == hc.MERGE_BLOCK + 91 * 7_200
    assert hc.anchors(365)[0] >= hc.start_block(365)


def test_parquet_path_encodes_window():
    assert hc.parquet_for(90).name == "history_w90.parquet"


def test_block_to_date_at_merge():
    assert hc.block_to_date(hc.MERGE_BLOCK) == datetime(2022, 9, 15, 6, 42, 59, tzinfo=timezone.utc)


def test_block_to_date_one_day_later():
    one_day = hc.block_to_date(hc.MERGE_BLOCK + 7_200)
    assert one_day == datetime(2022, 9, 16, 6, 42, 59, tzinfo=timezone.utc)
