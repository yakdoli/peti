#!/usr/bin/env python3
"""Validate generated artifact JSON files and PDF files."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sample_add(samples: list[dict[str, Any]], item: dict[str, Any], limit: int) -> None:
    if len(samples) < limit:
        samples.append(item)


def source_from_path(path: Path) -> str:
    parts = path.parts
    if "searchThema" in parts:
        return "searchThema"
    if "pety" in parts:
        return "pety"
    return "unknown"


def normalize_artifact_path(path_text: str, repo_root: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def migrated_pety_pdf_path(path_text: str, repo_root: Path) -> Path | None:
    normalized = path_text.replace("\\", "/")
    prefix = "artifacts/pdfs/"
    if not normalized.startswith(prefix):
        return None
    return (repo_root / "artifacts" / "pety" / "pdfs" / normalized[len(prefix):]).resolve()


def validate_json_file(path_text: str, repo_root_text: str) -> dict[str, Any]:
    path = Path(path_text)
    repo_root = Path(repo_root_text)
    result: dict[str, Any] = {
        "path": path_text,
        "ok": False,
        "source": source_from_path(path),
        "pdf_ref": None,
    }
    try:
        size = path.stat().st_size
        result["size_bytes"] = size
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        result["ok"] = True
        result["top_level_type"] = type(data).__name__
    except Exception as exc:
        result["error"] = str(exc)
        return result

    if not isinstance(data, dict):
        return result

    pdf = data.get("pdf")
    if not isinstance(pdf, dict):
        return result

    pdf_path_text = str(pdf.get("path") or "").strip()
    if not pdf_path_text:
        return result

    ref_path = normalize_artifact_path(pdf_path_text, repo_root)
    migrated_path = migrated_pety_pdf_path(pdf_path_text, repo_root)
    result["pdf_ref"] = {
        "json_path": path_text,
        "source": result["source"],
        "status": pdf.get("status"),
        "path_text": pdf_path_text,
        "path": str(ref_path),
        "migrated_path": str(migrated_path) if migrated_path else "",
        "size_bytes": pdf.get("size_bytes"),
        "sha256": pdf.get("sha256"),
    }
    return result


def validate_pdf_file(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    result: dict[str, Any] = {
        "path": path_text,
        "ok": False,
        "source": source_from_path(path),
        "size_bytes": 0,
        "header_ok": False,
        "eof_ok": False,
        "readable": False,
    }
    try:
        size = path.stat().st_size
        result["size_bytes"] = size
        if size <= 0:
            result["error"] = "empty file"
            return result

        with path.open("rb") as handle:
            header = handle.read(8)
            result["readable"] = True
            result["header"] = header.decode("ascii", errors="replace")
            result["header_ok"] = header.startswith(b"%PDF-")
            tail_size = min(size, 4096)
            handle.seek(-tail_size, os.SEEK_END)
            tail = handle.read(tail_size)
            result["eof_ok"] = b"%%EOF" in tail

        result["ok"] = bool(result["header_ok"] and result["eof_ok"])
        if not result["ok"]:
            result["error"] = "invalid pdf header or EOF marker"
    except Exception as exc:
        result["error"] = str(exc)
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_files(root: Path, pattern: str) -> list[Path]:
    return sorted(path for path in root.rglob(pattern) if path.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate artifact JSON and PDF integrity.")
    parser.add_argument("--artifacts-root", default="artifacts")
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--workers", type=int, default=max(4, min(32, (os.cpu_count() or 4) * 2)))
    parser.add_argument("--sample-limit", type=int, default=50)
    parser.add_argument("--hash", action="store_true", help="Verify PDF sha256 values from item JSON metadata.")
    args = parser.parse_args()

    repo_root = Path.cwd().resolve()
    artifacts_root = (repo_root / args.artifacts_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = iso_now()
    json_files = list_files(artifacts_root, "*.json")
    pdf_files = [
        path
        for path in list_files(artifacts_root, "*.pdf")
        if not path.name.endswith(".pdf.tmp")
    ]
    tmp_pdf_files = list_files(artifacts_root, "*.pdf.tmp")

    print(f"artifact integrity validation started: {started_at}", flush=True)
    print(f"json_files={len(json_files)} pdf_files={len(pdf_files)} tmp_pdf_files={len(tmp_pdf_files)}", flush=True)

    json_summary = Counter()
    json_samples: dict[str, list[dict[str, Any]]] = {
        "invalid_json": [],
        "missing_pdf_ref_path": [],
        "migrated_pdf_ref_path": [],
        "size_mismatch": [],
        "hash_mismatch": [],
    }
    pdf_refs: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(validate_json_file, str(path), str(repo_root))
            for path in json_files
        ]
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            json_summary["total"] += 1
            json_summary["ok" if result.get("ok") else "failed"] += 1
            if not result.get("ok"):
                sample_add(json_samples["invalid_json"], result, args.sample_limit)
            ref = result.get("pdf_ref")
            if ref:
                pdf_refs.append(ref)
                json_summary["pdf_refs"] += 1
            if index % 50000 == 0:
                print(f"json_checked={index}/{len(json_files)}", flush=True)

    pdf_summary = Counter()
    pdf_samples: dict[str, list[dict[str, Any]]] = {
        "invalid_pdf": [],
        "orphan_pdf": [],
    }
    pdf_results_by_path: dict[str, dict[str, Any]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(validate_pdf_file, str(path.resolve())) for path in pdf_files]
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            pdf_results_by_path[result["path"]] = result
            pdf_summary["total"] += 1
            pdf_summary["ok" if result.get("ok") else "failed"] += 1
            pdf_summary[f"source:{result.get('source', 'unknown')}"] += 1
            if not result.get("ok"):
                sample_add(pdf_samples["invalid_pdf"], result, args.sample_limit)
            if index % 50000 == 0:
                print(f"pdf_checked={index}/{len(pdf_files)}", flush=True)

    referenced_pdf_paths: set[str] = set()
    hash_targets: dict[str, str] = {}
    for ref in pdf_refs:
        path = Path(str(ref["path"]))
        migrated = Path(str(ref["migrated_path"])) if ref.get("migrated_path") else None
        actual_path = path if path.exists() else migrated if migrated and migrated.exists() else path
        actual_text = str(actual_path)
        if actual_path.exists():
            referenced_pdf_paths.add(actual_text)
        else:
            json_summary["missing_pdf_ref_path"] += 1
            sample_add(json_samples["missing_pdf_ref_path"], ref, args.sample_limit)
            continue

        if migrated and actual_path == migrated and path != migrated:
            json_summary["migrated_pdf_ref_path"] += 1
            sample_add(json_samples["migrated_pdf_ref_path"], ref, args.sample_limit)

        expected_size = ref.get("size_bytes")
        if isinstance(expected_size, int):
            actual_size = actual_path.stat().st_size
            if expected_size != actual_size:
                json_summary["size_mismatch"] += 1
                sample = dict(ref)
                sample["actual_size_bytes"] = actual_size
                sample_add(json_samples["size_mismatch"], sample, args.sample_limit)

        expected_hash = ref.get("sha256")
        if args.hash and isinstance(expected_hash, str) and expected_hash:
            hash_targets.setdefault(actual_text, expected_hash)

    all_pdf_paths = {str(path.resolve()) for path in pdf_files}
    orphan_paths = sorted(all_pdf_paths - referenced_pdf_paths)
    pdf_summary["orphan_pdf"] = len(orphan_paths)
    for orphan in orphan_paths[: args.sample_limit]:
        sample_add(pdf_samples["orphan_pdf"], {"path": orphan}, args.sample_limit)

    if args.hash:
        print(f"sha256_targets={len(hash_targets)}", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as executor:
            futures = {
                executor.submit(sha256_file, Path(path)): (path, expected)
                for path, expected in hash_targets.items()
            }
            for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                path, expected = futures[future]
                try:
                    actual = future.result()
                except Exception as exc:
                    json_summary["hash_read_error"] += 1
                    sample_add(json_samples["hash_mismatch"], {"path": path, "error": str(exc)}, args.sample_limit)
                    continue
                if actual != expected:
                    json_summary["hash_mismatch"] += 1
                    sample_add(
                        json_samples["hash_mismatch"],
                        {"path": path, "expected_sha256": expected, "actual_sha256": actual},
                        args.sample_limit,
                    )
                if index % 10000 == 0:
                    print(f"sha256_checked={index}/{len(hash_targets)}", flush=True)

    report = {
        "started_at": started_at,
        "finished_at": iso_now(),
        "artifacts_root": str(artifacts_root),
        "hash_verified": bool(args.hash),
        "json": dict(json_summary),
        "pdf": dict(pdf_summary),
        "tmp_pdf_files": {
            "count": len(tmp_pdf_files),
            "samples": [str(path) for path in tmp_pdf_files[: args.sample_limit]],
        },
        "samples": {
            "json": json_samples,
            "pdf": pdf_samples,
        },
    }

    output_path = output_dir / f"artifact_integrity_{utc_stamp()}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"report={output_path}", flush=True)
    print(json.dumps({"json": report["json"], "pdf": report["pdf"], "tmp_pdf_count": len(tmp_pdf_files)}, ensure_ascii=False), flush=True)

    failed = (
        json_summary.get("failed", 0)
        + pdf_summary.get("failed", 0)
        + json_summary.get("missing_pdf_ref_path", 0)
        + json_summary.get("size_mismatch", 0)
        + json_summary.get("hash_mismatch", 0)
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
