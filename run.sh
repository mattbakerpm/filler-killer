#!/usr/bin/env bash
# Launch Filler Coach.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "No .venv found. Run ./setup.sh first."
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
exec python coach.py "$@"
