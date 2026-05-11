#!/bin/bash
# Optimized Chunked Sync Script v3.1
# Fixes split syntax and adds robust logging.

set -e

SOURCE="/root/peti/artifacts/"
DEST="hf://buckets/yakdoli/peti-artifacts"
CHUNK_SIZE=2500
SLEEP_TIME=2
MAX_RETRIES=3
WORK_DIR="/root/peti/sync_work"
FULL_PLAN="$WORK_DIR/v3_full_plan.jsonl"
FILTERED_PLAN="$WORK_DIR/v3_filtered_plan.jsonl"
LOG_FILE="$WORK_DIR/sync_v3.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

mkdir -p "$WORK_DIR"
log "Starting sync process v3.1 (Optimized)..."

# Step 1: Filter plan (remove 'skip' entries)
log "Filtering plan to remove already uploaded items..."
HEADER=$(head -n 1 "$FULL_PLAN")
echo "$HEADER" > "$FILTERED_PLAN"
grep '"action": "upload"' "$FULL_PLAN" >> "$FILTERED_PLAN" || true

TOTAL_UPLOADS=$(tail -n +2 "$FILTERED_PLAN" | wc -l)
log "Total items to upload: $TOTAL_UPLOADS"

if [ "$TOTAL_UPLOADS" -eq 0 ]; then
    log "Everything is already synced. Exiting."
    exit 0
fi

# Step 2: Split into valid chunk-plan files
log "Splitting filtered plan into chunks of $CHUNK_SIZE..."
rm -f "$WORK_DIR"/v3_chunk_*.jsonl "$WORK_DIR"/v3_chunk_*.tmp
# Note the '-' for stdin in split
tail -n +2 "$FILTERED_PLAN" | split -l "$CHUNK_SIZE" -d --additional-suffix=.tmp - "$WORK_DIR/v3_chunk_"

for TMP_FILE in "$WORK_DIR"/v3_chunk_*.tmp; do
    CHUNK_PLAN="${TMP_FILE%.tmp}.jsonl"
    echo "$HEADER" > "$CHUNK_PLAN"
    cat "$TMP_FILE" >> "$CHUNK_PLAN"
    rm "$TMP_FILE"
done

# Read chunks into array, ensuring they exist
CHUNKS=($(ls "$WORK_DIR"/v3_chunk_*.jsonl 2>/dev/null | sort))
TOTAL_CHUNKS=${#CHUNKS[@]}
log "Total chunks to apply: $TOTAL_CHUNKS"

# Step 3: Apply
for i in "${!CHUNKS[@]}"; do
    CHUNK_PLAN="${CHUNKS[$i]}"
    CURRENT_CHUNK=$((i+1))
    log "[$CURRENT_CHUNK/$TOTAL_CHUNKS] Applying $(basename "$CHUNK_PLAN")..."

    SUCCESS=0
    RETRY_COUNT=0
    while [ $SUCCESS -eq 0 ] && [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        if hf sync --apply "$CHUNK_PLAN"; then
            SUCCESS=1
        else
            RETRY_COUNT=$((RETRY_COUNT + 1))
            log "Warning: Attempt $RETRY_COUNT failed for $(basename "$CHUNK_PLAN"). Retrying in 10s..."
            sleep 10
        fi
    done

    if [ $SUCCESS -eq 1 ]; then
        rm "$CHUNK_PLAN"
        log "[$CURRENT_CHUNK/$TOTAL_CHUNKS] Success. Sleeping ${SLEEP_TIME}s..."
        sleep "$SLEEP_TIME"
    else
        log "ERROR: Failed to apply $(basename "$CHUNK_PLAN") after $MAX_RETRIES attempts. Exiting."
        exit 1
    fi
done

log "Sync process completed successfully."
