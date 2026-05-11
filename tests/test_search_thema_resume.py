import asyncio
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


@pytest.fixture
def search_thema_item() -> dict:
    return {
        "stored_toc_seq": "I0000000000000001734498102442000",
        "stored_field_subject": "정부공직자윤리위원회고시제2024-1호",
        "stored_category_name": "고시",
        "stored_organ_nm": "정부공직자윤리위원회",
        "stored_field_url": (
            "/ezpdf/customLayout.jsp?contentId=I0000000000000001735535299326000"
            "&tocId=I0000000000000001734498102442000&isTocOrder=N"
        ),
        "stored_field_year": "2024",
        "stored_field_month": "12",
        "stored_field_day": "31",
    }


@pytest.fixture
def crawler_factory(tmp_data_dir: Path, mock_config: Mock):
    def _make_crawler(
        *,
        metadata_only: bool = True,
        resume: bool = True,
        limit: int | None = None,
        years: list[str] | None = None,
        institutions: list[str | None] | None = None,
        save_indexes: bool = False,
    ):
        metadata_manager = Mock(name="MetadataManager")
        metadata_manager.items = {}
        metadata_manager.load_item.return_value = None
        state = Mock(name="CrawlState")
        state.is_search_thema_completed.return_value = False

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
                    "institutions": ["정부공직자윤리위원회"],
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
            patch("crawler_search_thema.setup_logger", return_value=Mock(name="logger")),
            patch("crawler_search_thema.MetadataManager", return_value=metadata_manager),
            patch("crawler_search_thema.CrawlState", return_value=state),
        ):
            crawler = SearchThemaCrawler(
                metadata_only=metadata_only,
                resume=resume,
                limit=limit,
                years=years,
                institutions=institutions,
                save_indexes=save_indexes,
            )

        return crawler, metadata_manager, state

    return _make_crawler


def test_skip_completed_combination(crawler_factory) -> None:
    crawler, _metadata_manager, state = crawler_factory(years=["2024"])
    state.is_search_thema_completed.return_value = True

    assert crawler._should_skip_combination("2024", "정부공직자윤리위원회") is True

    state.is_search_thema_completed.assert_called_once_with("2024", "정부공직자윤리위원회", "metadata")
    assert crawler.stats["skipped_combinations"] == 1


def test_skip_existing_item(crawler_factory, search_thema_item: dict) -> None:
    crawler, metadata_manager, _state = crawler_factory(metadata_only=True)
    existing = {"id": search_thema_item["stored_toc_seq"], "pdf": {"status": "skipped"}}
    metadata_manager.get_item.return_value = existing

    assert crawler._should_skip_item(search_thema_item) is True

    metadata_manager.get_item.assert_called_once_with(search_thema_item["stored_toc_seq"])
    metadata_manager.add_item.assert_called_once_with(existing)
    assert crawler.stats["skipped_items"] == 1


def test_skip_completed_pdf(crawler_factory, search_thema_item: dict, tmp_path: Path) -> None:
    crawler, metadata_manager, _state = crawler_factory(metadata_only=False)
    pdf_path = tmp_path / "completed.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    metadata_manager.get_item.return_value = {
        "id": search_thema_item["stored_toc_seq"],
        "pdf": {"status": "completed", "path": str(pdf_path)},
    }

    assert crawler._should_skip_item(search_thema_item) is True

    assert crawler.stats["skipped_items"] == 1


def test_process_new_item(crawler_factory, search_thema_item: dict) -> None:
    crawler, metadata_manager, _state = crawler_factory(metadata_only=True)
    metadata_manager.get_item.return_value = None

    processed = asyncio.run(crawler._process_item(None, search_thema_item))

    assert processed is True
    assert crawler.stats["total_items"] == 1
    assert crawler.stats["saved_items"] == 1
    saved_item = metadata_manager.save_item.call_args.args[0]
    assert saved_item["id"] == search_thema_item["stored_toc_seq"]
    assert saved_item["status"] == "metadata_only"
    assert saved_item["pdf"]["status"] == "skipped"


def test_resume_interrupted_crawl(crawler_factory, search_thema_item: dict) -> None:
    crawler, metadata_manager, state = crawler_factory(
        metadata_only=True,
        years=["2024"],
        institutions=["정부공직자윤리위원회"],
        save_indexes=False,
    )
    metadata_manager.get_item.return_value = None

    with (
        patch.object(crawler, "fetch_items", new=AsyncMock(side_effect=[[search_thema_item], []])),
        patch.object(crawler, "_sleep", new=AsyncMock()),
    ):
        stats = asyncio.run(crawler.crawl())

    assert stats["completed_combinations"] == 1
    assert stats["saved_items"] == 1
    state.mark_search_thema_completed.assert_called_once()
    args = state.mark_search_thema_completed.call_args.args
    assert args[0:3] == ("2024", "정부공직자윤리위원회", "metadata")
    assert args[3]["saved_items"] == 1


def test_failed_combination_remains_resumable(crawler_factory) -> None:
    crawler, _metadata_manager, state = crawler_factory(
        metadata_only=False,
        years=["2024"],
        institutions=["정부공직자윤리위원회"],
        save_indexes=False,
    )

    with patch.object(crawler, "fetch_items", new=AsyncMock(side_effect=RuntimeError("Server disconnected"))):
        stats = asyncio.run(crawler.crawl())

    assert stats["completed_combinations"] == 0
    assert stats["failed_combinations"] == 1
    state.mark_search_thema_completed.assert_not_called()
