#!/usr/bin/env python3
"""Download missing SearchThema PDFs from a JSONL repair manifest."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from entrypoint_utils import add_project_paths, configure_windows_asyncio_policy


add_project_paths()

from src.crawler_search_thema import SearchThemaCrawler


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def year_allowed(row: dict[str, Any], include_years: set[str], exclude_years: set[str]) -> bool:
    year = str(row.get("date") or "")[:4]
    if include_years and year not in include_years:
        return False
    return not (exclude_years and year in exclude_years)


def csv_set(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def load_manifest_rows(
    manifest_path: Path,
    shard_index: int,
    shard_count: int,
    limit: int | None,
    include_years: set[str],
    exclude_years: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    filtered_index = 0
    with manifest_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                rows.append({"manifest_error": str(exc), "line_number": line_number})
                continue
            if not isinstance(row, dict):
                rows.append({"manifest_error": "row is not object", "line_number": line_number})
                continue
            if not year_allowed(row, include_years, exclude_years):
                continue
            if filtered_index % shard_count != shard_index:
                filtered_index += 1
                continue
            row["_line_number"] = line_number
            rows.append(row)
            filtered_index += 1
            if limit is not None and len(rows) >= limit:
                break
    return rows


def resolve_repo_path(path_text: str, repo_root: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def current_pdf_path(item: dict[str, Any], repo_root: Path) -> Path | None:
    pdf = item.get("pdf")
    if not isinstance(pdf, dict):
        return None
    path_text = str(pdf.get("path") or "").strip()
    if not path_text:
        return None
    return resolve_repo_path(path_text, repo_root)


async def process_item(
    crawler: SearchThemaCrawler,
    repo_root: Path,
    row: dict[str, Any],
    failure_log: Path,
) -> str:
    if "manifest_error" in row:
        append_jsonl(failure_log, {"failed_at": iso_now(), **row})
        return "manifest_error"

    item_path = resolve_repo_path(str(row.get("item_path") or ""), repo_root)
    try:
        item = json.loads(item_path.read_text(encoding="utf-8"))
    except Exception as exc:
        append_jsonl(
            failure_log,
            {
                "failed_at": iso_now(),
                "item_path": str(item_path),
                "id": row.get("id"),
                "error": f"metadata read failed: {exc}",
            },
        )
        return "metadata_error"

    if not isinstance(item, dict):
        append_jsonl(
            failure_log,
            {
                "failed_at": iso_now(),
                "item_path": str(item_path),
                "id": row.get("id"),
                "error": "metadata is not object",
            },
        )
        return "metadata_error"

    existing_path = current_pdf_path(item, repo_root)
    if existing_path and crawler._pdf_file_is_complete(existing_path):
        pdf = item.setdefault("pdf", {})
        changed = False
        for stale_key in ("error", "failed_at"):
            if stale_key in pdf:
                pdf.pop(stale_key, None)
                changed = True
        if item.get("status") != "completed":
            item["status"] = "completed"
            changed = True
        if changed:
            item["updated_at"] = iso_now()
            write_json(item_path, item)
        return "skipped_existing"

    item.setdefault("id", str(row.get("id") or item_path.stem))
    if not isinstance(item.get("pdf"), dict):
        item["pdf"] = {}

    try:
        result = await crawler._download_item_pdf(None, item)
    except Exception as exc:
        item.setdefault("pdf", {})
        item["pdf"]["status"] = "failed"
        item["pdf"]["error"] = str(exc)
        item["pdf"]["failed_at"] = iso_now()
        item["status"] = "download_failed"
        item["updated_at"] = iso_now()
        write_json(item_path, item)
        append_jsonl(
            failure_log,
            {
                "failed_at": iso_now(),
                "item_path": str(item_path),
                "id": item.get("id"),
                "error": str(exc),
            },
        )
        return "failed"

    result_pdf = result.get("pdf") if isinstance(result.get("pdf"), dict) else {}
    result_path_text = str((result_pdf or {}).get("path") or "").strip()
    result_path = resolve_repo_path(result_path_text, repo_root) if result_path_text else None
    write_json(item_path, result)

    if (result_pdf or {}).get("status") != "completed" or not result_path or not crawler._pdf_file_is_complete(result_path):
        append_jsonl(
            failure_log,
            {
                "failed_at": iso_now(),
                "item_path": str(item_path),
                "id": item.get("id"),
                "error": (result_pdf or {}).get("error") or "download did not produce a complete PDF",
            },
        )
        return "failed"

    return "downloaded"


async def run(args: argparse.Namespace) -> dict[str, int]:
    repo_root = Path.cwd().resolve()
    manifest_path = resolve_repo_path(args.manifest, repo_root)
    failure_log = resolve_repo_path(args.failure_log, repo_root)
    rows = load_manifest_rows(
        manifest_path,
        args.shard_index,
        args.shard_count,
        args.limit,
        csv_set(args.include_years),
        csv_set(args.exclude_years),
    )

    crawler = SearchThemaCrawler(
        metadata_only=False,
        resume=False,
        save_indexes=False,
        use_browser=args.browser_download_fallback,
        preload_metadata=not args.no_preload_metadata,
        concurrency=args.concurrency,
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    counts = {"total": len(rows), "downloaded": 0, "skipped_existing": 0, "failed": 0, "metadata_error": 0, "manifest_error": 0}
    completed = 0

    async def worker(row: dict[str, Any]) -> None:
        nonlocal completed
        async with semaphore:
            status = await process_item(crawler, repo_root, row, failure_log)
            counts[status] = counts.get(status, 0) + 1
            completed += 1
            if completed % args.progress_interval == 0 or completed == len(rows):
                print(
                    "progress "
                    f"shard={args.shard_index}/{args.shard_count} "
                    f"completed={completed}/{len(rows)} "
                    f"downloaded={counts.get('downloaded', 0)} "
                    f"skipped={counts.get('skipped_existing', 0)} "
                    f"failed={counts.get('failed', 0) + counts.get('metadata_error', 0) + counts.get('manifest_error', 0)}",
                    flush=True,
                )

    await asyncio.gather(*(worker(row) for row in rows))
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True), flush=True)
    return counts


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download SearchThema PDFs listed in a repair manifest.")
    parser.add_argument("--manifest", default="artifacts/searchThema/state/pdf_repair_manifest.jsonl")
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--failure-log", default="logs/catchup/searchthema_manifest_failures.jsonl")
    parser.add_argument("--progress-interval", type=int, default=25)
    parser.add_argument("--no-preload-metadata", action="store_true")
    parser.add_argument(
        "--browser-download-fallback",
        action="store_true",
        help="Allow headless browser-backed PDF download fallback after HTTP/session-pool retries fail.",
    )
    parser.add_argument("--include-years", default="", help="Comma-separated years to include.")
    parser.add_argument("--exclude-years", default="", help="Comma-separated years to exclude.")
    args = parser.parse_args(argv)

    if args.shard_count <= 0:
        raise SystemExit("--shard-count must be positive")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise SystemExit("--shard-index must be in [0, shard-count)")
    if args.concurrency <= 0:
        raise SystemExit("--concurrency must be positive")
    if args.progress_interval <= 0:
        raise SystemExit("--progress-interval must be positive")
    return args


def main() -> None:
    configure_windows_asyncio_policy()
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
