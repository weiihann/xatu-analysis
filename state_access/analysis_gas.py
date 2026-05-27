"""Relate gas (capacity + usage) to the warm set / cold-state share over post-Merge history.

Three nested questions:
  Q1  Does the warm set track gas? (gas is the driver of the cold-state movements.)
  Q2  State-access intensity per gas — distinct slots touched per unit gas — over time.
      The warm set grows with gas but sub-proportionally; intensity declines.
  Q3  Implication: a gas-limit increase inflates the Active-tier (warm) set at fixed W,
      but sub-linearly.

Reads data/gas_daily.parquet (run collect_gas first) and the history_w{W}.parquet sweeps.

    uv run python -m state_access.analysis_gas
"""

# %%
from __future__ import annotations

import pandas as pd
import plotly.graph_objs as go
from plotly.subplots import make_subplots

from state_access.collect_gas import trailing_window_sum
from state_access.config import DATA_DIR
from state_access.history_config import FORKS, MERGE_BLOCK, block_to_date, parquet_for

WINDOWS = [30, 90, 180, 365]
WINDOW_COLORS = {30: "#1565C0", 90: "#2E7D32", 180: "#EF6C00", 365: "#6A1B9A"}
FORK_DATES = {name: block_to_date(b).strftime("%Y-%m-%d") for name, b in FORKS.items()}


def show(fig: go.Figure) -> None:
    """Display interactively, or skip if no renderer (PNG is the durable artifact)."""
    try:
        fig.show()
    except ValueError:
        pass


def add_forks(fig: go.Figure, lo: str, hi: str) -> None:
    """Dashed fork markers within [lo, hi] (date strings)."""
    for name, block in FORKS.items():
        day = block_to_date(block).strftime("%Y-%m-%d")
        if lo <= day <= hi:
            fig.add_vline(x=day, line=dict(color="gray", width=1, dash="dash"))
            fig.add_annotation(x=day, y=1.0, yref="paper", yanchor="bottom",
                               text=name, showarrow=False, font=dict(size=10, color="gray"))


gas = pd.read_parquet(DATA_DIR / "gas_daily.parquet").sort_values("day_idx")
# Cumulative gas by day, on a gap-free index, so a trailing-W-day sum is cum[d] - cum[d-W].
_cum = gas.set_index("day_idx")["gas_used"].reindex(
    range(int(gas["day_idx"].min()), int(gas["day_idx"].max()) + 1), fill_value=0).cumsum()


def windowed_gas(anchor_day: int, w: int) -> float:
    """Total gas_used over the W days ending at `anchor_day`."""
    return trailing_window_sum(_cum, anchor_day, w)


def load_with_gas(w: int) -> pd.DataFrame:
    """Sweep rows for window `w` augmented with windowed gas and access intensity.

    Drops anchors whose trailing-W-day gas window reaches before the Merge (where gas_daily
    has no data) — i.e. anchor_day < w — since their windowed gas would be truncated.
    """
    df = pd.read_parquet(parquet_for(w)).sort_values("anchor_block").copy()
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df["anchor_day"] = ((df["anchor_block"] - MERGE_BLOCK) // 7200).astype(int)
    df = df[df["anchor_day"] >= w].reset_index(drop=True)
    df["win_gas"] = [windowed_gas(d, w) for d in df["anchor_day"]]
    df["slots_per_mgas"] = df["unique_storage_slots"] / (df["win_gas"] / 1e6)
    return df


frames = {w: load_with_gas(w) for w in WINDOWS}


# %%
# Q1 — warm set vs the gas that produced it, per window (one self-scaled dual-axis panel each).
w30 = frames[30]
gd = gas.copy()
gd["date"] = pd.to_datetime(gd["block"].map(block_to_date)).dt.tz_localize(None)
lo, hi = w30["date"].min().strftime("%Y-%m-%d"), w30["date"].max().strftime("%Y-%m-%d")

Q1_WINDOWS = [30, 180, 365]
fig1 = make_subplots(specs=[[{"secondary_y": True}]])
for w in Q1_WINDOWS:
    df = frames[w]
    warm_state = (df["unique_storage_slots"] + df["unique_accounts"]) / 1e6
    color = WINDOW_COLORS[w]
    fig1.add_trace(go.Scatter(x=df["date"], y=warm_state, name=f"W={w}d warm",
                              legendgroup=f"w{w}", line=dict(color=color, width=2.5)),
                   secondary_y=False)
    fig1.add_trace(go.Scatter(x=df["date"], y=df["win_gas"] / 1e12, name=f"W={w}d gas",
                              legendgroup=f"w{w}", line=dict(color=color, width=1.5, dash="dot")),
                   secondary_y=True)
add_forks(fig1, lo, hi)
fig1.update_layout(
    title="Warm state and the gas that produced it, by window"
          "<br><sub>solid = warm state (slots + accounts, left axis); dotted = gas used (right axis); "
          "trailing W days</sub>",
    template="plotly_white", width=1300, height=650,
    legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.85)"),
)
fig1.update_xaxes(title="date", gridcolor="lightgray")
fig1.update_yaxes(title_text="warm state — slots + accounts (M)", secondary_y=False, gridcolor="lightgray")
fig1.update_yaxes(title_text="gas used over window (×10¹²)", secondary_y=True)
fig1.write_image(DATA_DIR / "gas_warm_set.png", scale=2)
show(fig1)


