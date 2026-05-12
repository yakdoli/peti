"""Peer-review PDF text extraction paths: native text, MarkItDown, and OCR."""

from __future__ import annotations

import concurrent.futures
import difflib
import json
import os
import re
import signal
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator

from src.pdf_text_metadata import SOURCE_NAMES, analyze_pdf_text

try:
    from markitdown import MarkItDown  # type: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - dependency may be absent in minimal installs.
    MarkItDown = None  # type: ignore[assignment]

try:
    import pymupdf  # type: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - PyMuPDF exposes either pymupdf or fitz depending on version.
    try:
        import fitz as pymupdf  # type: ignore[no-redef, reportMissingImports]
    except ImportError:
        pymupdf = None  # type: ignore[assignment]

try:
    import pytesseract  # type: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - dependency may be absent in minimal installs.
    pytesseract = None  # type: ignore[assignment]


def analyze_pdf_extraction_peer_review(
    pdf_path: Path,
    *,
    image_output_dir: Path,
    max_pages: int | None = 1,
    sample_chars: int = 1200,
    timeout_seconds: int = 30,
    ocr_lang: str = "kor+eng",
    ocr_dpi: int = 200,
    run_markitdown: bool = True,
    run_ocr: bool = True,
) -> Dict[str, Any]:
    """Run extraction methods and produce a compact peer-review report."""
    result: Dict[str, Any] = {
        "path": str(pdf_path),
        "filename": pdf_path.name,
        "status": "ok",
        "analysis_scope": analysis_scope(max_pages),
        "generated_at": iso_now(),
        "peers": {},
        "review": {},
        "decision": {},
    }

    result["peers"]["pdf_text"] = extract_with_pdf_text(
        pdf_path,
        max_pages=max_pages,
        sample_chars=sample_chars,
        timeout_seconds=timeout_seconds,
    )
    if run_markitdown:
        result["peers"]["markitdown"] = extract_with_markitdown(
            pdf_path,
            sample_chars=sample_chars,
            timeout_seconds=timeout_seconds,
        )
    else:
        result["peers"]["markitdown"] = skipped_method("markitdown disabled")

    if run_ocr:
        result["peers"]["image_ocr"] = extract_with_image_ocr(
            pdf_path,
            image_output_dir=image_output_dir,
            max_pages=max_pages,
            sample_chars=sample_chars,
            timeout_seconds=timeout_seconds,
            lang=ocr_lang,
            dpi=ocr_dpi,
        )
    else:
        result["peers"]["image_ocr"] = skipped_method("ocr disabled")

    result["review"] = peer_review_extractions(result["peers"])
    result["decision"] = decide_extraction(result["peers"], result["review"])
    if all(peer.get("status") in {"error", "skipped"} for peer in result["peers"].values()):
        result["status"] = "error"
        result["error"] = "all extraction methods failed or were skipped"
    return result


