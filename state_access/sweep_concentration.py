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


def _set_view(hist: pd.DataFrame, access_type: str) -> tuple[pd.DataFrame, str]:
    """Filter histogram rows for the given access_type lens.

    Args:
        hist: DataFrame with columns (slice, n_w, n_r, n_keys).
        access_type: One of "W", "R", "RW_union".

    Returns:
        (filtered_DataFrame, access_column_name) where access_column_name is
        the column to use for sorting (either "n_w", "n_r", or a computed sum).
    """
    if access_type == "W":
        return hist[hist["slice"].isin(["w_only", "rw"])], "n_w"
    if access_type == "R":
        return hist[hist["slice"] == "r_only"], "n_r"
    # RW_union: computed on the fly
    return hist, None


def concentration_shares(hist: pd.DataFrame) -> dict[str, float]:
    """Top-1% / top-10% share of accesses for the W / R / R∪W sets, in one dict.

    Args:
        hist: DataFrame with columns (slice, n_w, n_r, n_keys).
              Rows are (access_per_key, value_count) pairs from a histogram.

    Returns:
        Dictionary with keys like "top1_W", "top10_W", "top1_R", "top10_R",
        "top1_RW_union", "top10_RW_union". Values are floats in [0, 1] or NaN
        if the set is empty.
    """
    out: dict[str, float] = {}
    for access_type in ("W", "R", "RW_union"):
        sub, col = _set_view(hist, access_type)
        if sub.empty:
            for name in _FRACTIONS:
                out[f"{name}_{access_type}"] = float("nan")
            continue
        # Materialize sub with access counts; sort descending by count (matches
        # pandas quicksort order to align with reference implementation).
        sub = sub.copy()
        if col is None:
            # RW_union: sum of n_w and n_r
            sub["_access_count"] = sub["n_w"] + sub["n_r"]
        else:
            # W or R: use the named column
            sub["_access_count"] = sub[col]
        sub = sub.sort_values("_access_count", ascending=False)

        # Cumulative sums: how many unique objects and total accesses up to each
        # histogram rank (sorted high→low).
        cum_keys = sub["n_keys"].cumsum()
        cum_accesses = (sub["_access_count"] * sub["n_keys"]).cumsum()

        n_objects = int(cum_keys.iloc[-1])
        total = int(cum_accesses.iloc[-1])

        for name, frac in _FRACTIONS.items():
            # Find the rank that covers the top-frac cutoff.
            target = math.ceil(frac * n_objects)
            # searchsorted(side="left") finds the first index where cum_keys ≥ target.
            idx = int(np.searchsorted(cum_keys.to_numpy(), target, side="left"))
            out[f"{name}_{access_type}"] = (
                float(cum_accesses.iloc[idx] / total) if total else 0.0
            )
    return out
