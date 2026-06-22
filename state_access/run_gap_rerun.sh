#!/usr/bin/env bash
# Relaunch the gap-anchor account rerun until it finishes. The T=180/365 account scans are
# heavy and can OOM-crash the node (non-zero exit); the rerun is resumable via its sidecar
# checkpoint, so we sleep and relaunch until it exits 0.
set -euo pipefail

cd /mnt/disk0/repos/xatu-analysis

for attempt in $(seq 1 300); do
  echo "=== gap rerun: launch attempt ${attempt} $(date -u +%FT%TZ) ==="
  if uv run python -m state_access.rerun_v2_gap_anchors; then
    echo "=== gap rerun finished cleanly on attempt ${attempt} ==="
    exit 0
  fi
  echo "=== exited non-zero (node may have OOM'd); resuming in 120s ==="
  sleep 120
done

echo "=== gave up after 300 attempts ===" >&2
exit 1
