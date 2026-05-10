"""Shared crawler helpers and abstract base class."""

from __future__ import annotations

import hashlib
import asyncio
import re
import socket
from abc import ABC, abstractmethod
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin

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


class BaseCrawler(ABC):
    """Common crawler helpers shared across themed crawler implementations."""

    viewer_base_url: str
    pdf_dir: Path
    logger: Logger
    stats: Dict[str, Any]
    max_retries: int
    retry_delay: float
    timeout_ms: int
    request_timeout: int
    chunk_size: int
    _connectivity_cache: Dict[str, bool]

    @abstractmethod
    async def fetch_items(self, page_number: int) -> List[Dict[str, Any]]:
        """Fetch a page of source items."""

    @abstractmethod
    def get_item_id(self, item: Dict[str, Any]) -> str:
        """Return the stable identifier for an item."""

    @abstractmethod
    def _state_mode(self) -> str:
        """Return the crawl state mode name."""

    async def _download_item_pdf(self, context: Any, item: Dict[str, Any]) -> Dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                result = await self._download_item_pdf_once(context, item)
                if attempt > 1:
                    self.logger.info(
                        f"PDF 다운로드 재시도 성공 ({self.get_item_id(item)}): {attempt}/{self.max_retries}"
                    )
                return result
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    self.logger.warning(
                        f"PDF 다운로드 재시도 {attempt}/{self.max_retries} ({self.get_item_id(item)}): {e}"
                    )
                    await self._sleep(self.retry_delay * attempt)

        item["pdf"]["status"] = "failed"
        item["pdf"]["error"] = str(last_error)
        item["status"] = "download_failed"
        self.stats["failed_downloads"] += 1
        self.logger.warning(f"PDF 다운로드 실패 ({self.get_item_id(item)}): {last_error}")
        return item

    async def _download_item_pdf_once(self, context: Any, item: Dict[str, Any]) -> Dict[str, Any]:
        viewer_path = item.get("viewer_path", "")
        if not viewer_path:
            raise RuntimeError("viewer_path가 없습니다.")

        viewer_url = self._viewer_url_for_item(item, viewer_path)
        prefers_browser = not await self._is_host_reachable("gwanbo.go.kr", 443)
        if prefers_browser:
            self.logger.info("네트워크 진단 결과 gwanbo.go.kr 직연결 불가, 브라우저 우선 경로 사용")
            viewer_html = await self._fetch_viewer_html_via_browser_page(context, viewer_url)
        else:
            try:
                viewer_response = await context.request.get(viewer_url, timeout=self.timeout_ms)
                if viewer_response.status != 200:
                    raise RuntimeError(f"뷰어 요청 실패: HTTP {viewer_response.status}")
                viewer_html = await viewer_response.text()
            except Exception as request_error:
                if "ENETUNREACH" not in str(request_error):
                    raise
                self.logger.warning("PDF 뷰어 요청 ENETUNREACH 감지, 브라우저 페이지 fallback 시도")
                viewer_html = await self._fetch_viewer_html_via_browser_page(context, viewer_url)

        download_url, form_data = self._extract_download_request(viewer_html, item)
        pdf_path = self._pdf_path_for_item(item)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        result = await self._download_pdf_stream(context, download_url, form_data, pdf_path)
        item["pdf"].update(result)
        item["status"] = "completed"
        self.stats["downloaded_pdfs"] += 1
        self.logger.info(f"PDF 다운로드 완료: {pdf_path}")
        return item

    async def _fetch_viewer_html_via_browser_page(self, context: Any, viewer_url: str) -> str:
        page = await context.new_page()
        try:
            response = await page.goto(viewer_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if response is not None and response.status >= 400:
                raise RuntimeError(f"뷰어 요청 실패(브라우저): HTTP {response.status}")
            return await page.content()
        finally:
            await page.close()

    async def _fetch_viewer_html_via_http(self, context: Any, viewer_url: str) -> str:
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

        timeout = aiohttp.ClientTimeout(total=max(self.request_timeout, 60))
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(viewer_url) as response:
                if response.status != 200:
                    raise RuntimeError(f"뷰어 요청 실패(aiohttp): HTTP {response.status}")
                return await response.text()

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
        context: Any,
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

        prefers_browser = not await self._is_host_reachable("gwanbo.go.kr", 443)
        if prefers_browser:
            self.logger.info("네트워크 진단 결과 gwanbo.go.kr 직연결 불가, 브라우저 fetch 우선 경로 사용")
            body = await self._download_pdf_body_via_browser_page(context, download_url, form_data)
            with open(temp_path, "wb") as f:
                f.write(body)
            sha256.update(body)
            size = len(body)
        else:
            try:
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
            except Exception as http_error:
                if "Network is unreachable" not in str(http_error) and "ENETUNREACH" not in str(http_error):
                    raise
                self.logger.warning("PDF aiohttp 다운로드 ENETUNREACH 감지, APIRequestContext fallback 시도")
                response = await context.request.post(download_url, form=form_data, timeout=self.timeout_ms)
                if response.status != 200:
                    raise RuntimeError(f"PDF 요청 실패(APIRequestContext): HTTP {response.status}")
                body = await response.body()
                with open(temp_path, "wb") as f:
                    f.write(body)
                sha256.update(body)
                size = len(body)

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

    async def _download_pdf_body_via_browser_page(
        self,
        context: Any,
        download_url: str,
        form_data: Dict[str, str],
    ) -> bytes:
        page = await context.new_page()
        try:
            await page.goto(self.viewer_base_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            result = await page.evaluate(
                """async ({ url, formData }) => {
                    const body = new URLSearchParams(formData).toString();
                    const response = await fetch(url, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
                        },
                        body,
                        credentials: "include"
                    });
                    if (!response.ok) {
                        throw new Error(`HTTP ${response.status}`);
                    }
                    const buffer = await response.arrayBuffer();
                    const bytes = new Uint8Array(buffer);
                    let binary = "";
                    const chunkSize = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunkSize) {
                        binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
                    }
                    return btoa(binary);
                }""",
                {"url": download_url, "formData": form_data},
            )
            if not isinstance(result, str):
                raise RuntimeError("브라우저 PDF fetch 결과(base64)가 문자열이 아닙니다.")
            import base64

            return base64.b64decode(result)
        finally:
            await page.close()

    async def _is_host_reachable(self, host: str, port: int) -> bool:
        if not hasattr(self, "_connectivity_cache"):
            self._connectivity_cache = {}
        cache_key = f"{host}:{port}"
        if cache_key in self._connectivity_cache:
            return self._connectivity_cache[cache_key]
        try:
            conn = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(conn, timeout=3)
            writer.close()
            await writer.wait_closed()
            self._connectivity_cache[cache_key] = True
        except (OSError, socket.gaierror, TimeoutError):
            self._connectivity_cache[cache_key] = False
        return self._connectivity_cache[cache_key]

    def _pdf_path_for_item(self, item: Dict[str, Any]) -> Path:
        date_text = item.get("date", "unknown")
        year = date_text[:4] if re.match(r"^\d{4}", date_text) else "unknown"
        date_key = date_text.replace("-", "") if re.match(r"^\d{4}-\d{2}-\d{2}$", date_text) else "unknown"
        return self.pdf_dir / year / date_key / f"{self._safe_filename(self.get_item_id(item))}.pdf"

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
        start_time = self.stats.get("start_time")
        end_time = self.stats.get("end_time")
        duration = (end_time - start_time).total_seconds() if end_time and start_time else 0
        result = dict(self.stats)
        result["duration_seconds"] = duration
        result["start_time"] = start_time.isoformat() if start_time else None
        result["end_time"] = end_time.isoformat() if end_time else None
        return result

    @staticmethod
    def _safe_filename(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", str(value)).strip("._") or "unknown"

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)
