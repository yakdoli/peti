"""Pytest fixtures and test helpers."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Provide a temporary data directory for tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


@pytest.fixture
def mock_config() -> Mock:
    """Provide a mock Config object for tests."""
    config = Mock(name="ConfigMock")
    config.config_file = "tests/fixtures/mock_config.yaml"
    config.config = {
        "crawler": {},
        "download": {},
        "metadata": {},
        "logging": {},
        "ocr": {},
    }

    def _get(key: str, default=None):
        value = config.config
        for part in key.split("."):
            if isinstance(value, dict):
                value = value.get(part, default)
            else:
                return default
        return value

    config.get.side_effect = _get
    config.get_crawler_config.return_value = config.config["crawler"]
    config.get_download_config.return_value = config.config["download"]
    config.get_metadata_config.return_value = config.config["metadata"]
    config.get_logging_config.return_value = config.config["logging"]
    config.get_ocr_config.return_value = config.config["ocr"]
    return config


@pytest.fixture
def load_fixture():
    """Return a helper that loads JSON fixtures by name."""

    def _load_fixture(name: str):
        fixture_path = Path(__file__).parent / "fixtures" / f"{name}.json"
        try:
            with fixture_path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Fixture not found: {fixture_path}") from exc

    return _load_fixture
