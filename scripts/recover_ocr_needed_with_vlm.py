#!/usr/bin/env python3
"""Recover OCR-needed Gwanbo PDFs with VLM OCR and optional CLI peer review."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.metadata_schema import apply_item_schema


SOURCE_NAMES = ("pety", "searchThema")
DEFAULT_MODEL_ID = "olberdingbrands/Qwen3.6-35B-A3B-AWQ"
DEFAULT_OPENCODE_MODEL_ID = "opencode/nemotron-3-ultra-free"
A4_250DPI_WIDTH = 2480
A4_250DPI_HEIGHT = 3508


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def parse_sources(value: str) -> set[str]:
    if value == "all":
        return set(SOURCE_NAMES)
    return {part.strip() for part in value.split(",") if part.strip()}


def iter_ocr_needed_items(artifacts_root: Path, sources: set[str]) -> list[Path]:
    paths: list[Path] = []
    for source in SOURCE_NAMES:
        if source not in sources:
            continue
        root = artifacts_root / source / "metadata" / "items"
        if root.exists():
            paths.extend(sorted(root.rglob("*.json")))
    selected: list[Path] = []
    for path in paths:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            selected.append(path)
            continue
        pdf_text = item.get("pdf_text") if isinstance(item.get("pdf_text"), dict) else {}
        if pdf_text.get("needs_ocr") is True:
            selected.append(path)
    return selected


def source_from_item_path(path: Path) -> str:
    parts = path.parts
    if "searchThema" in parts:
        return "searchThema"
    if "pety" in parts:
        return "pety"
    return "unknown"


def resolve_path(path_text: str, repo_root: Path) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else repo_root / path


def recovery_scope(args: argparse.Namespace) -> str:
    return f"first_{args.max_pages}_pages_{args.dpi}dpi_single_page_maxside{args.max_side}"


def effective_model_id(args: argparse.Namespace) -> str:
    if args.ocr_backend == "opencode_cli":
        return str(args.opencode_model)
    return str(args.model_id)


def existing_recovery_current(item: dict[str, Any], args: argparse.Namespace) -> bool:
    ocr = item.get("ocr") if isinstance(item.get("ocr"), dict) else {}
    recovery = ocr.get("vlm_recovery") if isinstance(ocr.get("vlm_recovery"), dict) else {}
    if recovery.get("status") not in {"recovered", "partial", "unrecovered"}:
        return False
    if recovery.get("engine") != args.ocr_backend:
        return False
    if recovery.get("model_id") != effective_model_id(args):
        return False
    if recovery.get("analysis_scope") != recovery_scope(args):
        return False
    return bool(recovery.get("pages"))


def render_pdf_page(pdf_path: Path, page_number: int, output_dir: Path, dpi: int) -> Path:
    output_prefix = output_dir / f"page_{page_number:04d}"
    cmd = [
        "pdftoppm",
        "-f",
        str(page_number),
        "-l",
        str(page_number),
        "-r",
        str(dpi),
        "-png",
        str(pdf_path),
        str(output_prefix),
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or f"pdftoppm exit {completed.returncode}"
        raise RuntimeError(message)
    candidates = sorted(output_dir.glob(f"{output_prefix.name}-*.png"))
    if not candidates:
        raise RuntimeError("pdftoppm produced no image")
    return candidates[0]


def image_data_url(path: Path, max_side: int) -> str:
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        if max(image.size) > max_side:
            ratio = max_side / float(max(image.size))
            size = (max(1, int(round(image.width * ratio))), max(1, int(round(image.height * ratio))))
            image = image.resize(size, Image.Resampling.LANCZOS)
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            image.save(tmp.name, format="PNG")
            data = Path(tmp.name).read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def cli_attachment_image(path: Path, max_side: int) -> tuple[Path, Path | None]:
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        if max(image.size) <= max_side:
            return path, None
        ratio = max_side / float(max(image.size))
        size = (max(1, int(round(image.width * ratio))), max(1, int(round(image.height * ratio))))
        image = image.resize(size, Image.Resampling.LANCZOS)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            temp_path = Path(tmp.name)
        image.save(temp_path, format="PNG")
    return temp_path, temp_path


def image_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as image:
        return image.size


def page_ocr_images(page_image: Path, output_dir: Path, args: argparse.Namespace) -> tuple[str, list[dict[str, Any]]]:
    width, height = image_size(page_image)
    return "single_page", [
        {
            "page_image": 1,
            "bbox": [0, 0, width, height],
            "image_path": page_image,
        }
    ]


def openai_chat_completion(endpoint_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"{endpoint_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def message_content(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return content.strip() if isinstance(content, str) else ""


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_text(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def ocr_prompt(context: str = "") -> str:
    prompt = (
        "You are doing OCR for a Korean government gazette scanned page. "
        "Transcribe only visible text in natural reading order. Preserve Korean Hangul/Hanja, "
        "digits, punctuation, dates, list markers, table cell text, and line breaks when clear. "
        "Do not infer text that is not visible. Return exactly one JSON object with this schema: "
        "{\"text\":\"...\",\"confidence\":0.0,\"notes\":\"...\"}. Do not add commentary."
    )
    if context:
        prompt = f"{prompt}\n\nImage context: {context}"
    return prompt


def ocr_result_from_response(raw: str, *, engine: str, model_id: str) -> dict[str, Any]:
    parsed = extract_json_object(raw) if raw else {}
    text = normalize_text(str(parsed.get("text", "")).strip())
    confidence = parsed.get("confidence", 0.0)
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.0
    return {
        "engine": engine,
        "model_id": model_id,
        "text": text,
        "confidence": max(0.0, min(1.0, confidence_value)),
        "notes": str(parsed.get("notes", "")).strip(),
        "raw_response": raw[-4000:] if raw else "",
        "status": "ok" if text else "empty",
    }


def qwen_ocr_page(
    image_path: Path,
    *,
    endpoint_url: str,
    model_id: str,
    timeout: float,
    max_tokens: int,
    seed: int,
    max_side: int,
    context: str = "",
) -> dict[str, Any]:
    prompt = ocr_prompt(context)
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url(image_path, max_side=max_side)}},
                ],
            }
        ],
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": max_tokens,
        "seed": seed,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    raw = message_content(openai_chat_completion(endpoint_url, payload, timeout))
    return ocr_result_from_response(raw, engine="qwen_vllm", model_id=model_id)


def opencode_ocr_page(
    image_path: Path,
    *,
    model_id: str,
    timeout: float,
    max_side: int,
    context: str = "",
) -> dict[str, Any]:
    attachment_path, cleanup_path = cli_attachment_image(image_path, max_side=max_side)
    prompt = "\n".join([ocr_prompt(context), "", f"Image path: {attachment_path.resolve()}"])
    command = [
        "opencode",
        "run",
        "-m",
        model_id,
        "--file",
        str(attachment_path),
        "--",
        prompt,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "engine": "opencode_cli",
            "model_id": model_id,
            "text": "",
            "confidence": 0.0,
            "notes": "",
            "raw_response": "",
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)
    result = ocr_result_from_response(completed.stdout, engine="opencode_cli", model_id=model_id)
    if completed.returncode != 0:
        result.update(
            {
                "status": "error",
                "returncode": completed.returncode,
                "stdout": completed.stdout[-2000:],
                "stderr": completed.stderr[-2000:],
            }
        )
    return result


def run_primary_ocr_page(
    image_path: Path,
    *,
    args: argparse.Namespace,
    page_number: int,
    context: str = "",
) -> dict[str, Any]:
    if args.ocr_backend == "opencode_cli":
        return opencode_ocr_page(
            image_path,
            model_id=args.opencode_model,
            timeout=args.opencode_timeout,
            max_side=args.max_side,
            context=context,
        )
    return qwen_ocr_page(
        image_path,
        endpoint_url=args.endpoint_url,
        model_id=args.model_id,
        timeout=args.qwen_timeout,
        max_tokens=args.max_tokens,
        seed=args.seed + page_number,
        max_side=args.max_side,
        context=context,
    )


def peer_prompt(ocr_text: str, image_path: Path, context: str = "") -> str:
    return "\n".join(
        [
            "You are peer-verifying Korean OCR from a scanned government gazette page.",
            "Compare the OCR text against the attached/local image. Do not transcribe the whole image unless needed.",
            "Return exactly one JSON object: {\"verdict\":\"accept|revise|reject\",\"corrected_text\":\"...\",\"issues\":[\"...\"],\"confidence\":0.0}.",
            "Use accept when the OCR is good enough. Use revise only when you can correct visible errors.",
            "",
            f"Image context: {context}" if context else "",
            f"Image path: {image_path.resolve()}",
            f"![page]({image_path.resolve()})",
            "",
            "Primary OCR text:",
            ocr_text[:12000],
        ]
    )


def run_peer_cli(peer: str, image_path: Path, ocr_text: str, timeout: float, context: str = "") -> dict[str, Any]:
    prompt = peer_prompt(ocr_text, image_path, context=context)
    if peer == "agy":
        command = ["agy", "-p", prompt, "--print-timeout", f"{max(1, int(round(timeout)))}s"]
    elif peer == "codex":
        command = [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "-i",
            str(image_path),
            "--",
            prompt,
        ]
    elif peer == "claude":
        command = [
            "claude",
            "-p",
            prompt,
            "--permission-mode",
            "dontAsk",
            "--add-dir",
            str(image_path.parent),
        ]
    else:
        return {"status": "skipped", "error": f"unknown peer: {peer}"}
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    parsed = extract_json_object(completed.stdout)
    if completed.returncode != 0:
        return {
            "status": "error",
            "returncode": completed.returncode,
            "stdout": completed.stdout[-2000:],
            "stderr": completed.stderr[-2000:],
        }
    return {
        "status": "ok" if parsed else "unparsed",
        "verdict": parsed.get("verdict") if parsed else "",
        "corrected_text": normalize_text(str(parsed.get("corrected_text", "")).strip()) if parsed else "",
        "issues": parsed.get("issues", []) if parsed else [],
        "confidence": parsed.get("confidence", 0.0) if parsed else 0.0,
        "stdout": completed.stdout[-2000:] if not parsed else "",
    }


def choose_final_text(qwen_result: dict[str, Any], peer_results: dict[str, dict[str, Any]]) -> tuple[str, str]:
    revisions = [
        result.get("corrected_text", "")
        for result in peer_results.values()
        if result.get("status") == "ok" and result.get("verdict") == "revise" and result.get("corrected_text")
    ]
    if revisions:
        return str(revisions[0]), "peer_revision"
    engine = str(qwen_result.get("engine") or "primary")
    source = "qwen_primary" if engine == "qwen_vllm" else f"{engine}_primary"
    return str(qwen_result.get("text", "")), source


def page_image_context(page_number: int, page_image: dict[str, Any], page_width: int, page_height: int, dpi: int) -> str:
    x0, y0, x1, y1 = page_image["bbox"]
    return (
        f"page={page_number}, dpi={dpi}, page_pixels={page_width}x{page_height}, "
        f"bbox={x0},{y0},{x1},{y1}. This is a full-page 250dpi-capable render."
    )


def process_item(path: Path, args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    source = source_from_item_path(path)
    result: dict[str, Any] = {"item_path": str(path), "source": source, "status": "unknown"}
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {**result, "status": "json_error", "error": str(exc)}
    if not isinstance(item, dict):
        return {**result, "status": "json_error", "error": "item is not object"}
    pdf_text = item.get("pdf_text") if isinstance(item.get("pdf_text"), dict) else {}
    if pdf_text.get("needs_ocr") is not True:
        return {**result, "status": "skipped_not_ocr_needed"}
    if not args.force and existing_recovery_current(item, args):
        return {**result, "status": "skipped_existing"}
    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    pdf_path_text = str(pdf.get("path") or "").strip()
    if not pdf_path_text:
        return {**result, "status": "missing_pdf_path"}
    pdf_path = resolve_path(pdf_path_text, repo_root)
    pages_total = int(pdf_text.get("pages") or 0)
    pages_to_process = min(args.max_pages, pages_total) if pages_total > 0 else args.max_pages
    recovery_pages: list[dict[str, Any]] = []
    peers = [peer.strip() for peer in args.peers.split(",") if peer.strip()]
    with tempfile.TemporaryDirectory(prefix="peti-vlm-ocr-") as temp:
        temp_dir = Path(temp)
        for page_number in range(1, pages_to_process + 1):
            page_record: dict[str, Any] = {"page": page_number, "status": "unknown"}
            try:
                image_path = render_pdf_page(pdf_path, page_number, temp_dir, dpi=args.dpi)
                page_width, page_height = image_size(image_path)
                page_mode, ocr_images = page_ocr_images(image_path, temp_dir / f"page_{page_number:04d}", args)
                image_records: list[dict[str, Any]] = []
                for page_image in ocr_images:
                    page_image_path = Path(page_image["image_path"])
                    context = page_image_context(page_number, page_image, page_width, page_height, args.dpi)
                    primary_ocr = run_primary_ocr_page(
                        page_image_path,
                        args=args,
                        page_number=page_number,
                        context=context,
                    )
                    peer_results = {
                        peer: run_peer_cli(
                            peer,
                            page_image_path,
                            primary_ocr.get("text", ""),
                            timeout=args.peer_timeout,
                            context=context,
                        )
                        for peer in peers
                        if primary_ocr.get("text")
                    }
                    final_text, final_source = choose_final_text(primary_ocr, peer_results)
                    image_record = {
                        "page_image": page_image["page_image"],
                        "bbox": page_image["bbox"],
                        "status": "recovered" if final_text else "empty",
                        "primary_ocr": primary_ocr,
                        "peers": peer_results,
                        "final_text": final_text,
                        "final_source": final_source,
                    }
                    image_record["opencode" if args.ocr_backend == "opencode_cli" else "qwen"] = primary_ocr
                    image_records.append(image_record)
                final_text = normalize_text(
                    "\n".join(record.get("final_text", "") for record in image_records if record.get("final_text"))
                )
                final_source = image_records[0].get("final_source", "") if image_records else ""
                page_record.update(
                    {
                        "status": "recovered" if final_text else "empty",
                        "render": {
                            "dpi": args.dpi,
                            "width": page_width,
                            "height": page_height,
                            "a4_250dpi_reference": {
                                "width": A4_250DPI_WIDTH,
                                "height": A4_250DPI_HEIGHT,
                            },
                        },
                        "page_ocr": {
                            "mode": page_mode,
                            "image_count": len(image_records),
                            "input_max_side": args.max_side,
                        },
                        "images": image_records,
                        "final_text": final_text,
                        "final_source": final_source,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                page_record.update({"status": "error", "error": str(exc)})
            recovery_pages.append(page_record)
    recovered_text = "\n\n".join(page.get("final_text", "") for page in recovery_pages if page.get("final_text")).strip()
    recovery = {
        "status": "recovered" if recovered_text else "unrecovered",
        "created_at": iso_now(),
        "engine": args.ocr_backend,
        "model_id": effective_model_id(args),
        "endpoint_url": args.endpoint_url if args.ocr_backend == "qwen_vllm" else "",
        "analysis_scope": recovery_scope(args),
        "pages_total": pages_total,
        "pages_processed": len(recovery_pages),
        "rendering": {
            "dpi": args.dpi,
            "page_ocr_mode": "single_page",
            "max_side": args.max_side,
            "a4_250dpi_reference": {
                "width": A4_250DPI_WIDTH,
                "height": A4_250DPI_HEIGHT,
            },
        },
        "peers": peers,
        "text": recovered_text,
        "pages": recovery_pages,
    }
    apply_item_schema(item, source_detail=source)
    ocr = item.setdefault("ocr", {})
    ocr["vlm_recovery"] = recovery
    if recovered_text:
        ocr["status"] = "vlm_recovered"
        ocr["skip_reason"] = ""
    else:
        ocr.setdefault("status", "pending")
    item["updated_at"] = iso_now()
    if not args.dry_run:
        write_json(path, item)
    return {
        **result,
        "status": "updated" if recovered_text else "updated_empty",
        "pages_processed": len(recovery_pages),
        "chars": len(recovered_text),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover OCR-needed PDFs with VLM OCR and CLI peer review.")
    parser.add_argument("--source", default="all", help="all, pety, searchThema, or comma-separated sources")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/validation"))
    parser.add_argument("--ocr-backend", choices=("opencode_cli", "qwen_vllm"), default="opencode_cli")
    parser.add_argument("--endpoint-url", default="http://127.0.0.1:30000", help="qwen_vllm OpenAI-compatible endpoint")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="qwen_vllm model id")
    parser.add_argument("--opencode-model", default=DEFAULT_OPENCODE_MODEL_ID, help="opencode CLI model id")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--dpi", type=int, default=250)
    parser.add_argument("--max-side", type=int, default=A4_250DPI_HEIGHT)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--qwen-timeout", type=float, default=420.0)
    parser.add_argument("--opencode-timeout", type=float, default=180.0)
    parser.add_argument("--peer-timeout", type=float, default=300.0)
    parser.add_argument("--peers", default="agy,codex", help="comma-separated: agy,codex,claude; empty disables peers")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--progress-every", type=int, default=1)
    args = parser.parse_args()
    if args.max_side <= 0:
        parser.error("--max-side must be positive")
    if args.ocr_backend == "opencode_cli" and not args.opencode_model.strip():
        parser.error("--opencode-model must be non-empty for opencode_cli")
    if args.dpi < 250:
        print(
            f"warning: dpi={args.dpi} is below A4 250dpi recovery target; use --dpi 250 for target coverage",
            file=sys.stderr,
            flush=True,
        )

    repo_root = Path.cwd().resolve()
    artifacts_root = (repo_root / args.artifacts_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = iter_ocr_needed_items(artifacts_root, parse_sources(args.source))
    if args.limit is not None:
        paths = paths[: args.limit]
    print(
        f"vlm ocr recovery started: {iso_now()} items={len(paths)} max_pages={args.max_pages} "
        f"dpi={args.dpi} page_ocr_mode=single_page max_side={args.max_side} "
        f"backend={args.ocr_backend} model={effective_model_id(args)} peers={args.peers}",
        flush=True,
    )

    counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = {"updated_empty": [], "json_error": [], "missing_pdf_path": []}
    for index, path in enumerate(paths, start=1):
        item_result = process_item(path, args, repo_root)
        status = str(item_result.get("status") or "unknown")
        counts["total"] += 1
        counts[status] += 1
        counts["chars"] += int(item_result.get("chars") or 0)
        if status in samples and len(samples[status]) < 20:
            samples[status].append(item_result)
        if args.progress_every and (index % args.progress_every == 0 or index == len(paths)):
            print(
                f"progress processed={index}/{len(paths)} updated={counts['updated']} "
                f"empty={counts['updated_empty']} existing={counts['skipped_existing']} chars={counts['chars']}",
                flush=True,
            )
    report = {
        "created_at": iso_now(),
        "settings": jsonable(vars(args)),
        "counts": dict(counts),
        "samples": samples,
    }
    report_path = output_dir / f"vlm_ocr_recovery_{utc_stamp()}.json"
    write_json(report_path, report)
    print(f"report={report_path}", flush=True)
    print(json.dumps({"counts": report["counts"]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
