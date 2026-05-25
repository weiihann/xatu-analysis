# state_access Historical Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-anchor `state_access` analysis into a post-Merge weekly time series at W=30, to test whether the hot/cold, writes-to-cold, and gas-concentration findings are stable over time.

**Architecture:** An outer anchor-loop reuses the existing, verified `state_access/queries.py` builders at a fixed window. Pure logic (anchor generation, block→date, row derivation, resume filtering) is unit-tested; the DB loop and charts are verified by running. Results checkpoint to one parquet after every anchor so a ~1.5 hr run is interruption-safe.

**Tech Stack:** Python 3.13, uv, clickhouse-connect, pandas, plotly, pytest.

---

## File Structure

- Create: `state_access/history_config.py` — sweep constants, `anchors()`, `block_to_date()`, fork blocks.
- Create: `state_access/collect_history.py` — pure helpers (`remaining_anchors`, `build_row`) + DB loop (`_fetch_anchor`, `main`).
- Create: `state_access/analysis_history.py` — `# %%` cells rendering 3 trend charts.
- Create: `tests/test_history_config.py`, `tests/test_collect_history.py` — unit tests for the pure logic.
- Modify: `pyproject.toml` — add `pytest` dev dependency and pytest `pythonpath` config.

Reused unchanged: `lib/clickhouse.py`, `state_access/queries.py`, `state_access/config.py`.

---

## Task 1: Test tooling + sweep config

**Files:**
- Modify: `pyproject.toml`
- Create: `state_access/history_config.py`
- Test: `tests/test_history_config.py`

- [ ] **Step 1: Add pytest and configure import path**

Run:
```bash
uv add --dev pytest
```

Then append to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_history_config.py`:
```python
from datetime import datetime, timezone

from state_access import history_config as hc


def test_anchors_end_at_static_anchor():
    a = hc.anchors()
    assert a[-1] == hc.END_BLOCK == 24_870_000


def test_anchors_sorted_ascending_and_evenly_spaced():
    a = hc.anchors()
    assert a == sorted(a)
    diffs = {b - x for x, b in zip(a, a[1:])}
    assert diffs == {hc.STEP}


def test_anchors_stay_within_post_merge_range():
    a = hc.anchors()
    assert a[0] >= hc.START_BLOCK
    assert a[0] - hc.STEP < hc.START_BLOCK  # can't fit another step below


def test_block_to_date_at_merge():
    assert hc.block_to_date(hc.MERGE_BLOCK) == datetime(2022, 9, 15, 6, 42, 59, tzinfo=timezone.utc)


