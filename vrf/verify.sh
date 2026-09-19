#!/usr/bin/env bash
set -euo pipefail

# Print vrf.py hash for the user to compare with the on-chain
# round's vrf_algorithm_hash, then run the verifier.
echo "=========================================="
echo "vrf.py sha256:"
sha256sum /app/vrf.py
echo "=========================================="
exec python3 /app/verify.py "$@"
