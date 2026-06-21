import sys
import time
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
    complete_pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")

    assert crawler._existing_pdf_is_complete({"pdf": {"status": "completed", "path": str(complete_pdf)}}) is True


def test_existing_pdf_incomplete_missing_file(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)

    assert crawler._existing_pdf_is_complete(
        {"pdf": {"status": "completed", "path": str(tmp_path / "missing.pdf")}}
    ) is False


def test_annotate_ocr_strategy_text_pdf(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)
    pdf_path = tmp_path / "text.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 dummy\n%%EOF\n")

    item = {"id": "1", "ocr": {"status": "pending", "ready_dir": "", "extracted_metadata": {}}}
    crawler._extract_text_pdf_metadata = Mock(return_value={"text_extractable": True, "pages": 1})

    crawler._annotate_ocr_strategy(item, pdf_path)

    assert item["ocr"]["status"] == "skipped_text_extractable"
    assert item["ocr"]["skip_reason"] == "text_extractable_pdf"
    assert item["ocr"]["extracted_metadata"]["text_extractable"] is True
    assert item["pdf_text"] == item["ocr"]["extracted_metadata"]


def test_annotate_ocr_strategy_image_pdf(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)
    pdf_path = tmp_path / "image.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 dummy\n%%EOF\n")

    item = {"id": "2", "ocr": {"status": "pending", "ready_dir": "", "extracted_metadata": {}}}
    crawler._extract_text_pdf_metadata = Mock(return_value={"text_extractable": False, "pages": 1})

    crawler._annotate_ocr_strategy(item, pdf_path)

    assert item["ocr"]["status"] == "pending"
    assert item["ocr"]["skip_reason"] == ""
    assert item["ocr"]["extracted_metadata"]["text_extractable"] is False
    assert item["pdf_text"] == item["ocr"]["extracted_metadata"]


def test_extract_text_pdf_metadata_detects_text(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)
    pdf_path = tmp_path / "text_extractable.pdf"
    pdf_path.write_bytes(b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 44>>stream
BT /F1 24 Tf 72 72 Td (Hello OCR Skip) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f 
0000000010 00000 n 
0000000060 00000 n 
0000000117 00000 n 
0000000243 00000 n 
0000000347 00000 n 
trailer<</Size 6/Root 1 0 R>>
startxref
417
%%EOF
""")

    result = crawler._extract_text_pdf_metadata(pdf_path)

    assert result["text_extractable"] is True
    assert result["text_pages"] >= 1
    assert result["total_chars"] > 0


def test_extract_text_pdf_metadata_handles_invalid_file(tmp_path: Path) -> None:
    crawler = DummyCrawler(tmp_path)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not-a-pdf")

    result = crawler._extract_text_pdf_metadata(broken)

    assert result["text_extractable"] is False
    assert "error" in result


def test_extract_text_pdf_metadata_handles_reader_page_errors(tmp_path: Path, monkeypatch) -> None:
    crawler = DummyCrawler(tmp_path)
    pdf_path = tmp_path / "xref_weird.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 dummy\n%%EOF\n")

    class BrokenReader:
        metadata = {}

        @property
        def pages(self):
            raise ValueError("invalid literal for int() with base 16: b'\\t'")

    monkeypatch.setattr("src.pdf_text_metadata.PdfReader", lambda _path: BrokenReader())

    result = crawler._extract_text_pdf_metadata(pdf_path)

    assert result["text_extractable"] is False
    assert result["pages"] == 0
    assert "invalid literal" in result["error"]


def test_throttle_ignores_future_monotonic_stamp(tmp_path: Path) -> None:
    lock_dir = tmp_path / "network"
    lock_dir.mkdir()
    (lock_dir / "gwanbo.last").write_text(str(time.monotonic() + 3600), encoding="utf-8")

    started = time.monotonic()
    BaseCrawler._throttle_network_sync(lock_dir, 0.01, 0)

    assert time.monotonic() - started < 1


def test_pdf_http_500_is_fallback_candidate() -> None:
    assert BaseCrawler._should_fallback_pdf_http_error(RuntimeError("PDF 요청 실패: HTTP 500")) is True
