import pandas as pd

from state_access.v1 import collect_history as ch


def test_remaining_anchors_excludes_done():
    existing = pd.DataFrame({"anchor_block": [100, 300]})
    assert ch.remaining_anchors([100, 200, 300, 400], existing) == [200, 400]


def test_remaining_anchors_handles_no_checkpoint():
    assert ch.remaining_anchors([100, 200], None) == [100, 200]


def test_build_row_derives_cold_and_concentration():
    state = {"unique_accounts": 10, "unique_storage_slots": 20}
    totals = {"accounts": 1000, "storages": 2000}
    # acct_pct/stor_pct/updt_pct are warm percentages of today's writes.
    row = ch.build_row(anchor=15_537_394, w=90, state=state,
                       acct_pct=90.0, stor_pct=70.0, updt_pct=80.0, totals=totals)

    assert row["anchor_block"] == 15_537_394
    assert row["window_days"] == 90
    assert row["pct_accounts_cold"] == 99.0   # 100 - 100*10/1000
    assert row["pct_storage_cold"] == 99.0    # 100 - 100*20/2000
    assert row["acct_writes_cold_pct"] == 10.0
    assert row["storage_writes_cold_pct"] == 30.0
    assert row["pct_state_warm"] == 1.0       # 100*20/2000
    assert row["concentration_x"] == 80.0     # 80.0 / 1.0
    assert row["date"].year == 2022
