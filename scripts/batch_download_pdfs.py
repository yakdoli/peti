#!/usr/bin/env python3
"""Standalone batch PDF downloader for existing metadata items.

Reads all metadata item JSONs from data/searchThema/metadata/items/ and
downloads PDFs concurrently for items missing them. Much faster than
re-crawling from API since it skips the search/fetch phase entirely.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import math
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Tuple
from urllib.parse import urljoin

import aiohttp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from src.config import get_config


DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class DownloadResult(NamedTuple):
    item_id: str
    status: str
    pdf_path: str
    size_bytes: int
    sha256: str
    error: str


class WorkItem(NamedTuple):
    item: dict
    source_path: Path


def safe_filename(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", value).strip("._") or "unknown"


def expected_pdf_path(item: dict, pdf_dir: Path) -> Path:
    date_text = str(item.get("date") or "unknown")
    year = date_text[:4] if re.match(r"^\d{4}", date_text) else "unknown"
    date_key = date_text.replace("-", "") if re.match(r"^\d{4}-\d{2}-\d{2}$", date_text) else "unknown"
    safe_id = safe_filename(str(item.get("id") or ""))
    return pdf_dir / year / date_key / f"{safe_id}.pdf"


def canonical_item_path(metadata_dir: Path, item: dict) -> Path:
    date_text = str(item.get("date") or "unknown-date")
    year = date_text[:4] if re.match(r"^\d{4}", date_text) else "unknown"
    date_key = date_text.replace("-", "") if re.match(r"^\d{4}-\d{2}-\d{2}$", date_text) else "unknown"
    safe_id = safe_filename(str(item.get("id") or ""))
    return metadata_dir / year / date_key / f"{safe_id}.json"


def write_item(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(item, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def pdf_path_is_valid(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def item_has_completed_pdf(item: dict) -> bool:
    pdf = item.get("pdf") or {}
    if pdf.get("status") != "completed":
        return False
    pdf_path = Path(str(pdf.get("path", "")))
    return pdf_path_is_valid(pdf_path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def item_with_existing_pdf(item: dict, pdf_path: Path, include_hash: bool) -> dict:
    repaired = dict(item)
    repaired["pdf"] = {
        "status": "completed",
        "path": str(pdf_path),
        "size_bytes": pdf_path.stat().st_size,
        "sha256": file_sha256(pdf_path) if include_hash else str((item.get("pdf") or {}).get("sha256") or ""),
        "downloaded_at": str((item.get("pdf") or {}).get("downloaded_at") or datetime.now().isoformat()),
    }
    repaired["status"] = "completed"
    repaired["updated_at"] = datetime.now().isoformat()
    return repaired


def choose_record(records: List[WorkItem], metadata_dir: Path) -> WorkItem:
    completed_records = [record for record in records if item_has_completed_pdf(record.item)]
    if completed_records:
        return completed_records[0]

    for record in records:
        if record.source_path == canonical_item_path(metadata_dir, record.item):
            return record
    return records[0]


def find_items_without_pdfs(
    metadata_dir: Path,
    pdf_dir: Path,
    repair_metadata: bool,
    cleanup_flat_items: bool,
) -> Tuple[List[WorkItem], int, int, int, int]:
    grouped: Dict[str, List[WorkItem]] = {}
    for item_file in sorted(metadata_dir.rglob("*.json")):
        if item_file.name.startswith("metadata"):
            continue
        try:
            item = json.loads(item_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        grouped.setdefault(item_id, []).append(WorkItem(item, item_file))

    items: List[WorkItem] = []
    skipped_existing = 0
    repaired = 0
    removed_flat = 0

    for records in grouped.values():
        record = choose_record(records, metadata_dir)
        item = record.item
        canonical_path = canonical_item_path(metadata_dir, item)
        completed_record = next((candidate for candidate in records if item_has_completed_pdf(candidate.item)), None)

        if completed_record:
            skipped_existing += 1
            if repair_metadata and completed_record.source_path != canonical_path:
                write_item(canonical_path, completed_record.item)
                repaired += 1
            if cleanup_flat_items:
                for candidate in records:
                    if candidate.source_path.parent == metadata_dir and candidate.source_path != canonical_path:
                        candidate.source_path.unlink(missing_ok=True)
                        removed_flat += 1
            continue

        existing_pdf = expected_pdf_path(item, pdf_dir)
        if pdf_path_is_valid(existing_pdf):
            skipped_existing += 1
            if repair_metadata:
                repaired_item = item_with_existing_pdf(item, existing_pdf, include_hash=True)
                write_item(canonical_path, repaired_item)
                repaired += 1
            if cleanup_flat_items:
                for candidate in records:
                    if candidate.source_path.parent == metadata_dir and candidate.source_path != canonical_path:
                        candidate.source_path.unlink(missing_ok=True)
                        removed_flat += 1
            continue

        if not item.get("viewer_path"):
            continue
        items.append(WorkItem(dict(item), canonical_path))

    return items, len(grouped), skipped_existing, repaired, removed_flat


def parse_query_params(path: str) -> Dict[str, str]:
    from urllib.parse import parse_qs, urlparse
    result: Dict[str, str] = {}
    for k, v in parse_qs(urlparse(str(path)).query).items():
        if v:
            result[k] = v[0]
    return result


async def download_one(
    session: aiohttp.ClientSession,
    item: dict,
    pdf_dir: Path,
    viewer_base: str,
    timeout: int,
    sem: asyncio.Semaphore,
) -> DownloadResult:
    item_id = str(item.get("id") or "")
    async with sem:
        try:
            viewer_path = str(item.get("viewer_path") or "")
            viewer_url = urljoin(viewer_base, viewer_path.lstrip("/"))
            async with session.get(viewer_url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Viewer HTTP {resp.status}")
                viewer_html = await resp.text()

            content_match = re.search(
                r"(/user/common/ofcttCntntDownload\.do(?:;jsessionid=[A-Za-z0-9_.-]+)?)",
                viewer_html,
            )
            content_id = item.get("toc_id") or item.get("stored_toc_seq") or ""
            if content_match and content_id:
                dl_url = urljoin(viewer_base, content_match.group(1))
                dl_data = {"cntnt_seq_no": content_id}
            else:
                issue_match = re.search(
                    r"(/user/common/ofcttDownload\.do(?:;jsessionid=[A-Za-z0-9_.-]+)?)",
                    viewer_html,
                )
                cid = item.get("content_id") or ""
                if not (issue_match and cid):
                    raise RuntimeError("PDF download endpoint not found")
                dl_url = urljoin(viewer_base, issue_match.group(1))
                dl_data = {"downType": "1", "ofctt_seq_no": cid}

            date_text = str(item.get("date") or "unknown")
            year = date_text[:4] if re.match(r"^\d{4}", date_text) else "unknown"
            date_key = date_text.replace("-", "") if re.match(r"^\d{4}-\d{2}-\d{2}$", date_text) else "unknown"
            safe_id = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", str(item_id)).strip("._") or "unknown"
            pdf_path = pdf_dir / year / date_key / f"{safe_id}.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)

            headers = {
                "User-Agent": DEFAULT_UA,
                "Referer": viewer_base,
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=max(timeout, 60)), headers=headers) as dl_sess:
                async with dl_sess.post(dl_url, data=dl_data) as dl_resp:
                    if dl_resp.status != 200:
                        raise RuntimeError(f"PDF download HTTP {dl_resp.status}")
                    tmp_path = pdf_path.with_suffix(".pdf.tmp")
                    sha256 = hashlib.sha256()
                    size = 0
                    with open(tmp_path, "wb") as f:
                        async for chunk in dl_resp.content.iter_chunked(8192):
                            if not chunk:
                                continue
                            f.write(chunk)
                            sha256.update(chunk)
                            size += len(chunk)

            if not tmp_path.exists() or tmp_path.stat().st_size == 0:
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError("Empty PDF")

            with open(tmp_path, "rb") as f:
                header = f.read(5)
            if header != b"%PDF-":
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError("Not a valid PDF")

            tmp_path.replace(pdf_path)
            item["pdf"] = {
                "status": "completed",
                "path": str(pdf_path),
                "size_bytes": size,
                "sha256": sha256.hexdigest(),
                "downloaded_at": datetime.now().isoformat(),
            }
            item["status"] = "completed"
            item["updated_at"] = datetime.now().isoformat()

            return DownloadResult(item_id, "completed", str(pdf_path), size, sha256.hexdigest(), "")

        except Exception as e:
            return DownloadResult(item_id, "failed", "", 0, "", str(e))


async def batch_download(
    items: List[WorkItem],
    pdf_dir: Path,
    viewer_base: str,
    concurrency: int,
    timeout: int,
    failure_log: Path,
):
    sem = asyncio.Semaphore(concurrency)
    total = len(items)
    downloaded = failed = 0
    failure_log.parent.mkdir(parents=True, exist_ok=True)

    for batch_start in range(0, total, concurrency * 4):
        batch = items[batch_start:batch_start + concurrency * 4]
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": DEFAULT_UA},
        ) as session:
            tasks = [download_one(session, work_item.item, pdf_dir, viewer_base, timeout, sem) for work_item in batch]
            results = await asyncio.gather(*tasks, return_exceptions=False)

        failure_rows = []
        for i, result in enumerate(results):
            work_item = batch[i]
            item = work_item.item
            if isinstance(result, BaseException):
                result = DownloadResult(item.get("id", ""), "failed", "", 0, "", str(result))
            if result.status == "completed":
                downloaded += 1
            else:
                failed += 1
                item["pdf"] = {
                    "status": "failed",
                    "error": result.error,
                    "failed_at": datetime.now().isoformat(),
                }
                failure_rows.append({
                    "item_id": result.item_id,
                    "error": result.error,
                    "viewer_path": item.get("viewer_path"),
                    "content_id": item.get("content_id"),
                    "toc_id": item.get("toc_id"),
                    "date": item.get("date"),
                    "title": item.get("title"),
                    "failed_at": item["pdf"]["failed_at"],
                })

            write_item(work_item.source_path, item)

        if failure_rows:
            with open(failure_log, "a", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

        pct = min(100, round((batch_start + len(batch)) / total * 100, 1))
        print(f"[{pct}%] {batch_start + len(batch)}/{total} | OK: {downloaded} | FAIL: {failed}", flush=True)

    return downloaded, failed


async def main():
    parser = argparse.ArgumentParser(description="Batch PDF downloader for existing metadata")
    parser.add_argument("--concurrency", type=int, default=10, help="동시 다운로드 수")
    parser.add_argument("--limit", type=int, help="최대 다운로드 항목 수")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP 타임아웃(초)")
    parser.add_argument("--failure-log", type=Path, help="실패 항목 JSONL 로그 경로")
    parser.add_argument("--repair-metadata", action="store_true", help="기존 PDF가 있으면 정식 nested item JSON에 pdf 메타데이터를 보강")
    parser.add_argument("--cleanup-flat-items", action="store_true", help="정식 nested item JSON으로 보강된 root-level 중복 item JSON 제거")
    parser.add_argument("--discover-only", action="store_true", help="대상 집계만 수행하고 다운로드하지 않음")
    args = parser.parse_args()

    config = get_config()
    st = config.get_search_thema_config()
    viewer_base = st.get("viewer_base_url", "https://gwanbo.go.kr/")
    download_cfg = config.get_download_config()
    pdf_dir = Path(download_cfg.get("pdf_directory", "data/pdfs"))
    metadata_dir = Path(download_cfg.get("metadata_directory", "data/metadata"))

    if "searchThema" not in str(pdf_dir):
        if pdf_dir.name == "pdfs":
            pdf_dir = pdf_dir.parent / "searchThema" / "pdfs"
        else:
            pdf_dir = pdf_dir / "searchThema" / "pdfs"

    if "searchThema" not in str(metadata_dir):
        if metadata_dir.name == "metadata":
            item_save_dir = metadata_dir.parent / "searchThema" / "metadata" / "items"
        else:
            item_save_dir = metadata_dir / "searchThema" / "metadata" / "items"
    else:
        item_save_dir = metadata_dir / "items"

    failure_log = args.failure_log or pdf_dir.parent / "pdf_failures.jsonl"
    items, scanned, skipped_existing, repaired, removed_flat = find_items_without_pdfs(
        item_save_dir,
        pdf_dir,
        repair_metadata=args.repair_metadata,
        cleanup_flat_items=args.cleanup_flat_items,
    )
    print(f"Scanned {scanned} unique items in {item_save_dir}")
    print(f"Skipped {skipped_existing} items with existing valid PDFs")
    if repaired:
        print(f"Repaired metadata for {repaired} items")
    if removed_flat:
        print(f"Removed {removed_flat} root-level duplicate item JSONs")
    print(f"Found {len(items)} items without PDFs in {item_save_dir}")

    if args.limit is not None:
        items = items[:args.limit]
        print(f"Limited to {args.limit} items")

    if args.discover_only:
        print("Discovery only. No downloads started.")
        return

    if not items:
        print("No items to download. Done.")
        return

    ok, fail = await batch_download(
        items, pdf_dir, viewer_base, args.concurrency, args.timeout, failure_log
    )
    print(f"\nComplete: {ok} OK, {fail} FAIL")


if __name__ == "__main__":
    asyncio.run(main())
