#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo_dir"
python_bin="${DSW_PYTHON:-python3}"
"$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "DSW requires Python 3.11 or newer")'
environment="$repo_dir/outputs/dsw/venv"
if [[ ! -x "$environment/bin/python" ]]; then
    "$python_bin" -m venv "$environment"
fi
constraints=()
if [[ "$(uname -s)" == Linux ]]; then
    constraints=(-c config/dsw/requirements_linux.txt)
fi
"$environment/bin/python" -m pip install -r config/dsw/requirements_build.txt "${constraints[@]}"
"$environment/bin/python" -m pip check
mkdir -p outputs/dsw/validation
"$environment/bin/python" -m pip freeze > outputs/dsw/validation/requirements_host.txt
echo 'GUI environment ready. Launch with: bash scripts/run_dsw.sh'
