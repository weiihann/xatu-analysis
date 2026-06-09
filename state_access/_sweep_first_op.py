"""Sweep slot_first_op + account_first_op + account_r_empty_split across all 8 W values."""
import time
import pandas as pd
from lib.clickhouse import run_query
from state_access.queries_v2 import slot_first_op, account_first_op, account_r_empty_split
from state_access.config_v2 import DATA_DIR_V2

ANCHOR = 24_870_000
WINDOWS = [1, 7, 14, 30, 60, 90, 180, 365]

DATA_DIR_V2.mkdir(parents=True, exist_ok=True)

# ----- 1) slot_first_op sweep -----
print("=== slot_first_op ===")
rows = []
for w in WINDOWS:
    t0 = time.time()
    df = run_query(slot_first_op(ANCHOR, w), profile='primary', settings={'max_execution_time': 5400})
    r = df.iloc[0]
    elapsed = time.time() - t0
    rows.append({
        'window_days': w,
        'total_slots': int(r['total_slots']),
        'first_is_write': int(r['first_is_write']),
        'first_is_zero_read': int(r['first_is_zero_read']),
        'first_is_nonzero_read': int(r['first_is_nonzero_read']),
    })
    print(f"  W={w:>3}d in {elapsed:>6.0f}s; total={rows[-1]['total_slots']:>12,}  "
          f"W={rows[-1]['first_is_write']:>12,}  Rz={rows[-1]['first_is_zero_read']:>11,}  "
          f"Rnz={rows[-1]['first_is_nonzero_read']:>10,}")
pd.DataFrame(rows).to_parquet(DATA_DIR_V2 / 'slot_first_op.parquet', index=False)

# ----- 2) account_first_op sweep -----
print("\n=== account_first_op ===")
rows = []
for w in WINDOWS:
    t0 = time.time()
    df = run_query(account_first_op(ANCHOR, w), profile='primary', settings={'max_execution_time': 5400})
    r = df.iloc[0]
    elapsed = time.time() - t0
    rows.append({
        'window_days': w,
        'total_accounts': int(r['total_accounts']),
        'first_is_write': int(r['first_is_write']),
        'first_is_nonzero_read': int(r['first_is_nonzero_read']),
        'first_is_zero_read': int(r['first_is_zero_read']),
        'first_is_appearance_read': int(r['first_is_appearance_read']),
    })
    print(f"  W={w:>3}d in {elapsed:>6.0f}s; total={rows[-1]['total_accounts']:>12,}  "
          f"W={rows[-1]['first_is_write']:>12,}  Rnz={rows[-1]['first_is_nonzero_read']:>10,}  "
          f"Rz={rows[-1]['first_is_zero_read']:>9,}  Rapp={rows[-1]['first_is_appearance_read']:>9,}")
pd.DataFrame(rows).to_parquet(DATA_DIR_V2 / 'account_first_op.parquet', index=False)

# ----- 3) account_r_empty_split sweep -----
print("\n=== account_r_empty_split ===")
rows = []
for w in WINDOWS:
    t0 = time.time()
    df = run_query(account_r_empty_split(ANCHOR, w), profile='primary', settings={'max_execution_time': 5400})
    r = df.iloc[0]
    elapsed = time.time() - t0
    rows.append({
        'window_days': w,
        'total_r': int(r['total_r']),
        'empty_accounts': int(r['empty_accounts']),
        'nonempty_accounts': int(r['nonempty_accounts']),
        'unknown_accounts': int(r['unknown_accounts']),
    })
    print(f"  W={w:>3}d in {elapsed:>6.0f}s; R={rows[-1]['total_r']:>11,}  "
          f"empty={rows[-1]['empty_accounts']:>10,}  "
          f"nonempty={rows[-1]['nonempty_accounts']:>11,}  "
          f"unknown={rows[-1]['unknown_accounts']:>10,}")
pd.DataFrame(rows).to_parquet(DATA_DIR_V2 / 'account_r_empty_split.parquet', index=False)

print("\nAll three sweeps persisted to", DATA_DIR_V2)
