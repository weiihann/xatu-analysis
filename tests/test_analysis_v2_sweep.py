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


def test_verify_rows_catches_stale_denominator():
    bad = _ok_row()
    bad["denom_block"] = bad["anchor_block"] - 60_000
    with pytest.raises(AssertionError, match="stale denominator"):
        verify_rows(pd.DataFrame([bad]))


def test_verify_rows_catches_broken_acct_additivity():
    bad = _ok_row()
    bad["acct_RW_union"] = 99
    with pytest.raises(AssertionError, match="acct additivity"):
        verify_rows(pd.DataFrame([bad]))


def test_verify_against_snapshot_catches_mismatch(tmp_path, monkeypatch):
    import state_access.analysis_v2_sweep as mod
    ref = pd.DataFrame([{"window_days": 30, "W": 11, "R": 4,
                         "total_updates": 9, "warm_updates": 8}])
    ref.to_parquet(tmp_path / "q1_warmth_slot_typed.parquet", index=False)
    ref.to_parquet(tmp_path / "q1_warmth_account.parquet", index=False)
    ref.to_parquet(tmp_path / "slot_update_coverage.parquet", index=False)
    monkeypatch.setattr(mod, "DATA_DIR_V2", tmp_path)
    row = _ok_row()  # slot_W=10 != ref W=11
    with pytest.raises(AssertionError, match="snapshot mismatch"):
        mod.verify_against_snapshot(pd.DataFrame([row]))


def test_verify_against_snapshot_tolerates_small_upd_drift(tmp_path, monkeypatch):
    import state_access.analysis_v2_sweep as mod
    ref_typed = pd.DataFrame([{"window_days": 30, "W": 10, "R": 4}])
    ref_acct = pd.DataFrame([{"window_days": 30, "W": 3}])
    ref_upd = pd.DataFrame([{"window_days": 30, "total_updates": 9_000_000, "warm_updates": 8}])
    ref_typed.to_parquet(tmp_path / "q1_warmth_slot_typed.parquet", index=False)
    ref_acct.to_parquet(tmp_path / "q1_warmth_account.parquet", index=False)
    ref_upd.to_parquet(tmp_path / "slot_update_coverage.parquet", index=False)
    monkeypatch.setattr(mod, "DATA_DIR_V2", tmp_path)
    row = _ok_row()
    row["slot_W"] = 10
    row["slot_R"] = 4
    row["acct_W"] = 3
    row["upd_total_updates"] = 9_005_000  # +0.055% drift, within 1%
    mod.verify_against_snapshot(pd.DataFrame([row]))  # must NOT raise


def test_verify_against_snapshot_rejects_large_upd_drift(tmp_path, monkeypatch):
    import state_access.analysis_v2_sweep as mod
    ref_typed = pd.DataFrame([{"window_days": 30, "W": 10, "R": 4}])
    ref_acct = pd.DataFrame([{"window_days": 30, "W": 3}])
    ref_upd = pd.DataFrame([{"window_days": 30, "total_updates": 9_000_000, "warm_updates": 8}])
    ref_typed.to_parquet(tmp_path / "q1_warmth_slot_typed.parquet", index=False)
    ref_acct.to_parquet(tmp_path / "q1_warmth_account.parquet", index=False)
    ref_upd.to_parquet(tmp_path / "slot_update_coverage.parquet", index=False)
    monkeypatch.setattr(mod, "DATA_DIR_V2", tmp_path)
    row = _ok_row()
    row["slot_W"] = 10
    row["slot_R"] = 4
    row["acct_W"] = 3
    row["upd_total_updates"] = 9_500_000  # +5.6% drift, exceeds 1%
    with pytest.raises(AssertionError, match="snapshot mismatch"):
        mod.verify_against_snapshot(pd.DataFrame([row]))
