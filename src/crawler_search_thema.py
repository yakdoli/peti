"""HTTP-based SearchThema crawler."""

from __future__ import annotations

import asyncio
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

try:
    from playwright.async_api import BrowserContext, async_playwright  # type: ignore[reportMissingImports]

    HAS_PLAYWRIGHT = True
except ImportError:
    BrowserContext = Any
    async_playwright = None
    HAS_PLAYWRIGHT = False

try:
    import aiohttp  # type: ignore[reportMissingImports]
except ImportError:
    class _MissingAioHttp:
        class ClientTimeout:
            def __init__(self, *args: Any, **kwargs: Any):
                self.args = args
                self.kwargs = kwargs

        class ClientSession:
            def __init__(self, *args: Any, **kwargs: Any):
                self.args = args
                self.kwargs = kwargs

            async def __aenter__(self):
                raise ImportError("aiohttp가 설치되지 않았습니다. `pip install aiohttp`를 실행하세요.")

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def post(self, *args: Any, **kwargs: Any):
                raise ImportError("aiohttp가 설치되지 않았습니다. `pip install aiohttp`를 실행하세요.")

    aiohttp = _MissingAioHttp()

try:
    from .base_crawler import BaseCrawler
    from .config import get_config
    from .crawl_state import CrawlState
    from .metadata_manager import MetadataManager
    from .search_thema_parser import parse_search_thema_response
except ImportError:
    from base_crawler import BaseCrawler  # type: ignore[reportMissingImports]
    from config import get_config  # type: ignore[reportMissingImports]
    from crawl_state import CrawlState  # type: ignore[reportMissingImports]
    from metadata_manager import MetadataManager  # type: ignore[reportMissingImports]
    from search_thema_parser import parse_search_thema_response  # type: ignore[reportMissingImports]

try:
    from .logger import setup_logger
except ImportError:
    try:
        from logger import setup_logger  # type: ignore[reportMissingImports]
    except ImportError:
        def setup_logger(name: str):
            return logging.getLogger(name)


