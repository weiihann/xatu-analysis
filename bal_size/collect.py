"""Collect per-block EIP-7928 BAL component counts over the trailing-6-month window.

Two resumable passes, each chunked into ``CHUNK_BLOCKS`` block ranges and checkpointed
after every chunk (a JSON sidecar records completed chunks, so a re-run skips them):

1. local pass (``primary``)      -> ``_local_perblock.parquet`` — write-side counts.
2. ethpandaops pass              -> ``_epo_perblock.parquet``   — addresses + read-only slots.

They are then merged on ``block_number`` into ``bal_perblock.parquet`` (the core artifact),
and a sample of blocks is cross-checked: local vs ethpandaops storage write slots.

Run:  uv run python -m bal_size.collect [local|epo|merge|crosscheck|all]
(no arg == all, in order).
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections.abc import Callable
from functools import reduce
from pathlib import Path

import pandas as pd
from clickhouse_connect.driver.exceptions import OperationalError

from bal_size import queries
from bal_size.config import (
    CHUNK_BLOCKS,
    COMPONENT_COLS,
    CROSSCHECK_SAMPLE,
    END_BLOCK,
    EPO_PARQUET,
    LOCAL_PARQUET,
    PERBLOCK_PARQUET,
    START_BLOCK,
    block_to_date,
)
from lib.clickhouse import run_query

MAX_ATTEMPTS = 6        # ride out transient node/Tailscale blips over a long run
RETRY_BASE_DELAY = 20   # seconds, multiplied by attempt number


def chunks() -> list[tuple[int, int]]:
    """Inclusive ``[lo, hi]`` block ranges covering the window, ascending."""
    out: list[tuple[int, int]] = []
    lo = START_BLOCK
    while lo <= END_BLOCK:
        out.append((lo, min(lo + CHUNK_BLOCKS - 1, END_BLOCK)))
        lo += CHUNK_BLOCKS
    return out


def _done_path(parquet: Path) -> Path:
    return parquet.with_suffix(".done.json")


def _load_done(parquet: Path) -> set[int]:
    path = _done_path(parquet)
    return set(json.loads(path.read_text())) if path.exists() else set()


def _save_done(parquet: Path, done: set[int]) -> None:
    _done_path(parquet).write_text(json.dumps(sorted(done)))


def _run_with_retry(label: str, fetch: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    """Run ``fetch``, retrying transient connection errors with linear backoff."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fetch()
        except OperationalError as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY * attempt
            print(f"  {label}: attempt {attempt} failed ({type(exc).__name__}); "
                  f"retry in {delay}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")  # loop either returns or raises


def _append_chunk(parquet: Path, new: pd.DataFrame) -> None:
    """Append a chunk's rows to the checkpoint parquet, deduped by block_number."""
    if parquet.exists():
        new = pd.concat([pd.read_parquet(parquet), new], ignore_index=True)
    new.drop_duplicates("block_number").sort_values("block_number").to_parquet(
        parquet, index=False
    )


def _collect_pass(
    parquet: Path,
    profile: str,
    fetch_chunk: Callable[[int, int], pd.DataFrame],
    label: str,
) -> None:
    """Run (or resume) a chunked collection pass into ``parquet``."""
    done = _load_done(parquet)
    todo = [c for c in chunks() if c[0] not in done]
    print(f"{label} pass [{profile}]: {len(todo)} chunks to collect "
          f"({len(done)} already done), writing {parquet.name}", flush=True)

    for i, (lo, hi) in enumerate(todo, 1):
        df = _run_with_retry(f"chunk {lo:,}-{hi:,}", lambda: fetch_chunk(lo, hi))
        _append_chunk(parquet, df)
        done.add(lo)
        _save_done(parquet, done)
        print(f"  [{i}/{len(todo)}] {lo:,}-{hi:,} {block_to_date(lo):%Y-%m-%d}: "
              f"{len(df):,} blocks", flush=True)
    print(f"{label} pass done.\n", flush=True)


