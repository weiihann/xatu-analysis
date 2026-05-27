# xatu-analysis

Ad-hoc analyses over [Xatu](https://github.com/ethpandaops/xatu) ClickHouse data.

Each analysis lives in its own subfolder and shares one ClickHouse access layer (`lib/`).

## Setup

```bash
uv sync                 # install dependencies
cp .env.example .env    # then fill in any required passwords
```

Credentials are read from `.env` (git-ignored). Two named connection profiles are supported:

| profile       | env prefix              | used for |
|---------------|-------------------------|----------|
| `primary`     | `XATU_CLICKHOUSE_*`      | bulk data (the personal Xatu node) |
| `ethpandaops` | `ETHPANDAOPS_CLICKHOUSE_*` | reference data, e.g. `execution_state_size` |

## The reusable ClickHouse layer

`lib/clickhouse.py` is the one pattern every analysis uses:

```python
from lib.clickhouse import run_query

df = run_query("SELECT count() FROM canonical_execution_storage_diffs")          # primary
totals = run_query("SELECT accounts FROM execution_state_size", profile="ethpandaops")
```

`run_query(sql, profile="primary", settings=None) -> pandas.DataFrame`. It opens a
short-lived client, applies a generous `max_execution_time`, and returns a DataFrame.

## Adding an analysis

1. Create a subfolder with an `__init__.py`.
2. Put parameterised SQL builders in `queries.py`, configuration in `config.py`.
3. A `collect.py` runs the queries and writes parquet/json into `<analysis>/data/`.
4. An `analysis.py` (`# %%` cells) loads those outputs and renders charts/tables —
   keeping the slow queries separate from fast, re-runnable plotting.

## Analyses

- [`state_access/`](state_access/) — hot vs cold Ethereum state and the EIP-8188
  state-tiering / gas-concentration view.
- [`state_delta/`](state_delta/) — net post-Merge growth of Ethereum live-state size.
- [`bal_size/`](bal_size/) — how big EIP-7928 block-level access lists would be, per block,
  over the trailing 6 months.
