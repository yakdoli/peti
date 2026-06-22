#!/usr/bin/env python3
"""Manage OCR tasks that are completed by a Codex subagent in this session."""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recover_ocr_needed_with_vlm import (  # noqa: E402
    A4_250DPI_HEIGHT,
    IMAGE_PREPROCESSORS,
    QWEN_VL_250DPI_SHARP_PREPROCESSOR,
    choose_final_text,
    image_size,
    jsonable,
    normalize_text,
    ocr_prompt,
    page_image_context,
    page_ocr_images,
    parse_sources,
    render_pdf_page,
    resolve_path,
    run_peer_cli,
    source_from_item_path,
    write_json,
)
from scripts.run_vlm_ocr_batch_job import (  # noqa: E402
    append_jsonl,
    item_result_path,
    selected_paths,
)


TERMINAL_STATUSES = {"updated", "updated_empty", "error", "json_error", "missing_pdf_path"}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_key(item_path: str, page_number: int) -> str:
    return f"{item_path}#page={page_number:04d}"


def pages_to_schedule(pages_total: int, max_pages: int) -> int:
    if max_pages < 0:
        raise ValueError("max_pages must be >= 0")
    if max_pages == 0:
        return max(1, pages_total)
    return min(max_pages, pages_total) if pages_total > 0 else max_pages


def build_page_tasks(paths: list[Path], max_pages: int) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for path in paths:
        item, _error = load_item(path)
        pdf_text = item.get("pdf_text") if isinstance(item, dict) and isinstance(item.get("pdf_text"), dict) else {}
        pages_total = int(pdf_text.get("pages") or 0)
        for page_number in range(1, pages_to_schedule(pages_total, max_pages) + 1):
            item_path = str(path)
            tasks.append(
                {
                    "task_key": task_key(item_path, page_number),
                    "item_path": item_path,
                    "page_number": page_number,
                    "pages_total": pages_total,
                    "source": source_from_item_path(path),
                }
            )
    return tasks


def state_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(state.get("tasks"), list):
        return [task for task in state["tasks"] if isinstance(task, dict)]
    return [
        {
            "task_key": str(item),
            "item_path": str(item),
            "page_number": 1,
            "pages_total": 0,
            "source": source_from_item_path(Path(str(item))),
        }
        for item in state.get("items", [])
    ]


def page_result_path(job_dir: Path, repo_root: Path, item_path: Path, page_number: int) -> Path:
    base = item_result_path(job_dir, repo_root, item_path).with_suffix("")
    return base.with_name(f"{base.name}.p{page_number:04d}.json")


@contextmanager
def locked_state(job_dir: Path) -> Any:
    job_dir.mkdir(parents=True, exist_ok=True)
    lock_path = job_dir / ".queue.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = job_dir / "queue_state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            state = {}
        yield state
        write_json(state_path, state)
        write_status(job_dir, state)
        fcntl.flock(lock, fcntl.LOCK_UN)


def status_payload(state: dict[str, Any]) -> dict[str, Any]:
    tasks = state_tasks(state)
    item_paths = sorted({str(task.get("item_path", "")) for task in tasks if task.get("item_path")})
    task_state = state.get("task_state") if isinstance(state.get("task_state"), dict) else state.get("item_state", {})
    statuses = [str(task_state.get(str(task.get("task_key")), {}).get("status", "pending")) for task in tasks]
    counts = Counter(status for status in statuses if status in TERMINAL_STATUSES)
    claimed = sum(1 for status in statuses if status == "claimed")
    processed = sum(counts.values())
    completed_item_paths = {
        str(task.get("item_path", ""))
        for task in tasks
        if str(task_state.get(str(task.get("task_key")), {}).get("status", "pending")) in TERMINAL_STATUSES
    }
    return {
        "job_name": state.get("job_name", ""),
        "updated_at": iso_now(),
        "started_at": state.get("started_at", ""),
        "settings": state.get("settings", {}),
        "work_unit": "page",
        "total_items": len(item_paths),
        "processed_count": processed,
        "remaining_count": max(0, len(tasks) - processed),
        "claimed_count": claimed,
        "total_pdf_items": len(item_paths),
        "processed_pdf_items": len(completed_item_paths),
        "remaining_pdf_items": max(0, len(item_paths) - len(completed_item_paths)),
        "total_pages_scheduled": len(tasks),
        "processed_pages": processed,
        "remaining_pages": max(0, len(tasks) - processed),
        "claimed_pages": claimed,
        "counts": dict(counts),
        "last_item": state.get("last_item", {}),
        "agent": state.get("agent", {}),
    }


