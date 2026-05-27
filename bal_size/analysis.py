"""Render the bal_size charts and read-out tables from the per-block parquet.

Run ``bal_size/collect.py`` first. Organised as ``# %%`` cells (run interactively in
VS Code / Jupyter) and also top-to-bottom as a script, saving PNGs next to the parquet.

    uv run python -m bal_size.analysis

Counts are primary: BAL size is reported in EIP-7928 "items" (storage_keys + addresses).
A derived byte view (RLP type widths + ~6% framing overhead) is included as a rough
secondary; RLP strips leading zeros, so it over-estimates encoded balances/nonces.
"""

# %%
from __future__ import annotations

import pandas as pd
import plotly.graph_objs as go
from plotly.subplots import make_subplots

from bal_size.config import (
    DATA_DIR,
    END_BLOCK,
    FORKS,
    GAS_LIMITS,
    ITEM_COST,
    PERBLOCK_PARQUET,
    START_BLOCK,
    block_to_date,
)
from bal_size.metrics import add_derived

KIB = 1024.0
# Component -> (label, colour) for the breakdown views.
COMPONENTS = {
    "addresses": ("addresses", "#1565C0"),
    "storage_writes": ("storage writes", "#B71C1C"),
    "storage_reads": ("storage reads", "#E65100"),
    "balance": ("balance diffs", "#2E7D32"),
    "nonce": ("nonce diffs", "#6A1B9A"),
    "code": ("code", "#5D4037"),
}
# Forks within the window get a marker; all post-Merge forks here predate the 6-month
# window, so this is typically empty (filled in below once the data range is known).
FORK_BLOCKS = {name: b for name, b in FORKS.items() if START_BLOCK <= b <= END_BLOCK}


def show(fig: go.Figure) -> None:
    """Display ``fig`` interactively, or skip if no renderer is available."""
    try:
        fig.show()
    except ValueError:
        pass


def _add_fork_lines(fig: go.Figure) -> None:
    """Dotted vertical markers at any hard forks that fall within the window."""
    for name, block in FORK_BLOCKS.items():
        date = block_to_date(block).strftime("%Y-%m-%d")
        fig.add_vline(x=date, line=dict(color="gray", width=1, dash="dot"))
        fig.add_annotation(x=date, yref="paper", y=1.0, text=name, showarrow=False,
                           font=dict(size=10, color="gray"), yshift=8)


df = add_derived(pd.read_parquet(PERBLOCK_PARQUET))
ts = df.set_index("date").sort_index()
ts.index = pd.DatetimeIndex(ts.index).tz_convert(None)
daily = ts["items"].resample("D").agg(["mean", "median",
                                       lambda s: s.quantile(0.9)]).rename(
    columns={"<lambda>": "p90"})
print(f"Per-block series: {len(df):,} blocks, "
      f"{df['date'].min():%Y-%m-%d} -> {df['date'].max():%Y-%m-%d}")


# %%
# Trend: per-block BAL items over time (daily mean/median + p90 band) and the daily mean
# contribution of the three storage-key/address components.
def trend_figure() -> go.Figure:
    comp = {c: ts[f"n_{c}"].resample("D").mean()
            for c in ("addresses", "write_slots", "read_only_slots")}
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=["BAL size per block (items)",
                                        "Mean per-block components (items)"])
    fig.add_trace(go.Scatter(x=daily.index, y=daily["p90"], name="p90",
                             line=dict(color="#90CAF9", width=0)), row=1, col=1)
    fig.add_trace(go.Scatter(x=daily.index, y=daily["mean"], name="mean", fill="tonexty",
                             fillcolor="rgba(21,101,192,0.12)",
                             line=dict(color="#1565C0")), row=1, col=1)
    fig.add_trace(go.Scatter(x=daily.index, y=daily["median"], name="median",
                             line=dict(color="#B71C1C", dash="dash")), row=1, col=1)
    labels = {"addresses": ("addresses", "#1565C0"),
              "write_slots": ("storage write slots", "#B71C1C"),
              "read_only_slots": ("read-only slots", "#E65100")}
    for key, (label, colour) in labels.items():
        fig.add_trace(go.Scatter(x=comp[key].index, y=comp[key], name=label,
                                 line=dict(color=colour)), row=2, col=1)
    fig.update_yaxes(title_text="items / block", gridcolor="lightgray", row=1, col=1)
    fig.update_yaxes(title_text="items / block", gridcolor="lightgray", row=2, col=1)
    fig.update_layout(title="EIP-7928 BAL size over the trailing 6 months",
                      template="plotly_white", width=1100, height=720)
    _add_fork_lines(fig)
    return fig


