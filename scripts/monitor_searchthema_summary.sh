#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-/root/peti}"
INTERVAL="${MONITOR_INTERVAL:-15}"
SEARCHTHEMA_TARGET_GB="${SEARCHTHEMA_TARGET_GB:-147}"
PETY_TARGET_GB="${PETY_TARGET_GB:-6.8}"

cd "${ROOT_DIR}"

active_log_files() {
  tmux list-sessions -F '#{session_name}' 2>/dev/null \
    | awk '
        /^searchthema_catchup_/ {print "logs/catchup/" $1 ".log"}
        /^searchthema_newpdf_/ {print "logs/catchup/" $1 ".log"}
        /^searchthema_fullpdf_/ {print "logs/catchup/" $1 ".log"}
        /^metadata_full_then_pdf_catchup$/ {print "logs/catchup/metadata_full_then_pdf_catchup.log"}
      ' \
    | while read -r log_file; do
        [[ -f "${log_file}" ]] && printf '%s\n' "${log_file}"
      done
}

count_completed_since() {
  local start="$1"
  local files=()
  mapfile -t files < <(active_log_files)
  if [[ "${#files[@]}" -eq 0 ]]; then
    echo 0
    return
  fi
  awk -v start="${start}" '$1" "$2 >= start && /PDF 다운로드 완료/ {c++} END{print c+0}' "${files[@]}" 2>/dev/null
}

count_warnings_since() {
  local start="$1"
  local files=()
  mapfile -t files < <(active_log_files)
  if [[ "${#files[@]}" -eq 0 ]]; then
    echo 0
    return
  fi
  awk -v start="${start}" '$1" "$2 >= start && /(WARNING|Connection reset|Server disconnected|Timeout|재시도)/ {c++} END{print c+0}' "${files[@]}" 2>/dev/null
}

