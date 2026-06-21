from src.metadata_schema import apply_item_schema
from src.pety_parser import parse_pety_list_page


def test_apply_item_schema_preserves_legacy_source_and_defaults() -> None:
    item = {
        "id": "TOC-1",
        "theme": "pety",
        "source": "legacyEndpoint",
        "viewer_path": "/ezpdf/customLayout.jsp?contentId=CONTENT-1&tocId=TOC-1",
        "ocr": {"status": "pending", "extracted_metadata": {"text_extractable": True, "pages": 1}},
    }

    apply_item_schema(item, source_detail="pety", source_endpoint="petyListAjax")

    assert item["schema_version"] == "gwanbo.item.v1"
    assert item["source"] == "legacyEndpoint"
    assert item["source_system"] == "gwanbo"
    assert item["source_detail"] == "pety"
    assert item["source_endpoint"] == "petyListAjax"
    assert item["source_ids"] == {"id": "TOC-1", "toc_id": "TOC-1", "content_id": "CONTENT-1"}
    assert item["urls"]["viewer_path"].startswith("/ezpdf/customLayout.jsp")
    assert item["pdf_text"] == item["ocr"]["extracted_metadata"]
    assert item["pdf_layout"]["status"] == "pending"
    assert item["graph"]["nodes"] == []
    assert item["embedding"]["dimensions"] == 0


def test_pety_parser_emits_gwanbo_item_schema() -> None:
    html = """
    <div id="countArea">1건</div>
    <div id="tableArea">
      <table><tbody>
      <tr>
        <td>공고</td><td>제목</td><td>기관</td><td>법령</td><td>2024-01-02</td>
        <td><a onclick="fnDetail('TOC1','제목','2024-01-02','호','공고','기관','법령','/viewer?contentId=C1&amp;tocId=TOC1','N','')">보기</a></td>
      </tr>
      </tbody></table>
    </div>
    """

    item = parse_pety_list_page(html, "https://open.gwanbo.go.kr/OpenApi/web/petyListAjax").items[0]

    assert item["schema_version"] == "gwanbo.item.v1"
    assert item["source"] == "petyListAjax"
    assert item["source_system"] == "gwanbo"
    assert item["source_detail"] == "pety"
    assert item["source_endpoint"] == "petyListAjax"
    assert item["source_ids"]["toc_id"] == "TOC1"
    assert item["source_ids"]["content_id"] == "C1"
    assert item["urls"]["source"] == "https://open.gwanbo.go.kr/OpenApi/web/petyListAjax"
    assert item["pdf_text"]["status"] == "pending"
    assert item["pdf_layout"]["status"] == "pending"
