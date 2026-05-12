#!/usr/bin/env python3
"""Validate classified PDF artifacts and inspect PyPDF2 text extractability."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pdf_text_metadata import analyze_pdf_text


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def compact_text_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key != "sample_text"}


def basic_pdf_integrity(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "size_bytes": 0,
        "header_ok": False,
        "eof_ok": False,
        "ok": False,
    }
    try:
        size = path.stat().st_size
        result["exists"] = True
        result["size_bytes"] = size
        if size <= 0:
            result["error"] = "empty file"
            return result
        with path.open("rb") as handle:
            header = handle.read(8)
            result["header"] = header.decode("ascii", errors="replace")
            result["header_ok"] = header.startswith(b"%PDF-")
            tail_size = min(size, 4096)
            handle.seek(-tail_size, os.SEEK_END)
            result["eof_ok"] = b"%%EOF" in handle.read(tail_size)
        result["ok"] = bool(result["header_ok"] and result["eof_ok"])
        if not result["ok"]:
            result["error"] = "invalid pdf header or EOF marker"
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        row["_line_number"] = line_number
        rows.append(row)
    return rows


def update_item_metadata(item_path: Path, integrity: dict[str, Any], text_metadata: dict[str, Any]) -> bool:
    try:
        item = json.loads(item_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(item, dict):
        return False

    item["pdf_integrity"] = {
        "checked_at": iso_now(),
        **{key: integrity.get(key) for key in ("path", "size_bytes", "header_ok", "eof_ok", "ok", "error") if key in integrity},
    }
    item["pdf_text"] = compact_text_metadata(text_metadata)
    item["updated_at"] = iso_now()
    write_json(item_path, item)
    return True


def validate(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path.cwd().resolve()
    manifest = resolve_path(args.classified_manifest, repo_root)
    output_dir = resolve_path(args.output_dir, repo_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    report_jsonl = output_dir / f"classified_pdf_integrity_text_{utc_stamp()}.jsonl"
    report_summary = report_jsonl.with_suffix(".summary.json")
    report_jsonl.write_text("", encoding="utf-8")

    rows = load_rows(manifest)
    counts: Counter[str] = Counter()
    by_primary_class: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = {
        "missing_pdf": [],
        "invalid_pdf": [],
        "text_error": [],
        "text_extractable": [],
        "image_or_unextractable": [],
    }

    for index, row in enumerate(rows, start=1):
        counts["total"] += 1
        pdf_path_text = str(row.get("issue_pdf_path") or row.get("content_pdf_expected_path") or "")
        pdf_path = resolve_path(pdf_path_text, repo_root) if pdf_path_text else Path("")
        item_path = resolve_path(str(row.get("item_path") or ""), repo_root)

        integrity = basic_pdf_integrity(pdf_path) if pdf_path_text else {"ok": False, "error": "missing pdf path"}
        if integrity.get("exists"):
            counts["exists"] += 1
        else:
            counts["missing"] += 1
        if integrity.get("header_ok"):
            counts["header_ok"] += 1
        if integrity.get("eof_ok"):
            counts["eof_ok"] += 1
        if integrity.get("ok"):
            counts["integrity_ok"] += 1
        else:
            counts["integrity_failed"] += 1

        text_metadata: dict[str, Any]
        if integrity.get("ok"):
            text_metadata = analyze_pdf_text(
                pdf_path,
                include_sample=args.include_sample,
                sample_chars=args.sample_chars,
                include_sha256=args.include_sha256,
                max_pages=args.max_pages,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            text_metadata = {
                "path": str(pdf_path),
                "status": "error",
                "error": integrity.get("error") or "pdf integrity failed",
                "text_extractable": False,
                "generated_at": iso_now(),
            }

        text_metadata["scope"] = "issue" if row.get("issue_pdf_path") else "content"
        text_metadata["content_pdf_primary_class"] = row.get("primary_class")

        if text_metadata.get("status") == "error":
            counts["text_errors"] += 1
            if len(samples["text_error"]) < args.sample_limit:
                samples["text_error"].append({"id": row.get("id"), "path": str(pdf_path), "error": text_metadata.get("error")})
        elif text_metadata.get("text_extractable"):
            counts["text_extractable"] += 1
            if len(samples["text_extractable"]) < args.sample_limit:
                samples["text_extractable"].append({"id": row.get("id"), "path": str(pdf_path), "chars": text_metadata.get("total_chars")})
        else:
            counts["image_or_unextractable"] += 1
            if len(samples["image_or_unextractable"]) < args.sample_limit:
                samples["image_or_unextractable"].append({"id": row.get("id"), "path": str(pdf_path)})

        if not integrity.get("exists") and len(samples["missing_pdf"]) < args.sample_limit:
            samples["missing_pdf"].append({"id": row.get("id"), "path": str(pdf_path)})
        elif not integrity.get("ok") and len(samples["invalid_pdf"]) < args.sample_limit:
            samples["invalid_pdf"].append({"id": row.get("id"), "path": str(pdf_path), "error": integrity.get("error")})

        if args.update_items and item_path.exists():
            if update_item_metadata(item_path, integrity, text_metadata):
                counts["updated_items"] += 1

        by_primary_class[str(row.get("primary_class") or "unknown")] += 1
        append_jsonl(
            report_jsonl,
            {
                "id": row.get("id"),
                "item_path": row.get("item_path"),
                "pdf_path": str(pdf_path),
                "primary_class": row.get("primary_class"),
                "management_class": row.get("management_class"),
                "integrity": integrity,
                "text": compact_text_metadata(text_metadata),
            },
        )
        if args.progress_every and (index % args.progress_every == 0 or index == len(rows)):
            print(
                f"progress checked={index}/{len(rows)} integrity_ok={counts['integrity_ok']} "
                f"text_extractable={counts['text_extractable']} text_errors={counts['text_errors']}",
                flush=True,
            )

    summary = {
        "created_at": iso_now(),
        "classified_manifest": str(manifest.relative_to(repo_root)),
        "report_jsonl": str(report_jsonl.relative_to(repo_root)),
        "counts": dict(counts),
        "by_primary_class": dict(sorted(by_primary_class.items())),
        "samples": samples,
        "settings": {
            "include_sha256": args.include_sha256,
            "include_sample": args.include_sample,
            "max_pages": args.max_pages,
            "timeout_seconds": args.timeout_seconds,
            "update_items": args.update_items,
        },
    }
    write_json(report_summary, summary)
    print(str(report_summary.relative_to(repo_root)))
    print(json.dumps(summary["counts"], ensure_ascii=False, sort_keys=True), flush=True)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate classified PDF integrity and text extractability.")
    parser.add_argument("--classified-manifest", required=True)
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--update-items", action="store_true")
    parser.add_argument("--include-sample", action="store_true")
    parser.add_argument("--sample-chars", type=int, default=1000)
    parser.add_argument("--include-sha256", action="store_true")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--progress-every", type=int, default=5)
    parser.add_argument("--sample-limit", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    validate(parse_args())


if __name__ == "__main__":
    main()
