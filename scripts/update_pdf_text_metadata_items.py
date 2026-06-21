#!/usr/bin/env python3
"""Update item JSON files with PDF text-extractability metadata."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PyPDF2 import PdfReader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.metadata_schema import apply_item_schema, sync_pdf_text_metadata
from src.pdf_text_metadata import analyze_pdf_text, compact_item_metadata


SOURCE_NAMES = ("pety", "searchThema")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def resolve_path(path_text: str, repo_root: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def source_from_item_path(path: Path) -> str:
    parts = path.parts
    if "searchThema" in parts:
        return "searchThema"
    if "pety" in parts:
        return "pety"
    return "unknown"


def compact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return compact_item_metadata(metadata)


def existing_metadata_current(item: dict[str, Any], pdf_path_text: str, max_pages: int | None) -> bool:
    metadata = item.get("pdf_text")
    if not isinstance(metadata, dict):
        return False
    if "text_extractable" not in metadata:
        return False
    if "pdf_text_class" not in metadata:
        return False
    if "needs_ocr" not in metadata:
        return False
    current_path = str(metadata.get("pdf_path_text") or metadata.get("pdf_path") or metadata.get("path") or "")
    if current_path != pdf_path_text:
        return False
    if metadata.get("analysis_scope") == analysis_scope(max_pages):
        if metadata.get("text_extractable") is True:
            ocr = item.get("ocr") if isinstance(item.get("ocr"), dict) else {}
            if ocr.get("status") != "skipped_text_extractable":
                return False
            if ocr.get("skip_reason") != "text_extractable_pdf":
                return False
        return True
    return False


def classify_item_for_update(
    item_path: Path,
    *,
    max_pages: int | None,
    force: bool,
    include_non_completed: bool,
) -> str:
    try:
        item = json.loads(item_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "json_error"
    if not isinstance(item, dict):
        return "json_error"

    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    pdf_status = str((pdf or {}).get("status") or "")
    if not include_non_completed and pdf_status != "completed":
        return "not_completed"

    pdf_path_text = str((pdf or {}).get("path") or "").strip()
    if not pdf_path_text:
        return "missing_pdf_path"

    if force:
        return "needs_update"
    if existing_metadata_current(item, pdf_path_text, max_pages):
        return "current"
    return "needs_update"


def classify_existing_unextractable_candidate(
    item_path: Path,
    *,
    max_pages: int | None,
    force: bool,
    include_non_completed: bool,
) -> str:
    try:
        item = json.loads(item_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "json_error"
    if not isinstance(item, dict):
        return "json_error"

    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    pdf_status = str((pdf or {}).get("status") or "")
    if not include_non_completed and pdf_status != "completed":
        return "not_completed"

    pdf_path_text = str((pdf or {}).get("path") or "").strip()
    if not pdf_path_text:
        return "missing_pdf_path"

    metadata = item.get("pdf_text") if isinstance(item.get("pdf_text"), dict) else {}
    if not metadata:
        return "missing_pdf_text"
    if metadata.get("text_extractable") is True:
        return "existing_text_extractable"
    if not force and existing_metadata_current(item, pdf_path_text, max_pages):
        return "current_unextractable"
    return "existing_unextractable"


def analysis_scope(max_pages: int | None) -> str:
    return "all_pages" if max_pages is None else f"first_{max_pages}_pages"


def pdf_integrity_ok(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
        if size <= 0:
            return False, "empty file"
        with path.open("rb") as handle:
            if not handle.read(8).startswith(b"%PDF-"):
                return False, "invalid pdf header"
            tail_size = min(size, 4096)
            handle.seek(-tail_size, os.SEEK_END)
            if b"%%EOF" not in handle.read(tail_size):
                return False, "missing EOF marker"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _pdf_object(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value


def _content_bytes(page: Any) -> bytes:
    contents = page.get_contents()
    if contents is None:
        return b""
    if isinstance(contents, list):
        parts = []
        for item in contents:
            obj = _pdf_object(item)
            if hasattr(obj, "get_data"):
                parts.append(obj.get_data())
        return b"\n".join(parts)
    obj = _pdf_object(contents)
    if hasattr(obj, "get_data"):
        return obj.get_data()
    return b""


def analyze_pdf_text_layer_probe(
    pdf_path: Path,
    *,
    max_pages: int | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    import signal

    def raise_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError("PDF text layer probe timed out")

    result: dict[str, Any] = {
        "path": str(pdf_path),
        "filename": pdf_path.name,
        "status": "ok",
        "text_extractable": False,
        "text_layer_detected": False,
        "text_pages": 0,
        "total_chars": 0,
        "extraction_method": "PyPDF2.content_stream_text_operator_probe",
        "generated_at": iso_now(),
    }
    try:
        result["size_bytes"] = pdf_path.stat().st_size
    except OSError as exc:
        result.update({"status": "error", "error": str(exc)})
        return result

    previous_handler = None
    if timeout_seconds > 0:
        previous_handler = signal.signal(signal.SIGALRM, raise_timeout)
        signal.alarm(timeout_seconds)
    try:
        try:
            reader = PdfReader(str(pdf_path))
            page_count = len(reader.pages)
            result["pages"] = page_count
            pages_to_scan = page_count if max_pages is None else min(page_count, max_pages)
            result["scanned_pages"] = pages_to_scan
            for index in range(pages_to_scan):
                page = reader.pages[index]
                resources = _pdf_object(page.get("/Resources") or {})
                fonts = _pdf_object(resources.get("/Font") or {}) if isinstance(resources, dict) else {}
                data = _content_bytes(page)
                has_text_object = b"BT" in data and b"ET" in data
                has_text_show = any(token in data for token in (b"Tj", b"TJ", b"'", b'"'))
                if fonts and has_text_object and has_text_show:
                    result["text_pages"] += 1
            result["text_layer_detected"] = result["text_pages"] > 0
            result["text_extractable"] = result["text_layer_detected"]
        except Exception as exc:  # noqa: BLE001
            result.update({"status": "error", "error": str(exc), "text_extractable": False})
    finally:
        if timeout_seconds > 0:
            signal.alarm(0)
            if previous_handler is not None:
                signal.signal(signal.SIGALRM, previous_handler)
    return result


def update_item_ocr(item: dict[str, Any], metadata: dict[str, Any]) -> None:
    sync_pdf_text_metadata(item, compact_metadata(metadata))


def process_item(
    item_path_text: str,
    repo_root_text: str,
    max_pages: int | None,
    timeout_seconds: int,
    include_sample: bool,
    sample_chars: int,
    force: bool,
    include_non_completed: bool,
    method: str,
) -> dict[str, Any]:
    repo_root = Path(repo_root_text)
    item_path = Path(item_path_text)
    source = source_from_item_path(item_path)
    result: dict[str, Any] = {
        "item_path": item_path_text,
        "source": source,
        "status": "unknown",
        "text_extractable": False,
    }
    try:
        item = json.loads(item_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        result.update({"status": "json_error", "error": str(exc)})
        return result
    if not isinstance(item, dict):
        result.update({"status": "json_error", "error": "item is not object"})
        return result

    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    pdf_status = str((pdf or {}).get("status") or "")
    if not include_non_completed and pdf_status != "completed":
        result["status"] = "skipped_not_completed"
        return result

    pdf_path_text = str((pdf or {}).get("path") or "").strip()
    if not pdf_path_text:
        result["status"] = "skipped_missing_pdf_path"
        return result

    if not force and existing_metadata_current(item, pdf_path_text, max_pages):
        metadata = item.get("pdf_text") if isinstance(item.get("pdf_text"), dict) else {}
        result.update(
            {
                "status": "skipped_existing",
                "text_extractable": bool(metadata.get("text_extractable")),
                "pdf_text_class": metadata.get("pdf_text_class"),
                "recovered_text": bool(metadata.get("recovered_text")),
                "preferred_text_source": metadata.get("preferred_text_source"),
                "needs_ocr": metadata.get("needs_ocr"),
            }
        )
        return result

    pdf_path = resolve_path(pdf_path_text, repo_root)
    result["pdf_path"] = str(pdf_path)
    integrity_ok, integrity_error = pdf_integrity_ok(pdf_path)
    if not integrity_ok:
        metadata = {
            "status": "error",
            "error": integrity_error,
            "text_extractable": False,
            "pdf_text_class": "error",
            "needs_ocr": True,
            "path": str(pdf_path),
            "pdf_path": str(pdf_path),
            "pdf_path_text": pdf_path_text,
            "source": source,
            "analysis_scope": analysis_scope(max_pages),
            "generated_at": iso_now(),
        }
        apply_item_schema(item, source_detail=source)
        update_item_ocr(item, metadata)
        item["updated_at"] = iso_now()
        write_json(item_path, item)
        result.update(
            {
                "status": "updated_error",
                "error": integrity_error,
                "pdf_text_class": metadata.get("pdf_text_class"),
                "needs_ocr": metadata.get("needs_ocr"),
            }
        )
        return result

    if method == "text-layer-probe":
        metadata = analyze_pdf_text_layer_probe(
            pdf_path,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
        )
    else:
        metadata = analyze_pdf_text(
            pdf_path,
            include_sample=include_sample,
            sample_chars=sample_chars,
            include_sha256=False,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
        )
    metadata.update(
        {
            "source": source,
            "item_path": item_path_text,
            "pdf_path": str(pdf_path),
            "pdf_path_text": pdf_path_text,
            "pdf_scope": (pdf or {}).get("scope") or "content",
            "analysis_scope": analysis_scope(max_pages),
        }
    )

    apply_item_schema(item, source_detail=source)
    update_item_ocr(item, metadata)
    item["updated_at"] = iso_now()
    write_json(item_path, item)

    status = "updated_error" if metadata.get("status") == "error" else "updated"
    result.update(
        {
            "status": status,
            "text_extractable": bool(metadata.get("text_extractable")),
            "metadata_status": metadata.get("status"),
            "pages": metadata.get("pages"),
            "scanned_pages": metadata.get("scanned_pages"),
            "total_chars": metadata.get("total_chars"),
            "error": metadata.get("error"),
            "pdf_text_class": metadata.get("pdf_text_class"),
            "recovered_text": bool(metadata.get("recovered_text")),
            "preferred_text_source": metadata.get("preferred_text_source"),
            "needs_ocr": metadata.get("needs_ocr"),
        }
    )
    return result


def iter_item_paths(artifacts_root: Path, sources: set[str]) -> list[Path]:
    paths: list[Path] = []
    for source in SOURCE_NAMES:
        if source not in sources:
            continue
        root = artifacts_root / source / "metadata" / "items"
        if root.exists():
            paths.extend(sorted(root.rglob("*.json")))
    return paths


def bounded_process(
    item_paths: list[Path],
    args: argparse.Namespace,
    repo_root: Path,
) -> Iterator[dict[str, Any]]:
    iterator = iter(item_paths)
    max_pending = max(args.workers * 4, args.workers)
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
        pending: set[concurrent.futures.Future[dict[str, Any]]] = set()
        for _ in range(min(max_pending, len(item_paths))):
            path = next(iterator, None)
            if path is None:
                break
            pending.add(submit_item(executor, path, args, repo_root))

        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                yield future.result()
                path = next(iterator, None)
                if path is not None:
                    pending.add(submit_item(executor, path, args, repo_root))


def submit_item(
    executor: concurrent.futures.ProcessPoolExecutor,
    path: Path,
    args: argparse.Namespace,
    repo_root: Path,
) -> concurrent.futures.Future[dict[str, Any]]:
    return executor.submit(
        process_item,
        str(path),
        str(repo_root),
        args.max_pages,
        args.timeout_seconds,
        args.include_sample,
        args.sample_chars,
        args.force,
        args.include_non_completed,
        args.method,
    )


def parse_sources(value: str) -> set[str]:
    if value == "all":
        return set(SOURCE_NAMES)
    return {part.strip() for part in value.split(",") if part.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Update PDF text-extractability metadata on item JSON files.")
    parser.add_argument("--source", default="all", help="all, pety, searchThema, or comma-separated sources")
    parser.add_argument("--artifacts-root", default="artifacts")
    parser.add_argument("--output-dir", default="artifacts/validation")
    parser.add_argument("--workers", type=int, default=max(2, min(16, (os.cpu_count() or 4) // 2)))
    parser.add_argument("--max-pages", type=int, default=3, help="Pages to probe per PDF; 0 means all pages.")
    parser.add_argument(
        "--method",
        choices=("extract-text", "text-layer-probe"),
        default="extract-text",
        help="extract-text uses PyPDF2 extract_text; text-layer-probe uses a faster content stream heuristic.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--progress-every", type=int, default=5000)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true", help="Recompute existing pdf_text metadata.")
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Process only item JSON files whose pdf_text metadata is missing or stale.",
    )
    parser.add_argument(
        "--only-existing-unextractable",
        action="store_true",
        help="Process only completed items whose existing pdf_text metadata is not text-extractable.",
    )
    parser.add_argument("--include-non-completed", action="store_true")
    parser.add_argument("--include-sample", action="store_true")
    parser.add_argument("--sample-chars", type=int, default=1000)
    parser.add_argument("--sample-limit", type=int, default=100)
    args = parser.parse_args()
    if args.max_pages == 0:
        args.max_pages = None

    repo_root = Path.cwd().resolve()
    artifacts_root = (repo_root / args.artifacts_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    sources = parse_sources(args.source)

    item_paths = iter_item_paths(artifacts_root, sources)
    selection_counts: Counter[str] = Counter()
    if args.only_existing_unextractable:
        selected_paths = []
        for path in item_paths:
            reason = classify_existing_unextractable_candidate(
                path,
                max_pages=args.max_pages,
                force=args.force,
                include_non_completed=args.include_non_completed,
            )
            selection_counts[reason] += 1
            if reason == "existing_unextractable":
                selected_paths.append(path)
        item_paths = selected_paths
    elif args.only_missing:
        selected_paths: list[Path] = []
        for path in item_paths:
            reason = classify_item_for_update(
                path,
                max_pages=args.max_pages,
                force=args.force,
                include_non_completed=args.include_non_completed,
            )
            selection_counts[reason] += 1
            if reason in {"needs_update", "json_error"}:
                selected_paths.append(path)
        item_paths = selected_paths
    if args.limit is not None:
        item_paths = item_paths[: args.limit]

    print(
        f"pdf text metadata update started: {iso_now()} "
        f"items={len(item_paths)} workers={args.workers} scope={analysis_scope(args.max_pages)} "
        f"method={args.method}",
        flush=True,
    )
    if selection_counts:
        print(
            "selection "
            + " ".join(f"{key}={selection_counts[key]}" for key in sorted(selection_counts)),
            flush=True,
        )

    counts: Counter[str] = Counter()
    by_source: dict[str, Counter[str]] = {source: Counter() for source in SOURCE_NAMES}
    samples: dict[str, list[dict[str, Any]]] = {
        "updated_error": [],
        "json_error": [],
        "skipped_missing_pdf_path": [],
    }

    for index, result in enumerate(bounded_process(item_paths, args, repo_root), start=1):
        status = str(result.get("status") or "unknown")
        source = str(result.get("source") or "unknown")
        counts["total"] += 1
        counts[status] += 1
        if result.get("text_extractable"):
            counts["text_extractable"] += 1
            by_source.setdefault(source, Counter())["text_extractable"] += 1
        elif status in {"updated", "updated_error", "skipped_existing"}:
            counts["image_or_unextractable"] += 1
            by_source.setdefault(source, Counter())["image_or_unextractable"] += 1
        pdf_text_class = str(result.get("pdf_text_class") or "unknown")
        counts[f"class:{pdf_text_class}"] += 1
        by_source.setdefault(source, Counter())[f"class:{pdf_text_class}"] += 1
        if result.get("recovered_text"):
            counts["recovered_text"] += 1
            by_source.setdefault(source, Counter())["recovered_text"] += 1
        by_source.setdefault(source, Counter())[status] += 1
        if status in samples and len(samples[status]) < args.sample_limit:
            samples[status].append(result)
        if args.progress_every and (index % args.progress_every == 0 or index == len(item_paths)):
            print(
                f"progress processed={index}/{len(item_paths)} updated={counts['updated']} "
                f"existing={counts['skipped_existing']} text_extractable={counts['text_extractable']} "
                f"errors={counts['updated_error'] + counts['json_error']}",
                flush=True,
            )

    report = {
        "created_at": iso_now(),
        "sources": sorted(sources),
        "analysis_scope": analysis_scope(args.max_pages),
        "settings": {
            "max_pages": args.max_pages,
            "timeout_seconds": args.timeout_seconds,
            "workers": args.workers,
            "force": args.force,
            "only_missing": args.only_missing,
            "only_existing_unextractable": args.only_existing_unextractable,
            "include_non_completed": args.include_non_completed,
            "method": args.method,
        },
        "selection_counts": dict(selection_counts),
        "counts": dict(counts),
        "by_source": {source: dict(counter) for source, counter in sorted(by_source.items())},
        "samples": samples,
    }
    output_path = output_dir / f"pdf_text_metadata_update_{utc_stamp()}.json"
    write_json(output_path, report)
    print(f"report={output_path}", flush=True)
    print(json.dumps({"counts": report["counts"], "by_source": report["by_source"]}, ensure_ascii=False), flush=True)
    return 1 if counts.get("updated_error", 0) or counts.get("json_error", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
