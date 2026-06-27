"""Fill every remaining first-op / empty-split window after the cluster came back.

Idempotent: each sweep loads its existing parquet and only runs the missing windows.
Heavy queries (account first-op at large W, the empty-split LEFT JOINs) get GROUP BY
spill-to-disk settings so they don't OOM the cluster again.
"""
import time
import pandas as pd
from lib.clickhouse import run_query
from state_access.v2.queries import (
    slot_first_op, account_first_op, account_r_empty_split,
)
from state_access.v2.config import DATA_DIR_V2

ANCHOR = 24_870_000
ALL_W = [1, 7, 14, 30, 60, 90, 180, 365]

# Spill GROUP BY / JOIN state to disk instead of OOMing the cluster.
HEAVY = {
    "max_execution_time": 7200,
    "max_bytes_before_external_group_by": 20_000_000_000,
    "max_bytes_before_external_sort": 20_000_000_000,
    "join_algorithm": "grace_hash",
    "max_bytes_in_join": 20_000_000_000,
}


def _resume(name: str, builder, cols: list[str], windows=ALL_W, settings=HEAVY):
    p = DATA_DIR_V2 / f"{name}.parquet"
    done = pd.read_parquet(p) if p.exists() else pd.DataFrame()
    done_w = set(int(w) for w in done["window_days"]) if len(done) else set()
    todo = [w for w in windows if w not in done_w]
    print(f"\n=== {name}: have {sorted(done_w)}, todo {todo} ===", flush=True)
    rows = done.to_dict("records") if len(done) else []
    for w in todo:
        t0 = time.time()
        df = run_query(builder(ANCHOR, w), profile="primary", settings=settings)
        r = df.iloc[0]
        rows.append({"window_days": w, **{c: int(r[c]) for c in cols}})
        pd.DataFrame(rows).to_parquet(p, index=False)
        print(f"  W={w:>3}d {time.time()-t0:>6.0f}s  "
              + "  ".join(f"{c}={int(r[c]):,}" for c in cols), flush=True)


# 1) slot first-op — only W=365 left
_resume("slot_first_op", slot_first_op,
        ["total_slots", "first_is_write", "first_is_zero_read", "first_is_nonzero_read"])

# 2) account first-op — W=90, 180, 365 left
_resume("account_first_op", account_first_op,
        ["total_accounts", "first_is_write", "first_is_nonzero_read",
         "first_is_zero_read", "first_is_appearance_read"])

# 3) R-only empty/non-empty split — all windows
_resume("account_r_empty_split", account_r_empty_split,
        ["total_r", "empty_accounts", "nonempty_accounts", "unknown_accounts"])

print("\nAll resume sweeps complete.")
