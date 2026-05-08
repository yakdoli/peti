#!/usr/bin/env python3
"""Record real SearchRestApi.jsp responses into JSON fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import requests


BASE_URL = "https://gwanbo.go.kr"
SEARCH_URL = f"{BASE_URL}/SearchRestApi.jsp"
THEME_BASE_INFO_URL = f"{BASE_URL}/user/search/getThemeBaseInfo.do"
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; fixture-recorder/1.0)",
    "Accept": "application/json, text/plain, */*",
}


def post_json(url: str, data: dict) -> requests.Response:
    return requests.post(url, data=data, timeout=30, headers=HEADERS)


def save_fixture(path: Path, response: requests.Response) -> None:
    response.raise_for_status()
    json.loads(response.content.decode("utf-8"))
    path.write_bytes(response.content)


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    theme_base_info = post_json(THEME_BASE_INFO_URL, {"tabType": 1})
    save_fixture(FIXTURES_DIR / "get_theme_base_info.json", theme_base_info)
    theme_query = theme_base_info.json()["themeQuery"]

    single_search = post_json(
        SEARCH_URL,
        {
            "mode": "theme",
            "index": "gwanbo",
            "query": "unstored_field_subject:(정부공직자윤리위원회) AND keyword_category_order:(@@ORDER_NUM)",
            "pageNo": 1,
            "listSize": 5,
            "sort": "",
        },
    )
    save_fixture(FIXTURES_DIR / "search_thema_single_page.json", single_search)

    empty_search = post_json(
        SEARCH_URL,
        {
            "mode": "theme",
            "index": "gwanbo",
            "query": "unstored_field_subject:(낙타오렌지우주선) AND keyword_category_order:(@@ORDER_NUM)",
            "pageNo": 1,
            "listSize": 5,
            "sort": "",
        },
    )
    save_fixture(FIXTURES_DIR / "search_thema_empty.json", empty_search)

    multi_search = post_json(
        SEARCH_URL,
        {
            "mode": "theme",
            "index": "gwanbo",
            "query": f"({theme_query}) AND keyword_category_order:(@@ORDER_NUM)",
            "pageNo": 1,
            "listSize": 20,
            "sort": "",
        },
    )
    save_fixture(FIXTURES_DIR / "search_thema_multi_category.json", multi_search)

    print("Recorded fixtures in", FIXTURES_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
