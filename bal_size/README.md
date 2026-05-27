# bal_size — how big would EIP-7928 BALs be?

## Problem

[EIP-7928](https://eips.ethereum.org/EIPS/eip-7928) adds a **Block-Level Access List (BAL)**
to every block: a per-block, deduplicated record of all state touched during execution —
storage writes, read-only storage slots, balance/nonce changes, new contract code, and the
set of touched addresses. This analysis estimates how large those BALs would have been for
recent mainnet blocks, over the trailing ~6 months.

Size is measured in EIP-7928's own unit, **items** = `storage_keys + addresses`, which the
EIP caps per block at `block_gas_limit / ITEM_COST` (`ITEM_COST = 2000`). Per-component
counts are therefore the EIP's native sizing unit; a derived byte view is included as a
rough secondary (RLP type widths + ~6% framing overhead).

## Method

For each block in the window we reconstruct the BAL component counts from xatu's
per-transaction state-access tables:

| BAL component | derived from | per-block count |
|---|---|---|
| `AccountChanges` (addresses) | union of all 6 access tables | unique touched addresses |
| `storage_changes` slots | `storage_diffs` | unique `(address, slot)` |
| `storage_changes` entries | `storage_diffs` | unique `(address, slot, tx)` |
| `storage_reads` (read-only) | `storage_reads` **anti-join** `storage_diffs` | read slots not also written |
| `balance_changes` | `balance_diffs` | unique `(address, tx)` |
| `nonce_changes` | `nonce_diffs` | unique `(address, tx)` |
| code changes | `contracts` | new contracts + `n_code_bytes` |

Two subtleties matter for matching a real BAL:

- **Read-only ≠ all reads.** EIP-7928 puts a slot in exactly one of `storage_changes` or
  `storage_reads`; a slot both read and written belongs to the write list. xatu logs such a
  slot in *both* tables, so read-only slots are computed by an exact per-block **anti-join**
  of read `(address, slot)` against written `(address, slot)`.
- **Balance/nonce reads are not separate entries.** An account merely read contributes only
  its 20-byte address (an `AccountChanges` with empty change lists), so balance/nonce reads
  feed the address union but produce no separate count.

### Data sources

The `canonical_execution_*_reads` tables exist only on **ethpandaops**, so the read-side
(read-only slots, address union) is collected there. The write-side counts come from the
local **primary** node, which offloads the shared cluster and exercises the local data; a
random sample of blocks cross-checks local vs ethpandaops storage write slots. Unique counts
use `uniq` (HyperLogLog, ~1% error), matching the other analyses; the anti-join is exact.

### Window

Trailing ~6 months (`WINDOW_DAYS = 180`) ending at `END_BLOCK = 24,870,000`, the latest block
jointly available on the local node. Dates are the deterministic post-Merge projection
(12 s/block; missed slots add drift), shared with the other analyses via `history_config`.

## Run

```bash
uv run python -m bal_size.collect      # local -> epo -> merge -> crosscheck (resumable)
uv run python -m bal_size.analysis     # charts + read-out tables
```

`collect.py` checkpoints each chunk (a `.done.json` sidecar), so a re-run resumes.

## Outputs (`data/`)

| file | what |
|---|---|
| `bal_perblock.parquet` | one row per block: the BAL component counts (core artifact) |
| `bal_size_trend.png` | daily mean/median + p90 of items/block, and component means over time |
| `bal_size_distribution.png` | histogram of per-block items with p50/p90/p99 markers |
| `bal_component_breakdown.png` | mean bytes per component + component share over time |

Intermediate `_local_perblock.parquet` / `_epo_perblock.parquet` are the per-pass checkpoints.
