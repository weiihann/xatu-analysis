# Post-Merge Historical Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay every windowed REPORT_v2 section (§3–§8) at weekly anchors across the
post-merge range, at T ∈ {30, 90, 180, 365} days, producing time-series parquets,
charts, and a new "Part III — Historical sweep" report section.

**Architecture:** Per (anchor, T), new in-SQL scalar-summary queries (same per-key CTEs
as the proven v2 builders, classification pushed into `countIf`) return one row each;
§6 concentration is reduced in-driver from the existing histogram queries; §7/§8 reuse
existing scalar builders. A resumable driver (v1 `collect_history.py` pattern)
checkpoints one wide row per anchor into `data/v2/sweep_w{T}.parquet`. A separate
analysis module verifies (newest anchor must reproduce the committed snapshot) and
renders time-series charts with fork annotations.

**Tech Stack:** Python 3.13 / uv, pandas, plotly+kaleido (process-isolated rendering —
kaleido deadlocks after a few in-process renders), ClickHouse via `lib.clickhouse`,
pytest.

**Spec:** `docs/superpowers/specs/2026-06-12-historical-sweep-design.md`

---

### Task 1: Sweep grid config (`anchors_v2`)

**Files:**
- Modify: `state_access/config_v2.py`
- Test: `tests/test_config_v2_sweep.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config_v2_sweep.py
from state_access import config_v2 as cfg
from state_access.history_config import MERGE_BLOCK


def test_sweep_windows_match_v1_grid():
    assert cfg.SWEEP_WINDOWS == [30, 90, 180, 365]


def test_anchors_v2_newest_is_snapshot_anchor():
    for t in cfg.SWEEP_WINDOWS:
        assert cfg.anchors_v2(t)[-1] == cfg.ANCHOR_BLOCK_V2


def test_anchors_v2_weekly_ascending():
    a = cfg.anchors_v2(30)
    assert a == sorted(a)
    assert {b - x for x, b in zip(a, a[1:])} == {cfg.SWEEP_STEP} == {50_400}


def test_anchors_v2_floor_keeps_lookback_post_merge():
    for t in cfg.SWEEP_WINDOWS:
        a = cfg.anchors_v2(t)
        floor = MERGE_BLOCK + t * 7_200
        assert a[0] >= floor            # whole lookback stays post-merge
        assert a[0] - cfg.SWEEP_STEP < floor  # can't fit another anchor below


def test_anchors_v2_counts_are_plausible():
    counts = {t: len(cfg.anchors_v2(t)) for t in cfg.SWEEP_WINDOWS}
    assert 175 <= counts[30] <= 185
    assert 130 <= counts[365] <= 140
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_v2_sweep.py -q`
Expected: FAIL with `AttributeError: ... 'SWEEP_WINDOWS'`

- [ ] **Step 3: Implement in `config_v2.py`** (append after `MERGE_BLOCK`; note
`config_v2.MERGE_BLOCK` already exists and equals `history_config.MERGE_BLOCK`)

```python
# Historical sweep grid: weekly anchors descending from the snapshot anchor, floored so
# each window's whole lookback stays post-merge (local read coverage starts at the
# merge; 7,200 blocks/day holds from there).
SWEEP_WINDOWS = [30, 90, 180, 365]
SWEEP_STEP = 50_400  # 7 days * 7,200 blocks/day


def anchors_v2(window_days: int) -> list[int]:
    """Weekly anchor blocks for `window_days`, ascending; last == ANCHOR_BLOCK_V2."""
    floor = MERGE_BLOCK + window_days * 7_200
    out: list[int] = []
    block = ANCHOR_BLOCK_V2
    while block >= floor:
        out.append(block)
        block -= SWEEP_STEP
    return sorted(out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config_v2_sweep.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add state_access/config_v2.py tests/test_config_v2_sweep.py
git commit -m "state_access v2: sweep grid config (weekly anchors, per-window floors)"
```

---

### Task 2: Scalar summary SQL builders

**Files:**
- Modify: `state_access/queries_v2.py` (append after `account_appearance_read_totals`)

- [ ] **Step 1: Add `slot_sweep_summary`**

Inner CTE is identical to `slot_typed_histogram`'s; the outer SELECT classifies per-key
aggregates into one row. `_ZERO` and `NETWORK` already exist in the module.

