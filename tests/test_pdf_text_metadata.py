import json
from pathlib import Path

from src import pdf_text_metadata
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

    result = analyze_pdf_text(pdf_path, recover_with_pymupdf=False)

    assert result["status"] == "ok"
    assert result["text_extractable"] is False
    assert result["page_error_count"] == 1
    assert "broken page" in result["page_errors"][0]["error"]
    assert result["pdf_text_class"] == "unknown_unextractable"
    assert result["needs_ocr"] is True


def test_analyze_pdf_text_skips_recovery_without_digital_evidence(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "image_only.pdf"
    pdf_path.write_bytes(TEXT_PDF)

    class ImageOnlyPage:
        def get(self, key, default=None):
            if key == "/Resources":
                return {}
            return default

        def extract_text(self):
            return ""

    class ImageOnlyReader:
        metadata = {}
        pages = [ImageOnlyPage()]

    def fail_recovery(*_args, **_kwargs):
        raise AssertionError("recovery should not run without digital PDF evidence")

    monkeypatch.setattr("src.pdf_text_metadata.PdfReader", lambda _path: ImageOnlyReader())
    monkeypatch.setattr(pdf_text_metadata, "apply_pymupdf_recovery", fail_recovery)

    result = analyze_pdf_text(pdf_path)

    assert result["text_extractable"] is False
    assert result["pdf_text_class"] == "unknown_unextractable"
    assert result["needs_ocr"] is True
    assert "recovery" not in result


def test_analyze_pdf_text_recovers_digital_pdf_with_pymupdf(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "digital_no_pypdf_text.pdf"
    pdf_path.write_bytes(TEXT_PDF)

    class EmptyTextPage:
        def get(self, key, default=None):
            if key == "/Resources":
                return {"/Font": {"/F1": object()}}
            return default

        def extract_text(self):
            return ""

    class EmptyTextReader:
        metadata = {"/Producer": "digital producer"}
        pages = [EmptyTextPage()]

    class FakePyMuPDFPage:
        def get_text(self, mode="text"):
            assert mode == "text"
            return "복구된 디지털 텍스트"

    class FakePyMuPDFDocument:
        def __len__(self):
            return 1

        def __getitem__(self, index: int):
            return FakePyMuPDFPage()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakePyMuPDF:
        def open(self, _path: str):
            return FakePyMuPDFDocument()

    monkeypatch.setattr("src.pdf_text_metadata.PdfReader", lambda _path: EmptyTextReader())
    monkeypatch.setattr(pdf_text_metadata, "pymupdf", FakePyMuPDF())

    result = analyze_pdf_text(pdf_path, include_sample=True)

    assert result["text_extractable"] is True
    assert result["primary_text_extractable"] is False
    assert result["recovered_text"] is True
    assert result["preferred_text_source"] == "pymupdf"
    assert result["pdf_text_class"] == "digital_text_recovered"
    assert result["needs_ocr"] is False
    assert result["digital_origin_evidence"]["has_fonts"] is True
    assert result["recovery"]["text_extractable"] is True
    assert result["recovery"]["text_quality"]["status"] == "unknown_short_text"
    assert "복구된 디지털 텍스트" in result["sample_text"]


def test_analyze_pdf_text_recovers_digital_pdf_with_markitdown(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "digital_markitdown.pdf"
    pdf_path.write_bytes(TEXT_PDF)

    class EmptyTextPage:
        def get(self, key, default=None):
            if key == "/Resources":
                return {"/Font": {"/F1": object()}}
            return default

        def extract_text(self):
            return ""

    class EmptyTextReader:
        metadata = {}
        pages = [EmptyTextPage()]

    class EmptyPyMuPDF:
        def open(self, _path: str):
            raise RuntimeError("no native text")

    class FakeMarkItDownResult:
        text_content = "마크다운으로 복구한 텍스트"

    class FakeMarkItDown:
        def __init__(self, enable_plugins=False):
            self.enable_plugins = enable_plugins

        def convert(self, _path: str):
            return FakeMarkItDownResult()

    monkeypatch.setattr("src.pdf_text_metadata.PdfReader", lambda _path: EmptyTextReader())
    monkeypatch.setattr(pdf_text_metadata, "pymupdf", EmptyPyMuPDF())
    monkeypatch.setattr(pdf_text_metadata, "MarkItDown", FakeMarkItDown)

    result = analyze_pdf_text(pdf_path, include_sample=True)

    assert result["text_extractable"] is True
    assert result["pdf_text_class"] == "digital_text_recovered"
    assert result["preferred_text_source"] == "markitdown"
    assert result["recovery"]["preferred_text_source"] == "markitdown"
    assert result["recovery"]["attempts"][0]["backend"] == "pymupdf"
    assert result["recovery"]["attempts"][1]["backend"] == "markitdown"
    assert "마크다운으로 복구한 텍스트" in result["sample_text"]


def test_analyze_pdf_text_recovers_after_ghostscript_normalization(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "digital_ghostscript.pdf"
    pdf_path.write_bytes(TEXT_PDF)

    class EmptyTextPage:
        def get(self, key, default=None):
            if key == "/Resources":
                return {"/Font": {"/F1": object()}}
            return default

        def extract_text(self):
            return ""

    class EmptyTextReader:
        metadata = {}
        pages = [EmptyTextPage()]

    def fake_pymupdf_extract(path: Path, **kwargs):
        if path.name == "ghostscript-normalized.pdf":
            return {
                "status": "ok",
                "method": "PyMuPDF.Page.get_text(text)",
                "text_extractable": True,
                "text_pages": 1,
                "total_chars": 14,
                "sample_text": "GS 복구 텍스트",
            }
        return {
            "status": "ok",
            "method": "PyMuPDF.Page.get_text(text)",
            "text_extractable": False,
            "text_pages": 0,
            "total_chars": 0,
        }

    def fake_markitdown_extract(_path: Path, **_kwargs):
        return {
            "status": "error",
            "method": "MarkItDown.convert",
            "text_extractable": False,
            "text_pages": 0,
            "total_chars": 0,
            "error": "no text",
        }

    def fake_ghostscript(_pdf_path: Path, output_path: Path, **_kwargs):
        output_path.write_bytes(b"%PDF-1.7 normalized")
        return {
            "status": "ok",
            "method": "Ghostscript pdfwrite",
            "text_extractable": False,
            "output_size_bytes": output_path.stat().st_size,
        }

    monkeypatch.setattr("src.pdf_text_metadata.PdfReader", lambda _path: EmptyTextReader())
    monkeypatch.setattr(pdf_text_metadata, "extract_with_pymupdf_text", fake_pymupdf_extract)
    monkeypatch.setattr(pdf_text_metadata, "extract_with_markitdown_text", fake_markitdown_extract)
    monkeypatch.setattr(pdf_text_metadata, "normalize_pdf_with_ghostscript", fake_ghostscript)

    result = analyze_pdf_text(pdf_path, include_sample=True)

    assert result["text_extractable"] is True
    assert result["pdf_text_class"] == "digital_text_recovered"
    assert result["preferred_text_source"] == "ghostscript_pymupdf"
    assert result["recovery"]["ghostscript_normalized"] is True
    assert [attempt["backend"] for attempt in result["recovery"]["attempts"]] == [
        "pymupdf",
        "markitdown",
        "ghostscript_pdfwrite",
        "ghostscript_pymupdf",
    ]


def test_analyze_pdf_text_rejects_mojibake_recovery(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "digital_mojibake.pdf"
    pdf_path.write_bytes(TEXT_PDF)

    class EmptyTextPage:
        def get(self, key, default=None):
            if key == "/Resources":
                return {"/Font": {"/F1": object()}}
            return default

        def extract_text(self):
            return ""

    class EmptyTextReader:
        metadata = {}
        pages = [EmptyTextPage()]

    mojibake = "샼뛳뎲떵볒맦벭뾡듫쟏뾩쇶맦샧솤맽솦" * 8

    def fake_pymupdf_extract(_path: Path, **_kwargs):
        return {
            "status": "ok",
            "method": "PyMuPDF.Page.get_text(text)",
            "text_extractable": True,
            "text_pages": 1,
            "total_chars": len(mojibake),
            "sample_text": mojibake,
            "text_quality": pdf_text_metadata.assess_extracted_text_quality(mojibake),
        }

    def fake_markitdown_extract(_path: Path, **_kwargs):
        return {
            "status": "error",
            "method": "MarkItDown.convert",
            "text_extractable": False,
            "text_pages": 0,
            "total_chars": 0,
            "error": "no text",
        }

    def fake_ghostscript(_pdf_path: Path, output_path: Path, **_kwargs):
        output_path.write_bytes(b"%PDF-1.7 normalized")
        return {"status": "ok", "method": "Ghostscript pdfwrite", "output_size_bytes": output_path.stat().st_size}

    monkeypatch.setattr("src.pdf_text_metadata.PdfReader", lambda _path: EmptyTextReader())
    monkeypatch.setattr(pdf_text_metadata, "extract_with_pymupdf_text", fake_pymupdf_extract)
    monkeypatch.setattr(pdf_text_metadata, "extract_with_markitdown_text", fake_markitdown_extract)
    monkeypatch.setattr(pdf_text_metadata, "normalize_pdf_with_ghostscript", fake_ghostscript)

    result = analyze_pdf_text(pdf_path)

    assert result["text_extractable"] is False
    assert result["recovered_text"] is False
    assert result["pdf_text_class"] == "digital_text_unrecovered"
    assert result["needs_ocr"] is True
    assert result["recovery"]["attempts"][0]["text_quality"]["status"] == "suspect_mojibake"


def test_analyze_pdf_text_rejects_markitdown_cid_placeholders(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "digital_cid_markitdown.pdf"
    pdf_path.write_bytes(TEXT_PDF)

    class EmptyTextPage:
        def get(self, key, default=None):
            if key == "/Resources":
                return {"/Font": {"/F1": object()}}
            return default

        def extract_text(self):
            return ""

    class EmptyTextReader:
        metadata = {}
        pages = [EmptyTextPage()]

    cid_text = " ".join(f"(cid:{49000 + index})" for index in range(20))

    def fake_pymupdf_extract(_path: Path, **_kwargs):
        return {
            "status": "ok",
            "method": "PyMuPDF.Page.get_text(text)",
            "text_extractable": False,
            "text_pages": 0,
            "total_chars": 0,
        }

    def fake_markitdown_extract(_path: Path, **_kwargs):
        return {
            "status": "ok",
            "method": "MarkItDown.convert",
            "text_extractable": True,
            "text_pages": 1,
            "total_chars": len(cid_text),
            "sample_text": cid_text,
            "text_quality": pdf_text_metadata.assess_extracted_text_quality(cid_text),
        }

    def fake_ghostscript(_pdf_path: Path, output_path: Path, **_kwargs):
        output_path.write_bytes(b"%PDF-1.7 normalized")
        return {"status": "ok", "method": "Ghostscript pdfwrite", "output_size_bytes": output_path.stat().st_size}

    monkeypatch.setattr("src.pdf_text_metadata.PdfReader", lambda _path: EmptyTextReader())
    monkeypatch.setattr(pdf_text_metadata, "extract_with_pymupdf_text", fake_pymupdf_extract)
    monkeypatch.setattr(pdf_text_metadata, "extract_with_markitdown_text", fake_markitdown_extract)
    monkeypatch.setattr(pdf_text_metadata, "normalize_pdf_with_ghostscript", fake_ghostscript)

    result = analyze_pdf_text(pdf_path)

    assert result["text_extractable"] is False
    assert result["pdf_text_class"] == "digital_text_unrecovered"
    assert result["recovery"]["attempts"][1]["text_quality"]["status"] == "suspect_cid_placeholders"


def test_normalize_pdf_with_ghostscript_limits_pages(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "input.pdf"
    output_path = tmp_path / "normalized.pdf"
    pdf_path.write_bytes(TEXT_PDF)
    captured = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output_arg = next(part for part in command if part.startswith("-sOutputFile="))
        Path(output_arg.split("=", 1)[1]).write_bytes(b"%PDF-1.7 normalized")
        return Completed()

    monkeypatch.setattr(pdf_text_metadata.shutil, "which", lambda _name: "/usr/bin/gs")
    monkeypatch.setattr(pdf_text_metadata.subprocess, "run", fake_run)

    result = pdf_text_metadata.normalize_pdf_with_ghostscript(pdf_path, output_path, max_pages=3)

    assert result["status"] == "ok"
    assert result["normalized_pages"] == 3
    assert "-dFirstPage=1" in captured["command"]
    assert "-dLastPage=3" in captured["command"]
    assert captured["command"][-1] == str(pdf_path)


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
    assert item["schema_version"] == "gwanbo.item.v1"
    assert item["source_system"] == "gwanbo"
    assert item["pdf_text"] == item["ocr"]["extracted_metadata"]
    assert "pdf_layout" in item
    assert "graph" in item
    assert "embedding" in item
    assert item["ocr"]["status"] == "skipped_text_extractable"
    assert item["ocr"]["skip_reason"] == "text_extractable_pdf"


def test_update_item_metadata_marks_recovered_digital_text_as_ocr_skip(tmp_path: Path) -> None:
    item_path = tmp_path / "items" / "2024" / "20240101" / "abc.json"
    item_path.parent.mkdir(parents=True)
    item_path.write_text(json.dumps({"id": "abc", "ocr": {"status": "pending"}}, ensure_ascii=False), encoding="utf-8")

    updated = update_item_metadata(
        item_path,
        {
            "text_extractable": True,
            "primary_text_extractable": False,
            "recovered_text": True,
            "pdf_text_class": "digital_text_recovered",
            "preferred_text_source": "pymupdf",
            "needs_ocr": False,
            "sample_text": "not stored",
            "recovery": {"text_extractable": True, "sample_text": "not stored"},
        },
    )

    item = json.loads(item_path.read_text(encoding="utf-8"))
    assert updated is True
    assert item["pdf_text"]["pdf_text_class"] == "digital_text_recovered"
    assert item["pdf_text"]["recovered_text"] is True
    assert "sample_text" not in item["pdf_text"]
    assert "sample_text" not in item["pdf_text"]["recovery"]
    assert item["ocr"]["status"] == "skipped_text_extractable"


def test_update_item_metadata_strips_nested_recovery_samples(tmp_path: Path) -> None:
    item_path = tmp_path / "items" / "2024" / "20240101" / "abc.json"
    item_path.parent.mkdir(parents=True)
    item_path.write_text(json.dumps({"id": "abc", "ocr": {"status": "pending"}}, ensure_ascii=False), encoding="utf-8")

    updated = update_item_metadata(
        item_path,
        {
            "text_extractable": True,
            "sample_text": "top sample",
            "recovery": {
                "sample_text": "recovery sample",
                "attempts": [
                    {"backend": "markitdown", "sample_text": "attempt sample", "text_extractable": True}
                ],
            },
        },
    )

    item = json.loads(item_path.read_text(encoding="utf-8"))
    assert updated is True
    assert "sample_text" not in item["pdf_text"]
    assert "sample_text" not in item["pdf_text"]["recovery"]
    assert "sample_text" not in item["pdf_text"]["recovery"]["attempts"][0]


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
    assert item["pdf_text"] == item["ocr"]["extracted_metadata"]
