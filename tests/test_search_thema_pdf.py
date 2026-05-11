import asyncio
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock, patch

import pytest


if "aiohttp" not in sys.modules:
    aiohttp_stub = ModuleType("aiohttp")
    setattr(aiohttp_stub, "ClientTimeout", Mock(name="ClientTimeout"))
    setattr(aiohttp_stub, "ClientSession", Mock(name="ClientSession"))
    sys.modules["aiohttp"] = aiohttp_stub

from crawler_search_thema import SearchThemaCrawler  # type: ignore[reportMissingImports]


@pytest.fixture
def crawler(tmp_data_dir: Path, mock_config: Mock, monkeypatch: pytest.MonkeyPatch) -> SearchThemaCrawler:
    monkeypatch.setenv("SEARCH_SESSION_POOL_SIZE", "0")
    monkeypatch.setenv("SEARCH_SESSION_POOL_PATH", str(tmp_data_dir / "state" / "searchthema_session_pool.json"))
    mock_config.config["crawler"] = {
        "timeout": 30,
        "retry_delay": 0,
        "max_retries": 1,
        "themes": {
            "searchThema": {
                "search_api_url": "https://gwanbo.go.kr/SearchRestApi.jsp",
                "theme_info_url": "https://gwanbo.go.kr/user/search/getThemeBaseInfo.do",
                "viewer_base_url": "https://gwanbo.go.kr/",
                "index": "gwanbo",
                "list_size": 10,
                "institution_query_map": {},
            }
        },
    }
    mock_config.config["download"] = {
        "pdf_directory": str(tmp_data_dir / "pdfs"),
        "chunk_size": 8192,
    }
    mock_config.get_crawler_config.return_value = mock_config.config["crawler"]
    mock_config.get_download_config.return_value = mock_config.config["download"]
    mock_config.get_search_thema_config.return_value = mock_config.config["crawler"]["themes"]["searchThema"]

    with (
        patch("crawler_search_thema.get_config", return_value=mock_config),
        patch("crawler_search_thema.setup_logger", return_value=Mock(name="logger")),
    ):
        return SearchThemaCrawler(metadata_only=False, preload_metadata=False)


@pytest.fixture
def search_thema_item() -> dict:
    return {
        "stored_toc_seq": "I0000000000000001734498102442000",
        "stored_field_url": (
            "/ezpdf/customLayout.jsp?contentId=I0000000000000001735535299326000"
            "&tocId=I0000000000000001734498102442000&isTocOrder=N"
        ),
        "stored_field_year": "2024",
        "stored_field_month": "12",
        "stored_field_day": "31",
    }


def test_viewer_url_from_stored_field_url(crawler: SearchThemaCrawler, search_thema_item: dict) -> None:
    assert crawler._viewer_url_for_item(search_thema_item, "") == (
        "https://gwanbo.go.kr/ezpdf/customLayout.jsp?contentId=I0000000000000001735535299326000"
        "&tocId=I0000000000000001734498102442000&isTocOrder=N"
    )


def test_download_extraction_from_viewer_html(crawler: SearchThemaCrawler, search_thema_item: dict) -> None:
    crawler._prepare_pdf_item(search_thema_item)
    viewer_html = '<form action="/user/common/ofcttCntntDownload.do"></form>'

    download_url, form_data = crawler._extract_download_request(viewer_html, search_thema_item)

    assert download_url == "https://gwanbo.go.kr/user/common/ofcttCntntDownload.do"
    assert form_data == {"cntnt_seq_no": "I0000000000000001734498102442000"}


def test_direct_pdf_request_from_toc_id(crawler: SearchThemaCrawler, search_thema_item: dict) -> None:
    crawler._prepare_pdf_item(search_thema_item)

    direct_request = crawler._direct_pdf_download_request(search_thema_item)

    assert direct_request == (
        "https://gwanbo.go.kr/user/common/ofcttCntntDownload.do",
        {"cntnt_seq_no": "I0000000000000001734498102442000"},
    )


