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
from clickhouse_connect.driver.exceptions import DatabaseError

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


def _q(sql: str) -> pd.DataFrame:
    """Heavy analysis query: 2h budget on both the query and the socket."""
    return run_query(sql, settings=HEAVY, send_receive_timeout=7200)


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
    slot = _q(q.slot_sweep_summary(anchor, window_days)).iloc[0]
    acct = _q(q.account_sweep_summary(anchor, window_days)).iloc[0]

    conc: dict = {}
    slot_hist = _q(q.slot_histogram(anchor, window_days))
    for k, v in concentration_shares(slot_hist).items():
        conc[f"slot_{k}"] = v
    acct_hist = _q(q.account_histogram(anchor, window_days))
    for k, v in concentration_shares(acct_hist).items():
        conc[f"acct_{k}"] = v

    upd = _q(q.slot_update_coverage(anchor, window_days)).iloc[0]
    sfo = _q(q.slot_first_op(anchor, window_days)).iloc[0]
    afo = _q(q.account_first_op(anchor, window_days)).iloc[0]
    res = _q(q.account_r_empty_split(anchor, window_days)).iloc[0]

    return build_row(anchor, window_days, slot=slot, acct=acct, conc=conc, upd=upd,
                     sfo=sfo, afo=afo, res=res, denom=_denominators(anchor))


def _fetch_with_retry(anchor: int, window_days: int) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _fetch_anchor(anchor, window_days)
        except DatabaseError as exc:
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
        tmp = parquet.with_suffix(".parquet.tmp")
        pd.DataFrame(rows).sort_values("anchor_block").to_parquet(tmp, index=False)
        tmp.replace(parquet)
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
