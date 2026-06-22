#!/usr/bin/env python3
"""Run resumable VLM OCR batch jobs with separate primary/peer strategies."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recover_ocr_needed_with_vlm import (  # noqa: E402
    A4_250DPI_HEIGHT,
    DEFAULT_CLAUDE_MODEL_ID,
    DEFAULT_MODEL_ID,
    DEFAULT_OPENCODE_AGENT_ID,
    DEFAULT_OPENCODE_MODEL_ID,
    DEFAULT_QWEN_MIN_P,
    DEFAULT_QWEN_PRESENCE_PENALTY,
    DEFAULT_QWEN_TEMPERATURE,
    DEFAULT_QWEN_TOP_K,
    DEFAULT_QWEN_TOP_P,
    IMAGE_PREPROCESSORS,
    QWEN_API_PROFILES,
    QWEN_VL_250DPI_SHARP_PREPROCESSOR,
    choose_final_text,
    image_size,
    iter_ocr_needed_items,
    jsonable,
    normalize_text,
    ocr_prompt,
    ocr_result_from_response,
    claude_ocr_page,
    opencode_ocr_page,
    page_image_context,
    page_ocr_images,
    parse_sources,
    prepare_ocr_image_bytes,
    qwen_ocr_page,
    render_pdf_page,
    resolve_path,
    run_peer_cli,
    source_from_item_path,
    write_json,
)

PRIMARY_BACKENDS = ("qwen_vllm", "agy_cli", "opencode_cli", "claude_cli", "codex_cli")
AGY_FALLBACK_BACKENDS = ("none", "codex_cli", "opencode_cli", "claude_cli")
DEFAULT_AGY_AGENT_FILE = Path(".agy/agents/peti-ocr-primary.md")
DEFAULT_AGY_SKILL_FILE = Path(".agy/skills/peti-korean-ocr-primary/SKILL.md")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def item_result_path(output_dir: Path, repo_root: Path, item_path: Path) -> Path:
    try:
        rel = item_path.resolve().relative_to(repo_root).with_suffix("")
    except ValueError:
        rel = Path(item_path.name).with_suffix("")
    return output_dir / "item_results" / rel.with_suffix(".json")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def load_processed(checkpoint_path: Path, results_path: Path, *, retry_failed: bool) -> dict[str, str]:
    processed: dict[str, str] = {}
    if checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            for item_path, status in checkpoint.get("processed", {}).items():
                if retry_failed and status in {"error", "updated_empty"}:
                    continue
                processed[str(item_path)] = str(status)
        except Exception:
            pass
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_path = str(record.get("item_path") or "")
            status = str(record.get("status") or "")
            if not item_path:
                continue
            if retry_failed and status in {"error", "updated_empty"}:
                continue
            processed[item_path] = status
    return processed


def parse_partition_spec(spec: str) -> list[tuple[str, float]]:
    partitions: list[tuple[str, float]] = []
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"partition entry must be NAME:WEIGHT: {part}")
        name, raw_weight = part.split(":", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"partition name is empty in: {part}")
        try:
            weight = float(raw_weight)
        except ValueError as exc:
            raise ValueError(f"partition weight must be numeric in: {part}") from exc
        if weight <= 0:
            raise ValueError(f"partition weight must be positive in: {part}")
        partitions.append((name, weight))
    names = [name for name, _weight in partitions]
    if len(names) != len(set(names)):
        raise ValueError(f"partition names must be unique: {spec}")
    return partitions


def weighted_partition_paths(paths: list[Path], spec: str, partition_name: str) -> list[Path]:
    if not spec and not partition_name:
        return paths
    if not spec or not partition_name:
        raise ValueError("--partition-spec and --partition-name must be used together")
    partitions = parse_partition_spec(spec)
    if partition_name not in {name for name, _weight in partitions}:
        raise ValueError(f"--partition-name {partition_name!r} is not present in --partition-spec")

    total_weight = sum(weight for _name, weight in partitions)
    assigned = {name: 0 for name, _weight in partitions}
    selected: list[Path] = []
    for path in paths:
        next_total = sum(assigned.values()) + 1
        best_name = max(
            partitions,
            key=lambda part: (next_total * part[1] / total_weight) - assigned[part[0]],
        )[0]
        assigned[best_name] += 1
        if best_name == partition_name:
            selected.append(path)
    return selected


def prepared_cli_image(
    image_path: Path,
    output_dir: Path,
    *,
    max_side: int,
    preprocess: str,
    upscale: float,
) -> tuple[Path, dict[str, Any]]:
    data, metadata = prepare_ocr_image_bytes(
        image_path,
        max_side=max_side,
        preprocess=preprocess,
        upscale=upscale,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared_path = output_dir / "prepared.png"
    prepared_path.write_bytes(data)
    return prepared_path, metadata


def read_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def agy_primary_prompt(
    image_path: Path,
    *,
    context: str,
    agent_file: Path,
    skill_file: Path,
) -> str:
    agent_text = read_optional_text(agent_file)
    skill_text = read_optional_text(skill_file)
    sections = [
        "You are running as the peti-ocr-primary Agy agent.",
        "",
        "Task: OCR the local Korean Gwanbo page image and return exactly one JSON object.",
        "",
        f"Image path: {image_path.resolve()}",
        f"Image directory: {image_path.resolve().parent}",
        f"Image context: {context}",
        "",
        "Important: inspect the local image file. Do not return empty text unless image inspection actually fails.",
        "",
    ]
    if agent_text:
        sections.extend(["# Agent instructions", agent_text, ""])
    if skill_text:
        sections.extend(["# Skill instructions", skill_text, ""])
    sections.extend(
        [
            "# Required output",
            'Return exactly: {"text":"...","confidence":0.0,"notes":"..."}',
            "No markdown. No commentary outside JSON.",
        ]
    )
    return "\n".join(sections)


def cli_primary_ocr_page(
    image_path: Path,
    *,
    backend: str,
    timeout: float,
    context: str,
    input_image: dict[str, Any],
    agy_agent_file: Path = DEFAULT_AGY_AGENT_FILE,
    agy_skill_file: Path = DEFAULT_AGY_SKILL_FILE,
    agy_model: str = "",
    agy_add_dir: Path | None = None,
    agy_dangerously_skip_permissions: bool = True,
) -> dict[str, Any]:
    if backend == "agy_cli":
        prompt = agy_primary_prompt(
            image_path,
            context=context,
            agent_file=agy_agent_file,
            skill_file=agy_skill_file,
        )
        command = ["agy", "-p", prompt, "--print-timeout", f"{max(1, int(round(timeout)))}s"]
        if agy_model:
            command.extend(["--model", agy_model])
        command.extend(["--add-dir", str((agy_add_dir or image_path.parent).resolve())])
        if agy_dangerously_skip_permissions:
            command.append("--dangerously-skip-permissions")
        engine = "agy_cli"
    elif backend == "codex_cli":
        prompt = "\n".join(
            [
                ocr_prompt(context),
                "",
                f"Image path: {image_path.resolve()}",
                f"![page]({image_path.resolve()})",
            ]
        )
        command = [
            "codex",
            "exec",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
            "-i",
            str(image_path),
            "--",
            prompt,
        ]
        engine = "codex_cli"
    else:
        return {"status": "error", "engine": backend, "error": f"unsupported primary backend: {backend}"}

    started = time.perf_counter()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "status": "error",
            "engine": engine,
            "model_id": engine,
            "text": "",
            "confidence": 0.0,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_s": time.perf_counter() - started,
            "input_image": input_image,
        }

    result = ocr_result_from_response(completed.stdout, engine=engine, model_id=engine)
    result["duration_s"] = time.perf_counter() - started
    result["input_image"] = input_image
    result["returncode"] = completed.returncode
    if completed.stderr:
        result["stderr"] = completed.stderr[-2000:]
    if completed.returncode != 0:
        result.update(
            {
                "status": "error",
                "stdout": completed.stdout[-2000:],
                "stderr": completed.stderr[-2000:],
            }
        )
    elif result.get("status") == "empty":
        result["stdout"] = completed.stdout[-2000:]
        result["stderr"] = completed.stderr[-2000:]
    return result


def run_primary_ocr_page(
    raw_page_image: Path,
    prepared_page_image: Path,
    prepared_metadata: dict[str, Any],
    *,
    args: argparse.Namespace,
    page_number: int,
    context: str,
) -> dict[str, Any]:
    if args.primary == "qwen_vllm":
        return qwen_ocr_page(
            raw_page_image,
            endpoint_url=args.endpoint_url,
            model_id=args.model_id,
            timeout=args.qwen_timeout,
            max_tokens=args.max_tokens,
            seed=args.seed + page_number,
            max_side=args.max_side,
            image_preprocess=args.image_preprocess,
            image_upscale=args.image_upscale,
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            min_p=args.min_p,
            presence_penalty=args.presence_penalty,
            enable_thinking=args.enable_thinking,
            thinking_budget=args.thinking_budget,
            api_profile=args.qwen_api_profile,
            api_key_env=args.qwen_api_key_env,
            context=context,
        )
    if args.primary == "opencode_cli":
        return opencode_ocr_page(
            prepared_page_image,
            model_id=args.opencode_model,
            agent_id=args.opencode_agent,
            timeout=args.opencode_timeout,
            max_side=args.max_side,
            context=context,
            pure=args.opencode_pure,
            skip_permissions=args.opencode_skip_permissions,
        )
    if args.primary == "claude_cli":
        return claude_ocr_page(
            prepared_page_image,
            model_id=args.claude_model,
            timeout=args.claude_timeout,
            max_side=args.max_side,
            context=context,
        )
    primary = cli_primary_ocr_page(
        prepared_page_image,
        backend=args.primary,
        timeout=args.primary_cli_timeout,
        context=context,
        input_image=prepared_metadata,
        agy_agent_file=args.agy_agent_file,
        agy_skill_file=args.agy_skill_file,
        agy_model=args.agy_model,
        agy_add_dir=args.agy_add_dir,
        agy_dangerously_skip_permissions=args.agy_dangerously_skip_permissions,
    )
    if (
        args.primary == "agy_cli"
        and args.agy_fallback_backend in {"codex_cli", "opencode_cli", "claude_cli"}
        and primary.get("status") in {"empty", "error"}
    ):
        started = time.perf_counter()
        if args.agy_fallback_backend == "codex_cli":
            fallback = cli_primary_ocr_page(
                prepared_page_image,
                backend="codex_cli",
                timeout=args.codex_fallback_timeout,
                context=f"{context}, fallback_from=agy_cli",
                input_image=prepared_metadata,
            )
        elif args.agy_fallback_backend == "claude_cli":
            fallback = claude_ocr_page(
                prepared_page_image,
                model_id=args.claude_fallback_model,
                timeout=args.claude_fallback_timeout,
                max_side=args.max_side,
                context=f"{context}, fallback_from=agy_cli",
            )
        else:
            fallback = opencode_ocr_page(
                prepared_page_image,
                model_id=args.opencode_fallback_model,
                agent_id=args.opencode_fallback_agent,
                timeout=args.opencode_fallback_timeout,
                max_side=args.max_side,
                context=f"{context}, fallback_from=agy_cli",
                pure=args.opencode_pure,
                skip_permissions=args.opencode_skip_permissions,
            )
        fallback["fallback_backend"] = args.agy_fallback_backend
        fallback["fallback_reason"] = str(primary.get("status") or "unknown")
        fallback["fallback_from"] = primary
        fallback["fallback_duration_s"] = time.perf_counter() - started
        return fallback
    return primary


def process_item(path: Path, args: argparse.Namespace, repo_root: Path, output_dir: Path) -> dict[str, Any]:
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
    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    pdf_path_text = str(pdf.get("path") or "").strip()
    if not pdf_path_text:
        return {**result, "status": "missing_pdf_path"}
    pdf_path = resolve_path(pdf_path_text, repo_root)
    pages_total = int(pdf_text.get("pages") or 0)
    pages_to_process = min(args.max_pages, pages_total) if pages_total > 0 else args.max_pages
    peers = [peer.strip() for peer in args.peers.split(",") if peer.strip()]
    recovery_pages: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix=f"peti-{args.job_name}-") as temp:
        temp_dir = Path(temp)
        for page_number in range(1, pages_to_process + 1):
            page_record: dict[str, Any] = {"page": page_number, "status": "unknown"}
            try:
                page_image = render_pdf_page(pdf_path, page_number, temp_dir, dpi=args.dpi)
                page_width, page_height = image_size(page_image)
                page_mode, ocr_images = page_ocr_images(page_image, temp_dir / f"page_{page_number:04d}", args)
                image_records: list[dict[str, Any]] = []
                for image_index, page_image_record in enumerate(ocr_images, start=1):
                    raw_image_path = Path(page_image_record["image_path"])
                    prepared_path, prepared_metadata = prepared_cli_image(
                        raw_image_path,
                        temp_dir / f"prepared_p{page_number:04d}_{image_index:02d}",
                        max_side=args.max_side,
                        preprocess=args.image_preprocess,
                        upscale=args.image_upscale,
                    )
                    context = page_image_context(page_number, page_image_record, page_width, page_height, args.dpi)
                    context = f"{context}, job={args.job_name}, primary={args.primary}"
                    primary_ocr = run_primary_ocr_page(
                        raw_image_path,
                        prepared_path,
                        prepared_metadata,
                        args=args,
                        page_number=page_number,
                        context=context,
                    )
                    peer_results = {
                        peer: run_peer_cli(
                            peer,
                            prepared_path,
                            primary_ocr.get("text", ""),
                            timeout=args.peer_timeout,
                            context=context,
                            opencode_model=args.opencode_model,
                            claude_model=args.claude_model,
                        )
                        for peer in peers
                        if primary_ocr.get("text")
                    }
                    final_text, final_source = choose_final_text(primary_ocr, peer_results)
                    image_records.append(
                        {
                            "page_image": page_image_record["page_image"],
                            "bbox": page_image_record["bbox"],
                            "status": "recovered" if final_text else "empty",
                            "prepared_image": prepared_metadata,
                            "primary_ocr": primary_ocr,
                            "peers": peer_results,
                            "final_text": final_text,
                            "final_source": final_source,
                        }
                    )
                final_text = normalize_text(
                    "\n".join(record.get("final_text", "") for record in image_records if record.get("final_text"))
                )
                page_record.update(
                    {
                        "status": "recovered" if final_text else "empty",
                        "render": {"dpi": args.dpi, "width": page_width, "height": page_height},
                        "page_ocr": {
                            "mode": page_mode,
                            "image_count": len(image_records),
                            "input_max_side": args.max_side,
                            "image_preprocess": args.image_preprocess,
                            "image_upscale": args.image_upscale,
                        },
                        "images": image_records,
                        "final_text": final_text,
                        "final_source": image_records[0].get("final_source", "") if image_records else "",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                page_record.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
            recovery_pages.append(page_record)

    recovered_text = "\n\n".join(page.get("final_text", "") for page in recovery_pages if page.get("final_text")).strip()
    status = "updated" if recovered_text else "updated_empty"
    if any(page.get("status") == "error" for page in recovery_pages) and not recovered_text:
        status = "error"
    recovery = {
        "job_name": args.job_name,
        "created_at": iso_now(),
        "status": "recovered" if recovered_text else "unrecovered",
        "primary": args.primary,
        "peers": peers,
        "model_id": args.model_id if args.primary == "qwen_vllm" else args.primary,
        "endpoint_url": args.endpoint_url if args.primary == "qwen_vllm" else "",
        "pages_total": pages_total,
        "pages_processed": len(recovery_pages),
        "rendering": {
            "dpi": args.dpi,
            "page_ocr_mode": "single_page",
            "max_side": args.max_side,
            "image_preprocess": args.image_preprocess,
            "image_upscale": args.image_upscale,
        },
        "generation": {
            "temperature": args.temperature,
            "top_p": args.top_p,
            "top_k": args.top_k,
            "min_p": args.min_p,
            "presence_penalty": args.presence_penalty,
            "max_tokens": args.max_tokens,
            "enable_thinking": args.enable_thinking,
            "thinking_budget": args.thinking_budget,
            "qwen_api_profile": args.qwen_api_profile,
        },
        "text": recovered_text,
        "pages": recovery_pages,
    }
    result_path = item_result_path(output_dir, repo_root, path)
    payload = {
        "item_path": str(path),
        "source": source,
        "pdf_path": str(pdf_path),
        "status": status,
        "chars": len(recovered_text),
        "pages_processed": len(recovery_pages),
        "recovery": recovery,
    }
    write_json(result_path, payload)
    return {
        **result,
        "status": status,
        "pages_processed": len(recovery_pages),
        "chars": len(recovered_text),
        "result_path": str(result_path),
    }


def selected_paths(args: argparse.Namespace, repo_root: Path) -> list[Path]:
    artifacts_root = (repo_root / args.artifacts_root).resolve()
    paths = iter_ocr_needed_items(artifacts_root, parse_sources(args.source))
    if args.limit is not None:
        paths = paths[: args.limit]
    return weighted_partition_paths(paths, args.partition_spec, args.partition_name)


def scheduled_pages_for_item(path: Path, max_pages: int) -> int:
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return max_pages
    if not isinstance(item, dict):
        return max_pages
    pdf_text = item.get("pdf_text") if isinstance(item.get("pdf_text"), dict) else {}
    pages_total = int(pdf_text.get("pages") or 0)
    return min(max_pages, pages_total) if pages_total > 0 else max_pages


def total_scheduled_pages(paths: list[Path], max_pages: int) -> int:
    return sum(scheduled_pages_for_item(path, max_pages) for path in paths)


def load_processed_page_counts(results_path: Path) -> dict[str, int]:
    page_counts: dict[str, int] = {}
    if not results_path.exists():
        return page_counts
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        item_path = str(record.get("item_path") or "")
        if not item_path:
            continue
        try:
            page_counts[item_path] = int(record.get("pages_processed") or 0)
        except (TypeError, ValueError):
            page_counts[item_path] = 0
    return page_counts


def build_checkpoint(
    args: argparse.Namespace,
    *,
    started_at: str,
    pid: int,
    paths: list[Path],
    processed: dict[str, str],
    total_pages: int,
    processed_pages: int,
    counts: Counter[str],
    last_item: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "job_name": args.job_name,
        "pid": pid,
        "updated_at": iso_now(),
        "started_at": started_at,
        "settings": jsonable(vars(args)),
        "work_unit": "pdf_item",
        "total_items": len(paths),
        "processed_count": len(processed),
        "remaining_count": max(0, len(paths) - len(processed)),
        "total_pdf_items": len(paths),
        "processed_pdf_items": len(processed),
        "remaining_pdf_items": max(0, len(paths) - len(processed)),
        "total_pages_scheduled": total_pages,
        "processed_pages": processed_pages,
        "remaining_pages": max(0, total_pages - processed_pages),
        "claimed_pages": 0,
        "max_pages_per_item": args.max_pages,
        "counts": dict(counts),
        "processed": processed,
        "last_item": last_item or {},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--primary", choices=PRIMARY_BACKENDS, required=True)
    parser.add_argument("--peers", default="")
    parser.add_argument("--source", default="all")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/validation/ocr_batch_sharp11"))
    parser.add_argument(
        "--partition-spec",
        default="",
        help="Comma-separated NAME:WEIGHT entries used to split the selected OCR-needed items.",
    )
    parser.add_argument("--partition-name", default="", help="Current partition name from --partition-spec.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--dpi", type=int, default=250)
    parser.add_argument("--max-side", type=int, default=A4_250DPI_HEIGHT)
    parser.add_argument("--image-preprocess", choices=IMAGE_PREPROCESSORS, default=QWEN_VL_250DPI_SHARP_PREPROCESSOR)
    parser.add_argument("--image-upscale", type=float, default=1.1)
    parser.add_argument("--endpoint-url", default="http://127.0.0.1:30001")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=DEFAULT_QWEN_TEMPERATURE)
    parser.add_argument("--top-p", type=float, default=DEFAULT_QWEN_TOP_P)
    parser.add_argument("--top-k", type=int, default=DEFAULT_QWEN_TOP_K)
    parser.add_argument("--min-p", type=float, default=DEFAULT_QWEN_MIN_P)
    parser.add_argument("--presence-penalty", type=float, default=DEFAULT_QWEN_PRESENCE_PENALTY)
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument("--thinking-budget", type=int, default=0)
    parser.add_argument("--qwen-api-profile", choices=QWEN_API_PROFILES, default="local")
    parser.add_argument("--qwen-api-key-env", default="")
    parser.add_argument("--qwen-timeout", type=float, default=600.0)
    parser.add_argument("--primary-cli-timeout", type=float, default=360.0)
    parser.add_argument("--peer-timeout", type=float, default=300.0)
    parser.add_argument("--agy-agent-file", type=Path, default=DEFAULT_AGY_AGENT_FILE)
    parser.add_argument("--agy-skill-file", type=Path, default=DEFAULT_AGY_SKILL_FILE)
    parser.add_argument("--agy-model", default="")
    parser.add_argument("--agy-add-dir", type=Path)
    parser.add_argument("--agy-dangerously-skip-permissions", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--agy-fallback-backend", choices=AGY_FALLBACK_BACKENDS, default="codex_cli")
    parser.add_argument("--codex-fallback-timeout", type=float, default=360.0)
    parser.add_argument("--opencode-model", default=DEFAULT_OPENCODE_MODEL_ID)
    parser.add_argument("--opencode-agent", default=DEFAULT_OPENCODE_AGENT_ID)
    parser.add_argument("--opencode-timeout", type=float, default=240.0)
    parser.add_argument(
        "--opencode-pure",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run opencode OCR/peer calls without external plugins. Disabled by default because OCR needs image attachment inspection.",
    )
    parser.add_argument(
        "--opencode-skip-permissions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-approve opencode image attachment access while agent-level bash/edit/write denies remain active.",
    )
    parser.add_argument("--opencode-fallback-model", default=DEFAULT_OPENCODE_MODEL_ID)
    parser.add_argument("--opencode-fallback-agent", default=DEFAULT_OPENCODE_AGENT_ID)
    parser.add_argument("--opencode-fallback-timeout", type=float, default=240.0)
    parser.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL_ID)
    parser.add_argument("--claude-timeout", type=float, default=360.0)
    parser.add_argument("--claude-fallback-model", default=DEFAULT_CLAUDE_MODEL_ID)
    parser.add_argument("--claude-fallback-timeout", type=float, default=360.0)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--retry-failed", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_pages <= 0:
        raise SystemExit("--max-pages must be positive")
    if args.max_side <= 0:
        raise SystemExit("--max-side must be positive")
    if args.image_upscale <= 0:
        raise SystemExit("--image-upscale must be positive")

    repo_root = Path.cwd().resolve()
    output_dir = (repo_root / args.output_root / args.job_name).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    results_path = output_dir / "results.jsonl"
    status_path = output_dir / "status.json"
    pid_path = output_dir / "pid"
    started_at = iso_now()
    pid = os.getpid()
    pid_path.write_text(f"{pid}\n", encoding="utf-8")
    paths = selected_paths(args, repo_root)
    processed = load_processed(checkpoint_path, results_path, retry_failed=args.retry_failed) if args.resume else {}
    if processed:
        path_keys = {str(path) for path in paths}
        processed = {item_path: status for item_path, status in processed.items() if item_path in path_keys}
    total_pages = total_scheduled_pages(paths, args.max_pages)
    processed_page_counts = load_processed_page_counts(results_path) if args.resume else {}
    processed_pages = sum(pages for item_path, pages in processed_page_counts.items() if item_path in processed)
    counts: Counter[str] = Counter(processed.values())
    last_item: dict[str, Any] | None = None
    checkpoint = build_checkpoint(
        args,
        started_at=started_at,
        pid=pid,
        paths=paths,
        processed=processed,
        total_pages=total_pages,
        processed_pages=processed_pages,
        counts=counts,
        last_item=last_item,
    )
    write_json(checkpoint_path, checkpoint)
    write_json(status_path, checkpoint)
    print(
        f"batch started job={args.job_name} primary={args.primary} peers={args.peers} "
        f"items={len(paths)} resume_skips={len(processed)} output={output_dir}",
        flush=True,
    )

    for index, path in enumerate(paths, start=1):
        path_key = str(path)
        if path_key in processed:
            continue
        started = time.perf_counter()
        item_result = process_item(path, args, repo_root, output_dir)
        item_result["index"] = index
        item_result["elapsed_sec"] = round(time.perf_counter() - started, 3)
        item_result["processed_at"] = iso_now()
        status = str(item_result.get("status") or "unknown")
        processed[path_key] = status
        counts[status] += 1
        processed_pages += int(item_result.get("pages_processed") or 0)
        last_item = item_result
        append_jsonl(results_path, item_result)
        checkpoint = build_checkpoint(
            args,
            started_at=started_at,
            pid=pid,
            paths=paths,
            processed=processed,
            total_pages=total_pages,
            processed_pages=processed_pages,
            counts=counts,
            last_item=last_item,
        )
        write_json(checkpoint_path, checkpoint)
        write_json(status_path, checkpoint)
        if args.progress_every and (len(processed) % args.progress_every == 0 or len(processed) == len(paths)):
            print(
                f"progress job={args.job_name} processed={len(processed)}/{len(paths)} "
                f"status={status} chars={item_result.get('chars', 0)} elapsed={item_result['elapsed_sec']}",
                flush=True,
            )

    final_checkpoint = build_checkpoint(
        args,
        started_at=started_at,
        pid=pid,
        paths=paths,
        processed=processed,
        total_pages=total_pages,
        processed_pages=processed_pages,
        counts=counts,
        last_item=last_item,
    )
    final_checkpoint["completed_at"] = iso_now()
    write_json(checkpoint_path, final_checkpoint)
    write_json(status_path, final_checkpoint)
    print(json.dumps({"job": args.job_name, "counts": dict(counts), "output": str(output_dir)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
