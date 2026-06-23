"""Time-series charts + verification for the post-merge historical sweep.

Reads data/v2/sweep_w{T}.parquet (written by collect_v2_sweep), verifies internal
identities and that the newest anchor reproduces the committed snapshot, then renders
the Part III charts. Chart rendering is process-isolated: kaleido deadlocks after a few
write_image calls in one process (observed 2026-06-12), so each figure renders in a
fresh subprocess with a timeout.

    uv run python -m state_access.analysis_v2_sweep
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pandas as pd
import plotly.graph_objs as go
from plotly.subplots import make_subplots

from state_access.config_v2 import ANCHOR_BLOCK_V2, DATA_DIR_V2, SWEEP_WINDOWS
from state_access.history_config import FORKS, block_to_date

WINDOW_COLORS = {30: "#90CAF9", 90: "#42A5F5", 180: "#1976D2", 365: "#0D47A1"}

# Every time-series chart shares this left bound so the panels line up. The 365d window
# has no anchors before late 2023, so its panel carries a blank lead-in rather than its
# own narrower axis.
X_AXIS_START = "2023-01-01"

# Write lifecycle composition: each written slot falls in exactly one class; shares sum to
# |W|. The six W_mixed combos collapse to four families (cycle-count split dropped). Stack
# order runs born → grown → modified in place → ephemeral/died, with one fixed colour each.
# Each entry: (label, colour, columns-to-sum). Numerator = sum(columns); denominator slot_W.
WRITE_CLASSES = [
    ("C", "#1976D2", ["slot_W_only_create"]),
    ("C+U", "#4FC3F7", ["slot_mixed_cu"]),
    ("U", "#FBC02D", ["slot_W_only_update"]),
    ("C+U+D", "#388E3C", ["slot_mixed_cud1", "slot_mixed_cudm"]),
    ("C+D", "#FB8C00", ["slot_mixed_cd1", "slot_mixed_cdm"]),
    ("U+D", "#7B1FA2", ["slot_mixed_ud"]),
    ("D", "#D32F2F", ["slot_W_only_delete"]),
]
# Read composition: zero-only vs nonzero-only as a share of |R| (R_mixed ≈ 0, not stacked).
READ_CLASSES = [
    ("zero-only", "#90CAF9", ["slot_R_only_zero"]),
    ("nonzero-only", "#1565C0", ["slot_R_only_nonzero"]),
]


def _render(fig_dict: dict, path: str) -> None:
    go.Figure(fig_dict).write_image(path, scale=2)


def write_image_safe(fig: go.Figure, path: Path, timeout: int = 180) -> None:
    """Render in a fresh subprocess — kaleido deadlocks on repeated in-process renders."""
    proc = mp.get_context("spawn").Process(target=_render, args=(fig.to_dict(), str(path)))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=10)
        if proc.is_alive():
            proc.kill()
            proc.join()
        raise RuntimeError(f"chart render timed out: {path}")
    if proc.exitcode != 0:
        raise RuntimeError(f"chart render failed (exit {proc.exitcode}): {path}")


def load_sweeps() -> pd.DataFrame:
    frames = []
    for t in SWEEP_WINDOWS:
        p = DATA_DIR_V2 / f"sweep_w{t}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        raise RuntimeError("no sweep parquets found — run collect_v2_sweep first")
    return pd.concat(frames, ignore_index=True).sort_values(
        ["window_days", "anchor_block"]).reset_index(drop=True)


def verify_rows(df: pd.DataFrame) -> None:
    """Per-row identities. Raises AssertionError naming the broken invariant."""
    has_denom = "denom_block" in df.columns
    for _, r in df.iterrows():
        where = f"anchor={int(r.anchor_block):,} T={int(r.window_days)}"
        assert r.slot_RW_union == r.slot_W + r.slot_R, f"slot additivity broken at {where}"
        assert r.acct_RW_union == r.acct_W + r.acct_R, f"acct additivity broken at {where}"
        only = r.slot_W_only_create + r.slot_W_only_update + r.slot_W_only_delete
        assert r.slot_W == only + r.slot_W_mixed, f"W partition broken at {where}"
        combos = (r.slot_mixed_cu + r.slot_mixed_cd1 + r.slot_mixed_cdm
                  + r.slot_mixed_ud + r.slot_mixed_cud1 + r.slot_mixed_cudm)
        assert r.slot_W_mixed == combos, f"mixed partition broken at {where}"
        r_parts = r.slot_R_only_zero + r.slot_R_only_nonzero + r.slot_R_mixed
        assert r.slot_R == r_parts, f"R partition broken at {where}"
        assert r.upd_total_updates == r.upd_warm_updates + r.upd_cold_updates, \
            f"update coverage partition broken at {where}"
        sfo = r.sfo_first_is_write + r.sfo_first_is_zero_read + r.sfo_first_is_nonzero_read
        assert r.sfo_total_slots == sfo, f"slot first-op partition broken at {where}"
        if has_denom:
            assert r.anchor_block - r.denom_block <= 50_400, f"stale denominator at {where}"


def verify_against_snapshot(df: pd.DataFrame) -> None:
    """The newest anchor of each window must reproduce the committed v2 parquets."""
    typed = pd.read_parquet(DATA_DIR_V2 / "q1_warmth_slot_typed.parquet")
    acct = pd.read_parquet(DATA_DIR_V2 / "q1_warmth_account.parquet")
    upd = pd.read_parquet(DATA_DIR_V2 / "slot_update_coverage.parquet")
    for t in df["window_days"].unique():
        row = df[(df.window_days == t) & (df.anchor_block == ANCHOR_BLOCK_V2)]
        if row.empty:
            continue
        row = row.iloc[0]
        ref = typed[typed.window_days == t].iloc[0]
        assert int(row.slot_W) == int(ref.W) and int(row.slot_R) == int(ref.R), \
            f"sweep snapshot mismatch (slot) at T={t}"
        ref = acct[acct.window_days == t].iloc[0]
        assert int(row.acct_W) == int(ref.W), f"sweep snapshot mismatch (acct) at T={t}"
        ref = upd[upd.window_days == t].iloc[0]
        # Raw SSTORE-event totals drift by <0.02% between the snapshot and the
        # sweep because canonical_execution_storage_diffs is a ReplacingMergeTree
        # whose older-block rows keep merging/deduping over time; wider windows
        # reach further back and drift more. Distinct-key counts (slot_W/R, acct_W
        # above) stay exact and pct_warm matches to 4 decimals, so a 1% relative
        # tolerance is the right guard for the event-count totals — loose enough
        # for the dedup noise, tight enough to catch a real regression.
        assert abs(int(row.upd_total_updates) - int(ref.total_updates)) <= 0.01 * int(ref.total_updates), \
            f"sweep snapshot mismatch (upd) at T={t}: " \
            f"{int(row.upd_total_updates):,} vs {int(ref.total_updates):,}"


def _base_fig(title: str, ytitle: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        title=title,
        xaxis=dict(title="anchor date", gridcolor="lightgray"),
        yaxis=dict(title=ytitle, ticksuffix="%", gridcolor="lightgray",
                   rangemode="tozero"),
        template="plotly_white", width=1100, height=560,
        legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.85)"),
    )
    for name, block in FORKS.items():
        when = block_to_date(block).strftime("%Y-%m-%d")
        fig.add_vline(x=when, line_dash="dot", line_color="#9E9E9E")
        fig.add_annotation(x=when, y=1.0, yref="paper", text=name, showarrow=False,
                           font=dict(size=10, color="#757575"), yanchor="bottom")
    return fig


def _add_window_traces(fig: go.Figure, df: pd.DataFrame, value_fn, label: str,
                       dash: str = "solid") -> None:
    for t in sorted(df["window_days"].unique()):
        sub = df[df.window_days == t]
        fig.add_trace(go.Scatter(
            x=sub["date"], y=value_fn(sub), name=f"{label} T={t}d",
            mode="lines", line=dict(color=WINDOW_COLORS[int(t)], width=2, dash=dash),
        ))


def verify_composition(df: pd.DataFrame) -> None:
    """Each cell's class shares must partition the access set exactly."""
    for _, r in df.iterrows():
        where = f"anchor={int(r.anchor_block):,} T={int(r.window_days)}"
        w_sum = sum(r[c] for _, _, cols in WRITE_CLASSES for c in cols)
        assert w_sum == r.slot_W, f"write classes don't sum to |W| at {where}"
        assert r.slot_R_only_zero + r.slot_R_only_nonzero + r.slot_R_mixed == r.slot_R, \
            f"read classes don't sum to |R| at {where}"


