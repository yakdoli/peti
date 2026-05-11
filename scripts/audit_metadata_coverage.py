#!/usr/bin/env python3
"""Audit source-list coverage against saved item metadata JSON files."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from entrypoint_utils import add_project_paths, configure_windows_asyncio_policy

add_project_paths()

from src.base_crawler import DEFAULT_BROWSER_USER_AGENT
from src.crawler import GwanboCrawler
from src.crawler_search_thema import SearchThemaCrawler
from src.logger import setup_logger
from src.pety_parser import parse_pety_list_page

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - runtime dependency guard
    async_playwright = None


SOURCES = ("pety", "searchThema")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def item_metadata_dir(artifacts_root: Path, source: str) -> Path:
    return artifacts_root / source / "metadata" / "items"


def scan_existing_metadata_ids(
    artifacts_root: Path,
    source: str,
    sample_limit: int,
) -> tuple[set[str], Counter, list[dict[str, Any]]]:
    ids: set[str] = set()
    summary: Counter = Counter()
    samples: list[dict[str, Any]] = []
    items_dir = item_metadata_dir(artifacts_root, source)
    if not items_dir.exists():
        summary["metadata_dir_missing"] += 1
        return ids, summary, samples

    for path in sorted(items_dir.rglob("*.json")):
        summary["json_files"] += 1
        try:
            with path.open("r", encoding="utf-8") as handle:
                item = json.load(handle)
        except Exception as exc:
            summary["invalid_json"] += 1
            if len(samples) < sample_limit:
                samples.append({"path": str(path), "error": str(exc)})
            continue

        if not isinstance(item, dict):
            summary["invalid_json"] += 1
            if len(samples) < sample_limit:
                samples.append({"path": str(path), "error": "top-level JSON is not an object"})
            continue

        item_id = str(item.get("id") or item.get("stored_toc_seq") or "").strip()
        if not item_id:
            summary["missing_id"] += 1
            if len(samples) < sample_limit:
                samples.append({"path": str(path), "error": "metadata id is missing"})
            continue
        ids.add(item_id)

    summary["unique_ids"] = len(ids)
    return ids, summary, samples


def write_manifest_row(handle: Any, row: dict[str, Any]) -> None:
    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()


def row_from_item(
    source: str,
    item: dict[str, Any],
    reason: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    row = {
        "source": source,
        "id": str(item.get("id") or item.get("stored_toc_seq") or ""),
        "date": item.get("date") or item.get("keyword_field_regdate"),
        "title": item.get("title") or item.get("stored_field_subject"),
        "category": item.get("category") or item.get("stored_category_name"),
        "agency": item.get("agency") or item.get("stored_organ_nm"),
        "viewer_path": item.get("viewer_path") or item.get("stored_field_url"),
        "pdf_path": pdf.get("path") if isinstance(pdf, dict) else None,
        "reason": reason,
        "audited_at": iso_now(),
    }
    if extra:
        row.update(extra)
    return row


def mark_metadata_only(item: dict[str, Any]) -> dict[str, Any]:
    repaired = dict(item)
    repaired["status"] = "metadata_only"
    pdf = dict(repaired.get("pdf") or {})
    pdf.setdefault("path", "")
    pdf.setdefault("size_bytes", 0)
    pdf.setdefault("sha256", "")
    pdf.setdefault("downloaded_at", "")
    pdf["status"] = "skipped"
    repaired["pdf"] = pdf
    repaired["updated_at"] = datetime.now().isoformat()
    return repaired


async def audit_pety(
    args: argparse.Namespace,
    artifacts_root: Path,
    existing_ids: set[str],
    manifest_handle: Any,
    samples: dict[str, list[dict[str, Any]]],
) -> Counter:
    logger = setup_logger("audit_metadata_coverage.pety")
    summary: Counter = Counter()
    crawler = GwanboCrawler(
        metadata_only=True,
        resume=False,
        download_pdfs=False,
        save_indexes=False,
        state_file=str(artifacts_root / "pety" / "state" / "metadata_coverage_audit.json"),
    )

    if async_playwright is None:
        raise RuntimeError("Playwright is required for pety metadata coverage audit")

    seen_ids: set[str] = set()
    async with async_playwright() as playwright:
        browser = await crawler._launch_browser(playwright)
        context = await browser.new_context(
            accept_downloads=False,
            ignore_https_errors=crawler.ignore_https_errors,
            extra_http_headers={
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": os.getenv("GWANBO_USER_AGENT", DEFAULT_BROWSER_USER_AGENT),
            },
        )
        try:
            await crawler._prime_browser_session(context)
            for window_start, window_end in crawler._date_windows():
                summary["windows"] += 1
                page_number = 1
                total_pages = 1
                while page_number <= total_pages:
                    try:
                        html = await crawler._fetch_list_page(context, window_start, window_end, page_number)
                        page = parse_pety_list_page(html, crawler.list_url)
                    except Exception as exc:
                        summary["page_errors"] += 1
                        row = {
                            "source": "pety",
                            "reason": "page_fetch_error",
                            "start_date": window_start.strftime("%Y-%m-%d"),
                            "end_date": window_end.strftime("%Y-%m-%d"),
                            "page": page_number,
                            "error": str(exc),
                            "audited_at": iso_now(),
                        }
                        write_manifest_row(manifest_handle, row)
                        if len(samples["pety_errors"]) < args.sample_limit:
                            samples["pety_errors"].append(row)
                        break

                    total_pages = max(page.total_pages, 1)
                    summary["pages"] += 1
                    for item in page.items:
                        item_id = crawler.get_item_id(item)
                        if not item_id:
                            summary["items_without_id"] += 1
                            continue
                        summary["items_scanned"] += 1
                        if item_id in seen_ids:
                            summary["duplicate_items"] += 1
                            continue
                        seen_ids.add(item_id)
                        summary["unique_items_scanned"] += 1
                        if item_id in existing_ids:
                            continue

                        summary["missing_metadata"] += 1
                        item["ocr"]["ready_dir"] = str(
                            crawler.ocr_ready_dir / crawler._safe_filename(crawler.get_item_id(item))
                        )
                        row = row_from_item(
                            "pety",
                            item,
                            "missing_metadata",
                            {
                                "start_date": window_start.strftime("%Y-%m-%d"),
                                "end_date": window_end.strftime("%Y-%m-%d"),
                                "page": page_number,
                            },
                        )
                        write_manifest_row(manifest_handle, row)
                        if len(samples["pety_missing_metadata"]) < args.sample_limit:
                            samples["pety_missing_metadata"].append(row)
                        if args.repair_missing:
                            crawler.metadata_manager.save_item(mark_metadata_only(item))
                            existing_ids.add(item_id)
                            summary["repaired_metadata"] += 1

                    page_number += 1
                    if args.pety_page_delay > 0:
                        await asyncio.sleep(args.pety_page_delay)

                if args.progress_interval and summary["windows"] % args.progress_interval == 0:
                    logger.info(
                        "pety audit progress windows=%s unique=%s missing=%s repaired=%s",
                        summary["windows"],
                        summary["unique_items_scanned"],
                        summary["missing_metadata"],
                        summary["repaired_metadata"],
                    )
        finally:
            await context.close()
            await browser.close()

    return summary


async def audit_search_thema(
    args: argparse.Namespace,
    artifacts_root: Path,
    existing_ids: set[str],
    manifest_handle: Any,
    samples: dict[str, list[dict[str, Any]]],
) -> Counter:
    logger = setup_logger("audit_metadata_coverage.searchThema")
    summary: Counter = Counter()
    crawler = SearchThemaCrawler(
        metadata_only=True,
        resume=False,
        save_indexes=False,
        state_file=str(artifacts_root / "searchThema" / "state" / "metadata_coverage_audit.json"),
        use_browser=False,
        preload_metadata=False,
    )
    crawler.list_size = args.search_list_size
    crawler.page_delay = args.search_page_delay

    seen_ids: set[str] = set()
    for year in crawler.years:
        for institution in crawler.institutions:
            summary["combinations"] += 1
            page_number = 1
            while True:
                try:
                    raw_items = await crawler.fetch_items(
                        page_number,
                        year=year,
                        institution=institution,
                        context=None,
                    )
                except Exception as exc:
                    summary["page_errors"] += 1
                    row = {
                        "source": "searchThema",
                        "reason": "page_fetch_error",
                        "year": str(year),
                        "institution": crawler._institution_state_value(institution),
                        "page": page_number,
                        "error": str(exc),
                        "audited_at": iso_now(),
                    }
                    write_manifest_row(manifest_handle, row)
                    if len(samples["searchThema_errors"]) < args.sample_limit:
                        samples["searchThema_errors"].append(row)
                    break

                if not raw_items:
                    break

                summary["pages"] += 1
                for raw in raw_items:
                    item = crawler._metadata_item_from_raw(raw)
                    item_id = crawler.get_item_id(item)
                    if not item_id:
                        summary["items_without_id"] += 1
                        continue
                    summary["items_scanned"] += 1
                    if item_id in seen_ids:
                        summary["duplicate_items"] += 1
                        continue
                    seen_ids.add(item_id)
                    summary["unique_items_scanned"] += 1
                    if item_id in existing_ids:
                        continue

                    summary["missing_metadata"] += 1
                    row = row_from_item(
                        "searchThema",
                        item,
                        "missing_metadata",
                        {
                            "year": str(year),
                            "institution": crawler._institution_state_value(institution),
                            "page": page_number,
                        },
                    )
                    write_manifest_row(manifest_handle, row)
                    if len(samples["searchThema_missing_metadata"]) < args.sample_limit:
                        samples["searchThema_missing_metadata"].append(row)
                    if args.repair_missing:
                        crawler.metadata_manager.save_item(mark_metadata_only(item))
                        existing_ids.add(item_id)
                        summary["repaired_metadata"] += 1

                page_number += 1
                if crawler.page_delay > 0:
                    await asyncio.sleep(crawler.page_delay)

            if args.progress_interval and summary["combinations"] % args.progress_interval == 0:
                logger.info(
                    "SearchThema audit progress combinations=%s unique=%s missing=%s repaired=%s",
                    summary["combinations"],
                    summary["unique_items_scanned"],
                    summary["missing_metadata"],
                    summary["repaired_metadata"],
                )

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit metadata item coverage against source lists.")
    parser.add_argument("--artifacts-root", default="artifacts")
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--manifest", default="artifacts/validation/metadata_gap_manifest.jsonl")
    parser.add_argument("--source", choices=["all", "pety", "searchThema"], default="all")
    parser.add_argument("--repair-missing", action="store_true", help="Save metadata-only item JSON for gaps.")
    parser.add_argument("--sample-limit", type=int, default=50)
    parser.add_argument("--progress-interval", type=int, default=10)
    parser.add_argument(
        "--search-list-size",
        type=int,
        default=int(os.getenv("METADATA_AUDIT_SEARCH_LIST_SIZE", "100")),
    )
    parser.add_argument(
        "--search-page-delay",
        type=float,
        default=float(os.getenv("METADATA_AUDIT_SEARCH_PAGE_DELAY", "0.05")),
    )
    parser.add_argument(
        "--pety-page-delay",
        type=float,
        default=float(os.getenv("METADATA_AUDIT_PETY_PAGE_DELAY", "0.02")),
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    logger = setup_logger("audit_metadata_coverage")
    repo_root = Path.cwd().resolve()
    artifacts_root = (repo_root / args.artifacts_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = (repo_root / args.manifest).resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    selected_sources = SOURCES if args.source == "all" else (args.source,)
    started_at = iso_now()
    summary: dict[str, dict[str, int]] = {}
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    existing_ids_by_source: dict[str, set[str]] = {}
    for source in selected_sources:
        ids, scan_summary, scan_samples = scan_existing_metadata_ids(
            artifacts_root,
            source,
            args.sample_limit,
        )
        existing_ids_by_source[source] = ids
        summary[f"{source}:existing_scan"] = dict(scan_summary)
        if scan_samples:
            samples[f"{source}_existing_scan"].extend(scan_samples)
        logger.info("%s existing metadata ids=%s", source, len(ids))

    tmp_manifest = manifest_path.with_suffix(f"{manifest_path.suffix}.{os.getpid()}.tmp")
    with tmp_manifest.open("w", encoding="utf-8") as manifest_handle:
        if "pety" in selected_sources:
            summary["pety:audit"] = dict(
                await audit_pety(
                    args,
                    artifacts_root,
                    existing_ids_by_source["pety"],
                    manifest_handle,
                    samples,
                )
            )
        if "searchThema" in selected_sources:
            summary["searchThema:audit"] = dict(
                await audit_search_thema(
                    args,
                    artifacts_root,
                    existing_ids_by_source["searchThema"],
                    manifest_handle,
                    samples,
                )
            )
    tmp_manifest.replace(manifest_path)

    report = {
        "started_at": started_at,
        "finished_at": iso_now(),
        "repair_missing": bool(args.repair_missing),
        "manifest": str(manifest_path),
        "summary": summary,
        "samples": samples,
    }
    report_path = output_dir / f"metadata_coverage_audit_{utc_stamp()}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    print(f"report={report_path}")
    print(f"manifest={manifest_path}")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> None:
    configure_windows_asyncio_policy()
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