def _local_chunk(lo: int, hi: int) -> pd.DataFrame:
    """Merge the four local write-side tables for one chunk into per-block rows."""
    parts = [
        run_query(queries.local_storage_perblock(lo, hi)),
        run_query(queries.local_balance_perblock(lo, hi)),
        run_query(queries.local_nonce_perblock(lo, hi)),
        run_query(queries.local_contracts_perblock(lo, hi)),
    ]
    merged = reduce(lambda a, b: a.merge(b, on="block_number", how="outer"), parts)
    cols = ["n_write_slots", "n_write_entries", "n_balance_changes",
            "n_nonce_changes", "n_contracts", "code_bytes"]
    return merged.fillna(0).astype({c: "int64" for c in cols})


def _epo_chunk(lo: int, hi: int) -> pd.DataFrame:
    """Per-block addresses + read-only slots for one chunk, from ethpandaops."""
    addr = run_query(queries.epo_addresses_perblock(lo, hi), profile="ethpandaops")
    ro = run_query(queries.epo_readonly_perblock(lo, hi), profile="ethpandaops")
    merged = addr.merge(ro, on="block_number", how="outer")
    return merged.fillna(0).astype({"n_addresses": "int64", "n_read_only_slots": "int64"})


def collect_local() -> None:
    _collect_pass(LOCAL_PARQUET, "primary", _local_chunk, "local")


def collect_epo() -> None:
    _collect_pass(EPO_PARQUET, "ethpandaops", _epo_chunk, "ethpandaops")


def merge_final() -> None:
    """Join the two passes on block_number into the core per-block artifact."""
    local = pd.read_parquet(LOCAL_PARQUET)
    epo = pd.read_parquet(EPO_PARQUET)
    df = local.merge(epo, on="block_number", how="outer").fillna(0)
    df = df.astype({c: "int64" for c in COMPONENT_COLS})
    df = df.sort_values("block_number").reset_index(drop=True)
    df["date"] = [block_to_date(b) for b in df["block_number"]]
    df.to_parquet(PERBLOCK_PARQUET, index=False)
    print(f"Merged {len(df):,} blocks ({df['block_number'].min():,}-"
          f"{df['block_number'].max():,}) -> {PERBLOCK_PARQUET.name}", flush=True)
    missing = [c for c in COMPONENT_COLS if df[c].isna().any()]
    if missing:
        raise RuntimeError(f"null component values after merge in: {missing}")


def crosscheck() -> None:
    """Compare local vs ethpandaops storage write slots on a random block sample."""
    local = pd.read_parquet(LOCAL_PARQUET)[["block_number", "n_write_slots"]]
    rng = random.Random(7928)
    sample = sorted(rng.sample(list(local["block_number"]),
                               min(CROSSCHECK_SAMPLE, len(local))))
    epo = run_query(queries.write_slots_for_blocks(sample), profile="ethpandaops")
    merged = local[local["block_number"].isin(sample)].merge(
        epo, on="block_number", how="inner", suffixes=("_local", "_epo")
    )
    diff = (merged["n_write_slots_local"] - merged["n_write_slots_epo"]).abs()
    denom = merged["n_write_slots_epo"].clip(lower=1)
    within_1pct = float((diff / denom <= 0.01).mean()) * 100
    print(f"Cross-check on {len(merged):,} sampled blocks: "
          f"{within_1pct:.1f}% within 1% (HLL error); "
          f"mean abs diff {diff.mean():.1f} slots, max {int(diff.max())}", flush=True)


_STEPS = {
    "local": collect_local,
    "epo": collect_epo,
    "merge": merge_final,
    "crosscheck": crosscheck,
}


def main(steps: list[str]) -> None:
    chosen = steps if steps and steps != ["all"] else list(_STEPS)
    for step in chosen:
        _STEPS[step]()


if __name__ == "__main__":
    main(sys.argv[1:])
