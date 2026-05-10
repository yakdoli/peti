"""Generate non-OCR text-extraction metadata for PDF artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import signal
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator

from PyPDF2 import PdfReader


SOURCE_NAMES = ("pety", "searchThema")


def analyze_pdf_text(
    pdf_path: Path,
    *,
    include_sample: bool = False,
    sample_chars: int = 1000,
    include_sha256: bool = False,
    max_pages: int | None = None,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Inspect whether a PDF has extractable text using PyPDF2 only."""
    result: Dict[str, Any] = {
        "path": str(pdf_path),
        "filename": pdf_path.name,
        "status": "ok",
        "text_extractable": False,
        "text_pages": 0,
        "total_chars": 0,
        "extraction_method": "PyPDF2.PdfReader.extract_text",
        "generated_at": datetime.now().isoformat(),
    }

    try:
        stat = pdf_path.stat()
        result["size_bytes"] = stat.st_size
    except OSError as exc:
        result.update({"status": "error", "error": str(exc)})
        return result

    if include_sha256:
        result["sha256"] = file_sha256(pdf_path)

    previous_handler = None
    if timeout_seconds > 0:
        previous_handler = signal.signal(signal.SIGALRM, _raise_timeout)
        signal.alarm(timeout_seconds)

    try:
        try:
            reader = PdfReader(str(pdf_path))
            page_count = len(reader.pages)
            result["pages"] = page_count
            result["pdf_metadata"] = {str(k): str(v) for k, v in (reader.metadata or {}).items()}

            sample_parts: list[str] = []
            pages_to_scan = page_count if max_pages is None else min(page_count, max_pages)
            page_errors: list[Dict[str, Any]] = []
            for index in range(pages_to_scan):
                try:
                    text = normalize_text(reader.pages[index].extract_text() or "")
                except Exception as exc:
                    page_errors.append({"page_index": index, "error": str(exc)})
                    continue
                if text:
                    result["text_pages"] += 1
                    result["total_chars"] += len(text)
                    if include_sample and len(" ".join(sample_parts)) < sample_chars:
                        sample_parts.append(text)

            result["scanned_pages"] = pages_to_scan
            if page_errors:
                result["page_errors"] = page_errors
                result["page_error_count"] = len(page_errors)
            result["text_extractable"] = result["text_pages"] > 0 and result["total_chars"] > 0
            if result["text_pages"]:
                result["avg_chars_per_text_page"] = round(result["total_chars"] / result["text_pages"], 2)
            if include_sample:
                result["sample_text"] = " ".join(sample_parts)[:sample_chars]
        except Exception as exc:
            result.update({"status": "error", "error": str(exc), "text_extractable": False})
    finally:
        if timeout_seconds > 0:
            signal.alarm(0)
            if previous_handler is not None:
                signal.signal(signal.SIGALRM, previous_handler)

    return result


def generate_source_text_metadata(
    source: str,
    *,
    artifacts_root: Path = Path("artifacts"),
    limit: int | None = None,
    update_items: bool = False,
    include_sample: bool = False,
    sample_chars: int = 1000,
    include_sha256: bool = False,
    max_pages: int | None = None,
    progress_every: int = 0,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Scan one source's PDFs and write text metadata sidecars and indexes."""
    if source not in SOURCE_NAMES:
        raise ValueError(f"지원하지 않는 source입니다: {source}")

    source_root = artifacts_root / source
    pdf_dir = source_root / "pdfs"
    output_dir = source_root / "text_metadata"
    item_metadata_dir = source_root / "metadata" / "items"

    output_items_dir = output_dir / "items"
    output_items_dir.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "source": source,
        "pdf_dir": str(pdf_dir),
        "output_dir": str(output_dir),
        "started_at": datetime.now().isoformat(),
        "total_pdfs": 0,
        "processed": 0,
        "text_extractable": 0,
        "image_or_unextractable": 0,
        "errors": 0,
        "updated_items": 0,
    }
    index: Dict[str, Dict[str, Any]] = {}

    for pdf_path in limited(iter_pdf_paths(pdf_dir), limit):
        summary["total_pdfs"] += 1
        rel_key = pdf_path.relative_to(pdf_dir).with_suffix("").as_posix()
        metadata = analyze_pdf_text(
            pdf_path,
            include_sample=include_sample,
            sample_chars=sample_chars,
            include_sha256=include_sha256,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
        )
        metadata["source"] = source
        metadata["pdf_key"] = rel_key
        metadata["pdf_path"] = str(pdf_path)

        if metadata.get("status") == "error":
            summary["errors"] += 1
        elif metadata.get("text_extractable"):
            summary["text_extractable"] += 1
        else:
            summary["image_or_unextractable"] += 1

        summary["processed"] += 1
        index[rel_key] = metadata
        write_json(output_items_dir / f"{rel_key}.json", metadata)

        if update_items and update_item_metadata(item_metadata_dir / f"{rel_key}.json", metadata):
            summary["updated_items"] += 1

        if progress_every and summary["processed"] % progress_every == 0:
            print(
                "source={source} processed={processed} text_extractable={text_extractable} "
                "image_or_unextractable={image_or_unextractable} errors={errors}".format(**summary),
                flush=True,
            )

    summary["completed_at"] = datetime.now().isoformat()
    write_json(output_dir / "metadata.json", index)
    write_json(output_dir / "summary.json", summary)
    return summary


def update_item_metadata(item_path: Path, pdf_text_metadata: Dict[str, Any]) -> bool:
    """Update an existing item JSON with pdf_text and OCR skip metadata."""
    if not item_path.exists():
        return False

    try:
        item = json.loads(item_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    compact = compact_item_metadata(pdf_text_metadata)
    item["pdf_text"] = compact

    ocr = item.setdefault("ocr", {})
    ocr["extracted_metadata"] = compact
    if pdf_text_metadata.get("text_extractable"):
        ocr["status"] = "skipped_text_extractable"
        ocr["skip_reason"] = "text_extractable_pdf"
    else:
        ocr.setdefault("status", "pending")
        ocr["skip_reason"] = ""

    item["updated_at"] = datetime.now().isoformat()
    write_json(item_path, item)
    return True


def compact_item_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Keep item JSON metadata useful without embedding large text samples."""
    excluded = {"sample_text"}
    return {key: value for key, value in metadata.items() if key not in excluded}


def iter_pdf_paths(pdf_dir: Path) -> Iterator[Path]:
    if not pdf_dir.exists():
        return iter(())
    return (path for path in sorted(pdf_dir.rglob("*.pdf")) if path.is_file())


def limited(paths: Iterable[Path], limit: int | None) -> Iterator[Path]:
    for index, path in enumerate(paths):
        if limit is not None and index >= limit:
            break
        yield path


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _raise_timeout(_signum: int, _frame: Any) -> None:
    raise TimeoutError("PDF text analysis timed out")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp_path.replace(path)
