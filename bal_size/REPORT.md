# BAL size over the trailing 6 months — findings

Reconstructed EIP-7928 Block-Level Access List component counts for **1,295,441 mainnet
blocks**, block 23,574,000 → 24,870,000 (the trailing ~6 months ending at the local node's
latest block). Size is reported in EIP-7928 **items** (`storage_keys + addresses`); the byte
figures are a derived nominal view (RLP type widths + 6.1% framing), discussed below.

## Headline

- **Median BAL ≈ 2,400 items per block; mean ≈ 2,540.** The distribution is right-skewed:
  p90 ≈ 4,060, p99 ≈ 5,740, max 15,616.
- **BALs sit well under the EIP-7928 item cap.** Even at today's ~30M-gas regime
  (cap = 15,000 items) the mean block uses **17%** of the cap and the p99 block **38%**. At a
  60M-gas limit those fall to 8.5% / 19%.
- **Storage dominates.** Storage writes + read-only reads are ~66% of derived BAL bytes,
  matching EIP-7928's own analysis almost exactly.

## Per-block size

| stat | items | KiB (derived) |
|---|---:|---:|
| mean | 2,537 | 140.5 |
| p50 | 2,398 | 131.7 |
| p90 | 4,064 | 229.8 |
| p99 | 5,743 | 320.0 |
| max | 15,616 | 580.8 |

### Headroom against the per-block item cap (`block_gas_limit / 2000`)

| gas limit | item cap | mean used | p99 used |
|---|---:|---:|---:|
| 30M | 15,000 | 16.9% | 38.3% |
| 36M | 18,000 | 14.1% | 31.9% |
| 60M | 30,000 | 8.5% | 19.1% |

The cap is not a binding constraint for typical blocks; it exists to bound adversarial worst
cases, which the observed max (15,616 items) approaches only at a 30M limit.

## Component breakdown (mean derived bytes per block)

| component | KiB | share | EIP-7928 cited share |
|---|---:|---:|---:|
| storage writes | 54.2 | 41% | 40.3% |
| storage reads (read-only) | 32.5 | 25% | 25.8% |
| balance diffs | 25.7 | 19% | 9.2% |
| addresses | 14.2 | 11% | ~15.5% |
| nonce diffs | 3.3 | 2% | 1.5% |
| code | 2.5 | 2% | 1.6% |

Storage write/read shares track the EIP's published breakdown closely. The balance-diff share
is overstated here (19% vs 9%): the derived byte view encodes every `Balance` at its full
`uint256` width (32 B), whereas RLP strips leading zeros and real balances encode in ~10–12 B.
The same nominal over-estimate inflates storage values. **This is why item counts, not derived
bytes, are the primary metric** — counts are exact (HLL ±1%); the byte view is a rough ceiling.

## Trend over the window

Per-block items are broadly flat at ~2,400–2,600 (median) with high per-block variance. Two
features stand out: a multi-day dip to ~2,000 around early December 2025, and a sharp rise in
touched addresses over the final ~weeks (block ~24.86M blocks average ~1,600 addresses vs
~600–750 earlier, with individual blocks reaching ~2,900) — an activity spike, not a
methodology artifact (the same block reports identically from both data sources).

## Data quality

- **Source split.** Write-side counts (storage write slots/entries, balance/nonce changes,
  contracts) came from the local `primary` node; read-only slots and the touched-address union
  from `ethpandaops` (the only cluster with the `*_reads` tables). Read-only slots use an exact
  per-block anti-join of read vs written `(address, slot)`, so slots both read and written are
  counted once (as writes), per the EIP.
- **Cross-check.** On 2,000 randomly sampled blocks, local vs ethpandaops storage write-slot
  counts agreed **within 1% on 99.9%** of blocks (mean abs diff 0.8 slots) — the local node's
  data, including the earlier backfilled gap, is consistent with the reference cluster.
- **Coverage.** 560 of 1,296,001 in-window blocks (0.04%) are absent from the merged dataset,
  scattered across the window with no systematic cluster; they do not affect the aggregates.

## Caveats

- Unique counts are HyperLogLog approximations (~1% error); the read-only anti-join is exact.
- Dates are the deterministic post-Merge projection (12 s/block; missed slots add drift),
  shared with the other analyses — they label the block axis, not wall-clock time.
- The byte view is nominal (type widths + 6.1% RLP framing), not the compressed on-wire size
  EIP-7928 cites (~72 KiB at 60M gas); treat it as an uncompressed upper estimate.