```python
def slot_sweep_summary(bn_now: int, days: int) -> str:
    """One-row scalar summary of the slot typed view for a (anchor, window) cell.

    Same per-key GROUP BY as `slot_typed_histogram`, but the classification that
    `analysis_v2.q1_warmth_slot_typed` / `_classify_mixed` do in pandas happens in SQL,
    so the sweep stores ~20 counts instead of a 100k-row histogram per anchor.
    """
    bn_lo, bn_hi = _window(bn_now, days)
    return f"""
WITH per_key AS (
    SELECT
        h,
        sum(is_w_create) AS c,
        sum(is_w_update) AS u,
        sum(is_w_delete) AS d,
        sum(is_r_zero)   AS rz,
        sum(is_r_nonzero) AS rn
    FROM (
        SELECT
            cityHash64(address, slot) AS h,
            toUInt8(from_value =  '{_ZERO}' AND to_value != '{_ZERO}') AS is_w_create,
            toUInt8(from_value != '{_ZERO}' AND to_value != '{_ZERO}') AS is_w_update,
            toUInt8(from_value != '{_ZERO}' AND to_value =  '{_ZERO}') AS is_w_delete,
            toUInt8(0) AS is_r_zero,
            toUInt8(0) AS is_r_nonzero
        FROM canonical_execution_storage_diffs
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT
            cityHash64(contract_address, slot) AS h,
            toUInt8(0), toUInt8(0), toUInt8(0),
            toUInt8(value =  '{_ZERO}') AS is_r_zero,
            toUInt8(value != '{_ZERO}') AS is_r_nonzero
        FROM canonical_execution_storage_reads
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
    )
    GROUP BY h
)
SELECT
    countIf(c + u + d > 0)                              AS W,
    countIf(c + u + d = 0 AND rz + rn > 0)              AS R,
    count()                                             AS RW_union,
    countIf(c > 0 AND u = 0 AND d = 0)                  AS W_only_create,
    countIf(u > 0 AND c = 0 AND d = 0)                  AS W_only_update,
    countIf(d > 0 AND c = 0 AND u = 0)                  AS W_only_delete,
    countIf(toUInt8(c > 0) + toUInt8(u > 0) + toUInt8(d > 0) >= 2) AS W_mixed,
    countIf(c > 0 AND u > 0 AND d = 0)                  AS mixed_cu,
    countIf(c > 0 AND d > 0 AND u = 0 AND c = 1)        AS mixed_cd1,
    countIf(c > 0 AND d > 0 AND u = 0 AND c >= 2)       AS mixed_cdm,
    countIf(u > 0 AND d > 0 AND c = 0)                  AS mixed_ud,
    countIf(c > 0 AND u > 0 AND d > 0 AND c = 1)        AS mixed_cud1,
    countIf(c > 0 AND u > 0 AND d > 0 AND c >= 2)       AS mixed_cudm,
    countIf(c > 0)                                      AS W_any_create,
    countIf(u > 0)                                      AS W_any_update,
    countIf(d > 0)                                      AS W_any_delete,
    countIf(c + u + d = 0 AND rz > 0 AND rn = 0)        AS R_only_zero,
    countIf(c + u + d = 0 AND rn > 0 AND rz = 0)        AS R_only_nonzero,
    countIf(c + u + d = 0 AND rz > 0 AND rn > 0)        AS R_mixed
FROM per_key
"""
```

- [ ] **Step 2: Add `account_sweep_summary`**

Inner CTE identical to `account_histogram`'s (reuses `_RELATIONSHIP_LIST`):

```python
def account_sweep_summary(bn_now: int, days: int) -> str:
    """One-row W / R / R∪W account summary (same sources as `account_histogram`)."""
    bn_lo, bn_hi = _window(bn_now, days)
    return f"""
WITH per_key AS (
    SELECT h, sum(is_w) AS n_w, sum(is_r) AS n_r
    FROM (
        SELECT cityHash64(address) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_balance_diffs
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), 1, 0
        FROM canonical_execution_nonce_diffs
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(contract_address), 1, 0
        FROM canonical_execution_contracts
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), 0, 1
        FROM canonical_execution_balance_reads
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), 0, 1
        FROM canonical_execution_nonce_reads
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), 0, 1
        FROM canonical_execution_address_appearances
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
          AND relationship IN ({_RELATIONSHIP_LIST})
    )
    GROUP BY h
)
SELECT
    countIf(n_w > 0)              AS W,
    countIf(n_w = 0 AND n_r > 0)  AS R,
    count()                       AS RW_union
FROM per_key
"""
```

- [ ] **Step 3: Live equivalence check against the committed snapshot**

The strongest test for SQL correctness: at the snapshot anchor the summaries must
reproduce the committed parquets exactly. Run:

```bash
uv run python -c "
import pandas as pd
from lib.clickhouse import run_query
from state_access import queries_v2 as q
from state_access.config_v2 import ANCHOR_BLOCK_V2, DATA_DIR_V2

S = {'max_execution_time': 7200}
s = run_query(q.slot_sweep_summary(ANCHOR_BLOCK_V2, 30), settings=S).iloc[0]
a = run_query(q.account_sweep_summary(ANCHOR_BLOCK_V2, 30), settings=S).iloc[0]

t = pd.read_parquet(DATA_DIR_V2 / 'q1_warmth_slot_typed.parquet')
t = t[t.window_days == 30].iloc[0]
for sql_col, ref_col in [('W','W'), ('R','R'), ('RW_union','RW_union'),
        ('W_only_create','W_only_create'), ('W_only_update','W_only_update'),
        ('W_only_delete','W_only_delete'), ('W_mixed','W_mixed'),
        ('W_any_create','W_any_create'), ('W_any_update','W_any_update'),
        ('W_any_delete','W_any_delete'), ('R_only_zero','R_only_zero'),
        ('R_only_nonzero','R_only_nonzero'), ('R_mixed','R_mixed')]:
    assert int(s[sql_col]) == int(t[ref_col]), (sql_col, int(s[sql_col]), int(t[ref_col]))

m = pd.read_parquet(DATA_DIR_V2 / 'q1_warmth_slot_mixed_decomp.parquet')
m = m[m.window_days == 30].set_index('combo')['n_keys']
combo_map = {'mixed_cu':'C+U', 'mixed_cd1':'C+D (1-cycle)', 'mixed_cdm':'C+D (multi-cycle)',
             'mixed_ud':'U+D', 'mixed_cud1':'C+U+D (1-cycle)', 'mixed_cudm':'C+U+D (multi-cycle)'}
for sql_col, combo in combo_map.items():
    assert int(s[sql_col]) == int(m[combo]), (sql_col, int(s[sql_col]), int(m[combo]))

qa = pd.read_parquet(DATA_DIR_V2 / 'q1_warmth_account.parquet')
qa = qa[qa.window_days == 30].iloc[0]
for col in ('W', 'R', 'RW_union'):
    assert int(a[col]) == int(qa[col]), (col, int(a[col]), int(qa[col]))
print('summary SQL == committed snapshot at T=30: OK')
"
```

