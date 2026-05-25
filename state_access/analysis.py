"""Render the state_access charts and read-out tables from the collected parquets.

Run `state_access/collect.py` first to produce the data. This file is organised as
`# %%` cells (run interactively in VS Code / Jupyter), and also runs top-to-bottom as
a script, saving PNGs next to the parquets.

    uv run python -m state_access.analysis
"""

# %%
from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objs as go

from state_access.config import DATA_DIR

ACCOUNT_COLOR = "#1565C0"
STORAGE_COLOR = "#B71C1C"


def show(fig: go.Figure) -> None:
    """Display ``fig`` interactively, or skip if no renderer is available.

    In `# %%` cells this shows inline; as a plain script the PNG written alongside
    is the durable artifact, so a missing interactive renderer is not an error.
    """
    try:
        fig.show()
    except ValueError:
        pass

state = pd.read_parquet(DATA_DIR / "hot_cold_state.parquet")
tradeoff = pd.read_parquet(DATA_DIR / "tradeoff.parquet")
gas = pd.read_parquet(DATA_DIR / "gas_concentration.parquet")
totals = json.loads((DATA_DIR / "totals.json").read_text())

TOTAL_ACCOUNTS = totals["accounts"]
TOTAL_STORAGE = totals["storages"]
print(f"Totals @ block {totals['snapshot_block']:,}: "
      f"{TOTAL_ACCOUNTS:,} accounts, {TOTAL_STORAGE:,} storage slots")

# %%
# Derived hot/cold percentages.
state["pct_accounts_hot"] = 100 * state["unique_accounts"] / TOTAL_ACCOUNTS
state["pct_storage_hot"] = 100 * state["unique_storage_slots"] / TOTAL_STORAGE
state["pct_accounts_cold"] = 100 - state["pct_accounts_hot"]
state["pct_storage_cold"] = 100 - state["pct_storage_hot"]

readout = state[["window_days", "unique_accounts", "unique_storage_slots",
                 "pct_accounts_hot", "pct_storage_hot",
                 "pct_accounts_cold", "pct_storage_cold"]].copy()
print(readout.to_string(index=False, float_format=lambda v: f"{v:7.3f}"))


# %%
def _line(df, x, y, name, color, position):
    return go.Scatter(
        x=df[x], y=df[y], mode="lines+markers+text", name=name,
        line=dict(color=color, width=2.5), marker=dict(size=8),
        text=[f"{v:.2f}%" for v in df[y]], textposition=position,
        textfont=dict(size=10, color=color),
    )


# Chart 1 — hot share vs window.
fig1 = go.Figure([
    _line(state, "window_days", "pct_accounts_hot", "accounts (hot %)", ACCOUNT_COLOR, "top center"),
    _line(state, "window_days", "pct_storage_hot", "storage slots (hot %)", STORAGE_COLOR, "bottom center"),
])
fig1.update_layout(
    title=f"Share of Ethereum state modified within N days — mainnet, block {totals['snapshot_block']:,}"
          f"<br><sub>denominators: {TOTAL_ACCOUNTS/1e6:.1f}M accounts, {TOTAL_STORAGE/1e9:.2f}B storage slots</sub>",
    xaxis=dict(title="window length (days, log)", type="log",
               tickvals=state["window_days"].tolist(), gridcolor="lightgray"),
    yaxis=dict(title="% of state that is HOT", rangemode="tozero", gridcolor="lightgray"),
    template="plotly_white", width=1200, height=600,
    legend=dict(x=0.01, y=0.98, bgcolor="rgba(255,255,255,0.85)"),
)
fig1.write_image(DATA_DIR / "hot_share_vs_window.png", scale=2)
show(fig1)

# %%
# Chart 2 — cold share vs window.
fig2 = go.Figure([
    go.Scatter(x=state["window_days"], y=state["pct_accounts_cold"],
               mode="lines+markers", name="accounts cold %", line=dict(color=ACCOUNT_COLOR, width=2.5)),
    go.Scatter(x=state["window_days"], y=state["pct_storage_cold"],
               mode="lines+markers", name="storage cold %", line=dict(color=STORAGE_COLOR, width=2.5)),
])
fig2.update_layout(
    title="Cold share — fraction of state NOT modified within N days",
    xaxis=dict(title="window length (days, log)", type="log",
               tickvals=state["window_days"].tolist(), gridcolor="lightgray"),
    yaxis=dict(title="% of state that is COLD", gridcolor="lightgray"),
    template="plotly_white", width=1200, height=500,
    legend=dict(x=0.01, y=0.10, bgcolor="rgba(255,255,255,0.85)"),
)
fig2.write_image(DATA_DIR / "cold_share_vs_window.png", scale=2)
show(fig2)

