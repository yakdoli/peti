#!/usr/bin/env bash
set -u -o pipefail

ROOT_DIR="${ROOT_DIR:-/root/peti}"
SESSION_NAME="${SESSION_NAME:?SESSION_NAME is required}"
MANIFEST="${MANIFEST:?MANIFEST is required}"
SHARD_INDEX="${SHARD_INDEX:?SHARD_INDEX is required}"
SHARD_COUNT="${SHARD_COUNT:-4}"
CONCURRENCY="${CONCURRENCY:-6}"
LOG="${LOG:?LOG is required}"
FAILURE_LOG="${FAILURE_LOG:?FAILURE_LOG is required}"
PROGRESS_INTERVAL="${PROGRESS_INTERVAL:-50}"

cd "${ROOT_DIR}"
source .venv/bin/activate
mkdir -p logs/catchup artifacts/state

export GWANBO_REQUEST_MIN_INTERVAL="${GWANBO_REQUEST_MIN_INTERVAL:-0.05}"
export GWANBO_REQUEST_JITTER="${GWANBO_REQUEST_JITTER:-0.005}"
export GWANBO_ASSUME_HOST_REACHABLE="${GWANBO_ASSUME_HOST_REACHABLE:-1}"
export GWANBO_USER_AGENT="${GWANBO_USER_AGENT:-Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.7727.15 Safari/537.36}"
export SEARCH_BROWSER_FALLBACK="${SEARCH_BROWSER_FALLBACK:-1}"
export SEARCH_BROWSER_FALLBACK_CONCURRENCY="${SEARCH_BROWSER_FALLBACK_CONCURRENCY:-1}"
export SEARCH_SESSION_POOL_SIZE="${SEARCH_SESSION_POOL_SIZE:-4}"
export SEARCH_SESSION_POOL_PATH="${SEARCH_SESSION_POOL_PATH:-artifacts/state/searchthema_newpdf_session_pool.json}"
export SEARCH_DIRECT_PDF_DOWNLOAD="${SEARCH_DIRECT_PDF_DOWNLOAD:-1}"

{
  printf "\n==== session %s restart %s manifest=%s shard=%s/%s concurrency=%s direct_pdf=%s ====\n" \
    "${SESSION_NAME}" \
    "$(date -u "+%F %T UTC")" \
    "${MANIFEST}" \
    "${SHARD_INDEX}" \
    "${SHARD_COUNT}" \
    "${CONCURRENCY}" \
    "${SEARCH_DIRECT_PDF_DOWNLOAD}"

  python scripts/download_searchthema_manifest.py \
    --manifest "${MANIFEST}" \
    --shard-count "${SHARD_COUNT}" \
    --shard-index "${SHARD_INDEX}" \
    --concurrency "${CONCURRENCY}" \
    --no-preload-metadata \
    --browser-download-fallback \
    --failure-log "${FAILURE_LOG}" \
    --progress-interval "${PROGRESS_INTERVAL}"

  rc=$?
  printf "\n==== session %s end rc=%s %s ====\n" "${SESSION_NAME}" "${rc}" "$(date -u "+%F %T UTC")"
  exit "${rc}"
} 2>&1 | tee -a "${LOG}"
exit "${PIPESTATUS[0]}"
