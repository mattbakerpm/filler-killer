#!/usr/bin/env bash
# Launch Filler Killer.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "No .venv found. Run ./setup.sh first."
  exit 1
fi
# speaker-gate helper (Core Audio process tap); rebuild when the source changes
if [ ! -x fk-systap ] || [ fk-systap.swift -nt fk-systap ]; then
  swiftc -O fk-systap.swift -o fk-systap || echo "warning: fk-systap build failed; speaker gate off"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
exec python coach.py "$@"
