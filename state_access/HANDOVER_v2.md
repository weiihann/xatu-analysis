# Handover — `state_access` v2 (hot/cold state, reads-aware)

Branch: **`feat/gas-analysis`** (despite the name, it now carries the whole v2
reads-aware state-access analysis, not just gas). Latest commit at handover: `1d6d114`.
No open PR.

This document is the single onboarding entry point for an agent picking up this work.
Read this, then `REPORT_v2.md` (the findings), then `queries_v2.py` (the SQL). Everything
below is true as of 2026-06-12.

---

## 1. What this project is

Measures Ethereum mainnet **state access** over trailing time windows, separating
**writes** from **reads**, and interprets it against **EIP-8188-based write-age tiering**
(state written within an "active window" is cheap/Active, stale state is
Inactive/expensive). NOTE: the June 2026 draft of EIP-8188 records `last_written_block`
metadata only and defers the tiering gas schedule to a separate proposal — see the scope
note at the top of `REPORT_v2.md`.

Two prior analyses exist and are **superseded** by v2 (kept for reference, not deleted):
- `analysis.py` / `REPORT.md` — write-only (uses `_diffs` tables only). The original
  replication of Toni Wahrstätter's `state_access.ipynb`.
- `analysis_gas.py`, `analysis_history.py`, `analysis_windows.py` — gas + historical sweep
  add-ons to the original.

**v2** (`*_v2.py`, `REPORT_v2.md`, `data/v2/`) is the current work: it adds the **reads**
dimension (`_reads` tables + `address_appearances`) that the `_diffs`-only analyses can't
see, and is the thing to keep building on.

### Core notation (important — recently disambiguated)
- **W / R / R∪W** = access **sets**: writes / reads-deduped-against-writes / union.
- **T** = the trailing **window length in days**. It used to also be "W", which collided
  with the writes set. As of commit `1d6d114`, the window is **always `T`**, never `W`,
  in the report and chart labels. Keep this discipline.

### Set definitions (per `(T, object_type)`)
- **W** — objects in writes-source tables in window (raw).
- **R** — objects in reads-source tables AND NOT in writes sources, same window
  (R ∩ W = ∅ by construction).
- **R∪W = W + R** — additive (disjoint), the full warm set.
- Empirically **W-only ≈ 0** (every written object is also read in window: SLOAD-before-
  SSTORE for slots; tx_from nonce read for accounts), which is *why* the 2-set additive
  view is used instead of a 3-way (W-only / R-only / R∩W) partition.

---

## 2. The snapshot (anchor + windows)

- **Anchor block: `24,870,000`** (mainnet). Chosen because it's the largest round block in
  the common-coverage envelope: on the local cluster `_diffs` end ~24.87M while `_reads` +
  `address_appearances` reach ~25.19M. Defined in `config_v2.ANCHOR_BLOCK_V2`.
- **Windows: `T ∈ {1, 7, 14, 30, 60, 90, 180, 365}` days.** `config_v2.WINDOWS_V2`.
  Block range per window: `[anchor − T·7200, anchor]` (post-Merge cadence 7,200 blk/day).
- **Object types:** storage slots `(contract_address, slot)` and accounts `(address)`.
- Mainnet only (`meta_network_name='mainnet'`).
- **This is a single-anchor snapshot.** A historical sweep over weekly post-Merge anchors
  is explicitly out of scope / future work (see §8).

---

## 3. Data infrastructure (READ THIS before re-running anything)

### ClickHouse profiles (`lib/clickhouse.py`, configured via repo-root `.env`)
- **`primary`** — the personal Xatu node (Tailscale host `asyuki.tailccb236.ts.net:8123`).
  Holds ALL source tables used by v2. This is where every v2 query runs.
- **`ethpandaops`** — the public ethPandaOps cluster. Used only for:
  (a) `execution_state_size` live-state totals (the % denominators), and
  (b) as the **source for backfills** into `primary`.

