"""Derive Q1/Q2/Q3 from the v2 histograms and render charts.

Reads `data/v2/{slot,account}_histogram.parquet` (produced by `collect_v2`), each row of
which is `(window_days, slice ∈ {w_only, r_only, rw}, n_w, n_r, n_keys)`. From these:

- **Q1 Warmth**: per `(access_type, W, object_type)` unique-set sizes |W|, |R|, |R∩W|,
  |R∪W|, |W-only|, |R-only|. Line chart, x=W, y=count, one line per access type.
- **Q2 Composition**: per `(slice, W, object_type)` per-bin share of the slice's objects.
  Stacked bar, x=W, y=share, stacked by `BINS`.
- **Q3 Concentration**: per `(access_type, W, object_type)` share of accesses captured by
  the top-1% and top-10% of objects (by access count). Line chart.

    uv run python -m state_access.analysis_v2
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Literal

import pandas as pd
import plotly.graph_objs as go
from plotly.subplots import make_subplots

from state_access.config import DATA_DIR
from state_access.config_v2 import BINS, DATA_DIR_V2, WINDOWS_V2

# Live-state denominators come from `data/totals.json` (produced by the original collect.py
# against the same anchor). We use them to express Q1 as a percentage of state size — the
# tier-relevant framing — alongside the absolute counts.
_TOTALS_PATH = DATA_DIR / "totals.json"


def _load_totals() -> dict[str, int]:
    if not _TOTALS_PATH.exists():
        raise RuntimeError(
            f"{_TOTALS_PATH} not found — run `uv run python -m state_access.collect` "
            "first so the live-state totals are available."
        )
    with _TOTALS_PATH.open() as f:
        return json.load(f)

#
# Two-set view.
#
# Sources are partitioned so the sets are disjoint, additive, and named for what they mean:
#   W   = objects that appear in `_diffs` tables in window (RAW — no dedup against reads).
#         This matches the original analysis's "warm" definition exactly.
#   R   = objects that appear in `_reads` / filtered `address_appearances` AND NOT in
#         `_diffs` in the same window (deduped against writes). The pure-read extra that
#         the reads data unlocks beyond the original analysis.
#   R∪W = W + R (additive, since W and R are disjoint by construction). The full warm set.
#
# Why this framing: empirically W ⊂ R-with-overlap because every SSTORE is preceded by an
# SLOAD at bytecode level (Solidity's "x = f(x)" codegen), and every tx_from has its nonce
# read for tx validation. So a partition like (W-only, R-only, R∩W) collapses with
# W-only ≈ 0 always; the two-set additive view captures the same information without the
# misleading near-zero W-only slice.
ACCESS_TYPES = ["W", "R", "RW_union"]
ACCESS_COLORS = {
    "W":         "#1565C0",  # original "warm"
    "R":         "#C2185B",  # what reads add (= old R-only)
    "RW_union":  "#455A64",  # total warm set
}
ACCESS_DASH = {
    "W":         "solid",
    "R":         "dash",
    "RW_union":  "solid",
}
SLICE_LABEL = {"W": "W (writes)", "R": "R (reads-not-written)"}
BIN_COLORS = ["#E0E0E0", "#90CAF9", "#42A5F5", "#1565C0", "#0D47A1"]


def _bin_label(count: int) -> str:
    for lo, hi, label in BINS:
        if count >= lo and (hi is None or count <= hi):
            return label
    raise ValueError(f"count {count} fell outside all bins")


def _access_count(row: pd.Series, access_type: str) -> int:
    """How many accesses this `(n_w, n_r)` key contributes under a given access_type lens.

    - W counts WRITE events only (`n_w`), restricted to keys in W.
    - R counts READ events only (`n_r`), restricted to R-deduped keys (slice='r_only').
    - R∪W counts all events for any touched key (`n_w + n_r`).
    """
    n_w, n_r = int(row["n_w"]), int(row["n_r"])
    if access_type == "W":
        return n_w if row["slice"] in ("w_only", "rw") else 0
    if access_type == "R":
        return n_r if row["slice"] == "r_only" else 0
    if access_type == "RW_union":
        return n_w + n_r
    raise ValueError(access_type)


def _key_in_set(row: pd.Series, access_type: str) -> bool:
    """Whether keys in this histogram row belong to the named access-type set."""
    if access_type == "W":
        return row["slice"] in ("w_only", "rw")
    if access_type == "R":
        return row["slice"] == "r_only"
    if access_type == "RW_union":
        return True
    raise ValueError(access_type)


def q1_warmth(hist: pd.DataFrame, total_live: int | None = None) -> pd.DataFrame:
    """Set sizes |W|, |R|, |R∪W| per window under the additive 2-set partition.

    W = objects in `_diffs` (raw — matches original analysis); R = objects in `_reads`
    deduped against W; R∪W = W + R (disjoint).

    If `total_live` is given, also emits `<set>_pct` columns (each set's share of the
    live-state denominator) and a `total_live` column.
    """
    rows = []
    for w in sorted(hist["window_days"].unique()):
        sub = hist[hist["window_days"] == w]
        slice_keys = sub.groupby("slice")["n_keys"].sum().to_dict()
        w_only = int(slice_keys.get("w_only", 0))
        r_only = int(slice_keys.get("r_only", 0))
        rw = int(slice_keys.get("rw", 0))
        rows.append({
            "window_days": int(w),
            "W":         w_only + rw,
            "R":         r_only,
            "RW_union":  w_only + r_only + rw,
        })
    out = pd.DataFrame(rows)
    if total_live is not None:
        out["total_live"] = int(total_live)
        for at in ACCESS_TYPES:
            out[f"{at}_pct"] = 100 * out[at] / int(total_live)
    return out


def q1_warmth_combined(slot_hist: pd.DataFrame, account_hist: pd.DataFrame,
                       totals: dict[str, int]) -> pd.DataFrame:
    """Sum slot + account warmth per window; denominator is the sum of live totals."""
    q1_s = q1_warmth(slot_hist)
    q1_a = q1_warmth(account_hist)
    merged = q1_s.merge(q1_a, on="window_days", suffixes=("_slot", "_account"))
    total_live = int(totals["storages"]) + int(totals["accounts"])
    out = pd.DataFrame({"window_days": merged["window_days"]})
    out["total_live"] = total_live
    for at in ACCESS_TYPES:
        out[at] = merged[f"{at}_slot"] + merged[f"{at}_account"]
        out[f"{at}_pct"] = 100 * out[at] / total_live
    return out


def q2_composition(hist: pd.DataFrame) -> pd.DataFrame:
    """Per (set, W, bin) the count of objects and total accesses in that bin.

    Under the 2-set additive partition:
        set = "W" → keys with slice ∈ {w_only, rw}, access count = n_w (writes only).
        set = "R" → keys with slice = r_only,       access count = n_r (reads only).
    """
    rows = []
    for w in sorted(hist["window_days"].unique()):
        sub_w = hist[hist["window_days"] == w]
        for set_name, mask, counts_col in [
            ("W", sub_w["slice"].isin(["w_only", "rw"]), "n_w"),
            ("R", sub_w["slice"] == "r_only",            "n_r"),
        ]:
            sub = sub_w[mask]
            if sub.empty:
                continue
            counts = sub[counts_col]
            binned = pd.Series([_bin_label(int(c)) for c in counts], index=sub.index)
            df = pd.DataFrame({
                "bin": binned,
                "n_keys": sub["n_keys"].values,
                "n_accesses": (counts * sub["n_keys"]).values,
            })
            agg = df.groupby("bin", as_index=False).sum()
            agg["slice"] = set_name
            agg["window_days"] = int(w)
            rows.append(agg)
    out = pd.concat(rows, ignore_index=True)
    return out[["window_days", "slice", "bin", "n_keys", "n_accesses"]]


def q3_concentration(hist: pd.DataFrame, fractions: Iterable[float] = (0.01, 0.10)) -> pd.DataFrame:
    """Top-N fraction of objects → share of accesses, per (access_type, W).

    Walks the histogram from the high-access-count tail. The denominator is the *accessed*
    set under the access_type lens (so e.g. for `W_only`, only w_only rows count).
    """
    rows = []
    for w in sorted(hist["window_days"].unique()):
        sub_w = hist[hist["window_days"] == w]
        for access_type in ACCESS_TYPES:
            mask = sub_w.apply(lambda r: _key_in_set(r, access_type), axis=1)
            sub = sub_w[mask].copy()
            if sub.empty:
                row = {"window_days": int(w), "access_type": access_type, "n_objects": 0,
                       "total_accesses": 0}
                for f in fractions:
                    row[f"top_{int(f * 100)}pct_share"] = float("nan")
                rows.append(row)
                continue
            sub["access_per_key"] = sub.apply(lambda r: _access_count(r, access_type), axis=1)
            sub = sub.sort_values("access_per_key", ascending=False)
            sub["cum_keys"] = sub["n_keys"].cumsum()
            sub["cum_accesses"] = (sub["access_per_key"] * sub["n_keys"]).cumsum()
            n_objects = int(sub["n_keys"].sum())
            total_accesses = int((sub["access_per_key"] * sub["n_keys"]).sum())
            row = {"window_days": int(w), "access_type": access_type,
                   "n_objects": n_objects, "total_accesses": total_accesses}
            for f in fractions:
                target = math.ceil(f * n_objects)
                # Cumulative table is sorted high→low; pick the first row whose cum_keys ≥ target.
                hit = sub[sub["cum_keys"] >= target]
                if hit.empty:
                    share = float("nan")
                else:
                    share = hit.iloc[0]["cum_accesses"] / total_accesses if total_accesses else 0.0
                row[f"top_{int(f * 100)}pct_share"] = float(share)
            rows.append(row)
    return pd.DataFrame(rows)


def _plot_warmth(q1: pd.DataFrame, object_type: str, total_live: int) -> go.Figure:
    """Three additive lines on % of live state: W (writes), R (reads-not-written), R∪W."""
    fig = go.Figure()
    for at in ACCESS_TYPES:
        fig.add_trace(go.Scatter(
            x=q1["window_days"], y=q1[f"{at}_pct"], name=at,
            mode="lines+markers",
            line=dict(color=ACCESS_COLORS[at], width=2.5, dash=ACCESS_DASH[at]),
        ))
    label = "live state" if object_type == "combined" else f"live {object_type}s"
    fig.update_layout(
        title=f"Q1 — Warmth: {label} touched, by access set and window"
              f"<br><sub>denominator = {total_live:,}; "
              f"W = writes (matches the original 'warm'); "
              f"R = pure reads added on top; R∪W = full warm set</sub>",
        xaxis=dict(title="window W (days)", type="log", gridcolor="lightgray"),
        yaxis=dict(title=f"% of {label}", ticksuffix="%",
                   gridcolor="lightgray", rangemode="tozero"),
        template="plotly_white", width=1100, height=600,
        legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.85)"),
    )
    return fig


def _plot_composition(q2: pd.DataFrame, object_type: str) -> go.Figure:
    """Stacked bar per slice: x is categorical W, y is share of slice's objects in each bin.

    Categorical x rather than log: stacked bars on a log axis collapse to slivers at the
    large-W end. With categories the bars are evenly spaced and readable.
    """
    bin_labels = [b[2] for b in BINS]
    slices_in_order = ["W", "R"]
    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=[SLICE_LABEL[s] for s in slices_in_order],
        shared_yaxes=True,
    )
    w_categories = [str(w) for w in sorted(q2["window_days"].unique())]
    for col, sl in enumerate(slices_in_order, start=1):
        sub = q2[q2["slice"] == sl].copy()
        slice_totals = sub.groupby("window_days")["n_keys"].sum()
        sub["share"] = sub.apply(
            lambda r: 100 * r["n_keys"] / slice_totals.get(r["window_days"], 1),
            axis=1,
        )
        pivot = sub.pivot(index="window_days", columns="bin", values="share").fillna(0)
        pivot = pivot.reindex(columns=bin_labels, fill_value=0)
        # Cast index to string so plotly treats x as categorical.
        x_cat = [str(w) for w in pivot.index]
        for i, bin_label in enumerate(bin_labels):
            fig.add_trace(go.Bar(
                x=x_cat, y=pivot[bin_label],
                name=bin_label, legendgroup=bin_label,
                showlegend=(col == 1),
                marker=dict(color=BIN_COLORS[i]),
            ), row=1, col=col)
    fig.update_layout(
        barmode="stack",
        title=f"Q2 — Composition: per-{object_type} access-count bins, by set and window"
              f"<br><sub>W bins by write events per object; R bins by read events per object</sub>",
        template="plotly_white", width=1200, height=560,
        legend=dict(orientation="h", x=0.5, y=-0.15, xanchor="center"),
    )
    for c in range(1, 3):
        fig.update_xaxes(title="W (days)", type="category", categoryorder="array",
                         categoryarray=w_categories, row=1, col=c)
    fig.update_yaxes(title="% of set's objects", ticksuffix="%", row=1, col=1)
    return fig


def _plot_concentration(q3: pd.DataFrame, object_type: str, which: Literal["top_1pct_share", "top_10pct_share"]) -> go.Figure:
    fig = go.Figure()
    for at in ACCESS_TYPES:
        sub = q3[q3["access_type"] == at].sort_values("window_days")
        fig.add_trace(go.Scatter(
            x=sub["window_days"], y=100 * sub[which],
            name=at, mode="lines+markers",
            line=dict(color=ACCESS_COLORS[at], width=2.5, dash=ACCESS_DASH[at]),
        ))
    label = "top 1%" if which == "top_1pct_share" else "top 10%"
    fig.update_layout(
        title=f"Q3 — Concentration: {label} of {object_type}s capture X% of accesses",
        xaxis=dict(title="window W (days)", type="log", gridcolor="lightgray"),
        yaxis=dict(title=f"share of accesses ({label} of objects)", ticksuffix="%",
                   gridcolor="lightgray", range=[0, 100]),
        template="plotly_white", width=1100, height=580,
        legend=dict(x=0.99, y=0.02, xanchor="right", bgcolor="rgba(255,255,255,0.85)"),
    )
    return fig


def _show(fig: go.Figure) -> None:
    """Open interactively when a renderer is configured; otherwise the PNG is the artifact."""
    import os
    if os.environ.get("STATE_ACCESS_INTERACTIVE"):
        try:
            fig.show()
        except (ValueError, BrokenPipeError):
            pass


_TOTAL_KEY = {"slot": "storages", "account": "accounts"}


def run_one(object_type: str, totals: dict[str, int]) -> None:
    p = DATA_DIR_V2 / f"{object_type}_histogram.parquet"
    if not p.exists():
        print(f"  no histogram at {p}; run collect_v2 first")
        return
    hist = pd.read_parquet(p)
    print(f"\n>>> {object_type}: {len(hist):,} histogram rows over "
          f"{hist['window_days'].nunique()} windows")

    total_live = int(totals[_TOTAL_KEY[object_type]])
    q1 = q1_warmth(hist, total_live=total_live)
    q2 = q2_composition(hist)
    q3 = q3_concentration(hist)

    q1.to_parquet(DATA_DIR_V2 / f"q1_warmth_{object_type}.parquet", index=False)
    q2.to_parquet(DATA_DIR_V2 / f"q2_composition_{object_type}.parquet", index=False)
    q3.to_parquet(DATA_DIR_V2 / f"q3_concentration_{object_type}.parquet", index=False)

    # Verification checks. Additive partition: |R∪W| = |W| + |R| (disjoint by construction).
    diff = (q1["RW_union"] - (q1["W"] + q1["R"])).abs().max()
    assert diff == 0, f"additive identity |R∪W|=|W|+|R| broken by {diff}"
    assert (q1["W"] >= 0).all() and (q1["R"] >= 0).all()
    assert (q1["RW_union"] <= total_live).all(), "warm set exceeds live state"
    valid = q3.dropna(subset=["top_1pct_share", "top_10pct_share"])
    assert (valid["top_10pct_share"] >= valid["top_1pct_share"]).all(), \
        "top-10% must be ≥ top-1%"

    fig1 = _plot_warmth(q1, object_type, total_live)
    fig1.write_image(DATA_DIR_V2 / f"q1_warmth_{object_type}.png", scale=2)
    _show(fig1)

    fig2 = _plot_composition(q2, object_type)
    fig2.write_image(DATA_DIR_V2 / f"q2_composition_{object_type}.png", scale=2)
    _show(fig2)

    fig3a = _plot_concentration(q3, object_type, "top_1pct_share")
    fig3a.write_image(DATA_DIR_V2 / f"q3_concentration_top1_{object_type}.png", scale=2)
    _show(fig3a)

    fig3b = _plot_concentration(q3, object_type, "top_10pct_share")
    fig3b.write_image(DATA_DIR_V2 / f"q3_concentration_top10_{object_type}.png", scale=2)
    _show(fig3b)


def run_combined(totals: dict[str, int]) -> None:
    """Q1-only chart pooling slots + accounts as 'state objects'.

    Denominator: live storages + live accounts. The combined view is the natural
    tier-policy denominator: "what fraction of all state objects (slots ∪ accounts) ends
    up Active under each W?".
    """
    slot_p = DATA_DIR_V2 / "slot_histogram.parquet"
    account_p = DATA_DIR_V2 / "account_histogram.parquet"
    if not (slot_p.exists() and account_p.exists()):
        print("  combined chart needs both slot + account histograms; skipping")
        return
    slot_hist = pd.read_parquet(slot_p)
    account_hist = pd.read_parquet(account_p)
    q1c = q1_warmth_combined(slot_hist, account_hist, totals)
    q1c.to_parquet(DATA_DIR_V2 / "q1_warmth_combined.parquet", index=False)

    total_live = int(q1c["total_live"].iloc[0])
    diff = (q1c["RW_union"] - (q1c["W"] + q1c["R"])).abs().max()
    assert diff == 0, f"combined identity |R∪W|=|W|+|R| broken by {diff}"
    assert (q1c["RW_union"] <= total_live).all(), "combined warm set exceeds total state"

    fig = _plot_warmth(q1c, "combined", total_live)
    fig.write_image(DATA_DIR_V2 / "q1_warmth_combined.png", scale=2)
    _show(fig)
    print(f"  combined: warm-set share ranges "
          f"{q1c['RW_union_pct'].min():.2f}% (W={q1c.iloc[0]['window_days']}d) → "
          f"{q1c['RW_union_pct'].max():.2f}% (W={q1c.iloc[-1]['window_days']}d)")


def main() -> None:
    DATA_DIR_V2.mkdir(parents=True, exist_ok=True)
    totals = _load_totals()
    print(f"Live-state totals @ block {totals['snapshot_block']:,}: "
          f"{totals['accounts']:,} accounts, {totals['storages']:,} slots")
    for object_type in ("slot", "account"):
        run_one(object_type, totals)
    print("\n>>> combined")
    run_combined(totals)
    print(f"\nDone. Charts + Q-parquets under {DATA_DIR_V2}")


if __name__ == "__main__":
    main()
