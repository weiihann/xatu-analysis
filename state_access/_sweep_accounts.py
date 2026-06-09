"""Persist slot data through W=180 + run account first-op + empty-split sweeps."""
import time
import pandas as pd
from lib.clickhouse import run_query
from state_access.queries_v2 import account_first_op, account_r_empty_split
from state_access.config_v2 import DATA_DIR_V2

DATA_DIR_V2.mkdir(parents=True, exist_ok=True)

slot_rows = [
    {'window_days':   1, 'total_slots':   2_585_444, 'first_is_write':   1_506_229, 'first_is_zero_read':    723_197, 'first_is_nonzero_read':    356_018},
    {'window_days':   7, 'total_slots':  15_429_499, 'first_is_write':   9_680_702, 'first_is_zero_read':  4_469_822, 'first_is_nonzero_read':  1_278_975},
    {'window_days':  14, 'total_slots':  32_091_770, 'first_is_write':  21_682_778, 'first_is_zero_read':  8_233_692, 'first_is_nonzero_read':  2_175_300},
    {'window_days':  30, 'total_slots':  64_952_555, 'first_is_write':  44_517_259, 'first_is_zero_read': 16_826_711, 'first_is_nonzero_read':  3_608_585},
    {'window_days':  60, 'total_slots': 125_907_892, 'first_is_write':  87_784_210, 'first_is_zero_read': 33_157_193, 'first_is_nonzero_read':  4_966_489},
    {'window_days':  90, 'total_slots': 174_747_166, 'first_is_write': 121_318_159, 'first_is_zero_read': 47_215_667, 'first_is_nonzero_read':  6_213_340},
    {'window_days': 180, 'total_slots': 332_244_146, 'first_is_write': 236_045_977, 'first_is_zero_read': 87_113_587, 'first_is_nonzero_read':  9_084_582},
]
pd.DataFrame(slot_rows).to_parquet(DATA_DIR_V2 / 'slot_first_op.parquet', index=False)
print(f"Persisted slot_first_op.parquet ({len(slot_rows)} windows; W=365 skipped — query too slow)")

WINDOWS = [1, 7, 14, 30, 60, 90, 180]
ANCHOR = 24_870_000

print("\n=== account_first_op ===")
acc_rows = []
for w in WINDOWS:
    t0 = time.time()
    df = run_query(account_first_op(ANCHOR, w), profile='primary',
                   settings={'max_execution_time': 3600})
    r = df.iloc[0]
    elapsed = time.time() - t0
    acc_rows.append({
        'window_days': w,
        'total_accounts': int(r['total_accounts']),
        'first_is_write': int(r['first_is_write']),
        'first_is_nonzero_read': int(r['first_is_nonzero_read']),
        'first_is_zero_read': int(r['first_is_zero_read']),
        'first_is_appearance_read': int(r['first_is_appearance_read']),
    })
    pd.DataFrame(acc_rows).to_parquet(DATA_DIR_V2 / 'account_first_op.parquet', index=False)
    last = acc_rows[-1]
    print(f"  W={w:>3}d {elapsed:>6.0f}s; total={last['total_accounts']:>11,}  "
          f"W={last['first_is_write']:>11,}  Rnz={last['first_is_nonzero_read']:>9,}  "
          f"Rz={last['first_is_zero_read']:>8,}  Rapp={last['first_is_appearance_read']:>8,}", flush=True)

print("\n=== account_r_empty_split ===")
es_rows = []
for w in WINDOWS:
    t0 = time.time()
    df = run_query(account_r_empty_split(ANCHOR, w), profile='primary',
                   settings={'max_execution_time': 3600})
    r = df.iloc[0]
    elapsed = time.time() - t0
    es_rows.append({
        'window_days': w,
        'total_r': int(r['total_r']),
        'empty_accounts': int(r['empty_accounts']),
        'nonempty_accounts': int(r['nonempty_accounts']),
        'unknown_accounts': int(r['unknown_accounts']),
    })
    pd.DataFrame(es_rows).to_parquet(DATA_DIR_V2 / 'account_r_empty_split.parquet', index=False)
    last = es_rows[-1]
    print(f"  W={w:>3}d {elapsed:>6.0f}s; R={last['total_r']:>10,}  "
          f"empty={last['empty_accounts']:>9,}  nonempty={last['nonempty_accounts']:>10,}  "
          f"unknown={last['unknown_accounts']:>9,}", flush=True)

print("\nDone.")
