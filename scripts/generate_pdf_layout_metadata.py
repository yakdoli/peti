#!/usr/bin/env python3
"""Generate layout classification and table JSON metadata for PDF artifacts."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.pdf_layout_metadata import SOURCE_NAMES, generate_source_layout_metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="텍스트 추출 가능 PDF의 레이아웃/테이블 메타데이터 생성")
    parser.add_argument("--source", choices=[*SOURCE_NAMES, "all"], default="all", help="대상 소스")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts"), help="아티팩트 루트")
    parser.add_argument("--limit", type=int, help="소스별 최대 처리 PDF 수")
    parser.add_argument("--max-pages", type=int, default=3, help="PDF당 분석할 최대 페이지 수; 0이면 전체 페이지")
    parser.add_argument(
        "--table-strategy",
        choices=("auto", "lines", "lines-strict", "text"),
        default="auto",
        help="pdfplumber 테이블 탐지 전략; auto는 라인/텍스트 전략을 모두 시도 후 중복 제거",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(8, (os.cpu_count() or 2) // 2)),
        help="병렬 분석 프로세스 수",
    )
    parser.add_argument("--force", action="store_true", help="기존 layout_metadata sidecar를 재생성")
    parser.add_argument("--timeout-seconds", type=int, default=30, help="PDF 1개 분석 최대 시간")
    parser.add_argument("--progress-every", type=int, default=500, help="진행 로그 출력 간격")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    max_pages = None if args.max_pages == 0 else args.max_pages
    sources = SOURCE_NAMES if args.source == "all" else (args.source,)
    for source in sources:
        summary = generate_source_layout_metadata(
            source,
            artifacts_root=args.artifacts_root,
            limit=args.limit,
            max_pages=max_pages,
            workers=args.workers,
            force=args.force,
            table_strategy=args.table_strategy,
            timeout_seconds=args.timeout_seconds,
            progress_every=args.progress_every,
        )
        print(
            "source={source} eligible={eligible} processed={processed} errors={errors} "
            "tables={tables} skipped_existing={skipped_existing}".format(**summary),
            flush=True,
        )


if __name__ == "__main__":
    main()
