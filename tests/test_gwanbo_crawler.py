import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import pytest


if "aiohttp" not in sys.modules:
    aiohttp_stub = ModuleType("aiohttp")
    setattr(aiohttp_stub, "ClientTimeout", Mock(name="ClientTimeout"))
    setattr(aiohttp_stub, "ClientSession", Mock(name="ClientSession"))
    sys.modules["aiohttp"] = aiohttp_stub

if "playwright.async_api" not in sys.modules:
    async_api_stub = ModuleType("playwright.async_api")
    setattr(async_api_stub, "BrowserContext", Mock(name="BrowserContext"))
    setattr(async_api_stub, "async_playwright", Mock(name="async_playwright"))
    sys.modules["playwright.async_api"] = async_api_stub

if "playwright" not in sys.modules:
    playwright_stub = ModuleType("playwright")
    setattr(playwright_stub, "async_api", sys.modules["playwright.async_api"])
    sys.modules["playwright"] = playwright_stub

from src.crawler import GwanboCrawler


@pytest.fixture
def crawler(tmp_data_dir: Path, mock_config: Mock) -> GwanboCrawler:
    download_dir = tmp_data_dir / "pdfs"
    ocr_dir = tmp_data_dir / "ocr_ready"
    state_file = tmp_data_dir / "state" / "crawl_state.json"

    mock_config.config["crawler"] = {
        "start_date": "2026-01-01",
        "end_date": "2026-02-15",
        "window_days": 31,
        "timeout": 30,
        "retry_delay": 2,
        "max_retries": 3,
        "headless": True,
        "themes": {
            "pety": {
                "thema_se": "02",
                "list_url": "https://open.gwanbo.go.kr/OpenApi/web/petyList",
                "ajax_url": "https://open.gwanbo.go.kr/OpenApi/web/petyListAjax",
                "viewer_base_url": "https://gwanbo.go.kr/",
                "row_per_page": 10,
            }
        },
    }
    mock_config.config["download"] = {
        "pdf_directory": str(download_dir),
        "ocr_ready_directory": str(ocr_dir),
        "chunk_size": 8192,
    }
    mock_config.config["state"] = {"file": str(state_file)}
    mock_config.get_crawler_config.return_value = mock_config.config["crawler"]
    mock_config.get_download_config.return_value = mock_config.config["download"]

    with (
        patch("src.crawler.get_config", return_value=mock_config),
        patch("src.crawler.MetadataManager") as metadata_manager_cls,
        patch("src.crawler.CrawlState") as crawl_state_cls,
        patch("src.crawler.async_playwright", new=Mock(name="async_playwright")),
        patch("src.crawler.BrowserContext", new=Mock(name="BrowserContext")),
        patch("src.crawler.setup_logger", return_value=Mock(name="logger")),
    ):
        metadata_manager_cls.return_value = Mock(name="MetadataManager")
        crawl_state_cls.return_value = Mock(name="CrawlState")
        return GwanboCrawler()


def test_parse_date_formats(crawler: GwanboCrawler) -> None:
    parsed = [
        crawler._parse_date("2026-04-24"),
        crawler._parse_date("2026/04/24"),
        crawler._parse_date("20260424"),
        crawler._parse_date("2026.04.24"),
    ]

    assert all(value == datetime(2026, 4, 24) for value in parsed)
    assert crawler._parse_date("today").date() == datetime.now().date()


def test_date_windows_generation(crawler: GwanboCrawler) -> None:
    windows = list(crawler._date_windows())

    assert windows == [
        (datetime(2026, 1, 1), datetime(2026, 1, 31)),
        (datetime(2026, 2, 1), datetime(2026, 2, 15)),
    ]


def test_extract_download_request(crawler: GwanboCrawler) -> None:
    content_url, content_form = crawler._extract_download_request(
        '<a href="/user/common/ofcttCntntDownload.do;jsessionid=ABC123">pdf</a>',
        {"toc_id": "TOC-123", "content_id": "CONTENT-IGNORED"},
    )
    issue_url, issue_form = crawler._extract_download_request(
        '<form action="/user/common/ofcttDownload.do;jsessionid=XYZ789"></form>',
        {"content_id": "CONTENT-456"},
    )

    assert content_url == "https://gwanbo.go.kr/user/common/ofcttCntntDownload.do;jsessionid=ABC123"
    assert content_form == {"cntnt_seq_no": "TOC-123"}
    assert issue_url == "https://gwanbo.go.kr/user/common/ofcttDownload.do;jsessionid=XYZ789"
    assert issue_form == {"downType": "1", "ofctt_seq_no": "CONTENT-456"}


def test_pdf_path_for_item(crawler: GwanboCrawler, tmp_path: Path) -> None:
    crawler.pdf_dir = tmp_path / "pdfs"
    item = {"id": "Notice 2026/04/24", "date": "2026-04-24"}

    pdf_path = crawler._pdf_path_for_item(item)

    assert pdf_path == tmp_path / "pdfs" / "2026" / "20260424" / "Notice_2026_04_24.pdf"


def test_viewer_url_construction(crawler: GwanboCrawler) -> None:
    direct_url = crawler._viewer_url_for_item(
        {"content_id": "CONTENT-1", "toc_id": "TOC-2"},
        "/ignored/path.jsp",
    )
    fallback_url = crawler._viewer_url_for_item({}, "/ezpdf/viewer.jsp?foo=bar")

    assert direct_url == (
        "https://gwanbo.go.kr/ezpdf/customLayout.jsp?contentId=CONTENT-1&tocId=TOC-2&isTocOrder=N"
    )
    assert fallback_url == "https://gwanbo.go.kr/ezpdf/viewer.jsp?foo=bar"


def test_existing_pdf_is_complete(crawler: GwanboCrawler, tmp_path: Path) -> None:
    complete_pdf = tmp_path / "complete.pdf"
    complete_pdf.write_bytes(b"%PDF-1.7")
    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"")

    assert crawler._existing_pdf_is_complete({"pdf": {"status": "completed", "path": str(complete_pdf)}}) is True
    assert crawler._existing_pdf_is_complete({"pdf": {"status": "completed", "path": str(empty_pdf)}}) is False
    assert crawler._existing_pdf_is_complete({"pdf": {"status": "pending", "path": str(complete_pdf)}}) is False
    assert crawler._existing_pdf_is_complete({"pdf": {"status": "completed", "path": str(tmp_path / "missing.pdf")}}) is False