fig_trend = trend_figure()
fig_trend.write_image(DATA_DIR / "bal_size_trend.png", scale=2)
show(fig_trend)


# %%
# Distribution: histogram of per-block items with percentile markers.
def distribution_figure() -> go.Figure:
    pct = df["items"].quantile([0.5, 0.9, 0.99]).to_dict()
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=df["items"], nbinsx=80, marker_color="#1565C0",
                               name="blocks"))
    for q, colour in [(0.5, "#2E7D32"), (0.9, "#E65100"), (0.99, "#B71C1C")]:
        fig.add_vline(x=pct[q], line=dict(color=colour, dash="dash"))
        fig.add_annotation(x=pct[q], yref="paper", y=1.0, showarrow=False,
                           text=f"p{int(q * 100)}={pct[q]:,.0f}",
                           font=dict(size=10, color=colour), yshift=8)
    fig.update_layout(title="Per-block BAL size distribution (items)",
                      xaxis_title="items / block", yaxis_title="blocks",
                      template="plotly_white", width=1000, height=500, bargap=0.02)
    return fig


fig_dist = distribution_figure()
fig_dist.write_image(DATA_DIR / "bal_size_distribution.png", scale=2)
show(fig_dist)


# %%
# Component breakdown: average byte contribution per component (bar) + share over time.
def breakdown_figure() -> go.Figure:
    means = {c: ts[f"b_{c}"].resample("D").mean() for c in COMPONENTS}
    overall = {c: df[f"b_{c}"].mean() / KIB for c in COMPONENTS}
    total = sum(overall.values())
    fig = make_subplots(rows=1, cols=2, column_widths=[0.42, 0.58],
                        subplot_titles=["Mean bytes per BAL component",
                                        "Component share over time (bytes)"],
                        specs=[[{"type": "bar"}, {"type": "scatter"}]])
    labels = [COMPONENTS[c][0] for c in COMPONENTS]
    fig.add_trace(go.Bar(
        x=[overall[c] for c in COMPONENTS], y=labels, orientation="h",
        marker_color=[COMPONENTS[c][1] for c in COMPONENTS],
        text=[f"{overall[c]:,.1f} KiB ({100 * overall[c] / total:.0f}%)" for c in COMPONENTS],
        textposition="auto", showlegend=False), row=1, col=1)
    for c, (label, colour) in COMPONENTS.items():
        fig.add_trace(go.Scatter(x=means[c].index, y=means[c] / KIB, name=label,
                                 stackgroup="bytes", line=dict(width=0.5, color=colour)),
                      row=1, col=2)
    fig.update_xaxes(title_text="KiB / block (mean)", row=1, col=1)
    fig.update_yaxes(title_text="KiB / block", row=1, col=2, gridcolor="lightgray")
    fig.update_layout(title="EIP-7928 BAL component breakdown (derived bytes)",
                      template="plotly_white", width=1200, height=520)
    return fig


fig_break = breakdown_figure()
fig_break.write_image(DATA_DIR / "bal_component_breakdown.png", scale=2)
show(fig_break)


# %%
# Read-out tables: item/byte percentiles and headroom against the per-block item cap.
def _pct(col: str) -> dict[str, float]:
    q = df[col].quantile([0.5, 0.9, 0.99])
    return {"mean": df[col].mean(), "p50": q[0.5], "p90": q[0.9],
            "p99": q[0.99], "max": df[col].max()}


print("\nPer-block BAL size — items (storage_keys + addresses):")
print(f"{'stat':>8}{'items':>14}{'KiB (derived)':>16}")
items_s, bytes_s = _pct("items"), _pct("bytes_total")
for k in ("mean", "p50", "p90", "p99", "max"):
    print(f"{k:>8}{items_s[k]:>14,.0f}{bytes_s[k] / KIB:>16,.1f}")

print("\nPer-block items vs cap (block_gas_limit / ITEM_COST):")
print(f"{'gas limit':>10}{'item cap':>12}{'mean used':>12}{'p99 used':>12}")
for name, gas in GAS_LIMITS.items():
    cap = gas / ITEM_COST
    print(f"{name:>10}{cap:>12,.0f}{100 * items_s['mean'] / cap:>11.1f}%"
          f"{100 * items_s['p99'] / cap:>11.1f}%")
