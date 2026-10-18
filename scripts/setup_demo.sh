#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -c requirements-tested.txt -e '.[dev]'
printf '\nReady: .venv/bin/python scripts/demo.py\n'
