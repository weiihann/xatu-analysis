"""Render the historical-sweep trend charts from history_w30.parquet.

Run `state_access/collect_history.py` first. Organised as `# %%` cells; also runs
top-to-bottom as a script, saving PNGs next to the parquet.

    uv run python -m state_access.analysis_history
"""

# %%
from __future__ import annotations

import pandas as pd
import plotly.graph_objs as go

from state_access.config import DATA_DIR
from state_access.history_config import FORKS, HISTORY_PARQUET, W, block_to_date

ACCOUNT_COLOR = "#1565C0"
STORAGE_COLOR = "#B71C1C"
GAS_COLOR = "#2E7D32"

df = pd.read_parquet(HISTORY_PARQUET).sort_values("anchor_block")
print(f"{len(df)} anchors, {df['date'].min():%Y-%m-%d} → {df['date'].max():%Y-%m-%d}")


def show(fig: go.Figure) -> None:
    """Display interactively, or skip if no renderer (PNG is the durable artifact)."""
    try:
        fig.show()
    except ValueError:
        pass


def add_forks(fig: go.Figure) -> None:
    """Annotate fork boundaries that fall within the swept range."""
    for name, block in FORKS.items():
        if df["anchor_block"].min() <= block <= df["anchor_block"].max():
            fig.add_vline(x=block_to_date(block), line=dict(color="gray", width=1, dash="dash"),
                          annotation_text=name, annotation_position="top")


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
fig1.write_image(DATA_DIR / "history_state_cold.png", scale=2)
show(fig1)

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
fig2.write_image(DATA_DIR / "history_writes_cold.png", scale=2)
show(fig2)

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
    legend=dict(x=0.01, y=0.02, bgcolor="rgba(255,255,255,0.85)"),
)
fig3.write_image(DATA_DIR / "history_gas_concentration.png", scale=2)
show(fig3)
