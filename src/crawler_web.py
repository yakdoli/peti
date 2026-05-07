#!/usr/bin/env python3
"""
관보 웹페이지 스크래핑 기반 크롤러
API 직접 호출 대신 웹페이지를 파싱하여 데이터 수집
"""

import asyncio
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any
import sys

# 프로젝트 경로 추가
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / 'src'))

from config import get_config
from logger import setup_logger
from metadata_manager import MetadataManager


class GwanboCrawlerWeb:
    """웹 스크래핑 기반 관보 크롤러"""

    def __init__(self):
        """초기화"""
        self.config = get_config()
        self.logger = setup_logger(__name__)
        self.metadata_manager = MetadataManager()
        
        self.base_url = "https://open.gwanbo.go.kr/OpenApi/web/petyList"
        self.crawler_config = self.config.get_crawler_config()
        self.download_config = self.config.get_download_config()
        
        self.stats = {
            'total_items': 0,
            'downloaded_pages': 0,
            'failed_downloads': 0,
            'start_time': None,
            'end_time': None,
        }

    async def fetch_page_html(self, session: aiohttp.ClientSession, date: str, page: int = 1) -> str:
        """웹페이지 HTML 가져오기"""
        params = {
            'pblancStartDate': date,
            'pblancEndDate': date,
            'pageNum': page,
        }
        
        try:
            async with session.get(
                self.base_url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    return await response.text()
                else:
                    self.logger.warning(f"HTML 가져오기 실패 ({response.status}): {date}")
                    return None
        except Exception as e:
            self.logger.error(f"HTML 가져오기 오류: {e}")
            return None

    def parse_table_data(self, html: str) -> List[Dict[str, Any]]:
        """테이블에서 데이터 파싱"""
        if not html:
            return []
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # 테이블 찾기
            table = soup.find('table')
            if not table:
                self.logger.debug("테이블을 찾을 수 없습니다")
                return []
            
            items = []
            rows = table.find_all('tr')[1:]  # 헤더 제외
            
            for idx, row in enumerate(rows):
                cells = row.find_all('td')
                if len(cells) >= 4:
                    try:
                        item = {
                            'id': f"{len(items)}_{idx}",  # 임시 ID
                            'title': cells[0].get_text(strip=True),
                            'agency': cells[1].get_text(strip=True),
                            'law': cells[2].get_text(strip=True),
                            'date': cells[3].get_text(strip=True),
                            'url': self.base_url,
                            'source': 'web_scraping'
                        }
                        items.append(item)
                    except Exception as e:
                        self.logger.debug(f"행 파싱 오류: {e}")
                        continue
            
            return items
        except Exception as e:
            self.logger.error(f"테이블 파싱 오류: {e}")
            return []

    async def crawl_date(self, session: aiohttp.ClientSession, date: str) -> int:
        """특정 날짜 데이터 크롤링"""
        self.logger.info(f"날짜 크롤링: {date}")
        
        # HTML 가져오기
        html = await self.fetch_page_html(session, date)
        if not html:
            return 0
        
        self.stats['downloaded_pages'] += 1
        
        # 데이터 파싱
        items = self.parse_table_data(html)
        self.logger.info(f"{date}: {len(items)}개 항목 발견")
        
        # 메타데이터 저장
        for item in items:
            metadata = {
                'id': f"{date.replace('-', '')}{item['id']}",
                'title': item['title'],
                'date': date,
                'agency': item['agency'],
                'law': item['law'],
                'url': item['url'],
                'status': 'discovered',
                'source': item['source'],
                'discovered_date': datetime.now().isoformat(),
            }
            
            self.metadata_manager.add_item(metadata)
            self.stats['total_items'] += 1
        
        return len(items)

    def _parse_date(self, date_str: str) -> datetime:
        """날짜 문자열을 datetime으로 변환"""
        formats = ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d']
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return datetime.now()

    async def crawl(self) -> Dict[str, Any]:
        """크롤링 실행"""
        self.stats['start_time'] = datetime.now()
        self.logger.info("=" * 60)
        self.logger.info("웹 스크래핑 기반 관보 크롤러 시작")
        self.logger.info("=" * 60)
        
        try:
            start_str = self.crawler_config.get('start_date', '1994-01-01')
            end_str = self.crawler_config.get('end_date', datetime.now().strftime('%Y-%m-%d'))
            
            start_date = self._parse_date(start_str)
            end_date = self._parse_date(end_str)
            
            self.logger.info(f"크롤링 범위: {start_str} ~ {end_str}")
            
            async with aiohttp.ClientSession() as session:
                current = start_date
                while current <= end_date:
                    date_str = current.strftime('%Y-%m-%d')
                    
                    try:
                        await self.crawl_date(session, date_str)
                    except Exception as e:
                        self.logger.error(f"날짜 크롤링 오류 ({date_str}): {e}")
                    
                    current += timedelta(days=1)
                    await asyncio.sleep(0.5)  # API 부하 방지
            
            self.logger.info("=" * 60)
            self.logger.info("크롤링 완료")
            self.logger.info("=" * 60)
            
        except Exception as e:
            self.logger.error(f"크롤링 오류: {e}")
        finally:
            self.stats['end_time'] = datetime.now()
        
        return self._get_statistics()

    def _get_statistics(self) -> Dict[str, Any]:
        """통계 반환"""
        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds() if \
                   self.stats['end_time'] and self.stats['start_time'] else 0
        
        return {
            'total_items': self.stats['total_items'],
            'downloaded_pages': self.stats['downloaded_pages'],
            'failed_downloads': self.stats['failed_downloads'],
            'duration_seconds': duration,
            'start_time': self.stats['start_time'].isoformat() if self.stats['start_time'] else None,
            'end_time': self.stats['end_time'].isoformat() if self.stats['end_time'] else None,
        }


async def main():
    """메인 함수"""
    crawler = GwanboCrawlerWeb()
    
    print("\n" + "=" * 70)
    print("🌐 웹 스크래핑 기반 관보 크롤러")
    print("=" * 70)
    
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


if __name__ == '__main__':
    asyncio.run(main())
