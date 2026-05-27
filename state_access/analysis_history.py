"""Render the historical-sweep trend charts for a window W from history_w{W}.parquet.

Run `collect_history.py` first. Organised as `# %%` cells; also runs top-to-bottom as a
script, saving PNGs next to the parquet.

    uv run python -m state_access.analysis_history [W]   # default W=30
"""

# %%
from __future__ import annotations

import sys

import pandas as pd
import plotly.graph_objs as go

from state_access.config import (
    ACCOUNT_KEY_BYTES,
    DATA_DIR,
    STORAGE_KEY_BYTES,
    working_set_bytes,
)
from state_access.history_config import DEFAULT_W, FORKS, block_to_date, parquet_for

W = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_W

ACCOUNT_COLOR = "#1565C0"
STORAGE_COLOR = "#B71C1C"
GAS_COLOR = "#2E7D32"
GB = 1e9

raw = pd.read_parquet(parquet_for(W)).sort_values("anchor_block")

# Data-quality filter: drop anchors whose window overlapped an ingestion gap (implausibly
# few slots). On a fully-backfilled node this excludes nothing; it stays as a safety net.
median_slots = raw["unique_storage_slots"].median()
complete = raw["unique_storage_slots"] >= 0.7 * median_slots
df = raw[complete]
excluded = raw[~complete]

print(f"W={W}d: {len(df)} anchors plotted, {df['date'].min():%Y-%m-%d} → {df['date'].max():%Y-%m-%d}")
if not excluded.empty:
    blocks = ", ".join(f"{b:,}" for b in excluded["anchor_block"])
    print(f"Excluded {len(excluded)} anchors with incomplete diff data "
          f"(< 70% of median storage slots): {blocks}")


def show(fig: go.Figure) -> None:
    """Display interactively, or skip if no renderer (PNG is the durable artifact)."""
    try:
        fig.show()
    except ValueError:
        pass


def add_forks(fig: go.Figure) -> None:
    """Annotate fork boundaries that fall within the swept range.

    plotly's add_vline mishandles datetime x values, so pass the date as a string.
    """
    for name, block in FORKS.items():
        if df["anchor_block"].min() <= block <= df["anchor_block"].max():
            day = block_to_date(block).strftime("%Y-%m-%d")
            fig.add_vline(x=day, line=dict(color="gray", width=1, dash="dash"))
            fig.add_annotation(x=day, y=1.0, yref="paper", yanchor="bottom",
                               text=name, showarrow=False, font=dict(size=10, color="gray"))


def save(fig: go.Figure, name: str) -> None:
    fig.write_image(DATA_DIR / f"history_w{W}_{name}.png", scale=2)
    show(fig)


# %%
# Chart 1 — hot/cold state share over time.
fig1 = go.Figure()
fig1.add_trace(go.Scatter(x=df["date"], y=df["pct_accounts_cold"], mode="lines+markers",
                          name="accounts cold %", line=dict(color=ACCOUNT_COLOR, width=2)))
fig1.add_trace(go.Scatter(x=df["date"], y=df["pct_storage_cold"], mode="lines+markers",
                          name="storage cold %", line=dict(color=STORAGE_COLOR, width=2)))
add_forks(fig1)
fig1.update_layout(
    title=f"Cold state share over time (W={W}d) — mainnet, post-Merge weekly",
    xaxis=dict(title="date", gridcolor="lightgray"),
    yaxis=dict(title="% of state COLD", ticksuffix="%", gridcolor="lightgray"),
    template="plotly_white", width=1300, height=550,
    legend=dict(x=0.01, y=0.02, bgcolor="rgba(255,255,255,0.85)"),
)
save(fig1, "state_cold")

# %%
# Chart 2 — writes-to-cold over time.
fig2 = go.Figure()
fig2.add_trace(go.Scatter(x=df["date"], y=df["acct_writes_cold_pct"], mode="lines+markers",
                          name="account writes → cold %", line=dict(color=ACCOUNT_COLOR, width=2)))
