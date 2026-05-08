"""SearchThema JSON parser utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List


@dataclass
class SearchThemaItem:
    """Parsed SearchThema item."""

    id: str
    title: str
    date: str
    ebook_no: str
    organ_nm: str
    category_name: str
    category_order: str
    viewer_url: str
    file_size: str
    page: str
    keyword: str
    pdf_file_path: str


@dataclass
class SearchThemaPage:
    """Parsed SearchThema category page."""

    total_count: int
    items: List[SearchThemaItem]
    page_list: str
    category_name: str
    category_order: str


def parse_search_thema_response(json_data: Dict[str, Any]) -> List[SearchThemaPage]:
    """Convert SearchThema API JSON into structured pages."""

    pages: List[SearchThemaPage] = []
    for entry in json_data.get("data") or []:
        items = [_parse_item(item) for item in entry.get("list") or []]
        pages.append(
            SearchThemaPage(
                total_count=int(entry.get("count") or 0),
                items=items,
                page_list=entry.get("pageList") or "",
                category_name=entry.get("category_name") or "",
                category_order=entry.get("category_order") or "",
            )
        )
    return pages


def _parse_item(item: Dict[str, Any]) -> SearchThemaItem:
    return SearchThemaItem(
        id=item.get("stored_toc_seq") or "",
        title=item.get("stored_field_subject") or "",
        date=_normalize_date(
            item.get("stored_field_year") or "",
            item.get("stored_field_month") or "",
            item.get("stored_field_day") or "",
            item.get("keyword_field_regdate") or "",
        ),
        ebook_no=item.get("stored_ebook_no") or "",
        organ_nm=item.get("stored_organ_nm") or "",
        category_name=item.get("stored_category_name") or "",
        category_order=item.get("keyword_category_order") or "",
        viewer_url=item.get("stored_field_url") or "",
        file_size=item.get("stored_file_size") or "",
        page=item.get("stored_page") or "",
        keyword=item.get("stored_field_keyword") or "",
        pdf_file_path=item.get("stored_pdf_file_path") or "",
    )


def _normalize_date(year: str, month: str, day: str, fallback: str = "") -> str:
    text = f"{year}{month}{day}"
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        pass
    try:
        if len(fallback) == 8 and fallback.isdigit():
            return datetime.strptime(fallback, "%Y%m%d").strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        pass
    return text or fallback
