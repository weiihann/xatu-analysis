import pandas as pd
import pytest

from state_access.analysis_v2_sweep import verify_rows


def _ok_row():
    return {
        "anchor_block": 24_870_000, "window_days": 30,
        "slot_W": 10, "slot_R": 4, "slot_RW_union": 14,
        "slot_W_only_create": 5, "slot_W_only_update": 2, "slot_W_only_delete": 1,
        "slot_W_mixed": 2,
        "slot_mixed_cu": 1, "slot_mixed_cd1": 1, "slot_mixed_cdm": 0,
        "slot_mixed_ud": 0, "slot_mixed_cud1": 0, "slot_mixed_cudm": 0,
        "slot_R_only_zero": 3, "slot_R_only_nonzero": 1, "slot_R_mixed": 0,
        "acct_W": 3, "acct_R": 1, "acct_RW_union": 4,
        "upd_total_updates": 9, "upd_warm_updates": 8, "upd_cold_updates": 1,
        "sfo_total_slots": 14, "sfo_first_is_write": 9, "sfo_first_is_zero_read": 3,
        "sfo_first_is_nonzero_read": 2,
        "denom_block": 24_869_000,
    }


def test_verify_rows_passes_consistent_data():
    verify_rows(pd.DataFrame([_ok_row()]))  # must not raise


def test_verify_rows_catches_broken_additivity():
    bad = _ok_row()
    bad["slot_RW_union"] = 99
    with pytest.raises(AssertionError, match="additivity"):
        verify_rows(pd.DataFrame([bad]))


def test_verify_rows_catches_broken_partition():
    bad = _ok_row()
    bad["slot_W_mixed"] = 7
    with pytest.raises(AssertionError, match="partition"):
        verify_rows(pd.DataFrame([bad]))