# %%
# Q2 — state-access intensity: distinct slots touched per Mgas, over time.
fig2 = go.Figure()
for w in (30, 365):
    df = frames[w]
    fig2.add_trace(go.Scatter(x=df["date"], y=df["slots_per_mgas"], name=f"W={w}d",
                              line=dict(color=WINDOW_COLORS[w], width=2.5)))
add_forks(fig2, lo, hi)
fig2.update_layout(
    title="State-access intensity: distinct storage slots touched per million gas"
          "<br><sub>warm slots ÷ gas used over the window; falling = each unit of gas touches fewer new slots</sub>",
    xaxis=dict(title="date", gridcolor="lightgray"),
    yaxis=dict(title="storage slots per Mgas", rangemode="tozero", gridcolor="lightgray"),
    template="plotly_white", width=1300, height=560,
    legend=dict(x=0.99, y=0.98, xanchor="right", bgcolor="rgba(255,255,255,0.85)"),
)
fig2.write_image(DATA_DIR / "gas_intensity.png", scale=2)
show(fig2)


# %%
# Yearly intensity per window, and the gas-limit regime framing.
print("Slots touched per Mgas, by year and window:")
print(f"{'year':>6}" + "".join(f"{f'W={w}d':>12}" for w in WINDOWS))
years = sorted({d.year for df in frames.values() for d in df["date"]})
for yr in years:
    row = f"{yr:>6}"
    for w in WINDOWS:
        s = frames[w].loc[frames[w]["date"].dt.year == yr, "slots_per_mgas"]
        row += f"{s.mean():>12.2f}" if len(s) else f"{'—':>12}"
    print(row)

print("\nGas-limit regime vs warm set (W=30):")
print(f"{'year':>6}{'gas_limit(M)':>14}{'gas_used/blk(M)':>17}{'warm slots(M)':>15}{'slots/Mgas':>12}")
for yr in years:
    g = gd[gd["date"].dt.year == yr]
    w = w30[w30["date"].dt.year == yr]
    if g.empty or w.empty:
        continue
    print(f"{yr:>6}{g['gas_limit'].mean() / 1e6:>14.1f}"
          f"{(g['gas_used'].mean() / 7200) / 1e6:>17.1f}"
          f"{w['unique_storage_slots'].mean() / 1e6:>15.1f}{w['slots_per_mgas'].mean():>12.2f}")


# %%
# Correlation: total warm state (slots + accounts) vs windowed gas, overall and by gas-limit regime.
print("\nCorrelation of total warm state vs windowed gas used:")
print(f"{'W':>5}{'r(all)':>9}{'rho(all)':>10}{'r(2025+)':>10}{'r(<=2024)':>11}")
for w in WINDOWS:
    df = frames[w].copy()
    df["warm_state"] = df["unique_storage_slots"] + df["unique_accounts"]
    recent = df[df["date"].dt.year >= 2025]
    early = df[df["date"].dt.year <= 2024]
    r = df["warm_state"].corr(df["win_gas"])
    rho = df["warm_state"].rank().corr(df["win_gas"].rank())  # Spearman without scipy
    r_recent = recent["warm_state"].corr(recent["win_gas"])
    r_early = early["warm_state"].corr(early["win_gas"])
    print(f"{w:>4}d{r:>9.2f}{rho:>10.2f}{r_recent:>10.2f}{r_early:>11.2f}")