`.env` keys (values are secrets, not in git): `XATU_CLICKHOUSE_*` (=primary),
`ETHPANDAOPS_CLICKHOUSE_*`.

### Cluster stability warning ⚠️
The `primary` node is **memory-limited and crashed several times** during this work under
heavy queries (big `GLOBAL JOIN`s, `UInt256` GROUP-BY state, W=365 scans). Symptoms:
`Connection refused` on port 8123, or `IncompleteRead`/code-241-style OOM. When it dies it
usually comes back on its own after a while. **All v2 queries were rewritten to avoid the
OOM triggers** (no GLOBAL JOINs, UInt8 flags instead of UInt256, single GROUP BY). If you
add new queries, follow the same discipline — see §5.

### Backfills already done (do NOT redo; they're complete)
The local `primary` node had gaps vs ethpandaops. All three backfill scripts have **already
been run to completion** and verified zero-gap over `[15,537,394, 25,189,620]`:

| script | table(s) | what it filled |
|---|---|---|
| `backfill_reads.py` | `canonical_execution_{storage,balance,nonce}_reads` | ~7.5M rows across narrow gap bands |
| `backfill_address_appearances.py` | `canonical_execution_address_appearances` | the `[20.5M, 25.19M]` mid+tail gaps (~900M rows total over 3 passes). (An earlier note claimed pre-Dencun `[15.5M, 19.4M]` was left unfilled — empirically false as of 2026-06-12: local bucket counts match ethpandaops to ~0.1% across the whole post-merge range.) |
| `backfill_transaction.py` | `canonical_execution_transaction` | 34,901 blocks in gap buckets 230/232/251. NOTE: this was needed by an interim `account_first_op` that used a JOIN to recover tx_index; the final query no longer JOINs, so this table is **not on the v2 query path anymore**, but the backfill is harmless and done. |

All three scripts are **idempotent** (ReplicatedReplacingMergeTree dedups; they re-discover
missing blocks at runtime) — safe to re-run, but unnecessary.

### Source tables (all on `primary`)
| set | tables |
|---|---|
| writes (accounts) | `canonical_execution_balance_diffs`, `…_nonce_diffs`, `…_contracts` (keyed on `contract_address`) |
| writes (slots) | `canonical_execution_storage_diffs` |
| reads (slots) | `canonical_execution_storage_reads` |
| reads (accounts) | `canonical_execution_balance_reads`, `…_nonce_reads`, `…_address_appearances` (relationships `{call_from, call_to, tx_from, tx_to, miner_fee, factory, create, suicide_refund, suicide}`; ERC-20/721 excluded) |
| denominators | `execution_state_size` (queried via **ethpandaops**; local copy is now populated and agrees) |

`_diffs` end ~24.87M on local; `_reads`/`address_appearances` reach ~25.19M. This is why
the anchor is capped at 24.87M (a later anchor needs a `_diffs` backfill first).

Data semantics (verified 2026-06-12, see REPORT_v2.md §2 "Granularity and known gaps"):
`_diffs` and `_reads` are one row per (tx, object); diffs are net-per-tx and exclude
reverted writes, reads include reverted reads; system-call writes (EIP-4788/2935/
7002/7251) and consensus-layer withdrawal credits are absent from `_diffs`.

---

## 4. Code map

