#!/usr/bin/env python3
"""Download SearchThema PDFs through a real browser page."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright


BASE_URL = "https://gwanbo.go.kr/"
THEME_URL = "https://gwanbo.go.kr/user/search/searchThema.do?tabType=1"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.7727.15 Safari/537.36"
)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browser-download SearchThema PDFs from a manifest.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--failure-log", default="")
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--progress-interval", type=int, default=5)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def safe_filename(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", str(value)).strip("._") or "unknown"


def first_query_value(url_text: str, key: str) -> str:
    values = parse_qs(urlparse(str(url_text or "")).query).get(key) or []
    return str(values[0]).strip() if values else ""


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


def item_date(item: dict[str, Any]) -> str:
    date = str(item.get("date") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return date
    regdate = str(item.get("keyword_field_regdate") or "").strip()
    if re.match(r"^\d{8}$", regdate):
        return f"{regdate[:4]}-{regdate[4:6]}-{regdate[6:8]}"
    year = str(item.get("stored_field_year") or "").strip()
    month = str(item.get("stored_field_month") or "").zfill(2)
    day = str(item.get("stored_field_day") or "").zfill(2)
    if year and month and day:
        return f"{year}-{month}-{day}"
    return "unknown"


def pdf_path_for_item(item: dict[str, Any], repo_root: Path) -> Path:
    date = item_date(item)
    year = date[:4] if len(date) >= 4 else "unknown"
    date_key = date.replace("-", "") if len(date) == 10 else "unknown"
    item_id = str(item.get("id") or toc_id_for_item(item) or "unknown")
    return repo_root / "artifacts" / "searchThema" / "pdfs" / year / date_key / f"{safe_filename(item_id)}.pdf"


def issue_pdf_path_for_item(item: dict[str, Any], repo_root: Path) -> Path:
    date = item_date(item)
    year = date[:4] if len(date) >= 4 else "unknown"
    date_key = date.replace("-", "") if len(date) == 10 else "unknown"
    item_id = str(item.get("id") or toc_id_for_item(item) or "unknown")
    return repo_root / "artifacts" / "searchThema" / "issue_pdfs" / year / date_key / f"{safe_filename(item_id)}.pdf"


def pdf_complete(path: Path) -> bool:
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize_body(path: Path) -> dict[str, Any]:
    try:
        body = path.read_bytes()
    except OSError as exc:
        return {"error": str(exc), "bytes": 0, "pdf_complete": False}
    prefix = ""
    if not body.startswith(b"%PDF-"):
        prefix = " ".join(body[:240].decode("utf-8", errors="replace").split())
    return {
        "bytes": len(body),
        "starts_pdf": body.startswith(b"%PDF-"),
        "has_eof": b"%%EOF" in body[-4096:],
        "pdf_complete": body.startswith(b"%PDF-") and b"%%EOF" in body[-4096:],
        "text_prefix": prefix,
    }


def load_manifest(path: Path, repo_root: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        item_path = Path(str(row.get("item_path") or ""))
        if not item_path.is_absolute():
            item_path = repo_root / item_path
        row["item_path"] = str(item_path)
        row["_line_number"] = line_number
        rows.append(row)
    return rows


async def submit_form_download(page: Page, url: str, form_data: dict[str, str], temp_path: Path, timeout_ms: int) -> dict[str, Any]:
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    started = datetime.now(timezone.utc)
    try:
        async with page.expect_download(timeout=timeout_ms) as download_info:
            await page.evaluate(
                """({ url, formData }) => {
                    const form = document.createElement("form");
                    form.method = "POST";
                    form.action = url;
                    form.style.display = "none";
                    for (const [key, value] of Object.entries(formData)) {
                        const input = document.createElement("input");
                        input.type = "hidden";
                        input.name = key;
                        input.value = value;
                        form.appendChild(input);
                    }
                    document.body.appendChild(form);
                    form.submit();
                }""",
                {"url": url, "formData": form_data},
            )
        download = await download_info.value
        await download.save_as(temp_path)
        return {
            "method": "form_download",
            "suggested_filename": download.suggested_filename,
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "body": summarize_body(temp_path),
        }
    except Exception as exc:  # noqa: BLE001
        temp_path.unlink(missing_ok=True)
        return {
            "method": "form_download",
            "error": str(exc),
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        }


async def browser_fetch_download(page: Page, url: str, form_data: dict[str, str], temp_path: Path, timeout_ms: int) -> dict[str, Any]:
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    started = datetime.now(timezone.utc)
    try:
        result = await page.evaluate(
            """async ({ url, formData }) => {
                const response = await fetch(url, {
                    method: "POST",
                    headers: {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                    body: new URLSearchParams(formData).toString(),
                    credentials: "include"
                });
                const buffer = await response.arrayBuffer();
                const bytes = new Uint8Array(buffer);
                let binary = "";
                const chunkSize = 0x8000;
                for (let i = 0; i < bytes.length; i += chunkSize) {
                    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
                }
                return {
                    status: response.status,
                    ok: response.ok,
                    contentType: response.headers.get("content-type") || "",
                    contentLength: response.headers.get("content-length") || "",
                    bodyBase64: btoa(binary)
                };
            }""",
            {"url": url, "formData": form_data},
        )
        temp_path.write_bytes(base64.b64decode(result["bodyBase64"]))
        return {
            "method": "browser_fetch",
            "status": result.get("status"),
            "ok": result.get("ok"),
            "content_type": result.get("contentType"),
            "content_length": result.get("contentLength"),
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "body": summarize_body(temp_path),
        }
    except Exception as exc:  # noqa: BLE001
        temp_path.unlink(missing_ok=True)
        return {
            "method": "browser_fetch",
            "error": str(exc),
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        }


def update_item_metadata(
    item_path: Path,
    item: dict[str, Any],
    pdf_path: Path,
    repo_root: Path,
    method: str,
    scope: str,
) -> None:
    item["id"] = str(item.get("id") or toc_id_for_item(item))
    item["toc_id"] = item.get("toc_id") or toc_id_for_item(item)
    item["content_id"] = item.get("content_id") or content_id_for_item(item)
    item["date"] = item.get("date") or item_date(item)
    item["pdf"] = {
        "downloaded_at": iso_now(),
        "path": str(pdf_path.relative_to(repo_root)),
        "sha256": sha256_file(pdf_path),
        "size_bytes": pdf_path.stat().st_size,
        "status": "completed",
        "method": method,
        "scope": scope,
    }
    item["status"] = "completed"
    item["updated_at"] = iso_now()
    write_json(item_path, item)


async def process_row(
    context: Any,
    row: dict[str, Any],
    repo_root: Path,
    temp_dir: Path,
    timeout_ms: int,
) -> dict[str, Any]:
    item_path = Path(str(row["item_path"]))
    record: dict[str, Any] = {"row": row, "item_path": str(item_path)}
    try:
        item = load_json(item_path)
    except Exception as exc:  # noqa: BLE001
        record.update({"status": "metadata_error", "error": str(exc)})
        return record

    toc_id = toc_id_for_item(item)
    record.update(
        {
            "id": str(item.get("id") or toc_id or item_path.stem),
            "toc_id": toc_id,
            "date": item.get("date") or item_date(item),
            "title": item.get("title") or item.get("stored_field_subject"),
            "stored_pdf_file_path": item.get("stored_pdf_file_path"),
            "stored_file_size": item.get("stored_file_size"),
        }
    )
    if not toc_id:
        record.update({"status": "failed", "error": "missing toc_id"})
        return record

    pdf_path = pdf_path_for_item(item, repo_root)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{safe_filename(str(record['id']))}.{os.getpid()}.pdf.tmp"
    attempts = [
        (
            "content_pdf",
            urljoin(BASE_URL, "user/common/ofcttCntntDownload.do"),
            {"cntnt_seq_no": toc_id},
        )
    ]
    content_id = content_id_for_item(item)
    if content_id:
        attempts.append(
            (
                "issue_pdf_fallback",
                urljoin(BASE_URL, "user/common/ofcttDownload.do"),
                {"downType": "1", "ofctt_seq_no": content_id},
            )
        )
    page = await context.new_page()
    try:
        for label, download_url, form_data in attempts:
            target_path = issue_pdf_path_for_item(item, repo_root) if label == "issue_pdf_fallback" else pdf_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            scope = "issue" if label == "issue_pdf_fallback" else "content"
            form_result = await submit_form_download(page, download_url, form_data, temp_path, timeout_ms)
            record[f"{label}_form_download"] = form_result
            if (form_result.get("body") or {}).get("pdf_complete"):
                temp_path.replace(target_path)
                method = f"browser_form_download_{label}"
                update_item_metadata(item_path, item, target_path, repo_root, method, scope)
                record.update({"status": "downloaded", "path": str(target_path), "method": method, "scope": scope})
                return record

            fetch_result = await browser_fetch_download(page, download_url, form_data, temp_path, timeout_ms)
            record[f"{label}_browser_fetch"] = fetch_result
            if (fetch_result.get("body") or {}).get("pdf_complete"):
                temp_path.replace(target_path)
                method = f"browser_fetch_download_{label}"
                update_item_metadata(item_path, item, target_path, repo_root, method, scope)
                record.update({"status": "downloaded", "path": str(target_path), "method": method, "scope": scope})
                return record

        temp_path.unlink(missing_ok=True)
        record.update({"status": "failed", "error": "browser download did not produce complete PDF"})
        return record
    finally:
        temp_path.unlink(missing_ok=True)
        await page.close()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path.cwd().resolve()
    manifest = Path(args.manifest)
    if not manifest.is_absolute():
        manifest = repo_root / manifest
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = repo_root / "artifacts" / "state" / "browser_download_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    failure_log = Path(args.failure_log) if args.failure_log else output_dir / f"browser_download_failures_{utc_stamp()}.jsonl"
    if not failure_log.is_absolute():
        failure_log = repo_root / failure_log
    failure_log.write_text("", encoding="utf-8")

    rows = load_manifest(manifest, repo_root)
    report_path = output_dir / f"browser_download_searchthema_{utc_stamp()}.json"
    results: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    completed = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        context = await browser.new_context(
            accept_downloads=True,
            ignore_https_errors=True,
            user_agent=USER_AGENT,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        prime_page = await context.new_page()
        try:
            await prime_page.goto(THEME_URL, wait_until="domcontentloaded", timeout=args.timeout_ms)
        finally:
            await prime_page.close()

        sem = asyncio.Semaphore(args.concurrency)

        async def worker(row: dict[str, Any]) -> None:
            nonlocal completed
            async with sem:
                result = await process_row(context, row, repo_root, temp_dir, args.timeout_ms)
            results.append(result)
            counts[str(result.get("status") or "unknown")] += 1
            if result.get("status") != "downloaded":
                append_jsonl(failure_log, result)
            completed += 1
            if completed % args.progress_interval == 0 or completed == len(rows):
                print(
                    f"progress completed={completed}/{len(rows)} "
                    f"downloaded={counts['downloaded']} failed={counts['failed']} metadata_error={counts['metadata_error']}",
                    flush=True,
                )

        await asyncio.gather(*(worker(row) for row in rows))
        await context.close()
        await browser.close()

    report = {
        "created_at": iso_now(),
        "manifest": str(manifest),
        "failure_log": str(failure_log),
        "row_count": len(rows),
        "counts": dict(counts),
        "results": results,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"report={report_path}")
    print(f"failure_log={failure_log}")
    print(json.dumps(report["counts"], ensure_ascii=False, sort_keys=True))
    return report


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