def test_block_to_date_one_day_later():
    one_day = hc.block_to_date(hc.MERGE_BLOCK + 7_200)
    assert one_day == datetime(2022, 9, 16, 6, 42, 59, tzinfo=timezone.utc)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_history_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'state_access.history_config'`

- [ ] **Step 4: Write the implementation**

Create `state_access/history_config.py`:
```python
"""Configuration for the post-Merge historical sweep of state_access at W=30.

Anchors step weekly (7 * 7,200 blocks) over the post-Merge range, generated
descending from END_BLOCK so the final anchor coincides with the static run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from state_access.config import DATA_DIR

START_BLOCK = 15_537_394  # The Merge (first PoS block)
END_BLOCK = 24_870_000    # matches the static analysis anchor
STEP = 50_400             # 7 days * 7,200 blocks/day (weekly)
W = 30                    # fixed active-window, in days

MERGE_BLOCK = 15_537_394
MERGE_TS = 1_663_224_179  # 2022-09-15 06:42:59 UTC, block 15,537,394
SECONDS_PER_BLOCK = 12

# Fork boundary blocks, for chart annotations.
FORKS = {
    "Shanghai": 17_034_870,
    "Dencun": 19_426_587,
    "Pectra": 22_431_084,
}

HISTORY_PARQUET = DATA_DIR / "history_w30.parquet"


def anchors() -> list[int]:
    """Anchor blocks, weekly across the post-Merge range, ascending; last == END_BLOCK."""
    out: list[int] = []
    block = END_BLOCK
    while block >= START_BLOCK:
        out.append(block)
        block -= STEP
    return sorted(out)


def block_to_date(block: int) -> datetime:
    """Deterministic post-Merge block → UTC datetime (12s cadence; missed slots add drift)."""
    ts = MERGE_TS + (block - MERGE_BLOCK) * SECONDS_PER_BLOCK
    return datetime.fromtimestamp(ts, tz=timezone.utc)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_history_config.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock state_access/history_config.py tests/test_history_config.py
git commit -m "Add historical-sweep config and test tooling"
```

---

## Task 2: Pure collection helpers (resume filter + row derivation)

**Files:**
- Create: `state_access/collect_history.py` (helpers only this task)
- Test: `tests/test_collect_history.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_collect_history.py`:
```python
import pandas as pd

from state_access import collect_history as ch


def test_remaining_anchors_excludes_done():
    existing = pd.DataFrame({"anchor_block": [100, 300]})
    assert ch.remaining_anchors([100, 200, 300, 400], existing) == [200, 400]


def test_remaining_anchors_handles_no_checkpoint():
    assert ch.remaining_anchors([100, 200], None) == [100, 200]


def test_build_row_derives_cold_and_concentration():
    state = {"unique_accounts": 10, "unique_storage_slots": 20}
    totals = {"accounts": 1000, "storages": 2000}
    # acct_pct/stor_pct/updt_pct are warm percentages of today's writes.
    row = ch.build_row(anchor=15_537_394, state=state,
                       acct_pct=90.0, stor_pct=70.0, updt_pct=80.0, totals=totals)

    assert row["anchor_block"] == 15_537_394
    assert row["pct_accounts_cold"] == 99.0   # 100 - 100*10/1000
    assert row["pct_storage_cold"] == 99.0    # 100 - 100*20/2000
    assert row["acct_writes_cold_pct"] == 10.0
    assert row["storage_writes_cold_pct"] == 30.0
    assert row["pct_state_warm"] == 1.0       # 100*20/2000
    assert row["concentration_x"] == 80.0     # 80.0 / 1.0
    assert row["date"].year == 2022
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_collect_history.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'state_access.collect_history'`

- [ ] **Step 3: Write the helpers**

Create `state_access/collect_history.py`:
```python
"""Resumable post-Merge historical sweep of state_access at W=30.

Reuses the verified query builders in state_access.queries, one anchor at a time,
checkpointing the full result parquet after each anchor.

Run with:  uv run python -m state_access.collect_history
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from lib.clickhouse import run_query
from state_access import queries
from state_access.history_config import HISTORY_PARQUET, W, anchors, block_to_date


def remaining_anchors(all_anchors: list[int], existing: pd.DataFrame | None) -> list[int]:
    """Anchors not already present in a checkpoint DataFrame."""
    done: set[int] = set()
    if existing is not None and not existing.empty:
        done = set(int(b) for b in existing["anchor_block"])
    return [a for a in all_anchors if a not in done]


