#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-/root/peti}"
PY="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
LOG_DIR="${ROOT_DIR}/logs/catchup"
GWANBO_REQUEST_MIN_INTERVAL="${GWANBO_REQUEST_MIN_INTERVAL:-0.2}"
GWANBO_REQUEST_JITTER="${GWANBO_REQUEST_JITTER:-0.05}"
SEARCH_CONCURRENCY="${SEARCH_CONCURRENCY:-1}"

mkdir -p \
  "${ROOT_DIR}/artifacts/pety/state" \
  "${ROOT_DIR}/artifacts/searchThema/state" \
  "${LOG_DIR}"

start_pety() {
  local idx="$1" start="$2" end="$3"
  local session="pety_catchup_${idx}"
  local log="${LOG_DIR}/${session}.log"
  tmux has-session -t "${session}" 2>/dev/null && tmux kill-session -t "${session}"
  tmux new -d -s "${session}" \
    "cd ${ROOT_DIR} && PYTHONUNBUFFERED=1 GWANBO_REQUEST_MIN_INTERVAL=${GWANBO_REQUEST_MIN_INTERVAL} GWANBO_REQUEST_JITTER=${GWANBO_REQUEST_JITTER} bash -lc 'for attempt in 1 2 3 4 5; do echo ===== attempt \$attempt for ${start}..${end} =====; ${PY} crawl.py --theme pety --resume --start-date ${start} --end-date ${end} --window-days 31 --state-file artifacts/pety/state/${session}.json --no-save-indexes && exit 0; sleep 20; done; exit 1' >> ${log} 2>&1"
  echo "started ${session}: ${start}..${end}"
}

start_search() {
  local idx="$1" years="$2"
  local session="searchthema_catchup_${idx}"
  local log="${LOG_DIR}/${session}.log"
  tmux has-session -t "${session}" 2>/dev/null && tmux kill-session -t "${session}"
  tmux new -d -s "${session}" \
    "cd ${ROOT_DIR} && PYTHONUNBUFFERED=1 GWANBO_REQUEST_MIN_INTERVAL=${GWANBO_REQUEST_MIN_INTERVAL} GWANBO_REQUEST_JITTER=${GWANBO_REQUEST_JITTER} bash -lc 'for y in ${years}; do echo ===== searchThema year: \$y =====; ${PY} crawl_search_thema.py --year \$y --resume --concurrency ${SEARCH_CONCURRENCY} --state-file artifacts/searchThema/state/searchthema_\$y.json; done' >> ${log} 2>&1"
  echo "started ${session}: years={${years}}"
}

start_pety 1 2007-01-01 2009-12-31
start_pety 2 2010-01-01 2012-12-31
start_pety 3 2013-01-01 2015-12-31
start_pety 4 2016-01-01 2018-12-31
start_pety 5 2019-01-01 2021-12-31
start_pety 6 2022-01-01 today

start_search 1 "2008 2009 2010"
start_search 2 "2011 2012 2013"
start_search 3 "2014 2015 2016"
start_search 4 "2017 2018 2019"
start_search 5 "2020 2021 2022"
start_search 6 "2023 2024 2025 2026"

tmux ls | sort