def extract_with_pdf_text(
    pdf_path: Path,
    *,
    max_pages: int | None,
    sample_chars: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    metadata = analyze_pdf_text(
        pdf_path,
        include_sample=True,
        sample_chars=sample_chars,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
    )
    sample = str(metadata.get("sample_text") or "")
    return {
        "status": metadata.get("status", "unknown"),
        "method": "PyPDF2.PdfReader.extract_text",
        "text_extractable": bool(metadata.get("text_extractable")),
        "pages": metadata.get("pages"),
        "scanned_pages": metadata.get("scanned_pages"),
        "text_chars": int(metadata.get("total_chars") or len(sample)),
        "sample_text": sample[:sample_chars],
        "error": metadata.get("error"),
    }


def extract_with_markitdown(
    pdf_path: Path,
    *,
    sample_chars: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    if MarkItDown is None:
        return error_method("markitdown is not installed", method="MarkItDown.convert")

    previous_handler = None
    if timeout_seconds > 0:
        previous_handler = signal.signal(signal.SIGALRM, _raise_timeout)
        signal.alarm(timeout_seconds)
    try:
        try:
            converter = MarkItDown(enable_plugins=False)
            converted = converter.convert(str(pdf_path))
            text = normalize_text(str(getattr(converted, "text_content", "") or ""))
            return {
                "status": "ok",
                "method": "MarkItDown.convert",
                "text_extractable": bool(text),
                "text_chars": len(text),
                "sample_text": text[:sample_chars],
            }
        except Exception as exc:  # noqa: BLE001
            return error_method(str(exc), method="MarkItDown.convert")
    finally:
        if timeout_seconds > 0:
            signal.alarm(0)
            if previous_handler is not None:
                signal.signal(signal.SIGALRM, previous_handler)


def extract_with_image_ocr(
    pdf_path: Path,
    *,
    image_output_dir: Path,
    max_pages: int | None,
    sample_chars: int,
    timeout_seconds: int,
    lang: str,
    dpi: int,
) -> Dict[str, Any]:
    image_output_dir.mkdir(parents=True, exist_ok=True)
    result: Dict[str, Any] = {
        "status": "ok",
        "method": "PyMuPDF.get_pixmap + pytesseract.image_to_string",
        "text_extractable": False,
        "text_chars": 0,
        "sample_text": "",
        "images": [],
        "lang": lang,
        "dpi": dpi,
    }
    if pymupdf is None:
        return error_method("pymupdf is not installed", method=result["method"], images=[])

    previous_handler = None
    if timeout_seconds > 0:
        previous_handler = signal.signal(signal.SIGALRM, _raise_timeout)
        signal.alarm(timeout_seconds)
    try:
        try:
            with pymupdf.open(str(pdf_path)) as document:
                page_count = len(document)
                pages_to_scan = page_count if max_pages is None else min(page_count, max_pages)
                result["pages"] = page_count
                result["scanned_pages"] = pages_to_scan
                sample_parts: list[str] = []
                page_errors: list[Dict[str, Any]] = []
                for page_index in range(pages_to_scan):
                    image_path = image_output_dir / f"page_{page_index + 1:03d}.png"
                    try:
                        page = document[page_index]
                        pixmap = page.get_pixmap(dpi=dpi)
                        pixmap.save(str(image_path))
                        page_text = ocr_image(image_path, lang=lang, timeout_seconds=timeout_seconds)
                        page_chars = len(normalize_text(page_text))
                        result["text_chars"] += page_chars
                        if page_text and len(" ".join(sample_parts)) < sample_chars:
                            sample_parts.append(normalize_text(page_text))
                        result["images"].append(
                            {
                                "page_index": page_index,
                                "path": str(image_path),
                                "status": "ok",
                                "text_chars": page_chars,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        error = str(exc)
                        page_errors.append({"page_index": page_index, "error": error, "image_path": str(image_path)})
                        result["images"].append(
                            {
                                "page_index": page_index,
                                "path": str(image_path),
                                "status": "error",
                                "error": error,
                            }
                        )
                result["sample_text"] = " ".join(sample_parts)[:sample_chars]
                result["text_extractable"] = result["text_chars"] > 0
                if page_errors:
                    result["page_errors"] = page_errors
                    result["page_error_count"] = len(page_errors)
                    if result["text_chars"] == 0:
                        result["status"] = "error"
                        result["error"] = page_errors[0]["error"]
        except Exception as exc:  # noqa: BLE001
            return error_method(str(exc), method=result["method"], images=result["images"])
    finally:
        if timeout_seconds > 0:
            signal.alarm(0)
            if previous_handler is not None:
                signal.signal(signal.SIGALRM, previous_handler)
    return result


def ocr_image(image_path: Path, *, lang: str, timeout_seconds: int) -> str:
    if pytesseract is None:
        raise RuntimeError("pytesseract is not installed")
    return str(pytesseract.image_to_string(str(image_path), lang=lang, timeout=timeout_seconds))


def peer_review_extractions(methods: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Compare extraction methods and choose the most useful text candidate."""
    summaries: Dict[str, Dict[str, Any]] = {}
    warnings: list[str] = []
    for name, method in methods.items():
        status = str(method.get("status") or "unknown")
        text_chars = int(method.get("text_chars") or 0)
        summaries[name] = {
            "status": status,
            "text_chars": text_chars,
            "text_extractable": bool(method.get("text_extractable")),
            "error": method.get("error"),
        }
        if status == "error":
            warnings.append(f"{name} failed: {method.get('error')}")
        elif status == "skipped":
            warnings.append(f"{name} skipped: {method.get('skip_reason')}")

    best_method = choose_best_method(methods)
    similarities = pairwise_similarities(methods)
    if best_method:
        best_chars = int(methods[best_method].get("text_chars") or 0)
        for name, method in methods.items():
            if name == best_method or method.get("status") != "ok":
                continue
            text_chars = int(method.get("text_chars") or 0)
            if best_chars and text_chars < best_chars * 0.3:
                warnings.append(f"{name} produced much less text than {best_method}")
    else:
        warnings.append("no successful text extraction method")

    return {
        "best_text_method": best_method,
        "peer_summaries": summaries,
        "pairwise_sample_similarity": similarities,
        "warnings": warnings,
    }


def decide_extraction(peers: Dict[str, Dict[str, Any]], review: Dict[str, Any]) -> Dict[str, Any]:
    best_method = review.get("best_text_method")
    text_layer_methods = ("pdf_text", "markitdown")
    text_layer_found = any(
        peers.get(name, {}).get("status") == "ok" and int(peers.get(name, {}).get("text_chars") or 0) > 0
        for name in text_layer_methods
    )
    ocr_found = peers.get("image_ocr", {}).get("status") == "ok" and int(peers.get("image_ocr", {}).get("text_chars") or 0) > 0
    if text_layer_found:
        return {
            "text_extractable": True,
            "preferred_text_source": best_method,
            "needs_ocr": False,
            "reason": "text layer or markdown conversion produced text",
        }
    if ocr_found:
        return {
            "text_extractable": True,
            "preferred_text_source": "image_ocr",
            "needs_ocr": True,
            "reason": "only image OCR produced text",
        }
    return {
        "text_extractable": False,
        "preferred_text_source": None,
        "needs_ocr": True,
        "reason": "no peer produced text",
    }


def choose_best_method(methods: Dict[str, Dict[str, Any]]) -> str | None:
    candidates = [
        (name, int(method.get("text_chars") or 0))
        for name, method in methods.items()
        if method.get("status") == "ok" and int(method.get("text_chars") or 0) > 0
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[1], method_preference(item[0])), reverse=True)
    return candidates[0][0]


def method_preference(name: str) -> int:
    return {"markitdown": 3, "pdf_text": 2, "image_ocr": 1}.get(name, 0)


def pairwise_similarities(methods: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    samples = {
        name: normalize_for_similarity(str(method.get("sample_text") or ""))
        for name, method in methods.items()
        if method.get("status") == "ok" and method.get("sample_text")
    }
    result: Dict[str, float] = {}
    names = sorted(samples)
    for left_index, left in enumerate(names):
        for right in names[left_index + 1 :]:
            result[f"{left}:{right}"] = round(difflib.SequenceMatcher(None, samples[left], samples[right]).ratio(), 4)
    return result


def generate_source_extraction_peer_review(
    source: str,
    *,
    artifacts_root: Path = Path("artifacts"),
    limit: int | None = None,
    max_pages: int | None = 1,
    workers: int = 1,
    force: bool = False,
    include_non_completed: bool = False,
    run_markitdown: bool = True,
    run_ocr: bool = True,
    ocr_lang: str = "kor+eng",
    ocr_dpi: int = 200,
    timeout_seconds: int = 30,
    progress_every: int = 0,
) -> Dict[str, Any]:
    """Run peer-review extraction sidecars for one artifact source."""
    if source not in SOURCE_NAMES:
        raise ValueError(f"지원하지 않는 source입니다: {source}")

    source_root = artifacts_root / source
    item_metadata_dir = source_root / "metadata" / "items"
    output_dir = source_root / "extraction_peer_review"
    output_items_dir = output_dir / "items"
    output_images_dir = output_dir / "images"
    output_items_dir.mkdir(parents=True, exist_ok=True)
    output_images_dir.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "source": source,
        "item_metadata_dir": str(item_metadata_dir),
        "output_dir": str(output_dir),
        "started_at": iso_now(),
        "total_items": 0,
        "eligible": 0,
        "processed": 0,
        "skipped_existing": 0,
        "skipped_not_completed": 0,
        "skipped_missing_pdf_path": 0,
        "json_errors": 0,
        "errors": 0,
        "images_saved": 0,
        "settings": {
            "max_pages": max_pages,
            "workers": workers,
            "include_non_completed": include_non_completed,
            "run_markitdown": run_markitdown,
            "run_ocr": run_ocr,
            "ocr_lang": ocr_lang,
            "ocr_dpi": ocr_dpi,
            "timeout_seconds": timeout_seconds,
        },
    }
    index: Dict[str, Dict[str, Any]] = {}
    work_items: list[Dict[str, Any]] = []

    for item_path in iter_item_paths(item_metadata_dir):
        summary["total_items"] += 1
        rel_key = item_path.relative_to(item_metadata_dir).with_suffix("").as_posix()
        sidecar_path = output_items_dir / f"{rel_key}.json"
        if sidecar_path.exists() and not force:
            summary["skipped_existing"] += 1
            existing = read_json(sidecar_path)
            if isinstance(existing, dict):
                index[rel_key] = compact_index_metadata(existing, sidecar_path, artifacts_root)
            continue

        item = read_json(item_path)
        if not isinstance(item, dict):
            summary["json_errors"] += 1
            continue

        pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
        if not include_non_completed and str((pdf or {}).get("status") or "") != "completed":
            summary["skipped_not_completed"] += 1
            continue

        pdf_path_text = str((pdf or {}).get("path") or "").strip()
        if not pdf_path_text:
            summary["skipped_missing_pdf_path"] += 1
            continue

        summary["eligible"] += 1
        work_items.append(
            {
                "source": source,
                "rel_key": rel_key,
                "item_path": str(item_path),
                "pdf_path": str(resolve_path(pdf_path_text, artifacts_root)),
                "pdf_path_text": pdf_path_text,
                "sidecar_path": str(sidecar_path),
                "image_output_dir": str(output_images_dir / rel_key),
                "max_pages": max_pages,
                "run_markitdown": run_markitdown,
                "run_ocr": run_ocr,
                "ocr_lang": ocr_lang,
                "ocr_dpi": ocr_dpi,
                "timeout_seconds": timeout_seconds,
            }
        )
        if limit is not None and len(work_items) >= limit:
            break

    best_method_counts: Counter[str] = Counter()
    for result in bounded_process(work_items, workers):
        rel_key = str(result["rel_key"])
        sidecar_path = Path(str(result["sidecar_path"]))
        metadata = result["metadata"]
        write_json(sidecar_path, metadata)
        index[rel_key] = compact_index_metadata(metadata, sidecar_path, artifacts_root)
        summary["processed"] += 1
        if metadata.get("status") == "error":
            summary["errors"] += 1
        image_ocr = (metadata.get("peers") or {}).get("image_ocr") or {}
        summary["images_saved"] += len(image_ocr.get("images") or [])
        best_method = ((metadata.get("review") or {}).get("best_text_method") or "none")
        best_method_counts[str(best_method)] += 1
        if progress_every and summary["processed"] % progress_every == 0:
            print(
                "source={source} processed={processed} eligible={eligible} errors={errors} images_saved={images_saved}".format(
                    **summary
                ),
                flush=True,
            )

    summary["by_best_text_method"] = dict(sorted(best_method_counts.items()))
    summary["completed_at"] = iso_now()
    write_json(output_dir / "metadata.json", index)
    write_json(output_dir / "summary.json", summary)
    return summary


def process_work_item(work_item: Dict[str, Any]) -> Dict[str, Any]:
    metadata = analyze_pdf_extraction_peer_review(
        Path(str(work_item["pdf_path"])),
        image_output_dir=Path(str(work_item["image_output_dir"])),
        max_pages=work_item.get("max_pages"),
        timeout_seconds=int(work_item.get("timeout_seconds") or 30),
        ocr_lang=str(work_item.get("ocr_lang") or "kor+eng"),
        ocr_dpi=int(work_item.get("ocr_dpi") or 200),
        run_markitdown=bool(work_item.get("run_markitdown")),
        run_ocr=bool(work_item.get("run_ocr")),
    )
    metadata.update(
        {
            "source": work_item["source"],
            "pdf_key": work_item["rel_key"],
            "pdf_path": work_item["pdf_path_text"],
            "resolved_pdf_path": work_item["pdf_path"],
            "item_path": work_item["item_path"],
            "image_output_dir": work_item["image_output_dir"],
        }
    )
    return {
        "rel_key": work_item["rel_key"],
        "sidecar_path": work_item["sidecar_path"],
        "metadata": metadata,
    }


def bounded_process(work_items: list[Dict[str, Any]], workers: int) -> Iterator[Dict[str, Any]]:
    if workers <= 1:
        for work_item in work_items:
            yield process_work_item(work_item)
        return

    iterator = iter(work_items)
    max_pending = max(workers * 2, workers)
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
        pending: set[concurrent.futures.Future[Dict[str, Any]]] = set()
        for _ in range(min(max_pending, len(work_items))):
            work_item = next(iterator, None)
            if work_item is None:
                break
            pending.add(executor.submit(process_work_item, work_item))

        while pending:
            done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                yield future.result()
                work_item = next(iterator, None)
                if work_item is not None:
                    pending.add(executor.submit(process_work_item, work_item))


def iter_item_paths(item_metadata_dir: Path) -> Iterator[Path]:
    if not item_metadata_dir.exists():
        return iter(())
    return (path for path in sorted(item_metadata_dir.rglob("*.json")) if path.is_file())


def compact_index_metadata(metadata: Dict[str, Any], sidecar_path: Path, artifacts_root: Path) -> Dict[str, Any]:
    review = metadata.get("review") if isinstance(metadata.get("review"), dict) else {}
    peers = metadata.get("peers") if isinstance(metadata.get("peers"), dict) else {}
    decision = metadata.get("decision") if isinstance(metadata.get("decision"), dict) else {}
    return {
        "status": metadata.get("status"),
        "source": metadata.get("source"),
        "pdf_key": metadata.get("pdf_key"),
            "pdf_path": metadata.get("pdf_path"),
            "sidecar_path": relative_to_artifacts_parent(sidecar_path, artifacts_root),
            "analysis_scope": metadata.get("analysis_scope"),
            "best_text_method": review.get("best_text_method"),
            "decision": decision,
        "peer_summaries": review.get("peer_summaries"),
        "image_count": len(((peers.get("image_ocr") or {}).get("images") or [])),
        "generated_at": metadata.get("generated_at"),
        "error": metadata.get("error"),
    }


def resolve_path(path_text: str, artifacts_root: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    candidates = [
        (artifacts_root.parent / path).resolve(),
        (Path.cwd() / path).resolve(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def relative_to_artifacts_parent(path: Path, artifacts_root: Path) -> str:
    try:
        return str(path.relative_to(artifacts_root.parent))
    except ValueError:
        return str(path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp_path.replace(path)


def skipped_method(reason: str) -> Dict[str, Any]:
    return {"status": "skipped", "skip_reason": reason, "text_extractable": False, "text_chars": 0}


def error_method(error: str, *, method: str, images: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": "error",
        "method": method,
        "error": error,
        "text_extractable": False,
        "text_chars": 0,
        "sample_text": "",
    }
    if images is not None:
        result["images"] = images
    return result


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def analysis_scope(max_pages: int | None) -> str:
    return "all_pages" if max_pages is None else f"first_{max_pages}_pages"


def normalize_for_similarity(text: str) -> str:
    return re.sub(r"\W+", "", text).lower()[:2000]


def iso_now() -> str:
    return datetime.now().isoformat()


def _raise_timeout(_signum: int, _frame: Any) -> None:
    raise TimeoutError("PDF extraction peer review timed out")