def _composition_fig(df: pd.DataFrame, classes: list, denom_col: str, title: str) -> go.Figure:
    """4-panel (one per window) stacked-area composition over time, shares to 100%."""
    windows = sorted(df["window_days"].unique())
    fig = make_subplots(
        rows=2, cols=2, subplot_titles=[f"T = {t}d" for t in windows],
        shared_yaxes=True, vertical_spacing=0.16, horizontal_spacing=0.06)
    for i, t in enumerate(windows):
        row, col = i // 2 + 1, i % 2 + 1
        sub = df[df.window_days == t].sort_values("date")
        denom = sub[denom_col].where(sub[denom_col] != 0)
        for label, color, cols in classes:
            num = sum(sub[c] for c in cols)
            fig.add_trace(go.Scatter(
                x=sub["date"], y=100 * num / denom, name=label, legendgroup=label,
                showlegend=(i == 0), mode="lines", stackgroup=f"w{t}",
                line=dict(color=color, width=0.5), fillcolor=color), row=row, col=col)
    # Fork lines span every panel; label each panel just above its plot area, below the
    # centred T = Nd title.
    for name, block in FORKS.items():
        x = block_to_date(block).strftime("%Y-%m-%d")
        fig.add_vline(x=x, line_dash="dot", line_color="#9E9E9E", row="all", col="all")
        for row in (1, 2):
            for col in (1, 2):
                fig.add_annotation(x=x, y=1.0, yref="y domain", text=name, row=row,
                                   col=col, showarrow=False, yanchor="bottom", yshift=3,
                                   font=dict(size=8, color="#757575"))
    fig.update_yaxes(range=[0, 100], ticksuffix="%", showgrid=False)
    fig.update_xaxes(showgrid=False)
    fig.update_layout(
        title=title, template="plotly_white", width=1200, height=860,
        legend=dict(orientation="h", y=-0.08, x=0.5, xanchor="center"))
    return fig


