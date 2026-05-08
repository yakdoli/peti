from src.crawl_state import CrawlState


def test_search_thema_key_format(tmp_path):
    state = CrawlState(state_file=str(tmp_path / "crawl_state.json"))

    assert state._search_thema_key("2024", "정부공직자윤리위원회", "pdf") == "searchThema:2024:정부공직자윤리위원회:pdf"


def test_search_thema_mark_and_check(tmp_path):
    state = CrawlState(state_file=str(tmp_path / "crawl_state.json"))

    stats = {"count": 3, "status": "done"}
    state.mark_search_thema_completed("2024", "정부공직자윤리위원회", "pdf", stats)

    assert state.is_search_thema_completed("2024", "정부공직자윤리위원회", "pdf") is True
    assert state.state["completed_windows"]["searchThema:2024:정부공직자윤리위원회:pdf"] == stats


def test_search_thema_mode_variants(tmp_path):
    state = CrawlState(state_file=str(tmp_path / "crawl_state.json"))

    state.mark_search_thema_completed("2024", "정부공직자윤리위원회", "pdf", {"count": 1})

    assert state.is_search_thema_completed("2024", "정부공직자윤리위원회", "pdf") is True
    assert state.is_search_thema_completed("2024", "정부공직자윤리위원회", "metadata") is False

    state.mark_search_thema_completed("2024", "정부공직자윤리위원회", "metadata", {"count": 2})

    assert state.is_search_thema_completed("2024", "정부공직자윤리위원회", "metadata") is True
    assert state.state["completed_windows"]["searchThema:2024:정부공직자윤리위원회:metadata"] == {"count": 2}


def test_legacy_window_key_still_works(tmp_path):
    state = CrawlState(state_file=str(tmp_path / "crawl_state.json"))

    state.mark_window_completed("pety", "2024-01-01", "2024-01-31", {"count": 5}, "pdf")

    assert state._window_key("pety", "2024-01-01", "2024-01-31", "pdf") == "pety:pdf:2024-01-01:2024-01-31"
    assert state.is_window_completed("pety", "2024-01-01", "2024-01-31", "pdf") is True
