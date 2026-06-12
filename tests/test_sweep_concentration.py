import math

import pandas as pd
import pytest

from state_access.config_v2 import DATA_DIR_V2
from state_access.sweep_concentration import concentration_shares


@pytest.mark.parametrize("obj", ["slot", "account"])
def test_matches_committed_q3(obj):
    hist = pd.read_parquet(DATA_DIR_V2 / f"{obj}_histogram.parquet")
    ref = pd.read_parquet(DATA_DIR_V2 / f"q3_concentration_{obj}.parquet")
    for t in (30, 365):
        shares = concentration_shares(hist[hist.window_days == t])
        for at in ("W", "R", "RW_union"):
            row = ref[(ref.window_days == t) & (ref.access_type == at)].iloc[0]
            assert shares[f"top1_{at}"] == pytest.approx(
                row.top_1pct_share, abs=3e-3
            )
            assert shares[f"top10_{at}"] == pytest.approx(
                row.top_10pct_share, abs=3e-3
            )


def test_empty_set_gives_nan():
    empty = pd.DataFrame({"slice": [], "n_w": [], "n_r": [], "n_keys": []})
    shares = concentration_shares(empty)
    assert math.isnan(shares["top1_W"])
