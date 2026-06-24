#!/usr/bin/env bash
# Relaunch the account warm-update coverage collection until it finishes. Heavy T=180/365
# scans can OOM-crash the node (non-zero exit); the collector is resumable via its sidecar
# checkpoint, so we sleep and relaunch until it exits 0.
set -euo pipefail

cd /mnt/disk0/repos/xatu-analysis

for attempt in $(seq 1 300); do
  echo "=== acct coverage: launch attempt ${attempt} $(date -u +%FT%TZ) ==="
  if uv run python -m state_access.collect_v2_acct_coverage; then
    echo "=== acct coverage finished cleanly on attempt ${attempt} ==="
    exit 0
  fi
  echo "=== exited non-zero (node may have OOM'd); resuming in 120s ==="
  sleep 120
done

echo "=== gave up after 300 attempts ===" >&2
exit 1
