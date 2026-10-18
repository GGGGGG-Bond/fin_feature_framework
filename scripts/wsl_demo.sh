#!/usr/bin/env bash
# Invoked by run-demo.cmd. Compute on the Linux filesystem, export viewable files.
set -euo pipefail
source_dir="$(wslpath -u -- "$1")"
python_bin="${PYTHON:-python3}"
"$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else "Python 3.12+ is required inside WSL.")'
target="$(mktemp -d /tmp/fin-feature-wsl.XXXXXXXX)"
"$python_bin" - "$source_dir" "$target" <<'PY'
import shutil,sys
from pathlib import Path
source,target=map(Path,sys.argv[1:])
# Copy only runtime code and public fixtures. Keep source and its history intact.
for name in ('fin_feature','examples','scripts'):
    shutil.copytree(source/name,target/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
for name in ('run.sh','pyproject.toml','requirements-tested.txt','config.yaml'):
    shutil.copy2(source/name,target/name)
PY
cd "$target"
bash run.sh --output-dir "$target/data/result"
"$target/.venv/bin/python" scripts/export_results.py --input "$target/data/result/showcase" --output "$source_dir/data/windows-demo"
printf '\nLinux runtime (including reconstructed evidence): %s\n' "$target"
