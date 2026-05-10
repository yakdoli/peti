#!/usr/bin/env python3
"""Generate OCR-free text metadata for PDF artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.pdf_text_metadata import SOURCE_NAMES, generate_source_text_metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PDF 패키지 기반 텍스트 추출 가능 여부 메타데이터 생성")
    parser.add_argument("--source", choices=[*SOURCE_NAMES, "all"], default="all", help="대상 소스")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts"), help="아티팩트 루트")
    parser.add_argument("--limit", type=int, help="소스별 최대 처리 PDF 수")
    parser.add_argument("--update-items", action="store_true", help="기존 item JSON의 pdf_text/ocr 필드도 보강")
    parser.add_argument("--include-sample", action="store_true", help="추출 샘플 텍스트를 sidecar 메타데이터에 포함")
    parser.add_argument("--sample-chars", type=int, default=1000, help="샘플 텍스트 최대 길이")
    parser.add_argument("--include-sha256", action="store_true", help="PDF SHA-256을 계산해 포함")
    parser.add_argument("--max-pages", type=int, help="PDF당 검사할 최대 페이지 수")
    parser.add_argument("--progress-every", type=int, default=500, help="진행 로그 출력 간격")
    parser.add_argument("--timeout-seconds", type=int, default=30, help="PDF 1개 분석 최대 시간")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sources = SOURCE_NAMES if args.source == "all" else (args.source,)
    for source in sources:
        summary = generate_source_text_metadata(
            source,
            artifacts_root=args.artifacts_root,
            limit=args.limit,
            update_items=args.update_items,
            include_sample=args.include_sample,
            sample_chars=args.sample_chars,
            include_sha256=args.include_sha256,
            max_pages=args.max_pages,
            progress_every=args.progress_every,
            timeout_seconds=args.timeout_seconds,
        )
        print(
            "source={source} processed={processed} text_extractable={text_extractable} "
            "image_or_unextractable={image_or_unextractable} errors={errors} "
            "updated_items={updated_items}".format(**summary),
            flush=True,
        )


if __name__ == "__main__":
    main()