Expected: `summary SQL == committed snapshot at T=30: OK`

- [ ] **Step 4: Lint and commit**

```bash
uvx ruff check state_access/queries_v2.py
git add state_access/queries_v2.py
git commit -m "state_access v2: in-SQL scalar sweep summaries (slot typed + account W/R)"
```

---

### Task 3: Vectorized concentration reduction

**Files:**
- Create: `state_access/sweep_concentration.py`
- Test: `tests/test_sweep_concentration.py` (create)

- [ ] **Step 1: Write the failing test** — fully offline, against the committed
histogram and the committed q3 results. Tolerance 0.003 share units: tie ordering at
the top-N cutoff differs between pandas quicksort (analysis_v2) and stable argsort.

```python
# tests/test_sweep_concentration.py
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
            assert shares[f"top1_{at}"] == pytest.approx(row.top_1pct_share, abs=3e-3)
            assert shares[f"top10_{at}"] == pytest.approx(row.top_10pct_share, abs=3e-3)


def test_empty_set_gives_nan():
    import math
    empty = pd.DataFrame({"slice": [], "n_w": [], "n_r": [], "n_keys": []})
    shares = concentration_shares(empty)
    assert math.isnan(shares["top1_W"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sweep_concentration.py -q`
Expected: FAIL with `ModuleNotFoundError: ... sweep_concentration`

- [ ] **Step 3: Implement `state_access/sweep_concentration.py`**

```python
"""Vectorized top-N concentration reduction over a (slice, n_w, n_r, n_keys) histogram.

Same semantics as `analysis_v2.q3_concentration` (which uses row-wise `.apply` and takes
minutes); this runs in milliseconds so the sweep driver can reduce each fetched
histogram to 6 scalars without persisting it.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

_FRACTIONS = {"top1": 0.01, "top10": 0.10}


def _set_view(hist: pd.DataFrame, access_type: str) -> tuple[pd.DataFrame, pd.Series]:
    if access_type == "W":
        sub = hist[hist["slice"].isin(["w_only", "rw"])]
        return sub, sub["n_w"]
    if access_type == "R":
        sub = hist[hist["slice"] == "r_only"]
        return sub, sub["n_r"]
    return hist, hist["n_w"] + hist["n_r"]


def concentration_shares(hist: pd.DataFrame) -> dict[str, float]:
    """Top-1% / top-10% share of accesses for the W / R / R∪W sets, in one dict."""
    out: dict[str, float] = {}
    for access_type in ("W", "R", "RW_union"):
        sub, acc = _set_view(hist, access_type)
        if sub.empty:
            for name in _FRACTIONS:
                out[f"{name}_{access_type}"] = float("nan")
            continue
        order = np.argsort(-acc.to_numpy(), kind="stable")
        keys = sub["n_keys"].to_numpy()[order]
        events = (acc.to_numpy() * sub["n_keys"].to_numpy())[order]
        cum_keys = keys.cumsum()
        cum_events = events.cumsum()
        n_objects = int(cum_keys[-1])
        total = int(cum_events[-1])
        for name, frac in _FRACTIONS.items():
            target = math.ceil(frac * n_objects)
            idx = int(np.searchsorted(cum_keys, target, side="left"))
            out[f"{name}_{access_type}"] = float(cum_events[idx] / total) if total else 0.0
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sweep_concentration.py -q`
Expected: 3 passed (runs in seconds, fully offline)

- [ ] **Step 5: Commit**

```bash
git add state_access/sweep_concentration.py tests/test_sweep_concentration.py
git commit -m "state_access v2: vectorized concentration reduction for the sweep"
```

---

### Task 4: Sweep driver

**Files:**
- Create: `state_access/collect_v2_sweep.py`
- Test: `tests/test_collect_v2_sweep.py` (create)

- [ ] **Step 1: Write the failing tests** (pure logic only — DB paths are covered by
the smoke run in Step 4)

```python
# tests/test_collect_v2_sweep.py
import pandas as pd

from state_access.collect_v2_sweep import build_row, remaining_anchors


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_collect_v2_sweep.py -q`
Expected: FAIL with `ModuleNotFoundError: ... collect_v2_sweep`

- [ ] **Step 3: Implement `state_access/collect_v2_sweep.py`**

