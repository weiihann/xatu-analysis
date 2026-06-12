"""Derive Q1 / Q3 from the v2 histograms and render charts.

Reads `data/v2/{slot,account}_histogram.parquet` and the typed-slot histogram (produced
by `collect_v2`). From these:

- **Q1 Warmth**: per `(access_type, W, object_type)` unique-set sizes |W|, |R|, |R∪W|.
  Plus typed views: slot W partitioned by `(create / update / delete)`, slot R partitioned
  by `(zero / nonzero)`, W_mixed decomposed into 6 sub-categories, and the per-event
  warm-update coverage under EIP-8188 semantics.
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

from state_access.config import DATA_DIR
from state_access.config_v2 import DATA_DIR_V2

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


def q1_warmth_slot_typed(typed_hist: pd.DataFrame, total_live: int) -> pd.DataFrame:
    """Per-window slot warmth split by value-transition type.

    `typed_hist` has rows `(window_days, n_w_create, n_w_update, n_w_delete, n_r_zero,
    n_r_nonzero, n_keys)`. Emits one row per `window_days` with:

    - Top-level sets: `W`, `R` (deduped against W — additive partition), `RW_union`.
    - W subtype counts (overlap allowed): keys with any create / update / delete event.
    - W pure-partition counts (disjoint): keys whose only writes are of one type.
    - R subtype counts on R-only keys (overlap allowed): any zero / any nonzero read.
    - R pure-partition counts on R-only keys (disjoint): only-zero / only-nonzero / mixed.

    Also emits `_pct` companions against `total_live`.
    """
    rows = []
    for w in sorted(typed_hist["window_days"].unique()):
        sub = typed_hist[typed_hist["window_days"] == w].copy()
        n_kw = sub["n_w_create"] + sub["n_w_update"] + sub["n_w_delete"]
        n_kr = sub["n_r_zero"] + sub["n_r_nonzero"]
        in_w = n_kw > 0
        in_r = n_kr > 0

        # Top-level (matches q1_warmth on slot_histogram).
        W = int(sub.loc[in_w, "n_keys"].sum())
        R = int(sub.loc[in_r & ~in_w, "n_keys"].sum())
        RW_union = int(sub.loc[in_w | in_r, "n_keys"].sum())

        # W subtypes — overlap allowed.
        W_any_create = int(sub.loc[in_w & (sub["n_w_create"] > 0), "n_keys"].sum())
        W_any_update = int(sub.loc[in_w & (sub["n_w_update"] > 0), "n_keys"].sum())
        W_any_delete = int(sub.loc[in_w & (sub["n_w_delete"] > 0), "n_keys"].sum())

        # W disjoint partition.
        only_create = (sub["n_w_create"] > 0) & (sub["n_w_update"] == 0) & (sub["n_w_delete"] == 0)
        only_update = (sub["n_w_update"] > 0) & (sub["n_w_create"] == 0) & (sub["n_w_delete"] == 0)
        only_delete = (sub["n_w_delete"] > 0) & (sub["n_w_create"] == 0) & (sub["n_w_update"] == 0)
        W_only_create = int(sub.loc[in_w & only_create, "n_keys"].sum())
        W_only_update = int(sub.loc[in_w & only_update, "n_keys"].sum())
        W_only_delete = int(sub.loc[in_w & only_delete, "n_keys"].sum())
        W_mixed = W - (W_only_create + W_only_update + W_only_delete)

        # R subtypes restricted to R-only keys (the meaningful "pure reads" set).
        r_only_mask = in_r & ~in_w
        R_any_zero    = int(sub.loc[r_only_mask & (sub["n_r_zero"] > 0),    "n_keys"].sum())
        R_any_nonzero = int(sub.loc[r_only_mask & (sub["n_r_nonzero"] > 0), "n_keys"].sum())

        only_zero    = (sub["n_r_zero"]    > 0) & (sub["n_r_nonzero"] == 0)
        only_nonzero = (sub["n_r_nonzero"] > 0) & (sub["n_r_zero"]    == 0)
        R_only_zero      = int(sub.loc[r_only_mask & only_zero,    "n_keys"].sum())
        R_only_nonzero   = int(sub.loc[r_only_mask & only_nonzero, "n_keys"].sum())
        R_mixed          = R - (R_only_zero + R_only_nonzero)

        rows.append({
            "window_days": int(w),
            "W": W, "R": R, "RW_union": RW_union,
            "W_any_create": W_any_create, "W_any_update": W_any_update, "W_any_delete": W_any_delete,
            "W_only_create": W_only_create, "W_only_update": W_only_update, "W_only_delete": W_only_delete,
            "W_mixed": W_mixed,
            "R_any_zero": R_any_zero, "R_any_nonzero": R_any_nonzero,
            "R_only_zero": R_only_zero, "R_only_nonzero": R_only_nonzero, "R_mixed": R_mixed,
        })
    out = pd.DataFrame(rows)
    out["total_live"] = int(total_live)
    for col in out.columns:
        if col in ("window_days", "total_live"):
            continue
        out[f"{col}_pct"] = 100 * out[col] / int(total_live)
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
        title=f"Warmth: {label} touched, by access set and window"
              f"<br><sub>denominator = {total_live:,}; "
              f"W = writes; R = pure reads added on top; R∪W = full warm set</sub>",
        xaxis=dict(title="window T (days)", type="log", gridcolor="lightgray"),
        yaxis=dict(title=f"% of {label}", ticksuffix="%",
                   gridcolor="lightgray", rangemode="tozero"),
        template="plotly_white", width=1100, height=600,
        legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.85)"),
    )
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
        title=f"Concentration: {label} of {object_type}s capture X% of accesses",
        xaxis=dict(title="window T (days)", type="log", gridcolor="lightgray"),
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

# Stacked-area colors for the W and R subtype views.
W_TYPE_COLORS = {
    "W_only_create": "#1976D2",
    "W_only_update": "#FBC02D",
    "W_only_delete": "#D32F2F",
    "W_mixed":       "#9E9E9E",
}
W_TYPE_LABEL = {
    "W_only_create": "create-only", "W_only_update": "update-only",
    "W_only_delete": "delete-only", "W_mixed": "mixed (≥2 types)",
}
R_TYPE_COLORS = {
    "R_only_zero":    "#90CAF9",
    "R_only_nonzero": "#1565C0",
    "R_mixed":        "#9E9E9E",
}
R_TYPE_LABEL = {
    "R_only_zero": "zero-only", "R_only_nonzero": "nonzero-only", "R_mixed": "mixed",
}


def _plot_slot_typed(q1t: pd.DataFrame, kind: Literal["W", "R"], total_live: int) -> go.Figure:
    """Stacked-area of W (or R-only) split by value-transition subtype, % of live slots."""
    if kind == "W":
        keys = ["W_only_create", "W_only_update", "W_only_delete", "W_mixed"]
        colors = W_TYPE_COLORS
        labels = W_TYPE_LABEL
        title = "Slot W (writes) split by value transition"
        sub = "create-only / update-only / delete-only / mixed; stacks sum to |W|"
    else:
        # R_mixed is ≤0.2% of |R| everywhere (net-per-tx diffs / reverted writes leak
        # intermediate values into reads — see REPORT_v2.md §2), so it isn't stacked.
        keys = ["R_only_zero", "R_only_nonzero"]
        colors = R_TYPE_COLORS
        labels = R_TYPE_LABEL
        title = "Slot R (reads-not-written) split by returned value"
        sub = "value=0 (empty-slot probe) vs value≠0 (populated read); stacks sum to |R| (to within R_mixed ≤0.2%)"
    fig = go.Figure()
    for k in keys:
        fig.add_trace(go.Scatter(
            x=q1t["window_days"], y=q1t[f"{k}_pct"], name=labels[k],
            mode="lines", stackgroup="one",
            line=dict(color=colors[k], width=0.5),
            fillcolor=colors[k],
        ))
    fig.update_layout(
        title=f"{title}<br><sub>{sub}; denominator = {total_live:,} live slots</sub>",
        xaxis=dict(title="window T (days)", type="log", gridcolor="lightgray"),
        yaxis=dict(title="% of live slots", ticksuffix="%",
                   gridcolor="lightgray", rangemode="tozero"),
        template="plotly_white", width=1100, height=560,
        legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.85)"),
    )
    return fig


_MIXED_COMBOS_ORDER = [
    "C+U", "C+D (1-cycle)", "C+D (multi-cycle)", "U+D",
    "C+U+D (1-cycle)", "C+U+D (multi-cycle)",
]
_MIXED_COMBOS_COLORS = {
    "C+U":                 "#1565C0",
    "C+D (1-cycle)":       "#FFA000",
    "C+D (multi-cycle)":   "#E65100",
    "U+D":                 "#7B1FA2",
    "C+U+D (1-cycle)":     "#388E3C",
    "C+U+D (multi-cycle)": "#1B5E20",
}


def _classify_mixed(row: pd.Series) -> str | None:
    """Sub-bin a typed-histogram row into one of the 6 W_mixed combos, or None if not mixed.

    Mixed = ≥2 of `{create, update, delete}` event types present in window. The structural
    rules (you can't have two creates without a delete between them; you can't update a
    deleted-then-zero slot) mean only `{C, D}` and `{C, U, D}` can carry multi-cycle
    structure; `{C, U}` always has exactly 1 create and `{U, D}` always has exactly 1
    delete.
    """
    has_c = row["n_w_create"] > 0
    has_u = row["n_w_update"] > 0
    has_d = row["n_w_delete"] > 0
    n_types = int(has_c) + int(has_u) + int(has_d)
    if n_types < 2:
        return None
    if has_c and has_u and not has_d:
        return "C+U"
    if has_u and has_d and not has_c:
        return "U+D"
    if has_c and has_d and not has_u:
        return "C+D (1-cycle)" if row["n_w_create"] == 1 else "C+D (multi-cycle)"
    return "C+U+D (1-cycle)" if row["n_w_create"] == 1 else "C+U+D (multi-cycle)"


def q1_warmth_slot_mixed_decomp(typed_hist: pd.DataFrame, total_live: int) -> pd.DataFrame:
    """Per-window decomposition of slot W_mixed into 6 sub-categories.

    Returns one row per (window_days, combo) with both share-of-mixed and share-of-state.
    """
    typed = typed_hist.copy()
    typed["combo"] = typed.apply(_classify_mixed, axis=1)
    mixed = typed[typed["combo"].notna()]
    agg = mixed.groupby(["window_days", "combo"], as_index=False)["n_keys"].sum()
    mixed_per_w = mixed.groupby("window_days")["n_keys"].sum()
    agg["share_of_mixed"] = agg.apply(
        lambda r: 100 * r["n_keys"] / mixed_per_w.get(r["window_days"], 1), axis=1)
    agg["share_of_state"] = 100 * agg["n_keys"] / int(total_live)
    return agg.sort_values(["window_days", "combo"]).reset_index(drop=True)


def _plot_slot_mixed_decomp(decomp: pd.DataFrame) -> go.Figure:
    """Stacked area: x=W, stacks=6 combos, y=% of W_mixed."""
    fig = go.Figure()
    pivot = decomp.pivot(index="window_days", columns="combo",
                         values="share_of_mixed").fillna(0)
    pivot = pivot.reindex(columns=_MIXED_COMBOS_ORDER, fill_value=0)
    for combo in _MIXED_COMBOS_ORDER:
        fig.add_trace(go.Scatter(
            x=pivot.index, y=pivot[combo], name=combo,
            mode="lines", stackgroup="one",
            line=dict(color=_MIXED_COMBOS_COLORS[combo], width=0.5),
            fillcolor=_MIXED_COMBOS_COLORS[combo],
        ))
    fig.update_layout(
        title="Slot W_mixed decomposition — composition of slots with ≥2 write types"
              "<br><sub>stacks sum to 100% of W_mixed at each T; "
              "C+D (1-cycle) = born and died once in window (ephemeral state)</sub>",
        xaxis=dict(title="window T (days)", type="log", gridcolor="lightgray"),
        yaxis=dict(title="% of W_mixed", ticksuffix="%",
                   gridcolor="lightgray", range=[0, 100]),
        template="plotly_white", width=1100, height=580,
        legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.85)"),
    )
    return fig


def run_slot_mixed_decomp(totals: dict[str, int]) -> None:
    """Decompose W_mixed and render the stacked-area chart."""
    p = DATA_DIR_V2 / "slot_typed_histogram.parquet"
    if not p.exists():
        print(f"  no typed-slot histogram at {p}; skipping mixed decomposition")
        return
    typed = pd.read_parquet(p)
    decomp = q1_warmth_slot_mixed_decomp(typed, int(totals["storages"]))
    decomp.to_parquet(DATA_DIR_V2 / "q1_warmth_slot_mixed_decomp.parquet", index=False)

    # Verification: shares-of-mixed sum to ~100 per window.
    sums = decomp.groupby("window_days")["share_of_mixed"].sum()
    assert (sums.between(99.99, 100.01)).all(), f"mixed decomp shares don't sum to 100: {sums}"

    fig = _plot_slot_mixed_decomp(decomp)
    fig.write_image(DATA_DIR_V2 / "q1_warmth_slot_mixed_decomp.png", scale=2)
    _show(fig)

    print("\n>>> W_mixed decomposition (share of W_mixed per T):")
    pivot = decomp.pivot(index="window_days", columns="combo",
                        values="share_of_mixed").fillna(0).reindex(
                            columns=_MIXED_COMBOS_ORDER, fill_value=0)
    print(pivot.round(2).to_string())


def run_slot_first_op() -> None:
    """Plot the per-W first-operation classification for slots (§4d)."""
    p = DATA_DIR_V2 / "slot_first_op.parquet"
    if not p.exists():
        print(f"  no slot_first_op parquet at {p}; sweep first")
        return
    df = pd.read_parquet(p).sort_values("window_days").reset_index(drop=True)
    df["pct_write"]        = 100 * df["first_is_write"]        / df["total_slots"]
    df["pct_zero_read"]    = 100 * df["first_is_zero_read"]    / df["total_slots"]
    df["pct_nonzero_read"] = 100 * df["first_is_nonzero_read"] / df["total_slots"]
    print(f"\n>>> slot_first_op: {len(df)} windows")

    x_cat = [str(w) for w in df["window_days"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_write"], name="first = write",
                         marker=dict(color="#1565C0")))
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_zero_read"], name="first = zero read",
                         marker=dict(color="#90CAF9")))
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_nonzero_read"], name="first = nonzero read",
                         marker=dict(color="#C2185B")))
    fig.update_layout(
        barmode="stack",
        title="Slot first-operation classification — policy-bad set is the nonzero-read piece"
              "<br><sub>under hypothetical EIP-8188 read-side period bumping, only nonzero "
              "reads would convert from no-op into a period-bumping operation</sub>",
        xaxis=dict(title="window T (days)", type="category"),
        yaxis=dict(title="% of slots in R∪W", ticksuffix="%", range=[0, 100],
                   gridcolor="lightgray"),
        template="plotly_white", width=1100, height=560,
        legend=dict(x=0.99, y=0.5, xanchor="right", bgcolor="rgba(255,255,255,0.85)"),
    )
    fig.write_image(DATA_DIR_V2 / "slot_first_op.png", scale=2)
    _show(fig)


def run_account_first_op() -> None:
    """Plot the per-W first-operation classification for accounts (§4d)."""
    p = DATA_DIR_V2 / "account_first_op.parquet"
    if not p.exists():
        print(f"  no account_first_op parquet at {p}; sweep first")
        return
    df = pd.read_parquet(p).sort_values("window_days").reset_index(drop=True)
    for col in ("first_is_write", "first_is_nonzero_read", "first_is_zero_read",
                "first_is_appearance_read"):
        # Strip the "first_" prefix to keep the column name a sensible pct label.
        df[f"pct_{col.removeprefix('first_')}"] = 100 * df[col] / df["total_accounts"]
    print(f"\n>>> account_first_op: {len(df)} windows")

    x_cat = [str(w) for w in df["window_days"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_is_write"], name="first = write",
                         marker=dict(color="#1565C0")))
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_is_zero_read"], name="first = zero read",
                         marker=dict(color="#90CAF9")))
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_is_nonzero_read"], name="first = nonzero read",
                         marker=dict(color="#C2185B")))
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_is_appearance_read"],
                         name="first = appearance read (value unknown)",
                         marker=dict(color="#9E9E9E")))
    fig.update_layout(
        barmode="stack",
        title="Account first-operation classification"
              "<br><sub>tie-break at same (block, tx_idx): writes > nonzero reads > zero "
              "reads > appearance reads</sub>",
        xaxis=dict(title="window T (days)", type="category"),
        yaxis=dict(title="% of accounts in R∪W", ticksuffix="%", range=[0, 100],
                   gridcolor="lightgray"),
        template="plotly_white", width=1100, height=560,
        legend=dict(x=0.99, y=0.5, xanchor="right", bgcolor="rgba(255,255,255,0.85)"),
    )
    fig.write_image(DATA_DIR_V2 / "account_first_op.png", scale=2)
    _show(fig)


def run_account_r_empty_split() -> None:
    """Plot the per-W empty vs non-empty split of R-only accounts (§4d)."""
    p = DATA_DIR_V2 / "account_r_empty_split.parquet"
    if not p.exists():
        print(f"  no account_r_empty_split parquet at {p}; sweep first")
        return
    df = pd.read_parquet(p).sort_values("window_days").reset_index(drop=True)
    df["pct_empty"]    = 100 * df["empty_accounts"]    / df["total_r"]
    df["pct_nonempty"] = 100 * df["nonempty_accounts"] / df["total_r"]
    df["pct_unknown"]  = 100 * df["unknown_accounts"]  / df["total_r"]
    print(f"\n>>> account_r_empty_split: {len(df)} windows")

    x_cat = [str(w) for w in df["window_days"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_nonempty"], name="non-empty (balance>0 or nonce>0)",
                         marker=dict(color="#C2185B")))
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_empty"], name="empty (balance=0 and nonce=0)",
                         marker=dict(color="#90CAF9")))
    fig.add_trace(go.Bar(x=x_cat, y=df["pct_unknown"], name="unknown (no balance/nonce reads)",
                         marker=dict(color="#9E9E9E")))
    fig.update_layout(
        barmode="stack",
        title="R-only accounts — empty vs non-empty"
              "<br><sub>R-only accounts have no writes in window, so balance and nonce are "
              "stable; empty = both observed as 0</sub>",
        xaxis=dict(title="window T (days)", type="category"),
        yaxis=dict(title="% of R-only accounts", ticksuffix="%", range=[0, 100],
                   gridcolor="lightgray"),
        template="plotly_white", width=1100, height=560,
        legend=dict(x=0.99, y=0.5, xanchor="right", bgcolor="rgba(255,255,255,0.85)"),
    )
    fig.write_image(DATA_DIR_V2 / "account_r_empty_split.png", scale=2)
    _show(fig)


def run_slot_update_coverage() -> None:
    """Plot the per-W warm/cold update split persisted by `collect_v2` (or the smoke run)."""
    p = DATA_DIR_V2 / "slot_update_coverage.parquet"
    if not p.exists():
        print(f"  no slot_update_coverage parquet at {p}; run the sweep first")
        return
    df = pd.read_parquet(p).sort_values("window_days").reset_index(drop=True)
    print(f"\n>>> slot_update_coverage: {len(df)} windows")

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["window_days"], y=df["pct_warm"], name="warm",
        mode="lines+markers",
        line=dict(color="#1565C0", width=3),
        marker=dict(size=8),
    ))
    cold_pct = 100 - df["pct_warm"]
    fig.add_trace(go.Scatter(
        x=df["window_days"], y=cold_pct, name="cold",
        mode="lines+markers",
        line=dict(color="#C2185B", width=2, dash="dash"),
        marker=dict(size=6),
    ))
    fig.update_layout(
        title="Slot UPDATE coverage — % of update events that are warm under EIP-8188 semantics"
              "<br><sub>warm = the slot had at least one prior create-or-update event in window; "
              "cold = the update IS the slot's first warming event</sub>",
        xaxis=dict(title="window T (days)", type="log", gridcolor="lightgray"),
        yaxis=dict(title="% of update events", ticksuffix="%", range=[0, 100],
                   gridcolor="lightgray"),
        template="plotly_white", width=1100, height=560,
        legend=dict(x=0.99, y=0.5, xanchor="right", bgcolor="rgba(255,255,255,0.85)"),
    )
    fig.write_image(DATA_DIR_V2 / "slot_update_coverage.png", scale=2)
    _show(fig)
    print(f"  W=1d:  warm={df.iloc[0]['pct_warm']:.2f}%  cold={cold_pct.iloc[0]:.2f}%")
    print(f"  W=30d: warm={df[df['window_days']==30]['pct_warm'].iloc[0]:.2f}%")
    print(f"  W=365d: warm={df.iloc[-1]['pct_warm']:.2f}%  cold={cold_pct.iloc[-1]:.2f}%")


def run_slot_typed(totals: dict[str, int]) -> None:
    """Derive the typed-slot Q1 view and plot W-by-transition and R-by-value charts."""
    p = DATA_DIR_V2 / "slot_typed_histogram.parquet"
    if not p.exists():
        print(f"  no typed-slot histogram at {p}; run collect_v2 first")
        return
    typed = pd.read_parquet(p)
    print(f"\n>>> slot_typed: {len(typed):,} histogram rows over "
          f"{typed['window_days'].nunique()} windows")
    total_live = int(totals["storages"])
    q1t = q1_warmth_slot_typed(typed, total_live)
    q1t.to_parquet(DATA_DIR_V2 / "q1_warmth_slot_typed.parquet", index=False)

    # Verifications.
    diff = (q1t["W"] - (q1t["W_only_create"] + q1t["W_only_update"]
                        + q1t["W_only_delete"] + q1t["W_mixed"])).abs().max()
    assert diff == 0, f"W disjoint partition broken by {diff}"
    diff_r = (q1t["R"] - (q1t["R_only_zero"] + q1t["R_only_nonzero"] + q1t["R_mixed"])).abs().max()
    assert diff_r == 0, f"R disjoint partition broken by {diff_r}"

    fig_w = _plot_slot_typed(q1t, "W", total_live)
    fig_w.write_image(DATA_DIR_V2 / "q1_warmth_slot_W_typed.png", scale=2)
    _show(fig_w)
    fig_r = _plot_slot_typed(q1t, "R", total_live)
    fig_r.write_image(DATA_DIR_V2 / "q1_warmth_slot_R_typed.png", scale=2)
    _show(fig_r)

    last = q1t.iloc[-1]

    def w_share(k: str) -> float:
        return 100 * last[k] / last["W"] if last["W"] else 0

    def r_share(k: str) -> float:
        return 100 * last[k] / last["R"] if last["R"] else 0

    print(f"  W=365d: |W|={last['W']:,}; "
          f"create-touching {w_share('W_any_create'):.1f}%, "
          f"update-touching {w_share('W_any_update'):.1f}%, "
          f"delete-touching {w_share('W_any_delete'):.1f}% (overlap)")
    print(f"  W=365d: |R|={last['R']:,}; "
          f"zero-touching {r_share('R_any_zero'):.1f}%, "
          f"nonzero-touching {r_share('R_any_nonzero'):.1f}% (overlap)")


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
    q3 = q3_concentration(hist)

    q1.to_parquet(DATA_DIR_V2 / f"q1_warmth_{object_type}.parquet", index=False)
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


# Display order + colors for the full-history event-mix charts.
_HISTORY_WRITE_GROUPS = {
    "slot_write": ["create", "update", "delete"],
    "account_balance_write": ["fund", "adjust", "drain"],
    "account_nonce_write": ["first_use", "subsequent"],
    "account_contract_create": ["create"],
}
_HISTORY_READ_GROUPS = {
    "slot_read": ["zero", "nonzero"],
    "account_balance_read": ["zero", "nonzero"],
    "account_nonce_read": ["zero", "nonzero"],
    "account_appearance_read": None,  # metrics are the relationships; take from data
}
_HISTORY_METRIC_COLORS = [
    "#1976D2", "#FBC02D", "#D32F2F", "#388E3C", "#7B1FA2", "#E65100",
    "#00838F", "#5D4037", "#90CAF9", "#9E9E9E",
]


def _history_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Sum chunk rows to per-(kind, metric) totals, plus pre/post-merge sub-totals."""
    from state_access.config_v2 import MERGE_BLOCK
    df = df.copy()
    df["era"] = df["bn_hi"].apply(lambda h: "pre_merge" if h < MERGE_BLOCK else "post_merge")
    total = df.groupby(["kind", "metric"], as_index=False)["n"].sum()
    era = df.pivot_table(index=["kind", "metric"], columns="era", values="n",
                         aggfunc="sum", fill_value=0).reset_index()
    out = total.merge(era, on=["kind", "metric"])
    for col in ("pre_merge", "post_merge"):
        if col not in out.columns:
            out[col] = 0
    return out


