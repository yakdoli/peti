#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-/root/peti}"
INTERVAL="${MONITOR_INTERVAL:-15}"
SEARCHTHEMA_TARGET_GB="${SEARCHTHEMA_TARGET_GB:-147}"
PETY_TARGET_GB="${PETY_TARGET_GB:-6.8}"

cd "${ROOT_DIR}"

count_completed_since() {
  local start="$1"
  awk -v start="${start}" '$1" "$2 >= start && /PDF 다운로드 완료/ {c++} END{print c+0}' logs/catchup/searchthema_catchup_*.log 2>/dev/null
}

count_warnings_since() {
  local start="$1"
  awk -v start="${start}" '$1" "$2 >= start && /(WARNING|Connection reset|Server disconnected|Timeout|재시도)/ {c++} END{print c+0}' logs/catchup/searchthema_catchup_*.log 2>/dev/null
}

count_failures_since() {
  local start="$1"
  awk -v start="${start}" '$1" "$2 >= start && /(PDF 다운로드 실패 \(|download_failed|Traceback)/ {c++} END{print c+0}' logs/catchup/searchthema_catchup_*.log 2>/dev/null
}

format_duration() {
  local seconds="$1"
  if [[ "${seconds}" -lt 0 ]]; then
    echo "unknown"
    return
  fi
  local days=$((seconds / 86400))
  local hours=$(((seconds % 86400) / 3600))
  local mins=$(((seconds % 3600) / 60))
  if [[ "${days}" -gt 0 ]]; then
    printf "%dd %02dh %02dm" "${days}" "${hours}" "${mins}"
  else
    printf "%02dh %02dm" "${hours}" "${mins}"
  fi
}

source_eta_line() {
  local label="$1"
  local source_dir="$2"
  local pdf_dir="$3"
  local target_gb="$4"
  local rate_start="$5"
  local now_epoch="$6"

  local current_bytes target_bytes recent_bytes rate_bpm remaining eta_seconds eta_at eta_left
  current_bytes="$(du -sb "${source_dir}" 2>/dev/null | awk '{print $1+0}')"
  target_bytes="$(awk -v gb="${target_gb}" 'BEGIN{printf "%.0f", gb*1000*1000*1000}')"
  recent_bytes="$(find "${pdf_dir}" -type f -name '*.pdf' -newermt "${rate_start}" -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')"
  rate_bpm="$(awk -v b="${recent_bytes}" 'BEGIN{printf "%.0f", b/10}')"
  remaining=$((target_bytes - current_bytes))

  if [[ "${remaining}" -le 0 ]]; then
    eta_left="done"
    eta_at="done"
  elif [[ "${rate_bpm}" -le 0 ]]; then
    eta_left="unknown"
    eta_at="unknown"
  else
    eta_seconds="$(awk -v r="${remaining}" -v bpm="${rate_bpm}" 'BEGIN{printf "%.0f", r/(bpm/60)}')"
    eta_left="$(format_duration "${eta_seconds}")"
    eta_at="$(date -u -d "@$((now_epoch + eta_seconds))" '+%F %T UTC')"
  fi

  awk \
    -v label="${label}" \
    -v current="${current_bytes}" \
    -v target="${target_bytes}" \
    -v rate="${rate_bpm}" \
    -v left="${eta_left}" \
    -v at="${eta_at}" \
    'BEGIN{
      printf "%-12s %9.2fGB / %8.2fGB  %8.2fMB/min  ETA %-12s  %s\n",
        label, current/1e9, target/1e9, rate/1e6, left, at
    }'
}

while true; do
  now_epoch="$(date +%s)"
  start_1m="$(date -d "@$((now_epoch - 60))" "+%F %T")"
  start_5m="$(date -d "@$((now_epoch - 300))" "+%F %T")"
  start_10m="$(date -d "@$((now_epoch - 600))" "+%F %T")"

  active_sessions="$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -c '^searchthema_catchup_' || true)"
  active_procs="$(pgrep -af '^/root/peti/.venv/bin/python crawl_search_thema.py --year' | wc -l)"
  artifact_size="$(du -sh artifacts 2>/dev/null | awk '{print $1}')"
  pdf_count="$(find artifacts/searchThema/pdfs artifacts/pety/pdfs -type f -name '*.pdf' 2>/dev/null | wc -l)"

  first_pid="$(pgrep -f '^/root/peti/.venv/bin/python crawl_search_thema.py --year' | head -1 || true)"
  settings="no active crawler process"
  if [[ -n "${first_pid}" && -r "/proc/${first_pid}/environ" ]]; then
    settings="$(tr '\0' '\n' < "/proc/${first_pid}/environ" | grep -E '^(GWANBO_REQUEST_MIN_INTERVAL|GWANBO_REQUEST_JITTER|SEARCH_CONCURRENCY|SEARCH_LIST_SIZE|SEARCH_PAGE_DELAY|GWANBO_ASSUME_HOST_REACHABLE)=' | sort | xargs echo)"
  fi

  clear
  echo "searchThema crawl monitor"
  echo "now_utc: $(date -u '+%F %T UTC')"
  echo "active_sessions=${active_sessions} active_python=${active_procs} artifacts=${artifact_size} pdf_count=${pdf_count}"
  echo "settings: ${settings}"
  echo
  echo "capacity ETA (10m byte rate)"
  source_eta_line "searchThema" "artifacts/searchThema" "artifacts/searchThema/pdfs" "${SEARCHTHEMA_TARGET_GB}" "${start_10m}" "${now_epoch}"
  source_eta_line "pety" "artifacts/pety" "artifacts/pety/pdfs" "${PETY_TARGET_GB}" "${start_10m}" "${now_epoch}"
  echo
  printf "%-10s %8s %12s %10s %10s\n" "window" "done" "done/min" "warnings" "failures"
  for label in 1m 5m 10m; do
    case "${label}" in
      1m) start="${start_1m}"; mins=1 ;;
      5m) start="${start_5m}"; mins=5 ;;
      10m) start="${start_10m}"; mins=10 ;;
    esac
    done_count="$(count_completed_since "${start}")"
    warn_count="$(count_warnings_since "${start}")"
    fail_count="$(count_failures_since "${start}")"
    rate="$(awk -v c="${done_count}" -v m="${mins}" 'BEGIN{printf "%.1f", c/m}')"
    printf "%-10s %8s %12s %10s %10s\n" "${label}" "${done_count}" "${rate}" "${warn_count}" "${fail_count}"
  done

  echo
  echo "per-minute completions (last 10m)"
  awk -v start="${start_10m}" '$1" "$2 >= start && /PDF 다운로드 완료/ {m=substr($1" "$2,1,16); c[m]++} END{for(m in c) print m,c[m]}' logs/catchup/searchthema_catchup_*.log 2>/dev/null | sort | tail -10

  echo
  echo "per-session completions (last 5m)"
  for f in logs/catchup/searchthema_catchup_*.log; do
    [[ -e "${f}" ]] || continue
    printf "%-28s " "$(basename "${f}")"
    awk -v start="${start_5m}" '$1" "$2 >= start && /PDF 다운로드 완료/ {c++} END{print c+0}' "${f}"
  done | sort -V

  echo
  echo "recent failures"
  awk -v start="${start_10m}" '$1" "$2 >= start && /(PDF 다운로드 실패 \(|download_failed|Traceback|HTTP 500|HTTP 429|HTTP 503)/ {print FILENAME ":" $0}' logs/catchup/searchthema_catchup_*.log 2>/dev/null | tail -8

  sleep "${INTERVAL}"
done
