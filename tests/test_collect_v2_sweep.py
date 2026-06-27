import pandas as pd

from state_access.v2.collect_sweep import build_row, remaining_anchors


def test_remaining_anchors_skips_done():
    existing = pd.DataFrame({"anchor_block": [100, 200]})
    assert remaining_anchors([300, 200, 100], existing) == [300, 200, 100][0:1]


def test_remaining_anchors_empty_checkpoint():
    assert remaining_anchors([3, 2, 1], None) == [3, 2, 1]


def test_build_row_flattens_with_prefixes():
    slot = {"W": 10, "R": 4, "RW_union": 14}
    acct = {"W": 3, "R": 1, "RW_union": 4}
    conc = {"slot_top1_W": 0.5, "acct_top1_W": 0.6}
    upd = {"total_updates": 9, "warm_updates": 8, "cold_updates": 1, "pct_warm": 88.9}
    sfo = {"total_slots": 14, "first_is_write": 9, "first_is_zero_read": 3,
           "first_is_nonzero_read": 2}
    afo = {"total_accounts": 4, "first_is_write": 3, "first_is_nonzero_read": 1,
           "first_is_zero_read": 0, "first_is_appearance_read": 0}
    res = {"total_r": 1, "empty_accounts": 0, "nonempty_accounts": 1,
           "unknown_accounts": 0}
    row = build_row(anchor=24_870_000, window_days=30, slot=slot, acct=acct, conc=conc,
                    upd=upd, sfo=sfo, afo=afo, res=res,
                    denom={"accounts": 100, "storages": 1000, "block": 24_869_999})
    assert row["anchor_block"] == 24_870_000
    assert row["slot_W"] == 10 and row["acct_RW_union"] == 4
    assert row["conc_slot_top1_W"] == 0.5
    assert row["upd_pct_warm"] == 88.9
    assert row["sfo_first_is_write"] == 9 and row["afo_total_accounts"] == 4
    assert row["res_nonempty_accounts"] == 1
    assert row["denom_storages"] == 1000 and row["denom_block"] == 24_869_999
    assert row["date"].year >= 2022
