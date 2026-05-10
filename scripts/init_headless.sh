#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"

echo "[1/5] Python/pip 확인"
"$PYTHON_BIN" --version
"$PYTHON_BIN" -m pip --version

echo "[2/5] Python 의존성 설치"
"$PYTHON_BIN" -m pip install -r requirements.txt

echo "[3/5] Playwright 브라우저 의존 라이브러리 설치"
"$PYTHON_BIN" -m playwright install-deps chromium

echo "[4/5] Playwright Chromium 설치"
"$PYTHON_BIN" -m playwright install chromium

echo "[5/5] headless 브라우저 스모크 테스트"
"$PYTHON_BIN" - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("http://example.com", wait_until="domcontentloaded", timeout=30000)
    print("TITLE:", page.title())
    browser.close()
PY

echo "완료: headless Chromium 초기화 및 검증 성공"
