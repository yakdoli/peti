#!/usr/bin/env python3
"""Diagnose SearchThema PDF download behavior with Playwright."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import APIRequestContext, BrowserContext, Page, async_playwright


BASE_URL = "https://gwanbo.go.kr/"
THEME_URL = "https://gwanbo.go.kr/user/search/searchThema.do?tabType=1"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.7727.15 Safari/537.36"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use Playwright to inspect SearchThema viewer and PDF download paths."
    )
    parser.add_argument(
        "--manifest",
        default="artifacts/searchThema/state/searchthema_new_metadata_pdf_manifest_20260511_152442.jsonl",
        help="JSONL manifest with item_path rows.",
    )
    parser.add_argument("--item", action="append", default=[], help="Specific metadata item JSON path. Repeatable.")
    parser.add_argument("--sample-limit", type=int, default=6)
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def mask_cookie_value(value: str) -> str:
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_sample_items(args: argparse.Namespace) -> list[tuple[Path, dict[str, Any]]]:
    paths: list[Path] = [Path(value) for value in args.item]
    manifest_rows: dict[str, dict[str, Any]] = {}
    manifest = Path(args.manifest)
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if len(paths) >= args.sample_limit:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_path = Path(str(row.get("item_path") or ""))
            if item_path.exists() and item_path not in paths:
                paths.append(item_path)
            if item_path.exists():
                manifest_rows[str(item_path)] = row
                manifest_rows[str(item_path.resolve())] = row
    return [(path, manifest_rows.get(str(path)) or manifest_rows.get(str(path.resolve())) or {}) for path in paths[: args.sample_limit]]


def viewer_url_for_item(item: dict[str, Any]) -> str:
    path = str(item.get("stored_field_url") or item.get("viewer_path") or "")
    if not path:
        raise RuntimeError("stored_field_url/viewer_path is missing")
    return urljoin(BASE_URL, path.lstrip("/"))


def extract_download_request(viewer_html: str, item: dict[str, Any]) -> tuple[str, dict[str, str]]:
    content_match = re.search(
        r"(/user/common/ofcttCntntDownload\.do(?:;jsessionid=[A-Za-z0-9_.-]+)?)",
        viewer_html,
    )
    toc_id = str(item.get("toc_id") or item.get("stored_toc_seq") or "")
    if content_match and toc_id:
        return urljoin(BASE_URL, content_match.group(1)), {"cntnt_seq_no": toc_id}

    issue_match = re.search(
        r"(/user/common/ofcttDownload\.do(?:;jsessionid=[A-Za-z0-9_.-]+)?)",
        viewer_html,
    )
    content_id = str(item.get("content_id") or "")
    if issue_match and content_id:
        return urljoin(BASE_URL, issue_match.group(1)), {"downType": "1", "ofctt_seq_no": content_id}

    raise RuntimeError("PDF download endpoint was not found in viewer HTML")


def direct_download_request(item: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
    toc_id = str(item.get("toc_id") or item.get("stored_toc_seq") or item.get("keyword_toc_seq") or "").strip()
    if not toc_id:
        return None
    return urljoin(BASE_URL, "user/common/ofcttCntntDownload.do"), {"cntnt_seq_no": toc_id}


def pdf_file_is_complete(path: Path) -> bool:
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


def local_pdf_summary(item: dict[str, Any]) -> dict[str, Any]:
    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    path_text = str((pdf or {}).get("path") or "").strip()
    path = Path(path_text) if path_text else None
    if path and not path.is_absolute():
        path = Path.cwd() / path
    return {
        "metadata_status": (pdf or {}).get("status"),
        "metadata_path": path_text,
        "metadata_size_bytes": (pdf or {}).get("size_bytes"),
        "metadata_error": (pdf or {}).get("error"),
        "exists": bool(path and path.exists()),
        "actual_size_bytes": path.stat().st_size if path and path.exists() else None,
        "complete": pdf_file_is_complete(path) if path else False,
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


def summarize_pdf_body(body: bytes) -> dict[str, Any]:
    return {
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "starts_pdf": body.startswith(b"%PDF-"),
        "has_eof": b"%%EOF" in body[-4096:],
        "head": body[:24].decode("latin1", errors="replace"),
        "tail": body[-48:].decode("latin1", errors="replace") if body else "",
    }


async def read_response_body(response: Any) -> bytes:
    try:
        return await response.body()
    except AttributeError:
        text = await response.text()
        return text.encode("utf-8", errors="replace")


async def prime_context(context: BrowserContext) -> dict[str, Any]:
    page = await context.new_page()
    started = datetime.now(timezone.utc)
    try:
        response = await page.goto(THEME_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1000)
        return {
            "status": response.status if response else None,
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "url": page.url,
            "title": await page.title(),
        }
    finally:
        await page.close()


async def fetch_viewer_with_network(
    context: BrowserContext,
    viewer_url: str,
    timeout_ms: int,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    page = await context.new_page()
    network: list[dict[str, Any]] = []

    async def handle_response(response: Any) -> None:
        url = response.url
        if not any(token in url for token in ("customLayout", "ofctt", "ezpdf", "pdf")):
            return
        record = {
            "url": url,
            "status": response.status,
            "method": response.request.method,
            "content_type": response.headers.get("content-type", ""),
        }
        try:
            text = await response.text()
            record["body_bytes"] = len(text.encode("utf-8", errors="replace"))
            record["body_prefix"] = " ".join(text[:160].split())
        except Exception as exc:  # noqa: BLE001 - diagnostics should keep going.
            record["body_error"] = str(exc)
        network.append(record)

    page.on("response", lambda response: asyncio.create_task(handle_response(response)))

    started = datetime.now(timezone.utc)
    try:
        response = await page.goto(viewer_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1500)
        html = await page.content()
        info = {
            "status": response.status if response else None,
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "url": page.url,
            "title": await page.title(),
            "html_bytes": len(html.encode("utf-8", errors="replace")),
        }
        return info, html, network
    finally:
        await page.close()


async def post_pdf_api(
    request: APIRequestContext,
    download_url: str,
    form_data: dict[str, str],
    timeout_ms: int,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    record: dict[str, Any] = {"method": "context.request.post"}
    try:
        response = await request.post(download_url, form=form_data, timeout=timeout_ms)
        body = await response.body()
        record.update(
            {
                "status": response.status,
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                "content_type": response.headers.get("content-type", ""),
                "content_length": response.headers.get("content-length", ""),
                "pdf": summarize_pdf_body(body),
            }
        )
    except Exception as exc:  # noqa: BLE001
        record.update(
            {
                "error": str(exc),
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            }
        )
    return record


async def post_pdf_browser_fetch(
    context: BrowserContext,
    download_url: str,
    form_data: dict[str, str],
    timeout_ms: int,
) -> dict[str, Any]:
    page = await context.new_page()
    started = datetime.now(timezone.utc)
    record: dict[str, Any] = {"method": "browser.fetch"}
    try:
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
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
            {"url": download_url, "formData": form_data},
        )
        body = base64.b64decode(result["bodyBase64"])
        record.update(
            {
                "status": result.get("status"),
                "ok": result.get("ok"),
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                "content_type": result.get("contentType", ""),
                "content_length": result.get("contentLength", ""),
                "pdf": summarize_pdf_body(body),
            }
        )
    except Exception as exc:  # noqa: BLE001
        record.update(
            {
                "error": str(exc),
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            }
        )
    finally:
        await page.close()
    return record


async def diagnose_item(
    context: BrowserContext,
    item_path: Path,
    timeout_ms: int,
    manifest_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = load_json(item_path)
    viewer_url = viewer_url_for_item(item)
    record: dict[str, Any] = {
        "item_path": str(item_path),
        "manifest_row": manifest_row or {},
        "id": str(item.get("id") or item.get("stored_toc_seq") or ""),
        "date": item.get("date"),
        "title": item.get("title") or item.get("stored_field_subject"),
        "stored_pdf_file_path": item.get("stored_pdf_file_path"),
        "viewer_url": viewer_url,
        "metadata": metadata_summary(item),
        "local_pdf": local_pdf_summary(item),
    }

    direct_request = direct_download_request(item)
    if direct_request is not None:
        direct_url, direct_form_data = direct_request
        record["metadata_direct_download_request"] = {"url": direct_url, "form_data": direct_form_data}
        record["metadata_direct_api_download"] = await post_pdf_api(context.request, direct_url, direct_form_data, timeout_ms)
        record["metadata_direct_browser_fetch_download"] = await post_pdf_browser_fetch(
            context,
            direct_url,
            direct_form_data,
            timeout_ms,
        )

    viewer_info, viewer_html, network = await fetch_viewer_with_network(context, viewer_url, timeout_ms)
    record["viewer"] = viewer_info
    record["viewer_network"] = network
    try:
        download_url, form_data = extract_download_request(viewer_html, item)
        record["download_request"] = {"url": download_url, "form_data": form_data}
    except Exception as exc:  # noqa: BLE001
        record["download_extract_error"] = str(exc)
        return record

    record["api_request_download"] = await post_pdf_api(context.request, download_url, form_data, timeout_ms)
    record["browser_fetch_download"] = await post_pdf_browser_fetch(context, download_url, form_data, timeout_ms)
    return record


async def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"searchthema_pdf_playwright_diagnostics_{utc_stamp()}.json"
    sample_items = load_sample_items(args)
    if not sample_items:
        raise SystemExit("No sample item paths found.")

    report: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "theme_url": THEME_URL,
        "sample_count": len(sample_items),
        "items": [],
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        context = await browser.new_context(
            ignore_https_errors=True,
            user_agent=USER_AGENT,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        report["prime"] = await prime_context(context)
        report["cookies"] = [
            {
                "name": cookie.get("name"),
                "domain": cookie.get("domain"),
                "path": cookie.get("path"),
                "value": mask_cookie_value(str(cookie.get("value") or "")),
                "httpOnly": cookie.get("httpOnly"),
                "sameSite": cookie.get("sameSite"),
            }
            for cookie in await context.cookies(BASE_URL)
        ]
        for item_path, manifest_row in sample_items:
            report["items"].append(await diagnose_item(context, item_path, args.timeout_ms, manifest_row))
            await asyncio.sleep(0.2)
        await browser.close()

    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)
    print("id date direct_api_status direct_api_bytes direct_api_pdf viewer_status api_status api_bytes api_pdf browser_status browser_bytes browser_pdf")
    for item in report["items"]:
        direct_api = item.get("metadata_direct_api_download") or {}
        direct_pdf = direct_api.get("pdf") or {}
        api = item.get("api_request_download") or {}
        browser_fetch = item.get("browser_fetch_download") or {}
        api_pdf = api.get("pdf") or {}
        browser_pdf = browser_fetch.get("pdf") or {}
        print(
            item.get("id"),
            item.get("date"),
            direct_api.get("status"),
            direct_pdf.get("bytes"),
            direct_pdf.get("starts_pdf") and direct_pdf.get("has_eof"),
            (item.get("viewer") or {}).get("status"),
            api.get("status"),
            api_pdf.get("bytes"),
            api_pdf.get("starts_pdf") and api_pdf.get("has_eof"),
            browser_fetch.get("status"),
            browser_pdf.get("bytes"),
            browser_pdf.get("starts_pdf") and browser_pdf.get("has_eof"),
        )


if __name__ == "__main__":
    asyncio.run(main())