```
state_access/
  config_v2.py            # ANCHOR_BLOCK_V2, MERGE_BLOCK, WINDOWS_V2, DATA_DIR_V2 (BINS is vestigial — Q2 was removed)
  queries_v2.py           # SQL builders (the actual analysis logic): 6 windowed + 8 full-history event-totals
  collect_v2.py           # drives slot/account/slot_typed histograms → data/v2/*.parquet (resumable per (T, object_type))
  collect_v2_history.py   # full-history event totals, 1M-block chunks, era-split profiles (resumable per (kind, chunk))
  collect_v2_sweep.py     # Part III: weekly-anchor sweep of §3–§8, newest-first, resumable per (T, anchor), atomic checkpoints
  sweep_concentration.py  # tie-aware exact top-N concentration reduction used by the sweep driver
  analysis_v2_sweep.py    # Part III: verifies sweep rows + snapshot equality, renders the time-series charts (process-isolated kaleido)
  analysis_v2.py          # derives tables + renders all PNG charts from the parquets; `main()` runs everything
  _sweep_resume_all.py    # one-off driver that ran first_op + empty_split sweeps incrementally (idempotent; kept for re-runs)
  backfill_*.py           # the three completed backfills (§3)
  REPORT_v2.md            # THE FINDINGS — read this
  HANDOVER_v2.md          # this file
  data/v2/*.parquet,*.png # all outputs (committed)

  # superseded (reference only):
  analysis.py, collect.py, queries.py, config.py, REPORT.md
  analysis_gas.py, collect_gas.py, analysis_history.py, collect_history.py, analysis_windows.py, history_config.py
```

### `queries_v2.py` — the 6 SQL builders (each takes `(bn_now, days)`, returns SQL string)
1. **`slot_histogram`** — per-key `(slice, n_w, n_r, n_keys)` over slots. Feeds Warmth (§3)
   and Concentration (§6). `slice ∈ {w_only, r_only, rw}` re-mapped to W/R in Python.
2. **`account_histogram`** — same shape for accounts.
3. **`slot_typed_histogram`** — per-key counts split by transition (`n_w_create/update/
   delete`) and returned value (`n_r_zero/nonzero`). Feeds Write structure (§4), Read
   structure (§5), W_mixed decomposition.
4. **`slot_update_coverage`** — per-event warm/cold update classification under EIP-8188
   (§7). Returns one row `(total_updates, warm, cold, pct_warm)`.
5. **`slot_first_op`** / **`account_first_op`** — first-operation-in-window classification
   for the hypothetical read-side period-bump policy (§8). Returns counts of
   first=write/zero_read/nonzero_read(/appearance_read).
6. **`account_r_empty_split`** — of R-only accounts, empty vs non-empty (§8). Single GROUP
   BY, UInt8 flags.

### `analysis_v2.py` — `main()` orchestrates:
`run_one('slot')`, `run_one('account')`, `run_combined()`, `run_slot_typed()`,
`run_slot_mixed_decomp()`, `run_slot_update_coverage()`, `run_slot_first_op()`,
`run_account_first_op()`, `run_account_r_empty_split()`. Each derives a parquet + PNG.
Has inline verification asserts (additivity `|R∪W|=|W|+|R|`, partition sums, monotonicity).

---

## 5. How to reproduce

```bash
# from repo root, with .env populated
uv run python -m state_access.collect_v2          # slot/account/slot_typed histograms (~20 min; resumable)
uv run python -m state_access.collect_v2_history  # full-history event totals (~10 min; resumable)
uv run python -m state_access.collect_v2_sweep    # Part III weekly sweep, T={30,90,180,365} (DAYS; resumable per (T,anchor))
uv run python -m state_access.analysis_v2         # derives Parts I/II tables + charts (no DB; ~1 min, but kaleido is slow)
uv run python -m state_access.analysis_v2_sweep   # Part III verification + time-series charts (no DB)
```

`collect_v2.py` only builds the three histograms (`slot`, `account`, `slot_typed` —
`OBJECT_TYPES` dict). The later queries were added as separate drivers:
- **`slot_first_op`, `account_first_op`, `account_r_empty_split`** → run by
  `_sweep_resume_all.py` (`uv run python -m state_access._sweep_resume_all`, idempotent —
  skips windows already in the parquet). Parquets committed.
