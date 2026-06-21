"""Generate non-OCR text-extraction metadata for PDF artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, Iterable, Iterator

from PyPDF2 import PdfReader

try:
    from .metadata_schema import apply_item_schema, sync_pdf_text_metadata
except ImportError:
    from metadata_schema import apply_item_schema, sync_pdf_text_metadata  # type: ignore[reportMissingImports]

try:
    import pymupdf  # type: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - PyMuPDF exposes either pymupdf or fitz depending on version.
    try:
        import fitz as pymupdf  # type: ignore[no-redef, reportMissingImports]
    except ImportError:
        pymupdf = None  # type: ignore[assignment]

try:
    from markitdown import MarkItDown  # type: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - dependency may be absent in minimal installs.
    MarkItDown = None  # type: ignore[assignment]


SOURCE_NAMES = ("pety", "searchThema")
COMMON_KOREAN_SYLLABLES = set(
    "의이하에가고는은를을로으로다시구동면리도군청서제정변경관할행기관업무주민등록"
    "고시따라다음같시행합니다서울특별중세종대로대한민국공무원재산신고공개"
)


def analyze_pdf_text(
    pdf_path: Path,
    *,
    include_sample: bool = False,
    sample_chars: int = 1000,
    include_sha256: bool = False,
    max_pages: int | None = None,
    timeout_seconds: int = 30,
    recover_with_pymupdf: bool = True,
) -> Dict[str, Any]:
    """Inspect whether a PDF has extractable text and recover native text when possible."""
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
            result["digital_origin_evidence"] = inspect_pdf_digital_origin(reader, pages_to_scan)
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
            result["primary_text_extractable"] = result["text_extractable"]
            result["primary_text_pages"] = result["text_pages"]
            result["primary_total_chars"] = result["total_chars"]
            if result["text_pages"]:
                result["avg_chars_per_text_page"] = round(result["total_chars"] / result["text_pages"], 2)
            if include_sample:
                result["sample_text"] = " ".join(sample_parts)[:sample_chars]
            digital_evidence = result.get("digital_origin_evidence")
            should_attempt_recovery = (
                recover_with_pymupdf
                and not result["text_extractable"]
                and isinstance(digital_evidence, dict)
                and bool(digital_evidence.get("has_digital_evidence"))
            )
            if should_attempt_recovery:
                apply_pymupdf_recovery(
                    result,
                    pdf_path,
                    include_sample=include_sample,
                    sample_chars=sample_chars,
                    max_pages=max_pages,
                    timeout_seconds=timeout_seconds,
                )
            classify_pdf_text_result(result)
        except Exception as exc:
            result.update(
                {
                    "status": "error",
                    "error": str(exc),
                    "text_extractable": False,
                    "pages": int(result.get("pages") or 0),
                    "text_pages": int(result.get("text_pages") or 0),
                    "total_chars": int(result.get("total_chars") or 0),
                }
            )
            classify_pdf_text_result(result)
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
        "recovered_text_extractable": 0,
        "image_or_unextractable": 0,
        "by_pdf_text_class": {},
        "errors": 0,
        "updated_items": 0,
    }
    class_counts: Dict[str, int] = {}
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
            if metadata.get("recovered_text"):
                summary["recovered_text_extractable"] += 1
        else:
            summary["image_or_unextractable"] += 1
        pdf_text_class = str(metadata.get("pdf_text_class") or "unknown")
        class_counts[pdf_text_class] = class_counts.get(pdf_text_class, 0) + 1

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
    summary["by_pdf_text_class"] = dict(sorted(class_counts.items()))
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
    apply_item_schema(item, source_detail=str(pdf_text_metadata.get("source") or ""))
    sync_pdf_text_metadata(item, compact)

    item["updated_at"] = datetime.now().isoformat()
    write_json(item_path, item)
    return True


def compact_item_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Keep item JSON metadata useful without embedding large text samples."""
    return strip_sample_text(metadata)


