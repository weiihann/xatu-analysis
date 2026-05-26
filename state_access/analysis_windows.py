"""Consolidate cold-state share across all swept windows into one chart.

`analysis_history.py` plots one window at a time; this overlays every window's combined
cold-state share — accounts and storage slots pooled into one population — on a shared
timeline, to test whether the static-snapshot levels are typical of post-Merge history or an
artifact of a quiet (high-cold) moment at the anchor block.

Reads the existing history_w{W}.parquet files — no database access.

    uv run python -m state_access.analysis_windows
"""

# %%
from __future__ import annotations

import pandas as pd
import plotly.graph_objs as go

from state_access.config import DATA_DIR
from state_access.history_config import FORKS, block_to_date, parquet_for

WINDOWS = [30, 90, 180, 365]
# Distinct hues per window (red is reserved for the static-anchor marker).
WINDOW_COLORS = {30: "#1565C0", 90: "#2E7D32", 180: "#EF6C00", 365: "#6A1B9A"}


def show(fig: go.Figure) -> None:
    """Display interactively, or skip if no renderer (PNG is the durable artifact)."""
    try:
        fig.show()
    except ValueError:
        pass


def load_window(w: int) -> pd.DataFrame:
    """Sweep rows for window `w`, with a combined (accounts + slots) cold share.

    Drops anchors with incomplete diff data. ``pct_state_cold`` pools both populations:
    cold objects / total objects, so storage slots (the larger population) dominate.
    """
    raw = pd.read_parquet(parquet_for(w)).sort_values("anchor_block")
    df = raw[raw["unique_storage_slots"] >= 0.7 * raw["unique_storage_slots"].median()].copy()
    cold = (df["total_accounts"] - df["unique_accounts"]) \
        + (df["total_storages"] - df["unique_storage_slots"])
    df["pct_state_cold"] = 100 * cold / (df["total_accounts"] + df["total_storages"])
    return df


frames = {w: load_window(w) for w in WINDOWS}


# %%
def anchor_percentile(s: pd.Series) -> float:
    """Percentile rank of the final (anchor) value within the window's own history."""
    anchor = s.iloc[-1]
    return 100 * (s <= anchor).mean()


print("Combined state-cold share (accounts + slots) — anchor vs its own post-Merge history")
print(f"{'W':>5}{'min':>9}{'mean':>9}{'max':>9}{'anchor':>9}{'pctile':>9}")
for w in WINDOWS:
    s = frames[w]["pct_state_cold"]
    print(f"{w:>4}d{s.min():>9.2f}{s.mean():>9.2f}{s.max():>9.2f}"
          f"{s.iloc[-1]:>9.2f}{anchor_percentile(s):>8.0f}%")


# %%
fig = go.Figure()
overall_min = min(f["date"].min() for f in frames.values())
overall_max = max(f["date"].max() for f in frames.values())

for w in WINDOWS:
    df = frames[w]
    fig.add_trace(go.Scatter(
        x=df["date"], y=df["pct_state_cold"], mode="lines", name=f"W={w}d",
        line=dict(color=WINDOW_COLORS[w], width=2.5),
    ))
    last = df.iloc[-1]
    fig.add_annotation(x=last["date"].strftime("%Y-%m-%d"), y=last["pct_state_cold"],
                       xanchor="left", xshift=6, text=f"<b>{last['pct_state_cold']:.0f}%</b>",
                       showarrow=False, font=dict(size=11, color=WINDOW_COLORS[w]))

# Forks within the swept range.
for name, block in FORKS.items():
    if overall_min <= block_to_date(block) <= overall_max:
        day = block_to_date(block).strftime("%Y-%m-%d")
        fig.add_vline(x=day, line=dict(color="gray", width=1, dash="dash"))
        fig.add_annotation(x=day, y=1.0, yref="paper", yanchor="bottom",
                           text=name, showarrow=False, font=dict(size=10, color="gray"))

# The static-snapshot anchor is the final sample of every sweep (right edge).
anchor_day = frames[30].iloc[-1]["date"].strftime("%Y-%m-%d")
fig.add_vline(x=anchor_day, line=dict(color="#B71C1C", width=1.5, dash="dot"))
fig.add_annotation(x=anchor_day, y=0.02, yref="paper", xanchor="right", xshift=-4,
                   text="static anchor", showarrow=False, font=dict(size=10, color="#B71C1C"))

fig.update_layout(
    title="Cold state share across windows, over post-Merge history"
          "<br><sub>accounts + storage slots pooled; weekly anchors; "
          "static snapshot is the final point of each line</sub>",
    xaxis=dict(title="date", gridcolor="lightgray"),
    yaxis=dict(title="% of state objects COLD (not modified within W)", ticksuffix="%",
               gridcolor="lightgray"),
    template="plotly_white", width=1300, height=600,
    legend=dict(x=0.01, y=0.02, bgcolor="rgba(255,255,255,0.85)"),
)
fig.write_image(DATA_DIR / "history_windows_cold.png", scale=2)
show(fig)