# %%
# Chart 3 — tiering tradeoff: cold-bucket size vs writes-to-cold.
fig3 = go.Figure()
fig3.add_trace(go.Scatter(
    x=tradeoff["window_days"], y=tradeoff["state_cold_pct"],
    mode="lines+markers+text", name="State in COLD bucket (big = architectural benefit)",
    line=dict(color=ACCOUNT_COLOR, width=3), marker=dict(size=10),
    text=[f"{v:.1f}%" for v in tradeoff["state_cold_pct"]], textposition="top center",
))
acct = tradeoff.dropna(subset=["acct_writes_cold_pct"])
fig3.add_trace(go.Scatter(
    x=acct["window_days"], y=acct["acct_writes_cold_pct"],
    mode="lines+markers+text", name="Account writes → COLD (small = less penalty)",
    line=dict(color="#2E7D32", width=2.5), marker=dict(size=9, symbol="diamond"),
    text=[f"{v:.1f}%" for v in acct["acct_writes_cold_pct"]], textposition="bottom center",
))
stor = tradeoff.dropna(subset=["storage_writes_cold_pct"])
fig3.add_trace(go.Scatter(
    x=stor["window_days"], y=stor["storage_writes_cold_pct"],
    mode="lines+markers+text", name="Storage-slot writes → COLD (small = less penalty)",
    line=dict(color=STORAGE_COLOR, width=2.5), marker=dict(size=9, symbol="square"),
    text=[f"{v:.1f}%" for v in stor["storage_writes_cold_pct"]], textposition="top center",
))
fig3.add_vrect(x0=14, x1=60, fillcolor="rgba(255,200,0,0.18)", line_width=0, layer="below",
               annotation_text="sweet spot", annotation_position="top right")
fig3.add_vline(x=180, line=dict(color="gray", width=1.5, dash="dash"),
               annotation_text="EIP-8188 target ≈ 180d", annotation_position="bottom left")
fig3.update_layout(
    title="State tiering tradeoff: cold-bucket size vs writes-to-cold"
          f"<br><sub>mainnet, block {totals['snapshot_block']:,}</sub>",
    xaxis=dict(title="W = active-window length (days, log)", type="log",
               tickvals=tradeoff["window_days"].tolist(), gridcolor="lightgray"),
    yaxis=dict(title="% of state (blue) / % of write events (green/red)",
               range=[0, 105], ticksuffix="%", gridcolor="lightgray"),
    template="plotly_white", width=1300, height=650,
    legend=dict(x=0.99, y=0.55, xanchor="right", bgcolor="rgba(255,255,255,0.95)"),
)
fig3.write_image(DATA_DIR / "tradeoff_cold_vs_writes.png", scale=2)
show(fig3)

# Key deltas between adjacent windows.
print("\nKey deltas — change between adjacent W values")
print("-" * 75)
print(f"{'transition':<22}{'state cold Δ':>14}{'acct writes Δ':>16}{'storage writes Δ':>18}")
for i in range(len(tradeoff) - 1):
    a, b = tradeoff.iloc[i], tradeoff.iloc[i + 1]
    def _d(col):
        return f"{b[col]-a[col]:+.2f} pp" if pd.notna(a[col]) and pd.notna(b[col]) else "   —   "
    label = f"W={a['window_days']:.0f} → {b['window_days']:.0f}d"
    print(f"{label:<22}{_d('state_cold_pct'):>14}{_d('acct_writes_cold_pct'):>16}"
          f"{_d('storage_writes_cold_pct'):>18}")

# %%
# Chart 4 — gas concentration in the warm tier.
asymptote = gas["pct_update_gas_warm"].max()
fig4 = go.Figure()
fig4.add_trace(go.Scatter(x=[0.01, 100], y=[0.01, 100], mode="lines",
                          name="no concentration (y = x)", line=dict(color="gray", dash="dash")))
fig4.add_trace(go.Scatter(
    x=gas["pct_state_warm"], y=gas["pct_update_gas_warm"],
    mode="lines+markers+text", name="warm slots → update gas",
    line=dict(color=ACCOUNT_COLOR, width=2.5), marker=dict(size=11),
    text=[f"W={int(w)}d" for w in gas["window_days"]], textposition="top center",
))
for _, r in gas.iterrows():
    fig4.add_annotation(x=r["pct_state_warm"], y=r["pct_update_gas_warm"],
                        text=f"<b>{r['concentration_x']:.0f}×</b>", showarrow=False, yshift=-22,
                        font=dict(size=11, color=STORAGE_COLOR))
fig4.add_hline(y=asymptote, line=dict(color="#2E7D32", width=1, dash="dot"),
               annotation_text=f"max observed ≈ {asymptote:.0f}%", annotation_position="top right")
fig4.update_layout(
    title="Gas concentration in the warm tier — storage SSTORE updates"
          f"<br><sub>X = % of slots warm; Y = % of update-gas hitting them; N× = Y/X.  "
          f"mainnet, block {totals['snapshot_block']:,}</sub>",
    xaxis=dict(title="% of storage slots in WARM tier (log)", type="log", gridcolor="lightgray"),
    yaxis=dict(title="% of SSTORE update-gas hitting WARM tier", range=[0, 100],
               ticksuffix="%", gridcolor="lightgray"),
    template="plotly_white", width=1200, height=650,
    legend=dict(x=0.99, y=0.05, xanchor="right", bgcolor="rgba(255,255,255,0.95)"),
)
fig4.write_image(DATA_DIR / "gas_concentration.png", scale=2)
show(fig4)

print("\nGas concentration per window:")
print("-" * 60)
print(f"{'W':>5}{'state warm %':>16}{'update gas warm %':>20}{'concentration':>15}")
for _, r in gas.iterrows():
    print(f"{int(r['window_days']):>4}d{r['pct_state_warm']:>15.3f}%"
          f"{r['pct_update_gas_warm']:>18.2f}%{r['concentration_x']:>14.0f}×")