# Each warmth chart fixes one metric (W / R / R∪W) and shows slots, accounts, and combined
# as a share of their respective live-state denominator, one panel per window.
WARMTH_SERIES = (
    ("slots", "#1976D2", ("slot_{m}",), ("denom_storages",)),
    ("accounts", "#FB8C00", ("acct_{m}",), ("denom_accounts",)),
    ("combined", "#388E3C", ("slot_{m}", "acct_{m}"), ("denom_storages", "denom_accounts")),
)


def _warmth_metric_fig(df: pd.DataFrame, metric: str, title: str) -> go.Figure:
    """4-panel (one per window) over-time chart of one warmth metric, by object type."""
    windows = sorted(df["window_days"].unique())
    fig = make_subplots(
        rows=2, cols=2, subplot_titles=[f"T = {t}d" for t in windows],
        vertical_spacing=0.16, horizontal_spacing=0.07)
    for i, t in enumerate(windows):
        row, col = i // 2 + 1, i % 2 + 1
        sub = df[df.window_days == t].sort_values("date")
        for label, color, nums, dens in WARMTH_SERIES:
            num = sum(sub[c.format(m=metric)] for c in nums)
            den = sum(sub[c] for c in dens)
            fig.add_trace(go.Scatter(
                x=sub["date"], y=100 * num / den, name=label, legendgroup=label,
                showlegend=(i == 0), mode="lines", line=dict(color=color, width=2)),
                row=row, col=col)
    for name, block in FORKS.items():
        x = block_to_date(block).strftime("%Y-%m-%d")
        fig.add_vline(x=x, line_dash="dot", line_color="#9E9E9E", row="all", col="all")
        for row in (1, 2):
            for col in (1, 2):
                fig.add_annotation(x=x, y=1.0, yref="y domain", text=name, row=row,
                                   col=col, showarrow=False, yanchor="bottom", yshift=3,
                                   font=dict(size=8, color="#757575"))
    fig.update_yaxes(ticksuffix="%", rangemode="tozero", showgrid=False)
    fig.update_xaxes(showgrid=False)
    fig.update_layout(
        title=title, template="plotly_white", width=1200, height=860,
        legend=dict(orientation="h", y=-0.08, x=0.5, xanchor="center"))
    return fig