def write_status(job_dir: Path, state: dict[str, Any]) -> None:
    write_json(job_dir / "status.json", status_payload(state))


def load_item(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    if not isinstance(item, dict):
        return None, "item is not object"
    return item, ""


def claim_id_for(item_path: Path) -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{abs(hash(str(item_path))) & 0xffffffff:08x}"


def init_queue(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    output_dir = (repo_root / args.output_root / args.job_name).resolve()
    init_args = argparse.Namespace(
        source=args.source,
        artifacts_root=args.artifacts_root,
        limit=args.limit,
        partition_spec=args.partition_spec,
        partition_name=args.partition_name,
    )
    paths = selected_paths(init_args, repo_root)
    tasks = build_page_tasks(paths, args.max_pages)
    settings = {key: value for key, value in vars(args).items() if key != "func"}
    with locked_state(output_dir) as state:
        if state and not args.rebuild:
            print(json.dumps(status_payload(state), ensure_ascii=False, sort_keys=True))
            return 0
        state.clear()
        state.update(
            {
                "job_name": args.job_name,
                "started_at": iso_now(),
                "settings": jsonable(settings),
                "items": [str(path) for path in paths],
                "tasks": tasks,
                "task_state": {str(task["task_key"]): {"status": "pending"} for task in tasks},
                "last_item": {},
            }
        )
    print(
        json.dumps(
            {"job": args.job_name, "pdf_items": len(paths), "page_tasks": len(tasks), "output": str(output_dir)},
            ensure_ascii=False,
        )
    )
    return 0


def claim_task(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    job_dir = args.job_dir.resolve()
    with locked_state(job_dir) as state:
        task_state = state.setdefault("task_state", state.setdefault("item_state", {}))
        now = time.time()
        tasks = state_tasks(state)
        task_by_key = {str(task["task_key"]): task for task in tasks}
        for task_id in task_by_key:
            record = task_state.setdefault(task_id, {"status": "pending"})
            if record.get("status") != "claimed":
                continue
            claimed_at = float(record.get("claimed_at_epoch") or 0)
            if args.claim_timeout and now - claimed_at > args.claim_timeout:
                record["status"] = "pending"
                record["stale_claim"] = record.get("claim_id", "")
        selected_task_key = ""
        for task in tasks:
            task_id = str(task["task_key"])
            record = task_state.setdefault(task_id, {"status": "pending"})
            if record.get("status") == "pending":
                selected_task_key = task_id
                break
        if not selected_task_key:
            print(json.dumps({"status": "empty"}, ensure_ascii=False))
            return 0
        selected_task = task_by_key[selected_task_key]
        selected = str(selected_task["item_path"])
        item_path = Path(selected)
        page_number = int(selected_task.get("page_number") or 1)
        claim_id = claim_id_for(Path(selected_task_key))
        task_dir = job_dir / "tasks" / claim_id
        record = task_state[selected_task_key] = {
            "status": "claimed",
            "claim_id": claim_id,
            "claimed_at": iso_now(),
            "claimed_at_epoch": now,
            "task_dir": str(task_dir),
            "task_key": selected_task_key,
        }

    item, error = load_item(item_path)
    if item is None:
        with locked_state(job_dir) as state:
            state.setdefault("task_state", {})[selected_task_key] = {"status": "json_error", "error": error}
            state["last_item"] = {"task_key": selected_task_key, "item_path": selected, "status": "json_error", "error": error}
        print(json.dumps({"status": "json_error", "error": error}, ensure_ascii=False))
        return 0

    pdf_text = item.get("pdf_text") if isinstance(item.get("pdf_text"), dict) else {}
    pdf = item.get("pdf") if isinstance(item.get("pdf"), dict) else {}
    pdf_path_text = str(pdf.get("path") or "").strip()
    if not pdf_path_text:
        with locked_state(job_dir) as state:
            state.setdefault("task_state", {})[selected_task_key] = {"status": "missing_pdf_path"}
            state["last_item"] = {"task_key": selected_task_key, "item_path": selected, "status": "missing_pdf_path"}
        print(json.dumps({"status": "missing_pdf_path"}, ensure_ascii=False))
        return 0

    settings = json.loads((job_dir / "queue_state.json").read_text(encoding="utf-8")).get("settings", {})
    dpi = int(settings.get("dpi") or 250)
    max_side = int(settings.get("max_side") or A4_250DPI_HEIGHT)
    preprocess = str(settings.get("image_preprocess") or QWEN_VL_250DPI_SHARP_PREPROCESSOR)
    image_upscale = float(settings.get("image_upscale") or 1.1)
    pdf_path = resolve_path(pdf_path_text, repo_root)
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        page_image = render_pdf_page(pdf_path, page_number, task_dir, dpi=dpi)
        page_width, page_height = image_size(page_image)
        page_mode, ocr_images = page_ocr_images(page_image, task_dir / f"page_{page_number:04d}", args)
        raw_image_path = Path(ocr_images[0]["image_path"])
        from scripts.run_vlm_ocr_batch_job import prepared_cli_image

        prepared_path, prepared_metadata = prepared_cli_image(
            raw_image_path,
            task_dir / "prepared",
            max_side=max_side,
            preprocess=preprocess,
            upscale=image_upscale,
        )
        context = page_image_context(page_number, ocr_images[0], page_width, page_height, dpi)
        context = f"{context}, job={state_name(job_dir)}, primary=codex_subagent"
        prompt = ocr_prompt(context)
        transcript_path = task_dir / "transcript.txt"
        prompt_path = task_dir / "prompt.txt"
        packet_path = task_dir / "task_packet.md"
        complete_command = (
            "rtk .venv/bin/python scripts/subagent_ocr_queue.py complete "
            f"--job-dir {job_dir.relative_to(repo_root)} --claim-id {claim_id} "
            f"--text-file {transcript_path} --confidence 0.85 "
            "--notes \"codex current-session subagent primary OCR\""
        )
        fail_command = (
            "rtk .venv/bin/python scripts/subagent_ocr_queue.py fail "
            f"--job-dir {job_dir.relative_to(repo_root)} --claim-id {claim_id} --error \"<reason>\""
        )
        task = {
            "status": "claimed",
            "claim_id": claim_id,
            "task_key": selected_task_key,
            "item_path": selected,
            "source": source_from_item_path(item_path),
            "pdf_path": str(pdf_path),
            "page_number": page_number,
            "pages_total": int(pdf_text.get("pages") or 0),
            "page_mode": page_mode,
            "raw_image_path": str(raw_image_path),
            "image_path": str(prepared_path),
            "prepared_image": prepared_metadata,
            "context": context,
            "prompt_path": str(prompt_path),
            "packet_path": str(packet_path),
            "transcript_path": str(transcript_path),
            "complete_command": complete_command,
            "fail_command": fail_command,
        }
        write_json(task_dir / "task.json", task)
        prompt_path.write_text(prompt + "\n", encoding="utf-8")
        packet_path.write_text(
            "\n".join(
                [
                    "# Codex Subagent OCR Task",
                    f"claim_id: {claim_id}",
                    f"task_key: {selected_task_key}",
                    f"page: {page_number}/{task['pages_total'] or '?'}",
                    f"image_path: {prepared_path}",
                    f"transcript_path: {transcript_path}",
                    "",
                    "Rules: transcribe only visible text, preserve Korean/Hanja/digits/punctuation/tables/line breaks, do not summarize or infer.",
                    "",
                    "Complete:",
                    complete_command,
                    "",
                    "Fail:",
                    fail_command,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": "claimed",
                    "claim_id": claim_id,
                    "task_key": selected_task_key,
                    "item_path": selected,
                    "page_number": page_number,
                    "pages_total": task["pages_total"],
                    "image_path": str(prepared_path),
                    "packet_path": str(packet_path),
                    "transcript_path": str(transcript_path),
                    "complete_command": complete_command,
                    "fail_command": fail_command,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        with locked_state(job_dir) as state:
            state.setdefault("task_state", {})[selected_task_key] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            state["last_item"] = {
                "task_key": selected_task_key,
                "item_path": selected,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 0


def state_name(job_dir: Path) -> str:
    return job_dir.name


def complete_task(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    job_dir = args.job_dir.resolve()
    notes = args.notes or ""
    started = time.perf_counter()
    state = json.loads((job_dir / "queue_state.json").read_text(encoding="utf-8"))
    claim_record: tuple[str, dict[str, Any]] | None = None
    task_state = state.get("task_state") if isinstance(state.get("task_state"), dict) else state.get("item_state", {})
    for task_id, record in task_state.items():
        if record.get("claim_id") == args.claim_id and record.get("status") == "claimed":
            claim_record = (task_id, record)
            break
    if claim_record is None:
        raise SystemExit(f"claim not found or not active: {args.claim_id}")
    selected_task_key, record = claim_record
    task_dir = Path(record["task_dir"])
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    text_file = args.text_file or Path(task.get("transcript_path", task_dir / "transcript.txt"))
    text = normalize_text(text_file.read_text(encoding="utf-8"))
    image_path = Path(task["image_path"])
    peer_results: dict[str, dict[str, Any]] = {}
    peers = [peer.strip() for peer in args.peers.split(",") if peer.strip()]
    for peer in peers:
        if text:
            peer_results[peer] = run_peer_cli(peer, image_path, text, timeout=args.peer_timeout, context=task["context"])
    primary_ocr = {
        "engine": "codex_subagent",
        "model_id": "current_session_subagent",
        "text": text,
        "confidence": args.confidence,
        "notes": notes,
        "status": "ok" if text else "empty",
        "input_image": task.get("prepared_image", {}),
    }
    final_text, final_source = choose_final_text(primary_ocr, peer_results)
    status = "updated" if final_text else "updated_empty"
    item_path_text = str(task.get("item_path") or "")
    item_path = Path(item_path_text)
    page_number = int(task.get("page_number") or 1)
    source = source_from_item_path(item_path)
    result_path = page_result_path(job_dir, repo_root, item_path, page_number)
    page = {
        "page": page_number,
        "status": "recovered" if final_text else "empty",
        "page_ocr": {
            "mode": task.get("page_mode", "single_page"),
            "image_count": 1,
            "input_max_side": task.get("prepared_image", {}).get("max_side", 0),
            "image_preprocess": task.get("prepared_image", {}).get("preprocess", ""),
            "image_upscale": task.get("prepared_image", {}).get("upscale", 0),
        },
        "images": [
            {
                "page_image": 1,
                "bbox": [0, 0, task.get("prepared_image", {}).get("source_width", 0), task.get("prepared_image", {}).get("source_height", 0)],
                "status": "recovered" if final_text else "empty",
                "prepared_image": task.get("prepared_image", {}),
                "primary_ocr": primary_ocr,
                "peers": peer_results,
                "final_text": final_text,
                "final_source": final_source,
            }
        ],
        "final_text": final_text,
        "final_source": final_source,
    }
    payload = {
        "item_path": item_path_text,
        "task_key": selected_task_key,
        "source": source,
        "pdf_path": task.get("pdf_path", ""),
        "status": status,
        "chars": len(final_text),
        "pages_processed": 1,
        "recovery": {
            "job_name": job_dir.name,
            "created_at": iso_now(),
            "status": "recovered" if final_text else "unrecovered",
            "primary": "codex_subagent",
            "peers": peers,
            "model_id": "current_session_subagent",
            "endpoint_url": "",
            "pages_total": task.get("pages_total", 0),
            "pages_processed": 1,
            "rendering": {
                "dpi": 250,
                "page_ocr_mode": "single_page",
                "max_side": task.get("prepared_image", {}).get("max_side", 0),
                "image_preprocess": task.get("prepared_image", {}).get("preprocess", ""),
                "image_upscale": task.get("prepared_image", {}).get("upscale", 0),
            },
            "generation": {"operator": "current_session_codex_subagent"},
            "text": final_text,
            "pages": [page],
        },
    }
    write_json(result_path, payload)
    item_result = {
        "item_path": item_path_text,
        "task_key": selected_task_key,
        "page_number": page_number,
        "source": source,
        "status": status,
        "chars": len(final_text),
        "pages_processed": 1,
        "result_path": str(result_path),
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "processed_at": iso_now(),
    }
    append_jsonl(job_dir / "results.jsonl", item_result)
    with locked_state(job_dir) as state:
        state.setdefault("task_state", {})[selected_task_key] = {
            "status": status,
            "claim_id": args.claim_id,
            "completed_at": iso_now(),
            "result_path": str(result_path),
        }
        state["last_item"] = item_result
    print(json.dumps(item_result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def fail_task(args: argparse.Namespace) -> int:
    job_dir = args.job_dir.resolve()
    with locked_state(job_dir) as state:
        task_state = state.get("task_state") if isinstance(state.get("task_state"), dict) else state.get("item_state", {})
        for task_id, record in task_state.items():
            if record.get("claim_id") == args.claim_id:
                task_state[task_id] = {
                    "status": "error",
                    "claim_id": args.claim_id,
                    "error": args.error,
                    "completed_at": iso_now(),
                }
                state["last_item"] = {"task_key": task_id, "status": "error", "error": args.error}
                print(json.dumps(state["last_item"], ensure_ascii=False))
                return 0
    raise SystemExit(f"claim not found: {args.claim_id}")


def set_agent(args: argparse.Namespace) -> int:
    job_dir = args.job_dir.resolve()
    with locked_state(job_dir) as state:
        state["agent"] = {"id": args.agent_id, "name": args.name, "updated_at": iso_now()}
    return 0


def show_status(args: argparse.Namespace) -> int:
    state_path = args.job_dir.resolve() / "queue_state.json"
    if not state_path.exists():
        raise SystemExit(f"queue not initialized: {args.job_dir}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    print(json.dumps(status_payload(state), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def add_common_init_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--source", default="all")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/validation/ocr_batch_sharp11_subagent"))
    parser.add_argument("--partition-spec", required=True)
    parser.add_argument("--partition-name", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-pages", type=int, default=1, help="Pages per PDF item; 0 schedules all known pages.")
    parser.add_argument("--dpi", type=int, default=250)
    parser.add_argument("--max-side", type=int, default=A4_250DPI_HEIGHT)
    parser.add_argument("--image-preprocess", choices=IMAGE_PREPROCESSORS, default=QWEN_VL_250DPI_SHARP_PREPROCESSOR)
    parser.add_argument("--image-upscale", type=float, default=1.1)
    parser.add_argument("--rebuild", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    add_common_init_args(init_parser)
    init_parser.set_defaults(func=init_queue)

    claim_parser = subparsers.add_parser("claim")
    claim_parser.add_argument("--job-dir", type=Path, required=True)
    claim_parser.add_argument("--claim-timeout", type=float, default=3600.0)
    claim_parser.set_defaults(func=claim_task)

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--job-dir", type=Path, required=True)
    complete_parser.add_argument("--claim-id", required=True)
    complete_parser.add_argument("--text-file", type=Path)
    complete_parser.add_argument("--confidence", type=float, default=0.8)
    complete_parser.add_argument("--notes", default="")
    complete_parser.add_argument("--peers", default="agy")
    complete_parser.add_argument("--peer-timeout", type=float, default=360.0)
    complete_parser.set_defaults(func=complete_task)

    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("--job-dir", type=Path, required=True)
    fail_parser.add_argument("--claim-id", required=True)
    fail_parser.add_argument("--error", required=True)
    fail_parser.set_defaults(func=fail_task)

    agent_parser = subparsers.add_parser("set-agent")
    agent_parser.add_argument("--job-dir", type=Path, required=True)
    agent_parser.add_argument("--agent-id", required=True)
    agent_parser.add_argument("--name", default="")
    agent_parser.set_defaults(func=set_agent)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--job-dir", type=Path, required=True)
    status_parser.set_defaults(func=show_status)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
