#!/usr/bin/env python3
"""Summarize running VLM OCR batch jobs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def process_alive(pid: int | None) -> bool:
    return bool(pid and Path(f"/proc/{pid}").exists())


def job_rows(output_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job_dir in sorted(path for path in output_root.iterdir() if path.is_dir()):
        if not (job_dir / "status.json").exists():
            continue
        status = load_json(job_dir / "status.json")
        pid = read_pid(job_dir / "pid")
        total_items = int(status.get("total_pdf_items") or status.get("total_items") or 0)
        processed_items = int(status.get("processed_pdf_items") or status.get("processed_count") or 0)
        remaining_items = int(status.get("remaining_pdf_items") or max(0, total_items - processed_items))
        total_pages = int(status.get("total_pages_scheduled") or status.get("total_items") or 0)
        processed_pages = int(status.get("processed_pages") or status.get("processed_count") or 0)
        remaining_pages = int(status.get("remaining_pages") or max(0, total_pages - processed_pages))
        claimed_pages = int(status.get("claimed_pages") or status.get("claimed_count") or 0)
        percent = round((processed_pages / total_pages) * 100.0, 2) if total_pages else 0.0
        rows.append(
            {
                "job": job_dir.name,
                "pid": pid,
                "alive": process_alive(pid),
                "processed": processed_items,
                "total": total_items,
                "remaining": remaining_items,
                "processed_pages": processed_pages,
                "total_pages": total_pages,
                "remaining_pages": remaining_pages,
                "claimed": claimed_pages,
                "percent": percent,
                "work_unit": status.get("work_unit", ""),
                "counts": status.get("counts", {}),
                "agent": status.get("agent", {}),
                "last_item": status.get("last_item", {}),
                "updated_at": status.get("updated_at", ""),
                "job_dir": str(job_dir),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    output_root = args.output_root.resolve()
    if not output_root.exists():
        raise SystemExit(f"output root does not exist: {output_root}")
    rows = job_rows(output_root)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("job alive pid pdf_items pages claimed_pages percent counts agent updated_at")
    for row in rows:
        counts = ",".join(f"{key}:{value}" for key, value in sorted(dict(row["counts"]).items()))
        agent = dict(row.get("agent") or {})
        agent_text = agent.get("name") or agent.get("id") or "-"
        print(
            f"{row['job']} {str(row['alive']).lower()} {row['pid'] or '-'} "
            f"{row['processed']}/{row['total']} {row['processed_pages']}/{row['total_pages']} "
            f"{row['claimed']} {row['percent']:.2f}% "
            f"{counts or '-'} {agent_text} {row['updated_at']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
