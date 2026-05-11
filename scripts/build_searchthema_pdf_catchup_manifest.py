#!/usr/bin/env python3
"""Build a SearchThema PDF catchup manifest from all metadata items."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_filename(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", str(value)).strip("._") or "unknown"


def repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path.resolve())


def resolve_path(path_text: str, repo_root: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


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


def item_date_parts(item: dict[str, Any]) -> tuple[str, str]:
    date = str(item.get("date") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return date[:4], date.replace("-", "")
    keyword_date = str(item.get("keyword_field_regdate") or "").strip()
    if re.match(r"^\d{8}$", keyword_date):
        return keyword_date[:4], keyword_date
    year = str(item.get("stored_field_year") or "")[:4]
    if re.match(r"^\d{4}$", year):
        month = str(item.get("stored_field_month") or "").zfill(2)
        day = str(item.get("stored_field_day") or "").zfill(2)
        if re.match(r"^\d{2}$", month) and re.match(r"^\d{2}$", day):
            return year, f"{year}{month}{day}"
        return year, "unknown"
    return "unknown", "unknown"


def first_query_value(url_text: str, key: str) -> str:
    if not url_text:
        return ""
    values = parse_qs(urlparse(url_text).query).get(key) or []
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


def build_pdf_index(pdf_dir: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    if not pdf_dir.exists():
        return index
    for path in pdf_dir.rglob("*.pdf"):
        if path.name.endswith(".pdf.tmp"):
            continue
        index[path.name].append(path.resolve())
    return index


def expected_pdf_path(artifacts_root: Path, item: dict[str, Any]) -> Path:
    year, date_key = item_date_parts(item)
    item_id = safe_filename(str(item.get("id") or toc_id_for_item(item) or ""))
    return artifacts_root / "searchThema" / "pdfs" / year / date_key / f"{item_id}.pdf"


def candidate_paths(
    item: dict[str, Any],
    item_path: Path,
    repo_root: Path,
    artifacts_root: Path,
    pdf_index: dict[str, list[Path]],
) -> list[Path]:
    candidates: list[Path] = []
    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    path_text = str((pdf or {}).get("path") or "").strip()
    if path_text:
        candidates.append(resolve_path(path_text, repo_root))

    candidates.append(expected_pdf_path(artifacts_root, item).resolve())

    names: list[str] = []
    if path_text:
        names.append(Path(path_text).name)
    names.append(f"{safe_filename(str(item.get('id') or item_path.stem))}.pdf")
    toc_id = toc_id_for_item(item)
    if toc_id:
        names.append(f"{safe_filename(toc_id)}.pdf")

    for name in names:
        candidates.extend(pdf_index.get(name, []))

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        text = str(candidate)
        if text not in seen:
            seen.add(text)
            unique.append(candidate)
    return unique


def reason_for_missing(candidates: list[Path]) -> str:
    for path in candidates:
        try:
            if path.exists() and not pdf_complete(path):
                return "invalid_pdf"
        except OSError:
            continue
    return "missing_pdf"


def iter_item_paths(items_dir: Path) -> list[Path]:
    if not items_dir.exists():
        return []
    return sorted(items_dir.rglob("*.json"))


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path.cwd().resolve()
    artifacts_root = resolve_path(args.artifacts_root, repo_root)
    items_dir = resolve_path(args.items_dir, repo_root)
    manifest_path = resolve_path(args.manifest, repo_root)
    summary_path = resolve_path(args.summary, repo_root) if args.summary else manifest_path.with_suffix(".summary.json")

    pdf_index = build_pdf_index(artifacts_root / "searchThema" / "pdfs")
    item_paths = iter_item_paths(items_dir)

    counts: Counter[str] = Counter()
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    manifest_rows: list[dict[str, Any]] = []

    for item_path in item_paths:
        counts["metadata_items"] += 1
        try:
            item = json.loads(item_path.read_text(encoding="utf-8"))
        except Exception as exc:
            counts["invalid_json"] += 1
            if len(samples["invalid_json"]) < args.sample_limit:
                samples["invalid_json"].append({"item_path": repo_relative(item_path, repo_root), "error": str(exc)})
            continue
        if not isinstance(item, dict):
            counts["invalid_json"] += 1
            continue

        year, _date_key = item_date_parts(item)
        toc_id = toc_id_for_item(item)
        if toc_id:
            counts["with_toc_id"] += 1
        else:
            counts["missing_toc_id"] += 1

        candidates = candidate_paths(item, item_path, repo_root, artifacts_root, pdf_index)
        valid_candidates = [path for path in candidates if pdf_complete(path)]
        if valid_candidates:
            counts["complete_pdf"] += 1
            by_year[year]["complete_pdf"] += 1
            continue

        reason = reason_for_missing(candidates)
        counts[reason] += 1
        by_year[year][reason] += 1

        row = {
            "item_path": repo_relative(item_path, repo_root),
            "id": str(item.get("id") or item_path.stem),
            "date": item.get("date"),
            "reason": reason,
        }
        if toc_id:
            row["toc_id"] = toc_id
        content_id = content_id_for_item(item)
        if content_id:
            row["content_id"] = content_id
        if not toc_id and not item.get("viewer_path") and not item.get("stored_field_url"):
            row["reason"] = "missing_download_key"
            counts["missing_download_key"] += 1
            counts[reason] -= 1
            by_year[year]["missing_download_key"] += 1
            by_year[year][reason] -= 1

        manifest_rows.append(row)
        counts["manifest_rows"] += 1
        if len(samples[row["reason"]]) < args.sample_limit:
            samples[row["reason"]].append(row)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_manifest = manifest_path.with_suffix(f"{manifest_path.suffix}.{os.getpid()}.tmp")
    with tmp_manifest.open("w", encoding="utf-8") as handle:
        for row in manifest_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp_manifest.replace(manifest_path)

    report = {
        "created_at": iso_now(),
        "metadata_items_dir": repo_relative(items_dir, repo_root),
        "manifest": repo_relative(manifest_path, repo_root),
        "pdf_index_names": len(pdf_index),
        "summary": {key: value for key, value in sorted(counts.items()) if value},
        "by_year": {year: dict(counter) for year, counter in sorted(by_year.items())},
        "samples": samples,
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_summary = summary_path.with_suffix(f"{summary_path.suffix}.{os.getpid()}.tmp")
    tmp_summary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_summary.replace(summary_path)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_stamp = utc_stamp()
    parser = argparse.ArgumentParser(description="Build a SearchThema PDF catchup manifest from all metadata.")
    parser.add_argument("--artifacts-root", default="artifacts")
    parser.add_argument("--items-dir", default="artifacts/searchThema/metadata/items")
    parser.add_argument(
        "--manifest",
        default=f"artifacts/searchThema/state/pdf_catchup_full_metadata_tocid_{default_stamp}.jsonl",
    )
    parser.add_argument("--summary", default="")
    parser.add_argument("--sample-limit", type=int, default=25)
    return parser.parse_args(argv)


def main() -> int:
    report = build_manifest(parse_args())
    print(f"manifest={report['manifest']}")
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