def build_row(
    anchor: int,
    state: Mapping[str, object],
    acct_pct: float,
    stor_pct: float,
    updt_pct: float,
    totals: Mapping[str, int],
) -> dict[str, object]:
    """Assemble one time-series row from raw per-anchor query outputs."""
    unique_accounts = int(state["unique_accounts"])
    unique_slots = int(state["unique_storage_slots"])
    total_accounts = int(totals["accounts"])
    total_storages = int(totals["storages"])
    pct_state_warm = 100 * unique_slots / total_storages
    return {
        "anchor_block": anchor,
        "date": block_to_date(anchor),
        "unique_accounts": unique_accounts,
        "unique_storage_slots": unique_slots,
        "total_accounts": total_accounts,
        "total_storages": total_storages,
        "pct_accounts_cold": round(100 - 100 * unique_accounts / total_accounts, 4),
        "pct_storage_cold": round(100 - 100 * unique_slots / total_storages, 4),
        "acct_writes_cold_pct": round(100 - acct_pct, 4),
        "storage_writes_cold_pct": round(100 - stor_pct, 4),
        "pct_update_gas_warm": round(updt_pct, 4),
        "pct_state_warm": round(pct_state_warm, 4),
        "concentration_x": round(updt_pct / pct_state_warm, 2),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_collect_history.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add state_access/collect_history.py tests/test_collect_history.py
git commit -m "Add pure helpers for historical sweep collection"
```

---

## Task 3: Collection loop (DB integration) + 3-anchor dry run

**Files:**
- Modify: `state_access/collect_history.py` (add `_fetch_anchor`, `main`)

- [ ] **Step 1: Add the DB loop**

Append to `state_access/collect_history.py`:
```python
def _fetch_anchor(anchor: int) -> dict[str, object]:
    """Run all queries for one anchor and build its row."""
    state = run_query(queries.state_touched(anchor, W)).iloc[0]
    acct_pct = float(run_query(queries.account_writes_warm(anchor, W)).iloc[0]["pct_warm"])
    stor_pct = float(run_query(queries.storage_writes_warm(anchor, W)).iloc[0]["pct_warm"])
    updt_pct = float(run_query(queries.update_writes_warm(anchor, W)).iloc[0]["pct_warm"])

    tdf = run_query(queries.totals(anchor), profile="ethpandaops")
    if tdf.empty:
        raise RuntimeError(f"No execution_state_size snapshot at or before block {anchor}.")
    totals = {"accounts": int(tdf.iloc[0]["accounts"]), "storages": int(tdf.iloc[0]["storages"])}

    return build_row(anchor, state, acct_pct, stor_pct, updt_pct, totals)


def main() -> None:
    existing = pd.read_parquet(HISTORY_PARQUET) if HISTORY_PARQUET.exists() else None
    rows = existing.to_dict("records") if existing is not None else []

    todo = remaining_anchors(anchors(), existing)
    print(f"W={W}d sweep: {len(todo)} anchors to collect "
          f"({len(rows)} already done), writing {HISTORY_PARQUET}")

    for i, anchor in enumerate(todo, 1):
        row = _fetch_anchor(anchor)
        rows.append(row)
        pd.DataFrame(rows).sort_values("anchor_block").to_parquet(HISTORY_PARQUET, index=False)
        print(f"  [{i}/{len(todo)}] block {anchor:,} {row['date']:%Y-%m-%d}: "
              f"storage cold {row['pct_storage_cold']:.1f}%, "
              f"update-gas warm {row['pct_update_gas_warm']:.1f}%, "
              f"conc {row['concentration_x']:.0f}x")

    print(f"\nDone. {len(rows)} anchors in {HISTORY_PARQUET}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Lint**

Run: `uvx ruff check state_access/collect_history.py`
Expected: `All checks passed!`

- [ ] **Step 3: Dry run on 3 anchors**

Run:
```bash
uv run python -c "
import state_access.collect_history as ch
ch.anchors = lambda: [15_537_394, 20_000_000, 24_870_000]
ch.main()
"
```
Expected: 3 lines printed; the block 24,870,000 row prints `storage cold` ≈ 97.0% and `update-gas warm` ≈ 84.8% (matching the static `collect.py`/`analysis.py` numbers within `uniq` noise).

- [ ] **Step 4: Verify the checkpoint parquet**

Run:
```bash
uv run python -c "
import pandas as pd
from state_access.history_config import HISTORY_PARQUET
df = pd.read_parquet(HISTORY_PARQUET)
print(df[['anchor_block','date','pct_storage_cold','pct_update_gas_warm','concentration_x']].to_string(index=False))
assert len(df) == 3
assert (df['concentration_x'] > 1).all()
assert df['pct_storage_cold'].between(0, 100).all()
end = df[df['anchor_block'] == 24_870_000].iloc[0]
assert abs(end['pct_update_gas_warm'] - 84.79) < 1.0, end['pct_update_gas_warm']
print('OK: end anchor reproduces static result')
"
```
Expected: 3-row table, all asserts pass, "OK" printed.

- [ ] **Step 5: Verify resumability**

Run:
```bash
uv run python -c "
import state_access.collect_history as ch
ch.anchors = lambda: [15_537_394, 20_000_000, 24_870_000]
print('remaining after dry run:',
      ch.remaining_anchors(ch.anchors(), __import__('pandas').read_parquet(ch.HISTORY_PARQUET)))
"
```
Expected: `remaining after dry run: []` (all three already checkpointed).

- [ ] **Step 6: Clear the dry-run parquet**

Run: `trash state_access/data/history_w30.parquet`
(So the real full run starts clean.)

- [ ] **Step 7: Commit**

```bash
git add state_access/collect_history.py
git commit -m "Add historical sweep collection loop"
```

---

## Task 4: Trend charts

**Files:**
- Create: `state_access/analysis_history.py`

- [ ] **Step 1: Write the charting script**

Create `state_access/analysis_history.py`:
```python
"""Render the historical-sweep trend charts from history_w30.parquet.

Run `state_access/collect_history.py` first. Organised as `# %%` cells; also runs
top-to-bottom as a script, saving PNGs next to the parquet.

    uv run python -m state_access.analysis_history
"""

# %%
from __future__ import annotations

import pandas as pd
import plotly.graph_objs as go

from state_access.config import DATA_DIR
from state_access.history_config import FORKS, HISTORY_PARQUET, W, block_to_date

ACCOUNT_COLOR = "#1565C0"
STORAGE_COLOR = "#B71C1C"
GAS_COLOR = "#2E7D32"

df = pd.read_parquet(HISTORY_PARQUET).sort_values("anchor_block")
print(f"{len(df)} anchors, {df['date'].min():%Y-%m-%d} → {df['date'].max():%Y-%m-%d}")


def show(fig: go.Figure) -> None:
    """Display interactively, or skip if no renderer (PNG is the durable artifact)."""
    try:
        fig.show()
    except ValueError:
        pass


def add_forks(fig: go.Figure) -> None:
    """Annotate fork boundaries that fall within the swept range."""
    for name, block in FORKS.items():
        if df["anchor_block"].min() <= block <= df["anchor_block"].max():
            fig.add_vline(x=block_to_date(block), line=dict(color="gray", width=1, dash="dash"),
                          annotation_text=name, annotation_position="top")


# %%
# Chart 1 — hot/cold state share over time.
fig1 = go.Figure()
fig1.add_trace(go.Scatter(x=df["date"], y=df["pct_accounts_cold"], mode="lines+markers",
                          name="accounts cold %", line=dict(color=ACCOUNT_COLOR, width=2)))
fig1.add_trace(go.Scatter(x=df["date"], y=df["pct_storage_cold"], mode="lines+markers",
                          name="storage cold %", line=dict(color=STORAGE_COLOR, width=2)))
add_forks(fig1)
fig1.update_layout(
    title=f"Cold state share over time (W={W}d) — mainnet, post-Merge weekly",
    xaxis=dict(title="date", gridcolor="lightgray"),
    yaxis=dict(title="% of state COLD", ticksuffix="%", gridcolor="lightgray"),
    template="plotly_white", width=1300, height=550,
    legend=dict(x=0.01, y=0.02, bgcolor="rgba(255,255,255,0.85)"),
)
fig1.write_image(DATA_DIR / "history_state_cold.png", scale=2)
show(fig1)

# %%
# Chart 2 — writes-to-cold over time.
fig2 = go.Figure()
fig2.add_trace(go.Scatter(x=df["date"], y=df["acct_writes_cold_pct"], mode="lines+markers",
                          name="account writes → cold %", line=dict(color=ACCOUNT_COLOR, width=2)))
fig2.add_trace(go.Scatter(x=df["date"], y=df["storage_writes_cold_pct"], mode="lines+markers",
                          name="storage writes → cold %", line=dict(color=STORAGE_COLOR, width=2)))
add_forks(fig2)
fig2.update_layout(
    title=f"Writes hitting the COLD tier over time (W={W}d)",
    xaxis=dict(title="date", gridcolor="lightgray"),
    yaxis=dict(title="% of today's writes that are cold", ticksuffix="%", gridcolor="lightgray"),
    template="plotly_white", width=1300, height=550,
    legend=dict(x=0.01, y=0.98, bgcolor="rgba(255,255,255,0.85)"),
)
fig2.write_image(DATA_DIR / "history_writes_cold.png", scale=2)
show(fig2)

# %%
# Chart 3 — update-gas warm share + concentration over time (dual axis).
fig3 = go.Figure()
fig3.add_trace(go.Scatter(x=df["date"], y=df["pct_update_gas_warm"], mode="lines+markers",
                          name="update-gas hitting warm %", line=dict(color=GAS_COLOR, width=2)))
fig3.add_trace(go.Scatter(x=df["date"], y=df["concentration_x"], mode="lines+markers",
                          name="concentration (×)", line=dict(color=STORAGE_COLOR, width=2, dash="dot"),
                          yaxis="y2"))
add_forks(fig3)
fig3.update_layout(
    title=f"Gas concentration over time (W={W}d): warm-gas coverage vs concentration",
    xaxis=dict(title="date", gridcolor="lightgray"),
    yaxis=dict(title="update-gas warm %", ticksuffix="%", gridcolor="lightgray"),
    yaxis2=dict(title="concentration (×)", overlaying="y", side="right", showgrid=False),
    template="plotly_white", width=1300, height=550,
    legend=dict(x=0.01, y=0.02, bgcolor="rgba(255,255,255,0.85)"),
)
fig3.write_image(DATA_DIR / "history_gas_concentration.png", scale=2)
show(fig3)
```

- [ ] **Step 2: Lint**

Run: `uvx ruff check state_access/analysis_history.py`
Expected: `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add state_access/analysis_history.py
git commit -m "Add historical sweep trend charts"
```

---

## Task 5: Full run + documentation

**Files:**
- Modify: `state_access/README.md`

- [ ] **Step 1: Run the full sweep (background, ~1–1.5 hr)**

Run: `uv run python -m state_access.collect_history`
Expected: ~186 progress lines; finishes "Done. 186 anchors in .../history_w30.parquet". Safe to interrupt and re-run — it resumes.

- [ ] **Step 2: Render the trend charts**

Run: `uv run python -m state_access.analysis_history`
Expected: prints the anchor count + date range; writes `history_state_cold.png`, `history_writes_cold.png`, `history_gas_concentration.png` to `data/`.

- [ ] **Step 3: Document the sweep in the analysis README**

Add this section to `state_access/README.md` (after the "Run" section):
```markdown
## Historical sweep

`collect_history.py` / `analysis_history.py` extend the static analysis into a post-Merge
weekly time series at fixed W=30 (block 15.54M → 24.87M, ~186 anchors), to test whether the
findings are stable over time. It reuses the same query builders, one anchor at a time,
checkpointing `data/history_w30.parquet` after each anchor (resumable).

```bash
uv run python -m state_access.collect_history   # ~1–1.5 hr, resumable
uv run python -m state_access.analysis_history   # 3 trend charts
```

Outputs: `data/history_w30.parquet` and `history_{state_cold,writes_cold,gas_concentration}.png`.
```

- [ ] **Step 4: Commit**

```bash
git add state_access/README.md state_access/data/history_w30.parquet state_access/data/history_*.png
git commit -m "Run historical sweep and document it"
```

---

## Self-Review notes

- **Spec coverage:** all three metric families (Task 3 row + Task 4 charts 1/2/3); W=30 fixed (`history_config.W`); post-Merge range + weekly step (`history_config` constants, Task 1 tests); reuse of existing builders (Task 3 `_fetch_anchor`); resumability (Task 2 `remaining_anchors` + Task 3 checkpoint/Step 5); block→date (Task 1 `block_to_date`); fork annotations (Task 4 `add_forks`); end-anchor reproduces static result (Task 3 Step 4); per-block totals (Task 3 `_fetch_anchor` via `queries.totals`).
- **No placeholders:** every code/command step is complete.
- **Type consistency:** `build_row(anchor, state, acct_pct, stor_pct, updt_pct, totals)` and `remaining_anchors(all_anchors, existing)` signatures match between Task 2 definition, Task 2 tests, and Task 3 caller; row keys used in Task 4 charts (`pct_accounts_cold`, `pct_storage_cold`, `acct_writes_cold_pct`, `storage_writes_cold_pct`, `pct_update_gas_warm`, `concentration_x`, `date`, `anchor_block`) all exist in the Task 2 `build_row` output.
