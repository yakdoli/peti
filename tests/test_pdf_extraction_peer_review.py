import json
from pathlib import Path

from src import pdf_extraction_peer_review
from src.pdf_extraction_peer_review import (
    analyze_pdf_extraction_peer_review,
    decide_extraction,
    extract_with_markitdown,
    extract_with_image_ocr,
    generate_source_extraction_peer_review,
    peer_review_extractions,
)


class FakeMarkItDownResult:
    text_content = "# 제목\n\n마크다운 본문입니다."


class FakeMarkItDown:
    def __init__(self, enable_plugins=False):
        self.enable_plugins = enable_plugins

    def convert(self, _path):
        return FakeMarkItDownResult()


class FakePixmap:
    def save(self, path: str) -> None:
        Path(path).write_bytes(b"fake png")


class FakePage:
    def get_pixmap(self, dpi=200):
        return FakePixmap()


class FakeDocument:
    def __init__(self, _path: str):
        self.pages = [FakePage(), FakePage()]

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index: int):
        return self.pages[index]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class FakePyMuPDF:
    def open(self, path: str):
        return FakeDocument(path)


class FakePytesseract:
    def image_to_string(self, image_path: str, lang: str = "kor+eng", timeout: int = 30):
        return f"OCR {Path(image_path).name} {lang}"


def write_item(path: Path, pdf_path: Path, status: str = "completed") -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"id": path.stem, "pdf": {"status": status, "path": str(pdf_path)}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_peer_review_prefers_longest_successful_text() -> None:
    peers = {
        "pdf_text": {"status": "ok", "text_chars": 10, "text_extractable": True, "sample_text": "짧은 글"},
        "markitdown": {"status": "ok", "text_chars": 100, "text_extractable": True, "sample_text": "긴 글" * 10},
        "image_ocr": {"status": "error", "text_chars": 0, "error": "no tesseract"},
    }

    review = peer_review_extractions(peers)
    decision = decide_extraction(peers, review)

    assert review["best_text_method"] == "markitdown"
    assert decision == {
        "text_extractable": True,
        "preferred_text_source": "markitdown",
        "needs_ocr": False,
        "reason": "text layer or markdown conversion produced text",
    }
    assert any("image_ocr failed" in warning for warning in review["warnings"])


def test_image_ocr_saves_page_images_and_extracts_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pdf_extraction_peer_review, "pymupdf", FakePyMuPDF())
    monkeypatch.setattr(pdf_extraction_peer_review, "pytesseract", FakePytesseract())
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")

    result = extract_with_image_ocr(
        pdf_path,
        image_output_dir=tmp_path / "images",
        max_pages=1,
        sample_chars=100,
        timeout_seconds=30,
        lang="kor+eng",
        dpi=150,
    )

    assert result["status"] == "ok"
    assert result["text_extractable"] is True
    assert result["scanned_pages"] == 1
    assert result["images"][0]["path"].endswith("page_001.png")
    assert Path(result["images"][0]["path"]).exists()
    assert "OCR page_001.png" in result["sample_text"]


def test_image_ocr_records_error_after_saving_image_when_tesseract_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pdf_extraction_peer_review, "pymupdf", FakePyMuPDF())
    monkeypatch.setattr(pdf_extraction_peer_review, "pytesseract", None)
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")

    result = extract_with_image_ocr(
        pdf_path,
        image_output_dir=tmp_path / "images",
        max_pages=1,
        sample_chars=100,
        timeout_seconds=30,
        lang="kor+eng",
        dpi=150,
    )

    assert result["status"] == "error"
    assert result["images"][0]["status"] == "error"
    assert Path(result["images"][0]["path"]).exists()
    assert "pytesseract is not installed" in result["error"]


def test_markitdown_records_missing_dependency(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pdf_extraction_peer_review, "MarkItDown", None)
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")

    result = extract_with_markitdown(pdf_path, sample_chars=100, timeout_seconds=30)

    assert result["status"] == "error"
    assert result["text_extractable"] is False
    assert "markitdown is not installed" in result["error"]


def test_analyze_pdf_extraction_peer_review_uses_all_peers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pdf_extraction_peer_review, "MarkItDown", FakeMarkItDown)
    monkeypatch.setattr(pdf_extraction_peer_review, "pymupdf", FakePyMuPDF())
    monkeypatch.setattr(pdf_extraction_peer_review, "pytesseract", FakePytesseract())
    monkeypatch.setattr(
        pdf_extraction_peer_review,
        "analyze_pdf_text",
        lambda *args, **kwargs: {
            "status": "ok",
            "text_extractable": True,
            "pages": 1,
            "scanned_pages": 1,
            "total_chars": 8,
            "sample_text": "PDF 텍스트",
        },
    )
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")

    result = analyze_pdf_extraction_peer_review(pdf_path, image_output_dir=tmp_path / "images", max_pages=1)

    assert result["analysis_scope"] == "first_1_pages"
    assert set(result["peers"]) == {"pdf_text", "markitdown", "image_ocr"}
    assert result["peers"]["markitdown"]["status"] == "ok"
    assert result["peers"]["image_ocr"]["images"][0]["status"] == "ok"
    assert result["decision"]["text_extractable"] is True


def test_generate_source_extraction_peer_review_writes_sidecar_and_index(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "artifacts"
    pdf_path = root / "searchThema" / "pdfs" / "2024" / "20240101" / "abc.pdf"
    item_path = root / "searchThema" / "metadata" / "items" / "2024" / "20240101" / "abc.json"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
    write_item(item_path, pdf_path)

    def fake_analyze(pdf_path_arg, *, image_output_dir, **_kwargs):
        image_output_dir.mkdir(parents=True)
        image_path = image_output_dir / "page_001.png"
        image_path.write_bytes(b"fake png")
        return {
            "status": "ok",
            "generated_at": "2026-01-01T00:00:00",
            "peers": {
                "pdf_text": {"status": "ok", "text_chars": 5, "text_extractable": True},
                "markitdown": {"status": "skipped", "text_chars": 0},
                "image_ocr": {"status": "ok", "text_chars": 4, "images": [{"path": str(image_path)}]},
            },
            "review": {"best_text_method": "pdf_text", "peer_summaries": {}},
            "decision": {"text_extractable": True, "preferred_text_source": "pdf_text", "needs_ocr": False},
        }

    monkeypatch.setattr(pdf_extraction_peer_review, "analyze_pdf_extraction_peer_review", fake_analyze)

    summary = generate_source_extraction_peer_review("searchThema", artifacts_root=root, workers=1)

    sidecar = root / "searchThema" / "extraction_peer_review" / "items" / "2024" / "20240101" / "abc.json"
    aggregate = root / "searchThema" / "extraction_peer_review" / "metadata.json"
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    index = json.loads(aggregate.read_text(encoding="utf-8"))

    assert summary["processed"] == 1
    assert summary["images_saved"] == 1
    assert metadata["pdf_key"] == "2024/20240101/abc"
    assert index["2024/20240101/abc"]["best_text_method"] == "pdf_text"
