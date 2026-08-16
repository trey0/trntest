#!/bin/sh
# Runs the "heavy" pytest suite (tests marked @pytest.mark.heavy -- real SPICE kernel furnishing,
# live NAIF network fetches) inside Docker, since spiceypy/network access aren't available on the
# host. Plain `pytest` (no Docker needed) skips these by default (see pyproject.toml's addopts and
# README's Tests section). Extra arguments are passed through to pytest, e.g.:
#   scripts/run_heavy_tests.sh tests/test_maneuver_detection.py -k 2010 -v
set -e
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"
docker compose -f docker/docker-compose.yml run --rm demo pytest -m heavy "$@"
