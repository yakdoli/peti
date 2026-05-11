#!/bin/bash
# Optimized Chunked Sync Script v2
# Each chunk is a valid standalone plan file.

set -e

SOURCE="/root/peti/artifacts/"
DEST="hf://buckets/yakdoli/peti-artifacts"
CHUNK_SIZE=1000
SLEEP_TIME=5
MAX_RETRIES=3
WORK_DIR="/root/peti/sync_work"
FULL_PLAN="$WORK_DIR/full_sync_plan.jsonl"
LOG_FILE="$WORK_DIR/sync_v2.log"

# Function to log with timestamp
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

mkdir -p "$WORK_DIR"
log "Starting sync process..."

# Step 1: Plan
if [ ! -f "$FULL_PLAN" ]; then
    log "Generating full plan file (this may take minutes)..."
    hf sync --plan "$FULL_PLAN" "$SOURCE" "$DEST"
else
    log "Using existing full plan: $FULL_PLAN"
fi

# Step 2: Split and Prepare Plans
log "Preparing individual chunk-plan files..."
HEADER=$(head -n 1 "$FULL_PLAN")
# Clear old files
rm -f "$WORK_DIR"/chunk_*.jsonl "$WORK_DIR"/chunk_*.tmp

# Filter only non-skip operations to reduce number of chunks (optional, but safer to keep all)
# For now, keep it simple and split the whole file
tail -n +2 "$FULL_PLAN" | split -l "$CHUNK_SIZE" -d --additional-suffix=.tmp - "$WORK_DIR/chunk_"

for TMP_FILE in "$WORK_DIR"/chunk_*.tmp; do
    CHUNK_PLAN="${TMP_FILE%.tmp}.jsonl"
    echo "$HEADER" > "$CHUNK_PLAN"
    cat "$TMP_FILE" >> "$CHUNK_PLAN"
    rm "$TMP_FILE"
done

CHUNKS=($(ls "$WORK_DIR"/chunk_*.jsonl | sort))
TOTAL_CHUNKS=${#CHUNKS[@]}
log "Total chunk-plans to apply: $TOTAL_CHUNKS"

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
