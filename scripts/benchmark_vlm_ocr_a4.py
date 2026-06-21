#!/usr/bin/env python3
"""Benchmark A4 250dpi single-page VLM OCR throughput and output quality."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recover_ocr_needed_with_vlm import (  # noqa: E402
    A4_250DPI_HEIGHT,
    DEFAULT_MODEL_ID,
    extract_json_object,
    image_data_url,
    image_size,
    iter_ocr_needed_items,
    jsonable,
    normalize_text,
    parse_sources,
    render_pdf_page,
    resolve_path,
    write_json,
)


@dataclass(frozen=True)
class BenchPage:
    item_path: Path
    pdf_path: Path
    source: str
    page: int
    image_path: Path
    width: int
    height: int
    render_sec: float
    encode_sec: float
    image_url: str


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_csv_ints(value: str) -> list[int]:
    values = [int(part.strip()) for part in value.split(",") if part.strip()]
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("expected comma-separated positive integers")
    return values


def source_from_path(path: Path) -> str:
    parts = path.parts
    if "searchThema" in parts:
        return "searchThema"
    if "pety" in parts:
        return "pety"
    return "unknown"


def load_item(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"item is not an object: {path}")
    return data


def select_items(args: argparse.Namespace, repo_root: Path) -> list[tuple[Path, Path, int]]:
    artifacts_root = (repo_root / args.artifacts_root).resolve()
    candidates = iter_ocr_needed_items(artifacts_root, parse_sources(args.source))
    selected: list[tuple[Path, Path, int]] = []
    for item_path in candidates:
        try:
            item = load_item(item_path)
        except Exception:
            continue
        pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
        pdf_path_text = str(pdf.get("path") or "").strip()
        if not pdf_path_text:
            continue
        pdf_path = resolve_path(pdf_path_text, repo_root)
        if not pdf_path.exists():
            continue
        pdf_text = item.get("pdf_text") if isinstance(item.get("pdf_text"), dict) else {}
        pages_total = int(pdf_text.get("pages") or 0)
        pages_to_process = min(args.max_pages, pages_total) if pages_total > 0 else args.max_pages
        if pages_to_process <= 0:
            continue
        selected.append((item_path, pdf_path, pages_to_process))
        if args.limit is not None and len(selected) >= args.limit:
            break
    return selected


def prepare_pages(args: argparse.Namespace, repo_root: Path, work_dir: Path) -> list[BenchPage]:
    pages: list[BenchPage] = []
    for item_path, pdf_path, pages_to_process in select_items(args, repo_root):
        source = source_from_path(item_path)
        item_dir = work_dir / source / item_path.stem
        item_dir.mkdir(parents=True, exist_ok=True)
        for page in range(1, pages_to_process + 1):
            started = time.perf_counter()
            image_path = render_pdf_page(pdf_path, page, item_dir, dpi=args.dpi)
            render_sec = time.perf_counter() - started
            width, height = image_size(image_path)
            started = time.perf_counter()
            data_url = image_data_url(image_path, max_side=args.max_side)
            encode_sec = time.perf_counter() - started
            pages.append(
                BenchPage(
                    item_path=item_path,
                    pdf_path=pdf_path,
                    source=source,
                    page=page,
                    image_path=image_path,
                    width=width,
                    height=height,
                    render_sec=render_sec,
                    encode_sec=encode_sec,
                    image_url=data_url,
                )
            )
            if args.total_pages is not None and len(pages) >= args.total_pages:
                return pages
    return pages


def prompt_for_page(page: BenchPage, dpi: int) -> str:
    schema = '{"text":"...","confidence":0.0,"notes":"..."}'
    return (
        "You are doing OCR for a Korean government gazette scanned page. "
        "Transcribe only visible text in natural reading order. Preserve Korean Hangul/Hanja, "
        "digits, punctuation, dates, list markers, table cell text, and line breaks when clear. "
        "Do not infer text that is not visible. Return exactly one JSON object with this schema: "
        f"{schema}. Do not add commentary.\n\n"
        f"Image context: page={page.page}, dpi={dpi}, page_pixels={page.width}x{page.height}, "
        f"bbox=0,0,{page.width},{page.height}. This is a full-page A4 250dpi-capable render."
    )


def build_payload(page: BenchPage, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "model": args.model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_for_page(page, args.dpi)},
                    {"type": "image_url", "image_url": {"url": page.image_url}},
                ],
            }
        ],
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
        "seed": args.seed + page.page,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def message_content(data: dict[str, Any]) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return content.strip() if isinstance(content, str) else ""


def text_quality_metrics(text: str) -> dict[str, Any]:
    normalized = normalize_text(text)
    chars = len(normalized)
    hangul = sum(1 for char in normalized if "\uac00" <= char <= "\ud7a3")
    hanja = sum(1 for char in normalized if "\u4e00" <= char <= "\u9fff")
    digits = sum(1 for char in normalized if char.isdigit())
    whitespace = sum(1 for char in normalized if char.isspace())
    replacement = normalized.count("\ufffd")
    cid_markers = normalized.count("(cid:")
    printable = sum(1 for char in normalized if char.isprintable() or char.isspace())
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    unique_lines = len(set(lines))
    duplicate_line_ratio = 0.0 if not lines else 1.0 - (unique_lines / len(lines))
    suspect = (
        chars == 0
        or replacement > 0
        or cid_markers > 0
        or (chars >= 100 and hangul / max(1, chars - whitespace) < 0.05)
        or duplicate_line_ratio > 0.45
    )
    return {
        "chars": chars,
        "lines": len(lines),
        "hangul_chars": hangul,
        "hanja_chars": hanja,
        "digits": digits,
        "hangul_ratio": round(hangul / max(1, chars - whitespace), 4),
        "printable_ratio": round(printable / max(1, chars), 4),
        "replacement_chars": replacement,
        "cid_markers": cid_markers,
        "duplicate_line_ratio": round(duplicate_line_ratio, 4),
        "suspect": suspect,
    }


def run_one(page: BenchPage, args: argparse.Namespace, concurrency: int) -> dict[str, Any]:
    url = f"{args.endpoint_url.rstrip('/')}/v1/chat/completions"
    payload = build_payload(page, args)
    started = time.perf_counter()
    try:
        response = post_json(url, payload, timeout=args.timeout)
        latency_sec = time.perf_counter() - started
        raw = message_content(response)
        parsed = extract_json_object(raw) if raw else {}
        text = normalize_text(str(parsed.get("text", "")).strip()) if parsed else ""
        confidence = parsed.get("confidence", 0.0) if parsed else 0.0
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        return {
            "status": "ok" if text else "empty",
            "error": "",
            "source": page.source,
            "item_path": str(page.item_path),
            "pdf_path": str(page.pdf_path),
            "page": page.page,
            "image_path": str(page.image_path),
            "image_width": page.width,
            "image_height": page.height,
            "render_sec": round(page.render_sec, 3),
            "encode_sec": round(page.encode_sec, 3),
            "concurrency": concurrency,
            "latency_sec": round(latency_sec, 3),
            "parse_ok": bool(parsed),
            "confidence": max(0.0, min(1.0, confidence_value)),
            "notes": str(parsed.get("notes", "")).strip()[:500] if parsed else "",
            "quality": text_quality_metrics(text),
            "sample_text": text[: args.sample_chars],
            "raw_tail": raw[-1000:] if args.include_raw else "",
        }
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "source": page.source,
            "item_path": str(page.item_path),
            "pdf_path": str(page.pdf_path),
            "page": page.page,
            "image_path": str(page.image_path),
            "image_width": page.width,
            "image_height": page.height,
            "render_sec": round(page.render_sec, 3),
            "encode_sec": round(page.encode_sec, 3),
            "concurrency": concurrency,
            "latency_sec": round(time.perf_counter() - started, 3),
            "parse_ok": False,
            "confidence": 0.0,
            "quality": text_quality_metrics(""),
            "sample_text": "",
            "raw_tail": "",
        }


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[max(0, min(99, int(q) - 1))]


def summarize(records: list[dict[str, Any]], elapsed_sec: float) -> dict[str, Any]:
    latencies = [float(record.get("latency_sec") or 0.0) for record in records if record.get("latency_sec")]
    ok_records = [record for record in records if record.get("status") == "ok"]
    chars = [int(record.get("quality", {}).get("chars") or 0) for record in ok_records]
    hangul_ratios = [float(record.get("quality", {}).get("hangul_ratio") or 0.0) for record in ok_records]
    confidences = [float(record.get("confidence") or 0.0) for record in ok_records]
    return {
        "requests": len(records),
        "ok": len(ok_records),
        "empty": sum(1 for record in records if record.get("status") == "empty"),
        "errors": sum(1 for record in records if record.get("status") == "error"),
        "parse_ok": sum(1 for record in records if record.get("parse_ok") is True),
        "suspect": sum(1 for record in ok_records if record.get("quality", {}).get("suspect") is True),
        "elapsed_sec": round(elapsed_sec, 3),
        "pages_per_min": round((len(records) / elapsed_sec) * 60.0, 3) if elapsed_sec > 0 else 0.0,
        "latency_avg_sec": round(statistics.fmean(latencies), 3) if latencies else 0.0,
        "latency_p50_sec": round(percentile(latencies, 50), 3),
        "latency_p90_sec": round(percentile(latencies, 90), 3),
        "chars_avg": round(statistics.fmean(chars), 1) if chars else 0.0,
        "confidence_avg": round(statistics.fmean(confidences), 3) if confidences else 0.0,
        "hangul_ratio_avg": round(statistics.fmean(hangul_ratios), 4) if hangul_ratios else 0.0,
    }


def run_concurrency(pages: list[BenchPage], args: argparse.Namespace, concurrency: int) -> dict[str, Any]:
    started = time.perf_counter()
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(run_one, page, args, concurrency) for page in pages]
        for future in as_completed(futures):
            records.append(future.result())
    elapsed_sec = time.perf_counter() - started
    records.sort(key=lambda item: (str(item.get("item_path")), int(item.get("page") or 0)))
    return {
        "concurrency": concurrency,
        "summary": summarize(records, elapsed_sec),
        "records": records,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="all", help="all, pety, searchThema, or comma-separated sources")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/validation"))
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--endpoint-url", default="http://127.0.0.1:30000")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--limit", type=int, default=2, help="number of OCR-needed PDF items to sample")
    parser.add_argument("--total-pages", type=int, default=None, help="cap prepared pages across all items")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--dpi", type=int, default=250)
    parser.add_argument("--max-side", type=int, default=A4_250DPI_HEIGHT)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=420.0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--concurrency-values", type=parse_csv_ints, default=[1])
    parser.add_argument("--sample-chars", type=int, default=1200)
    parser.add_argument("--include-raw", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_pages <= 0:
        raise SystemExit("--max-pages must be positive")
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.total_pages is not None and args.total_pages <= 0:
        raise SystemExit("--total-pages must be positive")

    repo_root = Path.cwd().resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    work_dir = (repo_root / args.work_dir).resolve() if args.work_dir else output_dir / f"vlm_ocr_bench_images_{utc_stamp()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        models = get_json(f"{args.endpoint_url.rstrip('/')}/v1/models", timeout=min(args.timeout, 30.0))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"VLM endpoint health check failed: {args.endpoint_url}: {exc}") from exc

    prepared_started = time.perf_counter()
    pages = prepare_pages(args, repo_root, work_dir)
    prepare_sec = time.perf_counter() - prepared_started
    if not pages:
        raise SystemExit("no OCR-needed PDF pages selected")

    runs = []
    for concurrency in args.concurrency_values:
        print(f"benchmark concurrency={concurrency} pages={len(pages)}", flush=True)
        runs.append(run_concurrency(pages, args, concurrency))

    report = {
        "created_at": iso_now(),
        "settings": jsonable(vars(args)),
        "endpoint_models": models,
        "prepared_pages": len(pages),
        "prepare_sec": round(prepare_sec, 3),
        "work_dir": str(work_dir),
        "runs": runs,
    }
    report_path = output_dir / f"vlm_ocr_a4_benchmark_{utc_stamp()}.json"
    write_json(report_path, report)
    compact = {
        "report": str(report_path),
        "prepared_pages": len(pages),
        "prepare_sec": report["prepare_sec"],
        "summaries": [
            {"concurrency": run["concurrency"], **run["summary"]}
            for run in runs
        ],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