fig2.add_trace(go.Scatter(x=df["date"], y=df["storage_writes_cold_pct"], mode="lines+markers",
                          name="storage writes → cold %", line=dict(color=STORAGE_COLOR, width=2)))
add_forks(fig2)
fig2.update_layout(
    title=f"Writes hitting the COLD tier over time (W={W}d)",
    xaxis=dict(title="date", gridcolor="lightgray"),
    yaxis=dict(title="% of today's writes that are cold", ticksuffix="%", gridcolor="lightgray"),
    template="plotly_white", width=1300, height=550,
    legend=dict(x=0.01, y=0.98, bgcolor="rgba(255,255,255,0.85)"),
)
save(fig2, "writes_cold")

# %%
# Chart 3 — update-gas warm share + concentration over time (dual axis).
fig3 = go.Figure()
fig3.add_trace(go.Scatter(x=df["date"], y=df["pct_update_gas_warm"], mode="lines+markers",
                          name="update-gas hitting warm %", line=dict(color=GAS_COLOR, width=2)))
fig3.add_trace(go.Scatter(x=df["date"], y=df["concentration_x"], mode="lines+markers",
                          name="concentration (×)", line=dict(color=STORAGE_COLOR, width=2, dash="dot"),
                          yaxis="y2"))
add_forks(fig3)
fig3.update_layout(
    title=f"Gas concentration over time (W={W}d): warm-gas coverage vs concentration",
    xaxis=dict(title="date", gridcolor="lightgray"),
    yaxis=dict(title="update-gas warm %", ticksuffix="%", gridcolor="lightgray"),
    yaxis2=dict(title="concentration (×)", overlaying="y", side="right", showgrid=False),
    template="plotly_white", width=1300, height=550,
    legend=dict(x=0.01, y=0.02, bgcolor="rgba(255,255,255,0.95)"),
)
save(fig3, "gas_concentration")

# %%
# Chart 4 — write working-set size over time (keys only): the byte size of the unique set
# of accounts and storage slots written in the window, sized as 20-byte addresses and
# 52-byte (address, slot) keys. Stacked so the split (slots dominate) and total are visible.
acct_gb = ACCOUNT_KEY_BYTES * df["unique_accounts"] / GB
slot_gb = STORAGE_KEY_BYTES * df["unique_storage_slots"] / GB
fig4 = go.Figure()
fig4.add_trace(go.Scatter(x=df["date"], y=acct_gb, mode="lines", name="account keys (20 B)",
                          stackgroup="size", line=dict(color=ACCOUNT_COLOR, width=0.5)))
fig4.add_trace(go.Scatter(x=df["date"], y=slot_gb, mode="lines", name="storage keys (52 B)",
                          stackgroup="size", line=dict(color=STORAGE_COLOR, width=0.5)))
add_forks(fig4)
fig4.update_layout(
    title=f"Write working-set size over time (W={W}d, keys only) — total = top of stack",
    xaxis=dict(title="date", gridcolor="lightgray"),
    yaxis=dict(title="size (GB)", gridcolor="lightgray"),
    template="plotly_white", width=1300, height=550,
    legend=dict(x=0.01, y=0.98, bgcolor="rgba(255,255,255,0.85)"),
)
save(fig4, "workingset_size")

total_gb = working_set_bytes(df["unique_accounts"], df["unique_storage_slots"]) / GB
print(f"Working-set size (W={W}d): latest {total_gb.iloc[-1]:.2f} GB "
      f"({df['date'].iloc[-1]:%Y-%m-%d}); range {total_gb.min():.2f}–{total_gb.max():.2f} GB, "
      f"mean {total_gb.mean():.2f} GB; storage keys are "
      f"{100 * slot_gb.iloc[-1] / total_gb.iloc[-1]:.0f}% of the latest total")
