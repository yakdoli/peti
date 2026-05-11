#!/bin/bash
# Hugging Face 증분 동기화 스크립트 (Incremental Catch-up)
# 동작 방식: 실시간 플랜 생성 -> 업로드 대상 필터링 -> 2500건 청킹 -> 2초 대기 적용

set -e

# 설정
SOURCE="/root/peti/artifacts/"
DEST="hf://buckets/yakdoli/peti-artifacts"
CHUNK_SIZE=2500
SLEEP_TIME=2
MAX_RETRIES=3
WORK_DIR="/root/peti/sync_work/incremental"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FULL_PLAN="$WORK_DIR/plan_$TIMESTAMP.jsonl"
FILTERED_PLAN="$WORK_DIR/filtered_$TIMESTAMP.jsonl"
LOG_FILE="$WORK_DIR/sync_$TIMESTAMP.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

mkdir -p "$WORK_DIR"
log "증분 동기화 시작: $SOURCE -> $DEST"

# Step 1: 현재 상태 분석 및 플랜 생성
log "현재 상태 분석 중 (플랜 생성)..."
hf sync --plan "$FULL_PLAN" "$SOURCE" "$DEST"

# 업로드 대상 개수 확인
TOTAL_UPLOADS=$(grep -c '"action": "upload"' "$FULL_PLAN" || echo "0")
log "새로 업로드할 항목 수: $TOTAL_UPLOADS"

if [ "$TOTAL_UPLOADS" -eq 0 ]; then
    log "변경 사항이 없습니다. 동기화가 이미 완료된 상태입니다."
    rm "$FULL_PLAN"
    exit 0
fi

# Step 2: 업로드 항목만 필터링하여 새로운 플랜 구성
log "업로드 대상 필터링 및 청킹 준비 중..."
HEADER=$(head -n 1 "$FULL_PLAN")
echo "$HEADER" > "$FILTERED_PLAN"
grep '"action": "upload"' "$FULL_PLAN" >> "$FILTERED_PLAN"

# 청킹 (split 명령 사용)
tail -n +2 "$FILTERED_PLAN" | split -l "$CHUNK_SIZE" -d --additional-suffix=.tmp - "$WORK_DIR/chunk_${TIMESTAMP}_"

# 각 청크를 유효한 플랜 파일로 변환
for TMP_FILE in "$WORK_DIR"/chunk_${TIMESTAMP}_*.tmp; do
    CHUNK_PLAN="${TMP_FILE%.tmp}.jsonl"
    echo "$HEADER" > "$CHUNK_PLAN"
    cat "$TMP_FILE" >> "$CHUNK_PLAN"
    rm "$TMP_FILE"
done

CHUNKS=($(ls "$WORK_DIR"/chunk_${TIMESTAMP}_*.jsonl | sort))
TOTAL_CHUNKS=${#CHUNKS[@]}
log "총 $TOTAL_CHUNKS 개의 청크를 순차적으로 적용합니다."

# Step 3: 실행
for i in "${!CHUNKS[@]}"; do
    CHUNK_PLAN="${CHUNKS[$i]}"
    CURRENT=$((i+1))
    log "[$CURRENT/$TOTAL_CHUNKS] 적용 중: $(basename "$CHUNK_PLAN")"

    SUCCESS=0
    RETRY=0
    until [ $SUCCESS -eq 1 ] || [ $RETRY -ge $MAX_RETRIES ]; do
        if hf sync --apply "$CHUNK_PLAN"; then
            SUCCESS=1
        else
            RETRY=$((RETRY + 1))
            log "경고: $RETRY회 실패. 10초 후 재시도..."
            sleep 10
        fi
    done

    if [ $SUCCESS -eq 1 ]; then
        rm "$CHUNK_PLAN"
        log "[$CURRENT/$TOTAL_CHUNKS] 성공. ${SLEEP_TIME}초 대기..."
        sleep "$SLEEP_TIME"
    else
        log "오류: $MAX_RETRIES회 시도 후 실패. 중단합니다."
        exit 1
    fi
done

# 완료 후 플랜 파일 정리
rm "$FULL_PLAN" "$FILTERED_PLAN"
log "동기화 프로세스가 성공적으로 완료되었습니다."
