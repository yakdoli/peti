#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/workspace/peti"
VENV_ACTIVATE="${ROOT_DIR}/.venv/bin/activate"
PETY_START_YEAR="${PETY_START_YEAR:-2002}"
PETY_MAX_SESSIONS="${PETY_MAX_SESSIONS:-12}"
SEARCH_CONCURRENCY="${SEARCH_CONCURRENCY:-6}"
LOG_DIR="${ROOT_DIR}/logs/orchestrator"
mkdir -p "$LOG_DIR"

install_tmux_if_missing() {
  if command -v tmux >/dev/null 2>&1; then return 0; fi
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y tmux
  else
    echo "tmux is required" >&2
    exit 1
  fi
}

wait_for_prefix_done() {
  local prefix="$1"
  while true; do
    local alive
    alive=$(tmux ls 2>/dev/null | rg -c "^${prefix}_" || true)
    if [[ -z "$alive" || "$alive" -eq 0 ]]; then
      break
    fi
    echo "[$(date -u '+%F %T UTC')] waiting: ${prefix} alive=${alive}" | tee -a "$LOG_DIR/run_pety_then_searchthema.log"
    sleep 30
  done
}

start_pety_sessions() {
  local y
  for ((i=0; i<PETY_MAX_SESSIONS; i++)); do
    y=$((PETY_START_YEAR + i))
    local s="pety_${y}"
    tmux has-session -t "$s" 2>/dev/null && continue
    tmux new -d -s "$s" "cd ${ROOT_DIR} && source ${VENV_ACTIVATE} && python crawl.py --theme pety --resume --start-date ${y}-01-01 --end-date ${y}-12-31 --state-file artifacts/pety/state/crawl_state_${y}.json"
    echo "started $s" | tee -a "$LOG_DIR/run_pety_then_searchthema.log"
  done
}

run_s3_staging() {
  if [[ -x "${ROOT_DIR}/scripts/sync_to_spaces.sh" ]]; then
    echo "start s3 staging" | tee -a "$LOG_DIR/run_pety_then_searchthema.log"
    (cd "$ROOT_DIR" && ./scripts/sync_to_spaces.sh) | tee -a "$LOG_DIR/run_pety_then_searchthema.log"
  else
    echo "sync_to_spaces.sh not found, skip s3 staging" | tee -a "$LOG_DIR/run_pety_then_searchthema.log"
  fi
}

start_searchthema_catchup() {
  local s="searchthema_catchup"
  tmux has-session -t "$s" 2>/dev/null && tmux kill-session -t "$s"
  tmux new -d -s "$s" "cd ${ROOT_DIR} && source ${VENV_ACTIVATE} && python crawl_search_thema.py --resume --concurrency ${SEARCH_CONCURRENCY} --state-file artifacts/searchThema/state/crawl_state_catchup.json"
  echo "started $s" | tee -a "$LOG_DIR/run_pety_then_searchthema.log"
}

cd "$ROOT_DIR"
install_tmux_if_missing
mkdir -p artifacts/pety/state artifacts/searchThema/state
start_pety_sessions
wait_for_prefix_done "pety"
run_s3_staging
start_searchthema_catchup

echo "orchestration completed" | tee -a "$LOG_DIR/run_pety_then_searchthema.log"
