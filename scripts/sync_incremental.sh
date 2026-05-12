#!/bin/bash
# Hugging Face 증분 동기화 스크립트 (Incremental Catch-up) v3.5
# 전략: 사전 배치 샤딩(Sharding) 기반 3개 워커 동시 실행

set -e

# 설정
SOURCE="/root/peti/artifacts/"
DEST="hf://buckets/yakdoli/peti-artifacts"
CHUNK_SIZE=1500
SLEEP_TIME=0.5
MAX_RETRIES=3
NUM_WORKERS=3  # 워커 수 (사전 샤딩 기준)
WORK_DIR="/root/peti/sync_work/incremental"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FULL_PLAN="$WORK_DIR/plan_$TIMESTAMP.jsonl"
FILTERED_PLAN="$WORK_DIR/filtered_$TIMESTAMP.jsonl"
LOG_FILE="$WORK_DIR/sync_$TIMESTAMP.log"

log() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "$timestamp - $1" | tee -a "$LOG_FILE"
}

mkdir -p "$WORK_DIR"
log "=== 증분 동기화 시작 (샤딩 모드: $NUM_WORKERS 워커) ==="

# Step 1: 분석 및 청킹
log "플랜 분석 및 청크 생성 중..."
hf sync --plan "$FULL_PLAN" "$SOURCE" "$DEST"

HEADER=$(head -n 1 "$FULL_PLAN")
UPLOADS=$(grep -c '"action": "upload"' "$FULL_PLAN" || echo "0")

if [ "$UPLOADS" -eq 0 ]; then
    log ">>> 결과: 업데이트할 내용이 없습니다. 종료합니다."
    rm -f "$FULL_PLAN"
    exit 0
fi

echo "$HEADER" > "$FILTERED_PLAN"
grep '"action": "upload"' "$FULL_PLAN" >> "$FILTERED_PLAN"

# 청크 분할
tail -n +2 "$FILTERED_PLAN" | split -l "$CHUNK_SIZE" -d --additional-suffix=.tmp - "$WORK_DIR/chunk_${TIMESTAMP}_"

# 독립 플랜 파일화
for TMP_FILE in "$WORK_DIR"/chunk_${TIMESTAMP}_*.tmp; do
    CHUNK_PLAN="${TMP_FILE%.tmp}.jsonl"
    echo "$HEADER" > "$CHUNK_PLAN"
    cat "$TMP_FILE" >> "$CHUNK_PLAN"
    rm "$TMP_FILE"
done

# 모든 청크 목록 확보
CHUNKS=($(ls "$WORK_DIR"/chunk_${TIMESTAMP}_*.jsonl | sort))
TOTAL_CHUNKS=${#CHUNKS[@]}
log "총 $TOTAL_CHUNKS 개의 청크를 $NUM_WORKERS 개의 워커로 샤딩합니다."

# Step 2: 워커별 샤딩 및 실행 함수
run_worker() {
    local worker_id=$1
    shift
    local my_chunks=("$@")
    local my_total=${#my_chunks[@]}
    
    for j in "${!my_chunks[@]}"; do
        local chunk_file="${my_chunks[$j]}"
        local chunk_basename=$(basename "$chunk_file")
        local progress=$((j + 1))
        
        log "[Worker $worker_id] ($progress/$my_total) 시작 -> $chunk_basename"
        
        local success=0
        local retry=0
        until [ $success -eq 1 ] || [ $retry -ge $MAX_RETRIES ]; do
            if hf sync --apply "$chunk_file" > /dev/null 2>&1; then
                success=1
            else
                retry=$((retry + 1))
                log "[Worker $worker_id] ! 경고: $chunk_basename $retry회 실패. 재시도 중..."
                sleep 5
            fi
        done
        
        if [ $success -eq 1 ]; then
            rm -f "$chunk_file"
            log "[Worker $worker_id] ($progress/$my_total) 완료 <- $chunk_basename"
            sleep "$SLEEP_TIME"
        else
            log "[Worker $worker_id] !!! 오류: $chunk_basename 최종 실패."
            return 1
        fi
    done
    log "[Worker $worker_id] 할당된 모든 작업 완료."
}

# Step 3: 워커 할당 및 동시 실행
for ((w=0; w<NUM_WORKERS; w++)); do
    # 각 워커에 할당될 청크 인덱스 계산 (라운드 로빈 방식 샤딩)
    worker_chunks=()
    for ((i=w; i<TOTAL_CHUNKS; i+=NUM_WORKERS)); do
        worker_chunks+=("${CHUNKS[$i]}")
    done
    
    if [ ${#worker_chunks[@]} -gt 0 ]; then
        log "워커 $w 배정 완료 (${#worker_chunks[@]}개 청크)"
        run_worker "$w" "${worker_chunks[@]}" &
    fi
done

# 모든 워커 종료 대기
wait
log "=== 모든 워커의 증분 동기화 작업이 완료되었습니다 ==="

# 정리
rm -f "$FULL_PLAN" "$FILTERED_PLAN"
