import json
from pathlib import Path

from src import pdf_layout_metadata
from src.pdf_layout_metadata import (
    analyze_page_layout,
    classify_layout,
    classify_text_quality,
    extract_page_tables,
    generate_source_layout_metadata,
    table_rows_to_json,
)


class FakeTable:
    bbox = (0, 120, 540, 720)

    def extract(self):
        return [
            ["인증번호", "인증번호", ""],
            ["제1-14-1-1096호", "회사A", "품목A"],
            ["제1-14-1-1097호", "회사B", "품목B"],
        ]


class FakePage:
    width = 600
    height = 800

    def extract_text(self):
        return "인증번호 인증번호\n제1-14-1-1096호 회사A 품목A\n제1-14-1-1097호 회사B 품목B"

    def extract_words(self):
        return [
            {"x0": 30, "top": 10},
            {"x0": 35, "top": 20},
            {"x0": 40, "top": 30},
            {"x0": 45, "top": 40},
            {"x0": 250, "top": 10},
            {"x0": 255, "top": 20},
            {"x0": 260, "top": 30},
            {"x0": 265, "top": 40},
        ]

    def find_tables(self, table_settings=None):
        return [FakeTable()]


class FakePdf:
    pages = [FakePage()]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class FakePdfPlumber:
    def open(self, _path):
        return FakePdf()


class TextOnlyTable(FakeTable):
    bbox = (10, 20, 110, 120)

    def extract(self):
        return [["A", "B"], ["1", "2"]]


class TextFallbackPage:
    width = 200
    height = 200

    def extract_text(self):
        return "A B\n1 2"

    def extract_words(self):
        return [{"x0": 10}, {"x0": 60}, {"x0": 10}, {"x0": 60}]

    def find_tables(self, table_settings=None):
        if table_settings and table_settings.get("vertical_strategy") == "text":
            return [TextOnlyTable()]
        return []


class SparseTextTable(FakeTable):
    bbox = (10, 20, 110, 120)

    def extract(self):
        return [["", "2001-", "165", "", "", ""], ["", "", "", "", "8", ""], ["", "", "", "", "", ""]]


class SparseTextPage(TextFallbackPage):
    def find_tables(self, table_settings=None):
        if table_settings and table_settings.get("vertical_strategy") == "text":
            return [SparseTextTable()]
        return []


def write_item(path: Path, pdf_path: Path, text_extractable: bool, pdf_status: str = "completed") -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "id": path.stem,
                "pdf": {"status": pdf_status, "path": str(pdf_path)},
                "pdf_text": {"text_extractable": text_extractable},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_table_rows_to_json_uses_stable_column_keys_for_duplicate_and_empty_headers() -> None:
    table = table_rows_to_json(
        [["이름", "이름", ""], ["홍길동", "서울", "10"], ["김철수", "부산", "20"]],
        page_index=0,
        table_index=0,
        bbox=(0, 1, 2, 3),
    )

    assert table["columns"] == [
        {"key": "col_1", "label": "이름"},
        {"key": "col_2", "label": "이름"},
        {"key": "col_3", "label": ""},
    ]
    assert table["records"][0] == {"col_1": "홍길동", "col_2": "서울", "col_3": "10"}
    assert table["bbox"] == [0.0, 1.0, 2.0, 3.0]


def test_classify_layout_table_ratio_classes() -> None:
    base_metric = {
        "page_index": 0,
        "text_chars": 100,
        "line_count": 10,
        "word_count": 20,
        "estimated_columns": 1,
        "text_quality": "readable",
        "form_score": 0.0,
        "table_count": 0,
        "table_chars": 0,
        "table_text_ratio": 0.0,
    }

    heavy = classify_layout([{**base_metric, "table_count": 1, "table_chars": 70}], [{"table_id": "t1"}])
    mixed = classify_layout([{**base_metric, "table_count": 1, "table_chars": 20}], [{"table_id": "t1"}])
    body = classify_layout([base_metric], [])

    assert heavy["document_class"] == "table_heavy"
    assert mixed["document_class"] == "table_with_body"
    assert body["document_class"] == "body_text"


def test_classify_text_quality_flags_pdfminer_cid_text() -> None:
    assert classify_text_quality("(cid:48115)(cid:51363)(cid:47575) " * 3) == "suspect_or_encoded"


def test_auto_strategy_falls_back_to_text_tables() -> None:
    tables = extract_page_tables(TextFallbackPage(), page_index=0, table_strategy="auto")

    assert len(tables) == 1
    assert tables[0]["extraction_strategy"] == "text"
    assert tables[0]["cell_density"] == 1.0
    assert tables[0]["records"] == [{"col_1": "1", "col_2": "2"}]


def test_auto_strategy_filters_sparse_text_false_tables() -> None:
    assert extract_page_tables(SparseTextPage(), page_index=0, table_strategy="auto") == []


def test_auto_strategy_deduplicates_same_bbox_tables() -> None:
    metric, tables = analyze_page_layout(FakePage(), page_index=0, table_strategy="auto")

    assert metric["table_count"] == 1
    assert len(tables) == 1
    assert tables[0]["alternate_strategies"] == ["lines", "lines_strict", "text"]


def test_generate_source_layout_metadata_skips_non_extractable_items(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    pdf_path = root / "searchThema" / "pdfs" / "2024" / "20240101" / "abc.pdf"
    item_path = root / "searchThema" / "metadata" / "items" / "2024" / "20240101" / "abc.json"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
    write_item(item_path, pdf_path, text_extractable=False)

    summary = generate_source_layout_metadata("searchThema", artifacts_root=root, workers=1)

    sidecar = root / "searchThema" / "layout_metadata" / "items" / "2024" / "20240101" / "abc.json"
    index = root / "searchThema" / "layout_metadata" / "metadata.json"
    assert summary["processed"] == 0
    assert summary["skipped_not_text_extractable"] == 1
    assert not sidecar.exists()
    assert json.loads(index.read_text(encoding="utf-8")) == {}


def test_generate_source_layout_metadata_writes_fake_pdfplumber_sidecar(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pdf_layout_metadata, "pdfplumber", FakePdfPlumber())
    root = tmp_path / "artifacts"
    pdf_path = root / "searchThema" / "pdfs" / "2024" / "20240101" / "abc.pdf"
    item_path = root / "searchThema" / "metadata" / "items" / "2024" / "20240101" / "abc.json"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
    write_item(item_path, pdf_path, text_extractable=True)

    summary = generate_source_layout_metadata("searchThema", artifacts_root=root, workers=1)

    sidecar = root / "searchThema" / "layout_metadata" / "items" / "2024" / "20240101" / "abc.json"
    aggregate = root / "searchThema" / "layout_metadata" / "metadata.json"
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    index = json.loads(aggregate.read_text(encoding="utf-8"))
    item = json.loads(item_path.read_text(encoding="utf-8"))

    assert summary["processed"] == 1
    assert summary["tables"] == 1
    assert summary["updated_items"] == 1
    assert metadata["status"] == "ok"
    assert metadata["layout"]["metrics"]["table_count"] == 1
    assert metadata["tables"][0]["table_id"] == "p001-t001"
    assert metadata["tables"][0]["records"][0] == {
        "col_1": "제1-14-1-1096호",
        "col_2": "회사A",
        "col_3": "품목A",
    }
    assert index["2024/20240101/abc"]["table_count"] == 1
    assert item["schema_version"] == "gwanbo.item.v1"
    assert item["source_detail"] == "searchThema"
    assert item["pdf_layout"]["status"] == "ok"
    assert item["pdf_layout"]["table_count"] == 1
    assert "tables" not in item["pdf_layout"]
