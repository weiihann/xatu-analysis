# Write/read structure as composition-over-time — design

2026-06-18 · branch `feat/gas-analysis` · refactors §4.1 / §4.2 of `state_access/REPORT_v2.md`

## Context

§4.1 and §4.2 present the slot write/read transition breakdown **only at the latest
anchor**, as **% of live state**, in a two-level structure (a 4-way partition table, then a
separate 6-way W_mixed decomposition). We have the full transition partition for **every
window at every one of the 648 swept anchors** (`sweep_summary.parquet`), so the breakdown
should be shown **over time**, as a **share of the access set** (not of live state), with the
mixed sub-types merged into one flat class list.

Decisions settled in brainstorming:
- **Writes:** one **7-class lifecycle partition of |W|** — `create-only`, `update-only`,
  `delete-only`, `C+U`, `C+D`, `U+D`, `C+U+D` — collapsing the 1-cycle/multi-cycle splits
  (`C+D = mixed_cd1+mixed_cdm`, `C+U+D = mixed_cud1+mixed_cudm`). Shares sum to 100% of |W|.
- **Reads:** the parallel **2-class partition of |R|** — `zero-only`, `nonzero-only`
  (`R_mixed ≈ 0`, noted not stacked). Shares sum to ~100% of |R|.
- **Denominator is the access set** (|W| / |R|), not live state.
- **Primary view is over time**: a 4-panel stacked-area chart (one panel per swept window
  T = 30/90/180/365), x = anchor date, y = % of the set, fork lines.
- **Keep a compact latest-anchor reference table** (exact current numbers) alongside the
  chart.
- **Mirror the structure across §4.1 and §4.2** so both read: intro → full-history events →
  composition (table + over-time chart). (§4.1 already leads with full-history events; move
  §4.2's full-history read events to the front to match.)
- No new data collection; uses committed parquets.

## Target structure (both subsections parallel)

**§4.1 Write structure**
1. intro — define the 7 lifecycle classes (born / grown / died framing), replacing the
   "pure types then decompose mixed" two-level text.
2. `#### Write events over the entire chain history` — unchanged (already at top).
3. `#### Write structure — the lifecycle of a written slot`
   - lead sentence (composition is a share of |W|, shown over time)
   - **latest-anchor table**: 7 classes as **% of |W|** across windows (from the committed
     `q1_warmth_slot_typed` parquet — 8 windows — collapsed to the 7 classes).
   - **`sweep_write_composition.png`**: 4-panel stacked area, 7 bands, % of |W|, over time.
   - readings (2–3): create-dominance, the update-only/C+U shift over time, ephemeral C+D.
   - short policy bridge: any-update ≈ 21% of |W| → ~5.4% of state (the only place % of live
     state appears, via §5.1's |W|), feeding §6.

**§4.2 Read structure** (same shape)
1. intro — zero vs nonzero reads.
2. `#### Read events over the entire chain history` — **moved to front** to match §4.1.
3. `#### Read structure — what reads return`
   - latest-anchor table: `zero-only` / `nonzero-only` as **% of |R|** across windows.
   - **`sweep_read_composition.png`**: 4-panel stacked area, 2 bands, % of |R|, over time.
   - readings: empty-probe dominance, the short-window upward drift, long-window flatness.

**Removed/replaced** (subsumed): the "% of live state" write partition table; the standalone
W_mixed 6-way tables; the "% of live state" read partition table; and the old sweep charts
`sweep_write_structure.png`, `sweep_mixed_decomp.png`, `sweep_read_structure.png`.

## Files

- `state_access/analysis_v2_sweep.py` — add `render_write_composition(df)` and
  `render_read_composition(df)` (4-panel `make_subplots` stacked areas via the existing
  process-isolated `write_image_safe`); drop the three retired charts from `render_all`;
  add a `verify_composition(df)` check (7 write classes sum to slot_W; 2 read classes +
  R_mixed sum to slot_R).
- `state_access/REPORT_v2.md` — rewrite §4.1 / §4.2 per the structure above.
- `state_access/HANDOVER_v2.md` — update the Appendix-B chart list.

## Derived classes (from `sweep_summary` columns)

```
write: create_only = slot_W_only_create ; update_only = slot_W_only_update ;
       delete_only = slot_W_only_delete ; CU = slot_mixed_cu ;
       CD = slot_mixed_cd1 + slot_mixed_cdm ; UD = slot_mixed_ud ;
       CUD = slot_mixed_cud1 + slot_mixed_cudm              # ÷ slot_W
read:  zero_only = slot_R_only_zero ; nonzero_only = slot_R_only_nonzero  # ÷ slot_R
```

Band/stack order (born → grown → modified → died), fixed colors per class so all 4 panels
share a legend.

## Verification

1. Per cell: the 7 write shares sum to 1.000 (±1e-9); `zero_only+nonzero_only+R_mixed = slot_R`.
2. Latest-anchor table cross-checks the committed `q1_warmth_slot_typed` parquet
   (create-only/W, update-only/W, … match; CD == (cd1+cdm)).
3. `analysis_v2_sweep.main()` runs clean; both new PNGs render; retired PNGs removed.
4. Report integrity: every other table-row unchanged (sorted-multiset diff vs pre-edit);
   pandoc parses; no `§` dangling refs.

## Out of scope

- New DB collection (event-based shares would need a per-window event sweep — not this).
- Account-side composition (accounts have no transition partition in the sweep).
- Touch-rate ("any create/update/delete") over-time chart — the key 21% number stays in
  prose only.
