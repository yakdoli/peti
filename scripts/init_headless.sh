#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
PLAYWRIGHT_BROWSER="${PLAYWRIGHT_BROWSER:-chromium}"
PLAYWRIGHT_TEST_URL="${PLAYWRIGHT_TEST_URL:-http://example.com}"
PLAYWRIGHT_RETRIES="${PLAYWRIGHT_RETRIES:-3}"
PLAYWRIGHT_TIMEOUT_MS="${PLAYWRIGHT_TIMEOUT_MS:-30000}"

# Optional HF artifact upload
HF_UPLOAD_ARTIFACT="${HF_UPLOAD_ARTIFACT:-0}"
HF_REPO_ID="${HF_REPO_ID:-}"
HF_REPO_TYPE="${HF_REPO_TYPE:-dataset}"
HF_PRIVATE="${HF_PRIVATE:-0}"
HF_ARTIFACT_PATH="${HF_ARTIFACT_PATH:-}"
HF_ARTIFACT_REPO_PATH="${HF_ARTIFACT_REPO_PATH:-artifacts/headless_smoke.txt}"
HF_COMMIT_MESSAGE="${HF_COMMIT_MESSAGE:-chore: upload headless smoke artifact}"

retry() {
  local attempts="$1"
  shift
  local i=1
  until "$@"; do
    if [[ "$i" -ge "$attempts" ]]; then
      echo "명령 실패(재시도 소진): $*" >&2
      return 1
    fi
    echo "명령 실패, ${i}/${attempts} 재시도 후 재실행: $*" >&2
    i=$((i + 1))
    sleep 2
  done
}

run_with_fallback() {
  local preferred=("$@")
  if "${preferred[@]}"; then
    return 0
  fi
  echo "기본 옵션 실패, --with-deps 없이 재시도합니다: ${preferred[*]}" >&2
  if [[ "${preferred[0]}" == "$PYTHON_BIN" && "${preferred[1]}" == "-m" && "${preferred[2]}" == "playwright" && "${preferred[3]}" == "install" ]]; then
    "$PYTHON_BIN" -m playwright install "$PLAYWRIGHT_BROWSER"
  else
    return 1
  fi
}

echo "[1/6] Python/pip 확인"
"$PYTHON_BIN" --version
"$PYTHON_BIN" -m pip --version

echo "[2/6] Python 의존성 설치"
retry "$PLAYWRIGHT_RETRIES" "$PYTHON_BIN" -m pip install -r requirements.txt

echo "[3/6] Playwright 브라우저 의존 라이브러리 설치"
if command -v apt-get >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
  retry "$PLAYWRIGHT_RETRIES" "$PYTHON_BIN" -m playwright install-deps "$PLAYWRIGHT_BROWSER"
else
  echo "패키지 매니저 없음: install-deps 단계 건너뜀"
fi

echo "[4/6] Playwright ${PLAYWRIGHT_BROWSER} 설치"
retry "$PLAYWRIGHT_RETRIES" "$PYTHON_BIN" -m playwright install "$PLAYWRIGHT_BROWSER"

echo "[5/6] headless 브라우저 스모크 테스트"
retry "$PLAYWRIGHT_RETRIES" "$PYTHON_BIN" - <<PY
from playwright.sync_api import sync_playwright

browser_name = "${PLAYWRIGHT_BROWSER}"
url = "${PLAYWRIGHT_TEST_URL}"
timeout_ms = int("${PLAYWRIGHT_TIMEOUT_MS}")

with sync_playwright() as p:
    browser_type = getattr(p, browser_name)
    browser = browser_type.launch(headless=True)
    page = browser.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    print("TITLE:", page.title())
    browser.close()
PY

echo "[6/6] Optional: Hugging Face 아티팩트 업로드"
if [[ "$HF_UPLOAD_ARTIFACT" == "1" ]]; then
  if [[ -z "$HF_REPO_ID" ]]; then
    echo "HF_UPLOAD_ARTIFACT=1 이지만 HF_REPO_ID가 비어 있습니다." >&2
    exit 1
  fi

  if [[ -z "$HF_ARTIFACT_PATH" ]]; then
    HF_ARTIFACT_PATH="${ROOT_DIR}/data/searchThema/state/headless_smoke_$(date -u +%Y%m%dT%H%M%SZ).txt"
    mkdir -p "$(dirname "$HF_ARTIFACT_PATH")"
    {
      echo "timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "browser=${PLAYWRIGHT_BROWSER}"
      echo "url=${PLAYWRIGHT_TEST_URL}"
      echo "status=ok"
    } > "$HF_ARTIFACT_PATH"
  fi

  HF_PRIVATE_BOOL="False"
  if [[ "$HF_PRIVATE" == "1" ]]; then
    HF_PRIVATE_BOOL="True"
  fi

  "$PYTHON_BIN" - <<PY
from pathlib import Path
from huggingface_hub import HfApi

repo_id = "${HF_REPO_ID}"
repo_type = "${HF_REPO_TYPE}"
private = ${HF_PRIVATE_BOOL}
local_path = Path("${HF_ARTIFACT_PATH}")
path_in_repo = "${HF_ARTIFACT_REPO_PATH}"
message = "${HF_COMMIT_MESSAGE}"

if not local_path.exists():
    raise FileNotFoundError(f"artifact not found: {local_path}")

api = HfApi()
api.create_repo(repo_id=repo_id, repo_type=repo_type, private=private, exist_ok=True)
api.upload_file(
    path_or_fileobj=str(local_path),
    path_in_repo=path_in_repo,
    repo_id=repo_id,
    repo_type=repo_type,
    commit_message=message,
)
print(f"uploaded: {local_path} -> hf://{repo_type}/{repo_id}/{path_in_repo}")
PY
else
  echo "HF_UPLOAD_ARTIFACT=0: 업로드 단계 건너뜀"
fi

echo "완료: headless ${PLAYWRIGHT_BROWSER} 초기화 및 검증 성공"