```python
"""Resumable post-merge historical sweep of the v2 windowed analyses.

One wide row per (anchor, window): scalar sweep summaries (slot typed + account W/R),
concentration shares reduced in-process from the existing histograms, §7 update
coverage, §8 first-op and empty-split, and per-anchor live-state denominators from the
LOCAL execution_state_size (the ethpandaops copy is TTL'd to a recent band).

Anchors run newest-first so the snapshot anchor (24,870,000) lands immediately and can
be verified against the committed v2 parquets. Checkpoint after every anchor; re-runs
skip anchors already present.

    uv run python -m state_access.collect_v2_sweep [T ...]   # default: 30 90 180 365
    SWEEP_LIMIT=2 uv run python -m state_access.collect_v2_sweep 30   # smoke run
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping

import pandas as pd
from clickhouse_connect.driver.exceptions import OperationalError

from lib.clickhouse import run_query
from state_access import queries_v2 as q
from state_access.config_v2 import DATA_DIR_V2, SWEEP_STEP, SWEEP_WINDOWS, anchors_v2
from state_access.history_config import block_to_date
from state_access.sweep_concentration import concentration_shares

MAX_ATTEMPTS = 6
RETRY_BASE_DELAY = 20
HEAVY = {
    "max_execution_time": 7200,
    "max_bytes_before_external_group_by": 20_000_000_000,
    "max_bytes_before_external_sort": 20_000_000_000,
}


def parquet_for(window_days: int):
    return DATA_DIR_V2 / f"sweep_w{window_days}.parquet"


def remaining_anchors(all_anchors: list[int], existing: pd.DataFrame | None) -> list[int]:
    done: set[int] = set()
    if existing is not None and not existing.empty:
        done = {int(b) for b in existing["anchor_block"]}
    return [a for a in all_anchors if a not in done]


def build_row(anchor: int, window_days: int, slot: Mapping, acct: Mapping,
              conc: Mapping, upd: Mapping, sfo: Mapping, afo: Mapping, res: Mapping,
              denom: Mapping) -> dict:
    """Flatten the per-anchor query outputs into one wide row with stable prefixes."""
    row: dict = {
        "anchor_block": anchor,
        "window_days": window_days,
        "date": block_to_date(anchor),
        "denom_accounts": int(denom["accounts"]),
        "denom_storages": int(denom["storages"]),
        "denom_block": int(denom["block"]),
    }
    for prefix, mapping in (("slot", slot), ("acct", acct), ("conc", conc),
                            ("upd", upd), ("sfo", sfo), ("afo", afo), ("res", res)):
        for k, v in dict(mapping).items():
            row[f"{prefix}_{k}"] = float(v) if isinstance(v, float) else int(v)
    return row


def _denominators(anchor: int) -> dict:
    df = run_query(
        f"SELECT block_number, accounts, storages FROM execution_state_size "
        f"WHERE meta_network_name = 'mainnet' AND block_number <= {anchor} "
        f"ORDER BY block_number DESC LIMIT 1",
        settings={"max_execution_time": 600},
    )
    if df.empty:
        raise RuntimeError(f"no execution_state_size row at or before block {anchor:,}")
    block = int(df.iloc[0]["block_number"])
    if anchor - block > SWEEP_STEP:
        raise RuntimeError(
            f"execution_state_size too stale at anchor {anchor:,}: nearest row "
            f"is block {block:,} ({anchor - block:,} blocks earlier)"
        )
    return {"accounts": int(df.iloc[0]["accounts"]),
            "storages": int(df.iloc[0]["storages"]), "block": block}


def _fetch_anchor(anchor: int, window_days: int) -> dict:
    slot = run_query(q.slot_sweep_summary(anchor, window_days), settings=HEAVY).iloc[0]
    acct = run_query(q.account_sweep_summary(anchor, window_days), settings=HEAVY).iloc[0]

    conc: dict = {}
    slot_hist = run_query(q.slot_histogram(anchor, window_days), settings=HEAVY)
    for k, v in concentration_shares(slot_hist).items():
        conc[f"slot_{k}"] = v
    acct_hist = run_query(q.account_histogram(anchor, window_days), settings=HEAVY)
    for k, v in concentration_shares(acct_hist).items():
        conc[f"acct_{k}"] = v

    upd = run_query(q.slot_update_coverage(anchor, window_days), settings=HEAVY).iloc[0]
    sfo = run_query(q.slot_first_op(anchor, window_days), settings=HEAVY).iloc[0]
    afo = run_query(q.account_first_op(anchor, window_days), settings=HEAVY).iloc[0]
    res = run_query(q.account_r_empty_split(anchor, window_days), settings=HEAVY).iloc[0]

    return build_row(anchor, window_days, slot=slot, acct=acct, conc=conc, upd=upd,
                     sfo=sfo, afo=afo, res=res, denom=_denominators(anchor))


def _fetch_with_retry(anchor: int, window_days: int) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _fetch_anchor(anchor, window_days)
        except OperationalError as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY * attempt
            print(f"  block {anchor:,}: attempt {attempt} failed "
                  f"({type(exc).__name__}); retry in {delay}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def collect(window_days: int, limit: int | None = None) -> None:
    parquet = parquet_for(window_days)
    existing = pd.read_parquet(parquet) if parquet.exists() else None
    rows = existing.to_dict("records") if existing is not None else []

    # Newest-first: the snapshot anchor lands first and verifies against committed v2.
    todo = remaining_anchors(sorted(anchors_v2(window_days), reverse=True), existing)
    if limit is not None:
        todo = todo[:limit]
    print(f"T={window_days}d sweep: {len(todo)} anchors to collect "
          f"({len(rows)} done), writing {parquet}", flush=True)

    for i, anchor in enumerate(todo, 1):
        t0 = time.time()
        row = _fetch_with_retry(anchor, window_days)
        rows.append(row)
        pd.DataFrame(rows).sort_values("anchor_block").to_parquet(parquet, index=False)
        print(f"  [{i}/{len(todo)}] block {anchor:,} {row['date']:%Y-%m-%d} "
              f"{time.time()-t0:5.0f}s: slot R∪W={row['slot_RW_union']:,} "
              f"upd warm={row['upd_pct_warm']:.1f}%", flush=True)

    print(f"T={window_days}d done: {len(rows)} anchors in {parquet}\n", flush=True)


def main(windows: list[int]) -> None:
    limit = int(os.environ["SWEEP_LIMIT"]) if "SWEEP_LIMIT" in os.environ else None
    for w in windows:
        collect(w, limit=limit)


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or list(SWEEP_WINDOWS))
```

