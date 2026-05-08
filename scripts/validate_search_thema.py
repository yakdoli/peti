#!/usr/bin/env python3
"""Standalone spike validator for SearchRestApi.jsp and viewer PDF flow."""

from __future__ import annotations

import json
import importlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests


BASE_URL = "https://gwanbo.go.kr"
SEARCH_URL = f"{BASE_URL}/SearchRestApi.jsp"
THEME_BASE_INFO_URL = f"{BASE_URL}/user/search/getThemeBaseInfo.do"
RESULTS_PATH = Path(__file__).with_name("spike_results.json")


def post_form(url: str, data: Dict[str, Any]) -> requests.Response:
    return requests.post(
        url,
        data=data,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; spike-validator/1.0)",
            "Accept": "application/json, text/plain, */*",
        },
    )


def safe_json(response: requests.Response) -> Tuple[Optional[Any], Optional[str]]:
    try:
        return response.json(), None
    except Exception as exc:  # pragma: no cover - best-effort spike helper
        return None, f"invalid json: {exc}"


def find_first_item(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    for group in data:
        if not isinstance(group, dict):
            continue
        items = group.get("list")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("stored_field_url"):
                return item
    return None


def find_conditions(payload: Any) -> List[Any]:
    if isinstance(payload, dict):
        conditions = payload.get("conditions")
        if isinstance(conditions, list):
            return conditions
        for value in payload.values():
            found = find_conditions(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_conditions(item)
            if found:
                return found
    return []


def extract_institution_names(conditions: List[Any]) -> List[str]:
    names: List[str] = []
    for condition in conditions:
        if isinstance(condition, dict):
            for key in ("themeTitle", "condition_name", "conditionName", "name", "text", "label"):
                value = condition.get(key)
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
                    break
    return names


def test_search_http() -> Tuple[bool, Dict[str, Any], List[str]]:
    errors: List[str] = []
    results: Dict[str, Any] = {}
    response = post_form(
        SEARCH_URL,
        {
            "mode": "theme",
            "index": "gwanbo",
            "query": "unstored_field_subject:(정부공직자윤리위원회) AND keyword_category_order:(@@ORDER_NUM)",
            "pageNo": 1,
            "listSize": 2,
            "sort": "",
        },
    )
    results["status_code"] = response.status_code
    if response.status_code != 200:
        return False, results, [f"SearchRestApi.jsp HTTP {response.status_code}"]
    payload, err = safe_json(response)
    if err:
        return False, results, [err]
    item = find_first_item(payload)
    if not item:
        return False, results, ["missing data[].list[] item with stored_field_url"]
    results["item"] = item
    results["stored_field_url"] = item.get("stored_field_url")
    results["stored_toc_seq"] = item.get("stored_toc_seq")
    if not item.get("stored_field_url"):
        return False, results, ["stored_field_url missing on first item"]
    if not item.get("stored_toc_seq"):
        return False, results, ["stored_toc_seq missing on first item"]
    return True, results, []


def test_viewer_flow(stored_field_url: str, stored_toc_seq: Any) -> Tuple[bool, Dict[str, Any], List[str]]:
    errors: List[str] = []
    results: Dict[str, Any] = {}
    viewer_url = urljoin(BASE_URL, stored_field_url)
    viewer_response = requests.get(
        viewer_url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (compatible; spike-validator/1.0)"},
    )
    results["viewer_status_code"] = viewer_response.status_code
    if viewer_response.status_code != 200:
        return False, results, [f"viewer HTTP {viewer_response.status_code}"]

    match = re.search(r"(/user/common/ofcttCntntDownload\.do(?:;jsessionid=[A-Za-z0-9_.-]+)?)", viewer_response.text)
    if not match:
        return False, results, ["download URL regex did not match viewer HTML"]
    download_path = match.group(1)
    download_url = urljoin(BASE_URL, download_path)
    results["download_path"] = download_path

    download_response = post_form(download_url, {"cntnt_seq_no": stored_toc_seq})
    results["download_status_code"] = download_response.status_code
    body = download_response.content
    if download_response.status_code != 200:
        return False, results, [f"download POST HTTP {download_response.status_code}"]
    if not body.startswith(b"%PDF-"):
        return False, results, ["download response is not a PDF (%PDF- missing)"]
    results["pdf_bytes"] = len(body)
    return True, results, []


def test_theme_base_info() -> Tuple[bool, Dict[str, Any], List[str]]:
    results: Dict[str, Any] = {}
    response = post_form(THEME_BASE_INFO_URL, {"tabType": 1})
    results["status_code"] = response.status_code
    if response.status_code != 200:
        return False, results, [f"getThemeBaseInfo.do HTTP {response.status_code}"]
    payload, err = safe_json(response)
    if err:
        return False, results, [err]
    conditions = find_conditions(payload)
    results["conditions_len"] = len(conditions)
    if len(conditions) != 3:
        return False, results, [f"conditions length was {len(conditions)} instead of 3"]
    results["institutions"] = extract_institution_names(conditions)
    return True, results, []


def test_search_http_via_playwright() -> Tuple[bool, List[str]]:
    try:
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except Exception as exc:  # pragma: no cover - optional fallback
        return False, [f"playwright unavailable: {exc}"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        response = page.request.post(
            SEARCH_URL,
            form={
                "mode": "theme",
                "index": "gwanbo",
                "query": "unstored_field_subject:(정부공직자윤리위원회) AND keyword_category_order:(@@ORDER_NUM)",
                "pageNo": 1,
                "listSize": 2,
                "sort": "",
            },
            timeout=30000,
        )
        ok = response.ok
        body = response.text()
        context.close()
        browser.close()
        if not ok:
            return False, [f"playwright HTTP {response.status}"]
        try:
            payload = json.loads(body)
        except Exception as exc:
            return False, [f"playwright invalid json: {exc}"]
        item = find_first_item(payload)
        if not item:
            return False, ["playwright fallback did not find stored_field_url item"]
        return True, []


def main() -> int:
    report: Dict[str, Any] = {
        "http_api_works": False,
        "viewer_flow_works": False,
        "pdf_download_works": False,
        "institutions": [],
        "needs_playwright": False,
        "error_messages": [],
    }

    http_ok, http_details, http_errors = test_search_http()
    report["http_api_works"] = http_ok
    report["http_details"] = http_details
    report["error_messages"].extend(http_errors)

    stored_field_url = http_details.get("stored_field_url") if http_ok else None
    stored_toc_seq = http_details.get("stored_toc_seq") if http_ok else None

    if http_ok and stored_field_url is not None and stored_toc_seq is not None:
        viewer_ok, viewer_details, viewer_errors = test_viewer_flow(stored_field_url, stored_toc_seq)
        report["viewer_flow_works"] = viewer_ok
        report["viewer_details"] = viewer_details
        report["pdf_download_works"] = viewer_ok
        report["error_messages"].extend(viewer_errors)
    else:
        report["error_messages"].append("skipped viewer/pdf flow because HTTP search failed")

    base_ok, base_details, base_errors = test_theme_base_info()
    report["institutions"] = base_details.get("institutions", [])
    report["theme_base_info_works"] = base_ok
    report["theme_base_info_details"] = base_details
    report["error_messages"].extend(base_errors)

    if not http_ok or not report["viewer_flow_works"] or not report["theme_base_info_works"]:
        report["needs_playwright"] = True
        fallback_ok, fallback_errors = test_search_http_via_playwright()
        report["playwright_fallback_http_works"] = fallback_ok
        report["error_messages"].extend(fallback_errors)

    RESULTS_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["http_api_works"] and report["viewer_flow_works"] and report["theme_base_info_works"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
