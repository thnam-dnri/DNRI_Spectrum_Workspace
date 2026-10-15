#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo_dir"
if [[ "$(uname -s)" == Darwin && -x outputs/dsw/dist/DSW.app/Contents/MacOS/DSW ]]; then
    exec outputs/dsw/dist/DSW.app/Contents/MacOS/DSW "$@"
fi
if [[ -x outputs/dsw/dist/DSW/DSW ]]; then
    exec outputs/dsw/dist/DSW/DSW "$@"
fi
python_bin="${DSW_PYTHON:-$repo_dir/outputs/dsw/venv/bin/python}"
if [[ -x "$python_bin" ]]; then
    exec "$python_bin" scripts/run_dsw.py "$@"
fi
echo 'Create the GUI environment first: bash scripts/setup_dsw.sh' >&2
exit 1
