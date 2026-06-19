#!/usr/bin/env bash
# Relaunch the account repatch until it finishes. The cluster node OOM-crashes under the
# heavy T=365 account scans, which surfaces as a non-zero exit; the repatch is resumable
# via its sidecar checkpoint, so we just sleep and relaunch until it exits 0.
set -euo pipefail

cd /mnt/disk0/repos/xatu-analysis

for attempt in $(seq 1 300); do
  echo "=== account repatch: launch attempt ${attempt} $(date -u +%FT%TZ) ==="
  if uv run python -m state_access.repatch_v2_account_sweep; then
    echo "=== account repatch finished cleanly on attempt ${attempt} ==="
    exit 0
  fi
  echo "=== exited non-zero (node may have OOM'd); resuming in 120s ==="
  sleep 120
done

echo "=== gave up after 300 attempts ===" >&2
exit 1
