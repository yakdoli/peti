#!/bin/bash
# Chunked sync script for Hugging Face Buckets
# Splits a large sync operation into batches of 1000 to avoid rate limits.
# Each chunk is transformed into a valid plan file by prepending the original plan header.

set -e

SOURCE="/root/peti/artifacts/"
DEST="hf://buckets/yakdoli/peti-artifacts"
CHUNK_SIZE=1000
SLEEP_TIME=5
MAX_RETRIES=3
WORK_DIR="/root/peti/sync_work"
FULL_PLAN="$WORK_DIR/full_sync_plan.jsonl"

# Create work directory
mkdir -p "$WORK_DIR"
# Clean up previous chunks to avoid confusion
rm -f "$WORK_DIR"/chunk_*.jsonl
rm -f "$WORK_DIR"/apply_chunk_*.jsonl

echo "--------------------------------------------------"
echo "Step 1: Generating/Verifying full sync plan..."
echo "--------------------------------------------------"

# Generate the full plan if it doesn't exist
if [ ! -f "$FULL_PLAN" ]; then
    echo "Generating full plan file: $FULL_PLAN..."
    hf sync --plan "$FULL_PLAN" "$SOURCE" "$DEST"
else
    echo "Using existing full plan file: $FULL_PLAN"
fi

if [ ! -f "$FULL_PLAN" ]; then
    echo "Error: Failed to generate plan file."
    exit 1
fi

echo "--------------------------------------------------"
echo "Step 2: Splitting plan into valid chunk-plan files..."
echo "--------------------------------------------------"

# Extract the mandatory header (first line)
HEADER=$(head -n 1 "$FULL_PLAN")

# Split operations (starting from line 2) into temporary files
tail -n +2 "$FULL_PLAN" | split -l "$CHUNK_SIZE" -d --additional-suffix=.tmp - "$WORK_DIR/chunk_"

# For each temporary split, create a valid .jsonl plan file with the header
for TMP_FILE in "$WORK_DIR"/chunk_*.tmp; do
    CHUNK_PLAN="${TMP_FILE%.tmp}.jsonl"
    echo "$HEADER" > "$CHUNK_PLAN"
    cat "$TMP_FILE" >> "$CHUNK_PLAN"
    rm "$TMP_FILE"
done

CHUNKS=($(ls "$WORK_DIR"/chunk_*.jsonl | sort))
TOTAL_CHUNKS=${#CHUNKS[@]}

if [ "$TOTAL_CHUNKS" -eq 0 ]; then
    echo "No operations to perform. Sync is already up to date."
    exit 0
fi

echo "Created $TOTAL_CHUNKS individual plan chunks."

echo "--------------------------------------------------"
echo "Step 3: Applying chunk-plan files..."
echo "--------------------------------------------------"

for i in "${!CHUNKS[@]}"; do
    CHUNK_PLAN="${CHUNKS[$i]}"

    echo "[$((i+1))/$TOTAL_CHUNKS] Applying plan: $(basename "$CHUNK_PLAN")..."

    # Execute the chunk with retries
    RETRY_COUNT=0
    SUCCESS=0
    until [ $SUCCESS -eq 1 ]; do
        if hf sync --apply "$CHUNK_PLAN"; then
            SUCCESS=1
        else
            RETRY_COUNT=$((RETRY_COUNT + 1))
            if [ "$RETRY_COUNT" -ge "$MAX_RETRIES" ]; then
                echo "Error: Failed to apply $CHUNK_PLAN after $MAX_RETRIES attempts."
                exit 1
            fi
            echo "Warning: Sync failed (possibly rate limit), retrying in 10 seconds ($RETRY_COUNT/$MAX_RETRIES)..."
            sleep 10
        fi
    done

    # Clean up the applied chunk-plan
    rm "$CHUNK_PLAN"

    echo "Chunk $((i+1)) completed. Resting for $SLEEP_TIME seconds to avoid API limits..."
    sleep "$SLEEP_TIME"
done

echo "--------------------------------------------------"
echo "All $TOTAL_CHUNKS chunks applied successfully!"
echo "Sync completed."
echo "--------------------------------------------------"
