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

from state_access.config_v2 import ANCHOR_BLOCK_V2, DATA_DIR_V2, SWEEP_WINDOWS
from state_access.history_config import FORKS, block_to_date

WINDOW_COLORS = {30: "#90CAF9", 90: "#42A5F5", 180: "#1976D2", 365: "#0D47A1"}
_MIXED_COMBOS = {
    "slot_mixed_cu": ("C+U", "#1565C0"),
    "slot_mixed_cd1": ("C+D (1-cycle)", "#FFA000"),
    "slot_mixed_cdm": ("C+D (multi-cycle)", "#E65100"),
    "slot_mixed_ud": ("U+D", "#7B1FA2"),
    "slot_mixed_cud1": ("C+U+D (1-cycle)", "#388E3C"),
    "slot_mixed_cudm": ("C+U+D (multi-cycle)", "#1B5E20"),
}


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


def render_all(df: pd.DataFrame) -> None:
    charts: list[tuple[str, go.Figure]] = []

    for obj, denom_col, label in (("slot", "denom_storages", "live slots"),
                                  ("acct", "denom_accounts", "live accounts")):
        fig = _base_fig(f"Warmth over time — {label}", f"% of {label}")
        _add_window_traces(fig, df, lambda s, o=obj, d=denom_col:
                           100 * s[f"{o}_RW_union"] / s[d], "R∪W")
        _add_window_traces(fig, df, lambda s, o=obj, d=denom_col:
                           100 * s[f"{o}_R"] / s[d], "R", dash="dash")
        charts.append((f"sweep_warmth_{obj}.png", fig))

    fig = _base_fig("Combined warmth over time — slots + accounts", "% of live state")
    _add_window_traces(fig, df, lambda s:
                       100 * (s.slot_RW_union + s.acct_RW_union)
                       / (s.denom_storages + s.denom_accounts), "R∪W")
    charts.append(("sweep_warmth_combined.png", fig))

    fig = _base_fig("Slot write structure over time", "% of |W|")
    _add_window_traces(fig, df, lambda s: 100 * s.slot_W_only_create / s.slot_W,
                       "create-only")
    _add_window_traces(fig, df, lambda s: 100 * s.slot_W_any_update / s.slot_W,
                       "any-update", dash="dash")
    charts.append(("sweep_write_structure.png", fig))

    d365 = df[df.window_days == 365]
    if not d365.empty:
        fig = _base_fig("W_mixed composition over time (T=365d)", "% of W_mixed")
        for col, (label, color) in _MIXED_COMBOS.items():
            fig.add_trace(go.Scatter(
                x=d365["date"],
                y=100 * d365[col] / d365["slot_W_mixed"].where(d365["slot_W_mixed"] != 0),
                name=label,
                mode="lines", stackgroup="one", line=dict(color=color, width=0.5),
                fillcolor=color))
        fig.update_yaxes(range=[0, 100], rangemode=None)
        charts.append(("sweep_mixed_decomp.png", fig))

    fig = _base_fig("Slot read structure over time", "% of |R|")
    _add_window_traces(fig, df, lambda s: 100 * s.slot_R_only_zero / s.slot_R,
                       "zero-only")
    charts.append(("sweep_read_structure.png", fig))

    fig = _base_fig("Concentration over time — top-1% share of accesses",
                    "share of accesses")
    _add_window_traces(fig, df, lambda s: 100 * s.conc_slot_top1_R, "slot R")
    _add_window_traces(fig, df, lambda s: 100 * s.conc_acct_top1_R, "acct R",
                       dash="dash")
    charts.append(("sweep_concentration.png", fig))

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

    for name, fig in charts:
        write_image_safe(fig, DATA_DIR_V2 / name)
        print(f"  rendered {name}", flush=True)


def main() -> None:
    df = load_sweeps()
    print(f"{len(df)} sweep rows across windows {sorted(df.window_days.unique())}")
    verify_rows(df)
    verify_against_snapshot(df)
    df.to_parquet(DATA_DIR_V2 / "sweep_summary.parquet", index=False)
    render_all(df)
    print("Done.")


if __name__ == "__main__":
    main()
