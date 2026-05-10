import asyncio
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import pytest


if "aiohttp" not in sys.modules:
    aiohttp_stub = ModuleType("aiohttp")
    setattr(aiohttp_stub, "ClientTimeout", Mock(name="ClientTimeout"))
    setattr(aiohttp_stub, "ClientSession", Mock(name="ClientSession"))
    sys.modules["aiohttp"] = aiohttp_stub

from crawler_search_thema import SearchThemaCrawler  # type: ignore[reportMissingImports]


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse, calls: list[dict]):
        self._response = response
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def post(self, url, data):
        self._calls.append({"url": url, "data": data})
        return self._response


class FakeContextRequest:
    def __init__(self, error: Exception):
        self._error = error

    async def post(self, *args, **kwargs):
        raise self._error


class FakeContext:
    def __init__(self, error: Exception):
        self.request = FakeContextRequest(error)


@pytest.fixture
def crawler(tmp_data_dir: Path, mock_config: Mock) -> SearchThemaCrawler:
    pdf_dir = tmp_data_dir / "pdfs"
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
                "institution_query_map": {
                    "정부공직자윤리위원회": "정부공직자",
                    "대법원공직자윤리위원회": "대법원",
                },
                "enable_pety_html_fallback": True,
            }
        },
    }
    mock_config.config["download"] = {
        "pdf_directory": str(pdf_dir),
        "chunk_size": 8192,
    }
    mock_config.get_crawler_config.return_value = mock_config.config["crawler"]
    mock_config.get_download_config.return_value = mock_config.config["download"]
    mock_config.get_search_thema_config.return_value = mock_config.config["crawler"]["themes"]["searchThema"]

    with (
        patch("crawler_search_thema.get_config", return_value=mock_config),
        patch("crawler_search_thema.setup_logger", return_value=Mock(name="logger")),
    ):
        return SearchThemaCrawler()


def test_build_query_year_only(crawler: SearchThemaCrawler) -> None:
    assert crawler._build_query(2024, None) == "unstored_field_subject:(2024) AND keyword_category_order:(@@ORDER_NUM)"


def test_build_query_year_institution(crawler: SearchThemaCrawler) -> None:
    assert crawler._build_query(2024, "정부공직자윤리위원회") == (
        "unstored_field_subject:(2024 AND 정부공직자) AND keyword_category_order:(@@ORDER_NUM)"
    )


def test_fetch_items_mocked(crawler: SearchThemaCrawler, load_fixture) -> None:
    calls: list[dict] = []
    payload = load_fixture("search_thema_single_page")
    session_factory = lambda **kwargs: FakeSession(FakeResponse(200, payload), calls)

    with (
        patch("crawler_search_thema.aiohttp.ClientTimeout", return_value=Mock(name="timeout")),
        patch("crawler_search_thema.aiohttp.ClientSession", side_effect=session_factory),
    ):
        items = asyncio.run(crawler.fetch_items(2024, "정부공직자윤리위원회", 2))

    assert len(items) == 8
    assert crawler.get_item_id(items[0]) == "I0000000000000001734498102442000"
    assert calls == [
        {
            "url": "https://gwanbo.go.kr/SearchRestApi.jsp",
            "data": {
                "mode": "theme",
                "index": "gwanbo",
                "query": "unstored_field_subject:(2024 AND 정부공직자) AND keyword_category_order:(@@ORDER_NUM)",
                "pageNo": 2,
                "listSize": 10,
                "tab_Year1": "2024",
                "GOV_1": "정부공직자윤리위원회",
            },
        }
    ]


def test_pagination_detection(crawler: SearchThemaCrawler, load_fixture) -> None:
    payload = load_fixture("search_thema_empty")
    session_factory = lambda **kwargs: FakeSession(FakeResponse(200, payload), [])

    with (
        patch("crawler_search_thema.aiohttp.ClientTimeout", return_value=Mock(name="timeout")),
        patch("crawler_search_thema.aiohttp.ClientSession", side_effect=session_factory),
    ):
        items = asyncio.run(crawler.fetch_items(2024, None, 1))

    assert items == []


def test_empty_response_handling(crawler: SearchThemaCrawler) -> None:
    session_factory = lambda **kwargs: FakeSession(FakeResponse(200, {}), [])

    with (
        patch("crawler_search_thema.aiohttp.ClientTimeout", return_value=Mock(name="timeout")),
        patch("crawler_search_thema.aiohttp.ClientSession", side_effect=session_factory),
    ):
        items = asyncio.run(crawler.fetch_items(2024, None, 1))

    assert items == []


def test_error_response_handling(crawler: SearchThemaCrawler) -> None:
    session_factory = lambda **kwargs: FakeSession(FakeResponse(500, {}), [])

    with (
        patch("crawler_search_thema.aiohttp.ClientTimeout", return_value=Mock(name="timeout")),
        patch("crawler_search_thema.aiohttp.ClientSession", side_effect=session_factory),
    ):
        with pytest.raises(RuntimeError, match="SearchThema 목록 요청 실패"):
            asyncio.run(crawler.fetch_items(2024, None, 1))