def _verify_history_tiling(df: pd.DataFrame) -> None:
    """Each kind's chunk ranges must tile [0, anchor] exactly (no gap, no overlap)."""
    from state_access.config_v2 import ANCHOR_BLOCK_V2
    for kind, sub in df.groupby("kind"):
        ranges = sorted(set(zip(sub["bn_lo"], sub["bn_hi"])))
        prev_hi = -1
        for lo, hi in ranges:
            assert lo == prev_hi + 1, f"{kind}: gap/overlap at [{lo}, {hi}] after {prev_hi}"
            prev_hi = hi
        assert prev_hi == ANCHOR_BLOCK_V2, f"{kind}: tiling ends at {prev_hi}"


def _plot_history_mix(totals: pd.DataFrame, kinds: dict, title: str) -> go.Figure:
    """One 100%-stacked bar per kind; shares of that kind's (non-total) events."""
    fig = go.Figure()
    seen_metrics: list[str] = []
    for kind, order in kinds.items():
        sub = totals[(totals["kind"] == kind) & (totals["metric"] != "total")]
        metrics = order if order else sorted(sub["metric"], key=lambda m: -int(
            sub.loc[sub["metric"] == m, "n"].iloc[0]))
        denom = sub["n"].sum()
        for m in metrics:
            n = int(sub.loc[sub["metric"] == m, "n"].sum())
            if m not in seen_metrics:
                seen_metrics.append(m)
            fig.add_trace(go.Bar(
                x=[kind], y=[100 * n / denom if denom else 0], name=m,
                legendgroup=m, showlegend=(kind == next(iter(kinds)) or order is None),
                marker=dict(color=_HISTORY_METRIC_COLORS[
                    seen_metrics.index(m) % len(_HISTORY_METRIC_COLORS)]),
            ))
    fig.update_layout(
        barmode="stack",
        title=title,
        xaxis=dict(title="event source"),
        yaxis=dict(title="% of source's events", ticksuffix="%", range=[0, 100],
                   gridcolor="lightgray"),
        template="plotly_white", width=1100, height=560,
        legend=dict(x=1.02, y=1.0, xanchor="left"),
    )
    return fig


