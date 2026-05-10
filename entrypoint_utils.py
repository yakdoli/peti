"""Shared helpers for command-line entrypoints."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def add_project_paths() -> None:
    """Ensure the repository root and src/ are importable."""
    root = str(PROJECT_ROOT)
    src = str(PROJECT_ROOT / "src")
    if root not in sys.path:
        sys.path.insert(0, root)
    if src not in sys.path:
        sys.path.insert(0, src)


def configure_windows_asyncio_policy() -> None:
    """Use the selector policy on Windows so subprocess-based CLIs behave consistently."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def log_stats(logger, title: str, stats: dict) -> None:
    """Log a consistent stats block for CLI entrypoints."""
    logger.info("=" * 60)
    logger.info(title)
    logger.info("=" * 60)
    for key, value in stats.items():
        logger.info(f"  {key}: {value}")