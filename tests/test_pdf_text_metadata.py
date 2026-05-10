import json
from pathlib import Path

from src.pdf_text_metadata import analyze_pdf_text, generate_source_text_metadata, update_item_metadata


TEXT_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 43>>stream
BT /F1 24 Tf 72 72 Td (Hello PDF Text) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f 
0000000010 00000 n 
0000000060 00000 n 
0000000117 00000 n 
0000000243 00000 n 
0000000346 00000 n 
trailer<</Size 6/Root 1 0 R>>
startxref
416
%%EOF
"""


def test_analyze_pdf_text_detects_extractable_text(tmp_path: Path) -> None:
    pdf_path = tmp_path / "text.pdf"
    pdf_path.write_bytes(TEXT_PDF)

    result = analyze_pdf_text(pdf_path, include_sample=True, include_sha256=True)

    assert result["status"] == "ok"
    assert result["text_extractable"] is True
    assert result["text_pages"] == 1
    assert result["total_chars"] > 0
    assert "Hello PDF Text" in result["sample_text"]
    assert len(result["sha256"]) == 64


def test_analyze_pdf_text_records_page_extract_errors(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "page_error.pdf"
    pdf_path.write_bytes(TEXT_PDF)

    class BrokenPage:
        def extract_text(self):
            raise ValueError("broken page")

    class BrokenReader:
        metadata = {}
        pages = [BrokenPage()]

    monkeypatch.setattr("src.pdf_text_metadata.PdfReader", lambda _path: BrokenReader())

    result = analyze_pdf_text(pdf_path)

    assert result["status"] == "ok"
    assert result["text_extractable"] is False
    assert result["page_error_count"] == 1
    assert "broken page" in result["page_errors"][0]["error"]


def test_analyze_pdf_text_records_timeout(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "timeout.pdf"
    pdf_path.write_bytes(TEXT_PDF)

    class SlowReader:
        @property
        def pages(self):
            import time

            time.sleep(2)
            return []

    monkeypatch.setattr("src.pdf_text_metadata.PdfReader", lambda _path: SlowReader())

    result = analyze_pdf_text(pdf_path, timeout_seconds=1)

    assert result["status"] == "error"
    assert "timed out" in result["error"]


def test_update_item_metadata_marks_text_extractable(tmp_path: Path) -> None:
    item_path = tmp_path / "items" / "2024" / "20240101" / "abc.json"
    item_path.parent.mkdir(parents=True)
    item_path.write_text(json.dumps({"id": "abc", "ocr": {"status": "pending"}}, ensure_ascii=False), encoding="utf-8")

    updated = update_item_metadata(
        item_path,
        {"text_extractable": True, "pages": 1, "text_pages": 1, "total_chars": 14, "sample_text": "not stored"},
    )

    item = json.loads(item_path.read_text(encoding="utf-8"))
    assert updated is True
    assert item["pdf_text"]["text_extractable"] is True
    assert "sample_text" not in item["pdf_text"]
    assert item["ocr"]["status"] == "skipped_text_extractable"
    assert item["ocr"]["skip_reason"] == "text_extractable_pdf"


def test_generate_source_text_metadata_writes_sidecars_and_updates_items(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    pdf_path = root / "pety" / "pdfs" / "2024" / "20240101" / "abc.pdf"
    item_path = root / "pety" / "metadata" / "items" / "2024" / "20240101" / "abc.json"
    pdf_path.parent.mkdir(parents=True)
    item_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(TEXT_PDF)
    item_path.write_text(json.dumps({"id": "abc", "ocr": {"status": "pending"}}, ensure_ascii=False), encoding="utf-8")

    summary = generate_source_text_metadata("pety", artifacts_root=root, update_items=True)

    sidecar = root / "pety" / "text_metadata" / "items" / "2024" / "20240101" / "abc.json"
    aggregate = root / "pety" / "text_metadata" / "metadata.json"
    item = json.loads(item_path.read_text(encoding="utf-8"))

    assert summary["processed"] == 1
    assert summary["text_extractable"] == 1
    assert summary["updated_items"] == 1
    assert json.loads(sidecar.read_text(encoding="utf-8"))["text_extractable"] is True
    assert "2024/20240101/abc" in json.loads(aggregate.read_text(encoding="utf-8"))
    assert item["ocr"]["status"] == "skipped_text_extractable"
