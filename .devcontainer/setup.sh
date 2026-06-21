#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

python -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt

if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    python -m playwright install --with-deps chromium
else
    python -m playwright install chromium
fi

python -m pytest --version
