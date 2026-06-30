#!/usr/bin/env bash
# Relaunch the read-through sweep until it finishes. The strict ordering-aware query uses a
# window function over all in-window events, which can OOM-crash the node on the densest
# anchors (non-zero exit); the sweep is resumable via its parquet, so we sleep and relaunch
# until it exits 0.
set -euo pipefail

cd /mnt/disk0/repos/xatu-analysis

for attempt in $(seq 1 300); do
  echo "=== read-through sweep: launch attempt ${attempt} $(date -u +%FT%TZ) ==="
  if uv run python -m state_access.v2.collect_read_through; then
    echo "=== read-through sweep finished cleanly on attempt ${attempt} ==="
    exit 0
  fi
  echo "=== exited non-zero (node may have OOM'd); resuming in 120s ==="
  sleep 120
done

echo "=== gave up after 300 attempts ===" >&2
exit 1