class SearchThemaCrawler(BaseCrawler):
    """Fetch SearchThema metadata pages over HTTP POST."""

    def __init__(
        self,
        metadata_only: bool = True,
        resume: bool = True,
        limit: int | None = None,
        years: Iterable[int | str] | None = None,
        institutions: Iterable[str | None] | None = None,
        save_indexes: bool = True,
        state_file: str | None = None,
        headless: bool = True,
        concurrency: int = 5,
    ):
        super().__init__()

        self.config = get_config()
        self.logger = setup_logger(__name__)

        crawler_config = self.config.get_crawler_config()
        download_config = self.config.get_download_config()
        search_config = self.config.get_search_thema_config()

        self.theme = "searchThema"
        self.metadata_only = metadata_only
        self.download_pdfs = not metadata_only
        self.resume = resume
        self.limit = limit
        self.save_indexes = save_indexes
        self.headless = headless
        self.concurrency = concurrency
        self._limit_reached = False
        self.search_api_url = search_config.get("search_api_url", "https://gwanbo.go.kr/SearchRestApi.jsp")
        self.theme_info_url = search_config.get("theme_info_url", "https://gwanbo.go.kr/user/search/getThemeBaseInfo.do")
        self.viewer_base_url = search_config.get("viewer_base_url", "https://gwanbo.go.kr/")
        self.search_index = str(search_config.get("index", "gwanbo"))
        self.list_size = int(search_config.get("list_size", 10))
        self.institution_query_map = dict(search_config.get("institution_query_map", {}))
        self.years = [str(year) for year in years] if years is not None else self._configured_years(search_config)
        self.institutions = list(institutions) if institutions is not None else self._configured_institutions(search_config)

        self.timeout_ms = int(crawler_config.get("timeout", 30)) * 1000
        self.request_timeout = int(crawler_config.get("timeout", 30))
        self.retry_delay = float(crawler_config.get("retry_delay", 2))
        self.max_retries = int(crawler_config.get("max_retries", 3))
        self.chunk_size = int(download_config.get("chunk_size", 8192))
        self.pdf_dir = self._search_thema_pdf_dir(download_config.get("pdf_directory", "artifacts/pdfs"))
        self.metadata_manager = MetadataManager()
        self._use_search_thema_metadata_dir(download_config.get("metadata_directory", "artifacts/metadata"))
        self.state = CrawlState(state_file or self.config.get("state.file", "artifacts/state/crawl_state.json"))

        self.stats: Dict[str, Any] = {
            "theme": self.theme,
            "total_items": 0,
            "saved_items": 0,
            "skipped_items": 0,
            "visited_pages": 0,
            "downloaded_pdfs": 0,
            "failed_downloads": 0,
            "completed_combinations": 0,
            "skipped_combinations": 0,
            "start_time": None,
            "end_time": None,
        }

    async def crawl(self, context: Any = None) -> Dict[str, Any]:
        self.stats["start_time"] = datetime.now()
        self.logger.info("=" * 60)
        self.logger.info("SearchThema 크롤링 시작")
        self.logger.info("=" * 60)

        try:
            if context is None and HAS_PLAYWRIGHT:
                assert async_playwright is not None
                try:
                    async with async_playwright() as playwright:
                        browser = await playwright.chromium.launch(headless=self.headless)
                        browser_context = await browser.new_context(ignore_https_errors=True)
                        try:
                            await self._prime_browser_session(browser_context)
                            await self._crawl_all_combinations(browser_context)
                        finally:
                            await browser_context.close()
                            await browser.close()
                except Exception as exc:
                    self.logger.warning(f"브라우저 세션 초기화 실패, HTTP 모드로 진행: {exc}")
                    await self._crawl_all_combinations(None)
            else:
                await self._crawl_all_combinations(context)
        finally:
            self.stats["end_time"] = datetime.now()
            if self.save_indexes:
                self.metadata_manager.save_metadata()
                self.metadata_manager.save_as_csv()
                self.metadata_manager.save_by_category()

        self.logger.info("=" * 60)
        self.logger.info("SearchThema 크롤링 완료")
        self.logger.info("=" * 60)
        return self._get_statistics()


    async def _crawl_all_combinations(self, context: Any) -> None:
        for year in self.years:
            if self._limit_reached:
                break
            for institution in self.institutions:
                if self._limit_reached:
                    break
                if self._should_skip_combination(year, institution):
                    continue

                try:
                    combination_stats = await self._crawl_combination(context, year, institution)
                except Exception as exc:
                    self.logger.error(f"조합 수집 실패 {year}/{institution}: {exc}")
                    combination_stats = {"year": str(year), "institution": self._institution_state_value(institution), "error": str(exc)}
                if not self._limit_reached and self.limit is None:
                    self.state.mark_search_thema_completed(
                        str(year),
                        self._institution_state_value(institution),
                        self._state_mode(),
                        combination_stats,
                    )
                    self.stats["completed_combinations"] += 1

    async def _prime_browser_session(self, context: BrowserContext) -> None:
        page = await context.new_page()
        try:
            await page.goto(self.theme_info_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        finally:
            await page.close()

    async def _crawl_combination(self, context: Any, year: int | str, institution: str | None) -> Dict[str, Any]:
        self.logger.info(f"SearchThema 조합 수집: {year} / {self._institution_log_value(institution)}")
        combination_stats = {
            "year": str(year),
            "institution": self._institution_state_value(institution),
            "pages": 0,
            "items": 0,
            "saved_items": 0,
            "skipped_items": 0,
            "downloaded_pdfs": 0,
            "failed_downloads": 0,
        }

        page_number = 1
        while not self._limit_reached:
            items = await self.fetch_items(year, institution, page_number, context=context)
            if not items:
                break
            combination_stats["pages"] += 1

            # Phase 1: Prepare and skip-check (fast, sequential)
            to_process: List[Dict[str, Any]] = []
            to_download: List[Dict[str, Any]] = []
            for item in items:
                if self._limit_reached:
                    break
                prepared = self._metadata_item_from_raw(item)
                combination_stats["items"] += 1
                self.stats["total_items"] += 1
                if self._should_skip_item(prepared):
                    combination_stats["skipped_items"] += 1
                    continue
                if self.download_pdfs:
                    to_download.append(prepared)
                else:
                    prepared["status"] = "metadata_only"
                    prepared["pdf"]["status"] = "skipped"
                    prepared["updated_at"] = datetime.now().isoformat()
                    self.metadata_manager.save_item(prepared)
                    self.stats["saved_items"] += 1
                    combination_stats["saved_items"] += 1

            # Phase 2: Concurrent PDF downloads
            if to_download:
                sem = asyncio.Semaphore(self.concurrency)

                async def _download_one(itm: Dict[str, Any]) -> Dict[str, Any]:
                    async with sem:
                        return await self._download_item_pdf(context, itm)

                results = await asyncio.gather(
                    *[_download_one(itm) for itm in to_download],
                    return_exceptions=True,
                )
                for _i, result in enumerate(results):
                    item_ref = to_download[_i]
                    if isinstance(result, BaseException):
                        item_ref["pdf"]["status"] = "failed"
                        item_ref["pdf"]["error"] = str(result)
                        item_ref["status"] = "download_failed"
                        self.stats["failed_downloads"] += 1
                        combination_stats["failed_downloads"] += 1
                    elif isinstance(result, dict):
                        item_ref.update(result)
                        if item_ref.get("pdf", {}).get("status") == "completed":
                            combination_stats["downloaded_pdfs"] += 1
                    item_ref["updated_at"] = datetime.now().isoformat()
                    self.metadata_manager.save_item(item_ref)
                    self.stats["saved_items"] += 1
                    combination_stats["saved_items"] += 1

            page_number += 1
            await self._sleep(0.2)

        return combination_stats

    def _build_query(self, year: int | str, institution: Optional[str]) -> str:
        order_filter = "keyword_category_order:(@@ORDER_NUM)"
        year_text = str(year)
        if institution is None:
            return f"unstored_field_subject:({year_text}) AND {order_filter}"

        institution_query = self.institution_query_map.get(institution, institution)
        return f"unstored_field_subject:({year_text} AND {institution_query}) AND {order_filter}"

    async def fetch_items(
        self,
        page_number: int | str,
        *args: Any,
        year: int | str | None = None,
        institution: Optional[str] = None,
        context: Any = None,
    ) -> List[Dict[str, Any]]:
        if len(args) == 2:
            year = page_number
            institution = args[0]
            page_number = int(args[1])
        elif args:
            raise TypeError("fetch_items() expects either (page_number) or (year, institution, page_number)")

        if year is None:
            raise ValueError("year is required")

        page_number = int(page_number)

        query = self._build_query(year, institution)
        payload = {
            "mode": "theme",
            "index": self.search_index,
            "query": query,
            "pageNo": page_number,
            "listSize": self.list_size,
            "tab_Year1": str(year),
        }
        if institution is not None:
            payload["GOV_1"] = institution

        last_error: Exception | None = None
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        for attempt in range(1, self.max_retries + 1):
            try:
                if context is not None and hasattr(context, "request"):
                    response = await context.request.post(
                        self.search_api_url,
                        form=payload,
                        timeout=self.timeout_ms,
                    )
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}: {self.search_api_url}")
                    response_json = await response.json()
                    if response_json.get("error"):
                        raise RuntimeError(f"SearchThema API 오류: {response_json['error']}")
                else:
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(self.search_api_url, data=payload) as response:
                            if response.status != 200:
                                raise RuntimeError(f"HTTP {response.status}: {self.search_api_url}")
                            response_json = await self._response_json(response)
                            if response_json.get("error"):
                                raise RuntimeError(f"SearchThema API 오류: {response_json['error']}")

                pages = parse_search_thema_response(response_json)
                self.stats["visited_pages"] += 1
                if not any(page.items for page in pages):
                    return []

                items: List[Dict[str, Any]] = []
                for entry in response_json.get("data") or []:
                    items.extend(entry.get("list") or [])
                return items
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self.logger.warning(f"SearchThema 목록 요청 재시도 {attempt}/{self.max_retries}: {exc}")
                    await self._sleep(self.retry_delay)

        raise RuntimeError(f"SearchThema 목록 요청 실패: {last_error}")

    def get_item_id(self, item: Dict[str, Any]) -> str:
        return str(item.get("stored_toc_seq") or item.get("id") or "")

    def _state_mode(self) -> str:
        return "metadata" if self.metadata_only else "pdf"

    async def _process_item(self, context: Any, item: Dict[str, Any]) -> bool:
        if self.limit is not None and self.stats["total_items"] >= self.limit:
            self._limit_reached = True
            return False

        item = self._metadata_item_from_raw(item)
        self.stats["total_items"] += 1

        if self._should_skip_item(item):
            return True

        if self.download_pdfs:
            item = await self._download_item_pdf(context, item)
        else:
            item["status"] = "metadata_only"
            item["pdf"]["status"] = "skipped"

        item["updated_at"] = datetime.now().isoformat()
        self.metadata_manager.save_item(item)
        self.stats["saved_items"] += 1
        return True

    def _should_skip_combination(self, year: int | str, institution: str | None) -> bool:
        if not self.resume:
            return False
        state_institution = self._institution_state_value(institution)
        if self.state.is_search_thema_completed(str(year), state_institution, self._state_mode()):
            self.stats["skipped_combinations"] += 1
            self.logger.info(f"완료된 조합 건너뜀: {year} / {state_institution}")
            return True
        return False

    def _should_skip_item(self, item: Dict[str, Any]) -> bool:
        if not self.resume:
            return False
        existing = self.metadata_manager.get_item(self.get_item_id(item))
        if not existing:
            return False
        if self.metadata_only or self._existing_pdf_is_complete(existing):
            self.metadata_manager.add_item(existing)
            self.stats["skipped_items"] += 1
            self.logger.debug(f"이미 완료된 SearchThema 항목 건너뜀: {self.get_item_id(item)}")
            return True
        return False

    def _metadata_item_from_raw(self, item: Dict[str, Any]) -> Dict[str, Any]:
        prepared = dict(item)
        self._prepare_pdf_item(prepared)
        prepared.setdefault("id", self.get_item_id(prepared))
        prepared.setdefault("theme", self.theme)
        prepared.setdefault("title", prepared.get("stored_field_subject") or "")
        prepared.setdefault("category", prepared.get("stored_category_name") or "")
        prepared.setdefault("agency", prepared.get("stored_organ_nm") or "")
        prepared.setdefault("url", self._viewer_url_for_item(prepared, prepared.get("viewer_path", "")))
        prepared.setdefault("ocr", {"status": "pending", "ready_dir": "", "extracted_metadata": {}})
        if not isinstance(prepared.get("pdf"), dict):
            prepared["pdf"] = {}
        return prepared

    def _viewer_url_for_item(self, item: Dict[str, Any], viewer_path: str) -> str:
        search_thema_path = item.get("stored_field_url") or viewer_path
        if not search_thema_path:
            raise RuntimeError("stored_field_url이 없습니다.")
        return urljoin(self.viewer_base_url, str(search_thema_path).lstrip("/"))

    async def _download_item_pdf_once(self, context: Any, item: Dict[str, Any]) -> Dict[str, Any]:
        self._prepare_pdf_item(item)
        try:
            result = await self._download_pdf_via_http(item)
        except Exception as http_error:
            if context is None:
                raise RuntimeError(f"SearchThema HTTP PDF 다운로드 실패: {http_error}") from http_error
            self.logger.warning(f"SearchThema HTTP PDF 다운로드 실패, Playwright fallback 시도: {http_error}")
            result = await self._download_with_playwright_fallback(context, item)

        item["pdf"].update(result)
        item["status"] = "completed"
        self.stats["downloaded_pdfs"] += 1
        self.logger.info(f"SearchThema PDF 다운로드 완료: {result['path']}")
        return item

    async def _download_pdf_via_http(self, item: Dict[str, Any]) -> Dict[str, Any]:
        viewer_path = item.get("viewer_path", "")
        viewer_url = self._viewer_url_for_item(item, str(viewer_path))
        pdf_path = self._pdf_path_for_item(item)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        headers = self._browser_headers(viewer_url)
        timeout = aiohttp.ClientTimeout(total=max(self.request_timeout, 60))
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(viewer_url) as viewer_response:
                if viewer_response.status != 200:
                    raise RuntimeError(f"뷰어 요청 실패: HTTP {viewer_response.status}")
                viewer_html = await viewer_response.text()

            download_url, form_data = self._extract_download_request(viewer_html, item)
            return await self._download_pdf_stream(self._empty_cookie_context(), download_url, form_data, pdf_path)

    async def _download_with_playwright_fallback(self, context: Any, item: Dict[str, Any]) -> Dict[str, Any]:
        viewer_path = item.get("viewer_path", "")
        viewer_url = self._viewer_url_for_item(item, str(viewer_path))
        viewer_response = await context.request.get(viewer_url, timeout=self.timeout_ms)
        if viewer_response.status != 200:
            raise RuntimeError(f"Playwright 뷰어 요청 실패: HTTP {viewer_response.status}")
        viewer_html = await viewer_response.text()

        download_url, form_data = self._extract_download_request(viewer_html, item)
        pdf_path = self._pdf_path_for_item(item)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        return await self._download_pdf_stream(context, download_url, form_data, pdf_path)

    async def _response_json(self, response: Any) -> Dict[str, Any]:
        headers = getattr(response, "headers", None)
        if headers is not None and hasattr(response, "text"):
            content_type = str(headers.get("Content-Type", headers.get("content-type", ""))).lower()
            encoding = "euc-kr" if "euc-kr" in content_type else "utf-8" if "utf-8" in content_type else None
            try:
                text = await response.text(encoding=encoding) if encoding else await response.text()
            except TypeError:
                text = await response.text()
            return json.loads(text)
        return await response.json(content_type=None)

    def _pdf_path_for_item(self, item: Dict[str, Any]) -> Path:
        date_text = self._item_date(item)
        year = date_text[:4] if len(date_text) >= 4 else "unknown"
        date_key = date_text.replace("-", "") if len(date_text) == 10 else "unknown"
        return self.pdf_dir / year / date_key / f"{self._safe_filename(self.get_item_id(item))}.pdf"

    def _prepare_pdf_item(self, item: Dict[str, Any]) -> None:
        viewer_path = item.get("viewer_path") or item.get("stored_field_url") or ""
        query_values = parse_qs(urlparse(str(viewer_path)).query)

        item["viewer_path"] = str(viewer_path)
        item["toc_id"] = item.get("toc_id") or item.get("stored_toc_seq") or self._first_query_value(query_values, "tocId")
        item["content_id"] = item.get("content_id") or self._first_query_value(query_values, "contentId")
        item["date"] = item.get("date") or self._item_date(item)
        if not isinstance(item.get("pdf"), dict):
            item["pdf"] = {}

    def _item_date(self, item: Dict[str, Any]) -> str:
        date = str(item.get("date") or "")
        if date:
            return date
        year = str(item.get("stored_field_year") or "")
        month = str(item.get("stored_field_month") or "").zfill(2)
        day = str(item.get("stored_field_day") or "").zfill(2)
        if year and month and day:
            return f"{year}-{month}-{day}"
        return "unknown"

    @staticmethod
    def _first_query_value(query_values: Dict[str, List[str]], key: str) -> str:
        values = query_values.get(key) or []
        return values[0] if values else ""

    @staticmethod
    def _browser_headers(referer: str) -> Dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
        }

    @staticmethod
    def _empty_cookie_context():
        class _EmptyCookieContext:
            async def cookies(self, base_url: str) -> List[Dict[str, str]]:
                return []

        return _EmptyCookieContext()

    @staticmethod
    def _configured_years(search_config: Dict[str, Any]) -> List[str]:
        start_year = int(search_config.get("year_start", 1994))
        configured_end = search_config.get("year_end", "current")
        end_year = datetime.now().year if configured_end == "current" else int(configured_end)
        return [str(year) for year in range(start_year, end_year + 1)]

    @staticmethod
    def _configured_institutions(search_config: Dict[str, Any]) -> List[str | None]:
        return [None, *list(search_config.get("institutions") or [])]

    @staticmethod
    def _institution_state_value(institution: str | None) -> str:
        return institution or "ALL"

    @staticmethod
    def _institution_log_value(institution: str | None) -> str:
        return institution or "전체"

    def _use_search_thema_metadata_dir(self, metadata_directory: str) -> None:
        metadata_dir = self._search_thema_metadata_dir(metadata_directory)
        self.metadata_manager.metadata_dir = metadata_dir
        self.metadata_manager.items_dir = metadata_dir / "items"
        self.metadata_manager.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_manager.items_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_manager.items = {}
        self.metadata_manager.load_existing_metadata()

    @staticmethod
    def _search_thema_metadata_dir(metadata_directory: str) -> Path:
        path = Path(metadata_directory)
        if "searchThema" in path.parts:
            return path
        if path.name == "metadata":
            return path.parent / "searchThema" / "metadata"
        return path / "searchThema" / "metadata"

    @staticmethod
    def _search_thema_pdf_dir(pdf_directory: str) -> Path:
        path = Path(pdf_directory)
        if "searchThema" in path.parts:
            return path
        if path.name == "pdfs":
            return path.parent / "searchThema" / "pdfs"
        return path / "searchThema" / "pdfs"
