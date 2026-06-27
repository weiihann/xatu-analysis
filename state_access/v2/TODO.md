# state_access — deferred work

## Created-and-read: genuine read-back on creation-class slots (PAUSED)

**What:** For *created* storage slots, measure how many are also read back with a populated
value in a **different transaction** from the write — "created once, then its value read
elsewhere." Restricted to the two creation classes: **C** (create-only) and **C+D**
(created + deleted, ephemeral). The write's own coupled `SLOAD` is excluded (a genuine read
= nonzero read in a tx that did not write the slot).

**Status:** query + collector built, committed, and **validated**; full sweep not run
(too slow to finish in-session — parked here).

- Query: `queries_v2.slot_creation_reads(bn_now, days)` — `(tx, slot)`-grain, join-free
  (`maxIf(rnz, is_w = 0)` is the genuine-read test). Commit `fb07515`.
- Collector: `collect_v2_creation_reads.py` → `data/v2/creation_reads.parquet`
  (resumable per `(anchor, window)`, newest-first, atomic checkpoint, HEAVY spill,
  `DatabaseError` retry). Additive — does NOT touch `slot_sweep_summary` / `sweep_summary`.
- Validated at the snapshot anchor (24,870,000): `c_only` == `sweep_summary`
  `slot_W_only_create` and `cd` == `slot_mixed_cd1 + slot_mixed_cdm`, **exact**, at T=30
  and T=90. Read-back shares so far: C ≈ 9.6% (T=30) / 12.1% (T=90); C+D ≈ 3.2% / 3.5%.
- `creation_reads.parquet` currently holds the T=30 and T=90 snapshot-anchor cells (the
  collector will skip them on resume).

**To finish:**
1. Complete the gate: run the snapshot anchor at T=180 and T=365 too —
   `LIMIT=1 uv run python -m state_access.v2.collect_creation_reads 180 365`. **T=365 at
   `(tx, slot)` grain is the heaviest query in the project (~28 min/anchor) and the OOM
   risk** — this is the go/no-go for the sweep.
2. If the gate holds, run the full weekly sweep (no `LIMIT`): ~**5 days** of cluster time
   (~124h: ≈7h/19h/35h/63h for T=30/90/180/365), resumable. Wrap it in a resilient
   relaunch loop (the node OOM-crashes under heavy scans; see the earlier
   `_resume_sweep_365.sh` pattern) so it survives restarts unattended.
3. Presentation (deferred, after data lands): fold the "created and read back" split into
   §4's write structure in `REPORT_v2.md` and decide the chart. Spec:
   `docs/superpowers/specs/2026-06-18-write-read-composition-over-time-design.md` neighborhood
   — write a small analysis_v2_sweep renderer + report subsection.

**Why paused:** the full sweep is multi-day; not worth blocking a session on. Pick up by
finishing the gate (step 1) when the cluster is free.