def test_soft_error_response_handling(crawler: SearchThemaCrawler) -> None:
    session_factory = lambda **kwargs: FakeSession(FakeResponse(200, {"error": "bad request"}), [])

    with (
        patch("crawler_search_thema.aiohttp.ClientTimeout", return_value=Mock(name="timeout")),
        patch("crawler_search_thema.aiohttp.ClientSession", side_effect=session_factory),
    ):
        with pytest.raises(RuntimeError, match="SearchThema 목록 요청 실패"):
            asyncio.run(crawler.fetch_items(2024, None, 1))


def test_text_response_encoding_handling(crawler: SearchThemaCrawler, load_fixture) -> None:
    class TextResponse(FakeResponse):
        headers = {"Content-Type": "application/json; charset=utf-8"}

        async def text(self, encoding=None):
            import json

            return json.dumps(self._payload, ensure_ascii=False)

    calls: list[dict] = []
    session_factory = lambda **kwargs: FakeSession(TextResponse(200, load_fixture("search_thema_empty")), calls)

    with (
        patch("crawler_search_thema.aiohttp.ClientTimeout", return_value=Mock(name="timeout")),
        patch("crawler_search_thema.aiohttp.ClientSession", side_effect=session_factory),
    ):
        assert asyncio.run(crawler.fetch_items(2024, None, 1)) == []


def test_json_parse_error_fallback_to_pety_style(crawler: SearchThemaCrawler) -> None:
    html = """
    <div id="countArea">1건</div>
    <div id="tableArea">
      <table><tbody>
      <tr>
        <td>공고</td><td>제목</td><td>기관</td><td>법령</td><td>2024-01-02</td>
        <td><a onclick="fnDetail('TOC1','제목','2024-01-02','호','공고','기관','법령','/viewer?contentId=C1&tocId=TOC1','N','')">보기</a></td>
      </tr>
      </tbody></table>
    </div>
    """

    class JsonErrorResponse(FakeResponse):
        async def text(self, encoding=None):
            return self._payload

        async def json(self, content_type=None):
            raise ValueError("json decode error")

    calls: list[dict] = []
    responses = [JsonErrorResponse(200, "{}"), FakeResponse(200, html)]
    session_index = {"value": 0}

    def session_factory(**kwargs):
        response = responses[session_index["value"]]
        session_index["value"] += 1
        return FakeSession(response, calls)
    with (
        patch("crawler_search_thema.aiohttp.ClientTimeout", return_value=Mock(name="timeout")),
        patch("crawler_search_thema.aiohttp.ClientSession", side_effect=session_factory),
    ):
        items = asyncio.run(crawler.fetch_items(2024, None, 1))

    assert len(items) == 1
    assert items[0]["id"] == "TOC1"


def test_playwright_request_enetunreach_fallback_to_aiohttp(crawler: SearchThemaCrawler, load_fixture) -> None:
    calls: list[dict] = []
    payload = load_fixture("search_thema_single_page")
    session_factory = lambda **kwargs: FakeSession(FakeResponse(200, payload), calls)

    with (
        patch("crawler_search_thema.aiohttp.ClientTimeout", return_value=Mock(name="timeout")),
        patch("crawler_search_thema.aiohttp.ClientSession", side_effect=session_factory),
    ):
        context = FakeContext(RuntimeError("connect ENETUNREACH 27.101.207.105:443"))
        items = asyncio.run(crawler.fetch_items(2024, "정부공직자윤리위원회", 1, context=context))

    assert len(items) == 8
    assert calls, "aiohttp fallback should be used when Playwright request fails"


def test_pety_html_fallback_disabled_by_default(tmp_data_dir: Path, mock_config: Mock) -> None:
    mock_config.config["crawler"] = {
        "timeout": 30,
        "retry_delay": 0,
        "max_retries": 1,
        "themes": {"searchThema": {"search_api_url": "https://gwanbo.go.kr/SearchRestApi.jsp"}},
    }
    mock_config.config["download"] = {"pdf_directory": str(tmp_data_dir / "pdfs"), "chunk_size": 8192}
    mock_config.get_crawler_config.return_value = mock_config.config["crawler"]
    mock_config.get_download_config.return_value = mock_config.config["download"]
    mock_config.get_search_thema_config.return_value = mock_config.config["crawler"]["themes"]["searchThema"]

    with (
        patch("crawler_search_thema.get_config", return_value=mock_config),
        patch("crawler_search_thema.setup_logger", return_value=Mock(name="logger")),
    ):
        crawler = SearchThemaCrawler()

    result = asyncio.run(crawler._fallback_items_from_error(ValueError("bad json"), None, 2024, None, 1))
    assert result is None