- **`slot_update_coverage`** → has **no committed driver**. Its parquet is committed, but
  if you need to rebuild it, write a small loop over `WINDOWS_V2` calling
  `queries_v2.slot_update_coverage(ANCHOR_BLOCK_V2, T)` and writing
  `data/v2/slot_update_coverage.parquet` with columns
  `(window_days, total_updates, warm_updates, cold_updates, pct_warm)`. (Heads-up: the
  `run_slot_update_coverage` docstring in `analysis_v2.py` wrongly says "persisted by
  collect_v2" — it isn't.)

**All parquets are committed**, so `analysis_v2.py` alone regenerates every chart/table
without touching the DB. Only re-hit the DB if you change a query.

Vestigial: `config_v2.BINS` is defined but used nowhere (leftover from the removed Q2). Safe
to delete.

### Query-writing discipline (to avoid crashing `primary`)
- **No `GLOBAL JOIN`** on big tables. Prefer a single `UNION ALL` event stream + one
  `GROUP BY cityHash64(key)`.
- **No `UInt256` in GROUP BY state.** Aggregate `max(value != 0)` (UInt8), not `max(value)`.
- Pack ordering into a single monotone `event_order` integer (block·M + tx_idx·10 +
  priority) and use `argMin`/`argMinIf` instead of joins/window functions.
- `cityHash64` the key so the distributed shuffle stays compact at T=365.
- Set generous `max_execution_time` (queries pass `settings={'max_execution_time': N}`).

---

## 6. Report structure (`REPORT_v2.md`)

```
1 Summary · 2 Data and method
Part I  — Descriptive:  3 Warmth · 4 Write structure · 5 Read structure · 6 Concentration
Part II — Policy:       7 Warm-update coverage (EIP-8188) · 8 Read-side period-bump first-op
9 What this opens up
Part III — Historical sweep: 10 Warmth · 11 R/W · 12 Write structure · 13 Read structure ·
           14 Concentration · 15 Policy stability — all over post-Merge time
Appendix A (ALL SQL) · Appendix B (outputs)
```

Recently reorganized (commit `1d6d114`): dropped vestigial Q1/Q3 labels and the 4b/4c/4d
suffixes; split descriptive vs policy; **removed the "vs the original analysis" framing**
entirely (the user does not want comparisons to the first write-only analysis). If you add
sections, keep that — describe v2 on its own terms.

---

## 7. Key findings (so you know what's established)

- Warm set R∪W is ~32–52% bigger than writes-alone (≈⅓ for T≥30d; reads matter).
- Slot W is **mostly creations, not updates** — 62% create-only at T=365d; only ~21% of W
  has any update. Write-age tiering only reprices updates, so policy-relevant write set
  ≈ 5.4% of state at T=365d, not 25%.
- Slot R is **93% empty-slot probes** (SLOAD returning 0). Only ~7% is populated reads.
- `W_mixed` is dominated by **C+D 1-cycle** (born+died once in window — ephemeral state),
  2.86% of live state at T=365d.
- **Warm-update coverage** (§7): per-event, 94% of update SSTOREs at T=30d stay Active
  (97% at T=90d). A naive static-set check undercounts this to 84.8% — the gap is
  intra-window promotion.
- **Read-side period bump** (§8): if reads also bumped periods, the bad-UX set (first op is
  a nonzero read) is ~5.6% of warm slots / ~10% of warm accounts at T=30d. And **93–98% of
  R-only accounts are non-empty**, so the "empty accounts are free" escape hatch is tiny.
- **Full-history event mix** (§4/§5 history subsections): write traffic is
  update-dominated (66% of 9.2B slot write events) and read traffic populated-dominated
  (70% of 23.7B SLOADs) — both the inverse of the per-object windowed views; the mix is
  era-stable across the merge. Creates−deletes closes to 100.4% of live slots. Reads
  outnumber writes ~2.6:1 (slots) / ~6:1 (accounts). `nonce_reads.nonce` is never 0
  (post-increment artifact — flagged in §5).
- **Historical sweep** (Part III, §10–§15, 648 weekly anchor-cells): the **active fraction
  of state shrinks over time** at every window (T=365: 45%→35%) — the longitudinal tiering
  case. **R/W rises** over time (reads matter more, not less). **§7 warm-update coverage is
  flat and high across 3.5 years** (the 94% snapshot value is representative, not a lucky
  anchor) — the policy conclusions are effectively time-invariant. **The account-read
  concentration spike (~98% top-1%) is recent** — it climbed from ~85% in 2022–23, not a
  structural constant. **No series steps at any fork** (Shanghai/Dencun/Pectra/Fusaka) —
  state-access structure tracks application behaviour, not protocol changes.

### Known caveat (documented in §8, important)
The `account_r_empty_split` "empty" bucket is **not confirmed-empty**. At every T the
*confirmed*-empty count (both balance AND nonce read, both 0) is ≈0. The "empty" accounts
are entirely `call_to`/`tx_to` recipients whose **balance was read as 0 but nonce was never
read** — because the EVM never reads a call recipient's nonce (only the sender's, which is a
write/increment). So "empty" really means "zero-value call target, existence unconfirmed."
The bias is **conservative** for the policy conclusion (a misclassified dormant nonce>0 EOA
would be non-empty, pushing the policy-bad share up). This is flagged as a blockquote in §8.
A future improvement could split it into `confirmed_empty` vs `balance_zero_unresolved`, but
it wasn't done because the conclusion is unaffected.

