#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

HF_BUCKET_ID="${HF_BUCKET_ID:-yakdoli/peti-artifacts}"
PETY_PREFIX="${PETY_PREFIX:-pety}"
SEARCH_PREFIX="${SEARCH_PREFIX:-searchThema}"
PETY_DATE="${PETY_DATE:-2026-04-24}"
SEARCH_YEAR="${SEARCH_YEAR:-2024}"
SEARCH_INST="${SEARCH_INST:-정부공직자윤리위원회}"
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"

reset_local_artifacts() {
  echo "[1/6] 로컬 아티팩트 경로 초기화"
  rm -rf artifacts/metadata artifacts/pdfs artifacts/state artifacts/searchThema
  mkdir -p artifacts/metadata/items artifacts/pdfs artifacts/state
  mkdir -p artifacts/searchThema/metadata/items artifacts/searchThema/pdfs artifacts/searchThema/state
}

reset_hf_bucket() {
  echo "[2/6] HF bucket 초기화 (bucket: ${HF_BUCKET_ID})"
  python - <<'PY'
import os
import tempfile
from huggingface_hub import HfApi

bucket_id = os.environ["HF_BUCKET_ID"]
token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
if not token:
    raise SystemExit("HF token not found")
api = HfApi(token=token)
with tempfile.TemporaryDirectory() as empty_dir:
    dest = f"hf://buckets/{bucket_id}"
    plan = api.sync_bucket(source=empty_dir, dest=dest, token=token, delete=True, dry_run=True)
    print(f"reset plan -> uploads={len([o for o in plan.operations if o.action=='upload'])}, deletes={len([o for o in plan.operations if o.action=='delete'])}")
    api.sync_bucket(source=empty_dir, dest=dest, token=token, delete=True)
print("bucket reset completed")
PY
}

run_pety_live_test() {
  echo "[3/6] petyList 소규모 라이브 테스트"
  python crawl.py --start-date "$PETY_DATE" --end-date "$PETY_DATE" --limit 1 --metadata-only --no-resume
}

run_search_live_test() {
  echo "[4/6] searchThema 소규모 라이브 테스트"
  python crawl_search_thema.py --year "$SEARCH_YEAR" --institution "$SEARCH_INST" --limit 1 --metadata-only --no-resume
}

sync_prefix() {
  local source_dir="$1"
  local prefix="$2"
  echo "[sync] ${source_dir} -> hf://buckets/${HF_BUCKET_ID}/${prefix}"
  python - <<'PY'
import os
import sys
from huggingface_hub import HfApi

source_dir = os.environ["SYNC_SOURCE_DIR"]
prefix = os.environ["SYNC_PREFIX"]
bucket_id = os.environ["HF_BUCKET_ID"]
token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
if not token:
    raise SystemExit("HF token not found")
api = HfApi(token=token)
dest = f"hf://buckets/{bucket_id}/{prefix}".rstrip("/")
plan = api.sync_bucket(source=source_dir, dest=dest, token=token, delete=True, dry_run=True)
print(f"sync plan {source_dir} -> {dest}: uploads={len([o for o in plan.operations if o.action=='upload'])}, deletes={len([o for o in plan.operations if o.action=='delete'])}")
api.sync_bucket(source=source_dir, dest=dest, token=token, delete=True)
print("sync completed")
PY
}


trim_search_artifacts() {
  echo "[4.5/6] searchThema 결과 소규모화 (1건 유지)"
  python - <<'PY'
import json
from pathlib import Path
base = Path('artifacts/searchThema/metadata')
meta = base / 'metadata.json'
if not meta.exists():
    raise SystemExit('metadata.json not found')
items = json.loads(meta.read_text(encoding='utf-8'))
if not items:
    raise SystemExit('no searchThema items')
if isinstance(items, dict):
    first_id = next(iter(items))
    keep_item = items[first_id]
    keep = {first_id: keep_item}
    keep_id = first_id
else:
    keep_item = items[0]
    keep = [keep_item]
    keep_id = keep_item.get('id')
meta.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding='utf-8')
# metadata_*.json 최소화
for fp in base.glob('metadata_*.json'):
    arr = json.loads(fp.read_text(encoding='utf-8'))
    if isinstance(arr, dict):
        arr = {k:v for k,v in arr.items() if k == keep_id}
    else:
        arr = [x for x in arr if x.get('id') == keep_id]
    fp.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding='utf-8')
# csv 재생성(헤더+1행 유지)
csv = base / 'metadata.csv'
if csv.exists():
    lines = csv.read_text(encoding='utf-8').splitlines()
    if len(lines) > 2:
        csv.write_text('\n'.join(lines[:2]) + '\n', encoding='utf-8')
item_id = keep_id
for fp in (base / 'items').rglob('*.json'):
    if fp.stem != item_id:
        fp.unlink()
print('kept item id:', item_id)
PY
}

upload_results() {
  echo "[5/6] pety 업로드"
  SYNC_SOURCE_DIR="artifacts/metadata" SYNC_PREFIX="$PETY_PREFIX/metadata" HF_BUCKET_ID="$HF_BUCKET_ID" sync_prefix "artifacts/metadata" "$PETY_PREFIX/metadata"

  echo "[6/6] searchThema 업로드"
  SYNC_SOURCE_DIR="artifacts/searchThema/metadata" SYNC_PREFIX="$SEARCH_PREFIX/metadata" HF_BUCKET_ID="$HF_BUCKET_ID" sync_prefix "artifacts/searchThema/metadata" "$SEARCH_PREFIX/metadata"
}

HF_BUCKET_ID="$HF_BUCKET_ID" reset_local_artifacts
HF_BUCKET_ID="$HF_BUCKET_ID" reset_hf_bucket
run_pety_live_test
run_search_live_test
trim_search_artifacts
upload_results
