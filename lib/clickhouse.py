"""Reusable ClickHouse access for xatu-analysis.

Connection details live in ``.env`` at the repo root. Two named profiles are
supported so an analysis can read bulk data from one cluster and reference data
(e.g. state totals) from another:

- ``primary``     — the personal Xatu node holding the canonical_execution_*_diffs tables.
- ``ethpandaops`` — the public ethPandaOps Xatu cluster (execution_state_size totals).

Typical use::

    from lib.clickhouse import run_query

    df = run_query("SELECT count() FROM canonical_execution_storage_diffs")
    totals = run_query("SELECT accounts FROM execution_state_size LIMIT 1", profile="ethpandaops")
"""

from __future__ import annotations

import os
from pathlib import Path

import certifi
import clickhouse_connect
import pandas as pd
from clickhouse_connect.driver.client import Client
from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

# Map profile name -> the env-var prefix that configures it.
_PROFILE_PREFIX = {
    "primary": "XATU_CLICKHOUSE",
    "ethpandaops": "ETHPANDAOPS_CLICKHOUSE",
}

# Analytical queries over billions of rows need more than the default time budget.
_DEFAULT_SETTINGS = {"max_execution_time": 600}


def _profile_config(profile: str) -> dict[str, object]:
    """Read connection settings for ``profile`` from the environment.

    Raises:
        ValueError: if the profile is unknown or its host is not configured.
    """
    try:
        prefix = _PROFILE_PREFIX[profile]
    except KeyError:
        known = ", ".join(sorted(_PROFILE_PREFIX))
        raise ValueError(f"Unknown ClickHouse profile {profile!r}; known profiles: {known}")

    host = os.getenv(f"{prefix}_HOST")
    if not host:
        raise ValueError(
            f"{prefix}_HOST is not set. Copy .env.example to .env and fill in the {profile} profile."
        )

    protocol = os.getenv(f"{prefix}_PROTOCOL", "http").lower()
    secure = protocol == "https"
    config: dict[str, object] = {
        "host": host,
        "port": int(os.getenv(f"{prefix}_PORT", "8123")),
        "username": os.getenv(f"{prefix}_USERNAME", "default"),
        "password": os.getenv(f"{prefix}_PASSWORD", ""),
        "secure": secure,
        "connect_timeout": 30,
        "send_receive_timeout": 600,  # heavy reads/inserts over Tailscale can be slow
    }
    if secure:
        # Use certifi's CA bundle; the macOS system Python often can't locate one.
        config["ca_cert"] = certifi.where()
    return config


def get_client(profile: str = "primary") -> Client:
    """Open a ClickHouse client for the named profile."""
    return clickhouse_connect.get_client(**_profile_config(profile))


def run_query(
    sql: str,
    profile: str = "primary",
    settings: dict[str, object] | None = None,
) -> pd.DataFrame:
    """Run ``sql`` against the named profile and return a DataFrame.

    Args:
        sql: The query to execute.
        profile: Which configured cluster to use (``primary`` or ``ethpandaops``).
        settings: Optional ClickHouse query settings, merged over the defaults.
    """
    merged = {**_DEFAULT_SETTINGS, **(settings or {})}
    client = get_client(profile)
    try:
        return client.query_df(sql, settings=merged)
    finally:
        client.close()