def inspect_pdf_digital_origin(reader: Any, pages_to_scan: int) -> Dict[str, Any]:
    """Return lightweight evidence that a PDF is digital-born rather than scanned image only."""
    evidence: Dict[str, Any] = {
        "font_count": 0,
        "image_xobject_count": 0,
        "xobject_count": 0,
        "has_fonts": False,
        "has_images": False,
        "has_digital_evidence": False,
        "resource_errors": [],
    }
    font_names: set[str] = set()
    image_count = 0
    xobject_count = 0
    resource_errors: list[Dict[str, Any]] = []
    for page_index in range(pages_to_scan):
        try:
            page = reader.pages[page_index]
            resources = dereference_pdf_object(page.get("/Resources", {})) or {}
            fonts = dereference_pdf_object(resources.get("/Font", {})) if hasattr(resources, "get") else {}
            if hasattr(fonts, "keys"):
                font_names.update(str(name) for name in fonts.keys())
            xobjects = dereference_pdf_object(resources.get("/XObject", {})) if hasattr(resources, "get") else {}
            if hasattr(xobjects, "items"):
                for _name, value in xobjects.items():
                    xobject_count += 1
                    xobject = dereference_pdf_object(value)
                    if hasattr(xobject, "get") and str(xobject.get("/Subtype") or "") == "/Image":
                        image_count += 1
        except Exception as exc:  # noqa: BLE001 - resource inspection should never fail classification.
            resource_errors.append({"page_index": page_index, "error": str(exc)})

    evidence["font_count"] = len(font_names)
    evidence["image_xobject_count"] = image_count
    evidence["xobject_count"] = xobject_count
    evidence["has_fonts"] = bool(font_names)
    evidence["has_images"] = image_count > 0
    evidence["has_digital_evidence"] = bool(font_names)
    if resource_errors:
        evidence["resource_errors"] = resource_errors
        evidence["resource_error_count"] = len(resource_errors)
    return evidence


def dereference_pdf_object(value: Any) -> Any:
    if hasattr(value, "get_object"):
        return value.get_object()
    return value


def apply_pymupdf_recovery(
    result: Dict[str, Any],
    pdf_path: Path,
    *,
    include_sample: bool,
    sample_chars: int,
    max_pages: int | None,
    timeout_seconds: int,
) -> None:
    recovery = recover_digital_text(
        pdf_path,
        include_sample=include_sample,
        sample_chars=sample_chars,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
    )
    result["recovery"] = recovery
    if recovery.get("text_extractable"):
        result["recovered_text"] = True
        result["text_extractable"] = True
        result["text_pages"] = recovery.get("text_pages", 0)
        result["total_chars"] = recovery.get("total_chars", 0)
        result["extraction_method"] = recovery.get("method")
        result["preferred_text_source"] = recovery.get("preferred_text_source")
        if include_sample:
            result["sample_text"] = recovery.get("sample_text", "")
    else:
        result["recovered_text"] = False


