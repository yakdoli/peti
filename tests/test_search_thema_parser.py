"""Tests for SearchThema parser."""

from search_thema_parser import (  # type: ignore[reportMissingImports]
    SearchThemaItem,
    SearchThemaPage,
    parse_search_thema_response,
)


def test_parse_single_page_with_items(load_fixture):
    pages = parse_search_thema_response(load_fixture("search_thema_single_page"))

    assert len(pages) == 21
    page = next(page for page in pages if page.category_name == "고시")
    assert page.total_count == 2
    assert page.category_order == "10"
    assert page.page_list
    assert len(page.items) == 2
    assert isinstance(page.items[0], SearchThemaItem)


def test_parse_empty_response(load_fixture):
    pages = parse_search_thema_response(load_fixture("search_thema_empty"))

    assert len(pages) == 21
    assert all(page.total_count == 0 for page in pages)
    assert all(page.items == [] for page in pages)


def test_parse_multi_category(load_fixture):
    pages = parse_search_thema_response(load_fixture("search_thema_multi_category"))

    assert len(pages) == 21
    assert next(page for page in pages if page.category_name == "헌법").total_count == 1
    assert next(page for page in pages if page.category_name == "법률").total_count == 25
    assert next(page for page in pages if page.category_name == "공고").total_count == 3012


def test_item_field_mapping(load_fixture):
    pages = parse_search_thema_response(load_fixture("search_thema_multi_category"))
    item = next(page for page in pages if page.category_name == "헌법").items[0]

    assert item == SearchThemaItem(
        id="00000000000000001522044475961000",
        title="대통령공고 제278호(大韓民國憲法 개정안 공고)",
        date="2018-03-26",
        ebook_no="19221",
        organ_nm="",
        category_name="헌법",
        category_order="1",
        viewer_url="/ezpdf/customLayout.jsp?contentId=00000000000000001522044475739000&tocId=00000000000000001522044475961000&isTocOrder=N",
        file_size="0.7MB",
        page="1",
        keyword="관보(그2)",
        pdf_file_path="/ndata/gwanbo/00000000000000001522044475739000/toc/00000000000000001522044475961000.pdf",
    )


def test_date_normalization(load_fixture):
    pages = parse_search_thema_response(load_fixture("search_thema_single_page"))
    item = next(page for page in pages if page.category_name == "고시").items[0]

    assert item.date == "2024-12-31"
    assert item.date != "20241231"


def test_date_falls_back_to_keyword_regdate():
    pages = parse_search_thema_response({
        "data": [
            {
                "category_name": "공고",
                "count": 1,
                "pageList": "",
                "category_order": "21",
                "list": [
                    {
                        "stored_toc_seq": "toc-1",
                        "stored_field_year": "",
                        "stored_field_month": "",
                        "stored_field_day": "",
                        "keyword_field_regdate": "20240508",
                    }
                ],
            }
        ]
    })

    assert pages[0].items[0].date == "2024-05-08"
