#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
bash scripts/setup_demo.sh
exec .venv/bin/python scripts/demo.py "$@"
