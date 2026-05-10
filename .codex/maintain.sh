#!/usr/bin/env bash
set -euo pipefail

if [ ! -x .venv/bin/python ]; then
    exec ./.codex/setup.sh
fi

source .venv/bin/activate
python -m pip install --upgrade -r requirements.txt
python -m playwright install chromium
