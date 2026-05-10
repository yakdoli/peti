#!/usr/bin/env python3
"""Run petyList crawling in multi-process date batches."""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class DateBatch:
    index: int
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()}_{self.end.isoformat()}"


@dataclass(frozen=True)
class BatchResult:
    batch: DateBatch
    returncode: int
    log_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="전자관보 petyList 날짜 범위 병렬 배치 수집기")
    parser.add_argument("--theme", default="pety", help="수집 테마. 현재 pety만 지원합니다.")
    parser.add_argument("--start-date", default="1994-01-01", help="시작일 YYYY-MM-DD/YYYY/MM/DD/YYYYMMDD")
    parser.add_argument("--end-date", default="today", help="종료일 YYYY-MM-DD/YYYY/MM/DD/YYYYMMDD 또는 today")
    parser.add_argument("--years-per-batch", type=int, default=5, help="배치 하나의 연 단위 길이")
    parser.add_argument("--concurrency", type=int, default=2, help="동시에 실행할 하위 크롤러 프로세스 수")
    parser.add_argument("--window-days", type=int, default=31, help="하위 크롤러 검색 윈도우 일수")
    parser.add_argument("--metadata-only", action="store_true", help="PDF 다운로드 없이 메타데이터만 저장")
    parser.add_argument("--no-download-pdfs", dest="download_pdfs", action="store_false", default=True, help="PDF 다운로드 생략")
    parser.add_argument("--resume", action="store_true", default=True, help="완료된 항목/윈도우를 건너뜁니다.")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="배치 상태 파일을 무시하고 다시 처리합니다.")
    parser.add_argument("--headed", action="store_true", help="브라우저를 headless=false로 실행합니다.")
    parser.add_argument("--limit-per-batch", type=int, help="각 배치에서 최대 처리할 항목 수")
    parser.add_argument("--logs-dir", default="logs/batches", help="배치별 로그 저장 디렉토리")
    parser.add_argument("--state-dir", default="artifacts/state/batches", help="배치별 상태 파일 저장 디렉토리")
    parser.add_argument("--skip-rebuild-index", action="store_true", help="모든 배치 완료 후 인덱스 재생성을 생략")
    parser.add_argument("--dry-run", action="store_true", help="배치와 명령만 출력하고 실행하지 않음")
    return parser.parse_args()


def parse_date(value: str) -> date:
    text = (value or "").strip()
    if text.lower() == "today":
        return datetime.now().date()

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"날짜 형식을 파싱할 수 없습니다: {value}")


def add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(month=2, day=28, year=value.year + years)


def iter_batches(start: date, end: date, years_per_batch: int) -> Iterable[DateBatch]:
    if years_per_batch < 1:
        raise ValueError("--years-per-batch는 1 이상이어야 합니다.")
    if start > end:
        raise ValueError("start-date가 end-date보다 늦습니다.")

    current = start
    index = 1
    while current <= end:
        next_start = add_years(current, years_per_batch)
        batch_end = min(next_start - timedelta(days=1), end)
        yield DateBatch(index=index, start=current, end=batch_end)
        current = batch_end + timedelta(days=1)
        index += 1


def build_command(args: argparse.Namespace, batch: DateBatch, state_path: Path) -> List[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "crawl.py"),
        "--theme",
        args.theme,
        "--start-date",
        batch.start.isoformat(),
        "--end-date",
        batch.end.isoformat(),
        "--window-days",
        str(args.window_days),
        "--state-file",
        str(state_path),
        "--no-save-indexes",
    ]
    if args.metadata_only:
        command.append("--metadata-only")
    elif not args.download_pdfs:
        command.append("--no-download-pdfs")
    if not args.resume:
        command.append("--no-resume")
    if args.headed:
        command.append("--headed")
    if args.limit_per_batch is not None:
        command.extend(["--limit", str(args.limit_per_batch)])
    return command


async def run_batch(args: argparse.Namespace, batch: DateBatch, logs_dir: Path, state_dir: Path) -> BatchResult:
    state_path = state_dir / f"pety_{batch.label}.json"
    log_path = logs_dir / f"pety_{batch.label}.log"
    command = build_command(args, batch, state_path)

    print(f"[{batch.index}] 시작: {batch.start} ~ {batch.end}", flush=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    with open(log_path, "ab") as log_file:
        header = (
            "\n"
            f"===== batch {batch.index}: {batch.start} ~ {batch.end} =====\n"
            f"$ {shlex.join(command)}\n"
        )
        log_file.write(header.encode("utf-8"))
        log_file.flush()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
        )
        returncode = await process.wait()

    status = "완료" if returncode == 0 else f"실패({returncode})"
    print(f"[{batch.index}] {status}: {batch.start} ~ {batch.end} -> {log_path}", flush=True)
    return BatchResult(batch=batch, returncode=returncode, log_path=log_path)


async def rebuild_index(logs_dir: Path) -> int:
    log_path = logs_dir / "rebuild_index.log"
    command = [sys.executable, str(PROJECT_ROOT / "crawl.py"), "--rebuild-index"]
    print(f"인덱스 재생성 시작: {log_path}", flush=True)

    with open(log_path, "ab") as log_file:
        header = "\n===== rebuild index =====\n" f"$ {shlex.join(command)}\n"
        log_file.write(header.encode("utf-8"))
        log_file.flush()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(PROJECT_ROOT),
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
        )
        returncode = await process.wait()

    print(f"인덱스 재생성 종료: {returncode}", flush=True)
    return returncode


async def run(args: argparse.Namespace) -> int:
    start = parse_date(args.start_date)
    end = parse_date(args.end_date)
    batches = list(iter_batches(start, end, args.years_per_batch))
    logs_dir = PROJECT_ROOT / args.logs_dir
    state_dir = PROJECT_ROOT / args.state_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"배치 {len(batches)}개 / 동시 실행 {args.concurrency}개 / 범위 {start} ~ {end}",
        flush=True,
    )

    if args.dry_run:
        for batch in batches:
            state_path = state_dir / f"pety_{batch.label}.json"
            command = build_command(args, batch, state_path)
            print(f"[{batch.index}] {batch.start} ~ {batch.end}: {shlex.join(command)}")
        return 0

    semaphore = asyncio.Semaphore(max(args.concurrency, 1))

    async def guarded_run(batch: DateBatch) -> BatchResult:
        async with semaphore:
            return await run_batch(args, batch, logs_dir, state_dir)

    tasks = [asyncio.create_task(guarded_run(batch)) for batch in batches]
    results: List[BatchResult] = []
    for task in asyncio.as_completed(tasks):
        results.append(await task)

    failures = [result for result in results if result.returncode != 0]
    if failures:
        print("실패한 배치가 있어 인덱스 재생성을 건너뜁니다.", flush=True)
        for result in sorted(failures, key=lambda item: item.batch.index):
            print(
                f"[{result.batch.index}] {result.batch.start} ~ {result.batch.end}: {result.log_path}",
                flush=True,
            )
        return 1

    if args.skip_rebuild_index:
        print("인덱스 재생성을 생략했습니다.", flush=True)
        return 0

    return await rebuild_index(logs_dir)


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
