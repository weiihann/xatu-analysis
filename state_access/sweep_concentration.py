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
    """Top-1% / top-10% share of accesses for the W / R / R∪W sets, in one dict.

    Exact for "top ceil(f·N) keys": rows are aggregated into equal-access-count bands
    (keys within a band are interchangeable), and the band at the cutoff is filled
    partially. Deterministic and order-independent — per-row sorts instead include
    whole histogram rows at the cutoff, overshooting the key target by a
    sort-order-dependent amount (up to several pp when the cutoff lands inside a
    large tie band).
    """
    out: dict[str, float] = {}
    for access_type in ("W", "R", "RW_union"):
        sub, acc = _set_view(hist, access_type)
        if sub.empty:
            for name in _FRACTIONS:
                out[f"{name}_{access_type}"] = float("nan")
            continue
        acc_arr = acc.to_numpy()
        keys_arr = sub["n_keys"].to_numpy()
        neg_counts, inverse = np.unique(-acc_arr, return_inverse=True)
        band_keys = np.zeros(len(neg_counts), dtype=np.int64)
        np.add.at(band_keys, inverse, keys_arr)
        band_counts = -neg_counts
        cum_keys = band_keys.cumsum()
        cum_events = (band_counts * band_keys).cumsum()
        n_objects = int(cum_keys[-1])
        total = int(cum_events[-1])
        for name, frac in _FRACTIONS.items():
            target = math.ceil(frac * n_objects)
            idx = int(np.searchsorted(cum_keys, target, side="left"))
            prev_keys = int(cum_keys[idx - 1]) if idx else 0
            prev_events = int(cum_events[idx - 1]) if idx else 0
            events_at_target = prev_events + (target - prev_keys) * int(band_counts[idx])
            out[f"{name}_{access_type}"] = (
                float(events_at_target / total) if total else 0.0
            )
    return out
