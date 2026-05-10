"""Browser-session based petyList crawler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from playwright.async_api import BrowserContext, async_playwright  # type: ignore[reportMissingImports]

    HAS_PLAYWRIGHT = True
except ImportError:
    BrowserContext = Any
    async_playwright = None
    HAS_PLAYWRIGHT = False

try:
    from .base_crawler import BaseCrawler
    from .config import get_config
    from .crawl_state import CrawlState
    from .logger import setup_logger
    from .metadata_manager import MetadataManager
    from .pety_parser import parse_pety_list_page
except ImportError:
    from base_crawler import BaseCrawler  # type: ignore[reportMissingImports]
    from config import get_config  # type: ignore[reportMissingImports]
    from crawl_state import CrawlState  # type: ignore[reportMissingImports]
    from logger import setup_logger  # type: ignore[reportMissingImports]
    from metadata_manager import MetadataManager  # type: ignore[reportMissingImports]
    from pety_parser import parse_pety_list_page  # type: ignore[reportMissingImports]


class GwanboCrawler(BaseCrawler):
    """Collect petyList metadata and PDFs using a Playwright browser context."""

    def __init__(
        self,
        theme: str = "pety",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        resume: bool = True,
        download_pdfs: bool = True,
        metadata_only: bool = False,
        limit: Optional[int] = None,
        window_days: Optional[int] = None,
        headless: Optional[bool] = None,
        save_indexes: bool = True,
        state_file: Optional[str] = None,
    ):
        if theme != "pety":
            raise ValueError("현재 구현된 실행 테마는 pety 입니다.")
        if not HAS_PLAYWRIGHT:
            raise ImportError("Playwright가 설치되지 않았습니다. `pip install playwright`를 실행하세요.")

        self.config = get_config()
        self.logger = setup_logger(__name__)
        self.metadata_manager = MetadataManager()
        self.state = CrawlState(state_file or self.config.get("state.file", "artifacts/state/crawl_state.json"))

        crawler_config = self.config.get_crawler_config()
        download_config = self.config.get_download_config()
        theme_config = crawler_config.get("themes", {}).get(theme, {})

        self.theme = theme
        self.start_date = self._parse_date(start_date or crawler_config.get("start_date", "1994-01-01"))
        self.end_date = self._parse_date(end_date or crawler_config.get("end_date", "today"))
        self.resume = resume
        self.download_pdfs = download_pdfs and not metadata_only
        self.metadata_only = metadata_only
        self.limit = limit
        self.window_days = window_days or int(crawler_config.get("window_days", 31))
        self.headless = crawler_config.get("headless", True) if headless is None else headless
        self.save_indexes = save_indexes
        self.browser_executable_path = crawler_config.get("browser_executable_path", "")

        self.list_url = theme_config.get(
            "list_url",
            crawler_config.get("api_base_url", "https://open.gwanbo.go.kr/OpenApi/web/petyList"),
        )
        self.ajax_url = theme_config.get("ajax_url", "https://open.gwanbo.go.kr/OpenApi/web/petyListAjax")
        self.viewer_base_url = theme_config.get("viewer_base_url", "https://gwanbo.go.kr/")
        self.theme_code = str(theme_config.get("thema_se", "02"))
        self.row_per_page = str(theme_config.get("row_per_page", crawler_config.get("row_per_page", 10)))
        self.timeout_ms = int(crawler_config.get("timeout", 30)) * 1000
        self.request_timeout = int(crawler_config.get("timeout", 30))
        self.retry_delay = float(crawler_config.get("retry_delay", 2))
        self.max_retries = int(crawler_config.get("max_retries", 3))
        self.pdf_dir = Path(download_config.get("pdf_directory", "artifacts/pdfs"))
        self.ocr_ready_dir = Path(download_config.get("ocr_ready_directory", "artifacts/ocr_ready"))
        self.chunk_size = int(download_config.get("chunk_size", 8192))

        self.stats: Dict[str, Any] = {
            "theme": self.theme,
            "total_items": 0,
            "saved_items": 0,
            "skipped_items": 0,
            "downloaded_pdfs": 0,
            "failed_downloads": 0,
            "visited_pages": 0,
            "completed_windows": 0,
            "skipped_windows": 0,
            "start_time": None,
            "end_time": None,
        }
        self._limit_reached = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None

    async def fetch_items(self, page_number: int) -> List[Dict[str, Any]]:
        raise NotImplementedError("GwanboCrawler는 날짜 윈도우 컨텍스트에서 항목을 조회합니다.")

    def get_item_id(self, item: Dict[str, Any]) -> str:
        return str(item["id"])

    async def crawl(self) -> Dict[str, Any]:
        """Run the full crawl."""
        self.stats["start_time"] = datetime.now()
        self.logger.info("=" * 60)
        self.logger.info("petyList 브라우저 세션 크롤링 시작")
        self.logger.info(f"범위: {self.start_date:%Y-%m-%d} ~ {self.end_date:%Y-%m-%d}")
        self.logger.info("=" * 60)

        try:
            assert async_playwright is not None
            async with async_playwright() as playwright:
                browser = await self._launch_browser(playwright)
                context = await browser.new_context(
                    accept_downloads=True,
                    extra_http_headers={
                        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/125.0.0.0 Safari/537.36"
                        ),
                    },
                )
                try:
                    await self._prime_browser_session(context)
                    for window_start, window_end in self._date_windows():
                        if self._limit_reached:
                            break

                        start_text = window_start.strftime("%Y-%m-%d")
                        end_text = window_end.strftime("%Y-%m-%d")
                        if self.resume and self.state.is_window_completed(
                            self.theme,
                            start_text,
                            end_text,
                            self._state_mode(),
                        ):
                            self.logger.info(f"완료된 윈도우 건너뜀: {start_text} ~ {end_text}")
                            self.stats["skipped_windows"] += 1
                            continue

                        window_stats = await self._crawl_window(context, window_start, window_end)
                        if not self._limit_reached and self.limit is None:
                            self.state.mark_window_completed(
                                self.theme,
                                start_text,
                                end_text,
                                window_stats,
                                self._state_mode(),
                            )
                            self.stats["completed_windows"] += 1
                finally:
                    await context.close()
                    await browser.close()
        finally:
            self.stats["end_time"] = datetime.now()
            if self.save_indexes:
                self.metadata_manager.save_metadata()
                self.metadata_manager.save_as_csv()
                self.metadata_manager.save_by_category()
            else:
                self.logger.info("배치 모드: aggregate metadata/csv/category 저장을 건너뜁니다.")

        self.logger.info("=" * 60)
        self.logger.info("petyList 크롤링 완료")
        self.logger.info("=" * 60)
        return self._get_statistics()

    async def _prime_browser_session(self, context: Any) -> None:
        page = await context.new_page()
        try:
            await page.goto(self.list_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        finally:
            await page.close()

    async def _launch_browser(self, playwright):
        launch_options = {"headless": self.headless}
        if self.browser_executable_path:
            launch_options["executable_path"] = self.browser_executable_path
            return await playwright.chromium.launch(**launch_options)

        for executable_path in self._system_browser_candidates():
            if executable_path.exists():
                self.logger.info(f"시스템 브라우저 사용: {executable_path}")
                return await playwright.chromium.launch(
                    headless=self.headless,
                    executable_path=str(executable_path),
                )

        try:
            return await playwright.chromium.launch(**launch_options)
        except Exception as bundled_error:
            self.logger.warning(f"Playwright 번들 Chromium 실행 실패: {bundled_error}")
            raise

    async def _crawl_window(
        self,
        context: Any,
        window_start: datetime,
        window_end: datetime,
    ) -> Dict[str, Any]:
        start_text = window_start.strftime("%Y-%m-%d")
        end_text = window_end.strftime("%Y-%m-%d")
        self.logger.info(f"윈도우 수집: {start_text} ~ {end_text}")

        window_stats = {
            "pages": 0,
            "items": 0,
            "downloaded_pdfs": 0,
            "failed_downloads": 0,
        }

        page_number = 1
        total_pages = 1
        while page_number <= total_pages:
            html = await self._fetch_list_page(context, window_start, window_end, page_number)
            parsed = parse_pety_list_page(html, self.list_url)
            total_pages = max(parsed.total_pages, 1)
            window_stats["pages"] += 1
            self.stats["visited_pages"] += 1

            self.logger.info(
                f"{start_text} ~ {end_text} / page {page_number}/{total_pages}: "
                f"{len(parsed.items)}개 항목"
            )

            for item in parsed.items:
                if self._limit_reached:
                    break
                before_downloaded = self.stats["downloaded_pdfs"]
                before_failed = self.stats["failed_downloads"]
                processed = await self._process_item(context, item)
                if processed:
                    window_stats["items"] += 1
                    window_stats["downloaded_pdfs"] += self.stats["downloaded_pdfs"] - before_downloaded
                    window_stats["failed_downloads"] += self.stats["failed_downloads"] - before_failed

            page_number += 1
            await asyncio.sleep(0.2)

        return window_stats

    async def _fetch_list_page(
        self,
        context: Any,
        start_date: datetime,
        end_date: datetime,
        page_number: int,
    ) -> str:
        form = {
            "rowPerPage": self.row_per_page,
            "currentPage": str(page_number),
            "themaSe": self.theme_code,
            "reqFrom": start_date.strftime("%Y.%m.%d"),
            "reqTo": end_date.strftime("%Y.%m.%d"),
            "search": "",
            "pblcnSearch": "",
            "lawNmSearch": "",
        }
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await context.request.post(
                    self.ajax_url,
                    form=form,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                    timeout=self.timeout_ms,
                )
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}: {self.ajax_url}")
                return await response.text()
            except Exception as e:
                last_error = e
                self.logger.warning(f"목록 요청 재시도 {attempt}/{self.max_retries}: {e}")
                await asyncio.sleep(self.retry_delay)
        raise RuntimeError(f"목록 요청 실패: {last_error}")

    async def _process_item(self, context: Any, item: Dict[str, Any]) -> bool:
        if self.limit is not None and self.stats["total_items"] >= self.limit:
            self._limit_reached = True
            return False

        self.stats["total_items"] += 1
        item["ocr"]["ready_dir"] = str(self.ocr_ready_dir / self._safe_filename(self.get_item_id(item)))

        existing = self.metadata_manager.get_item(self.get_item_id(item))
        if self.resume and existing and self._existing_pdf_is_complete(existing):
            self.metadata_manager.add_item(existing)
            self.stats["skipped_items"] += 1
            self.logger.debug(f"이미 완료된 항목 건너뜀: {self.get_item_id(item)}")
            return True

        if self.download_pdfs:
            item = await self._download_item_pdf(context, item)
        elif self.metadata_only:
            item["status"] = "metadata_only"
            item["pdf"]["status"] = "skipped"

        item["updated_at"] = datetime.now().isoformat()
        self.metadata_manager.save_item(item)
        self.stats["saved_items"] += 1
        return True

    def _date_windows(self) -> Iterable[Tuple[datetime, datetime]]:
        current = self.start_date
        while current <= self.end_date:
            window_end = min(current + timedelta(days=self.window_days - 1), self.end_date)
            yield current, window_end
            current = window_end + timedelta(days=1)

    def _state_mode(self) -> str:
        return "metadata" if self.metadata_only else "pdf"

    @staticmethod
    def _system_browser_candidates() -> List[Path]:
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/chromium"),
        ]


async def main():
    async with GwanboCrawler() as crawler:
        stats = await crawler.crawl()
    print("\n=== 크롤링 통계 ===")
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
