import pandas as pd

from bal_size import config
from bal_size.collect import chunks
from bal_size.metrics import add_derived


def test_window_is_six_months_of_blocks():
    assert config.END_BLOCK == 24_870_000
    assert config.START_BLOCK == config.END_BLOCK - config.WINDOW_DAYS * config.BLOCKS_PER_DAY
    assert config.WINDOW_DAYS == 180


def test_chunks_cover_window_without_gaps_or_overlap():
    c = chunks()
    assert c[0][0] == config.START_BLOCK
    assert c[-1][1] == config.END_BLOCK
    for (_, hi), (lo_next, _) in zip(c, c[1:]):
        assert lo_next == hi + 1  # contiguous, no gap or overlap


def test_items_is_storage_keys_plus_addresses():
    df = pd.DataFrame({
        "n_addresses": [10], "n_write_slots": [3], "n_write_entries": [4],
        "n_read_only_slots": [5], "n_balance_changes": [2], "n_nonce_changes": [1],
        "n_contracts": [0], "code_bytes": [0],
    })
    out = add_derived(df)
    assert out["storage_keys"].iloc[0] == 3 + 5
    assert out["items"].iloc[0] == (3 + 5) + 10


def test_derived_bytes_sum_components_with_overhead():
    df = pd.DataFrame({
        "n_addresses": [1], "n_write_slots": [1], "n_write_entries": [1],
        "n_read_only_slots": [1], "n_balance_changes": [1], "n_nonce_changes": [1],
        "n_contracts": [1], "code_bytes": [100],
    })
    out = add_derived(df).iloc[0]
    raw = (
        config.ADDRESS_BYTES
        + config.STORAGE_KEY_BYTES + config.STORAGE_CHANGE_BYTES
        + config.STORAGE_KEY_BYTES
        + config.BALANCE_CHANGE_BYTES
        + config.NONCE_CHANGE_BYTES
        + config.CODE_INDEX_BYTES + 100
    )
    assert out["bytes_raw"] == raw
    assert out["bytes_total"] == raw * (1 + config.RLP_OVERHEAD_FRAC)
