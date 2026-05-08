import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock


if "aiohttp" not in sys.modules:
    aiohttp_stub = ModuleType("aiohttp")
    setattr(aiohttp_stub, "ClientTimeout", Mock(name="ClientTimeout"))
    setattr(aiohttp_stub, "ClientSession", Mock(name="ClientSession"))
    sys.modules["aiohttp"] = aiohttp_stub

from src.base_crawler import BaseCrawler


class DummyCrawler(BaseCrawler):
    def __init__(self, pdf_dir: Path):
        self.viewer_base_url = "https://gwanbo.go.kr/"
        self.pdf_dir = pdf_dir
        self.stats = {"start_time": None, "end_time": None}
        self.logger = Mock(name="logger")
        self.max_retries = 3
        self.retry_delay = 0.01
        self.request_timeout = 30
        self.chunk_size = 8192

    async def fetch_items(self, page_number: int):
        return []

    def get_item_id(self, item):
        return str(item["id"])

    def _state_mode(self) -> str:
        return "pdf"


def test_extract_download_request_content_type(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)

    url, form = crawler._extract_download_request(
        '<a href="/user/common/ofcttCntntDownload.do;jsessionid=ABC123">pdf</a>',
        {"toc_id": "TOC-123", "content_id": "CONTENT-IGNORED"},
    )

    assert url == "https://gwanbo.go.kr/user/common/ofcttCntntDownload.do;jsessionid=ABC123"
    assert form == {"cntnt_seq_no": "TOC-123"}


def test_extract_download_request_issue_type(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)

    url, form = crawler._extract_download_request(
        '<form action="/user/common/ofcttDownload.do;jsessionid=XYZ789"></form>',
        {"content_id": "CONTENT-456"},
    )

    assert url == "https://gwanbo.go.kr/user/common/ofcttDownload.do;jsessionid=XYZ789"
    assert form == {"downType": "1", "ofctt_seq_no": "CONTENT-456"}


def test_pdf_path_generation(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path / "pdfs")

    pdf_path = crawler._pdf_path_for_item({"id": "Notice 2026/04/24", "date": "2026-04-24"})

    assert pdf_path == tmp_path / "pdfs" / "2026" / "20260424" / "Notice_2026_04_24.pdf"


def test_viewer_url_content_id_toc_id(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)

    url = crawler._viewer_url_for_item(
        {"content_id": "CONTENT-1", "toc_id": "TOC-2"},
        "/ignored/path.jsp",
    )

    assert url == "https://gwanbo.go.kr/ezpdf/customLayout.jsp?contentId=CONTENT-1&tocId=TOC-2&isTocOrder=N"


def test_existing_pdf_complete_true(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)
    complete_pdf = tmp_path / "complete.pdf"
    complete_pdf.write_bytes(b"%PDF-1.7")

    assert crawler._existing_pdf_is_complete({"pdf": {"status": "completed", "path": str(complete_pdf)}}) is True


def test_existing_pdf_incomplete_missing_file(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)

    assert crawler._existing_pdf_is_complete(
        {"pdf": {"status": "completed", "path": str(tmp_path / "missing.pdf")}}
    ) is False
