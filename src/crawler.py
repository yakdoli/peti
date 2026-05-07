"""Browser-session based petyList crawler."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import aiohttp

try:
    from playwright.async_api import BrowserContext, async_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    BrowserContext = Any
    HAS_PLAYWRIGHT = False

try:
    from .config import get_config
    from .crawl_state import CrawlState
    from .logger import setup_logger
    from .metadata_manager import MetadataManager
    from .pety_parser import parse_pety_list_page
except ImportError:
    from config import get_config
    from crawl_state import CrawlState
    from logger import setup_logger
    from metadata_manager import MetadataManager
    from pety_parser import parse_pety_list_page


class GwanboCrawler:
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
        self.state = CrawlState(state_file or self.config.get("state.file", "data/state/crawl_state.json"))

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
        self.pdf_dir = Path(download_config.get("pdf_directory", "data/pdfs"))
        self.ocr_ready_dir = Path(download_config.get("ocr_ready_directory", "data/ocr_ready"))
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

    async def crawl(self) -> Dict[str, Any]:
        """Run the full crawl."""
        self.stats["start_time"] = datetime.now()
        self.logger.info("=" * 60)
        self.logger.info("petyList 브라우저 세션 크롤링 시작")
        self.logger.info(f"범위: {self.start_date:%Y-%m-%d} ~ {self.end_date:%Y-%m-%d}")
        self.logger.info("=" * 60)

        try:
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

    async def _prime_browser_session(self, context: BrowserContext) -> None:
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
        context: BrowserContext,
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
        context: BrowserContext,
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

    async def _process_item(self, context: BrowserContext, item: Dict[str, Any]) -> bool:
        if self.limit is not None and self.stats["total_items"] >= self.limit:
            self._limit_reached = True
            return False

        self.stats["total_items"] += 1
        item["ocr"]["ready_dir"] = str(self.ocr_ready_dir / self._safe_filename(item["id"]))

        existing = self.metadata_manager.get_item(item["id"])
        if self.resume and existing and self._existing_pdf_is_complete(existing):
            self.metadata_manager.add_item(existing)
            self.stats["skipped_items"] += 1
            self.logger.debug(f"이미 완료된 항목 건너뜀: {item['id']}")
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

    async def _download_item_pdf(self, context: BrowserContext, item: Dict[str, Any]) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                result = await self._download_item_pdf_once(context, item)
                if attempt > 1:
                    self.logger.info(f"PDF 다운로드 재시도 성공 ({item.get('id')}): {attempt}/{self.max_retries}")
                return result
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    self.logger.warning(
                        f"PDF 다운로드 재시도 {attempt}/{self.max_retries} ({item.get('id')}): {e}"
                    )
                    await asyncio.sleep(self.retry_delay * attempt)

        item["pdf"]["status"] = "failed"
        item["pdf"]["error"] = str(last_error)
        item["status"] = "download_failed"
        self.stats["failed_downloads"] += 1
        self.logger.warning(f"PDF 다운로드 실패 ({item.get('id')}): {last_error}")
        return item

    async def _download_item_pdf_once(self, context: BrowserContext, item: Dict[str, Any]) -> Dict[str, Any]:
        viewer_path = item.get("viewer_path", "")
        if not viewer_path:
            raise RuntimeError("viewer_path가 없습니다.")

        viewer_url = self._viewer_url_for_item(item, viewer_path)
        viewer_response = await context.request.get(viewer_url, timeout=self.timeout_ms)
        if viewer_response.status != 200:
            raise RuntimeError(f"뷰어 요청 실패: HTTP {viewer_response.status}")
        viewer_html = await viewer_response.text()

        download_url, form_data = self._extract_download_request(viewer_html, item)
        pdf_path = self._pdf_path_for_item(item)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        result = await self._download_pdf_stream(context, download_url, form_data, pdf_path)
        item["pdf"].update(result)
        item["status"] = "completed"
        self.stats["downloaded_pdfs"] += 1
        self.logger.info(f"PDF 다운로드 완료: {pdf_path}")
        return item

    def _viewer_url_for_item(self, item: Dict[str, Any], viewer_path: str) -> str:
        content_id = item.get("content_id")
        toc_id = item.get("toc_id")
        if content_id and toc_id:
            return urljoin(
                self.viewer_base_url,
                f"ezpdf/customLayout.jsp?contentId={content_id}&tocId={toc_id}&isTocOrder=N",
            )
        return urljoin(self.viewer_base_url, viewer_path.lstrip("/"))

    def _extract_download_request(self, viewer_html: str, item: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
        content_match = re.search(
            r"(/user/common/ofcttCntntDownload\.do(?:;jsessionid=[A-Za-z0-9_.-]+)?)",
            viewer_html,
        )
        if content_match and item.get("toc_id"):
            return urljoin(self.viewer_base_url, content_match.group(1)), {"cntnt_seq_no": item["toc_id"]}

        issue_match = re.search(
            r"(/user/common/ofcttDownload\.do(?:;jsessionid=[A-Za-z0-9_.-]+)?)",
            viewer_html,
        )
        if issue_match and item.get("content_id"):
            return urljoin(self.viewer_base_url, issue_match.group(1)), {
                "downType": "1",
                "ofctt_seq_no": item["content_id"],
            }

        raise RuntimeError("PDF 다운로드 엔드포인트를 찾을 수 없습니다.")

    async def _download_pdf_stream(
        self,
        context: BrowserContext,
        download_url: str,
        form_data: Dict[str, str],
        pdf_path: Path,
    ) -> Dict[str, Any]:
        cookies = await context.cookies(self.viewer_base_url)
        cookie_header = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": self.viewer_base_url,
        }
        if cookie_header:
            headers["Cookie"] = cookie_header

        temp_path = pdf_path.with_suffix(".pdf.tmp")
        sha256 = hashlib.sha256()
        size = 0

        timeout = aiohttp.ClientTimeout(total=max(self.request_timeout, 60))
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(download_url, data=form_data) as response:
                if response.status != 200:
                    raise RuntimeError(f"PDF 요청 실패: HTTP {response.status}")
                with open(temp_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(self.chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        sha256.update(chunk)
                        size += len(chunk)

        with open(temp_path, "rb") as f:
            header = f.read(5)
        if header != b"%PDF-":
            temp_path.unlink(missing_ok=True)
            raise RuntimeError("다운로드 결과가 PDF가 아닙니다.")

        temp_path.replace(pdf_path)
        return {
            "status": "completed",
            "path": str(pdf_path),
            "size_bytes": size,
            "sha256": sha256.hexdigest(),
            "downloaded_at": datetime.now().isoformat(),
        }

    def _date_windows(self) -> Iterable[Tuple[datetime, datetime]]:
        current = self.start_date
        while current <= self.end_date:
            window_end = min(current + timedelta(days=self.window_days - 1), self.end_date)
            yield current, window_end
            current = window_end + timedelta(days=1)

    def _pdf_path_for_item(self, item: Dict[str, Any]) -> Path:
        date_text = item.get("date", "unknown")
        year = date_text[:4] if re.match(r"^\d{4}", date_text) else "unknown"
        date_key = date_text.replace("-", "") if re.match(r"^\d{4}-\d{2}-\d{2}$", date_text) else "unknown"
        return self.pdf_dir / year / date_key / f"{self._safe_filename(item['id'])}.pdf"

    def _existing_pdf_is_complete(self, item: Dict[str, Any]) -> bool:
        pdf = item.get("pdf", {}) or {}
        path = Path(str(pdf.get("path", "")))
        return pdf.get("status") == "completed" and path.exists() and path.stat().st_size > 0

    def _parse_date(self, date_text: str) -> datetime:
        text = (date_text or "").strip()
        if text.lower() == "today":
            return datetime.now()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        raise ValueError(f"날짜 형식을 파싱할 수 없습니다: {date_text}")

    def _get_statistics(self) -> Dict[str, Any]:
        duration = (
            self.stats["end_time"] - self.stats["start_time"]
        ).total_seconds() if self.stats["end_time"] and self.stats["start_time"] else 0
        result = dict(self.stats)
        result["duration_seconds"] = duration
        result["start_time"] = self.stats["start_time"].isoformat() if self.stats["start_time"] else None
        result["end_time"] = self.stats["end_time"].isoformat() if self.stats["end_time"] else None
        return result

    def _state_mode(self) -> str:
        return "metadata" if self.metadata_only else "pdf"

    @staticmethod
    def _safe_filename(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", str(value)).strip("._") or "unknown"

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
