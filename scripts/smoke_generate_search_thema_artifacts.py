#!/usr/bin/env python3
"""네트워크 없이 SearchThema 메타데이터/PDF 아티팩트 생성 스모크."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.crawler_search_thema import SearchThemaCrawler


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "search_thema_single_page.json"


def _load_first_item() -> dict:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for entry in payload.get("data") or []:
        items = entry.get("list") or []
        if items:
            return items[0]
    raise RuntimeError("fixture에 SearchThema 아이템이 없습니다.")


async def _run() -> None:
    item = _load_first_item()
    crawler = SearchThemaCrawler(
        metadata_only=False,
        years=["2024"],
        institutions=["정부공직자윤리위원회"],
        limit=1,
        resume=False,
    )
    pdf_body = b"%PDF-1.4 smoke"

    async def fake_download(download_item: dict) -> dict:
        pdf_path = crawler._pdf_path_for_item(download_item)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(pdf_body)
        return {
            "status": "completed",
            "path": str(pdf_path),
            "size_bytes": len(pdf_body),
            "sha256": hashlib.sha256(pdf_body).hexdigest(),
            "downloaded_at": "2026-05-10T00:00:00",
        }

    with (
        patch.object(crawler, "fetch_items", new=AsyncMock(side_effect=[[item], []])),
        patch.object(crawler, "_download_pdf_via_http", side_effect=fake_download),
        patch.object(crawler, "_sleep", new=AsyncMock()),
    ):
        stats = await crawler.crawl()

    print("smoke stats:", stats)
    print("metadata dir:", crawler.metadata_manager.metadata_dir)
    print("pdf dir:", crawler.pdf_dir)


if __name__ == "__main__":
    asyncio.run(_run())