def recover_digital_text(
    pdf_path: Path,
    *,
    include_sample: bool = False,
    sample_chars: int = 1000,
    max_pages: int | None = None,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Try optional native-text recovery backends before falling back to OCR."""
    attempts: list[Dict[str, Any]] = []

    for backend, extractor in (
        ("pymupdf", extract_with_pymupdf_text),
        ("markitdown", extract_with_markitdown_text),
    ):
        attempt = extractor(
            pdf_path,
            include_sample=include_sample,
            sample_chars=sample_chars,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
        )
        attempts.append(compact_recovery_attempt(backend, attempt))
        if usable_recovered_text(attempt):
            return successful_recovery(backend, attempt, attempts)

    with TemporaryDirectory(prefix="peti-ghostscript-recovery-") as temp_dir:
        normalized_path = Path(temp_dir) / "ghostscript-normalized.pdf"
        ghostscript_attempt = normalize_pdf_with_ghostscript(
            pdf_path,
            normalized_path,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
        )
        attempts.append(compact_recovery_attempt("ghostscript_pdfwrite", ghostscript_attempt))
        if ghostscript_attempt.get("status") == "ok":
            for backend, extractor in (
                ("ghostscript_pymupdf", extract_with_pymupdf_text),
                ("ghostscript_markitdown", extract_with_markitdown_text),
            ):
                attempt = extractor(
                    normalized_path,
                    include_sample=include_sample,
                    sample_chars=sample_chars,
                    max_pages=max_pages,
                    timeout_seconds=timeout_seconds,
                )
                attempts.append(compact_recovery_attempt(backend, attempt))
                if usable_recovered_text(attempt):
                    recovery = successful_recovery(backend, attempt, attempts)
                    recovery["ghostscript_normalized"] = True
                    recovery["ghostscript"] = ghostscript_attempt
                    return recovery

    return {
        "status": "unrecovered",
        "text_extractable": False,
        "text_pages": 0,
        "total_chars": 0,
        "preferred_text_source": None,
        "attempts": attempts,
    }


def successful_recovery(
    backend: str,
    attempt: Dict[str, Any],
    attempts: list[Dict[str, Any]],
) -> Dict[str, Any]:
    recovery = {
        "status": "ok",
        "method": attempt.get("method"),
        "preferred_text_source": backend,
        "text_extractable": True,
        "text_pages": attempt.get("text_pages", 0),
        "total_chars": attempt.get("total_chars", 0),
        "sample_text": attempt.get("sample_text", ""),
        "attempts": attempts,
    }
    if "text_quality" in attempt:
        recovery["text_quality"] = attempt["text_quality"]
    return recovery


def extract_with_pymupdf_text(
    pdf_path: Path,
    *,
    include_sample: bool = False,
    sample_chars: int = 1000,
    max_pages: int | None = None,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Recover native PDF text with PyMuPDF for PDFs where PyPDF2 returns no text."""
    result: Dict[str, Any] = {
        "status": "ok",
        "method": "PyMuPDF.Page.get_text(text)",
        "text_extractable": False,
        "text_pages": 0,
        "total_chars": 0,
    }
    if pymupdf is None:
        return {
            **result,
            "status": "error",
            "error": "pymupdf is not installed",
        }

    previous_handler = None
    if timeout_seconds > 0:
        previous_handler = signal.signal(signal.SIGALRM, _raise_timeout)
        signal.alarm(timeout_seconds)
    try:
        try:
            with suppress_stderr_fd():
                with pymupdf.open(str(pdf_path)) as document:
                    page_count = len(document)
                    pages_to_scan = page_count if max_pages is None else min(page_count, max_pages)
                    result["pages"] = page_count
                    result["scanned_pages"] = pages_to_scan
                    sample_parts: list[str] = []
                    quality_parts: list[str] = []
                    page_errors: list[Dict[str, Any]] = []
                    for page_index in range(pages_to_scan):
                        try:
                            text = normalize_text(document[page_index].get_text("text") or "")
                        except Exception as exc:  # noqa: BLE001
                            page_errors.append({"page_index": page_index, "error": str(exc)})
                            continue
                        if text:
                            result["text_pages"] += 1
                            result["total_chars"] += len(text)
                            if len(" ".join(quality_parts)) < max(sample_chars, 1000):
                                quality_parts.append(text)
                            if include_sample and len(" ".join(sample_parts)) < sample_chars:
                                sample_parts.append(text)
                    result["text_extractable"] = result["text_pages"] > 0 and result["total_chars"] > 0
                    text_for_quality = " ".join(quality_parts)[: max(sample_chars, 1000)]
                    result["text_quality"] = assess_extracted_text_quality(text_for_quality)
                    if result["text_pages"]:
                        result["avg_chars_per_text_page"] = round(result["total_chars"] / result["text_pages"], 2)
                    if include_sample:
                        result["sample_text"] = " ".join(sample_parts)[:sample_chars]
                    if page_errors:
                        result["page_errors"] = page_errors
                        result["page_error_count"] = len(page_errors)
        except Exception as exc:  # noqa: BLE001
            result.update({"status": "error", "error": str(exc), "text_extractable": False})
    finally:
        if timeout_seconds > 0:
            signal.alarm(0)
            if previous_handler is not None:
                signal.signal(signal.SIGALRM, previous_handler)
    return result


def extract_with_markitdown_text(
    pdf_path: Path,
    *,
    include_sample: bool = False,
    sample_chars: int = 1000,
    max_pages: int | None = None,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Recover PDF text through MarkItDown when the dependency is available."""
    _ = max_pages
    result: Dict[str, Any] = {
        "status": "ok",
        "method": "MarkItDown.convert",
        "text_extractable": False,
        "text_pages": 0,
        "total_chars": 0,
    }
    if MarkItDown is None:
        return {
            **result,
            "status": "error",
            "error": "markitdown is not installed",
        }

    previous_handler = None
    if timeout_seconds > 0:
        previous_handler = signal.signal(signal.SIGALRM, _raise_timeout)
        signal.alarm(timeout_seconds)
    try:
        try:
            converter = MarkItDown(enable_plugins=False)
            converted = converter.convert(str(pdf_path))
            text = normalize_text(str(getattr(converted, "text_content", "") or ""))
            result["text_extractable"] = bool(text)
            result["text_pages"] = 1 if text else 0
            result["total_chars"] = len(text)
            result["text_quality"] = assess_extracted_text_quality(text)
            if include_sample:
                result["sample_text"] = text[:sample_chars]
        except Exception as exc:  # noqa: BLE001
            result.update({"status": "error", "error": str(exc), "text_extractable": False})
    finally:
        if timeout_seconds > 0:
            signal.alarm(0)
            if previous_handler is not None:
                signal.signal(signal.SIGALRM, previous_handler)
    return result


def normalize_pdf_with_ghostscript(
    pdf_path: Path,
    output_path: Path,
    *,
    max_pages: int | None = None,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Rewrite a PDF through Ghostscript pdfwrite into a temporary normalized file."""
    executable = shutil.which("gs")
    result: Dict[str, Any] = {
        "status": "ok",
        "method": "Ghostscript pdfwrite",
        "text_extractable": False,
        "command": "gs",
    }
    if executable is None:
        return {**result, "status": "error", "error": "ghostscript executable not found"}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-dQUIET",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        f"-sOutputFile={output_path}",
    ]
    if max_pages is not None and max_pages > 0:
        command.extend(["-dFirstPage=1", f"-dLastPage={max_pages}"])
        result["normalized_pages"] = max_pages
    command.append(str(pdf_path))
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds, 1),
        )
    except subprocess.TimeoutExpired:
        return {**result, "status": "error", "error": "ghostscript timed out"}
    except OSError as exc:
        return {**result, "status": "error", "error": str(exc)}

    if completed.returncode != 0:
        error = normalize_text(completed.stderr or completed.stdout or f"ghostscript exited {completed.returncode}")
        return {
            **result,
            "status": "error",
            "error": error,
            "returncode": completed.returncode,
        }
    if not output_path.exists() or output_path.stat().st_size <= 0:
        return {**result, "status": "error", "error": "ghostscript produced no output"}
    return {
        **result,
        "output_size_bytes": output_path.stat().st_size,
    }