---

## 8. Pending / future work (explicitly out of scope so far)

1. **Historical sweep — DONE** (Part III, §10–§15). Weekly anchors ≤ 24.87M, T={30,90,180,
   365}, 648 cells via `collect_v2_sweep.py` → `analysis_v2_sweep.py`. Answered: R/W rises
   over time, concentration spike is recent, no fork (incl. Dencun) moves the structure,
   policy conclusions are time-invariant. A future extension would need a `_diffs` backfill
   to push the anchor past 24.87M; the sweep grid floors each window at the Merge.
2. **R-only contract-class drill-down.** Which contracts dominate R-only (the ~98% top-1%
   account concentration)? Likely DEX routers / multicall / weth9 / proxy implementations —
   unverified. Would need labelling.
3. **Per-tx all-hot fraction.** What share of txs touch ONLY warm state under a given T? A
   user-impact framing rather than population-level. Considered, not built.
4. **empty/non-empty relabel** (the §7 caveat above) — optional honesty improvement.
5. **`account_r_empty_split` at T=180/365** runs but is slow (~130s/235s); fine, just noted.

### Things that were tried and removed (don't resurrect without reason)
- **Q2 "composition"** (per-object access-count bins) — removed (commit `4ea0476`): it
  mostly restated Q3 concentration. `config_v2.BINS` is the vestigial leftover.
- **Gas as a flagship dimension** — dropped early: SSTORE-update gas is count-weighted by
  construction (constant ~5k/update), so it added nothing over a unique-count. EIP-2929
  (per-tx warm) and EIP-8188 (per-T-day active) are incompatible time scales.
- The interim **`account_first_op` GLOBAL JOIN** against `canonical_execution_transaction`
  (to recover tx_index for contracts/appearances) — replaced by dropping `contracts`
  (redundant with `nonce_diffs`) and giving `address_appearances` an end-of-block sort
  position. This is why `backfill_transaction.py` exists but is no longer needed.

---

## 9. Git / workflow notes

- Branch `feat/gas-analysis` is ahead of `main` by the commits listed via
  `git log --oneline main..HEAD`. No PR opened yet — the user has not asked to merge.
- The user prefers: descriptive commit bodies, no "vs original" framing in the report,
  the `T` (window) vs `W` (writes) notation discipline, and conservative/honest treatment
  of approximations (flag them, state the bias direction).
- Charts are regenerated from committed parquets — after any `analysis_v2.py` edit, run it
  and re-commit the PNGs.
- There are stale orphan branches (`feat/gas-state-access`, `feat/consolidated-cold-chart`)
  from earlier; ignore unless asked to prune.
