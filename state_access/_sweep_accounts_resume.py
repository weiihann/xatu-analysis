"""Resume account first_op (W=90, 180) and run account_r_empty_split (W=1..180)."""
import time
import pandas as pd
from lib.clickhouse import run_query
from state_access.queries_v2 import account_first_op, account_r_empty_split
from state_access.config_v2 import DATA_DIR_V2

ANCHOR = 24_870_000

# ---- account_first_op: extend with W=90, 180 ----
p = DATA_DIR_V2 / "account_first_op.parquet"
done = pd.read_parquet(p) if p.exists() else pd.DataFrame()
done_w = set(int(w) for w in done["window_days"]) if len(done) else set()
todo = [w for w in [90, 180] if w not in done_w]
print(f"=== account_first_op (resume): todo={todo} ===")
acc_rows = done.to_dict("records") if len(done) else []
for w in todo:
    t0 = time.time()
    df = run_query(account_first_op(ANCHOR, w), profile="primary",
                   settings={"max_execution_time": 3600})
    r = df.iloc[0]
    elapsed = time.time() - t0
    acc_rows.append({
        "window_days": w,
        "total_accounts": int(r["total_accounts"]),
        "first_is_write": int(r["first_is_write"]),
        "first_is_nonzero_read": int(r["first_is_nonzero_read"]),
        "first_is_zero_read": int(r["first_is_zero_read"]),
        "first_is_appearance_read": int(r["first_is_appearance_read"]),
    })
    pd.DataFrame(acc_rows).to_parquet(p, index=False)
    last = acc_rows[-1]
    print(f"  W={w:>3}d {elapsed:>6.0f}s; total={last['total_accounts']:>11,}  "
          f"W={last['first_is_write']:>11,}  Rnz={last['first_is_nonzero_read']:>9,}  "
          f"Rz={last['first_is_zero_read']:>8,}  Rapp={last['first_is_appearance_read']:>8,}",
          flush=True)

# ---- account_r_empty_split: all W=1..180 ----
p_es = DATA_DIR_V2 / "account_r_empty_split.parquet"
done_es = pd.read_parquet(p_es) if p_es.exists() else pd.DataFrame()
done_es_w = set(int(w) for w in done_es["window_days"]) if len(done_es) else set()
todo_es = [w for w in [1, 7, 14, 30, 60, 90, 180] if w not in done_es_w]
print(f"\n=== account_r_empty_split: todo={todo_es} ===")
es_rows = done_es.to_dict("records") if len(done_es) else []
for w in todo_es:
    t0 = time.time()
    df = run_query(account_r_empty_split(ANCHOR, w), profile="primary",
                   settings={"max_execution_time": 3600})
    r = df.iloc[0]
    elapsed = time.time() - t0
    es_rows.append({
        "window_days": w,
        "total_r": int(r["total_r"]),
        "empty_accounts": int(r["empty_accounts"]),
        "nonempty_accounts": int(r["nonempty_accounts"]),
        "unknown_accounts": int(r["unknown_accounts"]),
    })
    pd.DataFrame(es_rows).to_parquet(p_es, index=False)
    last = es_rows[-1]
    print(f"  W={w:>3}d {elapsed:>6.0f}s; R={last['total_r']:>10,}  "
          f"empty={last['empty_accounts']:>9,}  nonempty={last['nonempty_accounts']:>10,}  "
          f"unknown={last['unknown_accounts']:>9,}", flush=True)

print("\nDone.")
