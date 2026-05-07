#!/usr/bin/env python3
"""
웹 스크래핑 기반 관보 크롤러 실행
"""

import asyncio
import sys
from pathlib import Path

# 프로젝트 루트를 Python 경로에 추가
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / 'src'))

from src.crawler_web import GwanboCrawlerWeb


async def main():
    """메인 함수"""
    print("\n" + "=" * 70)
    print("🌐 웹 스크래핑 기반 관보 크롤러")
    print("=" * 70)
    
    crawler = GwanboCrawlerWeb()
    stats = await crawler.crawl()
    
    # 메타데이터 저장
    print("\n💾 메타데이터 저장...")
    crawler.metadata_manager.save_metadata()
    crawler.metadata_manager.save_as_csv()
    
    # 통계 출력
    print("\n📊 크롤링 통계:")
    print("-" * 70)
    for key, value in stats.items():
        print(f"  {key}: {value}")
    
    metadata_stats = crawler.metadata_manager.get_statistics()
    print("\n📊 메타데이터 통계:")
    print("-" * 70)
    print(f"  총 항목: {metadata_stats['total_items']}")
    for status, count in metadata_stats['statuses'].items():
        print(f"    {status}: {count}")
    
    print("\n✅ 완료!")


if __name__ == '__main__':
    asyncio.run(main())
