#!/usr/bin/env python3
"""Main crawler CLI."""

from __future__ import annotations

import argparse
import asyncio
from typing import Optional

from entrypoint_utils import add_project_paths, configure_windows_asyncio_policy, log_stats

add_project_paths()

from src.crawler import GwanboCrawler
from src.config import get_config
from src.logger import setup_logger
from src.metadata_manager import MetadataManager


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="전자관보 petyList 수집기")
    parser.add_argument("--theme", default="pety", help="수집 테마. 현재 pety만 지원합니다.")
    parser.add_argument("--start-date", help="시작일 YYYY-MM-DD")
    parser.add_argument("--end-date", help="종료일 YYYY-MM-DD 또는 today")
    parser.add_argument("--window-days", type=int, help="검색 윈도우 일수")
    parser.add_argument("--limit", type=int, help="최대 처리 항목 수")
    parser.add_argument("--metadata-only", action="store_true", help="PDF 다운로드 없이 메타데이터만 저장")
    parser.add_argument("--download-pdfs", action="store_true", default=True, help="PDF 다운로드 수행")
    parser.add_argument("--no-download-pdfs", dest="download_pdfs", action="store_false", help="PDF 다운로드 생략")
    parser.add_argument("--resume", action="store_true", default=True, help="완료된 항목/윈도우를 건너뜁니다.")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="상태 파일을 무시하고 다시 처리합니다.")
    parser.add_argument("--headed", action="store_true", help="브라우저를 headless=false로 실행합니다.")
    parser.add_argument("--rebuild-index", action="store_true", help="항목별 JSON에서 metadata.json/csv/category 인덱스를 재생성합니다.")
    parser.add_argument("--no-save-indexes", dest="save_indexes", action="store_false", help="항목별 JSON만 저장하고 aggregate 인덱스 저장을 생략합니다.")
    parser.add_argument("--state-file", help="재시작 상태 파일 경로")
    return parser.parse_args()


async def run_crawler(args: argparse.Namespace) -> Optional[dict]:
    logger = setup_logger(__name__)

    if args.rebuild_index:
        config = get_config()
        manager = MetadataManager(
            GwanboCrawler._pety_metadata_dir(
                config.get_download_config().get("metadata_directory", "artifacts/metadata")
            )
        )
        manager.rebuild_indexes()
        logger.info("메타데이터 인덱스 재생성 완료")
        return None

    crawler = GwanboCrawler(
        theme=args.theme,
        start_date=args.start_date,
        end_date=args.end_date,
        resume=args.resume,
        download_pdfs=args.download_pdfs,
        metadata_only=args.metadata_only,
        limit=args.limit,
        window_days=args.window_days,
        headless=not args.headed,
        save_indexes=args.save_indexes,
        state_file=args.state_file,
    )
    stats = await crawler.crawl()
    log_stats(logger, "크롤링 통계", stats)
    return stats


def main() -> None:
    configure_windows_asyncio_policy()
    asyncio.run(run_crawler(parse_args()))


if __name__ == "__main__":
    main()