- [ ] **Step 4: Run pure-logic tests**

Run: `uv run pytest tests/test_collect_v2_sweep.py -q`
Expected: 3 passed

- [ ] **Step 5: Smoke run — 2 newest anchors at T=30**

```bash
SWEEP_LIMIT=2 uv run python -m state_access.collect_v2_sweep 30
```

Expected: two anchor lines (~2–4 min total), first being block 24,870,000;
`data/v2/sweep_w30.parquet` created with 2 rows.

- [ ] **Step 6: Verify the snapshot anchor reproduces committed v2 numbers**

```bash
uv run python -c "
import pandas as pd
from state_access.config_v2 import DATA_DIR_V2

sw = pd.read_parquet(DATA_DIR_V2 / 'sweep_w30.parquet')
row = sw[sw.anchor_block == 24_870_000].iloc[0]

t = pd.read_parquet(DATA_DIR_V2 / 'q1_warmth_slot_typed.parquet')
t = t[t.window_days == 30].iloc[0]
assert int(row.slot_W) == int(t.W) and int(row.slot_R) == int(t.R)
assert int(row.slot_W_mixed) == int(t.W_mixed)

qa = pd.read_parquet(DATA_DIR_V2 / 'q1_warmth_account.parquet')
qa = qa[qa.window_days == 30].iloc[0]
assert int(row.acct_W) == int(qa.W) and int(row.acct_RW_union) == int(qa.RW_union)

uc = pd.read_parquet(DATA_DIR_V2 / 'slot_update_coverage.parquet')
uc = uc[uc.window_days == 30].iloc[0]
assert int(row.upd_total_updates) == int(uc.total_updates)
assert int(row.upd_warm_updates) == int(uc.warm_updates)

fo = pd.read_parquet(DATA_DIR_V2 / 'slot_first_op.parquet')
fo = fo[fo.window_days == 30].iloc[0]
assert int(row.sfo_first_is_nonzero_read) == int(fo.first_is_nonzero_read)

af = pd.read_parquet(DATA_DIR_V2 / 'account_first_op.parquet')
af = af[af.window_days == 30].iloc[0]
assert int(row.afo_first_is_nonzero_read) == int(af.first_is_nonzero_read)

es = pd.read_parquet(DATA_DIR_V2 / 'account_r_empty_split.parquet')
es = es[es.window_days == 30].iloc[0]
assert int(row.res_nonempty_accounts) == int(es.nonempty_accounts)

q3 = pd.read_parquet(DATA_DIR_V2 / 'q3_concentration_slot.parquet')
ref = q3[(q3.window_days == 30) & (q3.access_type == 'R')].iloc[0]
assert abs(row.conc_slot_top1_R - ref.top_1pct_share) < 3e-3
print('sweep snapshot anchor == committed v2: OK')
"
```

Expected: `sweep snapshot anchor == committed v2: OK`

- [ ] **Step 7: Lint and commit** (the 2-row parquet is fine to commit — the full
sweep extends it)

```bash
uvx ruff check state_access/collect_v2_sweep.py
git add state_access/collect_v2_sweep.py tests/test_collect_v2_sweep.py \
        state_access/data/v2/sweep_w30.parquet
git commit -m "state_access v2: resumable historical sweep driver (newest-first, verified vs snapshot)"
```

---

### Task 5: Sweep analysis + charts

**Files:**
- Create: `state_access/analysis_v2_sweep.py`
- Test: `tests/test_analysis_v2_sweep.py` (create)

- [ ] **Step 1: Write the failing tests** (verification logic on synthetic rows)

```python
# tests/test_analysis_v2_sweep.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_analysis_v2_sweep.py -q`
Expected: FAIL with `ModuleNotFoundError: ... analysis_v2_sweep`

- [ ] **Step 3: Implement `state_access/analysis_v2_sweep.py`**