def test_http_download_uses_direct_endpoint_without_viewer(
    crawler: SearchThemaCrawler,
    search_thema_item: dict,
) -> None:
    crawler._prepare_pdf_item(search_thema_item)
    direct_result = {
        "status": "completed",
        "path": "direct.pdf",
        "size_bytes": 10,
        "sha256": "abc",
        "downloaded_at": "2026-05-11T00:00:00",
    }

    with (
        patch("crawler_search_thema.aiohttp.ClientSession") as session,
        patch.object(crawler, "_download_pdf_stream", new=AsyncMock(return_value=direct_result)) as stream,
    ):
        result = asyncio.run(crawler._download_pdf_via_http(search_thema_item))

    session.assert_not_called()
    stream.assert_awaited_once()
    assert stream.await_args.args[1] == "https://gwanbo.go.kr/user/common/ofcttCntntDownload.do"
    assert stream.await_args.args[2] == {"cntnt_seq_no": "I0000000000000001734498102442000"}
    assert result == direct_result


def test_pdf_path_generation_search_thema(crawler: SearchThemaCrawler, search_thema_item: dict, tmp_data_dir: Path) -> None:
    crawler._prepare_pdf_item(search_thema_item)

    assert crawler._pdf_path_for_item(search_thema_item) == (
        tmp_data_dir
        / "searchThema"
        / "pdfs"
        / "2024"
        / "20241231"
        / "I0000000000000001734498102442000.pdf"
    )