def _concentration_over_time_fig(df: pd.DataFrame) -> go.Figure:
    """4-panel (one per window) top-1% read concentration over time, slots vs accounts."""
    windows = sorted(df["window_days"].unique())
    fig = make_subplots(
        rows=2, cols=2, subplot_titles=[f"T = {t}d" for t in windows],
        vertical_spacing=0.16, horizontal_spacing=0.07)
    series = (("slots", "#1976D2", "conc_slot_top1_R"),
              ("accounts", "#FB8C00", "conc_acct_top1_R"))
    for i, t in enumerate(windows):
        row, col = i // 2 + 1, i % 2 + 1
        sub = df[df.window_days == t].sort_values("date")
        for label, color, colname in series:
            fig.add_trace(go.Scatter(
                x=sub["date"], y=100 * sub[colname], name=label, legendgroup=label,
                showlegend=(i == 0), mode="lines", line=dict(color=color, width=2)),
                row=row, col=col)
    for name, block in FORKS.items():
        x = block_to_date(block).strftime("%Y-%m-%d")
        fig.add_vline(x=x, line_dash="dot", line_color="#9E9E9E", row="all", col="all")
        for row in (1, 2):
            for col in (1, 2):
                fig.add_annotation(x=x, y=1.0, yref="y domain", text=name, row=row,
                                   col=col, showarrow=False, yanchor="bottom", yshift=3,
                                   font=dict(size=8, color="#757575"))
    fig.update_yaxes(ticksuffix="%", rangemode="tozero", showgrid=False)
    fig.update_xaxes(showgrid=False)
    fig.update_layout(
        title="Top-1% share of read accesses over time — by window",
        template="plotly_white", width=1200, height=860,
        legend=dict(orientation="h", y=-0.08, x=0.5, xanchor="center"))
    return fig


def render_all(df: pd.DataFrame) -> None:
    charts: list[tuple[str, go.Figure]] = []

    for metric, fname, mlabel in (("W", "W", "write set W"),
                                  ("Rp", "Rp", "populated reads R⁺"),
                                  ("RpW_union", "RpW", "populated warm set W + R⁺")):
        charts.append((f"sweep_warmth_{fname}.png", _warmth_metric_fig(
            df, metric, f"Warmth over time — {mlabel}, % of live state, by window")))

    charts.append(("sweep_write_composition.png", _composition_fig(
        df, WRITE_CLASSES, "slot_W",
        "Slot write lifecycle composition over time — % of |W|, by window")))
    charts.append(("sweep_read_composition.png", _composition_fig(
        df, READ_CLASSES, "slot_R",
        "Slot read composition over time — % of |R|, by window")))

    charts.append(("sweep_concentration.png", _concentration_over_time_fig(df)))

    fig = _base_fig("Warm-update coverage over time (§7)", "% of update events warm")
    _add_window_traces(fig, df, lambda s: s.upd_pct_warm, "warm")
    charts.append(("sweep_update_coverage.png", fig))

    fig = _base_fig("First-op = nonzero read over time (§8 policy-bad set)",
                    "% of R∪W objects")
    _add_window_traces(fig, df, lambda s:
                       100 * s.sfo_first_is_nonzero_read / s.sfo_total_slots, "slots")
    _add_window_traces(fig, df, lambda s:
                       100 * s.afo_first_is_nonzero_read / s.afo_total_accounts,
                       "accounts", dash="dash")
    charts.append(("sweep_first_op.png", fig))

    fig = _base_fig("R-only accounts non-empty share over time (§8)",
                    "% of R-only accounts")
    _add_window_traces(fig, df, lambda s:
                       100 * s.res_nonempty_accounts / s.res_total_r.where(s.res_total_r != 0),
                       "non-empty")
    charts.append(("sweep_empty_split.png", fig))

    x_end = df["date"].max().strftime("%Y-%m-%d")
    for name, fig in charts:
        fig.update_xaxes(range=[X_AXIS_START, x_end])
        write_image_safe(fig, DATA_DIR_V2 / name)
        print(f"  rendered {name}", flush=True)


def main() -> None:
    df = load_sweeps()
    print(f"{len(df)} sweep rows across windows {sorted(df.window_days.unique())}")
    verify_rows(df)
    verify_against_snapshot(df)
    verify_composition(df)
    # R⁺ = read-not-written objects whose reads return a nonzero (populated) value: slots
    # with a nonzero SLOAD, accounts with a positive balance/nonce read. The warm-set
    # measure (§5.1) uses R⁺ so empty-slot probes do not count as warm.
    df["slot_Rp"] = df["slot_R_only_nonzero"] + df["slot_R_mixed"]
    df["acct_Rp"] = df["res_nonempty_accounts"]
    df["slot_RpW_union"] = df["slot_W"] + df["slot_Rp"]
    df["acct_RpW_union"] = df["acct_W"] + df["acct_Rp"]
    df.to_parquet(DATA_DIR_V2 / "sweep_summary.parquet", index=False)
    render_all(df)
    print("Done.")


if __name__ == "__main__":
    main()