```python
"""Time-series charts + verification for the post-merge historical sweep.

Reads data/v2/sweep_w{T}.parquet (written by collect_v2_sweep), verifies internal
identities and that the newest anchor reproduces the committed snapshot, then renders
the Part III charts. Chart rendering is process-isolated: kaleido deadlocks after a few
write_image calls in one process (observed 2026-06-12), so each figure renders in a
fresh subprocess with a timeout.

    uv run python -m state_access.analysis_v2_sweep
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pandas as pd
import plotly.graph_objs as go

from state_access.config_v2 import ANCHOR_BLOCK_V2, DATA_DIR_V2, SWEEP_WINDOWS
from state_access.history_config import FORKS, block_to_date

WINDOW_COLORS = {30: "#90CAF9", 90: "#42A5F5", 180: "#1976D2", 365: "#0D47A1"}
_MIXED_COMBOS = {
    "slot_mixed_cu": ("C+U", "#1565C0"),
    "slot_mixed_cd1": ("C+D (1-cycle)", "#FFA000"),
    "slot_mixed_cdm": ("C+D (multi-cycle)", "#E65100"),
    "slot_mixed_ud": ("U+D", "#7B1FA2"),
    "slot_mixed_cud1": ("C+U+D (1-cycle)", "#388E3C"),
    "slot_mixed_cudm": ("C+U+D (multi-cycle)", "#1B5E20"),
}


def _render(fig_dict: dict, path: str) -> None:
    go.Figure(fig_dict).write_image(path, scale=2)


def write_image_safe(fig: go.Figure, path: Path, timeout: int = 180) -> None:
    """Render in a fresh subprocess — kaleido deadlocks on repeated in-process renders."""
    proc = mp.get_context("spawn").Process(target=_render, args=(fig.to_dict(), str(path)))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        raise RuntimeError(f"chart render timed out: {path}")
    if proc.exitcode != 0:
        raise RuntimeError(f"chart render failed (exit {proc.exitcode}): {path}")


def load_sweeps() -> pd.DataFrame:
    frames = []
    for t in SWEEP_WINDOWS:
        p = DATA_DIR_V2 / f"sweep_w{t}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        raise RuntimeError("no sweep parquets found — run collect_v2_sweep first")
    return pd.concat(frames, ignore_index=True).sort_values(
        ["window_days", "anchor_block"]).reset_index(drop=True)


def verify_rows(df: pd.DataFrame) -> None:
    """Per-row identities. Raises AssertionError naming the broken invariant."""
    for _, r in df.iterrows():
        where = f"anchor={int(r.anchor_block):,} T={int(r.window_days)}"
        assert r.slot_RW_union == r.slot_W + r.slot_R, f"slot additivity broken at {where}"
        assert r.acct_RW_union == r.acct_W + r.acct_R, f"acct additivity broken at {where}"
        only = r.slot_W_only_create + r.slot_W_only_update + r.slot_W_only_delete
        assert r.slot_W == only + r.slot_W_mixed, f"W partition broken at {where}"
        combos = (r.slot_mixed_cu + r.slot_mixed_cd1 + r.slot_mixed_cdm
                  + r.slot_mixed_ud + r.slot_mixed_cud1 + r.slot_mixed_cudm)
        assert r.slot_W_mixed == combos, f"mixed partition broken at {where}"
        r_parts = r.slot_R_only_zero + r.slot_R_only_nonzero + r.slot_R_mixed
        assert r.slot_R == r_parts, f"R partition broken at {where}"
        assert r.upd_total_updates == r.upd_warm_updates + r.upd_cold_updates, \
            f"update coverage partition broken at {where}"
        sfo = r.sfo_first_is_write + r.sfo_first_is_zero_read + r.sfo_first_is_nonzero_read
        assert r.sfo_total_slots == sfo, f"slot first-op partition broken at {where}"
        if "denom_block" in r:
            assert r.anchor_block - r.denom_block <= 50_400, f"stale denominator at {where}"


def verify_against_snapshot(df: pd.DataFrame) -> None:
    """The newest anchor of each window must reproduce the committed v2 parquets."""
    typed = pd.read_parquet(DATA_DIR_V2 / "q1_warmth_slot_typed.parquet")
    acct = pd.read_parquet(DATA_DIR_V2 / "q1_warmth_account.parquet")
    upd = pd.read_parquet(DATA_DIR_V2 / "slot_update_coverage.parquet")
    for t in df["window_days"].unique():
        row = df[(df.window_days == t) & (df.anchor_block == ANCHOR_BLOCK_V2)]
        if row.empty:
            continue
        row = row.iloc[0]
        ref = typed[typed.window_days == t].iloc[0]
        assert int(row.slot_W) == int(ref.W) and int(row.slot_R) == int(ref.R), \
            f"sweep snapshot mismatch (slot) at T={t}"
        ref = acct[acct.window_days == t].iloc[0]
        assert int(row.acct_W) == int(ref.W), f"sweep snapshot mismatch (acct) at T={t}"
        ref = upd[upd.window_days == t].iloc[0]
        assert int(row.upd_total_updates) == int(ref.total_updates), \
            f"sweep snapshot mismatch (upd) at T={t}"


def _base_fig(title: str, ytitle: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        title=title,
        xaxis=dict(title="anchor date", gridcolor="lightgray"),
        yaxis=dict(title=ytitle, ticksuffix="%", gridcolor="lightgray",
                   rangemode="tozero"),
        template="plotly_white", width=1100, height=560,
        legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.85)"),
    )
    for name, block in FORKS.items():
        when = block_to_date(block)
        fig.add_vline(x=when, line_dash="dot", line_color="#9E9E9E")
        fig.add_annotation(x=when, y=1.0, yref="paper", text=name, showarrow=False,
                           font=dict(size=10, color="#757575"), yanchor="bottom")
    return fig


def _add_window_traces(fig: go.Figure, df: pd.DataFrame, value_fn, label: str,
                       dash: str = "solid") -> None:
    for t in sorted(df["window_days"].unique()):
        sub = df[df.window_days == t]
        fig.add_trace(go.Scatter(
            x=sub["date"], y=value_fn(sub), name=f"{label} T={t}d",
            mode="lines", line=dict(color=WINDOW_COLORS[int(t)], width=2, dash=dash),
        ))


def render_all(df: pd.DataFrame) -> None:
    charts: list[tuple[str, go.Figure]] = []

    for obj, denom_col, label in (("slot", "denom_storages", "live slots"),
                                  ("acct", "denom_accounts", "live accounts")):
        fig = _base_fig(f"Warmth over time — {label}", f"% of {label}")
        _add_window_traces(fig, df, lambda s, o=obj, d=denom_col:
                           100 * s[f"{o}_RW_union"] / s[d], "R∪W")
        _add_window_traces(fig, df, lambda s, o=obj, d=denom_col:
                           100 * s[f"{o}_R"] / s[d], "R", dash="dash")
        charts.append((f"sweep_warmth_{obj}.png", fig))

    fig = _base_fig("Combined warmth over time — slots + accounts", "% of live state")
    _add_window_traces(fig, df, lambda s:
                       100 * (s.slot_RW_union + s.acct_RW_union)
                       / (s.denom_storages + s.denom_accounts), "R∪W")
    charts.append(("sweep_warmth_combined.png", fig))

    fig = _base_fig("Slot write structure over time", "% of |W|")
    _add_window_traces(fig, df, lambda s: 100 * s.slot_W_only_create / s.slot_W,
                       "create-only")
    _add_window_traces(fig, df, lambda s: 100 * s.slot_W_any_update / s.slot_W,
                       "any-update", dash="dash")
    charts.append(("sweep_write_structure.png", fig))

    d365 = df[df.window_days == 365]
    if not d365.empty:
        fig = _base_fig("W_mixed composition over time (T=365d)", "% of W_mixed")
        for col, (label, color) in _MIXED_COMBOS.items():
            fig.add_trace(go.Scatter(
                x=d365["date"], y=100 * d365[col] / d365["slot_W_mixed"], name=label,
                mode="lines", stackgroup="one", line=dict(color=color, width=0.5),
                fillcolor=color))
        fig.update_yaxes(range=[0, 100], rangemode=None)
        charts.append(("sweep_mixed_decomp.png", fig))

    fig = _base_fig("Slot read structure over time", "% of |R|")
    _add_window_traces(fig, df, lambda s: 100 * s.slot_R_only_zero / s.slot_R,
                       "zero-only")
    charts.append(("sweep_read_structure.png", fig))

    fig = _base_fig("Concentration over time — top-1% share of accesses",
                    "share of accesses")
    _add_window_traces(fig, df, lambda s: 100 * s.conc_slot_top1_R, "slot R")
    _add_window_traces(fig, df, lambda s: 100 * s.conc_acct_top1_R, "acct R",
                       dash="dash")
    charts.append(("sweep_concentration.png", fig))

    fig = _base_fig("Warm-update coverage over time (§7)", "% of update events warm")
    _add_window_traces(fig, df, lambda s: s.upd_pct_warm, "warm")
    charts.append(("sweep_update_coverage.png", fig))

    fig = _base_fig("First-op = nonzero read over time (§8 policy-bad set)",
                    "% of R∪W objects")
    _add_window_traces(fig, df, lambda s:
                       100 * s.sfo_first_is_nonzero_read / s.sfo_total_slots, "slots")
    _add_window_traces(fig, df, lambda s:
                       100 * s.afo_first_is_nonzero_read / s.afo_total_accounts,
                       "accounts", dash="dash")
    charts.append(("sweep_first_op.png", fig))

    fig = _base_fig("R-only accounts non-empty share over time (§8)",
                    "% of R-only accounts")
    _add_window_traces(fig, df, lambda s:
                       100 * s.res_nonempty_accounts / s.res_total_r, "non-empty")
    charts.append(("sweep_empty_split.png", fig))

    for name, fig in charts:
        write_image_safe(fig, DATA_DIR_V2 / name)
        print(f"  rendered {name}", flush=True)


def main() -> None:
    df = load_sweeps()
    print(f"{len(df)} sweep rows across windows {sorted(df.window_days.unique())}")
    verify_rows(df)
    verify_against_snapshot(df)
    df.to_parquet(DATA_DIR_V2 / "sweep_summary.parquet", index=False)
    render_all(df)
    print("Done.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_analysis_v2_sweep.py -q`
