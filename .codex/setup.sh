#!/usr/bin/env bash
set -euo pipefail

python -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install --upgrade -r requirements.txt
python -m playwright install chromium

if ! grep -q "peti codex environment" ~/.bashrc 2>/dev/null; then
    {
        echo ""
        echo "# peti codex environment"
        echo "source \"$PWD/.venv/bin/activate\""
    } >> ~/.bashrc
fi

python -m pytest --version
