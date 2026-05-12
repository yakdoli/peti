#!/usr/bin/env python3
"""Generate peer-review text extraction metadata for PDF artifacts."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.pdf_extraction_peer_review import SOURCE_NAMES, generate_source_extraction_peer_review


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PDF 텍스트 추출 경로별 피어 리뷰 메타데이터 생성")
    parser.add_argument("--source", choices=[*SOURCE_NAMES, "all"], default="all", help="대상 소스")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts"), help="아티팩트 루트")
    parser.add_argument("--limit", type=int, help="소스별 최대 처리 PDF 수")
    parser.add_argument("--max-pages", type=int, default=1, help="PDF당 분석/렌더링할 최대 페이지 수; 0이면 전체 페이지")
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(4, (os.cpu_count() or 2) // 2)),
        help="병렬 분석 프로세스 수",
    )
    parser.add_argument("--force", action="store_true", help="기존 extraction_peer_review sidecar를 재생성")
    parser.add_argument("--include-non-completed", action="store_true", help="pdf.status가 completed가 아닌 항목도 처리")
    parser.add_argument("--skip-markitdown", action="store_true", help="MarkItDown 변환 peer를 건너뜀")
    parser.add_argument("--skip-ocr", action="store_true", help="페이지 이미지 저장/OCR peer를 건너뜀")
    parser.add_argument("--ocr-lang", default="kor+eng", help="Tesseract OCR 언어")
    parser.add_argument("--ocr-dpi", type=int, default=200, help="페이지 이미지 렌더링 DPI")
    parser.add_argument("--timeout-seconds", type=int, default=30, help="PDF 1개 또는 OCR 호출 제한 시간")
    parser.add_argument("--progress-every", type=int, default=100, help="진행 로그 출력 간격")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    max_pages = None if args.max_pages == 0 else args.max_pages
    sources = SOURCE_NAMES if args.source == "all" else (args.source,)
    for source in sources:
        summary = generate_source_extraction_peer_review(
            source,
            artifacts_root=args.artifacts_root,
            limit=args.limit,
            max_pages=max_pages,
            workers=args.workers,
            force=args.force,
            include_non_completed=args.include_non_completed,
            run_markitdown=not args.skip_markitdown,
            run_ocr=not args.skip_ocr,
            ocr_lang=args.ocr_lang,
            ocr_dpi=args.ocr_dpi,
            timeout_seconds=args.timeout_seconds,
            progress_every=args.progress_every,
        )
        print(
            "source={source} eligible={eligible} processed={processed} errors={errors} "
            "images_saved={images_saved} skipped_existing={skipped_existing}".format(**summary),
            flush=True,
        )


if __name__ == "__main__":
    main()
