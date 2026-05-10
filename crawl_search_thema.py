#!/usr/bin/env python3
"""SearchThema crawler CLI."""

from __future__ import annotations

import argparse
import asyncio
from typing import Optional

from entrypoint_utils import add_project_paths, configure_windows_asyncio_policy, log_stats

add_project_paths()

from src.crawler_search_thema import SearchThemaCrawler
from src.logger import setup_logger


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="전자관보 SearchThema 공직자 재산공개 수집기")
    parser.add_argument("--year", help="수집 연도. 지정하지 않으면 1994년부터 현재까지 수집합니다.")
    parser.add_argument("--institution", help="수집 기관. 지정하지 않으면 전체 기관과 설정된 기관별로 수집합니다.")
    parser.add_argument("--metadata-only", action="store_true", help="PDF 다운로드 없이 메타데이터만 저장합니다.")
    parser.add_argument("--limit", type=int, help="최대 처리 항목 수")
    parser.add_argument("--resume", action="store_true", default=True, help="완료된 조합/항목을 건너뜁니다.")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="상태와 기존 항목을 무시하고 다시 처리합니다.")
    parser.add_argument("--headed", action="store_true", help="Playwright fallback 사용 시 브라우저를 headless=false로 실행합니다.")
    parser.add_argument("--rebuild-index", action="store_true", help="SearchThema 항목별 JSON에서 aggregate 인덱스를 재생성합니다.")
    parser.add_argument("--state-file", help="재시작 상태 파일 경로")
    parser.add_argument("--concurrency", type=int, default=5, help="동시 다운로드 수 (기본: 5)")
    return parser.parse_args(argv)


def _selected_years(args: argparse.Namespace) -> list[str] | None:
    return [args.year] if args.year else None


def _selected_institutions(args: argparse.Namespace) -> list[str] | None:
    return [args.institution] if args.institution else None


async def run_crawler(args: argparse.Namespace) -> Optional[dict]:
    logger = setup_logger(__name__)
    years = _selected_years(args)
    institutions = _selected_institutions(args)

    if args.rebuild_index:
        crawler = SearchThemaCrawler(
            metadata_only=True,
            resume=args.resume,
            years=years,
            institutions=institutions,
            save_indexes=False,
            state_file=args.state_file,
            headless=not args.headed,
        )
        crawler.metadata_manager.rebuild_indexes()
        logger.info("SearchThema 메타데이터 인덱스 재생성 완료")
        return None

    crawler = SearchThemaCrawler(
        metadata_only=args.metadata_only,
        resume=args.resume,
        limit=args.limit,
        years=years,
        institutions=institutions,
        state_file=args.state_file,
        headless=not args.headed,
        concurrency=args.concurrency,
    )
    stats = await crawler.crawl()
    log_stats(logger, "SearchThema 크롤링 통계", stats)
    return stats


def main() -> None:
    configure_windows_asyncio_policy()
    asyncio.run(run_crawler(parse_args()))


if __name__ == "__main__":
    main()
