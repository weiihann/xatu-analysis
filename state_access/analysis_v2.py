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

ACCESS_TYPES = ["W", "R", "RW_intersect", "RW_union", "W_only", "R_only"]
ACCESS_COLORS = {
    "W":            "#1565C0",
    "R":            "#2E7D32",
    "RW_intersect": "#6A1B9A",
    "RW_union":     "#455A64",
    "W_only":       "#EF6C00",
    "R_only":       "#C2185B",
}
ACCESS_DASH = {
    "W":            "solid",
    "R":            "solid",
    "RW_intersect": "solid",
    "RW_union":     "solid",
    "W_only":       "dash",
    "R_only":       "dash",
}
SLICE_LABEL = {"w_only": "W-only", "r_only": "R-only", "rw": "R∩W"}
BIN_COLORS = ["#E0E0E0", "#90CAF9", "#42A5F5", "#1565C0", "#0D47A1"]


def _bin_label(count: int) -> str:
    for lo, hi, label in BINS:
        if count >= lo and (hi is None or count <= hi):
            return label
    raise ValueError(f"count {count} fell outside all bins")


def _access_count(row: pd.Series, access_type: str) -> int:
    """How many accesses this `(n_w, n_r)` key contributes under a given access_type lens."""
    n_w, n_r = int(row["n_w"]), int(row["n_r"])
    if access_type == "W":
        return n_w
    if access_type == "R":
        return n_r
    if access_type == "W_only":
        return n_w if row["slice"] == "w_only" else 0
    if access_type == "R_only":
        return n_r if row["slice"] == "r_only" else 0
    if access_type == "RW_intersect":
        return n_w + n_r if row["slice"] == "rw" else 0
    if access_type == "RW_union":
        return n_w + n_r
    raise ValueError(access_type)


def _key_in_set(row: pd.Series, access_type: str) -> bool:
    """Whether keys in this histogram row belong to the named access-type set."""
    if access_type == "W":
        return row["slice"] in ("w_only", "rw")
    if access_type == "R":
        return row["slice"] in ("r_only", "rw")
    if access_type == "RW_intersect":
        return row["slice"] == "rw"
    if access_type == "RW_union":
        return True
    if access_type == "W_only":
        return row["slice"] == "w_only"
    if access_type == "R_only":
        return row["slice"] == "r_only"
    raise ValueError(access_type)


def q1_warmth(hist: pd.DataFrame, total_live: int | None = None) -> pd.DataFrame:
    """Set sizes |W|, |R|, |R∩W|, |R∪W|, |W-only|, |R-only| per window.

    If `total_live` is given, also emits `<access_type>_pct` columns (each set's share of
    the live-state denominator) and a `total_live` column. The pct view is the
    tier-relevant framing — what fraction of live state ends up Active under a given W.
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
            "W":            w_only + rw,
            "R":            r_only + rw,
            "RW_intersect": rw,
            "RW_union":     w_only + r_only + rw,
            "W_only":       w_only,
            "R_only":       r_only,
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
    """Per (slice, W, bin) the count of objects and total accesses in that bin.

    Access count for binning: the *natural* access count for the slice.
        w_only → n_w
        r_only → n_r
        rw     → n_w + n_r
    """
    rows = []
    for (slice_, w), sub in hist.groupby(["slice", "window_days"]):
        if slice_ == "w_only":
            counts = sub["n_w"]
        elif slice_ == "r_only":
            counts = sub["n_r"]
        else:
            counts = sub["n_w"] + sub["n_r"]
        binned = pd.Series([_bin_label(int(c)) for c in counts], index=sub.index)
        df = pd.DataFrame({
            "bin": binned,
            "n_keys": sub["n_keys"].values,
            "n_accesses": (counts * sub["n_keys"]).values,
        })
        agg = df.groupby("bin", as_index=False).sum()
        agg["slice"] = slice_
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
    """4 lines: the 3 disjoint partition pieces + the union, plotted as % of live state.

    Plot W, R, RW_intersect, RW_union too as derived quantities — but in practice |W| ≈
    |R∩W| for both slots and accounts on every window (every written object is also read
    in the same window), so a 6-line chart has heavy overlap. The 3-partition view is the
    information-bearing one. Y-axis is % of live state under EIP-8188's denominator, so
    "R∪W at W=30 = 4.2%" reads directly as "the Active tier under that policy is 4.2% of
    state".
    """
    fig = go.Figure()
    primary = ["W_only", "R_only", "RW_intersect", "RW_union"]
    for at in primary:
        fig.add_trace(go.Scatter(
            x=q1["window_days"], y=q1[f"{at}_pct"], name=at,
            mode="lines+markers",
            line=dict(color=ACCESS_COLORS[at], width=2.5, dash=ACCESS_DASH[at]),
        ))
    label = "live state" if object_type == "combined" else f"live {object_type}s"
    fig.update_layout(
        title=f"Q1 — Warmth: {label} touched, by access partition and window"
              f"<br><sub>denominator = {total_live:,}; R∪W is the warm set; "
              f"R-only / W-only / R∩W partition it</sub>",
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
    slices_in_order = ["w_only", "r_only", "rw"]
    fig = make_subplots(
        rows=1, cols=3,
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
        title=f"Q2 — Composition: per-{object_type} access-count bins, per slice and window",
        template="plotly_white", width=1400, height=560,
        legend=dict(orientation="h", x=0.5, y=-0.15, xanchor="center"),
    )
    for c in range(1, 4):
        fig.update_xaxes(title="W (days)", type="category", categoryorder="array",
                         categoryarray=w_categories, row=1, col=c)
    fig.update_yaxes(title="% of slice's objects", ticksuffix="%", row=1, col=1)
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

    # Verification checks.
    diff = (q1["RW_union"] - (q1["W"] + q1["R"] - q1["RW_intersect"])).abs().max()
    assert diff == 0, f"identity |R∪W|=|R|+|W|-|R∩W| broken by {diff}"
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
    diff = (q1c["RW_union"] - (q1c["W"] + q1c["R"] - q1c["RW_intersect"])).abs().max()
    assert diff == 0, f"combined identity broken by {diff}"
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