def compact_recovery_attempt(backend: str, attempt: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "status",
        "method",
        "text_extractable",
        "text_quality",
        "text_pages",
        "total_chars",
        "error",
        "returncode",
        "output_size_bytes",
        "normalized_pages",
    )
    compact = {"backend": backend}
    compact.update({key: attempt.get(key) for key in keys if key in attempt})
    return compact


def usable_recovered_text(attempt: Dict[str, Any]) -> bool:
    if not attempt.get("text_extractable"):
        return False
    quality = attempt.get("text_quality")
    if isinstance(quality, dict) and str(quality.get("status") or "").startswith("suspect_"):
        return False
    return True


def assess_extracted_text_quality(text: str) -> Dict[str, Any]:
    normalized = normalize_text(text)
    cid_placeholders = len(re.findall(r"\(cid:\d+\)", normalized))
    if cid_placeholders >= 5:
        return {
            "status": "suspect_cid_placeholders",
            "cid_placeholders": cid_placeholders,
            "reason": "text contains unresolved PDF CID placeholders",
        }
    hangul = [char for char in normalized if "\uac00" <= char <= "\ud7a3"]
    if not normalized:
        return {"status": "empty", "reason": "no extracted text"}
    if len(hangul) < 50:
        return {
            "status": "unknown_short_text",
            "hangul_syllables": len(hangul),
            "reason": "too little Hangul text for mojibake heuristic",
        }
    final_ratio = sum(1 for char in hangul if (ord(char) - 0xAC00) % 28 != 0) / len(hangul)
    common_ratio = sum(1 for char in hangul if char in COMMON_KOREAN_SYLLABLES) / len(hangul)
    status = "ok"
    reason = "Korean text passed lightweight mojibake heuristic"
    if final_ratio >= 0.85 and common_ratio <= 0.08:
        status = "suspect_mojibake"
        reason = "Hangul syllables look like bad CMap/encoding recovery"
    return {
        "status": status,
        "hangul_syllables": len(hangul),
        "hangul_final_ratio": round(final_ratio, 4),
        "common_korean_syllable_ratio": round(common_ratio, 4),
        "reason": reason,
    }


def strip_sample_text(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_sample_text(item) for key, item in value.items() if key != "sample_text"}
    if isinstance(value, list):
        return [strip_sample_text(item) for item in value]
    return value


@contextmanager
def suppress_stderr_fd() -> Iterator[None]:
    """Suppress noisy native-library stdout/stderr writes inside the current process."""
    saved_fds: list[tuple[int, int]] = []
    devnull_fd = None
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        for fd in (1, 2):
            saved_fds.append((fd, os.dup(fd)))
            os.dup2(devnull_fd, fd)
    except OSError:
        yield
        return
    try:
        yield
    finally:
        for fd, saved_fd in reversed(saved_fds):
            try:
                os.dup2(saved_fd, fd)
            except OSError:
                pass
            os.close(saved_fd)
        if devnull_fd is not None:
            os.close(devnull_fd)


def classify_pdf_text_result(result: Dict[str, Any]) -> None:
    evidence = result.get("digital_origin_evidence") if isinstance(result.get("digital_origin_evidence"), dict) else {}
    if result.get("status") == "error":
        pdf_text_class = "error"
        needs_ocr = True
    elif result.get("recovered_text"):
        pdf_text_class = "digital_text_recovered"
        needs_ocr = False
    elif result.get("text_extractable"):
        pdf_text_class = "text_extractable"
        needs_ocr = False
    elif evidence.get("has_digital_evidence"):
        pdf_text_class = "digital_text_unrecovered"
        needs_ocr = True
    elif evidence.get("has_images"):
        pdf_text_class = "image_or_scanned"
        needs_ocr = True
    else:
        pdf_text_class = "unknown_unextractable"
        needs_ocr = True
    result["pdf_text_class"] = pdf_text_class
    result["needs_ocr"] = needs_ocr


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
