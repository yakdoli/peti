import argparse
import importlib.util
from pathlib import Path

import pytest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    module_path = Path(__file__).resolve().parents[1] / "crawl_search_thema.py"
    spec = importlib.util.spec_from_file_location("crawl_search_thema", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = getattr(module, "parse_args")
    return parser(argv)


def test_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    for option in (
        "--year",
        "--institution",
        "--metadata-only",
        "--limit",
        "--resume",
        "--no-resume",
        "--rebuild-index",
        "--state-file",
    ):
        assert option in output


def test_cli_default_args() -> None:
    args = parse_args([])

    assert args.year is None
    assert args.institution is None
    assert args.metadata_only is False
    assert args.limit is None
    assert args.resume is True
    assert args.headed is False
    assert args.rebuild_index is False
    assert args.state_file is None


def test_cli_year_only() -> None:
    args = parse_args(["--year", "2025"])

    assert args.year == "2025"
    assert args.institution is None


def test_cli_institution_filter() -> None:
    args = parse_args([
        "--institution",
        "정부공직자윤리위원회",
        "--metadata-only",
        "--limit",
        "3",
        "--no-resume",
    ])

    assert args.institution == "정부공직자윤리위원회"
    assert args.metadata_only is True
    assert args.limit == 3
    assert args.resume is False
