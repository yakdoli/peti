#!/usr/bin/env python3
"""Repair SearchThema zero-page content PDFs by promoting valid issue PDFs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from entrypoint_utils import add_project_paths, configure_windows_asyncio_policy


add_project_paths()

from PyPDF2 import PdfReader  # noqa: E402
from src.crawler_search_thema import SearchThemaCrawler  # noqa: E402


@dataclass
class PdfValidation:
    status: str
    pages: int = 0
    error: str = ""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def repo_path(path_text: str | Path, repo_root: Path) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else repo_root / path


def rel_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path.resolve())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdfinfo_pages(path: Path) -> PdfValidation:
    completed = subprocess.run(["pdfinfo", str(path)], text=True, capture_output=True, check=False)
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0:
        return PdfValidation("error", error=output[:2000])
    for line in completed.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                pages = int(line.split(":", 1)[1].strip())
            except ValueError:
                return PdfValidation("error", error=f"invalid Pages line: {line}")
            if pages <= 0:
                return PdfValidation("error", pages=pages, error="pdfinfo returned zero pages")
            return PdfValidation("ok", pages=pages)
    return PdfValidation("error", error="pdfinfo did not report Pages")


def validate_pdf_render(path: Path, *, page: int = 1) -> PdfValidation:
    info = pdfinfo_pages(path)
    if info.status != "ok":
        return info
    if page < 1 or page > info.pages:
        return PdfValidation("error", pages=info.pages, error=f"page {page} is outside 1..{info.pages}")
    with tempfile.TemporaryDirectory(prefix="peti-pdf-render-check-") as temp:
        prefix = Path(temp) / "page"
        completed = subprocess.run(
            ["pdftoppm", "-f", str(page), "-l", str(page), "-r", "72", "-png", str(path), str(prefix)],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            output = f"{completed.stdout}\n{completed.stderr}".strip()
            return PdfValidation("error", pages=info.pages, error=output[:2000])
        if not list(Path(temp).glob("page-*.png")):
            return PdfValidation("error", pages=info.pages, error="pdftoppm produced no image")
    return info


def compact_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


def title_needles(item: dict[str, Any]) -> list[str]:
    title = str(item.get("title") or item.get("stored_field_subject") or item.get("keyword_field_subject") or "")
    candidates: list[str] = []
    doc_no = re.match(r"(.+?제\s*\d{4}\s*[-–]\s*\d+\s*호)", title)
    if doc_no:
        candidates.append(doc_no.group(1))
    before_paren = re.split(r"[\(<〈]", title, maxsplit=1)[0].strip()
    if before_paren:
        candidates.append(before_paren)
    candidates.append(title)

    seen: set[str] = set()
    needles: list[str] = []
    for candidate in candidates:
        needle = compact_text(candidate)
        if len(needle) < 8 or needle in seen:
            continue
        needles.append(needle)
        seen.add(needle)
    return needles


def find_target_page(pdf_path: Path, item: dict[str, Any]) -> dict[str, Any]:
    needles = title_needles(item)
    if not needles:
        return {"status": "not_found", "error": "no usable title needle", "needles": []}

    reader = PdfReader(str(pdf_path))
    hits: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        compact = compact_text(text)
        matched = [needle for needle in needles if needle and needle in compact]
        if matched:
            hits.append({"page": page_number, "matched_chars": max(len(needle) for needle in matched)})

    if not hits:
        return {"status": "not_found", "error": "title was not found in issue PDF", "needles": needles}

    non_toc_hits = [hit for hit in hits if int(hit["page"]) > 10]
    chosen = max(non_toc_hits or hits, key=lambda hit: (int(hit["matched_chars"]), -int(hit["page"])))
    return {
        "status": "ok",
        "page": int(chosen["page"]),
        "hits": hits[:20],
        "hit_count": len(hits),
        "needles": needles,
    }


def collect_failed_item_paths(output_roots: list[Path], repo_root: Path) -> list[Path]:
    paths: dict[str, Path] = {}
    for output_root in output_roots:
        root = repo_path(output_root, repo_root)
        for results_path in sorted(root.glob("*/results.jsonl")):
            for line in results_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(record.get("status") or "") != "error":
                    continue
                item_path_text = str(record.get("item_path") or "")
                if not item_path_text:
                    continue
                path = repo_path(item_path_text, repo_root).resolve()
                paths[str(path)] = path
    return [paths[key] for key in sorted(paths)]


async def download_candidate(
    crawler: SearchThemaCrawler,
    item: dict[str, Any],
    *,
    scope: str,
    target_path: Path,
) -> dict[str, Any]:
    request = crawler._direct_pdf_download_request(item) if scope == "content" else crawler._issue_pdf_download_request(item)
    if request is None:
        return {"scope": scope, "status": "error", "error": "download request is unavailable"}
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        download = await crawler._download_pdf_stream(crawler._empty_cookie_context(), request[0], request[1], target_path)
    except Exception as exc:
        return {"scope": scope, "status": "error", "error": f"{type(exc).__name__}: {exc}"}

    validation = validate_pdf_render(target_path, page=1)
    result = {
        "scope": scope,
        "path": str(target_path),
        "download": download,
        "strict_validation": {
            "status": validation.status,
            "pages": validation.pages,
            "error": validation.error,
        },
    }
    result["status"] = "ok" if validation.status == "ok" else "invalid"
    return result


def update_item_metadata(
    item: dict[str, Any],
    *,
    repo_root: Path,
    item_path: Path,
    selected_path: Path,
    selected_scope: str,
    selected_method: str,
    page_count: int,
    target_page: int,
    target_page_evidence: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    pdf = item.setdefault("pdf", {})
    previous_pdf = {
        "path": pdf.get("path"),
        "sha256": pdf.get("sha256"),
        "size_bytes": pdf.get("size_bytes"),
        "status": pdf.get("status"),
    }
    selected_rel_path = rel_path(selected_path, repo_root)
    now = iso_now()
    pdf.update(
        {
            "status": "completed",
            "path": selected_rel_path,
            "size_bytes": selected_path.stat().st_size,
            "sha256": sha256_file(selected_path),
            "downloaded_at": now,
            "method": selected_method,
            "scope": selected_scope,
            "page_count": page_count,
            "ocr_target_pages": [target_page],
            "strict_validation": {
                "status": "ok",
                "method": "pdfinfo+pdftoppm",
                "pages": page_count,
                "target_page_rendered": target_page,
                "checked_at": now,
            },
            "repair": {
                "status": "repaired",
                "strategy": "searchthema_zero_page_issue_pdf_fallback",
                "previous_pdf": previous_pdf,
                "candidates": candidates,
                "target_page_evidence": target_page_evidence,
                "repaired_at": now,
            },
        }
    )
    pdf.pop("error", None)
    pdf.pop("failed_at", None)

    pdf_text = item.setdefault("pdf_text", {})
    if isinstance(pdf_text, dict):
        pdf_text.update(
            {
                "path": str((repo_root / selected_rel_path).resolve()),
                "pdf_path": str((repo_root / selected_rel_path).resolve()),
                "pdf_path_text": selected_rel_path,
                "filename": selected_path.name,
                "size_bytes": selected_path.stat().st_size,
                "pages": page_count,
                "pdf_scope": selected_scope,
                "needs_ocr": True,
                "repair": {
                    "status": "repaired",
                    "target_pages": [target_page],
                    "source_item_path": str(item_path),
                    "updated_at": now,
                },
            }
        )

    ocr = item.setdefault("ocr", {})
    if isinstance(ocr, dict):
        ocr["status"] = "pending"
        ocr["skip_reason"] = ""

    item["status"] = "completed"
    item["updated_at"] = now
    return item


async def repair_item(crawler: SearchThemaCrawler, item_path: Path, repo_root: Path, *, dry_run: bool) -> dict[str, Any]:
    item = json.loads(item_path.read_text(encoding="utf-8"))
    if not isinstance(item, dict):
        return {"item_path": str(item_path), "status": "metadata_error", "error": "item metadata is not object"}

    crawler._prepare_pdf_item(item)
    content_path = crawler._pdf_path_for_item(item)
    issue_path = crawler._issue_pdf_path_for_item(item)
    candidates: list[dict[str, Any]] = []

    for scope, target_path in (("content", content_path), ("issue", issue_path)):
        candidate = await download_candidate(crawler, item, scope=scope, target_path=target_path)
        candidates.append(candidate)
        if candidate.get("status") != "ok":
            continue

        selected_path = Path(str(candidate["path"]))
        page_count = int(candidate["strict_validation"]["pages"])
        if scope == "content":
            target_page_info = {"status": "ok", "page": 1, "method": "content_pdf_default"}
        else:
            target_page_info = find_target_page(selected_path, item)
            if target_page_info.get("status") != "ok":
                continue
        target_page = int(target_page_info["page"])
        target_validation = validate_pdf_render(selected_path, page=target_page)
        if target_validation.status != "ok":
            candidate["target_validation"] = {
                "status": target_validation.status,
                "pages": target_validation.pages,
                "error": target_validation.error,
                "page": target_page,
            }
            continue

        if not dry_run:
            updated = update_item_metadata(
                item,
                repo_root=repo_root,
                item_path=item_path,
                selected_path=selected_path,
                selected_scope=scope,
                selected_method="content_pdf_direct" if scope == "content" else "issue_pdf_fallback_strict",
                page_count=page_count,
                target_page=target_page,
                target_page_evidence=target_page_info,
                candidates=candidates,
            )
            write_json(item_path, updated)
        return {
            "item_path": str(item_path),
            "status": "repaired",
            "scope": scope,
            "pdf_path": str(selected_path),
            "pages": page_count,
            "ocr_target_pages": [target_page],
            "target_page_evidence": target_page_info,
            "dry_run": dry_run,
        }

    return {"item_path": str(item_path), "status": "failed", "candidates": candidates, "dry_run": dry_run}


async def run(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    output_roots = [Path(part.strip()) for part in args.failed_output_roots.split(",") if part.strip()]
    item_paths = [repo_path(path, repo_root).resolve() for path in args.item_path]
    if not item_paths:
        item_paths = collect_failed_item_paths(output_roots, repo_root)
    if args.limit is not None:
        item_paths = item_paths[: args.limit]

    output_log = repo_path(args.output_log, repo_root)
    crawler = SearchThemaCrawler(
        metadata_only=False,
        resume=False,
        save_indexes=False,
        use_browser=False,
        preload_metadata=False,
        concurrency=1,
    )
    counts: dict[str, int] = {}
    print(f"repair started items={len(item_paths)} dry_run={args.dry_run} log={output_log}", flush=True)
    for index, item_path in enumerate(item_paths, start=1):
        result = await repair_item(crawler, item_path, repo_root, dry_run=args.dry_run)
        result["index"] = index
        result["processed_at"] = iso_now()
        append_jsonl(output_log, result)
        status = str(result.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        print(
            f"progress {index}/{len(item_paths)} status={status} "
            f"target={result.get('ocr_target_pages')} item={item_path.name}",
            flush=True,
        )
    print(json.dumps({"counts": counts, "output_log": str(output_log)}, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if counts.get("failed", 0) == 0 and counts.get("metadata_error", 0) == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--failed-output-roots",
        default="artifacts/validation/ocr_batch_v18_rebalanced_api_resume",
        help="comma-separated batch output roots to scan for status=error rows",
    )
    parser.add_argument("--item-path", action="append", default=[], help="explicit item metadata path; repeatable")
    parser.add_argument("--output-log", default=f"logs/pdf_repair/searchthema_zero_page_repair_{utc_stamp()}.jsonl")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    configure_windows_asyncio_policy()
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
