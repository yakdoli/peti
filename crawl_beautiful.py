#!/usr/bin/env python3
"""
BeautifulSoup 기반 고급 웹 스크래핑 크롤러
실제 관보 웹사이트의 테이블 데이터를 효율적으로 수집
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
import sys

# 프로젝트 경로 추가
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / 'src'))

from config import get_config
from logger import setup_logger
from metadata_manager import MetadataManager

import asyncio
import aiohttp
from bs4 import BeautifulSoup
import time


class GwanboCrawlerBeautiful:
    """BeautifulSoup 기반 고급 웹 크롤러"""

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
            'pages_visited': 0,
            'failed_pages': 0,
            'start_time': None,
            'end_time': None,
        }
        
        # 헤더 (실제 브라우저처럼 식별)
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                         'AppleWebKit/537.36 (KHTML, like Gecko) '
                         'Chrome/125.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ko-KR,ko;q=0.9',
            'Referer': 'https://open.gwanbo.go.kr/',
        }

    async def crawl_date_range(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """날짜 범위 크롤링"""
        self.stats['start_time'] = datetime.now()
        self.logger.info("=" * 60)
        self.logger.info("BeautifulSoup 웹 크롤러 시작")
        self.logger.info(f"날짜 범위: {start_date} ~ {end_date}")
        self.logger.info("=" * 60)
        
        try:
            connector = aiohttp.TCPConnector(limit_per_host=5)
            timeout = aiohttp.ClientTimeout(total=30)
            
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                # 날짜 범위 파싱
                start_dt = self._parse_date(start_date)
                end_dt = self._parse_date(end_date)
                
                # 1단계: 초기 데이터 수집 (현재 페이지 범위)
                self.logger.info(f"초기 페이지 데이터 수집...")
                await self._crawl_initial_page(session, start_date, end_date)
                
                # 2단계: 페이지네이션 처리
                self.logger.info(f"페이지네이션 처리...")
                await self._crawl_paginated(session, start_date, end_date)
            
            self.logger.info("=" * 60)
            self.logger.info("크롤링 완료")
            self.logger.info("=" * 60)
            
        except Exception as e:
            self.logger.error(f"크롤링 오류: {e}")
        finally:
            self.stats['end_time'] = datetime.now()
        
        return self._get_statistics()

    async def _crawl_initial_page(self, session: aiohttp.ClientSession, 
                                 start_date: str, end_date: str) -> None:
        """초기 페이지 크롤링"""
        try:
            self.logger.debug(f"초기 페이지 요청: {start_date} ~ {end_date}")
            
            # 쿼리 파라미터 구성
            params = {
                'pblancStartDate': start_date.replace('-', '.'),
                'pblancEndDate': end_date.replace('-', '.'),
                'pageNum': '1',
                'pageSize': '10',
            }
            
            html = await self._fetch_page(session, self.base_url, params)
            
            if html:
                items = await self._parse_table(html, start_date, end_date)
                self.logger.info(f"초기 페이지: {len(items)}개 항목 발견")
                
                for item in items:
                    self.metadata_manager.add_item(item)
                    self.stats['total_items'] += 1
                
                self.stats['pages_visited'] += 1
        except Exception as e:
            self.logger.error(f"초기 페이지 크롤링 오류: {e}")
            self.stats['failed_pages'] += 1

    async def _crawl_paginated(self, session: aiohttp.ClientSession,
                              start_date: str, end_date: str) -> None:
        """페이지네이션 크롤링"""
        try:
            # 페이지 2~10 시도
            for page_num in range(2, 11):
                try:
                    self.logger.debug(f"페이지 {page_num} 크롤링...")
                    
                    params = {
                        'pblancStartDate': start_date.replace('-', '.'),
                        'pblancEndDate': end_date.replace('-', '.'),
                        'pageNum': str(page_num),
                        'pageSize': '10',
                    }
                    
                    html = await self._fetch_page(session, self.base_url, params)
                    
                    if html:
                        items = await self._parse_table(html, start_date, end_date)
                        
                        if len(items) == 0:
                            self.logger.debug(f"페이지 {page_num}: 데이터 없음, 종료")
                            break
                        
                        self.logger.info(f"페이지 {page_num}: {len(items)}개 항목 발견")
                        
                        for item in items:
                            self.metadata_manager.add_item(item)
                            self.stats['total_items'] += 1
                        
                        self.stats['pages_visited'] += 1
                    else:
                        break
                    
                    await asyncio.sleep(0.5)  # 부하 분산
                    
                except Exception as e:
                    self.logger.warning(f"페이지 {page_num} 크롤링 오류: {e}")
                    self.stats['failed_pages'] += 1
                    continue
        except Exception as e:
            self.logger.error(f"페이지네이션 크롤링 오류: {e}")

    async def _fetch_page(self, session: aiohttp.ClientSession, 
                         url: str, params: Dict[str, str] = None) -> Optional[str]:
        """페이지 HTML 받기"""
        try:
            async with session.get(url, params=params, headers=self.headers) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    self.logger.warning(f"HTTP {resp.status}: {url}")
                    return None
        except asyncio.TimeoutError:
            self.logger.error(f"타임아웃: {url}")
            return None
        except Exception as e:
            self.logger.error(f"페이지 요청 오류 ({url}): {e}")
            return None

    async def _parse_table(self, html: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """테이블 HTML 파싱"""
        items = []
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # 테이블 찾기
            table = soup.find('table', {'role': 'table'})
            if not table:
                table = soup.find('table')
            
            if not table:
                self.logger.warning("테이블을 찾을 수 없음")
                return items
            
            # 테이블 행 추출
            rows = table.find_all('tr')
            
            self.logger.debug(f"테이블: {len(rows)}개 행 발견")
            
            # 헤더 제외 (첫 번째 행)
            for idx, row in enumerate(rows[1:], 1):
                try:
                    cells = row.find_all('td')
                    
                    if len(cells) >= 4:
                        # 텍스트 추출
                        title = cells[0].get_text(strip=True)
                        agency = cells[1].get_text(strip=True)
                        law = cells[2].get_text(strip=True)
                        date_text = cells[3].get_text(strip=True)
                        
                        if title:  # 제목이 있으면 유효한 항목
                            item = {
                                'id': f"{datetime.now().strftime('%Y%m%d')}{idx:03d}",
                                'title': title,
                                'agency': agency if agency else "미정",
                                'law': law if law else "미정",
                                'date': self._normalize_date(date_text),
                                'url': self.base_url,
                                'status': 'discovered',
                                'source': 'beautiful_soup_scraping',
                                'discovered_date': datetime.now().isoformat(),
                                'category': self._categorize(title),
                            }
                            items.append(item)
                except Exception as e:
                    self.logger.debug(f"행 파싱 오류 (행 {idx}): {e}")
                    continue
        except Exception as e:
            self.logger.error(f"테이블 파싱 오류: {e}")
        
        return items

    def _parse_date(self, date_str: str) -> datetime:
        """날짜 문자열을 datetime으로 변환"""
        formats = ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d', '%Y.%m.%d']
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return datetime.now()

    def _normalize_date(self, date_str: str) -> str:
        """날짜 문자열을 정규화 (YYYY-MM-DD)"""
        if not date_str:
            return datetime.now().strftime('%Y-%m-%d')
        
        formats = [
            ('%Y-%m-%d', '%Y-%m-%d'),
            ('%Y/%m/%d', '%Y-%m-%d'),
            ('%Y%m%d', '%Y-%m-%d'),
            ('%Y.%m.%d', '%Y-%m-%d'),
            ('%Y년%m월%d일', '%Y-%m-%d'),
        ]
        
        for input_fmt, output_fmt in formats:
            try:
                dt = datetime.strptime(date_str.strip(), input_fmt)
                return dt.strftime(output_fmt)
            except ValueError:
                continue
        
        return date_str.replace('.', '-').replace('/', '-')

    def _categorize(self, title: str) -> str:
        """제목에서 카테고리 추출"""
        keywords = {
            '공고': '공고',
            '공시': '공시',
            '고시': '고시',
            '알림': '알림',
            '보도': '보도',
        }
        
        title_lower = title.lower()
        for keyword, category in keywords.items():
            if keyword in title_lower:
                return category
        
        return '기타'

    def _get_statistics(self) -> Dict[str, Any]:
        """통계 반환"""
        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds() if \
                   self.stats['end_time'] and self.stats['start_time'] else 0
        
        return {
            'total_items': self.stats['total_items'],
            'pages_visited': self.stats['pages_visited'],
            'failed_pages': self.stats['failed_pages'],
            'duration_seconds': duration,
            'items_per_second': self.stats['total_items'] / duration if duration > 0 else 0,
            'start_time': self.stats['start_time'].isoformat() if self.stats['start_time'] else None,
            'end_time': self.stats['end_time'].isoformat() if self.stats['end_time'] else None,
        }


async def main():
    """메인 함수"""
    crawler = GwanboCrawlerBeautiful()
    
    print("\n" + "=" * 70)
    print("🕷️  BeautifulSoup 웹 크롤러")
    print("=" * 70)
    
    config = get_config()
    start_date = config.get_crawler_config().get('start_date', '1994-01-01')
    end_date = config.get_crawler_config().get('end_date', datetime.now().strftime('%Y-%m-%d'))
    
    stats = await crawler.crawl_date_range(start_date, end_date)
    
    # 메타데이터 저장
    print("\n💾 메타데이터 저장...")
    crawler.metadata_manager.save_metadata()
    crawler.metadata_manager.save_as_csv()
    crawler.metadata_manager.save_by_category()
    
    # 통계 출력
    print("\n📊 크롤링 통계:")
    print("-" * 70)
    for key, value in stats.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.2f}")
        else:
            print(f"  {key}: {value}")


if __name__ == '__main__':
    asyncio.run(main())
