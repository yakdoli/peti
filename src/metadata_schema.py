"""Shared metadata schema helpers for Gwanbo item JSON records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse


GWANBO_ITEM_SCHEMA_VERSION = "gwanbo.item.v1"
DEFAULT_SOURCE_SYSTEM = "gwanbo"

DEFAULT_PDF_TEXT: Dict[str, Any] = {
    "status": "pending",
    "text_extractable": False,
    "pages": 0,
    "text_pages": 0,
    "total_chars": 0,
}
DEFAULT_PDF_LAYOUT: Dict[str, Any] = {
    "status": "pending",
    "text_extractable": False,
    "layout": {},
    "table_count": 0,
}
DEFAULT_GRAPH: Dict[str, Any] = {
    "status": "pending",
    "nodes": [],
    "edges": [],
}
DEFAULT_EMBEDDING: Dict[str, Any] = {
    "status": "pending",
    "model": "",
    "dimensions": 0,
}


def apply_item_schema(
    item: Dict[str, Any],
    *,
    source_detail: str | None = None,
    source_endpoint: str | None = None,
    source_system: str = DEFAULT_SOURCE_SYSTEM,
) -> Dict[str, Any]:
    """Apply the gwanbo.item.v1 envelope while preserving legacy fields."""
    item.setdefault("schema_version", GWANBO_ITEM_SCHEMA_VERSION)
    if not item.get("source_system"):
        item["source_system"] = source_system

    detail = source_detail or infer_source_detail(item)
    if detail and not item.get("source_detail"):
        item["source_detail"] = detail

    endpoint = source_endpoint or infer_source_endpoint(item, detail)
    if endpoint and not item.get("source_endpoint"):
        item["source_endpoint"] = endpoint
    if endpoint and not item.get("source"):
        item["source"] = endpoint

    merge_dict_default(item, "source_ids", build_source_ids(item))
    merge_dict_default(item, "urls", build_urls(item))
    sync_pdf_text_metadata(item)
    sync_pdf_layout_metadata(item)
    merge_dict_default(item, "graph", deepcopy(DEFAULT_GRAPH))
    merge_dict_default(item, "embedding", deepcopy(DEFAULT_EMBEDDING))
    return item


def sync_pdf_text_metadata(
    item: Dict[str, Any],
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Keep item.pdf_text and item.ocr.extracted_metadata in lockstep."""
    selected = metadata
    if not isinstance(selected, dict):
        existing_pdf_text = item.get("pdf_text")
        if isinstance(existing_pdf_text, dict) and existing_pdf_text:
            selected = existing_pdf_text
        else:
            ocr = item.get("ocr") if isinstance(item.get("ocr"), dict) else {}
            extracted = (ocr or {}).get("extracted_metadata")
            selected = extracted if isinstance(extracted, dict) and extracted else None

    selected = selected or {}
    if selected and "status" not in selected:
        selected = {**selected, "status": "error" if selected.get("error") else "ok"}
    pdf_text = merge_defaults(selected, DEFAULT_PDF_TEXT)
    item["pdf_text"] = deepcopy(pdf_text)

    ocr = item.setdefault("ocr", {})
    if not isinstance(ocr, dict):
        ocr = {}
        item["ocr"] = ocr
    ocr["extracted_metadata"] = deepcopy(pdf_text)
    if pdf_text.get("text_extractable"):
        ocr["status"] = "skipped_text_extractable"
        ocr["skip_reason"] = "text_extractable_pdf"
    else:
        ocr.setdefault("status", "pending")
        ocr["skip_reason"] = ""
    return item["pdf_text"]


def sync_pdf_layout_metadata(
    item: Dict[str, Any],
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Ensure item.pdf_layout has the gwanbo.item.v1 default shape."""
    selected = metadata
    if not isinstance(selected, dict):
        existing = item.get("pdf_layout")
        selected = existing if isinstance(existing, dict) and existing else None
    item["pdf_layout"] = merge_defaults(selected or {}, DEFAULT_PDF_LAYOUT)
    return item["pdf_layout"]


def compact_pdf_layout_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Compact layout sidecar metadata for storage on the item record."""
    compact: Dict[str, Any] = {}
    for key in (
        "status",
        "text_extractable",
        "pages",
        "scanned_pages",
        "pdf_key",
        "pdf_path",
        "resolved_pdf_path",
        "generated_at",
        "error",
    ):
        if key in metadata:
            compact[key] = metadata[key]
    if isinstance(metadata.get("layout"), dict):
        compact["layout"] = metadata["layout"]
    compact["table_count"] = len(metadata.get("tables") or [])
    return compact


def merge_defaults(value: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Return defaults overlaid with value without sharing mutable objects."""
    merged = deepcopy(defaults)
    for key, item_value in value.items():
        if (
            isinstance(item_value, dict)
            and isinstance(merged.get(key), dict)
        ):
            merged[key] = merge_defaults(item_value, merged[key])
        else:
            merged[key] = deepcopy(item_value)
    return merged


def merge_dict_default(item: Dict[str, Any], key: str, defaults: Dict[str, Any]) -> None:
    current = item.get(key)
    if not isinstance(current, dict):
        item[key] = deepcopy(defaults)
        return
    merged = deepcopy(defaults)
    merged.update(current)
    item[key] = {k: v for k, v in merged.items() if not is_blank(v)}


def build_source_ids(item: Dict[str, Any]) -> Dict[str, str]:
    query_values = viewer_query_values(item)
    ids = {
        "id": item.get("id"),
        "toc_id": item.get("toc_id") or item.get("stored_toc_seq") or first_query_value(query_values, "tocId"),
        "content_id": item.get("content_id") or first_query_value(query_values, "contentId"),
        "stored_toc_seq": item.get("stored_toc_seq"),
        "stored_ebook_no": item.get("stored_ebook_no"),
    }
    return {key: str(value) for key, value in ids.items() if not is_blank(value)}


def build_urls(item: Dict[str, Any]) -> Dict[str, str]:
    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    urls = {
        "source": item.get("source_url"),
        "viewer": item.get("url") or item.get("viewer_url"),
        "viewer_path": item.get("viewer_path") or item.get("stored_field_url"),
        "pdf": (pdf or {}).get("url"),
    }
    return {key: str(value) for key, value in urls.items() if not is_blank(value)}


def infer_source_detail(item: Dict[str, Any]) -> str:
    explicit = str(item.get("theme") or item.get("source_detail") or "").strip()
    if explicit:
        return explicit
    source = str(item.get("source") or "").lower()
    if "pety" in source:
        return "pety"
    if "search" in source:
        return "searchThema"
    if item.get("stored_toc_seq") or item.get("stored_field_url"):
        return "searchThema"
    return ""


def infer_source_endpoint(item: Dict[str, Any], source_detail: str | None = None) -> str:
    source = str(item.get("source") or "").strip()
    if source:
        return source
    detail = str(source_detail or "").strip()
    if detail == "searchThema":
        return "SearchRestApi"
    if detail == "pety":
        return "petyListAjax"
    return ""


def viewer_query_values(item: Dict[str, Any]) -> Dict[str, list[str]]:
    viewer = item.get("viewer_path") or item.get("stored_field_url") or item.get("url") or ""
    return parse_qs(urlparse(str(viewer)).query)


def first_query_value(query_values: Dict[str, list[str]], key: str) -> str:
    values = query_values.get(key) or []
    return values[0] if values else ""


def is_blank(value: Any) -> bool:
    return value is None or value == ""