def run_history_event_totals(totals_live: dict[str, int]) -> None:
    """Full-history (genesis → anchor) event mix for writes and reads (§4/§5 extension)."""
    p = DATA_DIR_V2 / "history_event_totals.parquet"
    if not p.exists():
        print(f"  no history event totals at {p}; run collect_v2_history first")
        return
    df = pd.read_parquet(p)
    _verify_history_tiling(df)
    totals = _history_totals(df)
    totals.to_parquet(DATA_DIR_V2 / "history_event_totals_summary.parquet", index=False)

    print("\n>>> full-history event totals (genesis → anchor):")
    for kind, sub in totals.groupby("kind"):
        named = sub[sub["metric"] != "total"]
        denom = int(named["n"].sum())
        tot_row = sub[sub["metric"] == "total"]
        residual = int(tot_row["n"].iloc[0]) - denom if len(tot_row) else 0
        parts = ", ".join(
            f"{r['metric']}={int(r['n']):,} ({100*r['n']/denom:.1f}%)"
            for _, r in named.sort_values("n", ascending=False).iterrows())
        extra = f"  [residual vs total: {residual:,}]" if residual else ""
        print(f"  {kind:24s} {parts}{extra}")

    # Soft sanity check: net slot creations ≈ live slots at the anchor.
    sw = totals[totals["kind"] == "slot_write"].set_index("metric")["n"]
    net = int(sw["create"] - sw["delete"])
    live = int(totals_live["storages"])
    print(f"  sanity: slot creates−deletes = {net:,} vs live slots {live:,} "
          f"({100*net/live:.1f}%) — approximate (net-per-tx, system writes missing)")

    fig_w = _plot_history_mix(
        totals, _HISTORY_WRITE_GROUPS,
        "Full-history WRITE event mix (genesis → anchor)"
        "<br><sub>per-source 100%-stacked; events are net per-(tx, object) units</sub>")
    fig_w.write_image(DATA_DIR_V2 / "history_event_totals_writes.png", scale=2)
    _show(fig_w)
    fig_r = _plot_history_mix(
        totals, _HISTORY_READ_GROUPS,
        "Full-history READ event mix (genesis → anchor)"
        "<br><sub>pre-merge reads sourced from ethpandaops; post-merge from the local node</sub>")
    fig_r.write_image(DATA_DIR_V2 / "history_event_totals_reads.png", scale=2)
    _show(fig_r)


def main() -> None:
    DATA_DIR_V2.mkdir(parents=True, exist_ok=True)
    totals = _load_totals()
    print(f"Live-state totals @ block {totals['snapshot_block']:,}: "
          f"{totals['accounts']:,} accounts, {totals['storages']:,} slots")
    for object_type in ("slot", "account"):
        run_one(object_type, totals)
    print("\n>>> combined")
    run_combined(totals)
    run_slot_typed(totals)
    run_slot_mixed_decomp(totals)
    run_slot_update_coverage()
    run_slot_first_op()
    run_account_first_op()
    run_account_r_empty_split()
    run_history_event_totals(totals)
    print(f"\nDone. Charts + Q-parquets under {DATA_DIR_V2}")


if __name__ == "__main__":
    main()
