#!/usr/bin/env python3
"""Repair metadata PDF references and write a remaining SearchThema manifest."""

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


SOURCES = ("pety", "searchThema")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_filename(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", str(value)).strip("._") or "unknown"


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


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def item_date_key(item: dict[str, Any]) -> str:
    date = str(item.get("date") or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return date.replace("-", "")
    return ""


def expected_pdf_path(artifacts_root: Path, source: str, item: dict[str, Any]) -> Path:
    date = str(item.get("date") or "")
    year = date[:4] if re.match(r"^\d{4}", date) else "unknown"
    date_key = date.replace("-", "") if re.match(r"^\d{4}-\d{2}-\d{2}$", date) else "unknown"
    item_id = safe_filename(str(item.get("id") or ""))
    return artifacts_root / source / "pdfs" / year / date_key / f"{item_id}.pdf"


def normalize_path(path_text: str, repo_root: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def legacy_pety_path(path_text: str, repo_root: Path) -> Path | None:
    normalized = path_text.replace("\\", "/")
    prefix = "artifacts/pdfs/"
    if not normalized.startswith(prefix):
        return None
    return (repo_root / "artifacts" / "pety" / "pdfs" / normalized[len(prefix):]).resolve()


def build_pdf_index(artifacts_root: Path) -> dict[str, dict[str, list[Path]]]:
    index: dict[str, dict[str, list[Path]]] = {source: defaultdict(list) for source in SOURCES}
    for source in SOURCES:
        pdf_dir = artifacts_root / source / "pdfs"
        if not pdf_dir.exists():
            continue
        for path in pdf_dir.rglob("*.pdf"):
            if path.name.endswith(".pdf.tmp"):
                continue
            index[source][path.name].append(path.resolve())
    return index


def candidate_paths(
    repo_root: Path,
    artifacts_root: Path,
    source: str,
    item: dict[str, Any],
    item_path: Path,
    pdf_index: dict[str, dict[str, list[Path]]],
) -> list[Path]:
    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    path_text = str((pdf or {}).get("path") or "").strip()
    candidates: list[Path] = []
    if path_text:
        candidates.append(normalize_path(path_text, repo_root))
        legacy = legacy_pety_path(path_text, repo_root)
        if legacy:
            candidates.append(legacy)

    candidates.append(expected_pdf_path(artifacts_root, source, item).resolve())

    names = []
    if path_text:
        names.append(Path(path_text).name)
    item_id = safe_filename(str(item.get("id") or item_path.stem))
    names.append(f"{item_id}.pdf")

    for name in names:
        for candidate in pdf_index.get(source, {}).get(name, []):
            candidates.append(candidate)

    seen = set()
    unique = []
    for candidate in candidates:
        text = str(candidate)
        if text not in seen:
            seen.add(text)
            unique.append(candidate)
    return unique


def choose_valid_candidate(
    candidates: list[Path],
    item: dict[str, Any],
) -> Path | None:
    valid = [path for path in candidates if pdf_complete(path)]
    if not valid:
        return None

    date_key = item_date_key(item)
    if date_key:
        dated = [path for path in valid if date_key in path.parts]
        if len(dated) == 1:
            return dated[0]
        if dated:
            valid = dated

    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    expected_size = (pdf or {}).get("size_bytes")
    if isinstance(expected_size, int) and expected_size > 0:
        sized = [path for path in valid if path.stat().st_size == expected_size]
        if len(sized) == 1:
            return sized[0]
        if sized:
            valid = sized

    return sorted(valid, key=lambda path: str(path))[0]


def source_item_paths(artifacts_root: Path, source: str) -> list[Path]:
    items_dir = artifacts_root / source / "metadata" / "items"
    if not items_dir.exists():
        return []
    return sorted(items_dir.rglob("*.json"))


def item_needs_download(source: str, item: dict[str, Any], item_path: Path, reason: str) -> dict[str, Any] | None:
    if source != "searchThema":
        return None
    toc_id = str(item.get("toc_id") or item.get("stored_toc_seq") or "").strip()
    if not toc_id and not item.get("viewer_path") and not item.get("stored_field_url"):
        return None
    row = {
        "item_path": str(item_path),
        "id": str(item.get("id") or item_path.stem),
        "date": item.get("date"),
        "reason": reason,
    }
    if toc_id:
        row["toc_id"] = toc_id
    content_id = str(item.get("content_id") or "").strip()
    if content_id:
        row["content_id"] = content_id
    return row


def repair_item(
    repo_root: Path,
    artifacts_root: Path,
    source: str,
    item_path: Path,
    pdf_index: dict[str, dict[str, list[Path]]],
    apply: bool,
) -> tuple[str, dict[str, Any] | None]:
    try:
        item = json.loads(item_path.read_text(encoding="utf-8"))
    except Exception:
        return "invalid_json", None
    if not isinstance(item, dict):
        return "invalid_json", None

    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    path_text = str((pdf or {}).get("path") or "").strip()
    candidates = candidate_paths(repo_root, artifacts_root, source, item, item_path, pdf_index)
    valid_path = choose_valid_candidate(candidates, item)

    if valid_path:
        desired_path = str(valid_path.relative_to(repo_root)) if valid_path.is_relative_to(repo_root) else str(valid_path)
        desired_size = valid_path.stat().st_size
        already_basic_ok = (
            isinstance(pdf, dict)
            and pdf.get("status") == "completed"
            and path_text == desired_path
            and pdf.get("size_bytes") == desired_size
            and item.get("status") == "completed"
        )
        if already_basic_ok and pdf.get("sha256"):
            return "already_ok", None

        desired_hash = sha256_file(valid_path)
        needs_update = (
            not isinstance(pdf, dict)
            or pdf.get("status") != "completed"
            or path_text != desired_path
            or pdf.get("size_bytes") != desired_size
            or pdf.get("sha256") != desired_hash
            or item.get("status") != "completed"
        )
        if not needs_update:
            return "already_ok", None

        repaired = dict(item)
        repaired["pdf"] = {
            "status": "completed",
            "path": desired_path,
            "size_bytes": desired_size,
            "sha256": desired_hash,
            "downloaded_at": str((pdf or {}).get("downloaded_at") or iso_now()),
        }
        repaired["status"] = "completed"
        repaired["updated_at"] = iso_now()
        if apply:
            write_json(item_path, repaired)
        if path_text.startswith("artifacts/pdfs/") and source == "pety":
            return "repaired_legacy_pety_path", None
        return "repaired_existing_pdf", None

    reason = "missing_pdf"
    current_path = normalize_path(path_text, repo_root) if path_text else None
    if current_path and current_path.exists() and not pdf_complete(current_path):
        reason = "invalid_pdf"

    if isinstance(pdf, dict) and (pdf.get("status") == "completed" or path_text):
        repaired = dict(item)
        old_pdf = dict(pdf)
        old_pdf["status"] = "failed"
        old_pdf["error"] = f"{reason}: integrity repair could not find a valid PDF"
        old_pdf["failed_at"] = iso_now()
        old_pdf["previous_path"] = path_text
        repaired["pdf"] = old_pdf
        repaired["status"] = "download_failed"
        repaired["updated_at"] = iso_now()
        if apply:
            write_json(item_path, repaired)
        item = repaired

    manifest_item = item_needs_download(source, item, item_path, reason)
    return reason, manifest_item


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair artifact item JSON PDF references.")
    parser.add_argument("--artifacts-root", default="artifacts")
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--manifest", default="artifacts/searchThema/state/pdf_repair_manifest.jsonl")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=50)
    args = parser.parse_args()

    repo_root = Path.cwd().resolve()
    artifacts_root = (repo_root / args.artifacts_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = (repo_root / args.manifest).resolve()

    pdf_index = build_pdf_index(artifacts_root)
    summary = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    manifest_rows: list[dict[str, Any]] = []

    for source in SOURCES:
        for item_path in source_item_paths(artifacts_root, source):
            status, manifest_item = repair_item(repo_root, artifacts_root, source, item_path, pdf_index, args.apply)
            summary[f"{source}:{status}"] += 1
            summary["total"] += 1
            if manifest_item:
                manifest_rows.append(manifest_item)
                summary["manifest_rows"] += 1
            if status not in {"already_ok"} and len(samples[status]) < args.sample_limit:
                samples[status].append({"source": source, "path": str(item_path), "manifest": manifest_item})

    if args.apply:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = manifest_path.with_suffix(f"{manifest_path.suffix}.{os.getpid()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for row in manifest_rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        tmp_path.replace(manifest_path)

    report = {
        "started_at": iso_now(),
        "apply": bool(args.apply),
        "summary": dict(summary),
        "manifest": str(manifest_path),
        "samples": samples,
    }
    report_path = output_dir / f"pdf_ref_repair_{utc_stamp()}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"report={report_path}")
    print(f"manifest={manifest_path}")
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
