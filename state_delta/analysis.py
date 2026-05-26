"""Render the state_delta charts and read-out tables from the collected parquets.

Run ``state_delta/collect.py`` first. Organised as ``# %%`` cells (run interactively in
VS Code / Jupyter) and also top-to-bottom as a script, saving PNGs next to the parquets.

    uv run python -m state_delta.analysis
"""

# %%
from __future__ import annotations

import pandas as pd
import plotly.graph_objs as go
from plotly.subplots import make_subplots

from state_delta.config import (
    BYTE_COLS,
    COUNT_COLS,
    DATA_DIR,
    FORKS,
    METRIC_COLS,
    block_to_date,
)

GB = 1e9
LABELS = {
    "accounts": "accounts", "storages": "storage slots", "contract_codes": "contract codes",
    "account_bytes": "account bytes", "storage_bytes": "storage bytes",
    "contract_code_bytes": "contract-code bytes",
}
COLORS = {
    "accounts": "#1565C0", "storages": "#B71C1C", "contract_codes": "#2E7D32",
    "account_bytes": "#1565C0", "storage_bytes": "#B71C1C", "contract_code_bytes": "#2E7D32",
}
# Period resampling rules, in the order the user asked for.
RULES = {"daily": "D", "weekly": "W", "monthly": "MS", "yearly": "YS"}
# Plain date strings: tz-aware Timestamps in layout shapes break kaleido's serializer.
FORK_DATES = {name: block_to_date(b).strftime("%Y-%m-%d") for name, b in FORKS.items()}


def show(fig: go.Figure) -> None:
    """Display ``fig`` interactively, or skip if no renderer is available."""
    try:
        fig.show()
    except ValueError:
        pass


daily = pd.read_parquet(DATA_DIR / "state_delta_daily.parquet")
stats = pd.read_parquet(DATA_DIR / "state_delta_perblock_stats.parquet")
ts = daily.set_index("date").sort_index()
# tz-naive so resampled bar x-values serialize cleanly for kaleido.
ts.index = pd.DatetimeIndex(ts.index).tz_convert(None)
delta_cols = [f"d_{c}" for c in METRIC_COLS]
periods = {name: ts[delta_cols].resample(rule).sum() for name, rule in RULES.items()}

print(f"Daily series: {len(daily)} days, "
      f"{daily['date'].min():%Y-%m-%d} -> {daily['date'].max():%Y-%m-%d}")


# %%
def _add_fork_lines(fig: go.Figure) -> None:
    """Dotted vertical markers at the post-Merge hard forks (linear date axis)."""
    for name, date in FORK_DATES.items():
        fig.add_vline(x=date, line=dict(color="gray", width=1, dash="dot"))
        fig.add_annotation(x=date, yref="paper", y=1.0, text=name, showarrow=False,
                           font=dict(size=10, color="gray"), yshift=8)


def period_figure(cols: list[str], rule_name: str, divisor: float, unit: str) -> go.Figure:
    """Stacked per-metric bar chart of net Δ per period for one granularity."""
    df = periods[rule_name]
    fig = make_subplots(rows=len(cols), cols=1, shared_xaxes=True,
                        subplot_titles=[LABELS[c] for c in cols], vertical_spacing=0.06)
    for i, c in enumerate(cols, start=1):
        fig.add_trace(
            go.Bar(x=df.index, y=df[f"d_{c}"] / divisor, name=LABELS[c],
                   marker_color=COLORS[c]),
            row=i, col=1,
        )
        fig.update_yaxes(title_text=unit, row=i, col=1, gridcolor="lightgray")
    fig.update_layout(
        title=f"Net state Δ per {rule_name[:-2] if rule_name.endswith('ly') else rule_name} "
              f"— {'counts' if divisor == 1 else 'bytes'}",
        template="plotly_white", width=1100, height=750, showlegend=False, bargap=0.05,
    )
    _add_fork_lines(fig)
    return fig


# %%
# Period net-delta charts: counts and bytes, at daily / weekly / monthly / yearly.
for rule_name in RULES:
    fig_c = period_figure(COUNT_COLS, rule_name, 1.0, "entries")
    fig_c.write_image(DATA_DIR / f"delta_counts_{rule_name}.png", scale=2)
    show(fig_c)

    fig_b = period_figure(BYTE_COLS, rule_name, GB, "GB")
    fig_b.write_image(DATA_DIR / f"delta_bytes_{rule_name}.png", scale=2)
    show(fig_b)


# %%
# Period summary tables: total post-Merge growth and average growth rate per metric.
def _total(c: str) -> float:
    return float(daily[c].iloc[-1] - daily[c].iloc[0])


print("\nNet growth over the post-Merge window — counts (entries):")
print(f"{'metric':>22}{'total':>18}{'per day':>14}{'per year':>16}")
for c in COUNT_COLS:
    per_day = daily[f"d_{c}"].mean()
    print(f"{LABELS[c]:>22}{_total(c):>18,.0f}{per_day:>14,.0f}{per_day * 365:>16,.0f}")

print("\nNet growth over the post-Merge window — bytes:")
print(f"{'metric':>22}{'total (GB)':>16}{'per day (MB)':>16}{'per year (GB)':>16}")
for c in BYTE_COLS:
    per_day = daily[f"d_{c}"].mean()
    print(f"{LABELS[c]:>22}{_total(c) / GB:>16,.1f}{per_day / 1e6:>16,.1f}"
          f"{per_day * 365 / GB:>16,.2f}")


# %%
# Per-block net-delta distribution (overall + per year).
print("\nPer-block net delta — overall and per year:")
for c in COUNT_COLS:
    sub = stats[stats["metric"] == c].set_index("scope")
    print(f"\n  {LABELS[c]}:")
    print(f"    {'scope':>8}{'mean':>12}{'median':>10}{'p10':>10}{'p90':>10}{'max':>12}")
    for scope in [*sorted(s for s in sub.index if s != "overall"), "overall"]:
        r = sub.loc[scope]
        print(f"    {scope:>8}{r['mean']:>12,.1f}{r['median']:>10,.0f}{r['p10']:>10,.0f}"
              f"{r['p90']:>10,.0f}{r['max']:>12,.0f}")


# %%
# Per-block mean vs median over time (per year), for accounts and storage slots.
per_year = stats[stats["scope"] != "overall"].copy()
per_year["year"] = per_year["scope"].astype(int)
fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                    subplot_titles=[LABELS["accounts"], LABELS["storages"]])
for i, c in enumerate(("accounts", "storages"), start=1):
    sub = per_year[per_year["metric"] == c].sort_values("year")
    fig.add_trace(go.Bar(x=sub["year"], y=sub["mean"], name="mean", marker_color="#1565C0",
                         showlegend=(i == 1)), row=i, col=1)
    fig.add_trace(go.Bar(x=sub["year"], y=sub["median"], name="median", marker_color="#90CAF9",
                         showlegend=(i == 1)), row=i, col=1)
    fig.update_yaxes(title_text="entries / block", row=i, col=1, gridcolor="lightgray")
fig.update_layout(title="Per-block net delta by year — mean vs median",
                  template="plotly_white", width=1000, height=600, barmode="group")
fig.write_image(DATA_DIR / "perblock_by_year.png", scale=2)
show(fig)
