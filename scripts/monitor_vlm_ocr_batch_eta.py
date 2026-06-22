#!/usr/bin/env python3
"""Monitor VLM OCR batch jobs with simple ETA estimates."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def process_alive(pid: int | None) -> bool:
    return bool(pid and Path(f"/proc/{pid}").exists())


def duration_text(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def job_rows(output_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for job_dir in sorted(path for path in output_root.iterdir() if path.is_dir()):
        status_path = job_dir / "status.json"
        if not status_path.exists():
            continue
        status = load_json(status_path)
        pid = read_pid(job_dir / "pid")
        total_pages = int(status.get("total_pages_scheduled") or status.get("total_items") or 0)
        processed_pages = int(status.get("processed_pages") or status.get("processed_count") or 0)
        remaining_pages = int(status.get("remaining_pages") or max(0, total_pages - processed_pages))
        total_items = int(status.get("total_pdf_items") or status.get("total_items") or 0)
        processed_items = int(status.get("processed_pdf_items") or status.get("processed_count") or 0)
        started_at = parse_time(str(status.get("started_at") or ""))
        updated_at = str(status.get("updated_at") or "")
        elapsed_s = (now - started_at).total_seconds() if started_at else None
        rate = processed_pages / elapsed_s if elapsed_s and elapsed_s > 0 and processed_pages > 0 else 0.0
        eta_s = remaining_pages / rate if rate > 0 else None
        rows.append(
            {
                "job": job_dir.name,
                "alive": process_alive(pid),
                "pid": pid,
                "processed_items": processed_items,
                "total_items": total_items,
                "processed_pages": processed_pages,
                "total_pages": total_pages,
                "remaining_pages": remaining_pages,
                "percent": (processed_pages / total_pages * 100.0) if total_pages else 0.0,
                "rate": rate,
                "eta_s": eta_s,
                "counts": status.get("counts", {}),
                "updated_at": updated_at,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    if not output_root.exists():
        raise SystemExit(f"output root does not exist: {output_root}")

    rows = job_rows(output_root)
    total_pages = sum(row["total_pages"] for row in rows)
    processed_pages = sum(row["processed_pages"] for row in rows)
    remaining_pages = sum(row["remaining_pages"] for row in rows)
    total_rate = sum(row["rate"] for row in rows)
    total_eta = remaining_pages / total_rate if total_rate > 0 else None
    total_percent = processed_pages / total_pages * 100.0 if total_pages else 0.0
    alive_count = sum(1 for row in rows if row["alive"])

    print(
        f"TOTAL alive={alive_count}/{len(rows)} pages={processed_pages}/{total_pages} "
        f"remaining={remaining_pages} percent={total_percent:.2f}% "
        f"rate={total_rate * 60:.2f} pages/min eta={duration_text(total_eta)}"
    )
    print("job alive pages percent rate/min eta counts updated_at")
    for row in rows:
        counts = ",".join(f"{key}:{value}" for key, value in sorted(dict(row["counts"]).items())) or "-"
        print(
            f"{row['job']} {str(row['alive']).lower()} "
            f"{row['processed_pages']}/{row['total_pages']} {row['percent']:.2f}% "
            f"{row['rate'] * 60:.2f} {duration_text(row['eta_s'])} "
            f"{counts} {row['updated_at']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
