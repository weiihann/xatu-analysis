# state_access

Two generations of the Ethereum state-access analysis.

- **v1** — hot vs cold state and the EIP-8188 tiering tradeoff. Static snapshot plus a W=30
  post-Merge sweep. See [v1/README.md](v1/README.md) and [v1/REPORT.md](v1/REPORT.md).
- **v2** — reads-aware: write/read structure, warmth, concentration, and the EIP-8295
  tiering counterfactual, replayed weekly across post-Merge history. The current work.
  See [v2/REPORT_v2.md](v2/REPORT_v2.md) and [v2/HANDOVER_v2.md](v2/HANDOVER_v2.md).

## Layout

- `v1/`, `v2/` — code and report for each generation. Run modules as
  `uv run python -m state_access.v1.<module>` or `state_access.v2.<module>`.
- `config.py`, `history_config.py` — shared constants (block cadence, network, fork blocks,
  block-to-date mapping). `history_config` is also imported by the sibling `state_delta` and
  `bal_size` projects, so it stays at the top level.
- `data/v1/`, `data/v2/` — outputs for each generation.

Connection profiles live in `../.env` (`primary` local node, `ethpandaops` cluster), read via
`lib/clickhouse.py`.
