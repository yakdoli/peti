#!/usr/bin/env bash
set -u -o pipefail

ROOT_DIR="${ROOT_DIR:-/root/peti}"
START_YEAR="${START_YEAR:-2003}"
END_YEAR="${END_YEAR:-2026}"
LOG="${LOG:-logs/catchup/metadata_full_then_pdf_catchup.log}"
STATE_FILE="${STATE_FILE:-artifacts/searchThema/state/searchthema_metadata_full.json}"

cd "${ROOT_DIR}"
source .venv/bin/activate
mkdir -p logs/catchup artifacts/pety/state artifacts/searchThema/state artifacts/validation artifacts/state

{
  printf "\n==== session metadata_full_then_pdf_catchup restart %s mode=searchthema_from_%s list_size=%s page_delay=%s ====\n" \
    "$(date -u "+%F %T UTC")" \
    "${START_YEAR}" \
    "${SEARCH_LIST_SIZE:-500}" \
    "${SEARCH_PAGE_DELAY:-0.05}"

  export GWANBO_REQUEST_MIN_INTERVAL="${GWANBO_REQUEST_MIN_INTERVAL:-0.05}"
  export GWANBO_REQUEST_JITTER="${GWANBO_REQUEST_JITTER:-0.005}"
  export GWANBO_ASSUME_HOST_REACHABLE="${GWANBO_ASSUME_HOST_REACHABLE:-1}"
  export GWANBO_USER_AGENT="${GWANBO_USER_AGENT:-Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.7727.15 Safari/537.36}"
  export SEARCH_CONCURRENCY="${SEARCH_CONCURRENCY:-6}"
  export SEARCH_LIST_SIZE="${SEARCH_LIST_SIZE:-500}"
  export SEARCH_PAGE_DELAY="${SEARCH_PAGE_DELAY:-0.05}"
  export SEARCH_BROWSER_FALLBACK="${SEARCH_BROWSER_FALLBACK:-1}"
  export SEARCH_BROWSER_FALLBACK_CONCURRENCY="${SEARCH_BROWSER_FALLBACK_CONCURRENCY:-1}"
  export SEARCH_SESSION_POOL_SIZE="${SEARCH_SESSION_POOL_SIZE:-4}"
  export SEARCH_SESSION_POOL_PATH="${SEARCH_SESSION_POOL_PATH:-artifacts/state/searchthema_full_metadata_session_pool.json}"

  run_phase() {
    label="$1"
    shift
    printf "\n==== phase %s start %s ====\n" "${label}" "$(date -u "+%F %T UTC")"
    "$@"
    rc=$?
    printf "\n==== phase %s end rc=%s %s ====\n" "${label}" "${rc}" "$(date -u "+%F %T UTC")"
    return "${rc}"
  }

  metadata_rc=0
  for year in $(seq "${START_YEAR}" "${END_YEAR}"); do
    run_phase "searchthema_metadata_full_${year}" \
      python crawl_search_thema.py \
        --year "${year}" \
        --metadata-only \
        --resume \
        --http-only \
        --no-preload-metadata \
        --no-save-indexes \
        --state-file "${STATE_FILE}" \
        --concurrency "${SEARCH_CONCURRENCY}"
    phase_rc=$?
    if [ "${phase_rc}" -ne 0 ]; then
      metadata_rc="${phase_rc}"
      break
    fi
  done

  printf "\n==== phase searchthema_metadata_full end rc=%s %s ====\n" "${metadata_rc}" "$(date -u "+%F %T UTC")"
  if [ "${metadata_rc}" -ne 0 ]; then
    exit "${metadata_rc}"
  fi

  run_phase repair_pdf_refs \
    python scripts/repair_artifact_pdf_refs.py \
      --apply \
      --manifest artifacts/searchThema/state/pdf_repair_manifest_after_full_metadata.jsonl \
      --output-dir artifacts/validation || exit $?

  run_phase missing_pdf_catchup \
    python scripts/download_searchthema_manifest.py \
      --manifest artifacts/searchThema/state/pdf_repair_manifest_after_full_metadata.jsonl \
      --shard-count 1 \
      --shard-index 0 \
      --concurrency 6 \
      --no-preload-metadata \
      --browser-download-fallback \
      --failure-log logs/catchup/searchthema_after_full_metadata_failures.jsonl \
      --progress-interval 25 || exit $?

  printf "\n==== session metadata_full_then_pdf_catchup end rc=0 %s ====\n" "$(date -u "+%F %T UTC")"
} 2>&1 | tee -a "${LOG}"
exit "${PIPESTATUS[0]}"
