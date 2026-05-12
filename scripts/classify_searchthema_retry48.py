#!/usr/bin/env python3
"""Classify and separate the 48 SearchThema PDF retry exceptions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_filename(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", str(value)).strip("._") or "unknown"


def resolve_path(path_text: str, repo_root: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def item_id(item: dict[str, Any], fallback: str) -> str:
    return str(item.get("id") or item.get("toc_id") or item.get("stored_toc_seq") or fallback)


def issue_pdf_path(item: dict[str, Any], repo_root: Path, fallback_id: str) -> Path:
    date = item_date(item)
    year = date[:4] if len(date) >= 4 else "unknown"
    date_key = date.replace("-", "") if len(date) == 10 else "unknown"
    return (
        repo_root
        / "artifacts"
        / "searchThema"
        / "issue_pdfs"
        / year
        / date_key
        / f"{safe_filename(item_id(item, fallback_id))}.pdf"
    )


def content_pdf_path(item: dict[str, Any], repo_root: Path, fallback_id: str) -> Path:
    date = item_date(item)
    year = date[:4] if len(date) >= 4 else "unknown"
    date_key = date.replace("-", "") if len(date) == 10 else "unknown"
    return (
        repo_root
        / "artifacts"
        / "searchThema"
        / "pdfs"
        / year
        / date_key
        / f"{safe_filename(item_id(item, fallback_id))}.pdf"
    )


def partial_pdf_path(item: dict[str, Any], repo_root: Path, fallback_id: str) -> Path:
    date = item_date(item)
    year = date[:4] if len(date) >= 4 else "unknown"
    date_key = date.replace("-", "") if len(date) == 10 else "unknown"
    return (
        repo_root
        / "artifacts"
        / "searchThema"
        / "partial_pdfs"
        / year
        / date_key
        / f"{safe_filename(item_id(item, fallback_id))}.pdf"
    )


def stored_pdf_file_id(item: dict[str, Any]) -> str:
    value = str(item.get("stored_pdf_file_path") or "").strip()
    return Path(value).stem if value else ""


def classify_primary(diagnostic: dict[str, Any] | None) -> str:
    direct = (diagnostic or {}).get("direct_pdf_request") if isinstance(diagnostic, dict) else None
    if not isinstance(direct, dict):
        return "not_diagnosed"
    if direct.get("error"):
        return "content_request_error"

    status = direct.get("status")
    body = direct.get("body") if isinstance(direct.get("body"), dict) else {}
    size = body.get("bytes")
    starts_pdf = bool(body.get("starts_pdf"))
    complete = bool(body.get("pdf_complete"))

    if complete:
        return "content_pdf_complete"
    if status == 500 and size == 73:
        return "content_endpoint_500_minimal_html"
    if status == 500 and size == 651:
        return "content_endpoint_500_error_html"
    if status == 200 and starts_pdf:
        return "content_pdf_incomplete"
    if status == 200 and size == 0:
        return "content_empty_response"
    if status == 200:
        return "content_non_pdf_or_empty_200"
    return f"content_status_{status or 'unknown'}"


def load_manifest(path: Path, repo_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        item_path = resolve_path(str(row.get("item_path") or ""), repo_root)
        row["item_path"] = str(item_path)
        row["_line_number"] = line_number
        rows.append(row)
    return rows


def load_diagnostic(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else []
    return {str(item.get("id") or item.get("toc_id")): item for item in items if isinstance(item, dict)}


def classify(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path.cwd().resolve()
    manifest = resolve_path(args.manifest, repo_root)
    diagnostic = load_diagnostic(resolve_path(args.diagnostic_report, repo_root))
    output_dir = resolve_path(args.output_dir, repo_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = output_dir / f"pdf_retry_48_classified_{utc_stamp()}.jsonl"
    downloaded_path = jsonl_path.with_name(f"{jsonl_path.stem}.issue_downloaded.jsonl")
    pending_path = jsonl_path.with_name(f"{jsonl_path.stem}.issue_pending.jsonl")
    partial_jsonl_path = jsonl_path.with_name(f"{jsonl_path.stem}.partial_content.jsonl")
    summary_path = jsonl_path.with_suffix(".summary.json")
    jsonl_path.write_text("", encoding="utf-8")
    downloaded_path.write_text("", encoding="utf-8")
    pending_path.write_text("", encoding="utf-8")
    partial_jsonl_path.write_text("", encoding="utf-8")

    counts: Counter[str] = Counter()
    by_primary: Counter[str] = Counter()
    by_management: Counter[str] = Counter()
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in load_manifest(manifest, repo_root):
        counts["rows"] += 1
        item_path = Path(str(row["item_path"]))
        item = json.loads(item_path.read_text(encoding="utf-8"))
        record_id = str(row.get("id") or item_id(item, item_path.stem))
        diagnostic_row = diagnostic.get(record_id)
        primary_class = classify_primary(diagnostic_row)

        pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
        pdf_path = resolve_path(str((pdf or {}).get("path") or ""), repo_root) if (pdf or {}).get("path") else None
        expected_issue_path = issue_pdf_path(item, repo_root, record_id)
        expected_content_path = content_pdf_path(item, repo_root, record_id)
        expected_partial_path = partial_pdf_path(item, repo_root, record_id)
        issue_path = expected_issue_path
        moved_issue_pdf = False
        moved_partial_pdf = False
        updated_metadata = False

        current_method = str((pdf or {}).get("method") or "")
        current_scope = str((pdf or {}).get("scope") or "")
        current_is_issue = (
            current_scope == "issue"
            or current_method.startswith("issue_pdf")
            or current_method.endswith("_issue_pdf_fallback")
        )
        if args.apply and current_is_issue and pdf_path and pdf_path.exists() and pdf_path != expected_issue_path:
            expected_issue_path.parent.mkdir(parents=True, exist_ok=True)
            if expected_issue_path.exists() and sha256_file(expected_issue_path) == sha256_file(pdf_path):
                pdf_path.unlink()
            else:
                pdf_path.replace(expected_issue_path)
            pdf_path = expected_issue_path
            moved_issue_pdf = True

        issue_complete = bool(pdf_path and pdf_complete(pdf_path) and current_is_issue)
        if not issue_complete and pdf_complete(expected_issue_path):
            pdf_path = expected_issue_path
            issue_complete = True
            current_is_issue = True

        partial_path = None
        if expected_content_path.exists() and not pdf_complete(expected_content_path):
            partial_path = expected_partial_path
            if args.apply:
                expected_partial_path.parent.mkdir(parents=True, exist_ok=True)
                if expected_partial_path.exists() and sha256_file(expected_partial_path) == sha256_file(expected_content_path):
                    expected_content_path.unlink()
                else:
                    expected_content_path.replace(expected_partial_path)
                moved_partial_pdf = True
        elif expected_partial_path.exists():
            partial_path = expected_partial_path

        if args.apply and issue_complete and pdf_path:
            item.setdefault("pdf", {})
            item["pdf"].update(
                {
                    "status": "completed",
                    "path": str(pdf_path.relative_to(repo_root)),
                    "size_bytes": pdf_path.stat().st_size,
                    "sha256": sha256_file(pdf_path),
                    "scope": "issue",
                    "method": current_method or "issue_pdf_fallback",
                    "content_pdf_status": "unavailable",
                    "content_pdf_primary_class": primary_class,
                    "classified_at": iso_now(),
                }
            )
            if item.get("status") != "completed_issue_fallback":
                item["status"] = "completed_issue_fallback"
            item["updated_at"] = iso_now()
            write_json(item_path, item)
            updated_metadata = True
        elif args.apply and partial_path and partial_path.exists():
            item.setdefault("pdf", {})
            item["pdf"].update(
                {
                    "status": "failed",
                    "path": str(partial_path.relative_to(repo_root)),
                    "partial_path": str(partial_path.relative_to(repo_root)),
                    "size_bytes": partial_path.stat().st_size,
                    "sha256": sha256_file(partial_path),
                    "scope": "content_partial",
                    "content_pdf_status": "incomplete",
                    "content_pdf_primary_class": primary_class,
                    "classified_at": iso_now(),
                }
            )
            item["status"] = "download_failed"
            item["updated_at"] = iso_now()
            write_json(item_path, item)
            updated_metadata = True

        content_id = str(item.get("content_id") or row.get("content_id") or "").strip()
        if issue_complete:
            management_class = "issue_pdf_fallback_downloaded"
        elif content_id:
            management_class = "issue_pdf_fallback_pending"
        else:
            management_class = "manual_review_missing_content_id"

        toc_id = str(item.get("toc_id") or item.get("stored_toc_seq") or row.get("toc_id") or "").strip()
        file_id = stored_pdf_file_id(item)
        date = item_date(item)
        classified = {
            "id": record_id,
            "item_path": str(item_path.relative_to(repo_root)),
            "date": date,
            "year": date[:4] if len(date) >= 4 else "unknown",
            "title": item.get("title") or item.get("stored_field_subject"),
            "stored_category_name": item.get("stored_category_name"),
            "stored_file_size": item.get("stored_file_size"),
            "toc_id": toc_id,
            "content_id": content_id,
            "stored_pdf_file_id": file_id,
            "toc_file_id_mismatch": bool(toc_id and file_id and toc_id != file_id),
            "primary_class": primary_class,
            "management_class": management_class,
            "content_pdf_expected_path": str(expected_content_path.relative_to(repo_root)),
            "issue_pdf_expected_path": str(expected_issue_path.relative_to(repo_root)),
            "issue_pdf_path": str(pdf_path.relative_to(repo_root)) if issue_complete and pdf_path else "",
            "issue_pdf_complete": issue_complete,
            "partial_pdf_path": str(partial_path.relative_to(repo_root)) if partial_path and partial_path.exists() else "",
            "moved_issue_pdf": moved_issue_pdf,
            "moved_partial_pdf": moved_partial_pdf,
            "metadata_updated": updated_metadata,
        }
        append_jsonl(jsonl_path, classified)
        if management_class == "issue_pdf_fallback_downloaded":
            append_jsonl(downloaded_path, classified)
        elif management_class == "issue_pdf_fallback_pending":
            append_jsonl(pending_path, classified)
        if classified["partial_pdf_path"]:
            append_jsonl(partial_jsonl_path, classified)

        by_primary[primary_class] += 1
        by_management[management_class] += 1
        by_year[classified["year"]][management_class] += 1
        if classified["toc_file_id_mismatch"]:
            counts["toc_file_id_mismatch"] += 1
        if len(samples[management_class]) < args.sample_limit:
            samples[management_class].append(classified)

    summary = {
        "created_at": iso_now(),
        "apply": args.apply,
        "manifest": str(manifest.relative_to(repo_root)),
        "diagnostic_report": str(resolve_path(args.diagnostic_report, repo_root).relative_to(repo_root)),
        "classified_jsonl": str(jsonl_path.relative_to(repo_root)),
        "issue_downloaded_jsonl": str(downloaded_path.relative_to(repo_root)),
        "issue_pending_jsonl": str(pending_path.relative_to(repo_root)),
        "partial_content_jsonl": str(partial_jsonl_path.relative_to(repo_root)),
        "counts": dict(counts),
        "by_primary_class": dict(sorted(by_primary.items())),
        "by_management_class": dict(sorted(by_management.items())),
        "by_year": {year: dict(counter) for year, counter in sorted(by_year.items())},
        "samples": samples,
    }
    write_json(summary_path, summary)
    print(str(jsonl_path.relative_to(repo_root)))
    print(json.dumps(summary["by_management_class"], ensure_ascii=False, sort_keys=True))
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify and separate the 48 SearchThema PDF retry rows.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--diagnostic-report", required=True)
    parser.add_argument("--output-dir", default="artifacts/searchThema/state")
    parser.add_argument("--sample-limit", type=int, default=12)
    parser.add_argument("--apply", action="store_true", help="Move issue fallback PDFs and update metadata scope.")
    return parser.parse_args()


def main() -> None:
    classify(parse_args())


if __name__ == "__main__":
    main()
