import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock, patch

import pytest


if "aiohttp" not in sys.modules:
    aiohttp_stub = ModuleType("aiohttp")
    setattr(aiohttp_stub, "ClientTimeout", Mock(name="ClientTimeout"))
    setattr(aiohttp_stub, "ClientSession", Mock(name="ClientSession"))
    sys.modules["aiohttp"] = aiohttp_stub

from crawler_search_thema import SearchThemaCrawler  # type: ignore[reportMissingImports]


INSTITUTIONS = [
    "정부공직자윤리위원회",
    "대법원공직자윤리위원회",
    "중앙선거관리위원회공직자윤리위원회",
]


@pytest.fixture
def search_thema_items(load_fixture) -> list[dict]:
    payload = load_fixture("search_thema_single_page")
    items: list[dict] = []
    for entry in payload.get("data") or []:
        items.extend(entry.get("list") or [])
    return items


@pytest.fixture
def e2e_crawler_factory(tmp_data_dir: Path, mock_config: Mock):
    def _make_crawler(
        *,
        metadata_only: bool = True,
        years: list[str] | None = None,
        institutions: list[str | None] | None = None,
        limit: int | None = None,
        save_indexes: bool = False,
    ) -> SearchThemaCrawler:
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
                    "year_start": 2024,
                    "year_end": 2024,
                    "institutions": INSTITUTIONS,
                    "institution_query_map": {},
                }
            },
        }
        mock_config.config["download"] = {
            "pdf_directory": str(tmp_data_dir / "pdfs"),
            "metadata_directory": str(tmp_data_dir / "metadata"),
            "chunk_size": 8192,
        }
        mock_config.config["state"] = {"file": str(tmp_data_dir / "state" / "crawl_state.json")}
        mock_config.get_crawler_config.return_value = mock_config.config["crawler"]
        mock_config.get_download_config.return_value = mock_config.config["download"]
        mock_config.get_search_thema_config.return_value = mock_config.config["crawler"]["themes"]["searchThema"]

        with (
            patch("crawler_search_thema.get_config", return_value=mock_config),
            patch("crawler_search_thema.setup_logger", return_value=Mock(name="crawler_logger")),
            patch("metadata_manager.get_config", return_value=mock_config),
            patch("metadata_manager.setup_logger", return_value=Mock(name="metadata_logger")),
        ):
            return SearchThemaCrawler(
                metadata_only=metadata_only,
                years=years,
                institutions=institutions,
                limit=limit,
                save_indexes=save_indexes,
                state_file=str(tmp_data_dir / "state" / "crawl_state.json"),
            )

    return _make_crawler


def _saved_item_path(crawler: SearchThemaCrawler, item_id: str) -> Path:
    return crawler.metadata_manager.items_dir / "2024" / "20241231" / f"{item_id}.json"


def test_full_metadata_flow(e2e_crawler_factory, search_thema_items: list[dict], tmp_data_dir: Path) -> None:
    item = search_thema_items[0]
    crawler = e2e_crawler_factory(
        metadata_only=True,
        years=["2024"],
        institutions=["정부공직자윤리위원회"],
        limit=1,
    )

    with (
        patch.object(crawler, "fetch_items", new=AsyncMock(side_effect=[[item], []])),
        patch.object(crawler, "_sleep", new=AsyncMock()),
    ):
        stats = asyncio.run(crawler.crawl())

    item_path = _saved_item_path(crawler, item["stored_toc_seq"])
    saved_item = json.loads(item_path.read_text(encoding="utf-8"))
    assert stats["total_items"] == 1
    assert stats["saved_items"] == 1
    assert saved_item["id"] == item["stored_toc_seq"]
    assert saved_item["theme"] == "searchThema"
    assert saved_item["status"] == "metadata_only"
    assert saved_item["pdf"]["status"] == "skipped"
    assert str(crawler.metadata_manager.metadata_dir).startswith(str(tmp_data_dir))


def test_full_pdf_flow(e2e_crawler_factory, search_thema_items: list[dict]) -> None:
    item = search_thema_items[0]
    crawler = e2e_crawler_factory(
        metadata_only=False,
        years=["2024"],
        institutions=["정부공직자윤리위원회"],
        limit=1,
    )
    pdf_body = b"%PDF-1.4 e2e"

    async def fake_download(download_item: dict) -> dict:
        pdf_path = crawler._pdf_path_for_item(download_item)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(pdf_body)
        return {
            "status": "completed",
            "path": str(pdf_path),
            "size_bytes": len(pdf_body),
            "sha256": hashlib.sha256(pdf_body).hexdigest(),
            "downloaded_at": "2026-05-08T00:00:00",
        }

    with (
        patch.object(crawler, "fetch_items", new=AsyncMock(side_effect=[[item], []])),
        patch.object(crawler, "_download_pdf_via_http", side_effect=fake_download),
        patch.object(crawler, "_sleep", new=AsyncMock()),
    ):
        stats = asyncio.run(crawler.crawl())

    saved_item = json.loads(_saved_item_path(crawler, item["stored_toc_seq"]).read_text(encoding="utf-8"))
    pdf_path = Path(saved_item["pdf"]["path"])
    assert stats["downloaded_pdfs"] == 1
    assert saved_item["pdf"]["status"] == "completed"
    assert pdf_path.read_bytes().startswith(b"%PDF-")


def test_resume_flow(e2e_crawler_factory, search_thema_items: list[dict]) -> None:
    item = search_thema_items[0]
    first_crawler = e2e_crawler_factory(
        metadata_only=True,
        years=["2024"],
        institutions=["정부공직자윤리위원회"],
    )
    with (
        patch.object(first_crawler, "fetch_items", new=AsyncMock(side_effect=[[item], []])),
        patch.object(first_crawler, "_sleep", new=AsyncMock()),
    ):
        first_stats = asyncio.run(first_crawler.crawl())

    second_crawler = e2e_crawler_factory(
        metadata_only=True,
        years=["2024"],
        institutions=["정부공직자윤리위원회"],
    )
    fetch_items = AsyncMock()
    with patch.object(second_crawler, "fetch_items", new=fetch_items):
        second_stats = asyncio.run(second_crawler.crawl())

    assert first_stats["completed_combinations"] == 1
    assert second_stats["skipped_combinations"] == 1
    fetch_items.assert_not_awaited()


def test_empty_year_graceful(e2e_crawler_factory) -> None:
    crawler = e2e_crawler_factory(
        metadata_only=True,
        years=["1993"],
        institutions=["정부공직자윤리위원회"],
    )
    with (
        patch.object(crawler, "fetch_items", new=AsyncMock(return_value=[])),
        patch.object(crawler, "_sleep", new=AsyncMock()),
    ):
        stats = asyncio.run(crawler.crawl())

    assert stats["total_items"] == 0
    assert stats["saved_items"] == 0
    assert stats["completed_combinations"] == 1


def test_all_institutions_flow(e2e_crawler_factory) -> None:
    crawler = e2e_crawler_factory(metadata_only=True, years=["2024"])
    fetch_items = AsyncMock(side_effect=[[], [], [], []])
    with (
        patch.object(crawler, "fetch_items", new=fetch_items),
        patch.object(crawler, "_sleep", new=AsyncMock()),
    ):
        stats = asyncio.run(crawler.crawl())

    institutions = [call.args[1] for call in fetch_items.await_args_list]
    assert institutions == [None, *INSTITUTIONS]
    assert stats["completed_combinations"] == 4
