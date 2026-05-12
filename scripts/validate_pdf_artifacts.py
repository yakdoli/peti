#!/usr/bin/env python3
"""Validate PDF artifact files by scanning headers and EOF markers."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from collections.abc import Iterator
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bucket_for_path(path: Path, artifacts_root: Path) -> str:
    try:
        rel = path.relative_to(artifacts_root)
    except ValueError:
        return "outside_artifacts"
    parts = rel.parts
    if len(parts) >= 2 and parts[1] in {"pdfs", "issue_pdfs", "partial_pdfs"}:
        return f"{parts[0]}/{parts[1]}"
    if parts:
        return parts[0]
    return "unknown"


def validate_pdf(path_text: str, artifacts_root_text: str) -> dict[str, Any]:
    path = Path(path_text)
    artifacts_root = Path(artifacts_root_text)
    result: dict[str, Any] = {
        "path": path_text,
        "bucket": bucket_for_path(path, artifacts_root),
        "ok": False,
        "exists": False,
        "size_bytes": 0,
        "header_ok": False,
        "eof_ok": False,
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


def iter_pdf_files(artifacts_root: Path) -> list[Path]:
    return sorted(
        path
        for path in artifacts_root.rglob("*.pdf")
        if path.is_file() and not path.name.endswith(".tmp")
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp_path.replace(path)


def bounded_validate(
    pdf_files: list[Path],
    artifacts_root: Path,
    workers: int,
) -> Iterator[dict[str, Any]]:
    iterator = iter(pdf_files)
    max_pending = max(workers * 8, workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        pending: set[concurrent.futures.Future[dict[str, Any]]] = set()
        for _ in range(min(max_pending, len(pdf_files))):
            path = next(iterator, None)
            if path is None:
                break
            pending.add(executor.submit(validate_pdf, str(path.resolve()), str(artifacts_root)))

        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                yield future.result()
                path = next(iterator, None)
                if path is not None:
                    pending.add(executor.submit(validate_pdf, str(path.resolve()), str(artifacts_root)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate all PDF artifacts.")
    parser.add_argument("--artifacts-root", default="artifacts")
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--workers", type=int, default=max(4, min(64, (os.cpu_count() or 4) * 4)))
    parser.add_argument("--progress-every", type=int, default=50000)
    parser.add_argument("--sample-limit", type=int, default=100)
    args = parser.parse_args()

    repo_root = Path.cwd().resolve()
    artifacts_root = (repo_root / args.artifacts_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = iso_now()
    pdf_files = iter_pdf_files(artifacts_root)
    print(f"pdf artifact validation started: {started_at}", flush=True)
    print(f"pdf_files={len(pdf_files)} workers={args.workers}", flush=True)

    counts: Counter[str] = Counter()
    by_bucket: dict[str, Counter[str]] = defaultdict(Counter)
    invalid_rows: list[dict[str, Any]] = []
    invalid_samples: list[dict[str, Any]] = []
    size_by_bucket: Counter[str] = Counter()

    for index, result in enumerate(bounded_validate(pdf_files, artifacts_root, args.workers), start=1):
        bucket = str(result.get("bucket") or "unknown")
        ok = bool(result.get("ok"))
        counts["total"] += 1
        counts["ok" if ok else "failed"] += 1
        if result.get("exists"):
            counts["exists"] += 1
        if result.get("header_ok"):
            counts["header_ok"] += 1
        if result.get("eof_ok"):
            counts["eof_ok"] += 1
        by_bucket[bucket]["total"] += 1
        by_bucket[bucket]["ok" if ok else "failed"] += 1
        size_by_bucket[bucket] += int(result.get("size_bytes") or 0)
        if not ok:
            invalid_rows.append(result)
            if len(invalid_samples) < args.sample_limit:
                invalid_samples.append(result)
        if args.progress_every and (index % args.progress_every == 0 or index == len(pdf_files)):
            print(
                f"progress checked={index}/{len(pdf_files)} ok={counts['ok']} failed={counts['failed']}",
                flush=True,
            )

    by_bucket_report = {
        bucket: {**dict(counter), "size_bytes": size_by_bucket[bucket]}
        for bucket, counter in sorted(by_bucket.items())
    }
    report = {
        "started_at": started_at,
        "finished_at": iso_now(),
        "artifacts_root": str(artifacts_root),
        "counts": dict(counts),
        "by_bucket": by_bucket_report,
        "invalid_samples": invalid_samples,
        "invalid_jsonl": "",
    }
    output_path = output_dir / f"pdf_artifact_integrity_all_{utc_stamp()}.json"
    invalid_path = output_path.with_suffix(".invalid.jsonl")
    write_jsonl(invalid_path, invalid_rows)
    report["invalid_jsonl"] = str(invalid_path.relative_to(repo_root))
    write_json(output_path, report)

    print(f"report={output_path}", flush=True)
    print(f"invalid_jsonl={invalid_path}", flush=True)
    print(json.dumps({"counts": report["counts"], "by_bucket": report["by_bucket"]}, ensure_ascii=False), flush=True)
    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