Expected: 3 passed

- [ ] **Step 5: Smoke the full module against the 2-row parquet from Task 4**

Run: `uv run python -m state_access.analysis_v2_sweep`
Expected: verification passes, all charts render (2-point lines), `Done.`

- [ ] **Step 6: Lint, run the whole test suite, commit**

```bash
uvx ruff check state_access/analysis_v2_sweep.py
uv run pytest -q
git add state_access/analysis_v2_sweep.py tests/test_analysis_v2_sweep.py
git commit -m "state_access v2: sweep analysis — verification + fork-annotated time-series charts"
```

---

### Task 6: Full T=30d collection + first results

- [ ] **Step 1: Launch the T=30 sweep** (≈4.5h; resumable — just relaunch on any crash)

```bash
uv run python -m state_access.collect_v2_sweep 30
```

- [ ] **Step 2: When complete, re-run analysis and inspect**

```bash
uv run python -m state_access.analysis_v2_sweep
```

Expected: ~181 rows for T=30, all verifications pass, charts show full 2022→2026 series.

- [ ] **Step 3: Commit data + charts**

```bash
git add state_access/data/v2/sweep_w30.parquet state_access/data/v2/sweep_summary.parquet \
        state_access/data/v2/sweep_*.png
git commit -m "state_access v2: T=30d weekly sweep collected (merge → anchor)"
```