count_failures_since() {
  local start="$1"
  local files=()
  mapfile -t files < <(active_log_files)
  if [[ "${#files[@]}" -eq 0 ]]; then
    echo 0
    return
  fi
  awk -v start="${start}" '$1" "$2 >= start && /(PDF 다운로드 실패 \(|download_failed|Traceback)/ {c++} END{print c+0}' "${files[@]}" 2>/dev/null
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

s3_sync_line() {
  local s3_log progress_line uploaded total bytes_uploaded pct
  s3_log="$(ls -t logs/s3/sync_artifacts_*.log 2>/dev/null | head -1 || true)"
  if [[ -z "${s3_log}" ]]; then
    echo "s3 sync       no log"
    return
  fi

  if grep -q '^==== session s3_sync_artifacts end rc=' "${s3_log}" 2>/dev/null; then
    grep '^==== session s3_sync_artifacts end rc=' "${s3_log}" | tail -1 | sed 's/^/s3 sync       /'
    return
  fi

  progress_line="$(grep -E 'upload progress uploaded=' "${s3_log}" 2>/dev/null | tail -1 || true)"
  if [[ "${progress_line}" =~ uploaded=([0-9]+)/([0-9]+).*bytes_uploaded=([0-9]+) ]]; then
    uploaded="${BASH_REMATCH[1]}"
    total="${BASH_REMATCH[2]}"
    bytes_uploaded="${BASH_REMATCH[3]}"
    pct="$(awk -v u="${uploaded}" -v t="${total}" 'BEGIN{if(t>0) printf "%.1f", 100*u/t; else print "0.0"}')"
    awk -v u="${uploaded}" -v t="${total}" -v p="${pct}" -v b="${bytes_uploaded}" -v log_file="${s3_log}" \
      'BEGIN{printf "s3 sync       %s/%s files (%s%%), uploaded %.2fGB, log=%s\n", u, t, p, b/1e9, log_file}'
  else
    echo "s3 sync       waiting/probing, log=${s3_log}"
  fi
}

while true; do
  now_epoch="$(date +%s)"
  start_1m="$(date -d "@$((now_epoch - 60))" "+%F %T")"
  start_5m="$(date -d "@$((now_epoch - 300))" "+%F %T")"
  start_10m="$(date -d "@$((now_epoch - 600))" "+%F %T")"

  active_sessions="$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -Ec '^(searchthema_catchup_|searchthema_newpdf_|searchthema_fullpdf_|metadata_full_then_pdf_catchup|metadata_gap_audit_after_pipeline|s3_sync_artifacts)' || true)"
  active_logs=()
  mapfile -t active_logs < <(active_log_files)
  active_procs="$(ps -eo comm=,args= | awk '$1 ~ /^python/ && /(crawl_search_thema.py|download_searchthema_manifest.py|sync_spaces_artifacts.py|audit_metadata_coverage.py|crawl.py)/ {c++} END{print c+0}')"
  artifact_size="$(du -sh artifacts 2>/dev/null | awk '{print $1}')"
  pdf_count="$(find artifacts/searchThema/pdfs artifacts/pety/pdfs -type f -name '*.pdf' 2>/dev/null | wc -l)"
  search_metadata_count="$(find artifacts/searchThema/metadata/items -type f -name '*.json' 2>/dev/null | wc -l)"

  first_pid="$(ps -eo pid=,comm=,args= | awk '$2 ~ /^python/ && /crawl_search_thema.py/ {print $1; exit}')"
  if [[ -z "${first_pid}" ]]; then
    first_pid="$(ps -eo pid=,comm=,args= | awk '$2 ~ /^python/ && /download_searchthema_manifest.py/ {print $1; exit}')"
  fi
  settings="no active crawler process"
  if [[ -n "${first_pid}" && -r "/proc/${first_pid}/environ" ]]; then
    settings="$(tr '\0' '\n' < "/proc/${first_pid}/environ" | grep -E '^(GWANBO_REQUEST_MIN_INTERVAL|GWANBO_REQUEST_JITTER|GWANBO_USER_AGENT|SEARCH_CONCURRENCY|SEARCH_LIST_SIZE|SEARCH_PAGE_DELAY|GWANBO_ASSUME_HOST_REACHABLE|SEARCH_BROWSER_FALLBACK|SEARCH_BROWSER_FALLBACK_CONCURRENCY|SEARCH_SESSION_POOL_SIZE|SEARCH_SESSION_POOL_PATH|SEARCH_DIRECT_PDF_DOWNLOAD)=' | sort | xargs echo)"
  fi

  clear || true
  echo "searchThema crawl monitor"
  echo "now_utc: $(date -u '+%F %T UTC')"
  echo "active_sessions=${active_sessions} active_python=${active_procs} artifacts=${artifact_size} pdf_count=${pdf_count} search_metadata=${search_metadata_count}"
  echo "settings: ${settings}"
  echo
  echo "active tmux"
  tmux list-sessions -F '  #{session_name}: #{session_windows} windows #{session_attached}' 2>/dev/null \
    | grep -E '  (searchthema_catchup_|searchthema_newpdf_|searchthema_fullpdf_|metadata_full_then_pdf_catchup|metadata_gap_audit_after_pipeline|s3_sync_artifacts)' || true
  echo
  s3_sync_line
  echo
  echo "metadata phase"
  grep -E 'SearchThema 조합 수집:|^==== phase searchthema_metadata_full' logs/catchup/metadata_full_then_pdf_catchup.log 2>/dev/null | tail -6 | sed 's/^/  /' || true
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
  if [[ "${#active_logs[@]}" -gt 0 ]]; then
    awk -v start="${start_10m}" '$1" "$2 >= start && /PDF 다운로드 완료/ {m=substr($1" "$2,1,16); c[m]++} END{for(m in c) print m,c[m]}' "${active_logs[@]}" 2>/dev/null | sort | tail -10
  fi

  echo
  echo "per-session completions (last 5m)"
  for f in "${active_logs[@]}"; do
    printf "%-28s " "$(basename "${f}")"
    awk -v start="${start_5m}" '$1" "$2 >= start && /PDF 다운로드 완료/ {c++} END{print c+0}' "${f}"
  done | sort -V

  echo
  echo "recent failures"
  if [[ "${#active_logs[@]}" -gt 0 ]]; then
    awk -v start="${start_10m}" '$1" "$2 >= start && /(PDF 다운로드 실패 \(|download_failed|Traceback|HTTP 500|HTTP 429|HTTP 503)/ {print FILENAME ":" $0}' "${active_logs[@]}" 2>/dev/null | tail -8
  fi

  sleep "${INTERVAL}"
done
