"""HTTP-based SearchThema crawler."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set
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
    from .metadata_schema import apply_item_schema
    from .pety_parser import parse_pety_list_page
    from .search_thema_parser import parse_search_thema_response
except ImportError:
    from base_crawler import BaseCrawler  # type: ignore[reportMissingImports]
    from config import get_config  # type: ignore[reportMissingImports]
    from crawl_state import CrawlState  # type: ignore[reportMissingImports]
    from metadata_manager import MetadataManager  # type: ignore[reportMissingImports]
    from metadata_schema import apply_item_schema  # type: ignore[reportMissingImports]
    from pety_parser import parse_pety_list_page  # type: ignore[reportMissingImports]
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
        use_browser: bool = True,
        preload_metadata: bool = True,
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
        self.use_browser = use_browser
        self.preload_metadata = preload_metadata
        self.browser_fallback_enabled = os.getenv("SEARCH_BROWSER_FALLBACK", "1").lower() not in {"0", "false", "no"}
        self._browser_fallback_sem = asyncio.Semaphore(
            max(1, int(os.getenv("SEARCH_BROWSER_FALLBACK_CONCURRENCY", "1")))
        )
        self.session_pool_size = max(0, int(os.getenv("SEARCH_SESSION_POOL_SIZE", "4")))
        self.session_pool_path = Path(
            os.getenv("SEARCH_SESSION_POOL_PATH", "artifacts/state/searchthema_session_pool.json")
        )
        self.direct_pdf_download = os.getenv("SEARCH_DIRECT_PDF_DOWNLOAD", "1").lower() not in {"0", "false", "no"}
        self._session_pool_lock = asyncio.Lock()
        self._limit_reached = False
        self.search_api_url = search_config.get("search_api_url", "https://gwanbo.go.kr/SearchRestApi.jsp")
        self.theme_info_url = search_config.get("theme_info_url", "https://gwanbo.go.kr/user/search/getThemeBaseInfo.do")
        self.viewer_base_url = search_config.get("viewer_base_url", "https://gwanbo.go.kr/")
        self.pety_list_url = search_config.get("pety_list_url", "https://open.gwanbo.go.kr/OpenApi/web/petyList")
        self.enable_pety_html_fallback = bool(search_config.get("enable_pety_html_fallback", False))
        self.search_index = str(search_config.get("index", "gwanbo"))
        self.list_size = int(os.getenv("SEARCH_LIST_SIZE", search_config.get("list_size", 10)))
        self.page_delay = float(os.getenv("SEARCH_PAGE_DELAY", "0.2"))
        self.institution_query_map = dict(search_config.get("institution_query_map", {}))
        self.years = [str(year) for year in years] if years is not None else self._configured_years(search_config)
        self.institutions = list(institutions) if institutions is not None else self._configured_institutions(search_config)

        self.timeout_ms = int(crawler_config.get("timeout", 30)) * 1000
        self.request_timeout = int(crawler_config.get("timeout", 30))
        self.retry_delay = float(crawler_config.get("retry_delay", 2))
        self.max_retries = int(crawler_config.get("max_retries", 3))
        self.chunk_size = int(download_config.get("chunk_size", 8192))
        self.pdf_dir = self._search_thema_pdf_dir(download_config.get("pdf_directory", "artifacts/pdfs"))
        self.metadata_manager = MetadataManager(
            self._search_thema_metadata_dir(download_config.get("metadata_directory", "artifacts/metadata")),
            load_existing=preload_metadata,
        )
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
            "failed_combinations": 0,
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
            if context is None and HAS_PLAYWRIGHT and self.use_browser:
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
                    if self._combination_completed_successfully(combination_stats):
                        self.state.mark_search_thema_completed(
                            str(year),
                            self._institution_state_value(institution),
                            self._state_mode(),
                            combination_stats,
                        )
                        self.stats["completed_combinations"] += 1
                    else:
                        self.stats["failed_combinations"] += 1
                        self.logger.warning(
                            "미완료 조합은 resume 대상에 유지합니다: "
                            f"{year} / {self._institution_state_value(institution)}"
                        )

    @staticmethod
    def _combination_completed_successfully(stats: Dict[str, Any]) -> bool:
        if stats.get("error"):
            return False
        return int(stats.get("failed_downloads") or 0) == 0

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
                if self.limit is not None and self.stats["total_items"] >= self.limit:
                    self._limit_reached = True
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
            if self.page_delay > 0:
                await self._sleep(self.page_delay)

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
        headers = self._search_api_headers()
        for attempt in range(1, self.max_retries + 1):
            try:
                response_json: Dict[str, Any] | None = None
                if context is not None and hasattr(context, "request"):
                    try:
                        await self._throttle_network()
                        response = await context.request.post(
                            self.search_api_url,
                            form=payload,
                            timeout=self.timeout_ms,
                        )
                        if response.status != 200:
                            raise RuntimeError(f"HTTP {response.status}: {self.search_api_url}")
                        response_json = await response.json()
                    except Exception as browser_error:
                        self.logger.warning(
                            f"Playwright 요청 실패, aiohttp 폴백 시도: {browser_error}"
                        )
                if response_json is None:
                    async with aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=True) as session:
                        await self._throttle_network()
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
                fallback_items = await self._fallback_items_from_error(exc, context, year, institution, page_number)
                if fallback_items is not None:
                    self.logger.warning("SearchThema JSON 파싱 실패, petyList 방식 폴백으로 계속 진행합니다.")
                    self.stats["visited_pages"] += 1
                    return fallback_items
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
            existing = self.metadata_manager.load_item(item)
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
        prepared.setdefault("source_url", self.search_api_url)
        prepared.setdefault("ocr", {"status": "pending", "ready_dir": "", "extracted_metadata": {}})
        if not isinstance(prepared.get("pdf"), dict):
            prepared["pdf"] = {}
        apply_item_schema(
            prepared,
            source_detail=self.theme,
            source_endpoint=str(prepared.get("source") or "SearchRestApi"),
        )
        return prepared

    def _viewer_url_for_item(self, item: Dict[str, Any], viewer_path: str) -> str:
        search_thema_path = item.get("stored_field_url") or viewer_path
        if not search_thema_path:
            raise RuntimeError("stored_field_url이 없습니다.")
        return urljoin(self.viewer_base_url, str(search_thema_path).lstrip("/"))

    async def _download_item_pdf_once(self, context: Any, item: Dict[str, Any]) -> Dict[str, Any]:
        self._prepare_pdf_item(item)
        session_entry: Dict[str, Any] | None = None
        try:
            if context is None:
                if self._direct_pdf_download_request(item) is None:
                    session_entry = await self._next_session_entry(create_missing=True)
                result = await self._download_pdf_via_http(item, session_entry)
            else:
                result = await self._download_pdf_via_http(item)
        except Exception as http_error:
            if context is None and self._should_retry_with_session_pool(http_error):
                recovered_with_session = False
                if session_entry is None:
                    session_entry = await self._next_session_entry(create_missing=True)
                    if session_entry is not None:
                        try:
                            result = await self._download_pdf_via_http(item, session_entry)
                            recovered_with_session = True
                        except Exception as session_http_error:
                            http_error = session_http_error
                if not recovered_with_session:
                    failed_session_id = self._session_entry_id(session_entry)
                    try:
                        result = await self._download_with_session_pool_round_robin(
                            item,
                            http_error,
                            exclude_session_ids={failed_session_id} if failed_session_id else None,
                        )
                    except Exception as pool_error:
                        if not self._should_refresh_session_with_browser(pool_error):
                            raise RuntimeError(f"SearchThema HTTP PDF 다운로드 실패: {pool_error}") from pool_error
                        self.logger.warning(
                            "SearchThema 세션 ID 라운드로빈 실패, 브라우저 세션 갱신 fallback 시도: "
                            f"{pool_error}"
                        )
                        result = await self._download_with_fresh_browser_session(
                            item,
                            pool_error,
                            failed_session_id=failed_session_id,
                        )
            elif context is None and self._should_refresh_session_with_browser(http_error):
                self.logger.warning(f"SearchThema HTTP PDF 다운로드 실패, 브라우저 세션 갱신 fallback 시도: {http_error}")
                result = await self._download_with_fresh_browser_session(item, http_error)
            elif context is None:
                raise RuntimeError(f"SearchThema HTTP PDF 다운로드 실패: {http_error}") from http_error
            else:
                self.logger.warning(f"SearchThema HTTP PDF 다운로드 실패, Playwright fallback 시도: {http_error}")
                result = await self._download_with_playwright_fallback(context, item)

        pdf = item.setdefault("pdf", {})
        pdf.pop("error", None)
        pdf.pop("failed_at", None)
        pdf.update(result)
        if result.get("path"):
            self._annotate_ocr_strategy(item, Path(str(result["path"])))
        item["status"] = "completed"
        self.stats["downloaded_pdfs"] += 1
        self.logger.info(f"SearchThema PDF 다운로드 완료: {result['path']}")
        return item

    async def _download_pdf_via_http(
        self,
        item: Dict[str, Any],
        session_entry: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        viewer_path = item.get("viewer_path", "")
        viewer_url = self._viewer_url_for_item(item, str(viewer_path))
        pdf_path = self._pdf_path_for_item(item)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        cookies = self._session_entry_cookies(session_entry)
        direct_request = self._direct_pdf_download_request(item)
        if direct_request is not None:
            return await self._download_pdf_stream_with_issue_fallback(
                self._cookie_context(cookies),
                item,
                pdf_path,
                direct_request,
                "content_pdf_direct",
            )

        headers = self._browser_headers(viewer_url)
        cookie_header = self._cookie_header(cookies)
        if cookie_header:
            headers["Cookie"] = cookie_header
        timeout = aiohttp.ClientTimeout(total=max(self.request_timeout, 60))
        async with aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=True) as session:
            await self._throttle_network()
            async with session.get(viewer_url) as viewer_response:
                if viewer_response.status != 200:
                    raise RuntimeError(f"뷰어 요청 실패: HTTP {viewer_response.status}")
                viewer_html = await viewer_response.text()

            request = self._extract_download_request(viewer_html, item)
            return await self._download_pdf_stream_with_issue_fallback(
                self._cookie_context(cookies),
                item,
                pdf_path,
                request,
                self._download_method_name(request),
            )

    async def _download_with_playwright_fallback(self, context: Any, item: Dict[str, Any]) -> Dict[str, Any]:
        viewer_path = item.get("viewer_path", "")
        viewer_url = self._viewer_url_for_item(item, str(viewer_path))
        direct_request = self._direct_pdf_download_request(item)
        if direct_request is not None:
            pdf_path = self._pdf_path_for_item(item)
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            return await self._download_pdf_stream_with_issue_fallback(
                context,
                item,
                pdf_path,
                direct_request,
                "content_pdf_direct",
            )

        await self._throttle_network()
        viewer_response = await context.request.get(viewer_url, timeout=self.timeout_ms)
        if viewer_response.status != 200:
            raise RuntimeError(f"Playwright 뷰어 요청 실패: HTTP {viewer_response.status}")
        viewer_html = await viewer_response.text()

        request = self._extract_download_request(viewer_html, item)
        pdf_path = self._pdf_path_for_item(item)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        return await self._download_pdf_stream_with_issue_fallback(
            context,
            item,
            pdf_path,
            request,
            self._download_method_name(request),
        )

    def _direct_pdf_download_request(self, item: Dict[str, Any]) -> tuple[str, Dict[str, str]] | None:
        if not self.direct_pdf_download:
            return None
        toc_id = str(item.get("toc_id") or item.get("stored_toc_seq") or "").strip()
        if not toc_id:
            return None
        return urljoin(self.viewer_base_url, "user/common/ofcttCntntDownload.do"), {"cntnt_seq_no": toc_id}

    def _issue_pdf_download_request(self, item: Dict[str, Any]) -> tuple[str, Dict[str, str]] | None:
        content_id = str(item.get("content_id") or "").strip()
        if not content_id:
            viewer_path = str(item.get("viewer_path") or item.get("stored_field_url") or "")
            content_id = self._first_query_value(parse_qs(urlparse(viewer_path).query), "contentId")
        if not content_id:
            return None
        return urljoin(self.viewer_base_url, "user/common/ofcttDownload.do"), {
            "downType": "1",
            "ofctt_seq_no": content_id,
        }

    def _download_method_name(self, request: tuple[str, Dict[str, str]]) -> str:
        _url, form_data = request
        if form_data.get("ofctt_seq_no"):
            return "issue_pdf"
        return "content_pdf"

    async def _download_pdf_stream_with_issue_fallback(
        self,
        context: Any,
        item: Dict[str, Any],
        pdf_path: Path,
        primary_request: tuple[str, Dict[str, str]],
        primary_method: str,
    ) -> Dict[str, Any]:
        requests: list[tuple[str, tuple[str, Dict[str, str]]]] = [(primary_method, primary_request)]
        issue_request = self._issue_pdf_download_request(item)
        if issue_request is not None and issue_request != primary_request:
            requests.append(("issue_pdf_fallback", issue_request))

        last_error: Exception | None = None
        for index, (method, (download_url, form_data)) in enumerate(requests):
            target_path = self._issue_pdf_path_for_item(item) if method.startswith("issue_pdf") else pdf_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                result = await self._download_pdf_stream(context, download_url, form_data, target_path)
                result["method"] = method
                result["scope"] = "issue" if method.startswith("issue_pdf") else "content"
                if last_error is not None:
                    result["fallback_from"] = primary_method
                    result["fallback_error"] = str(last_error)
                return result
            except Exception as exc:
                last_error = exc
                if index >= len(requests) - 1:
                    raise
                self.logger.warning(
                    "SearchThema 본문 PDF 다운로드 실패, 전체 관보 PDF fallback 시도 "
                    f"({self.get_item_id(item)}): {exc}"
                )

        raise RuntimeError("PDF 다운로드 요청 후보가 없습니다.")

    def _should_refresh_session_with_browser(self, error: Exception) -> bool:
        if not self.browser_fallback_enabled or not HAS_PLAYWRIGHT or async_playwright is None:
            return False
        return self._is_session_retry_candidate(error)

    def _should_retry_with_session_pool(self, error: Exception) -> bool:
        return self.session_pool_size > 0 and self.browser_fallback_enabled and self._is_session_retry_candidate(error)

    @staticmethod
    def _is_session_retry_candidate(error: Exception) -> bool:
        text = str(error)
        markers = (
            "HTTP 401",
            "HTTP 403",
            "HTTP 500",
            "HTTP 502",
            "HTTP 503",
            "Connection reset by peer",
            "Server disconnected",
            "socket hang up",
            "PDF 다운로드 엔드포인트를 찾을 수 없습니다",
            "다운로드 결과가 PDF가 아닙니다",
            "다운로드 결과가 완전한 PDF가 아닙니다",
            "SearchThema 세션 ID 라운드로빈 재시도 대상이 없습니다",
            "SearchThema 세션 ID 라운드로빈 fallback 실패",
        )
        return any(marker in text for marker in markers)

    async def _download_with_session_pool_round_robin(
        self,
        item: Dict[str, Any],
        error: Exception,
        exclude_session_ids: Set[str] | None = None,
    ) -> Dict[str, Any]:
        sessions = await self._ensure_session_pool()
        if not sessions:
            raise RuntimeError(f"사용 가능한 SearchThema 세션 ID가 없습니다: {error}") from error

        exclude_session_ids = exclude_session_ids or set()
        last_error: Exception = error
        tried_session_ids: Set[str] = set()
        attempts = 0

        for _ in range(len(sessions)):
            session_entry = await self._next_session_entry(create_missing=False)
            session_id = self._session_entry_id(session_entry)
            if not session_entry or not session_id or session_id in tried_session_ids:
                continue
            tried_session_ids.add(session_id)
            if session_id in exclude_session_ids:
                continue

            attempts += 1
            self.logger.warning(
                "SearchThema 세션 ID 라운드로빈 PDF 재시도 "
                f"{attempts}/{len(sessions)}: {self._mask_session_id(session_id)}"
            )
            try:
                return await self._download_pdf_via_http(item, session_entry)
            except Exception as exc:
                last_error = exc
                self.logger.warning(
                    "SearchThema 세션 ID 라운드로빈 재시도 실패 "
                    f"{self._mask_session_id(session_id)}: {exc}"
                )

        if attempts == 0:
            raise RuntimeError("SearchThema 세션 ID 라운드로빈 재시도 대상이 없습니다.") from error
        raise RuntimeError(f"SearchThema 세션 ID 라운드로빈 fallback 실패({attempts}회): {last_error}") from last_error

    async def _download_with_fresh_browser_session(
        self,
        item: Dict[str, Any],
        error: Exception,
        failed_session_id: str | None = None,
    ) -> Dict[str, Any]:
        if async_playwright is None:
            raise RuntimeError(f"Playwright fallback 불가: {error}") from error

        async with self._browser_fallback_sem:
            self.logger.info("headless Chrome으로 SearchThema 세션/JSESSIONID 갱신")
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=self.headless)
                browser_context = await browser.new_context(
                    ignore_https_errors=True,
                    extra_http_headers=self._browser_headers(self.viewer_base_url),
                )
                try:
                    await self._prime_browser_session(browser_context)
                    try:
                        session_entry = await self._session_entry_from_browser_context(browser_context)
                    except Exception as session_error:
                        self.logger.warning(f"갱신된 브라우저 세션에서 JSESSIONID 추출 실패: {session_error}")
                        if not self.use_browser:
                            raise RuntimeError(
                                f"HTTP-only 모드에서 JSESSIONID 갱신 실패: {session_error}"
                            ) from session_error
                        return await self._download_with_playwright_fallback(browser_context, item)

                    await self._replace_session_entry(session_entry, failed_session_id=failed_session_id)
                    try:
                        return await self._download_pdf_via_http(item, session_entry)
                    except Exception as refreshed_http_error:
                        if not self.use_browser:
                            raise RuntimeError(
                                "HTTP-only 모드 세션 갱신 후 HTTP 재시도 실패: "
                                f"{refreshed_http_error}"
                            ) from refreshed_http_error
                        self.logger.warning(
                            "갱신 세션 ID HTTP 재시도 실패, Playwright context fallback 시도: "
                            f"{refreshed_http_error}"
                        )
                        return await self._download_with_playwright_fallback(browser_context, item)
                finally:
                    await browser_context.close()
                    await browser.close()

    async def _ensure_session_pool(self) -> List[Dict[str, Any]]:
        if self.session_pool_size <= 0:
            return []

        sessions = await self._read_session_pool_entries()
        if len(sessions) >= self.session_pool_size:
            return sessions
        if not self.browser_fallback_enabled or not HAS_PLAYWRIGHT or async_playwright is None:
            return sessions

        async with self._session_pool_lock:
            create_lock = await self._acquire_session_pool_create_lock()
            try:
                while True:
                    sessions = await self._read_session_pool_entries()
                    if len(sessions) >= self.session_pool_size:
                        return sessions
                    try:
                        session_entry = await self._create_browser_session_entry()
                    except Exception as exc:
                        self.logger.warning(f"SearchThema 세션 ID 풀 생성 실패: {exc}")
                        return sessions
                    added = await asyncio.to_thread(self._append_session_entry_if_room_sync, session_entry)
                    if not added:
                        return await self._read_session_pool_entries()
            finally:
                await asyncio.to_thread(fcntl.flock, create_lock.fileno(), fcntl.LOCK_UN)
                create_lock.close()

    async def _next_session_entry(self, create_missing: bool) -> Dict[str, Any] | None:
        if self.session_pool_size <= 0:
            return None
        if create_missing:
            await self._ensure_session_pool()
        async with self._session_pool_lock:
            return await asyncio.to_thread(self._next_session_entry_sync)

    async def _create_browser_session_entry(self) -> Dict[str, Any]:
        if async_playwright is None:
            raise RuntimeError("Playwright fallback 불가")

        async with self._browser_fallback_sem:
            self.logger.info("headless Chrome으로 SearchThema 세션/JSESSIONID 생성")
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=self.headless)
                browser_context = await browser.new_context(
                    ignore_https_errors=True,
                    extra_http_headers=self._browser_headers(self.viewer_base_url),
                )
                try:
                    await self._prime_browser_session(browser_context)
                    return await self._session_entry_from_browser_context(browser_context)
                finally:
                    await browser_context.close()
                    await browser.close()

    async def _session_entry_from_browser_context(self, context: BrowserContext) -> Dict[str, Any]:
        cookies = self._normalize_cookies(await context.cookies(self.viewer_base_url))
        session_id = self._session_id_from_cookies(cookies)
        if not session_id:
            raise RuntimeError("JSESSIONID 쿠키를 찾을 수 없습니다.")
        return {
            "id": session_id,
            "cookies": cookies,
            "created_at": datetime.now().isoformat(),
        }

    async def _read_session_pool_entries(self) -> List[Dict[str, Any]]:
        pool = await asyncio.to_thread(self._read_session_pool_locked_sync)
        return list(pool.get("sessions") or [])

    def _read_session_pool_locked_sync(self) -> Dict[str, Any]:
        def _op() -> Dict[str, Any]:
            return self._read_session_pool_unlocked()

        return self._with_session_pool_file_lock_sync(_op)

    def _next_session_entry_sync(self) -> Dict[str, Any] | None:
        def _op() -> Dict[str, Any] | None:
            pool = self._read_session_pool_unlocked()
            sessions = list(pool.get("sessions") or [])
            if not sessions:
                return None
            cursor = int(pool.get("cursor") or 0) % len(sessions)
            session_entry = sessions[cursor]
            pool["cursor"] = (cursor + 1) % len(sessions)
            pool["sessions"] = sessions
            self._write_session_pool_unlocked(pool)
            return session_entry

        return self._with_session_pool_file_lock_sync(_op)

    def _append_session_entry_if_room_sync(self, session_entry: Dict[str, Any]) -> bool:
        def _op() -> bool:
            pool = self._read_session_pool_unlocked()
            sessions = list(pool.get("sessions") or [])
            session_id = self._session_entry_id(session_entry)
            for index, current in enumerate(sessions):
                if self._session_entry_id(current) == session_id:
                    sessions[index] = session_entry
                    pool["sessions"] = sessions[: self.session_pool_size]
                    self._write_session_pool_unlocked(pool)
                    return True
            if len(sessions) >= self.session_pool_size:
                return False
            sessions.append(session_entry)
            pool["sessions"] = sessions
            self._write_session_pool_unlocked(pool)
            return True

        return self._with_session_pool_file_lock_sync(_op)

    async def _replace_session_entry(
        self,
        session_entry: Dict[str, Any],
        failed_session_id: str | None = None,
    ) -> None:
        async with self._session_pool_lock:
            await asyncio.to_thread(self._replace_session_entry_sync, session_entry, failed_session_id)

    def _replace_session_entry_sync(
        self,
        session_entry: Dict[str, Any],
        failed_session_id: str | None = None,
    ) -> None:
        def _op() -> None:
            pool = self._read_session_pool_unlocked()
            sessions = list(pool.get("sessions") or [])
            replacement_index: int | None = None
            if failed_session_id:
                for index, current in enumerate(sessions):
                    if self._session_entry_id(current) == failed_session_id:
                        replacement_index = index
                        break

            if replacement_index is None:
                session_id = self._session_entry_id(session_entry)
                for index, current in enumerate(sessions):
                    if self._session_entry_id(current) == session_id:
                        replacement_index = index
                        break

            if replacement_index is not None:
                sessions[replacement_index] = session_entry
            elif len(sessions) < self.session_pool_size:
                sessions.append(session_entry)
            elif sessions:
                replacement_index = int(pool.get("cursor") or 0) % len(sessions)
                sessions[replacement_index] = session_entry
                pool["cursor"] = (replacement_index + 1) % len(sessions)
            else:
                sessions.append(session_entry)

            pool["sessions"] = sessions[: self.session_pool_size]
            self._write_session_pool_unlocked(pool)

        self._with_session_pool_file_lock_sync(_op)

    def _read_session_pool_unlocked(self) -> Dict[str, Any]:
        if not self.session_pool_path.exists():
            return {"cursor": 0, "sessions": []}
        try:
            raw = json.loads(self.session_pool_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"cursor": 0, "sessions": []}
        sessions = [
            session
            for session in raw.get("sessions", [])
            if isinstance(session, dict) and self._session_entry_id(session) and self._session_entry_cookies(session)
        ]
        return {
            "cursor": int(raw.get("cursor") or 0),
            "sessions": sessions[: self.session_pool_size],
        }

    def _write_session_pool_unlocked(self, pool: Dict[str, Any]) -> None:
        self.session_pool_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.session_pool_path.with_name(f"{self.session_pool_path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(
            json.dumps(pool, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.session_pool_path)

    def _with_session_pool_file_lock_sync(self, callback: Any) -> Any:
        self.session_pool_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.session_pool_path.with_suffix(self.session_pool_path.suffix + ".lock")
        with open(lock_path, "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                return callback()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    async def _acquire_session_pool_create_lock(self) -> Any:
        self.session_pool_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.session_pool_path.with_suffix(self.session_pool_path.suffix + ".create.lock")
        lock_file = open(lock_path, "w", encoding="utf-8")
        try:
            await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_EX)
        except Exception:
            lock_file.close()
            raise
        return lock_file

    @staticmethod
    def _normalize_cookies(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if not name or not value:
                continue
            normalized.append(
                {
                    "name": name,
                    "value": value,
                    "domain": str(cookie.get("domain") or "gwanbo.go.kr"),
                    "path": str(cookie.get("path") or "/"),
                }
            )
        return normalized

    @staticmethod
    def _session_id_from_cookies(cookies: List[Dict[str, Any]]) -> str:
        for cookie in cookies:
            if str(cookie.get("name") or "").upper() == "JSESSIONID":
                return str(cookie.get("value") or "")
        return ""

    @staticmethod
    def _session_entry_id(session_entry: Dict[str, Any] | None) -> str:
        if not session_entry:
            return ""
        return str(session_entry.get("id") or SearchThemaCrawler._session_id_from_cookies(
            SearchThemaCrawler._session_entry_cookies(session_entry)
        ))

    @staticmethod
    def _session_entry_cookies(session_entry: Dict[str, Any] | None) -> List[Dict[str, Any]]:
        if not session_entry:
            return []
        cookies = session_entry.get("cookies") or []
        return [cookie for cookie in cookies if isinstance(cookie, dict)]

    @staticmethod
    def _cookie_header(cookies: List[Dict[str, Any]]) -> str:
        return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies if cookie.get("name"))

    @staticmethod
    def _mask_session_id(session_id: str) -> str:
        if len(session_id) <= 10:
            return "***"
        return f"{session_id[:6]}...{session_id[-4:]}"

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

    async def _fallback_items_from_error(
        self,
        error: Exception,
        context: Any,
        year: int | str,
        institution: str | None,
        page_number: int,
    ) -> List[Dict[str, Any]] | None:
        if not isinstance(error, (json.JSONDecodeError, ValueError, TypeError)):
            return None
        if not self.enable_pety_html_fallback:
            return None
        return await self._fetch_items_via_pety_style(context, year, institution, page_number)

    async def _fetch_items_via_pety_style(
        self,
        context: Any,
        year: int | str,
        institution: str | None,
        page_number: int,
    ) -> List[Dict[str, Any]]:
        payload = {
            "themaSe": "02",
            "searchType": "4",
            "rowPerPage": str(self.list_size),
            "pageNum": str(page_number),
            "searchCondition": str(year),
        }
        if institution:
            payload["searchKeyword"] = institution

        if context is not None and hasattr(context, "request"):
            await self._throttle_network()
            response = await context.request.post(self.pety_list_url.replace("petyList", "petyListAjax"), form=payload, timeout=self.timeout_ms)
            if response.status != 200:
                return []
            response_html = await response.text()
        else:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            headers = self._browser_headers(self.pety_list_url)
            headers.update(
                {
                    "Accept": "text/html, */*",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": self.viewer_base_url.rstrip("/"),
                }
            )
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=True) as session:
                await self._throttle_network()
                async with session.post(self.pety_list_url.replace("petyList", "petyListAjax"), data=payload) as response:
                    if response.status != 200:
                        return []
                    response_html = await response.text()

        parsed = parse_pety_list_page(response_html, self.pety_list_url)
        return parsed.items

    def _pdf_path_for_item(self, item: Dict[str, Any]) -> Path:
        date_text = self._item_date(item)
        year = date_text[:4] if len(date_text) >= 4 else "unknown"
        date_key = date_text.replace("-", "") if len(date_text) == 10 else "unknown"
        return self.pdf_dir / year / date_key / f"{self._safe_filename(self.get_item_id(item))}.pdf"

    def _issue_pdf_path_for_item(self, item: Dict[str, Any]) -> Path:
        date_text = self._item_date(item)
        year = date_text[:4] if len(date_text) >= 4 else "unknown"
        date_key = date_text.replace("-", "") if len(date_text) == 10 else "unknown"
        return self.pdf_dir.parent / "issue_pdfs" / year / date_key / f"{self._safe_filename(self.get_item_id(item))}.pdf"

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
        return BaseCrawler._browser_headers(referer)

    def _search_api_headers(self) -> Dict[str, str]:
        headers = self._browser_headers(self.theme_info_url)
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": self.viewer_base_url.rstrip("/"),
            }
        )
        return headers

    @staticmethod
    def _empty_cookie_context():
        return SearchThemaCrawler._cookie_context([])

    @staticmethod
    def _cookie_context(cookies: List[Dict[str, Any]]):
        class _CookieContext:
            async def cookies(self, base_url: str) -> List[Dict[str, Any]]:
                return cookies

        return _CookieContext()

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
