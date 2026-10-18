#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(uname -s)" != Linux ]]; then
  echo 'Linux is required. On Windows, run this command inside WSL.' >&2
  exit 2
fi
python_bin="${PYTHON:-python3}"
if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo 'Install Python 3.12+ with venv/pip support, or set PYTHON=/path/to/python3.12.' >&2
  exit 2
fi
"$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else "Python 3.12+ is required; set PYTHON to a supported interpreter.")'
if [[ ! -x .venv/bin/python ]]; then
  if ! "$python_bin" -m venv .venv; then
    echo 'Cannot create .venv. Install venv/ensurepip support for this Python, then retry.' >&2
    exit 2
  fi
fi
.venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else "Existing .venv uses Python <3.12; choose a new checkout or recreate that environment.")'
signature="$(.venv/bin/python -c 'import hashlib,pathlib,sys;print(hashlib.sha256(pathlib.Path("pyproject.toml").read_bytes()+pathlib.Path("requirements-tested.txt").read_bytes()+sys.version.encode()).hexdigest())')"
if [[ "$(cat .venv/.demo-installed 2>/dev/null || true)" != "$signature" ]]; then
  echo 'Preparing demo dependencies (first run requires package network)...'
  .venv/bin/python -m pip install --disable-pip-version-check -c requirements-tested.txt -e .
  .venv/bin/python -m pip check
  printf '%s' "$signature" > .venv/.demo-installed
else
  .venv/bin/python -m pip check
fi
printf '\nReady. Run: bash run.sh\n'
