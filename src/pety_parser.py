"""petyList HTML parser utilities."""

from __future__ import annotations

import ast
import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


@dataclass
class PetyListPage:
    """Parsed petyListAjax response."""

    total_count: int
    total_pages: int
    items: List[Dict[str, Any]]


def parse_pety_list_page(response_html: str, list_url: str) -> PetyListPage:
    """Parse a petyListAjax HTML response into structured items."""
    soup = BeautifulSoup(response_html or "", "html.parser")
    total_count = _parse_total_count(soup)
    page_numbers = [
        int(match.group(1))
        for match in re.finditer(r"fnGoPage\('(\d+)'\)", response_html or "")
    ]
    total_pages = max(page_numbers) if page_numbers else (1 if total_count else 0)

    items: List[Dict[str, Any]] = []
    for row in soup.select("#tableArea tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        link = row.find("a", onclick=re.compile(r"fnDetail\("))
        if not link:
            continue

        args = parse_fn_detail_args(link.get("onclick", ""))
        if len(args) < 10:
            continue

        viewer_path = html.unescape(args[7]).strip()
        content_id = _query_value(viewer_path, "contentId")
        toc_id = _query_value(viewer_path, "tocId") or args[0].strip()
        date = normalize_date(args[2] or cells[4].get_text(strip=True))
        item_id = toc_id or content_id
        if not item_id:
            continue

        item = {
            "id": item_id,
            "theme": "pety",
            "title": args[1].strip() or link.get_text(strip=True),
            "date": date,
            "book_name": args[3].strip(),
            "category": args[4].strip() or cells[0].get_text(strip=True),
            "agency": args[5].strip() or cells[2].get_text(strip=True),
            "law": args[6].strip() or cells[3].get_text(strip=True),
            "correction_yn": args[8].strip(),
            "revision_reason": args[9].strip(),
            "content_id": content_id,
            "toc_id": toc_id,
            "viewer_path": viewer_path,
            "source_url": list_url,
            "status": "discovered",
            "source": "petyListAjax",
            "discovered_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "pdf": {
                "status": "pending",
                "path": "",
                "size_bytes": 0,
                "sha256": "",
                "downloaded_at": "",
            },
            "ocr": {
                "status": "pending",
                "ready_dir": "",
                "extracted_metadata": {},
            },
        }
        items.append(item)

    return PetyListPage(total_count=total_count, total_pages=total_pages, items=items)


def parse_fn_detail_args(onclick: str) -> List[str]:
    """Parse string arguments from a fnDetail(...) onclick handler."""
    call = _extract_call(onclick, "fnDetail")
    if not call:
        return []

    call = html.unescape(call)
    try:
        parsed = ast.literal_eval(f"({call},)")
        return ["" if value is None else str(value) for value in parsed]
    except (SyntaxError, ValueError):
        return _split_js_string_args(call)


def normalize_date(date_text: str) -> str:
    """Normalize known Korean government date formats to YYYY-MM-DD."""
    text = (date_text or "").strip()
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text.replace(".", "-").replace("/", "-")


def _parse_total_count(soup: BeautifulSoup) -> int:
    count_text = soup.select_one("#countArea")
    if not count_text:
        return 0
    match = re.search(r"([\d,]+)\s*건", count_text.get_text(" ", strip=True))
    if not match:
        return 0
    return int(match.group(1).replace(",", ""))


def _query_value(url: str, key: str) -> str:
    query = parse_qs(urlparse(html.unescape(url)).query)
    values = query.get(key, [])
    return values[0] if values else ""


def _extract_call(source: str, function_name: str) -> str:
    marker = f"{function_name}("
    start = source.find(marker)
    if start < 0:
        return ""

    index = start + len(marker)
    depth = 1
    quote: Optional[str] = None
    escaped = False
    chars: List[str] = []

    while index < len(source):
        char = source[index]
        index += 1

        if quote:
            chars.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue

        if char in ("'", '"'):
            quote = char
            chars.append(char)
            continue
        if char == "(":
            depth += 1
            chars.append(char)
            continue
        if char == ")":
            depth -= 1
            if depth == 0:
                return "".join(chars)
            chars.append(char)
            continue
        chars.append(char)

    return ""


def _split_js_string_args(args: str) -> List[str]:
    values: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    escaped = False

    for char in args:
        if quote:
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            else:
                current.append(char)
            continue

        if char in ("'", '"'):
            quote = char
            continue
        if char == ",":
            values.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    values.append("".join(current).strip())
    return [html.unescape(value) for value in values]
