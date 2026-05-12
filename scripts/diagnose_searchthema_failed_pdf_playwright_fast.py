#!/usr/bin/env python3
"""Fast Playwright request diagnostics for failed SearchThema PDF rows."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import async_playwright


BASE_URL = "https://gwanbo.go.kr/"
THEME_URL = "https://gwanbo.go.kr/user/search/searchThema.do?tabType=1"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.7727.15 Safari/537.36"
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast Playwright diagnostics for failed SearchThema PDFs.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--concurrency", type=int, default=6)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def first_query_value(url_text: str, key: str) -> str:
    if not url_text:
        return ""
    values = parse_qs(urlparse(url_text).query).get(key) or []
    return str(values[0]).strip() if values else ""


def viewer_url_for_item(item: dict[str, Any]) -> str:
    path = str(item.get("stored_field_url") or item.get("viewer_path") or "")
    return urljoin(BASE_URL, path.lstrip("/")) if path else ""


def toc_id_for_item(item: dict[str, Any]) -> str:
    for key in ("toc_id", "stored_toc_seq", "keyword_toc_seq", "stored_file_name", "id"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    for key in ("viewer_path", "stored_field_url", "url"):
        value = first_query_value(str(item.get(key) or ""), "tocId")
        if value:
            return value
    return ""


def content_id_for_item(item: dict[str, Any]) -> str:
    value = str(item.get("content_id") or "").strip()
    if value:
        return value
    for key in ("viewer_path", "stored_field_url", "url"):
        value = first_query_value(str(item.get(key) or ""), "contentId")
        if value:
            return value
    return ""


def pdf_file_complete(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size <= 0:
            return False
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return False
            tail_size = min(size, 4096)
            handle.seek(-tail_size, os.SEEK_END)
            return b"%%EOF" in handle.read(tail_size)
    except OSError:
        return False


def local_pdf_summary(item: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    path_text = str((pdf or {}).get("path") or "").strip()
    path = Path(path_text) if path_text else None
    if path and not path.is_absolute():
        path = repo_root / path
    return {
        "metadata_status": (pdf or {}).get("status"),
        "metadata_path": path_text,
        "metadata_size_bytes": (pdf or {}).get("size_bytes"),
        "metadata_error": (pdf or {}).get("error"),
        "exists": bool(path and path.exists()),
        "actual_size_bytes": path.stat().st_size if path and path.exists() else None,
        "complete": pdf_file_complete(path) if path else False,
    }


def metadata_summary(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "toc_id",
        "stored_toc_seq",
        "keyword_toc_seq",
        "content_id",
        "stored_pdf_file_path",
        "stored_file_size",
        "stored_file_type",
        "stored_service_yn",
        "stored_field_url",
        "viewer_path",
        "date",
        "title",
        "stored_field_subject",
        "stored_category_name",
        "stored_organ_nm",
        "status",
    )
    return {key: item.get(key) for key in keys if item.get(key) not in (None, "")}


def summarize_body(body: bytes) -> dict[str, Any]:
    is_pdf = body.startswith(b"%PDF-")
    has_eof = b"%%EOF" in body[-4096:]
    text_prefix = ""
    if not is_pdf:
        text_prefix = " ".join(body[:240].decode("utf-8", errors="replace").split())
    return {
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest() if body else "",
        "starts_pdf": is_pdf,
        "has_eof": has_eof,
        "pdf_complete": is_pdf and has_eof,
        "text_prefix": text_prefix,
    }


def load_manifest_rows(manifest: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        row["_line_number"] = line_number
        rows.append(row)
    return rows


async def request_body_summary(response: Any) -> dict[str, Any]:
    body = await response.body()
    return summarize_body(body)


async def diagnose_one(request: Any, row: dict[str, Any], repo_root: Path, timeout_ms: int) -> dict[str, Any]:
    item_path = Path(str(row.get("item_path") or ""))
    if not item_path.is_absolute():
        item_path = repo_root / item_path
    record: dict[str, Any] = {
        "manifest_row": row,
        "item_path": str(item_path),
    }
    try:
        item = load_json(item_path)
    except Exception as exc:  # noqa: BLE001
        record["metadata_error"] = str(exc)
        return record

    toc_id = toc_id_for_item(item)
    viewer_url = viewer_url_for_item(item)
    record.update(
        {
            "id": str(item.get("id") or toc_id or item_path.stem),
            "toc_id": toc_id,
            "content_id": content_id_for_item(item),
            "date": item.get("date"),
            "title": item.get("title") or item.get("stored_field_subject"),
            "metadata": metadata_summary(item),
            "local_pdf": local_pdf_summary(item, repo_root),
            "viewer_url": viewer_url,
        }
    )

    if viewer_url:
        started = datetime.now(timezone.utc)
        try:
            response = await request.get(viewer_url, timeout=timeout_ms)
            html = await response.text()
            record["viewer_request"] = {
                "status": response.status,
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                "content_type": response.headers.get("content-type", ""),
                "html_bytes": len(html.encode("utf-8", errors="replace")),
                "has_cntnt_download_endpoint": "ofcttCntntDownload.do" in html,
                "has_issue_download_endpoint": "ofcttDownload.do" in html,
                "title_contains_ezpdf": "ezPDF" in html,
            }
        except Exception as exc:  # noqa: BLE001
            record["viewer_request"] = {
                "error": str(exc),
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            }

    if toc_id:
        download_url = urljoin(BASE_URL, "user/common/ofcttCntntDownload.do")
        form = {"cntnt_seq_no": toc_id}
        record["direct_download_request"] = {"url": download_url, "form_data": form}
        started = datetime.now(timezone.utc)
        try:
            response = await request.post(download_url, form=form, timeout=timeout_ms)
            record["direct_pdf_request"] = {
                "status": response.status,
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                "content_type": response.headers.get("content-type", ""),
                "content_length": response.headers.get("content-length", ""),
                "body": await request_body_summary(response),
            }
        except Exception as exc:  # noqa: BLE001
            record["direct_pdf_request"] = {
                "error": str(exc),
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            }
    return record


async def main() -> None:
    args = parse_args()
    repo_root = Path.cwd().resolve()
    manifest = Path(args.manifest)
    if not manifest.is_absolute():
        manifest = repo_root / manifest
    rows = load_manifest_rows(manifest)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"searchthema_failed_pdf_playwright_fast_{utc_stamp()}.json"

    async with async_playwright() as playwright:
        request = await playwright.request.new_context(
            ignore_https_errors=True,
            user_agent=USER_AGENT,
            extra_http_headers={"Referer": BASE_URL},
        )
        prime_started = datetime.now(timezone.utc)
        try:
            prime_response = await request.get(THEME_URL, timeout=args.timeout_ms)
            prime = {
                "status": prime_response.status,
                "duration_ms": int((datetime.now(timezone.utc) - prime_started).total_seconds() * 1000),
            }
        except Exception as exc:  # noqa: BLE001
            prime = {
                "error": str(exc),
                "duration_ms": int((datetime.now(timezone.utc) - prime_started).total_seconds() * 1000),
            }

        sem = asyncio.Semaphore(args.concurrency)

        async def worker(row: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                return await diagnose_one(request, row, repo_root, args.timeout_ms)

        items = await asyncio.gather(*(worker(row) for row in rows))
        await request.dispose()

    summary: Counter[str] = Counter()
    for item in items:
        direct = item.get("direct_pdf_request") or {}
        body = direct.get("body") or {}
        viewer = item.get("viewer_request") or {}
        if body.get("pdf_complete"):
            summary["direct_pdf_complete"] += 1
        elif direct.get("status"):
            summary[f"direct_status_{direct.get('status')}"] += 1
        elif direct.get("error"):
            summary["direct_error"] += 1
        if viewer.get("status"):
            summary[f"viewer_status_{viewer.get('status')}"] += 1
        elif viewer.get("error"):
            summary["viewer_error"] += 1
        if (item.get("local_pdf") or {}).get("complete"):
            summary["local_pdf_complete"] += 1

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(manifest),
        "row_count": len(rows),
        "timeout_ms": args.timeout_ms,
        "concurrency": args.concurrency,
        "prime": prime,
        "summary": dict(summary),
        "items": items,
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(output_path)
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    print("id date viewer direct_status direct_bytes direct_pdf_complete local_complete")
    for item in items:
        direct = item.get("direct_pdf_request") or {}
        body = direct.get("body") or {}
        viewer = item.get("viewer_request") or {}
        print(
            item.get("id"),
            item.get("date"),
            viewer.get("status") or "ERR",
            direct.get("status") or "ERR",
            body.get("bytes"),
            body.get("pdf_complete"),
            (item.get("local_pdf") or {}).get("complete"),
        )


if __name__ == "__main__":
    asyncio.run(main())