---

### Task 7: T=90 / 180 / 365 collection

- [ ] **Step 1: Launch remaining windows** (≈13h / 25h / 40h; run across nights,
relaunch freely — resume skips done anchors)

```bash
uv run python -m state_access.collect_v2_sweep 90 180 365
```

- [ ] **Step 2: Re-run analysis over all four windows**

```bash
uv run python -m state_access.analysis_v2_sweep
```

Expected: all windows verified (incl. snapshot equality per window), charts overlay
four lines.

- [ ] **Step 3: Commit**

```bash
git add state_access/data/v2/sweep_w*.parquet state_access/data/v2/sweep_summary.parquet \
        state_access/data/v2/sweep_*.png
git commit -m "state_access v2: full sweep grid collected (T=30/90/180/365 weekly)"
```

---

### Task 8: Report Part III + handover

**Files:**
- Modify: `state_access/REPORT_v2.md` (new part before Appendix A)
- Modify: `state_access/HANDOVER_v2.md` (code map, reproduce, key findings, pending work)

- [ ] **Step 1: Generate the summary tables for the report**

```bash
uv run python -c "
import pandas as pd
df = pd.read_parquet('state_access/data/v2/sweep_summary.parquet')
for t in (30, 365):
    sub = df[df.window_days == t]
    q = sub.iloc[::13]  # ~quarterly rows for compact tables
    print(f'--- T={t} ---')
    print(q.assign(
        warm_pct=100*(q.slot_RW_union+q.acct_RW_union)/(q.denom_storages+q.denom_accounts),
        r_share=100*q.slot_R/q.slot_RW_union,
        create_only=100*q.slot_W_only_create/q.slot_W,
        zero_share=100*q.slot_R_only_zero/q.slot_R,
        upd_warm=q.upd_pct_warm,
        sfo_bad=100*q.sfo_first_is_nonzero_read/q.sfo_total_slots,
    )[['date','warm_pct','r_share','create_only','zero_share','upd_warm','sfo_bad']]
      .to_string(index=False))
"
```

- [ ] **Step 2: Write "Part III — Historical sweep (post-merge)"** in REPORT_v2.md,
inserted between §9 and Appendix A. Structure (fill readings from the actual series):

```markdown
# Part III — Historical sweep (post-merge)

Method: weekly anchors (50,400-block step) from the merge to the snapshot anchor,
windows T ∈ {30, 90, 180, 365}d, each anchor floored so its lookback stays post-merge.
Per-anchor denominators from the local `execution_state_size` (nearest row ≤ anchor,
staleness ≤ 1 week enforced). The newest anchor reproduces Part I/II exactly
(verified). Charts annotate Shanghai / Dencun / Pectra / Fusaka.

## 10. Warmth over time          ← sweep_warmth_{slot,acct,combined}.png + readings:
   is the warm-set share trending? did any fork move it? is the ~30% R-uplift stable?
## 11. Write structure over time ← sweep_write_structure.png + sweep_mixed_decomp.png:
   is create-dominance drifting? did 4844 blobs change slot-creation behavior?
## 12. Read structure over time  ← sweep_read_structure.png:
   did the zero-probe share shift at Dencun/Pectra?
## 13. Concentration over time   ← sweep_concentration.png:
   is the R-only top-1% spike recent or structural?
## 14. Policy stability          ← sweep_update_coverage.png, sweep_first_op.png,
   sweep_empty_split.png: is §7's 94% (T=30d) flat across 3.5 years? do the §8
   policy-bad shares drift?
```

Each section: chart embed + 2–3 readings written from the data, same voice as Parts
I/II (no "vs original analysis" framing; T for windows, never W).

- [ ] **Step 3: Update HANDOVER_v2.md** — add `collect_v2_sweep.py`,
`analysis_v2_sweep.py`, `sweep_concentration.py` to the code map; add the sweep
commands to §5 reproduce; add a Part III line to §7 key findings; mark "historical
sweep" DONE in §8 pending work (it was item 1).

- [ ] **Step 4: Commit**

```bash
git add state_access/REPORT_v2.md state_access/HANDOVER_v2.md
git commit -m "state_access v2: Part III — post-merge historical sweep findings"
```

---

## Self-review notes

- Spec coverage: grid/config → Task 1; summary SQL → Task 2; concentration reduction →
  Task 3; driver incl. denominators/retry/resume → Task 4; analysis/verification/charts
  (incl. kaleido process isolation) → Task 5; execution order → Tasks 6–7; report →
  Task 8. Spec's "out of scope" items are untouched.
- The concentration snapshot comparison uses 3e-3 tolerance (tie ordering at the top-N
  cutoff differs between pandas quicksort and stable argsort); all count comparisons
  are exact.
- `build_row` keys must match what Tasks 5 and 8 read: `slot_*` columns mirror the SQL
  aliases of `slot_sweep_summary` (`W`, `R`, `RW_union`, `W_only_create`, …,
  `R_mixed`), `upd_*` mirrors `slot_update_coverage` (`total_updates`, `warm_updates`,
  `cold_updates`, `pct_warm`), `sfo_*`/`afo_*` mirror the first-op aliases,
  `res_*` mirrors `account_r_empty_split`, `conc_{slot,acct}_top{1,10}_{W,R,RW_union}`
  mirror `concentration_shares` keys prefixed in `_fetch_anchor`.
