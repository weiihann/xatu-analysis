from state_access import config_v2 as cfg
from state_access.history_config import MERGE_BLOCK


def test_sweep_windows_match_v1_grid():
    assert cfg.SWEEP_WINDOWS == [30, 90, 180, 365]


def test_anchors_v2_newest_is_snapshot_anchor():
    for t in cfg.SWEEP_WINDOWS:
        assert cfg.anchors_v2(t)[-1] == cfg.ANCHOR_BLOCK_V2


def test_anchors_v2_weekly_ascending():
    a = cfg.anchors_v2(30)
    assert a == sorted(a)
    assert {b - x for x, b in zip(a, a[1:])} == {cfg.SWEEP_STEP} == {50_400}


def test_anchors_v2_floor_keeps_lookback_post_merge():
    for t in cfg.SWEEP_WINDOWS:
        a = cfg.anchors_v2(t)
        floor = MERGE_BLOCK + t * 7_200
        assert a[0] >= floor            # whole lookback stays post-merge
        assert a[0] - cfg.SWEEP_STEP < floor  # can't fit another anchor below


def test_anchors_v2_counts_are_plausible():
    counts = {t: len(cfg.anchors_v2(t)) for t in cfg.SWEEP_WINDOWS}
    assert 175 <= counts[30] <= 185
    assert 130 <= counts[365] <= 140
