#!/usr/bin/env python3
"""Diagnose SearchThema metadata API behavior with Playwright."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import APIRequestContext, Page, async_playwright


BASE_URL = "https://gwanbo.go.kr"
PAGE_URL = f"{BASE_URL}/user/search/searchThema.do?tabType=1"
API_URL = f"{BASE_URL}/SearchRestApi.jsp"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.7727.15 Safari/537.36"
)
INSTITUTION_QUERY_MAP = {
    "정부공직자윤리위원회": "정부공직자",
    "대법원공직자윤리위원회": "대법원",
    "중앙선거관리위원회공직자윤리위원회": "중앙선거관리위원회",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use Playwright to inspect SearchThema page/API metadata collection behavior."
    )
    parser.add_argument("--year", default="2002", help="Year to probe.")
    parser.add_argument(
        "--institution",
        action="append",
        default=[],
        help="Institution to probe. Can be repeated. Defaults to ALL only.",
    )
    parser.add_argument("--page-sizes", nargs="+", type=int, default=[10, 100, 200, 500, 1000])
    parser.add_argument("--pages", nargs="+", type=int, default=[1, 10, 50])
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_query(year: str, institution: str | None) -> str:
    order_filter = "keyword_category_order:(@@ORDER_NUM)"
    if institution is None:
        return f"unstored_field_subject:({year}) AND {order_filter}"
    institution_query = INSTITUTION_QUERY_MAP.get(institution, institution)
    return f"unstored_field_subject:({year} AND {institution_query}) AND {order_filter}"


def build_form(year: str, institution: str | None, page_no: int, list_size: int) -> dict[str, str]:
    form = {
        "mode": "theme",
        "index": "gwanbo",
        "query": build_query(year, institution),
        "pageNo": str(page_no),
        "listSize": str(list_size),
        "tab_Year1": str(year),
    }
    if institution is not None:
        form["GOV_1"] = institution
    return form


def summarize_json(json_data: dict[str, Any]) -> dict[str, Any]:
    groups = []
    for entry in json_data.get("data") or []:
        items = entry.get("list") if isinstance(entry.get("list"), list) else []
        first = items[0] if items else {}
        last = items[-1] if items else {}
        groups.append(
            {
                "category_name": entry.get("category_name") or "",
                "category_order": entry.get("category_order") or "",
                "count": int(entry.get("count") or 0),
                "list_len": len(items),
                "first_id": first.get("stored_toc_seq") or "",
                "last_id": last.get("stored_toc_seq") or "",
            }
        )
    return {
        "error": json_data.get("error") or None,
        "data_groups": len(groups),
        "returned_items": sum(group["list_len"] for group in groups),
        "total_count_sum": sum(group["count"] for group in groups),
        "max_group_count": max((group["count"] for group in groups), default=0),
        "groups": groups,
    }


async def post_api(
    request: APIRequestContext,
    *,
    year: str,
    institution: str | None,
    page_no: int,
    list_size: int,
    timeout_ms: int,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    form = build_form(year, institution, page_no, list_size)
    record: dict[str, Any] = {
        "year": year,
        "institution": institution or "ALL",
        "page_no": page_no,
        "list_size": list_size,
        "form": form,
    }
    try:
        response = await request.post(
            API_URL,
            form=form,
            timeout=timeout_ms,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": BASE_URL,
                "Referer": PAGE_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        text = await response.text()
        record.update(
            {
                "status": response.status,
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                "content_type": response.headers.get("content-type", ""),
                "body_bytes": len(text.encode("utf-8", errors="replace")),
                "body_prefix": " ".join(text[:240].split()),
            }
        )
        try:
            record["summary"] = summarize_json(json.loads(text))
            record["ok"] = response.status == 200 and not record["summary"].get("error")
        except Exception as exc:  # noqa: BLE001 - diagnostics should capture parser details.
            record["ok"] = False
            record["parse_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - diagnostics should keep going.
        record.update(
            {
                "ok": False,
                "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                "error": str(exc),
            }
        )
    return record


async def inspect_page(page: Page, timeout_ms: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    network: list[dict[str, Any]] = []
    started_by_url: dict[str, datetime] = {}

    page.on("request", lambda request: started_by_url.__setitem__(request.url, datetime.now(timezone.utc)))

    async def handle_response(response: Any) -> None:
        if not any(token in response.url for token in ("SearchRestApi", "searchThema", "getThemeBaseInfo")):
            return
        started = started_by_url.get(response.url)
        record = {
            "url": response.url,
            "status": response.status,
            "method": response.request.method,
            "content_type": response.headers.get("content-type", ""),
            "duration_ms": (
                int((datetime.now(timezone.utc) - started).total_seconds() * 1000) if started else None
            ),
        }
        try:
            text = await response.text()
            record["body_bytes"] = len(text.encode("utf-8", errors="replace"))
            record["body_prefix"] = " ".join(text[:200].split())
        except Exception as exc:  # noqa: BLE001
            record["body_error"] = str(exc)
        network.append(record)

    page.on("response", lambda response: asyncio.create_task(handle_response(response)))

    started = datetime.now(timezone.utc)
    page_info: dict[str, Any] = {}
    try:
        response = await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(3000)
        page_info = {
            "status": response.status if response else None,
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "url": page.url,
            "title": await page.title(),
        }
    except Exception as exc:  # noqa: BLE001
        page_info = {
            "error": str(exc),
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "url": page.url,
        }
    return page_info, network


async def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"searchthema_playwright_diagnostics_{utc_stamp()}.json"

    report: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "page_url": PAGE_URL,
        "api_url": API_URL,
        "year": str(args.year),
        "page_sizes": args.page_sizes,
        "pages": args.pages,
        "api_tests": [],
        "page_network": [],
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        context = await browser.new_context(
            ignore_https_errors=True,
            user_agent=USER_AGENT,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        page = await context.new_page()
        page_info, page_network = await inspect_page(page, args.timeout_ms)
        report["page_goto"] = page_info
        report["page_network"] = page_network
        report["cookies"] = [
            {
                "name": cookie.get("name"),
                "domain": cookie.get("domain"),
                "path": cookie.get("path"),
                "httpOnly": cookie.get("httpOnly"),
                "sameSite": cookie.get("sameSite"),
            }
            for cookie in await context.cookies()
        ]

        institutions: list[str | None] = [None]
        institutions.extend(args.institution)
        for institution in institutions:
            for list_size in args.page_sizes:
                for page_no in args.pages:
                    report["api_tests"].append(
                        await post_api(
                            context.request,
                            year=str(args.year),
                            institution=institution,
                            page_no=page_no,
                            list_size=list_size,
                            timeout_ms=args.timeout_ms,
                        )
                    )
                    await page.wait_for_timeout(200)
        await browser.close()

    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)
    print("list_size page institution status ms returned count max_group bytes ok")
    for row in report["api_tests"]:
        summary = row.get("summary") or {}
        print(
            row["list_size"],
            row["page_no"],
            row["institution"],
            row.get("status"),
            row.get("duration_ms"),
            summary.get("returned_items"),
            summary.get("total_count_sum"),
            summary.get("max_group_count"),
            row.get("body_bytes"),
            row.get("ok"),
        )


if __name__ == "__main__":
    asyncio.run(main())