def test_pdf_header_validation(crawler: SearchThemaCrawler, tmp_path: Path) -> None:
    class FakeContent:
        def __init__(self, chunks: list[bytes]):
            self._chunks = chunks

        async def iter_chunked(self, chunk_size: int):
            for chunk in self._chunks:
                yield chunk

    class FakeStreamResponse:
        status = 200
        content = FakeContent([b"%PDF-", b"1.4 fake\n%%EOF\n"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeStreamSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def post(self, url, data):
            return FakeStreamResponse()

    pdf_path = tmp_path / "valid.pdf"
    with (
        patch("base_crawler.aiohttp.ClientTimeout", return_value=Mock(name="timeout")),
        patch("base_crawler.aiohttp.ClientSession", return_value=FakeStreamSession()),
    ):
        result = asyncio.run(crawler._download_pdf_stream(
            crawler._empty_cookie_context(),
            "https://gwanbo.go.kr/user/common/ofcttCntntDownload.do",
            {"cntnt_seq_no": "toc"},
            pdf_path,
        ))

    assert result["status"] == "completed"
    assert result["size_bytes"] == len(b"%PDF-1.4 fake\n%%EOF\n")
    assert pdf_path.read_bytes().startswith(b"%PDF-")


def test_playwright_fallback_triggered(crawler: SearchThemaCrawler, search_thema_item: dict) -> None:
    fallback_result = {
        "status": "completed",
        "path": "data/searchThema/pdfs/2024/20241231/I0000000000000001734498102442000.pdf",
        "size_bytes": 10,
        "sha256": "abc",
        "downloaded_at": "2026-05-08T00:00:00",
    }

    with (
        patch.object(crawler, "_download_pdf_via_http", side_effect=RuntimeError("HTTP failed")),
        patch.object(crawler, "_download_with_playwright_fallback", new=AsyncMock(return_value=fallback_result)) as fallback,
    ):
        item = asyncio.run(crawler._download_item_pdf_once(Mock(name="context"), search_thema_item))

    fallback.assert_awaited_once()
    assert item["pdf"]["status"] == "completed"
    assert item["status"] == "completed"
    assert crawler.stats["downloaded_pdfs"] == 1


def test_successful_download_clears_stale_failure_fields(
    crawler: SearchThemaCrawler,
    search_thema_item: dict,
) -> None:
    search_thema_item["pdf"] = {
        "status": "failed",
        "error": "old failure",
        "failed_at": "2026-05-11T00:00:00Z",
    }
    download_result = {
        "status": "completed",
        "path": "data/searchThema/pdfs/2024/20241231/I0000000000000001734498102442000.pdf",
        "size_bytes": 10,
        "sha256": "abc",
        "downloaded_at": "2026-05-08T00:00:00",
    }

    with patch.object(crawler, "_download_pdf_via_http", new=AsyncMock(return_value=download_result)):
        item = asyncio.run(crawler._download_item_pdf_once(None, search_thema_item))

    assert item["pdf"]["status"] == "completed"
    assert "error" not in item["pdf"]
    assert "failed_at" not in item["pdf"]


def test_http_only_failure_refreshes_browser_session(crawler: SearchThemaCrawler, search_thema_item: dict) -> None:
    fallback_result = {
        "status": "completed",
        "path": "data/searchThema/pdfs/2024/20241231/I0000000000000001734498102442000.pdf",
        "size_bytes": 10,
        "sha256": "abc",
        "downloaded_at": "2026-05-08T00:00:00",
    }

    with (
        patch("crawler_search_thema.HAS_PLAYWRIGHT", True),
        patch("crawler_search_thema.async_playwright", Mock(name="async_playwright")),
        patch.object(crawler, "_download_pdf_via_http", side_effect=RuntimeError("PDF 요청 실패: HTTP 500")),
        patch.object(crawler, "_download_with_fresh_browser_session", new=AsyncMock(return_value=fallback_result)) as fallback,
    ):
        item = asyncio.run(crawler._download_item_pdf_once(None, search_thema_item))

    fallback.assert_awaited_once()
    assert item["pdf"]["status"] == "completed"
    assert item["status"] == "completed"


def test_http_only_failure_tries_session_pool_round_robin(crawler: SearchThemaCrawler, search_thema_item: dict) -> None:
    crawler.session_pool_size = 4
    session_entry = {
        "id": "ABCDEF123456",
        "cookies": [{"name": "JSESSIONID", "value": "ABCDEF123456"}],
    }
    fallback_result = {
        "status": "completed",
        "path": "data/searchThema/pdfs/2024/20241231/I0000000000000001734498102442000.pdf",
        "size_bytes": 10,
        "sha256": "abc",
        "downloaded_at": "2026-05-08T00:00:00",
    }

    with (
        patch.object(crawler, "_next_session_entry", new=AsyncMock(return_value=session_entry)),
        patch.object(crawler, "_download_pdf_via_http", new=AsyncMock(side_effect=RuntimeError("PDF 요청 실패: HTTP 500"))),
        patch.object(crawler, "_download_with_session_pool_round_robin", new=AsyncMock(return_value=fallback_result)) as round_robin,
        patch.object(crawler, "_download_with_fresh_browser_session", new=AsyncMock()) as refresh,
    ):
        item = asyncio.run(crawler._download_item_pdf_once(None, search_thema_item))

    round_robin.assert_awaited_once()
    assert round_robin.await_args.kwargs["exclude_session_ids"] == {"ABCDEF123456"}
    refresh.assert_not_awaited()
    assert item["pdf"]["status"] == "completed"


def test_session_pool_failure_refreshes_failed_session_id(crawler: SearchThemaCrawler, search_thema_item: dict) -> None:
    crawler.session_pool_size = 4
    session_entry = {
        "id": "ABCDEF123456",
        "cookies": [{"name": "JSESSIONID", "value": "ABCDEF123456"}],
    }
    fallback_result = {
        "status": "completed",
        "path": "data/searchThema/pdfs/2024/20241231/I0000000000000001734498102442000.pdf",
        "size_bytes": 10,
        "sha256": "abc",
        "downloaded_at": "2026-05-08T00:00:00",
    }

    with (
        patch("crawler_search_thema.HAS_PLAYWRIGHT", True),
        patch("crawler_search_thema.async_playwright", Mock(name="async_playwright")),
        patch.object(crawler, "_next_session_entry", new=AsyncMock(return_value=session_entry)),
        patch.object(crawler, "_download_pdf_via_http", new=AsyncMock(side_effect=RuntimeError("PDF 요청 실패: HTTP 500"))),
        patch.object(
            crawler,
            "_download_with_session_pool_round_robin",
            new=AsyncMock(side_effect=RuntimeError("SearchThema 세션 ID 라운드로빈 fallback 실패: HTTP 500")),
        ),
        patch.object(crawler, "_download_with_fresh_browser_session", new=AsyncMock(return_value=fallback_result)) as refresh,
    ):
        item = asyncio.run(crawler._download_item_pdf_once(None, search_thema_item))

    refresh.assert_awaited_once()
    assert refresh.await_args.kwargs["failed_session_id"] == "ABCDEF123456"
    assert item["pdf"]["status"] == "completed"
